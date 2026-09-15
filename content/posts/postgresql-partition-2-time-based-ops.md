---
title: "[PostgreSQL] 시간 기반 파티션 운영 — DELETE 없이 데이터 버리기"
date: '2026-09-23 03:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '파티셔닝', '운영', 'DETACH', '데이터 보존', 'pg_partman']
series: ['PostgreSQL 파티셔닝']
description: "시간으로 나눈 파티션은 미리 만들어두고, 오래된 것은 떼어내서 버립니다. 파티션 생성을 놓쳤을 때 벌어지는 일, DEFAULT 파티션의 함정, DETACH CONCURRENTLY, 그리고 인덱스를 서비스 중단 없이 추가하는 순서를 정리합니다."
faq:
  - q: "파티션을 미리 안 만들어두면 어떻게 되나요?"
    a: "해당 범위의 파티션이 없으면 INSERT가 'no partition of relation found for row' 에러로 실패합니다. DEFAULT 파티션이 있으면 거기로 들어가지만, 나중에 그 범위의 파티션을 붙이려 할 때 DEFAULT에 이미 그 범위의 행이 있으면 ATTACH가 거부됩니다. 파티션은 항상 몇 주기 앞서 만들어두는 자동화가 필요합니다."
  - q: "오래된 데이터를 DELETE 대신 파티션 DROP으로 지우면 무엇이 다른가요?"
    a: "DELETE는 행마다 죽은 튜플을 남기고 WAL을 쓰며, 이후 VACUUM이 그 공간을 정리해야 합니다. 억 단위 행이면 몇 시간이 걸리고 테이블이 부풉니다. 파티션 DROP은 파일을 통째로 제거하므로 순간에 끝나고 죽은 튜플도 없습니다. 시간 기반 파티셔닝의 가장 확실한 이득입니다."
  - q: "DETACH CONCURRENTLY는 무엇이 다른가요?"
    a: "일반 DETACH는 부모 테이블에 ACCESS EXCLUSIVE 잠금을 잡아 그 순간 모든 접근이 대기합니다. PostgreSQL 14의 DETACH CONCURRENTLY는 두 단계로 나눠 진행해 부모에 약한 잠금만 잡으므로 서비스 중 실행할 수 있습니다. 대신 트랜잭션 블록 안에서는 실행할 수 없고, 중단되면 FINALIZE로 마무리해야 합니다."
---

[지난 편](/posts/postgresql-partition-1-pruning/)에서 파티셔닝의 확실한 이득은 프루닝보다 데이터를 파티션째 버리는 데 있다고 썼다. 이번 편은 그 운영 이야기다. 시간으로 나눈 파티션을 어떻게 만들고, 어떻게 떼어내고, 어디서 사고가 나는지.

## 파티션은 미리 있어야 한다

행이 들어올 때 그 값에 맞는 파티션이 없으면 실패한다.

```
ERROR:  no partition of relation "orders" found for row
DETAIL:  Partition key of the failing row contains (created_at) = (2026-10-01 00:00:00+00).
```

10월 1일 자정에 10월 파티션이 없으면 그 순간부터 모든 INSERT가 이 에러다. 월 초 자정에 장애 나는 전형적인 경로다.

그래서 파티션은 항상 앞서 만들어둔다. 몇 달치를 미리 만들고, 주기적으로 앞으로 더 만드는 작업을 돌린다.

```sql
CREATE TABLE orders_2026_10 PARTITION OF orders
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
```

이걸 크론으로 매주 돌리면서 "지금부터 3개월 뒤까지 존재하는지 확인하고 없으면 만든다"로 짜두면 된다. `pg_partman` 같은 확장이 이걸 대신 해주기도 한다.

## DEFAULT 파티션의 함정

파티션이 없을 때를 대비해 DEFAULT 파티션을 두는 방법이 있다.

```sql
CREATE TABLE orders_default PARTITION OF orders DEFAULT;
```

어느 범위에도 안 맞는 행이 여기로 들어간다. 장애는 안 난다. 대신 다른 문제가 생긴다.

