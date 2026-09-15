---
title: "[PostgreSQL] SKIP LOCKED로 작업 큐 만들기"
date: '2026-09-18 03:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '잠금', 'SKIP LOCKED', '작업 큐', '동시성']
series: ['PostgreSQL 잠금과 대기']
description: "여러 워커가 같은 테이블에서 작업을 집어갈 때, 잠긴 행을 기다리지 않고 건너뛰는 SELECT ... FOR UPDATE SKIP LOCKED로 별도 큐 시스템 없이 작업 큐를 만드는 방법과, 그때 밟게 되는 함정들을 정리합니다."
faq:
  - q: "SKIP LOCKED는 무엇을 하나요?"
    a: "FOR UPDATE로 행을 잠글 때 이미 다른 트랜잭션이 잠근 행을 기다리지 않고 건너뜁니다. 여러 워커가 동시에 같은 쿼리를 실행해도 각자 다른 행을 가져가게 되므로, 별도 메시지 큐 없이 데이터베이스 테이블만으로 작업 분배가 가능합니다. PostgreSQL 9.5부터 지원합니다."
  - q: "워커가 작업 도중 죽으면 어떻게 되나요?"
    a: "행 잠금은 트랜잭션이 끝나면 풀리므로, 잠금만으로 소유권을 표현하면 워커가 죽는 순간 그 작업은 다시 대기 상태로 돌아갑니다. 처리 상태를 컬럼에 기록하고 커밋한 뒤 작업을 수행하는 방식이라면, 시작 시각을 함께 기록해 일정 시간이 지난 미완료 작업을 되돌리는 회수 절차가 필요합니다."
  - q: "SKIP LOCKED 큐가 느려지는 이유는 무엇인가요?"
    a: "완료된 작업 행을 갱신하거나 삭제할 때마다 죽은 튜플이 생기고, 처리량이 높으면 autovacuum이 따라가지 못해 테이블과 인덱스가 부풀기 때문입니다. 대기 상태 행만 포함하는 부분 인덱스를 두고, 완료 행은 주기적으로 정리하거나 별도 테이블로 옮기는 것이 일반적인 대응입니다."
---

잠금 시리즈 마지막 편이다. 앞의 두 편이 잠금 때문에 멈추는 이야기였다면, 이번엔 잠금을 이용해서 일을 나누는 이야기다.

## 문제

작업 테이블이 있다. 워커 여러 대가 여기서 하나씩 집어가서 처리한다.

```sql
CREATE TABLE jobs (
  id         bigserial PRIMARY KEY,
  status     text NOT NULL DEFAULT 'pending',
  payload    jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

순진하게 짜면 이렇게 된다.

```sql
SELECT * FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1;
UPDATE jobs SET status = 'running' WHERE id = ?;
```

워커 둘이 동시에 SELECT하면 같은 행을 본다. 둘 다 UPDATE하고 둘 다 처리한다. 중복이다.

`FOR UPDATE`를 붙이면 중복은 없어진다. 첫 워커가 행을 잠그고, 둘째 워커는 그 행이 풀릴 때까지 기다린다. 그런데 이러면 워커가 열 대여도 한 번에 하나만 일한다. 나머지 아홉은 줄 서 있다.

## SKIP LOCKED

기다리는 대신 건너뛰면 된다.

```sql
SELECT * FROM jobs
WHERE status = 'pending'
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

첫 워커가 행 1을 잠근다. 둘째 워커는 행 1이 잠긴 걸 보고 건너뛰어 행 2를 잠근다. 셋째는 행 3. 열 대가 동시에 돌면 열 개 행이 동시에 잡힌다.

한 문장으로 잡고 상태까지 바꾸는 게 깔끔하다.

```sql
UPDATE jobs
SET    status = 'running', started_at = now()
WHERE  id = (
  SELECT id FROM jobs
  WHERE  status = 'pending'
  ORDER  BY id
  FOR UPDATE SKIP LOCKED
  LIMIT  1
)
RETURNING *;
```

서브쿼리가 잠그고, 바깥 UPDATE가 상태를 바꾸고, `RETURNING`으로 받아온다. 원자적이다.

`NOWAIT`이라는 것도 있는데 다르다. 잠긴 행을 만나면 건너뛰는 게 아니라 에러를 낸다. 큐에는 안 맞는다.

## 소유권을 어디에 둘 것인가

여기서 설계가 갈린다.

**방법 1: 잠금이 곧 소유권.** 트랜잭션을 열고, SKIP LOCKED로 행을 잡고, 그 트랜잭션 안에서 작업을 다 하고, 완료 표시를 하고, 커밋한다. 워커가 죽으면 트랜잭션이 롤백되고 잠금이 풀려서 행은 자동으로 `pending`으로 돌아간다.

단순하고 회수 로직이 필요 없다. 대신 작업 시간만큼 트랜잭션이 열려 있다. 작업이 몇 초면 괜찮지만 몇 분이면 [VACUUM 시리즈 3편](/posts/postgresql-vacuum-3-what-blocks-cleanup/)에서 다룬 문제가 생긴다. 긴 트랜잭션이 정리 기준선을 붙잡는다. 그리고 트랜잭션 안에서 외부 API를 부르는 구조가 된다.

**방법 2: 상태 컬럼이 소유권.** 위의 UPDATE ... RETURNING으로 `running`으로 바꾸고 즉시 커밋한다. 그 다음 작업을 한다. 끝나면 별도 트랜잭션에서 `done`으로 바꾼다.

