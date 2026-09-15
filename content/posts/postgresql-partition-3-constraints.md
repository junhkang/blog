---
title: "[PostgreSQL] 파티셔닝하면 못 하게 되는 것들 — 유니크, 외래키, 행 이동"
date: '2026-09-24 03:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '파티셔닝', '유니크 제약', '외래키', 'ON CONFLICT', '설계']
series: ['PostgreSQL 파티셔닝']
description: "파티션 테이블의 PK와 유니크 제약은 반드시 파티션 키를 포함해야 합니다. 그래서 파티션을 가로지르는 유일성은 보장할 수 없고, ON CONFLICT와 외래키 설계도 달라집니다. 파티셔닝을 결정하기 전에 알아야 할 제약들을 정리합니다."
faq:
  - q: "파티션 테이블에 id 하나로 PRIMARY KEY를 걸 수 없는 이유는 무엇인가요?"
    a: "유니크 인덱스는 파티션마다 따로 만들어지므로 파티션을 가로질러 중복을 검사할 방법이 없습니다. 그래서 PostgreSQL은 PK와 유니크 제약이 파티션 키 컬럼을 모두 포함하도록 강제합니다. created_at으로 나눴다면 PK는 (id, created_at)이어야 하고, id만으로는 전체 테이블에서의 유일성이 보장되지 않습니다."
  - q: "파티션 테이블에서 ON CONFLICT를 쓸 수 있나요?"
    a: "쓸 수 있지만 충돌 대상이 파티션 키를 포함한 유니크 제약이어야 합니다. ON CONFLICT (id) 는 id만으로는 유니크 인덱스가 없어 에러가 나고, ON CONFLICT (id, created_at) 처럼 파티션 키를 함께 지정해야 합니다. 즉 INSERT 시점에 파티션 키 값을 알고 있어야 합니다."
  - q: "파티션 키 컬럼을 UPDATE하면 어떻게 되나요?"
    a: "PostgreSQL 11부터 해당 행이 맞는 파티션으로 자동 이동합니다. 내부적으로는 원래 파티션에서 DELETE하고 새 파티션에 INSERT하는 것이라, 행 단위 트리거가 DELETE와 INSERT로 각각 실행되고, 같은 행을 동시에 갱신하려던 다른 트랜잭션은 에러를 받습니다. 키 갱신이 잦은 컬럼은 파티션 키로 부적합합니다."
---

파티셔닝 시리즈 마지막이다. 앞의 두 편이 얻는 것이었다면 이번 편은 잃는 것이다. 파티셔닝을 결정하기 전에 이 목록을 보고 감당할 수 있는지 판단하는 게 맞다.

## 유니크 제약은 파티션 키를 포함해야 한다

가장 먼저 부딪히는 벽이다.

```sql
CREATE TABLE orders (
  id         bigint,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (id)
) PARTITION BY RANGE (created_at);
```

```
ERROR:  unique constraint on partitioned table must include all partitioning columns
DETAIL:  PRIMARY KEY constraint on table "orders" lacks column "created_at"
         which is part of the partition key.
```

이유는 구조에 있다. 유니크 인덱스는 파티션마다 따로 만들어진다. 8월 파티션의 인덱스는 8월 파티션 안에서만 중복을 검사한다. `id = 100`이 8월에도 있고 9월에도 있는지는 아무도 검사하지 않는다. 그래서 PostgreSQL은 처음부터 허용하지 않는다.

PK는 이렇게 돼야 한다.

```sql
PRIMARY KEY (id, created_at)
```

이러면 `(id, created_at)` 조합이 유일하다는 뜻이지 `id`가 유일하다는 뜻이 아니다. `id = 100`인 행이 8월과 9월에 하나씩 있어도 제약 위반이 아니다.

