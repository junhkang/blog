---
title: "[PostgreSQL] VACUUM은 도는데 죽은 튜플이 안 줄어들 때"
date: '2026-09-11 21:15:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', 'Vacuum', 'MVCC', '복제', '트러블슈팅', '운영']
series: ['PostgreSQL VACUUM 깊이 보기']
description: "VACUUM은 아직 누군가 볼 수 있는 튜플을 지우지 못합니다. 오래 열려 있는 트랜잭션, 방치된 복제 슬롯, 준비된 트랜잭션이 정리 기준선을 붙잡는 구조와 진단 순서를 정리합니다."
faq:
  - q: "autovacuum이 계속 도는데 죽은 튜플이 줄지 않는 이유는 무엇인가요?"
    a: "VACUUM은 아직 어떤 트랜잭션에게 보일 가능성이 있는 튜플을 지우지 못하기 때문입니다. 오래 열려 있는 트랜잭션이나 방치된 복제 슬롯이 정리 기준선을 과거에 묶어두면, VACUUM은 정상적으로 실행되고도 지울 대상이 없다고 판단하고 끝납니다. 이 경우 설정을 아무리 공격적으로 바꿔도 해결되지 않습니다."
  - q: "정리를 막고 있는 원인을 어떻게 찾나요?"
    a: "정리 기준선을 붙잡을 수 있는 곳은 크게 네 군데입니다. pg_stat_activity의 backend_xmin으로 실행 중인 트랜잭션을, pg_replication_slots의 xmin과 catalog_xmin으로 복제 슬롯을, pg_prepared_xacts로 준비된 트랜잭션을, pg_stat_replication의 backend_xmin으로 대기 서버 피드백을 확인합니다. 이 중 가장 오래된 값이 실제 기준선입니다."
  - q: "사용하지 않는 복제 슬롯을 방치하면 어떻게 되나요?"
    a: "비활성 슬롯도 자신이 필요로 하는 지점을 계속 유지하므로, WAL이 쌓여 디스크를 채우고 정리 기준선을 과거에 묶어 죽은 튜플이 무한정 누적됩니다. 복제를 중단하거나 대기 서버를 폐기할 때 슬롯을 함께 제거하지 않으면 서서히 진행되다가 한 번에 문제가 되는 전형적인 사고 경로입니다."
---

앞선 두 편에서 [VACUUM이 공간을 반환하지 않는다는 것](/posts/postgresql-vacuum-1-disk-not-returned/)과 [autovacuum 발동 조건](/posts/postgresql-vacuum-2-autovacuum-tuning/)을 다뤘다. 이번 편은 성격이 다르다.

**설정을 아무리 만져도 해결되지 않는 경우**가 있다. autovacuum은 정상적으로 돌고, 실행 기록도 계속 갱신되는데, 죽은 튜플만 계속 늘어난다.

이건 VACUUM이 게을러서가 아니라 **지울 권한이 없어서**다.

## 정리 기준선

VACUUM은 아무 죽은 튜플이나 지우지 않는다. 지워도 되는지 판단하는 기준선이 있다.

기준은 단순하다. **지금 실행 중이거나 앞으로 시작할 어떤 트랜잭션도 이 튜플을 볼 수 없는가.** 하나라도 볼 가능성이 있으면 못 지운다. 지웠다가는 그 트랜잭션이 읽어야 할 데이터가 사라진다.

그래서 시스템 전체에서 **가장 오래된 트랜잭션 하나**가 기준선을 결정한다. 한 시간 전에 열린 트랜잭션이 아직 살아 있으면, 그 뒤로 죽은 모든 튜플은 지울 수 없다. 그 트랜잭션이 그 테이블을 건드리든 안 건드리든 상관없다.

이 구조 때문에 증상이 이렇게 나타난다.

- `last_autovacuum`은 계속 갱신된다 (VACUUM은 정상적으로 돌고 있다)
- `n_dead_tup`은 줄지 않는다 (지울 대상이 없다고 판정하고 끝난다)
- 테이블은 계속 커진다
- **설정을 공격적으로 바꿔도 아무 변화가 없다**

마지막 줄이 진단의 핵심이다. 튜닝을 했는데 아무 반응이 없다면 원인이 튜닝 쪽에 없다는 신호다.

## 기준선을 붙잡는 네 가지

### 1. 오래 열려 있는 트랜잭션

가장 흔하다. 배치 작업, 분석 쿼리, 그리고 **애플리케이션이 트랜잭션을 열어놓고 잊은 경우**다.