트랜잭션이 짧다. 대신 워커가 죽으면 행이 `running`인 채로 남는다. 아무도 안 건드린다. 그래서 회수 절차가 필요하다.

```sql
UPDATE jobs
SET    status = 'pending', started_at = NULL
WHERE  status = 'running'
  AND  started_at < now() - interval '10 minutes';
```

이걸 주기적으로 돌린다. 10분은 작업의 최대 소요 시간보다 넉넉히 길어야 한다. 짧으면 아직 도는 작업을 되돌려서 중복 실행된다.

대부분은 방법 2로 간다. 작업이 정말 짧고 외부 호출이 없을 때만 방법 1이 낫다.

## 인덱스

`status = 'pending'` 조건에 인덱스가 없으면 워커마다 전체를 훑는다. 그런데 `status` 전체에 인덱스를 걸면 `done` 행이 대부분이라 인덱스도 커진다.

부분 인덱스를 쓴다.

```sql
CREATE INDEX idx_jobs_pending ON jobs (id) WHERE status = 'pending';
```

대기 중인 행만 들어간다. 큐가 비어 있으면 인덱스도 거의 비어 있다. `ORDER BY id`도 이 인덱스로 처리된다.

우선순위가 있으면 인덱스 컬럼에 넣는다.

```sql
CREATE INDEX idx_jobs_pending ON jobs (priority DESC, id) WHERE status = 'pending';
```

## 느려지는 이유

큐 테이블은 갱신이 극단적으로 잦다. 행마다 최소 두 번 UPDATE된다. `pending → running → done`. 처리량이 초당 수백 건이면 죽은 튜플이 그 속도로 쌓인다.

[VACUUM 시리즈](/posts/postgresql-vacuum-2-autovacuum-tuning/)에서 다룬 이야기가 그대로 적용된다. 큐 테이블은 autovacuum을 공격적으로 잡아야 한다.

```sql
ALTER TABLE jobs SET (
  autovacuum_vacuum_scale_factor = 0.01,
  autovacuum_vacuum_cost_delay   = 0
);
```

그리고 `done` 행을 테이블에 계속 두지 않는다. 완료 행은 주기적으로 지우거나 아카이브 테이블로 옮긴다. `status`를 갱신하는 대신 완료 시 행을 DELETE하는 설계도 있다. UPDATE 한 번이 줄어들고, 부분 인덱스도 필요 없어진다. 처리 이력이 필요 없다면 이쪽이 가볍다.

인덱스 컬럼인 `status`를 갱신하므로 HOT update는 안 된다. [4편](/posts/postgresql-vacuum-4-hot-update-fillfactor/)의 이야기다. 이건 감수하는 수밖에 없다.

## 폴링

워커는 결국 주기적으로 SELECT를 던진다. 큐가 비어 있으면 빈 결과를 계속 받는다.

간격을 짧게 하면 지연이 줄지만 빈 쿼리가 늘고, 길게 하면 반대다. 대개 지수 백오프로 간다. 비어 있으면 간격을 늘리고, 작업이 나오면 다시 줄인다.

`LISTEN / NOTIFY`로 새 작업이 들어올 때 워커를 깨울 수도 있다. 다만 NOTIFY는 전달 보장이 없어서 폴링을 완전히 대체하지는 못한다. 폴링 간격을 길게 잡되 NOTIFY로 즉시 깨우는 조합이 흔하다.

## 언제 이걸 쓰나

전용 큐 시스템보다 나은 점은 하나다. 트랜잭션이 같이 묶인다. 주문을 저장하는 트랜잭션 안에서 작업 행을 같이 넣으면, 둘 다 커밋되거나 둘 다 안 된다. 외부 큐에 넣을 때 생기는 "DB엔 있는데 큐엔 없다"가 없다.

대신 처리량에 한계가 있다. 초당 수천 건이 넘어가면 죽은 튜플과 잠금 경합이 부담이 된다. 그 지점에서는 전용 큐로 옮기는 게 맞다.

그 전까지는 이걸로 충분한 경우가 많다. 부품이 하나 줄어드는 건 작은 이득이 아니다.

## 자주 묻는 질문

**Q. SKIP LOCKED는 무엇을 하나요?**

FOR UPDATE로 행을 잠글 때 이미 다른 트랜잭션이 잠근 행을 기다리지 않고 건너뜁니다. 여러 워커가 동시에 같은 쿼리를 실행해도 각자 다른 행을 가져가게 되므로, 별도 메시지 큐 없이 데이터베이스 테이블만으로 작업 분배가 가능합니다. PostgreSQL 9.5부터 지원합니다.

**Q. 워커가 작업 도중 죽으면 어떻게 되나요?**

행 잠금은 트랜잭션이 끝나면 풀리므로, 잠금만으로 소유권을 표현하면 워커가 죽는 순간 그 작업은 다시 대기 상태로 돌아갑니다. 처리 상태를 컬럼에 기록하고 커밋한 뒤 작업을 수행하는 방식이라면, 시작 시각을 함께 기록해 일정 시간이 지난 미완료 작업을 되돌리는 회수 절차가 필요합니다.

**Q. SKIP LOCKED 큐가 느려지는 이유는 무엇인가요?**

완료된 작업 행을 갱신하거나 삭제할 때마다 죽은 튜플이 생기고, 처리량이 높으면 autovacuum이 따라가지 못해 테이블과 인덱스가 부풀기 때문입니다. 대기 상태 행만 포함하는 부분 인덱스를 두고, 완료 행은 주기적으로 정리하거나 별도 테이블로 옮기는 것이 일반적인 대응입니다.
