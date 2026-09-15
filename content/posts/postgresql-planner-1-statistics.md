---
title: "[PostgreSQL] 플래너는 통계를 보고 결정한다 — 그 통계는 언제 틀리나"
date: '2026-09-19 03:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', '플래너', '통계', 'ANALYZE', 'pg_stats', '성능']
series: ['PostgreSQL 플래너가 틀리는 이유']
description: "실행 계획은 실제 데이터가 아니라 ANALYZE가 표본으로 만든 통계를 보고 세워집니다. 통계가 무엇을 담고 있는지, 언제 오래되거나 빗나가는지, 그리고 추정 행 수가 실제와 어긋날 때 무엇을 손봐야 하는지 정리합니다."
faq:
  - q: "EXPLAIN의 추정 행 수와 실제 행 수가 크게 다른 이유는 무엇인가요?"
    a: "플래너는 ANALYZE가 만든 표본 통계로 행 수를 추정하는데, 통계가 오래됐거나 표본이 데이터의 분포를 담지 못하면 추정이 빗나갑니다. 대량 적재 직후 ANALYZE가 아직 안 돌았거나, 값의 편중이 심해 표본 3만 행으로는 고유값 수를 잘못 세는 경우가 흔합니다."
  - q: "대량 적재 후 ANALYZE를 직접 실행해야 하나요?"
    a: "네. autovacuum의 자동 ANALYZE는 테이블 행 수의 10%가 바뀌어야 발동하므로, 큰 테이블에 몇 퍼센트를 추가하는 적재로는 발동하지 않습니다. 적재 직후 그 테이블에 ANALYZE를 명시적으로 실행해야 새 데이터의 분포가 통계에 반영됩니다."
  - q: "특정 컬럼의 통계 정확도만 올릴 수 있나요?"
    a: "ALTER TABLE ... ALTER COLUMN ... SET STATISTICS 로 컬럼별 표본 크기를 늘릴 수 있습니다. 기본 100에서 1000으로 올리면 표본이 30만 행으로 늘고 최빈값 목록과 히스토그램도 더 촘촘해집니다. 고유값 수가 계속 틀린다면 SET (n_distinct = ...) 로 직접 고정할 수도 있습니다."
---

`EXPLAIN ANALYZE`를 보면 두 숫자가 나란히 있다. `rows=100`과 `actual rows=1500000`. 플래너는 백 행이 나올 줄 알고 계획을 짰는데 실제로는 백오십만 행이 나왔다. 그 계획은 백 행에 맞는 계획이었을 테니, 결과는 느린 쿼리다.

플래너가 왜 백 행이라고 생각했는지를 따라가면 통계에 닿는다. 이 시리즈는 그 통계 이야기다.

## 통계는 표본이다

플래너는 실행 전에 테이블을 읽지 않는다. `ANALYZE`가 미리 만들어둔 요약을 본다.

`ANALYZE`는 테이블 전체를 읽지 않는다. 표본을 뽑는다. 표본 크기는 `default_statistics_target × 300`이고, 기본값이 100이라 3만 행이다. 테이블이 천 행이든 십억 행이든 3만 행이다.

그 표본에서 컬럼마다 이런 걸 계산해 `pg_statistic`에 저장한다. 사람이 읽는 뷰는 `pg_stats`다.

```sql
SELECT attname, null_frac, n_distinct,
       most_common_vals, most_common_freqs,
       histogram_bounds, correlation
FROM pg_stats
WHERE tablename = 'orders';
```

| 항목 | 뜻 |
|---|---|
| `null_frac` | NULL 비율 |
| `n_distinct` | 고유값 수. 양수면 절대값, 음수면 행 수 대비 비율 (-1은 전부 고유) |
| `most_common_vals` / `_freqs` | 최빈값 목록과 각각의 비율. 최대 target 개 |
| `histogram_bounds` | 최빈값을 뺀 나머지의 분포를 target 개 구간으로 |
| `correlation` | 물리적 저장 순서와 값 순서의 상관. 1에 가까우면 정렬돼 있음 |

`WHERE status = 'done'`이 들어오면 플래너는 `most_common_vals`에서 `'done'`을 찾고, 있으면 그 비율을 쓴다. 없으면 나머지 값들이 균등하다고 보고 `(1 - 최빈값 비율 합) / (n_distinct - 최빈값 수)`로 추정한다.

`WHERE created_at > '2026-01-01'`은 히스토그램에서 그 값이 몇 번째 구간에 있는지 보고 비율을 계산한다.

이 계산은 정교하다. 문제는 입력이 표본이라는 점이다.

## 통계가 틀리는 네 가지 경우

**오래됐다.** 통계는 `ANALYZE`가 돌 때만 갱신된다. autovacuum이 자동으로 돌려주는데, 조건이 있다. 행 수의 10%(`autovacuum_analyze_scale_factor`) + 50건이 바뀌어야 한다. 2천만 행 테이블에 백만 행을 넣으면 5%라 안 돈다. 그 백만 행에 대한 쿼리는 그 데이터가 없던 시절의 통계로 계획된다.

대량 적재 뒤에는 직접 돌린다.

```sql
ANALYZE orders;
```

몇 초에서 몇십 초면 끝난다. 적재 배치 마지막 줄에 넣어두는 게 맞다.