```sql
SELECT pid, state,
       age(backend_xmin)              AS xmin_age,
       now() - xact_start             AS xact_duration,
       now() - state_change           AS in_state,
       left(query, 80)                AS query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY age(backend_xmin) DESC
LIMIT 20;
```

`backend_xmin`이 NULL이 아니면 그 백엔드가 기준선을 붙잡고 있다는 뜻이다. `age()`가 클수록 오래 붙잡고 있다.

특히 봐야 할 건 `state`가 **`idle in transaction`**인 것들이다. 쿼리는 안 돌고 있는데 트랜잭션은 열려 있는 상태다. 커넥션 풀에서 트랜잭션을 시작해놓고 애플리케이션 로직이 오래 걸리거나, 예외 경로에서 커밋도 롤백도 안 하고 빠져나갔을 때 생긴다. 이건 아무 일도 안 하면서 정리만 막는다.

대응은 타임아웃이다.

```sql
-- 세션이 트랜잭션을 열어둔 채 놀고 있으면 끊는다
ALTER SYSTEM SET idle_in_transaction_session_timeout = '10min';
SELECT pg_reload_conf();
```

기본값이 0(비활성)이라 명시적으로 켜야 한다. 배치 계정만 예외를 주고 싶으면 `ALTER ROLE ... SET`으로 역할 단위로도 걸 수 있다.

### 2. 복제 슬롯

**가장 조용하게, 가장 오래 문제를 만드는 원인이다.**

복제 슬롯은 구독자가 아직 받아가지 않은 지점을 서버가 계속 유지하도록 만든다. 그래서 슬롯이 살아 있는 한 필요한 WAL이 보존되고, 논리 복제 슬롯의 경우 정리 기준선까지 함께 붙잡힌다.

문제는 **구독자가 사라져도 슬롯은 남는다**는 점이다. 대기 서버를 폐기했거나, 논리 복제를 테스트하고 정리를 안 했거나, CDC 도구를 걷어냈는데 슬롯을 안 지운 경우다. 아무도 안 읽는 슬롯이 남아서 WAL을 쌓고 기준선을 묶는다.

```sql
SELECT slot_name, slot_type, active,
       age(xmin)         AS xmin_age,
       age(catalog_xmin) AS catalog_xmin_age,
       pg_size_pretty(
         pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)
       )                 AS retained_wal
FROM pg_replication_slots
ORDER BY age(xmin) DESC NULLS LAST;
```

`active`가 `false`인데 `retained_wal`이 계속 커지고 있다면 그 슬롯은 버려진 것이다.

```sql
SELECT pg_drop_replication_slot('버려진_슬롯명');
```

지우기 전에 정말 아무도 안 쓰는지 반드시 확인한다. 살아 있는 복제의 슬롯을 지우면 그 복제는 복구 불가능하게 끊긴다.

이 문제는 진행이 느려서 더 위험하다. 몇 주에 걸쳐 서서히 쌓이다가 디스크가 차는 시점에 한꺼번에 드러난다.

### 3. 준비된 트랜잭션

2단계 커밋을 쓰면 `PREPARE TRANSACTION` 이후 커밋도 롤백도 안 된 상태로 남을 수 있다. 이건 영구적으로 열린 트랜잭션이나 마찬가지다. 서버를 재시작해도 사라지지 않는다.

```sql
SELECT gid, prepared, owner, database, age(transaction) AS xid_age
FROM pg_prepared_xacts
ORDER BY age(transaction) DESC;
```

분산 트랜잭션을 안 쓰는데 여기 뭔가 있다면 대개 사고의 잔해다. 확인 후 정리한다.

```sql
ROLLBACK PREPARED '해당_gid';
```

### 4. 대기 서버의 피드백

대기 서버에서 `hot_standby_feedback`이 켜져 있으면, 대기 서버에서 도는 긴 쿼리가 주 서버의 정리 기준선까지 붙잡는다. 대기 서버의 쿼리가 취소되는 걸 막으려고 켜는 옵션인데, 그 대가가 주 서버 쪽 정리 지연이다.

```sql
-- 주 서버에서
SELECT application_name, state,
       age(backend_xmin) AS xmin_age
FROM pg_stat_replication
ORDER BY age(backend_xmin) DESC NULLS LAST;
```

분석용 대기 서버에서 몇 시간짜리 쿼리를 돌리고 있다면 여기가 원인일 수 있다.

## 진단 순서

한 번에 훑는 게 편하다. 네 곳의 값을 모아서 가장 오래된 것을 찾는다.

