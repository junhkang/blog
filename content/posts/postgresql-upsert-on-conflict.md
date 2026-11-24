---
title: "[PostgreSQL] UPSERT — INSERT ON CONFLICT DO UPDATE 사용법과 주의점"
date: '2026-11-24 12:00:00'
categories: ['PostgreSQL']
tags: ['PostgreSQL', 'SQL', 'UPSERT', 'ON CONFLICT', 'MERGE']
description: "PostgreSQL에서 UPSERT는 INSERT ... ON CONFLICT (키) DO UPDATE SET ... = EXCLUDED....로 합니다. 충돌 대상 지정, EXCLUDED, DO NOTHING, 조건부 갱신, MERGE와의 차이, 시퀀스 소모 같은 실무 주의점을 예제로 정리합니다."
faq:
  - q: "PostgreSQL에서 UPSERT는 어떻게 하나요?"
    a: "INSERT INTO t (id, name) VALUES (1, 'a') ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name; 처럼 씁니다. 충돌 대상(id)에는 유니크 인덱스나 기본 키가 있어야 하고, EXCLUDED는 넣으려다 충돌한 새 행을 가리킵니다. PostgreSQL 9.5부터 지원합니다."
  - q: "ON CONFLICT DO NOTHING은 언제 쓰나요?"
    a: "이미 있는 행은 그대로 두고 없는 행만 넣고 싶을 때 씁니다. 중복 이벤트 적재처럼 같은 데이터가 여러 번 들어올 수 있는 경우에 에러 없이 무시할 수 있습니다. 충돌로 건너뛴 행은 RETURNING에 나오지 않습니다."
  - q: "MERGE와 ON CONFLICT는 무엇이 다른가요?"
    a: "MERGE(PostgreSQL 15+)는 조인 조건으로 일치/불일치를 판단해 INSERT·UPDATE·DELETE를 고를 수 있어 표현력이 넓습니다. 반면 동시에 같은 키를 넣는 경쟁 상황에서 유니크 위반 에러가 날 수 있습니다. ON CONFLICT는 유니크 인덱스에 기대어 동시 실행에서도 원자적으로 동작하므로 단순 UPSERT에는 ON CONFLICT가 더 안전합니다."
---

**결론부터:** PostgreSQL의 UPSERT는 `INSERT ... ON CONFLICT (키) DO UPDATE SET 컬럼 = EXCLUDED.컬럼`입니다. 충돌 대상에는 반드시 기본 키나 유니크 인덱스가 있어야 하고, 9.5부터 쓸 수 있습니다.

```sql
INSERT INTO users (id, email, name)
VALUES (1, 'a@example.com', '홍길동')
ON CONFLICT (id)
DO UPDATE SET email = EXCLUDED.email,
              name  = EXCLUDED.name;
```

아래에서 문법 요소 하나씩, 그리고 운영에서 실제로 밟는 함정을 정리합니다.

## 예제 테이블

```sql
CREATE TABLE users (
  id         bigint PRIMARY KEY,
  email      text   NOT NULL UNIQUE,
  name       text,
  login_cnt  int    NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

## 1. 충돌 대상 지정 — `ON CONFLICT (컬럼)`

괄호 안 컬럼 조합에 **정확히 맞는 유니크 인덱스(또는 기본 키)**가 있어야 합니다. 없으면 다음 에러가 납니다.

```
ERROR: there is no unique or exclusion constraint matching the ON CONFLICT specification
```

제약 조건 이름으로 지정할 수도 있습니다.

```sql
ON CONFLICT ON CONSTRAINT users_email_key DO UPDATE ...
```

부분 유니크 인덱스를 쓰는 경우에는 인덱스의 조건까지 같이 적어야 그 인덱스가 선택됩니다.

```sql
CREATE UNIQUE INDEX ON users (email) WHERE deleted_at IS NULL;

INSERT ... ON CONFLICT (email) WHERE deleted_at IS NULL DO UPDATE ...
```

## 2. `EXCLUDED` — 넣으려던 새 값

`DO UPDATE` 안에서 테이블 이름(`users.name`)은 **기존 행**, `EXCLUDED.name`은 **넣으려다 충돌한 새 행**입니다. 둘을 섞어 누적 갱신을 할 수 있습니다.

```sql
INSERT INTO users (id, email, login_cnt)
VALUES (1, 'a@example.com', 1)
ON CONFLICT (id)
DO UPDATE SET login_cnt  = users.login_cnt + 1,
              updated_at = now();
