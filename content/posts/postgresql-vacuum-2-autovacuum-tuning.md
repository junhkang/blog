---
title: "[PostgreSQL] autovacuum이 큰 테이블에서 안 도는 이유"
date: '2026-09-10 21:20:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', 'Vacuum', 'autovacuum', '튜닝', '운영', '성능']
series: ['PostgreSQL VACUUM 깊이 보기']
description: "autovacuum의 기본 발동 조건은 테이블 크기에 비례합니다. 그래서 큰 테이블일수록 늦게 돌고, 한 번 돌 때 오래 걸립니다. 발동 공식과 비용 기반 지연, 그리고 테이블별로 설정을 따로 주는 방법을 정리합니다."
faq:
  - q: "autovacuum이 큰 테이블에서 잘 안 도는 이유는 무엇인가요?"
    a: "기본 발동 조건이 테이블 행 수에 비례하기 때문입니다. 임계값은 autovacuum_vacuum_threshold에 autovacuum_vacuum_scale_factor와 행 수를 곱한 값을 더해 계산하는데, scale factor 기본값이 0.2라 1억 행 테이블은 죽은 튜플이 약 2천만 개 쌓여야 발동합니다. 그때는 이미 부풀어 있고 한 번의 작업량도 커집니다."
  - q: "autovacuum_max_workers를 늘리면 정리가 빨라지나요?"
    a: "대체로 아닙니다. 비용 기반 지연의 한도는 동시에 도는 워커들이 나눠 쓰도록 설계되어 있어서, 워커 수를 늘리면 워커당 처리 속도가 그만큼 줄어듭니다. 전체 처리량을 올리려면 워커 수가 아니라 vacuum_cost_limit을 올리거나 autovacuum_vacuum_cost_delay를 줄여야 합니다."
  - q: "테이블마다 autovacuum 설정을 다르게 줄 수 있나요?"
    a: "가능합니다. ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_threshold = 5000) 형태로 스토리지 파라미터를 지정하면 해당 테이블에만 적용됩니다. 갱신이 잦은 소수의 큰 테이블에만 공격적인 값을 주고 나머지는 전역 기본값을 쓰는 방식이 일반적입니다."
---

[지난 편](/posts/postgresql-vacuum-1-disk-not-returned/)에서 죽은 튜플 비율이 계속 올라가고 있다면 설정을 봐야 한다고 썼다. 그 설정 이야기다.

결론부터 말하면, **autovacuum의 기본값은 작은 테이블에 맞춰져 있다.** 테이블이 커질수록 늦게 돌고, 늦게 도는 만큼 한 번에 할 일이 많아진다.

## 발동 조건 공식

autovacuum은 테이블마다 임계값을 계산해서, 죽은 튜플 수가 그 값을 넘으면 대상으로 잡는다.

```
발동 임계값 = autovacuum_vacuum_threshold
            + autovacuum_vacuum_scale_factor × 테이블 행 수
```

기본값은 이렇다.

| 파라미터 | 기본값 |
|---|---|
| `autovacuum_vacuum_threshold` | 50 |
| `autovacuum_vacuum_scale_factor` | 0.2 (20%) |

지배적인 건 뒤쪽 항이다. 행 수에 곱해지기 때문이다.

| 테이블 행 수 | 발동에 필요한 죽은 튜플 |
|---|---|
| 1,000 | 250 |
| 100만 | 약 20만 |
| 1억 | **약 2천만** |

1억 행 테이블은 죽은 튜플이 2천만 개 쌓일 때까지 autovacuum이 손을 안 댄다. 그 시점에는 이미 상당히 부풀어 있고, 막상 돌기 시작하면 처리할 양이 많아 오래 걸린다. **늦게 시작해서 오래 하는 최악의 조합**이 기본값에서 자연스럽게 나온다.

ANALYZE도 같은 구조인데 계수만 다르다. `autovacuum_analyze_threshold`가 50, `autovacuum_analyze_scale_factor`가 0.1이다.

그리고 PostgreSQL 13부터는 **삽입만 일어나는 테이블**을 위한 조건이 따로 생겼다. `autovacuum_vacuum_insert_threshold`(기본 1000)와 `autovacuum_vacuum_insert_scale_factor`(기본 0.2)다. 그전에는 INSERT만 하는 테이블은 죽은 튜플이 안 생기니 autovacuum이 거의 안 돌았고, 그러다 freeze 한계에 걸려서야 한 번에 크게 도는 문제가 있었다.