**고유값 수가 빗나간다.** `n_distinct`가 특히 그렇다. 3만 행 표본에서 고유값이 2만 개 나왔을 때, 전체 십억 행에는 고유값이 몇 개일까. 표본에서 보이는 반복 패턴으로 외삽하는데, 값이 편중돼 있으면 크게 틀린다. 실제로 백만 개인데 만 개로 추정하거나 그 반대가 흔하다.

이게 틀리면 `GROUP BY` 결과 크기, 조인 결과 크기, `=` 조건의 선택도가 전부 틀린다.

```sql
-- 실제 고유값 수와 비교
SELECT n_distinct FROM pg_stats WHERE tablename = 'orders' AND attname = 'user_id';
SELECT count(DISTINCT user_id) FROM orders;
```

차이가 크면 직접 고정할 수 있다.

```sql
ALTER TABLE orders ALTER COLUMN user_id SET (n_distinct = -0.3);  -- 행 수의 30%
ALTER TABLE orders ALTER COLUMN user_id SET (n_distinct = 850000); -- 절대값
```

이 값은 다음 `ANALYZE`에서도 유지된다. 데이터가 바뀌면 다시 봐야 하니 남용은 금물이다.

**최빈값 목록이 짧다.** target이 100이면 최빈값을 100개까지만 기록한다. 값이 수천 종류인데 그중 상위 300개가 편중돼 있다면, 101번째부터는 "나머지는 균등"으로 취급된다. 실제로는 101번째 값도 꽤 많은데 아주 적다고 추정한다.

컬럼 단위로 target을 올리면 된다.

```sql
ALTER TABLE orders ALTER COLUMN status SET STATISTICS 1000;
ANALYZE orders;
```

표본이 30만 행으로 늘고 최빈값도 1000개까지 기록한다. `ANALYZE` 시간과 통계 크기가 늘지만, 문제되는 컬럼 몇 개에만 주면 부담은 작다. 최댓값은 10000이다.

**표현식에는 통계가 없다.** `WHERE lower(email) = 'a@b.com'`. 플래너는 `email`의 통계는 갖고 있지만 `lower(email)`의 통계는 없다. 이럴 때 기본 선택도를 쓴다. `=`는 0.5%, 부등호는 33%다. 데이터와 무관한 상수다.

표현식 인덱스를 만들면 그 표현식에 대해 통계도 같이 수집된다.

```sql
CREATE INDEX idx_orders_email_lower ON orders (lower(email));
ANALYZE orders;
```

인덱스는 필요 없고 통계만 필요하다면 PostgreSQL 14부터 이렇게 할 수 있다.

```sql
CREATE STATISTICS st_email_lower ON lower(email) FROM orders;
ANALYZE orders;
```

## 확인하는 순서

느린 쿼리를 만났을 때 통계 문제인지 가르는 순서다.

1. `EXPLAIN (ANALYZE, BUFFERS)`에서 추정 행 수와 실제 행 수가 10배 이상 차이 나는 노드를 찾는다.
2. 그 노드의 조건에 들어간 컬럼을 확인한다.
3. `pg_stat_user_tables`의 `last_analyze`, `last_autoanalyze`를 본다. 오래됐으면 `ANALYZE`부터.
4. 그래도 틀리면 `pg_stats`에서 `n_distinct`와 최빈값 목록을 실제와 비교한다.
5. 표현식이나 함수가 조건에 있으면 그 자체에 통계가 없는 것이다.

여기까지 봐도 안 맞는 경우가 하나 더 있다. 컬럼 하나하나는 정확한데 두 컬럼을 함께 쓰면 빗나가는 경우다. 그건 통계가 틀린 게 아니라 플래너가 두 컬럼을 독립이라고 가정하기 때문이고, 다음 편에서 다룬다.

## 자주 묻는 질문

**Q. EXPLAIN의 추정 행 수와 실제 행 수가 크게 다른 이유는 무엇인가요?**

플래너는 ANALYZE가 만든 표본 통계로 행 수를 추정하는데, 통계가 오래됐거나 표본이 데이터의 분포를 담지 못하면 추정이 빗나갑니다. 대량 적재 직후 ANALYZE가 아직 안 돌았거나, 값의 편중이 심해 표본 3만 행으로는 고유값 수를 잘못 세는 경우가 흔합니다.

**Q. 대량 적재 후 ANALYZE를 직접 실행해야 하나요?**

네. autovacuum의 자동 ANALYZE는 테이블 행 수의 10%가 바뀌어야 발동하므로, 큰 테이블에 몇 퍼센트를 추가하는 적재로는 발동하지 않습니다. 적재 직후 그 테이블에 ANALYZE를 명시적으로 실행해야 새 데이터의 분포가 통계에 반영됩니다.

**Q. 특정 컬럼의 통계 정확도만 올릴 수 있나요?**

`ALTER TABLE ... ALTER COLUMN ... SET STATISTICS`로 컬럼별 표본 크기를 늘릴 수 있습니다. 기본 100에서 1000으로 올리면 표본이 30만 행으로 늘고 최빈값 목록과 히스토그램도 더 촘촘해집니다. 고유값 수가 계속 틀린다면 `SET (n_distinct = ...)`로 직접 고정할 수도 있습니다.
