---
title: "[PostgreSQL] 데드락은 어떻게 생기고 어떻게 읽나"
date: '2026-09-17 03:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '잠금', 'Deadlock', '트랜잭션', '트러블슈팅']
series: ['PostgreSQL 잠금과 대기']
description: "데드락은 두 트랜잭션이 서로가 가진 것을 기다리는 상태입니다. PostgreSQL이 이를 감지해 한쪽을 끊는 방식, 로그에서 원인을 읽는 법, 그리고 외래키 때문에 생기는 예상 밖의 데드락까지 정리합니다."
faq:
  - q: "PostgreSQL은 데드락을 어떻게 처리하나요?"
    a: "잠금 대기가 deadlock_timeout(기본 1초)을 넘으면 대기 그래프에서 순환을 찾는 검사를 실행합니다. 순환이 발견되면 관련 트랜잭션 중 하나를 에러 코드 40P01로 강제 종료해 나머지가 진행되게 합니다. 데드락은 예방되는 것이 아니라 감지 후 한쪽을 희생시켜 해소됩니다."
  - q: "데드락 로그는 어떻게 읽나요?"
    a: "로그의 DETAIL 줄에 'Process A waits for ... blocked by process B' 형태로 대기 순환이 나열되고, 각 프로세스가 실행 중이던 문장이 함께 기록됩니다. 두 프로세스가 같은 테이블의 행을 서로 반대 순서로 잠근 경우가 대부분이며, 어떤 행을 어떤 순서로 건드렸는지가 원인입니다."
  - q: "외래키가 데드락을 일으킬 수 있나요?"
    a: "네. 자식 테이블에 행을 삽입하면 참조하는 부모 행에 KEY SHARE 잠금이 걸립니다. 같은 시점에 다른 트랜잭션이 그 부모 행의 키 컬럼을 갱신하려 하면 충돌합니다. 부모의 키가 아닌 컬럼만 갱신하면 FOR NO KEY UPDATE 잠금이라 충돌하지 않으므로, 갱신하는 컬럼이 무엇인지에 따라 결과가 달라집니다."
---

[지난 편](/posts/postgresql-lock-1-ddl-queue/)이 한 줄로 늘어선 대기였다면, 이번 편은 대기가 원을 그리는 경우다.

## 서로를 기다리면 끝나지 않는다

트랜잭션 A가 행 1을 잠그고 행 2를 기다린다. 트랜잭션 B는 행 2를 잠그고 행 1을 기다린다. 둘 다 상대가 끝나야 진행할 수 있고, 상대는 내가 끝나야 진행할 수 있다.

```
A: UPDATE accounts SET ... WHERE id = 1;   -- 행 1 잠금
B: UPDATE accounts SET ... WHERE id = 2;   -- 행 2 잠금
A: UPDATE accounts SET ... WHERE id = 2;   -- B가 끝나길 대기
B: UPDATE accounts SET ... WHERE id = 1;   -- A가 끝나길 대기 → 데드락
```

PostgreSQL은 이걸 막지 않는다. 대신 감지한다. 잠금 대기가 `deadlock_timeout`(기본 1초)을 넘으면 대기 그래프를 검사하고, 순환이 있으면 그중 하나를 끊는다.

```
ERROR:  deadlock detected
DETAIL:  Process 12345 waits for ShareLock on transaction 67890; blocked by process 12346.
         Process 12346 waits for ShareLock on transaction 67891; blocked by process 12345.
HINT:  See server log for query details.
```

끊긴 쪽은 에러 코드 `40P01`을 받는다. 살아남은 쪽은 아무 일 없었다는 듯 진행한다. 어느 쪽이 끊기는지는 정해져 있지 않다.

## 로그를 읽는 법

서버 로그에는 더 자세히 남는다.

```
ERROR:  deadlock detected
DETAIL:  Process 12345 waits for ShareLock on transaction 67890; blocked by process 12346.
         Process 12346 waits for ShareLock on transaction 67891; blocked by process 12345.
         Process 12345: UPDATE accounts SET balance = balance - 100 WHERE id = 2
         Process 12346: UPDATE accounts SET balance = balance + 100 WHERE id = 1
CONTEXT: while updating tuple (0,3) in relation "accounts"
```

읽는 순서는 이렇다.

1. DETAIL의 `Process X waits for ... blocked by Y` 줄로 순환을 확인한다. 두 줄이면 2자 순환, 세 줄이면 3자다.
2. 각 프로세스의 마지막 문장을 본다. 그게 대기하다 걸린 문장이다.
3. 그 문장 앞에 각 트랜잭션이 무엇을 잠갔는지는 로그에 없다. 애플리케이션 코드에서 그 트랜잭션이 어떤 순서로 무엇을 건드리는지 추적해야 한다.

3번이 실제 작업의 대부분이다. 로그는 "어디서 막혔나"를 알려주지 "왜 서로 반대로 잡았나"를 알려주지 않는다.

`log_lock_waits = on`을 켜두면 데드락까지 안 가고 `deadlock_timeout`을 넘긴 대기도 로그에 남는다. 데드락 직전 상태를 미리 보는 용도로 쓸 만하다.

## 흔한 원인 세 가지

**같은 행들을 반대 순서로 갱신한다.** 위의 예가 그렇다. 송금처럼 두 행을 한 트랜잭션에서 건드리는 로직에서 자주 나온다. 해결은 순서를 고정하는 것이다. 항상 id가 작은 쪽부터 잠근다.

