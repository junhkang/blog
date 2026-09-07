---
title: "[PostgreSQL] 트랜잭션 ID는 42억 개인데 왜 20억마다 freeze가 도는가"
date: '2026-07-14 21:30:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '트랜잭션', 'Vacuum', 'XID', 'wraparound', '데이터베이스']
description: "PostgreSQL의 트랜잭션 ID는 32비트라 약 42억 개인데, 실제 freeze 기준은 그 절반인 20억입니다. 순환 카운터에서 두 트랜잭션의 전후 관계를 부호 있는 정수 비교로 판정하기 때문인데, 그 원리와 age() 모니터링 방법을 정리합니다."
faq:
  - q: "PostgreSQL 트랜잭션 ID는 왜 42억이 아니라 20억이 한계인가요?"
    a: "트랜잭션 ID는 순환 카운터라서 두 ID의 절대적인 전후 관계가 존재하지 않습니다. PostgreSQL은 두 ID의 차이를 부호 있는 32비트 정수로 해석해 전후를 판정하는데, 이 방식으로 모호함 없이 구분할 수 있는 범위가 기준점 앞뒤로 각각 2^31, 즉 약 21억입니다. 그래서 전체 공간 42억의 절반이 실질적인 한계가 됩니다."
  - q: "age(relfrozenxid)는 무엇을 의미하나요?"
    a: "현재 트랜잭션 ID와 해당 테이블에서 가장 오래된 미동결 트랜잭션 ID 사이의 거리입니다. 이 값이 커질수록 wraparound 한계선에 가까워진다는 뜻이며, autovacuum_freeze_max_age(기본 2억)를 넘으면 autovacuum이 꺼져 있어도 강제로 freeze vacuum이 수행됩니다."
  - q: "wraparound를 방치하면 어떻게 되나요?"
    a: "한계선에 근접하면 PostgreSQL이 경고를 남기고, 남은 여유가 약 100만 XID까지 줄어들면 데이터 손실을 막기 위해 새 트랜잭션 발급을 거부합니다. 데이터베이스가 사실상 읽기 전용이 되며, 단일 사용자 모드로 접속해 VACUUM을 수행해야 복구됩니다."
---

## 시작은 사소한 의문이었다

PostgreSQL의 트랜잭션 ID(XID)는 32비트다. 그러면 2^32, 약 **42억 9천만 개**를 쓸 수 있다.

그런데 운영 문서를 읽다 보면 기준선이 자꾸 **20억** 근처에서 잡힌다. `autovacuum_freeze_max_age`의 상한도 20억이고, wraparound 경고도 20억 언저리에서 나온다. 공간은 42억인데 왜 절반만 쓰는 걸까.

"안전 마진이겠지" 하고 넘어갔다가, 마진치고는 너무 정확히 절반이라는 게 계속 걸렸다. 그래서 물어봤고, 답을 듣고 나니 이건 마진이 아니라 **구조적으로 그 이상 쓸 수 없는 값**이었다.

## 순환 카운터에는 "절대적인 전후"가 없다

핵심은 XID가 단조 증가하는 값이 아니라 **순환 카운터(circular counter)** 라는 데 있다. 42억까지 올라가면 다시 0부터 시작한다.

여기서 문제가 생긴다. MVCC는 "이 튜플을 만든 트랜잭션이 내 트랜잭션보다 과거인가"를 끊임없이 판정해야 하는데, 순환하는 공간에서는 두 수의 크기 비교가 전후 관계를 의미하지 않는다.

XID가 10억인 트랜잭션 A와 30억인 트랜잭션 B가 있다고 하자.

- B가 A보다 20억 뒤에 생성된 것일 수도 있고,
- A가 한 바퀴 돌아서 B보다 22억 뒤에 생성된 것일 수도 있다.

**두 값만 보고는 구분할 방법이 없다.** 순환 공간에서는 10억이 30억보다 앞일 수도, 뒤일 수도 있다.

## 그래서 부호 있는 정수로 비교한다

PostgreSQL이 택한 방법은 이렇다. 두 XID를 직접 비교하지 않고, **차이를 구한 다음 그 결과를 부호 있는 32비트 정수로 해석**한다.

```c
/* 개념적으로는 이런 판정이다 */
int32 diff = (int32) (xid_a - xid_b);

if (diff < 0)  /* A 가 B 보다 과거 */
if (diff > 0)  /* A 가 B 보다 미래 */
```