실무에서는 `id`가 시퀀스라서 어차피 안 겹친다. 하지만 그건 시퀀스가 보장하는 것이지 제약이 보장하는 것이 아니다. 데이터를 다른 곳에서 옮겨오거나, 시퀀스를 리셋하거나, 애플리케이션이 id를 직접 넣는 경우에 중복이 조용히 들어간다.

`email`처럼 시퀀스가 아닌 컬럼에 유니크를 걸고 싶으면 방법이 없다. `UNIQUE (email, created_at)`은 의미가 다르다. 같은 이메일이 다른 시각에 가입하면 통과한다.

우회는 두 가지다.

- 유일성이 필요한 컬럼을 별도 테이블로 뺀다. `users (email UNIQUE)` 같은 작은 테이블을 따로 두고 거기서 검사한다.
- 그 컬럼으로 파티셔닝한다. `email`로 해시 파티셔닝하면 같은 이메일은 항상 같은 파티션에 가고, 그 파티션 안의 유니크 인덱스가 검사한다. 대신 시간 기반 보존이 안 된다.

## ON CONFLICT도 같은 제약을 받는다

`INSERT ... ON CONFLICT`는 충돌 대상으로 유니크 제약을 지정한다. 그 제약이 파티션 키를 포함해야 하니 이렇게 된다.

```sql
INSERT INTO orders (id, created_at, ...)
VALUES (...)
ON CONFLICT (id) DO UPDATE ...;
-- ERROR: there is no unique or exclusion constraint matching the ON CONFLICT specification

ON CONFLICT (id, created_at) DO UPDATE ...;   -- 이건 됨
```

즉 upsert를 하려면 INSERT 시점에 파티션 키 값을 정확히 알고 있어야 한다. "이 id가 이미 있으면 갱신"은 안 되고 "이 id가 이 시각으로 이미 있으면 갱신"만 된다. 대개 이 둘은 다른 요구사항이다.

## 외래키

버전에 따라 다르다.

| 방향 | 지원 버전 |
|---|---|
| 파티션 테이블 → 일반 테이블 참조 | 11 |
| 일반 테이블 → 파티션 테이블 참조 | 12 |
| 파티션 테이블 → 파티션 테이블 | 12 |

12 이상이면 방향은 문제없다. 문제는 참조 대상이다.

외래키는 참조하는 쪽의 PK나 유니크 제약을 가리켜야 한다. 파티션 테이블의 PK가 `(id, created_at)`이면 외래키도 두 컬럼을 참조해야 한다.

```sql
CREATE TABLE order_items (
  order_id         bigint,
  order_created_at timestamptz,
  FOREIGN KEY (order_id, order_created_at) REFERENCES orders (id, created_at)
);
```

자식 테이블이 부모의 파티션 키를 같이 들고 있어야 한다. 스키마가 번잡해지고, 자식 테이블에 컬럼이 하나 더 생기고, 조인 조건도 두 개가 된다.

이걸 피하려고 외래키를 안 걸고 애플리케이션에서만 검사하는 경우도 많다. 그 선택의 대가는 정합성이 DB 밖으로 나간다는 것이다.

## 파티션 키를 갱신하면 행이 이동한다

PostgreSQL 11부터 파티션 키 컬럼을 UPDATE하면 행이 맞는 파티션으로 옮겨진다. 10 이하에서는 에러였다.

편리해 보이지만 내부 동작은 원래 파티션에서 DELETE, 새 파티션에 INSERT다. 그래서 이런 일이 생긴다.

- 행 단위 트리거가 DELETE 트리거와 INSERT 트리거로 따로 실행된다. UPDATE 트리거가 아니다.
- 같은 행을 동시에 갱신하던 다른 트랜잭션은 에러를 받는다. `tuple to be locked was already moved to another partition due to concurrent update`.
- 새 파티션에서 그 행은 새 튜플이다. 원래 파티션에는 죽은 튜플이 남는다.