10월 파티션을 안 만들었고 10월 데이터가 DEFAULT로 들어갔다고 하자. 나중에 10월 파티션을 만들어 붙이려 하면 거부된다.

```
ERROR:  updated partition constraint for default partition "orders_default"
        would be violated by some row
```

DEFAULT에 10월 행이 있는 채로 10월 파티션을 붙이면 같은 행이 두 곳에 속하게 되니 막는 것이다. DEFAULT에서 그 행들을 꺼내 옮긴 뒤에야 붙일 수 있다. 데이터가 많으면 이게 큰 작업이 된다.

게다가 DEFAULT 파티션이 있으면 새 파티션을 붙일 때마다 DEFAULT 전체를 스캔해서 겹치는 행이 없는지 확인한다. DEFAULT가 커질수록 ATTACH가 느려진다.

DEFAULT는 안전망이 아니라 경보로 쓰는 게 맞다. 만들어두되 비어 있어야 하고, 행이 생기면 파티션 생성이 밀렸다는 신호로 본다.

```sql
SELECT count(*) FROM orders_default;   -- 0 이어야 정상
```

## 떼어내기

오래된 파티션을 버리는 게 이 설계의 핵심 이득이다.

```sql
ALTER TABLE orders DETACH PARTITION orders_2023_01;
DROP TABLE orders_2023_01;
```

`DETACH`는 파티션을 부모에서 분리해 독립 테이블로 만든다. 그 다음 `DROP`은 파일을 지운다. 억 행이든 순간이다. 죽은 튜플이 안 생기고, VACUUM할 것도 없고, WAL도 거의 안 쓴다.

같은 양을 `DELETE`했다면 몇 시간, 죽은 튜플 억 개, 그 뒤 VACUUM 몇 시간이다. [VACUUM 시리즈](/posts/postgresql-vacuum-1-disk-not-returned/)에서 다룬 이야기가 통째로 안 생긴다.

한 가지 조심할 것이 있다. 일반 `DETACH`는 부모 테이블에 ACCESS EXCLUSIVE 잠금을 잡는다. 순간이긴 한데, [잠금 시리즈 1편](/posts/postgresql-lock-1-ddl-queue/)에서 다룬 줄 서기 문제가 그대로 생긴다. 긴 쿼리가 돌고 있으면 그 뒤로 전부 대기한다.

PostgreSQL 14부터는 이걸 피할 수 있다.

```sql
ALTER TABLE orders DETACH PARTITION orders_2023_01 CONCURRENTLY;
```

두 단계로 진행하면서 부모에는 약한 잠금만 잡는다. 실행 중인 쿼리를 막지 않는다. 대신 조건이 있다. 트랜잭션 블록 안에서는 안 되고, 중간에 끊기면 파티션이 "떼어내는 중" 상태로 남는다. 그때는 마무리 명령을 따로 준다.

```sql
ALTER TABLE orders DETACH PARTITION orders_2023_01 FINALIZE;
```

떼어낸 뒤 바로 지우지 않고 잠시 두는 것도 방법이다. 독립 테이블이 됐으니 아카이브 스토리지로 덤프하거나, 며칠 뒤 문제없으면 지운다.

## 붙이기

반대로 기존 테이블을 파티션으로 붙일 때가 있다. 아카이브에서 복원하거나, 별도로 적재한 테이블을 편입할 때다.

```sql
ALTER TABLE orders ATTACH PARTITION orders_2024_06
  FOR VALUES FROM ('2024-06-01') TO ('2024-07-01');
```

이때 PostgreSQL은 붙이려는 테이블의 모든 행이 그 범위 안에 있는지 확인한다. 테이블 전체를 스캔한다. 큰 테이블이면 오래 걸린다.

미리 같은 조건의 CHECK 제약을 걸어두면 스캔을 건너뛴다.

