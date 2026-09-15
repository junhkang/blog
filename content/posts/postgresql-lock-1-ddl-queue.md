---
title: "[PostgreSQL] ALTER TABLE 하나가 서비스를 멈추는 구조"
date: '2026-09-16 03:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '잠금', 'Lock', 'DDL', '운영', 'lock_timeout']
series: ['PostgreSQL 잠금과 대기']
description: "ALTER TABLE은 대부분 ACCESS EXCLUSIVE 잠금을 요구하고, 그 잠금을 기다리는 동안 뒤에 오는 SELECT까지 줄을 서게 됩니다. 긴 조회 하나와 DDL 하나가 만나면 테이블 전체가 멈추는 이유와, lock_timeout으로 피하는 방법을 정리합니다."
faq:
  - q: "ALTER TABLE을 실행했더니 관련 없는 SELECT까지 멈추는 이유는 무엇인가요?"
    a: "PostgreSQL은 잠금 요청을 도착 순서대로 처리합니다. ALTER TABLE이 ACCESS EXCLUSIVE 잠금을 기다리는 동안, 그 뒤에 도착한 SELECT의 ACCESS SHARE 요청은 앞의 ACCESS EXCLUSIVE와 충돌하므로 함께 대기합니다. 즉 실행 중인 긴 쿼리 하나가 ALTER를 막고, 그 ALTER가 이후의 모든 접근을 막는 연쇄가 생깁니다."
  - q: "lock_timeout은 어떻게 쓰나요?"
    a: "잠금을 얻기 위해 기다리는 시간의 상한입니다. SET lock_timeout = '2s' 뒤에 DDL을 실행하면 2초 안에 잠금을 못 얻을 때 문장이 취소되고, 뒤에 줄 선 요청들은 즉시 풀립니다. 실패하면 잠시 후 재시도하는 방식으로 운영 중 DDL을 안전하게 적용할 수 있습니다."
  - q: "잠금 없이 할 수 있는 스키마 변경은 무엇인가요?"
    a: "기본값이 없거나 상수 기본값인 컬럼 추가는 PostgreSQL 11부터 테이블을 다시 쓰지 않아 매우 짧은 잠금만 필요합니다. 인덱스는 CREATE INDEX CONCURRENTLY로, 제약조건은 NOT VALID로 추가한 뒤 VALIDATE CONSTRAINT로 검증하면 긴 배타 잠금을 피할 수 있습니다."
---

컬럼 하나 추가하는 ALTER TABLE을 돌렸는데 서비스가 멈췄다는 이야기는 흔하다. 원인은 대개 ALTER 자체가 아니다. ALTER가 잠금을 기다리는 동안 그 뒤로 줄이 생기는 구조 때문이다.

## 잠금은 줄을 선다

PostgreSQL의 테이블 잠금은 여덟 단계가 있고, 서로 충돌하는 조합이 정해져 있다. 실무에서 기억할 건 두 가지면 된다.

- SELECT는 ACCESS SHARE를 잡는다. 거의 아무것과도 충돌하지 않는다.
- 대부분의 ALTER TABLE은 ACCESS EXCLUSIVE를 잡는다. 모든 것과 충돌한다. SELECT와도.

문제는 잠금 요청이 도착 순서대로 처리된다는 점이다. 앞의 요청이 못 받고 있으면 뒤의 요청도 기다린다. 뒤의 요청이 앞의 것과 충돌하는 종류라면 그렇다.

이걸 시간 순서로 놓으면 이렇게 된다.

```
t0  긴 SELECT 시작           ACCESS SHARE 획득, 3분 걸림
t1  ALTER TABLE 실행         ACCESS EXCLUSIVE 요청 → SELECT 끝날 때까지 대기
t2  새 SELECT 도착           ACCESS SHARE 요청 → 앞의 ACCESS EXCLUSIVE와 충돌 → 대기
t3  또 새 SELECT 도착        대기
...
```

t1 이후에 들어온 모든 조회가 멈춘다. 그 조회들은 ALTER와 아무 관계가 없고, t0의 긴 쿼리와도 관계가 없다. 그냥 줄 뒤에 섰을 뿐이다.

ALTER 자체는 밀리초면 끝나는 작업이다. 그런데 t0의 쿼리가 3분 걸리면 서비스는 3분 멈춘다. 이게 "컬럼 하나 추가했는데 장애"의 실체다.

## 지금 누가 누구를 막고 있나

멈춘 순간에 볼 것은 이것이다.

```sql
SELECT pid,
       state,
       wait_event_type,
       now() - xact_start          AS xact_age,
       pg_blocking_pids(pid)       AS blocked_by,
       left(query, 60)             AS query
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0
   OR pid IN (SELECT unnest(pg_blocking_pids(pid)) FROM pg_stat_activity)
ORDER BY xact_age DESC;
```

`blocked_by`가 비어 있으면서 다른 행들의 `blocked_by`에 등장하는 pid가 맨 앞에 있는 놈이다. 대개 오래된 트랜잭션이고, `idle in transaction` 상태인 경우도 많다.

풀려면 그 pid를 끊는다.

```sql
SELECT pg_cancel_backend(<pid>);     -- 쿼리만 취소
SELECT pg_terminate_backend(<pid>);  -- 세션 종료
```

ALTER를 취소하는 게 더 안전한 경우도 있다. ALTER가 사라지면 줄이 즉시 풀린다.

## lock_timeout

사후 대응보다 중요한 건 애초에 줄이 안 생기게 하는 것이다.

```sql
SET lock_timeout = '2s';
ALTER TABLE orders ADD COLUMN note text;
```