파티션 키가 자주 바뀌는 컬럼이면 파티션 키로 부적합하다. 주문 상태처럼 생명주기 동안 여러 번 바뀌는 값은 키가 되면 안 된다. 생성 시각처럼 한 번 정해지면 안 바뀌는 값이 맞다.

## 파티션 키는 나중에 못 바꾼다

`created_at`으로 나눴다가 `region`으로 바꾸고 싶다면 방법이 없다. 새 파티션 테이블을 만들고 데이터를 옮겨야 한다. 억 단위 행이면 그 자체가 프로젝트다.

그래서 키 선택이 설계에서 가장 오래 봐야 할 지점이다. 판단 기준은 두 가지다. 주요 쿼리가 이 컬럼으로 범위를 제한하는가([1편](/posts/postgresql-partition-1-pruning/)). 이 컬럼으로 데이터를 버릴 것인가([2편](/posts/postgresql-partition-2-time-based-ops/)). 둘 다 아니면 파티셔닝을 안 하는 게 낫다.

## 그 밖에

- 배타 제약(EXCLUDE)도 파티션 키를 포함해야 한다.
- 파티션 테이블에 대한 행 단위 BEFORE 트리거는 PostgreSQL 13부터 된다.
- `TRUNCATE`는 부모에 하면 모든 파티션이 비워진다. 파티션 하나만 비우려면 그 파티션에 직접 한다.
- 파티션마다 `reloptions`(autovacuum 설정 등)를 따로 줄 수 있다. 부모에 준 설정은 상속되지 않는다.

## 시리즈를 마치며

파티셔닝은 얻는 것과 잃는 것이 분명한 기능이다.

얻는 것은 프루닝으로 읽는 양을 줄이는 것과, 오래된 데이터를 파일 단위로 버리는 것이다. 잃는 것은 파티션을 가로지르는 유일성, 단순한 외래키, 자유로운 upsert다.

대부분의 테이블은 파티셔닝이 필요 없다. 시간이 지나며 무한히 쌓이고, 오래된 것은 버려도 되고, 조회가 최근 데이터에 몰리는 테이블. 로그, 이벤트, 이력. 이런 것들에만 쓴다. 그 조건이 맞으면 잃는 것을 감수할 만하고, 안 맞으면 그냥 큰 테이블 하나가 낫다.

## 자주 묻는 질문

**Q. 파티션 테이블에 id 하나로 PRIMARY KEY를 걸 수 없는 이유는 무엇인가요?**

유니크 인덱스는 파티션마다 따로 만들어지므로 파티션을 가로질러 중복을 검사할 방법이 없습니다. 그래서 PostgreSQL은 PK와 유니크 제약이 파티션 키 컬럼을 모두 포함하도록 강제합니다. `created_at`으로 나눴다면 PK는 `(id, created_at)`이어야 하고, `id`만으로는 전체 테이블에서의 유일성이 보장되지 않습니다.

**Q. 파티션 테이블에서 ON CONFLICT를 쓸 수 있나요?**

쓸 수 있지만 충돌 대상이 파티션 키를 포함한 유니크 제약이어야 합니다. `ON CONFLICT (id)`는 id만으로는 유니크 인덱스가 없어 에러가 나고, `ON CONFLICT (id, created_at)`처럼 파티션 키를 함께 지정해야 합니다. 즉 INSERT 시점에 파티션 키 값을 알고 있어야 합니다.

**Q. 파티션 키 컬럼을 UPDATE하면 어떻게 되나요?**

PostgreSQL 11부터 해당 행이 맞는 파티션으로 자동 이동합니다. 내부적으로는 원래 파티션에서 DELETE하고 새 파티션에 INSERT하는 것이라, 행 단위 트리거가 DELETE와 INSERT로 각각 실행되고, 같은 행을 동시에 갱신하려던 다른 트랜잭션은 에러를 받습니다. 키 갱신이 잦은 컬럼은 파티션 키로 부적합합니다.