```sql
SELECT '실행 중 트랜잭션' AS source, max(age(backend_xmin)) AS oldest_age
FROM pg_stat_activity WHERE backend_xmin IS NOT NULL
UNION ALL
SELECT '복제 슬롯', max(greatest(age(xmin), age(catalog_xmin)))
FROM pg_replication_slots
UNION ALL
SELECT '준비된 트랜잭션', max(age(transaction))
FROM pg_prepared_xacts
UNION ALL
SELECT '대기 서버 피드백', max(age(backend_xmin))
FROM pg_stat_replication
ORDER BY oldest_age DESC NULLS LAST;
```

제일 위에 뜬 것이 지금 기준선을 붙잡고 있는 주범이다. 이걸 먼저 풀지 않으면 다른 걸 아무리 해도 소용없다.

`VACUUM (VERBOSE)`를 직접 돌려도 단서가 나온다. 출력에 지울 수 없는 튜플이 있다는 취지의 내용이 함께 표시되므로, 지울 대상이 없어서 끝난 것인지 아니면 못 지운 것인지 구분할 수 있다.

## 왜 이게 위험한가

죽은 튜플이 안 지워지면 두 가지가 동시에 나빠진다.

**디스크가 찬다.** 이건 눈에 보이니 그나마 낫다.

**freeze가 밀린다.** 이쪽이 더 위험하다. 정리 기준선이 과거에 묶여 있으면 오래된 트랜잭션 ID를 정리하는 작업도 같이 못 하고, 그 상태가 오래가면 결국 wraparound 한계선에 접근한다. 여기까지 가면 새 트랜잭션 발급이 거부되면서 데이터베이스가 사실상 멈춘다. 이 구조는 [트랜잭션 ID 글](/posts/postgresql-transaction-id-wraparound/)에 정리해뒀다.

그래서 이 항목들은 평상시에 대시보드에 올려두는 게 맞다. 넷 다 하루아침에 나빠지는 값이 아니라서, 보고 있기만 하면 대응할 시간이 충분하다. 반대로 안 보고 있으면 아무 경고 없이 몇 주가 흘러간다.

## 정리

- VACUUM은 아직 누군가 볼 수 있는 튜플을 지우지 못한다
- 시스템에서 가장 오래된 트랜잭션 하나가 전체 기준선을 정한다
- 붙잡는 곳은 넷이다 — 실행 중 트랜잭션, 복제 슬롯, 준비된 트랜잭션, 대기 서버 피드백
- **설정을 바꿨는데 아무 반응이 없으면 원인이 설정에 없다는 뜻이다**
- 복제 슬롯은 조용하고 느리게 진행되므로 특히 주의한다

다음 편에서는 방향을 바꿔서, 애초에 죽은 튜플을 덜 만드는 방법인 HOT update와 fillfactor를 다룬다.

## 자주 묻는 질문

**Q. autovacuum이 계속 도는데 죽은 튜플이 줄지 않는 이유는 무엇인가요?**

VACUUM은 아직 어떤 트랜잭션에게 보일 가능성이 있는 튜플을 지우지 못하기 때문입니다. 오래 열려 있는 트랜잭션이나 방치된 복제 슬롯이 정리 기준선을 과거에 묶어두면, VACUUM은 정상적으로 실행되고도 지울 대상이 없다고 판단하고 끝납니다. 이 경우 설정을 아무리 공격적으로 바꿔도 해결되지 않습니다.

**Q. 정리를 막고 있는 원인을 어떻게 찾나요?**

정리 기준선을 붙잡을 수 있는 곳은 크게 네 군데입니다. `pg_stat_activity`의 `backend_xmin`으로 실행 중인 트랜잭션을, `pg_replication_slots`의 `xmin`과 `catalog_xmin`으로 복제 슬롯을, `pg_prepared_xacts`로 준비된 트랜잭션을, `pg_stat_replication`의 `backend_xmin`으로 대기 서버 피드백을 확인합니다. 이 중 가장 오래된 값이 실제 기준선입니다.

**Q. 사용하지 않는 복제 슬롯을 방치하면 어떻게 되나요?**

비활성 슬롯도 자신이 필요로 하는 지점을 계속 유지하므로, WAL이 쌓여 디스크를 채우고 정리 기준선을 과거에 묶어 죽은 튜플이 무한정 누적됩니다. 복제를 중단하거나 대기 서버를 폐기할 때 슬롯을 함께 제거하지 않으면 서서히 진행되다가 한 번에 문제가 되는 전형적인 사고 경로입니다.
