---
title: "[PostgreSQL] STRING_AGG — 여러 행을 쉼표로 합치기 (GROUP_CONCAT, LISTAGG 대체)"
date: '2026-11-28 12:00:00'
draft: true
categories: ['PostgreSQL']
tags: ['PostgreSQL', 'SQL', 'STRING_AGG', '집계 함수']
description: "PostgreSQL에서 여러 행의 문자열을 하나로 합칠 때는 STRING_AGG(컬럼, ', ')를 씁니다. MySQL GROUP_CONCAT, Oracle LISTAGG에 해당하며, ORDER BY 정렬, DISTINCT 중복 제거, 숫자 컬럼 형 변환, NULL 처리, ARRAY_AGG와의 차이를 예제로 정리합니다."
faq:
  - q: "PostgreSQL에서 GROUP_CONCAT은 무엇으로 대체하나요?"
    a: "STRING_AGG(컬럼, ', ')를 씁니다. MySQL의 GROUP_CONCAT, Oracle의 LISTAGG와 같은 역할로, GROUP BY로 묶인 행의 문자열을 구분자로 이어 붙입니다. PostgreSQL 9.0부터 지원합니다."
  - q: "STRING_AGG 결과 순서를 정하려면 어떻게 하나요?"
    a: "함수 안에 ORDER BY를 넣습니다. STRING_AGG(name, ', ' ORDER BY name)처럼 쓰며, 쿼리 바깥의 ORDER BY는 합쳐지는 순서에 영향을 주지 않습니다."
  - q: "숫자 컬럼에 STRING_AGG를 쓰면 에러가 나요."
    a: "STRING_AGG는 text(또는 bytea)만 받으므로 id::text처럼 문자열로 형 변환해야 합니다. 그렇지 않으면 function string_agg(integer, unknown) does not exist 에러가 납니다."
---

**결론부터:** PostgreSQL에서 여러 행을 한 줄로 합치려면 `STRING_AGG(컬럼, 구분자)`를 씁니다. MySQL의 `GROUP_CONCAT`, Oracle의 `LISTAGG`에 해당합니다.

```sql
SELECT dept, STRING_AGG(name, ', ') AS members
FROM employees
GROUP BY dept;
```

| dept | members |
|---|---|
| 개발 | 김철수, 이영희, 박민수 |
| 영업 | 최지훈, 정수진 |

## 예제 테이블

```sql
CREATE TABLE employees (
  id   int,
  dept text,
  name text
);

INSERT INTO employees VALUES
  (1, '개발', '김철수'), (2, '개발', '이영희'), (3, '개발', '박민수'),
  (4, '영업', '최지훈'), (5, '영업', '정수진'), (6, '영업', NULL);
```

## 1. 정렬 — 함수 안에 ORDER BY

합쳐지는 순서는 보장되지 않습니다. 순서가 중요하면 **함수 안에** `ORDER BY`를 넣습니다.

```sql
SELECT dept, STRING_AGG(name, ', ' ORDER BY name) AS members
FROM employees
GROUP BY dept;
```

쿼리 끝의 `ORDER BY`는 결과 행의 순서만 바꿀 뿐, 문자열 안의 순서에는 영향을 주지 않습니다.

## 2. 중복 제거 — DISTINCT

```sql
SELECT STRING_AGG(DISTINCT dept, ', ' ORDER BY dept) FROM employees;
-- 개발, 영업
```

`DISTINCT`와 `ORDER BY`를 같이 쓸 때는 정렬 기준이 합치는 대상과 같아야 합니다. 다른 컬럼으로 정렬하면 다음 에러가 납니다.

```
ERROR: in an aggregate with DISTINCT, ORDER BY expressions must appear in argument list
```

## 3. 숫자 컬럼은 형 변환

`STRING_AGG`는 `text`만 받습니다.

```sql
SELECT STRING_AGG(id, ',') FROM employees;
-- ERROR: function string_agg(integer, unknown) does not exist

SELECT STRING_AGG(id::text, ',') FROM employees;
-- 1,2,3,4,5,6
```

## 4. NULL 처리

NULL 값은 **건너뜁니다**(구분자도 붙지 않습니다). 위 예제의 영업 부서 NULL 행은 결과에 나타나지 않습니다. 빈자리를 표시하고 싶다면 `COALESCE`로 바꿉니다.

```sql
SELECT dept, STRING_AGG(COALESCE(name, '(미정)'), ', ') FROM employees GROUP BY dept;
```

모든 값이 NULL이거나 행이 없으면 결과는 빈 문자열이 아니라 **NULL**입니다. 필요하면 `COALESCE(STRING_AGG(...), '')`로 감쌉니다.

## 5. 줄바꿈 등 다른 구분자

```sql
SELECT STRING_AGG(name, E'\n') FROM employees;   -- 줄바꿈
SELECT STRING_AGG(name, ' | ') FROM employees;   -- 임의 문자열
```

## 6. ARRAY_AGG와 차이

| | STRING_AGG | ARRAY_AGG |
|---|---|---|
| 결과 형 | text | 배열 (text[], int[] 등) |
| 입력 형 | text, bytea | 아무 형 |
| NULL | 건너뜀 | 배열 원소로 포함 |
| 용도 | 화면 표시, 리포트 | 애플리케이션에서 다시 파싱, `ANY()` 조건 |

배열로 모은 뒤 문자열로 만들 수도 있습니다: `ARRAY_TO_STRING(ARRAY_AGG(name), ', ')`.

## 7. 반대로 — 문자열을 행으로 나누기

```sql
SELECT UNNEST(STRING_TO_ARRAY('김철수, 이영희', ', '));
-- 정규식으로: SELECT REGEXP_SPLIT_TO_TABLE('김철수, 이영희', ',\s*');
```

## 주의점

- **대용량 그룹:** 한 그룹에 수십만 행이 모이면 결과 문자열이 그만큼 커지고 메모리를 씁니다. 화면용이라면 서브쿼리로 개수를 먼저 제한하세요.
- **윈도우 함수로도 됩니다:** `STRING_AGG(name, ', ') OVER (PARTITION BY dept)`로 행을 줄이지 않고 각 행 옆에 목록을 붙일 수 있습니다. ([윈도우 함수](/posts/40/))