```sql
ALTER TABLE orders_2024_06 ADD CONSTRAINT chk_range
  CHECK (created_at >= '2024-06-01' AND created_at < '2024-07-01');

ALTER TABLE orders ATTACH PARTITION orders_2024_06
  FOR VALUES FROM ('2024-06-01') TO ('2024-07-01');

ALTER TABLE orders_2024_06 DROP CONSTRAINT chk_range;  -- 이제 필요 없음
```

CHECK를 추가할 때 스캔이 한 번 일어나긴 하지만, 그건 부모 테이블과 무관한 독립 테이블에서 일어나므로 서비스에 영향이 없다.

## 인덱스를 나중에 추가할 때

PostgreSQL 11부터 부모에 인덱스를 만들면 모든 파티션에 자동으로 생기고, 새 파티션에도 자동으로 붙는다. 편하다. 대신 부모에 대한 `CREATE INDEX`는 `CONCURRENTLY`를 지원하지 않는다. 모든 파티션을 잠그고 순서대로 만든다.

운영 중에 인덱스를 추가하려면 순서를 바꾼다.

```sql
-- 1. 부모에만 인덱스 정의 (파티션에는 안 만듦, 순간)
CREATE INDEX idx_orders_user ON ONLY orders (user_id);

-- 2. 각 파티션에 CONCURRENTLY로 생성 (서비스 안 막음)
CREATE INDEX CONCURRENTLY idx_orders_2026_08_user ON orders_2026_08 (user_id);
CREATE INDEX CONCURRENTLY idx_orders_2026_09_user ON orders_2026_09 (user_id);
...

-- 3. 부모 인덱스에 붙임
ALTER INDEX idx_orders_user ATTACH PARTITION idx_orders_2026_08_user;
ALTER INDEX idx_orders_user ATTACH PARTITION idx_orders_2026_09_user;
...
```

모든 파티션의 인덱스가 붙으면 부모 인덱스가 유효해진다. 그 뒤로 생기는 새 파티션에는 자동으로 만들어진다.

## 보존 정책을 한 줄로

이 모든 게 결국 한 줄 정책으로 정리된다.

> 앞으로 N개월치 파티션을 미리 만들고, M개월 지난 파티션은 떼어내서 버린다.

N과 M을 정하고, 그걸 실행하는 작업을 주기적으로 돌리고, DEFAULT 파티션이 비어 있는지 감시한다. 이 셋이 시간 기반 파티션 운영의 전부다.

다음 편은 파티셔닝하면 못 하게 되는 것들, 유니크 제약과 외래키의 한계다.

## 자주 묻는 질문

**Q. 파티션을 미리 안 만들어두면 어떻게 되나요?**

해당 범위의 파티션이 없으면 INSERT가 "no partition of relation found for row" 에러로 실패합니다. DEFAULT 파티션이 있으면 거기로 들어가지만, 나중에 그 범위의 파티션을 붙이려 할 때 DEFAULT에 이미 그 범위의 행이 있으면 ATTACH가 거부됩니다. 파티션은 항상 몇 주기 앞서 만들어두는 자동화가 필요합니다.

**Q. 오래된 데이터를 DELETE 대신 파티션 DROP으로 지우면 무엇이 다른가요?**

DELETE는 행마다 죽은 튜플을 남기고 WAL을 쓰며, 이후 VACUUM이 그 공간을 정리해야 합니다. 억 단위 행이면 몇 시간이 걸리고 테이블이 부풉니다. 파티션 DROP은 파일을 통째로 제거하므로 순간에 끝나고 죽은 튜플도 없습니다. 시간 기반 파티셔닝의 가장 확실한 이득입니다.

**Q. DETACH CONCURRENTLY는 무엇이 다른가요?**

일반 DETACH는 부모 테이블에 ACCESS EXCLUSIVE 잠금을 잡아 그 순간 모든 접근이 대기합니다. PostgreSQL 14의 DETACH CONCURRENTLY는 두 단계로 나눠 진행해 부모에 약한 잠금만 잡으므로 서비스 중 실행할 수 있습니다. 대신 트랜잭션 블록 안에서는 실행할 수 없고, 중단되면 FINALIZE로 마무리해야 합니다.