잠금을 2초 안에 못 얻으면 ALTER가 에러로 끝나고, 그 뒤에 줄 서 있던 요청들은 바로 풀린다. 서비스 입장에서는 최대 2초의 지연만 생긴다.

그러면 ALTER는 어떻게 하나. 재시도한다.

```
loop:
  SET lock_timeout = '2s'
  ALTER TABLE ...
  성공 → 끝
  실패 → 몇 초 쉬고 다시
```

긴 쿼리가 끝나는 틈에 들어가면 성공한다. 마이그레이션 도구 대부분이 이 패턴을 지원하거나, 없으면 직접 감싸면 된다.

기본값은 0, 즉 무제한 대기다. 운영 DB에서 DDL을 실행하는 역할에는 기본으로 걸어두는 게 낫다.

```sql
ALTER ROLE migrator SET lock_timeout = '3s';
```

`statement_timeout`과는 다르다. 그건 쿼리 실행 시간 상한이고, `lock_timeout`은 잠금 대기 시간 상한이다. 둘 다 걸려 있으면 먼저 닿는 쪽이 끊는다.

## 애초에 무거운 잠금이 필요 없는 방법

같은 결과를 더 약한 잠금으로 얻을 수 있는 경우가 많다.

컬럼 추가는 PostgreSQL 11부터 기본값이 상수면 테이블을 다시 쓰지 않는다. 메타데이터만 바뀌고 잠금은 순간이다. 기본값이 `now()` 같은 휘발성 함수면 다시 쓴다.

```sql
ALTER TABLE t ADD COLUMN flag boolean DEFAULT false;   -- 빠름 (11+)
ALTER TABLE t ADD COLUMN ts timestamptz DEFAULT now(); -- 전체 재작성
```

인덱스는 `CONCURRENTLY`를 쓴다. SHARE UPDATE EXCLUSIVE만 잡아서 읽기와 쓰기를 막지 않는다. 대신 두 번 스캔하고, 실패하면 INVALID 인덱스가 남으니 확인 후 지워야 한다.

```sql
CREATE INDEX CONCURRENTLY idx_orders_status ON orders (status);
```

제약조건은 둘로 쪼갠다. `NOT VALID`로 추가하면 기존 행을 검사하지 않아 즉시 끝나고, 새로 들어오는 행만 검사한다. 그 다음 `VALIDATE`가 기존 행을 훑는데, 이건 SHARE UPDATE EXCLUSIVE라 서비스를 막지 않는다.

```sql
ALTER TABLE orders ADD CONSTRAINT fk_user
  FOREIGN KEY (user_id) REFERENCES users (id) NOT VALID;

ALTER TABLE orders VALIDATE CONSTRAINT fk_user;
```

NOT NULL 추가는 PostgreSQL 12부터 같은 조건의 CHECK 제약이 이미 검증돼 있으면 스캔을 건너뛴다. CHECK를 NOT VALID → VALIDATE로 먼저 만들고 NOT NULL을 붙이면 전체 스캔 잠금을 피할 수 있다.

컬럼 타입 변경은 대부분 전체 재작성이다. `varchar(50)`을 `varchar(100)`으로 늘리는 것처럼 바이너리 호환인 경우만 예외다. 타입을 바꿔야 하면 새 컬럼을 만들고 배치로 옮긴 뒤 교체하는 편이 안전하다.

## 정리하면

ALTER가 느린 게 아니라, ALTER가 기다리는 동안 뒤에 줄이 생기는 게 문제다. 그래서 대응은 두 방향이다. 기다리지 않게 하거나(`lock_timeout` + 재시도), 애초에 강한 잠금이 필요 없는 방법을 고르거나.

운영 DB에 DDL을 넣을 때 확인하는 순서는 이렇다. 이 변경이 어떤 잠금을 잡는가, 그 잠금이 필요 없는 대안이 있는가, 없다면 `lock_timeout`이 걸려 있는가.

다음 편은 잠금이 서로를 기다리다 영원히 안 풀리는 경우, 데드락이다.

## 자주 묻는 질문

**Q. ALTER TABLE을 실행했더니 관련 없는 SELECT까지 멈추는 이유는 무엇인가요?**

PostgreSQL은 잠금 요청을 도착 순서대로 처리합니다. ALTER TABLE이 ACCESS EXCLUSIVE 잠금을 기다리는 동안, 그 뒤에 도착한 SELECT의 ACCESS SHARE 요청은 앞의 ACCESS EXCLUSIVE와 충돌하므로 함께 대기합니다. 즉 실행 중인 긴 쿼리 하나가 ALTER를 막고, 그 ALTER가 이후의 모든 접근을 막는 연쇄가 생깁니다.

**Q. lock_timeout은 어떻게 쓰나요?**

잠금을 얻기 위해 기다리는 시간의 상한입니다. `SET lock_timeout = '2s'` 뒤에 DDL을 실행하면 2초 안에 잠금을 못 얻을 때 문장이 취소되고, 뒤에 줄 선 요청들은 즉시 풀립니다. 실패하면 잠시 후 재시도하는 방식으로 운영 중 DDL을 안전하게 적용할 수 있습니다.

**Q. 잠금 없이 할 수 있는 스키마 변경은 무엇인가요?**

기본값이 없거나 상수 기본값인 컬럼 추가는 PostgreSQL 11부터 테이블을 다시 쓰지 않아 매우 짧은 잠금만 필요합니다. 인덱스는 `CREATE INDEX CONCURRENTLY`로, 제약조건은 `NOT VALID`로 추가한 뒤 `VALIDATE CONSTRAINT`로 검증하면 긴 배타 잠금을 피할 수 있습니다.