```

## 3. `DO NOTHING` — 없을 때만 넣기

```sql
INSERT INTO users (id, email) VALUES (1, 'a@example.com')
ON CONFLICT DO NOTHING;
```

충돌 대상을 생략하면 **어떤 유니크 제약이든** 충돌 시 건너뜁니다. 중복 이벤트를 적재할 때 편하지만, 의도하지 않은 제약(예: email) 충돌까지 조용히 삼키므로 가능하면 대상을 명시하는 편이 좋습니다.

## 4. 바뀐 게 있을 때만 갱신 — `WHERE`

값이 같아도 UPDATE는 새 행 버전을 만들고(MVCC), 트리거·`updated_at`도 움직입니다. 변경이 있을 때만 갱신하려면 `WHERE`를 붙입니다.

```sql
INSERT INTO users (id, email, name)
VALUES (1, 'a@example.com', '홍길동')
ON CONFLICT (id)
DO UPDATE SET name = EXCLUDED.name, updated_at = now()
WHERE users.name IS DISTINCT FROM EXCLUDED.name;
```

`IS DISTINCT FROM`은 NULL끼리 비교도 올바르게 처리합니다(`<>`는 NULL이면 결과가 NULL이라 갱신되지 않습니다).

## 5. 들어갔는지 갱신됐는지 구분 — `RETURNING`

```sql
INSERT INTO users (id, email) VALUES (1, 'a@example.com')
ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email
RETURNING id, (xmax = 0) AS inserted;
```

`xmax = 0`이면 새로 INSERT된 행, 아니면 UPDATE된 행입니다. 널리 쓰이는 관용구지만 시스템 컬럼 구현에 기댄 것이라 공식 보장은 아닙니다. 정확히 알아야 한다면 별도 컬럼으로 관리하세요. `DO NOTHING`으로 건너뛴 행은 아예 반환되지 않습니다.

## 6. 여러 행 한 번에

```sql
INSERT INTO users (id, email, name) VALUES
  (1, 'a@example.com', 'A'),
  (2, 'b@example.com', 'B')
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name;
```

한 문장 안에 **같은 키가 두 번** 들어가면 다음 에러가 납니다.

```
ERROR: ON CONFLICT DO UPDATE command cannot affect row a second time
```

배치 적재라면 넣기 전에 `DISTINCT ON (id)` 등으로 키 중복을 먼저 제거해야 합니다.

## 운영에서 밟는 함정

- **시퀀스가 건너뜁니다.** `serial`/`identity` 기본값은 충돌 여부와 상관없이 먼저 소비됩니다. UPSERT가 대부분 UPDATE로 끝나는 테이블이면 id가 듬성듬성해집니다. 정상 동작이니 id 연속성에 의존하지 마세요.
- **UPDATE 비용은 그대로입니다.** 충돌 시 일반 UPDATE와 똑같이 죽은 튜플이 생깁니다. 대량 UPSERT 후 테이블이 부푼다면 4번의 `WHERE`로 무의미한 갱신을 줄이고 autovacuum 설정을 확인하세요. ([autovacuum 튜닝](/posts/postgresql-vacuum-2-autovacuum-tuning/))
- **잠금이 걸립니다.** 충돌한 행에는 행 잠금이 걸리므로, 여러 세션이 같은 키 집합을 다른 순서로 UPSERT하면 데드락이 날 수 있습니다. 배치는 키 순서로 정렬해서 넣는 것이 안전합니다. ([데드락은 어떻게 생기고 어떻게 읽나](/posts/postgresql-lock-2-deadlock/))
- **파티션 테이블:** 유니크 인덱스에 파티션 키가 포함돼야 하므로 충돌 대상도 파티션 키를 포함해야 합니다. ([파티셔닝 제약](/posts/postgresql-partition-3-constraints/))

## MERGE와 무엇을 쓸까

PostgreSQL 15부터 표준 SQL `MERGE`가 있습니다.

```sql
MERGE INTO users u
USING (VALUES (1, 'a@example.com', 'A')) AS s(id, email, name)
ON u.id = s.id
WHEN MATCHED THEN UPDATE SET name = s.name
WHEN NOT MATCHED THEN INSERT (id, email, name) VALUES (s.id, s.email, s.name);
```

조건에 따라 DELETE까지 고를 수 있어 표현력은 넓지만, 두 세션이 동시에 같은 새 키를 넣으면 한쪽이 유니크 위반으로 실패할 수 있습니다. **단순 "있으면 갱신, 없으면 삽입"은 `ON CONFLICT`**, 소스 테이블과 동기화하며 삭제까지 다뤄야 할 때 `MERGE`를 쓰는 것이 무난합니다.

## 요약

| 하고 싶은 것 | 문법 |
|---|---|
| 있으면 갱신, 없으면 삽입 | `ON CONFLICT (키) DO UPDATE SET c = EXCLUDED.c` |
| 없을 때만 삽입 | `ON CONFLICT (키) DO NOTHING` |
| 값이 바뀔 때만 갱신 | `... DO UPDATE ... WHERE t.c IS DISTINCT FROM EXCLUDED.c` |
| 누적 | `SET cnt = t.cnt + EXCLUDED.cnt` |
| 삽입/갱신 구분 | `RETURNING (xmax = 0) AS inserted` |