```sql
SELECT * FROM accounts WHERE id IN (1, 2) ORDER BY id FOR UPDATE;
```

`ORDER BY`가 핵심이다. `IN` 절의 순서는 보장되지 않는다.

**배치 UPDATE가 서로 다른 순서로 행을 훑는다.** 두 배치가 같은 테이블을 갱신하는데 하나는 인덱스 순으로, 하나는 물리 순으로 훑으면 중간에서 만난다. 배치도 `ORDER BY`를 넣거나, 아예 같은 시간에 돌지 않게 한다.

**외래키.** 이건 예상 못 하는 경우가 많다.

## 외래키가 만드는 데드락

자식 테이블에 INSERT하면 참조하는 부모 행에 잠금이 걸린다. 부모가 사라지면 안 되니까. 이 잠금이 `FOR KEY SHARE`다.

같은 시점에 다른 트랜잭션이 그 부모 행을 UPDATE하면 어떻게 되나. 갱신하는 컬럼에 따라 다르다.

| 부모 행 UPDATE | 잠금 | 자식 INSERT의 KEY SHARE와 |
|---|---|---|
| 키가 아닌 컬럼만 | FOR NO KEY UPDATE | 충돌 없음 |
| 키 컬럼 포함 | FOR UPDATE | 충돌 |

키 컬럼이란 유니크 인덱스에 포함된 컬럼이다. PK를 갱신하는 일은 드물지만, 유니크 제약이 걸린 다른 컬럼을 갱신하면 같은 일이 생긴다.

그래서 이런 시나리오가 나온다.

```
A: INSERT INTO orders (user_id) VALUES (10);   -- users.id=10 에 KEY SHARE
B: UPDATE users SET email = ... WHERE id = 10; -- email 이 유니크 → FOR UPDATE → A 대기
A: UPDATE users SET last_seen = now() WHERE id = 10;  -- B 대기 → 데드락
```

A는 주문을 넣고 유저의 마지막 접속 시각을 갱신했을 뿐이고, B는 이메일을 바꿨을 뿐이다. 둘 다 자기 코드만 보면 아무 문제가 없다.

대응은 두 가지다. 유니크 컬럼 갱신을 트랜잭션 맨 앞으로 옮기거나, 자식 INSERT와 부모 UPDATE를 같은 트랜잭션에서 하지 않거나.

## 애플리케이션 쪽 대응

데드락은 완전히 없애기 어렵다. 그래서 두 가지를 같이 한다.

첫째, 트랜잭션을 짧게 유지한다. 잠금을 오래 잡고 있을수록 순환에 걸릴 확률이 올라간다. 트랜잭션 안에서 외부 API를 부르거나 사용자 입력을 기다리는 구조는 그 자체로 위험하다.

둘째, `40P01`을 받으면 재시도한다. 데드락으로 끊긴 트랜잭션은 실패한 게 아니라 양보한 것이다. 처음부터 다시 실행하면 대개 성공한다. 재시도 횟수는 제한하고, 그 사이 잠깐 랜덤하게 쉰다.

```python
for attempt in range(3):
    try:
        with conn.transaction():
            ...
        break
    except DeadlockDetected:
        time.sleep(random.uniform(0.05, 0.2))
```

재시도가 안전하려면 트랜잭션이 멱등해야 한다. 외부 부작용이 트랜잭션 안에 있으면 재시도가 그걸 두 번 실행한다.

## 정리하면

데드락은 잠금 순서의 문제다. 로그는 어디서 걸렸는지만 알려주고, 왜 반대 순서가 됐는지는 코드에서 찾아야 한다. 대부분은 `ORDER BY`로 순서를 고정하면 사라지고, 외래키가 얽힌 경우는 어떤 컬럼을 갱신하는지를 봐야 한다.

다음 편은 잠금을 기다리는 대신 건너뛰는 방법, `SKIP LOCKED`로 작업 큐를 만드는 이야기다.

## 자주 묻는 질문

**Q. PostgreSQL은 데드락을 어떻게 처리하나요?**

잠금 대기가 `deadlock_timeout`(기본 1초)을 넘으면 대기 그래프에서 순환을 찾는 검사를 실행합니다. 순환이 발견되면 관련 트랜잭션 중 하나를 에러 코드 40P01로 강제 종료해 나머지가 진행되게 합니다. 데드락은 예방되는 것이 아니라 감지 후 한쪽을 희생시켜 해소됩니다.

**Q. 데드락 로그는 어떻게 읽나요?**

로그의 DETAIL 줄에 "Process A waits for ... blocked by process B" 형태로 대기 순환이 나열되고, 각 프로세스가 실행 중이던 문장이 함께 기록됩니다. 두 프로세스가 같은 테이블의 행을 서로 반대 순서로 잠근 경우가 대부분이며, 어떤 행을 어떤 순서로 건드렸는지가 원인입니다.

**Q. 외래키가 데드락을 일으킬 수 있나요?**

네. 자식 테이블에 행을 삽입하면 참조하는 부모 행에 KEY SHARE 잠금이 걸립니다. 같은 시점에 다른 트랜잭션이 그 부모 행의 키 컬럼을 갱신하려 하면 충돌합니다. 부모의 키가 아닌 컬럼만 갱신하면 FOR NO KEY UPDATE 잠금이라 충돌하지 않으므로, 갱신하는 컬럼이 무엇인지에 따라 결과가 달라집니다.