뺄셈은 32비트 안에서 자연스럽게 순환(wrap)하고, 그 결과를 부호 있는 값으로 읽으면 "가까운 쪽"으로 전후가 결정된다. 예를 들어보자. 편의상 G를 10억으로 쓴다.

| 비교 | 부호 있는 차이 | 판정 |
|---|---|---|
| 1.5G vs 2.5G | -1G | 1.5G 가 **과거** |
| 0.5G vs 3.5G | +1G | 0.5G 가 **미래** |

두 번째 줄이 직관에 어긋나 보이는 지점이다. 숫자만 보면 0.5G가 3.5G보다 작지만, 순환 공간에서 3.5G에서 1G만 더 가면 4.29G를 지나 0.5G에 도달한다. **가까운 경로 기준으로는 0.5G가 3.5G의 미래**다. 부호 있는 비교는 정확히 이 해석을 준다.

## 절반이 나오는 이유

이 방식이 성립하려면 조건이 하나 필요하다. **비교하려는 두 XID가 서로 2^31 이내에 있어야 한다.**

기준점에서 앞으로 2^31, 뒤로 2^31. 합쳐서 2^32, 즉 전체 공간이 딱 채워진다. 거리가 2^31을 넘어가는 순간 "앞으로 21억"과 "뒤로 21억"이 같은 지점을 가리키게 되어 전후 판정이 뒤집힌다.

즉 **어느 시점에서든 안전하게 전후를 판정할 수 있는 창은 2^31 ≈ 21억**이고, 이게 20억이라는 숫자의 정체다. 안전 마진이 아니라 알고리즘이 성립하는 경계선이다.

> 42억은 저장할 수 있는 값의 개수이고, 21억은 **의미를 부여할 수 있는 값의 개수**다.

## freeze는 이 창을 유지하는 장치다

그러면 오래된 튜플은 어떻게 되나. 계속 두면 언젠가 현재 XID와의 거리가 21억을 넘어 "미래"로 뒤집혀 버린다. 조회되던 데이터가 갑자기 사라지는 것이다.

그래서 PostgreSQL은 충분히 오래돼서 모든 트랜잭션에게 확실히 보이는 튜플을 **frozen** 상태로 표시한다. frozen 튜플은 XID 비교 자체를 건너뛰고 무조건 가시적으로 취급된다. 순환 공간에서 빠져나오는 셈이다.

이 작업을 하는 게 VACUUM이고, 관련 설정은 대략 이렇게 걸린다.

| 파라미터 | 기본값 | 역할 |
|---|---|---|
| `vacuum_freeze_min_age` | 5천만 | 이보다 오래된 튜플을 freeze 대상으로 본다 |
| `autovacuum_freeze_max_age` | 2억 | 이 값을 넘으면 autovacuum이 꺼져 있어도 강제로 vacuum |
| `vacuum_failsafe_age` | 16억 | 이 지점부터는 인덱스 정리 등을 건너뛰고 freeze에만 집중 |

기본값 2억은 한계선 21억에 비하면 대단히 보수적이다. 그럴 만한 이유가 있다. 한계에 임박해서 도는 강제 vacuum은 대상 테이블 전체를 훑기 때문에 트래픽이 몰리는 시간에 걸리면 그 자체가 장애가 된다. 여유가 있을 때 조금씩 나눠 처리하려고 일찍 트리거하는 것이다.

## 확인은 age()로 한다

`age(relfrozenxid)`는 현재 XID와 해당 테이블에서 가장 오래된 미동결 XID 사이의 거리를 준다. 한계선까지 얼마나 남았는지를 보는 값이다.

```sql
-- 테이블별 age 상위 20개
SELECT c.relname,
       age(c.relfrozenxid)                                  AS xid_age,
       round(100.0 * age(c.relfrozenxid) / 2147483648, 2)   AS pct_of_limit,
       pg_size_pretty(pg_total_relation_size(c.oid))        AS size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm', 't')          -- 테이블, 머티리얼라이즈드 뷰, TOAST
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY age(c.relfrozenxid) DESC
LIMIT 20;
```

```sql
-- 데이터베이스 단위
SELECT datname,
       age(datfrozenxid) AS xid_age
FROM pg_database
ORDER BY age(datfrozenxid) DESC;
```

읽는 기준은 단순하다.