## 비용 기반 지연 — 도는데 느린 경우

발동은 하는데 진행이 느린 경우가 있다. 이건 의도된 동작이다.

autovacuum은 I/O를 다 쓰지 않도록 스스로 브레이크를 건다. 작업하면서 비용을 누적하다가 한도에 닿으면 잠깐 쉰다.

| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `vacuum_cost_page_hit` | 1 | 버퍼에 있는 페이지를 읽음 |
| `vacuum_cost_page_miss` | 2 | 디스크에서 읽어야 함 |
| `vacuum_cost_page_dirty` | 20 | 페이지를 더럽힘 (쓰기 발생) |
| `vacuum_cost_limit` | 200 | 이만큼 누적되면 쉰다 |
| `autovacuum_vacuum_cost_delay` | 2ms | 쉬는 시간 |

`vacuum_cost_page_miss`는 PostgreSQL 14에서 10에서 2로 낮아졌고, `autovacuum_vacuum_cost_delay`는 12에서 20ms에서 2ms로 낮아졌다. 오래된 버전을 쓰고 있다면 기본값만으로도 훨씬 느리게 도니 버전을 먼저 확인하는 게 좋다.

오해가 하나 있다. 워커를 늘리면 빨라질 거라는 생각인데, 대체로 아니다.

`autovacuum_max_workers`(기본 3)를 늘려도 전체 처리량은 잘 안 는다. 비용 한도가 **동시에 도는 워커들이 나눠 쓰도록** 설계돼 있어서, 워커가 늘면 워커당 속도가 그만큼 줄어든다. 여러 테이블을 동시에 건드리게 되는 효과는 있지만 총량은 거의 그대로다.

처리량을 올리려면 워커 수가 아니라 `vacuum_cost_limit`을 올리거나 `autovacuum_vacuum_cost_delay`를 줄여야 한다. 디스크에 여유가 있는 환경이라면 이쪽이 훨씬 직접적이다.

## 메모리와 인덱스 재방문

한 가지 더 걸리는 지점이 있다.

VACUUM은 정리 대상 튜플의 위치를 메모리에 모았다가 인덱스를 훑으면서 지운다. 이 메모리가 `maintenance_work_mem`(autovacuum은 `autovacuum_work_mem`, 기본 -1이면 앞의 값을 따름)으로 제한된다.

죽은 튜플이 한 번에 다 안 담기면 어떻게 되냐면, 인덱스 전체를 여러 번 훑는다. 인덱스가 다섯 개인 테이블에서 이 일이 세 번 반복되면 인덱스 스캔이 열다섯 번이다. VACUUM이 유난히 오래 걸리는 사례의 상당수가 여기에 해당한다.

그래서 큰 테이블이 있는 인스턴스에서는 `maintenance_work_mem`을 넉넉히 주는 게 효과가 크다. PostgreSQL 17에서 이 자료구조가 훨씬 효율적인 방식으로 바뀌어 같은 메모리로 더 많은 튜플을 담을 수 있게 됐지만, 그 이전 버전이라면 신경 쓸 만한 항목이다.

## 진행 상황 보기

지금 돌고 있는 VACUUM이 어디쯤인지는 이렇게 본다.

```sql
SELECT p.pid, c.relname, p.phase,
       p.heap_blks_total, p.heap_blks_scanned, p.heap_blks_vacuumed,
       p.index_vacuum_count,
       round(100.0 * p.heap_blks_scanned / NULLIF(p.heap_blks_total,0), 1) AS pct
FROM pg_stat_progress_vacuum p
JOIN pg_class c ON c.oid = p.relid;
```

`index_vacuum_count`가 1보다 크면 위에서 말한 인덱스 재방문이 실제로 일어나고 있다는 뜻이다. 이 값을 보고 `maintenance_work_mem`을 올릴지 판단할 수 있다.

마지막으로 언제 돌았는지는 이렇게 본다.

```sql
SELECT relname, n_live_tup, n_dead_tup,
       last_autovacuum, autovacuum_count,
       last_autoanalyze, autoanalyze_count
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 20;
```

`last_autovacuum`이 오래됐는데 `n_dead_tup`이 크다면 발동 조건에 아직 안 닿은 것이고, 값이 계속 갱신되는데도 죽은 튜플이 안 줄면 정리 자체를 못 하고 있는 것이다. 후자는 설정 문제가 아니라 다른 원인이고, 다음 편 주제다.

## 테이블별로 따로 주기

전역 기본값을 공격적으로 바꾸면 작은 테이블까지 필요 이상으로 자주 돌게 된다. 그래서 문제가 되는 테이블에만 따로 주는 방식을 쓴다.

```sql
-- 갱신이 잦은 큰 테이블: 비율 대신 고정 건수에 가깝게
ALTER TABLE big_busy_table SET (
  autovacuum_vacuum_scale_factor = 0.01,   -- 20% → 1%
  autovacuum_vacuum_threshold    = 5000,
  autovacuum_analyze_scale_factor= 0.02,
  autovacuum_vacuum_cost_delay   = 0       -- 이 테이블만 브레이크 해제
);

-- 설정 확인
SELECT relname, reloptions FROM pg_class WHERE relname = 'big_busy_table';

-- 되돌리기
ALTER TABLE big_busy_table RESET (autovacuum_vacuum_scale_factor);
```

`scale_factor`를 0으로 두고 `threshold`만 쓰면 크기와 무관하게 고정 건수로 발동시킬 수도 있다. 아주 큰 테이블에서 자주 쓰는 방식이다.

## 그 밖에 autovacuum이 멈추는 경우

설정과 무관하게 안 도는 경우도 있다.

**충돌하는 잠금에 밀린다.** 일반 autovacuum은 다른 작업을 막게 되면 스스로 물러난다. DDL이 잦은 테이블은 시작했다가 취소되기를 반복하며 계속 밀릴 수 있다. 로그에서 취소 기록이 반복되는지 확인해볼 만하다.

**테이블별로 꺼져 있다.** 과거에 누가 `autovacuum_enabled = false`를 걸어놓고 잊은 경우다.

```sql
SELECT relname, reloptions FROM pg_class
WHERE reloptions::text LIKE '%autovacuum_enabled%';
```

**워커가 다 물려 있다.** 큰 테이블 몇 개가 워커를 오래 점유하면 나머지가 대기한다. `pg_stat_activity`에서 `autovacuum:`으로 시작하는 쿼리를 보면 확인된다.

## 정리

- 기본 발동 조건은 행 수에 비례하므로 큰 테이블일수록 늦게 돈다
- 워커를 늘려도 총량은 거의 안 는다. 비용 한도를 올리거나 지연을 줄여야 한다
- `maintenance_work_mem`이 부족하면 인덱스를 여러 번 훑는다. `index_vacuum_count`로 확인한다
- 전역값보다 테이블별 스토리지 파라미터로 접근하는 게 안전하다
- 발동은 하는데 죽은 튜플이 안 줄면 설정 문제가 아니다

다음 편은 그 경우, VACUUM이 돌고 있는데도 죽은 튜플을 못 지우는 상황을 다룬다.

## 자주 묻는 질문

**Q. autovacuum이 큰 테이블에서 잘 안 도는 이유는 무엇인가요?**

기본 발동 조건이 테이블 행 수에 비례하기 때문입니다. 임계값은 `autovacuum_vacuum_threshold`에 `autovacuum_vacuum_scale_factor`와 행 수를 곱한 값을 더해 계산하는데, scale factor 기본값이 0.2라 1억 행 테이블은 죽은 튜플이 약 2천만 개 쌓여야 발동합니다. 그때는 이미 부풀어 있고 한 번의 작업량도 커집니다.

**Q. autovacuum_max_workers를 늘리면 정리가 빨라지나요?**

대체로 아닙니다. 비용 기반 지연의 한도는 동시에 도는 워커들이 나눠 쓰도록 설계되어 있어서, 워커 수를 늘리면 워커당 처리 속도가 그만큼 줄어듭니다. 전체 처리량을 올리려면 워커 수가 아니라 `vacuum_cost_limit`을 올리거나 `autovacuum_vacuum_cost_delay`를 줄여야 합니다.

**Q. 테이블마다 autovacuum 설정을 다르게 줄 수 있나요?**

가능합니다. `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_threshold = 5000)` 형태로 스토리지 파라미터를 지정하면 해당 테이블에만 적용됩니다. 갱신이 잦은 소수의 큰 테이블에만 공격적인 값을 주고 나머지는 전역 기본값을 쓰는 방식이 일반적입니다.