- **2억 근처**: 정상. 강제 freeze가 도는 지점이라 여기서 오르내리는 건 설계대로 동작하는 것이다.
- **10억 이상**: 무언가 freeze를 막고 있다. 오래된 prepared transaction, 방치된 replication slot, 장시간 유휴 트랜잭션(`idle in transaction`)이 흔한 원인이다.
- **16억 이상**: failsafe 구간. 여기까지 왔으면 원인부터 찾아야 한다.

값이 안 내려간다면 vacuum이 안 도는 게 아니라 **못 도는** 경우가 대부분이다. 아래 셋을 먼저 본다.

```sql
-- 1. 오래된 prepared transaction
SELECT gid, prepared, age(transaction) FROM pg_prepared_xacts ORDER BY age(transaction) DESC;

-- 2. 방치된 replication slot (미소비 slot 은 WAL 과 XID 를 붙잡는다)
SELECT slot_name, active, age(xmin), age(catalog_xmin) FROM pg_replication_slots;

-- 3. 오래 열려 있는 트랜잭션
SELECT pid, state, age(backend_xmin), now() - xact_start AS duration
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY age(backend_xmin) DESC;
```

## 한계에 닿으면

여유가 줄어들면 경고 로그가 먼저 나온다. 그대로 두면 남은 XID가 약 100만 개가 되는 시점에 PostgreSQL이 **새 트랜잭션 발급을 거부**한다. 데이터를 잃느니 멈추겠다는 선택이다. 이 상태에서는 단일 사용자 모드로 접속해 VACUUM을 수행해야 풀린다.

실무에서 여기까지 가는 경우는 거의 없다. 다만 가는 경로는 대체로 비슷하다. **대량 배치가 도는 테이블에 autovacuum이 계속 밀리고, 아무도 age를 보고 있지 않은 상태.** 그래서 이 값은 디스크 사용량이나 커넥션 수처럼 상시 대시보드에 올려두는 게 맞다고 생각한다. 하루아침에 나빠지는 지표가 아니라서, 보고 있기만 하면 대응할 시간이 충분하다.

## 정리

- XID는 순환 카운터라서 두 값의 크기 비교가 전후 관계를 뜻하지 않는다.
- PostgreSQL은 차이를 부호 있는 32비트로 해석해 전후를 판정한다.
- 이 판정이 성립하는 범위가 2^31이고, 그래서 42억이 아니라 21억이 실질 한계다.
- freeze는 오래된 튜플을 이 순환 공간에서 빼내는 작업이고, VACUUM이 그 일을 한다.
- `age(relfrozenxid)`를 모니터링하고, 안 내려가면 vacuum을 막고 있는 것부터 찾는다.

숫자 하나가 왜 절반인지 따라가다 보니 MVCC가 시간을 다루는 방식까지 닿았다. 이런 게 데이터베이스를 읽는 재미인 것 같다.

## 자주 묻는 질문

**Q. PostgreSQL 트랜잭션 ID는 왜 42억이 아니라 20억이 한계인가요?**

트랜잭션 ID는 순환 카운터라서 두 ID의 절대적인 전후 관계가 존재하지 않습니다. PostgreSQL은 두 ID의 차이를 부호 있는 32비트 정수로 해석해 전후를 판정하는데, 이 방식으로 모호함 없이 구분할 수 있는 범위가 기준점 앞뒤로 각각 2^31, 즉 약 21억입니다. 그래서 전체 공간 42억의 절반이 실질적인 한계가 됩니다.

**Q. age(relfrozenxid)는 무엇을 의미하나요?**

현재 트랜잭션 ID와 해당 테이블에서 가장 오래된 미동결 트랜잭션 ID 사이의 거리입니다. 이 값이 커질수록 wraparound 한계선에 가까워진다는 뜻이며, `autovacuum_freeze_max_age`(기본 2억)를 넘으면 autovacuum이 꺼져 있어도 강제로 freeze vacuum이 수행됩니다.

**Q. wraparound를 방치하면 어떻게 되나요?**

한계선에 근접하면 PostgreSQL이 경고를 남기고, 남은 여유가 약 100만 XID까지 줄어들면 데이터 손실을 막기 위해 새 트랜잭션 발급을 거부합니다. 데이터베이스가 사실상 읽기 전용이 되며, 단일 사용자 모드로 접속해 VACUUM을 수행해야 복구됩니다.
