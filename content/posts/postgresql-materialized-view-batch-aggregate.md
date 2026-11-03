---
title: "[PostgreSQL] 배치마다 반복되는 집계는 Materialized View로 빼라"
date: '2026-11-10 03:00:00'
draft: true
categories: ['PostgreSQL']
tags: ['PostgreSQL', 'view', '성능', '쿼리성능', '운영']
description: "배치로 조금씩 읽어 가는 쿼리에 집계 서브쿼리가 붙어 있으면, LIMIT과 상관없이 배치마다 원천 테이블 전체를 다시 집계합니다. 집계를 Materialized View로 빼고, 결과가 원본과 같은지 EXCEPT ALL로 검증하고, 갱신을 운영하는 방법을 정리합니다."
faq:
  - q: "인덱스를 추가하면 해결되지 않나요?"
    a: "이 유형은 대개 인덱스로 풀리지 않습니다. 집계 서브쿼리는 바깥 쿼리의 LIMIT과 무관하게 GROUP BY 대상 전체를 먼저 계산하기 때문입니다. 필요한 인덱스가 이미 있는데도 느리다면, 배치마다 같은 집계를 반복하는 구조 자체를 의심해야 합니다."
  - q: "Materialized View로 바꾼 결과가 원본과 같은지 어떻게 확인하나요?"
    a: "원본 쿼리와 바꾼 쿼리를 EXCEPT ALL로 양방향 비교해 둘 다 0행인지 봅니다. EXCEPT가 아니라 EXCEPT ALL을 써야 중복 행의 개수 차이까지 잡힙니다. 행 수 비교만으로는 같은 개수의 다른 행을 놓칩니다."
  - q: "REFRESH 중에는 조회가 막히나요?"
    a: "일반 REFRESH MATERIALIZED VIEW는 갱신이 끝날 때까지 해당 뷰의 조회를 막습니다. 뷰에 UNIQUE 인덱스가 있으면 CONCURRENTLY 옵션으로 조회를 막지 않고 갱신할 수 있지만, 차이를 계산하는 만큼 더 오래 걸립니다."
---

검색 색인, 데이터 동기화, 리포트 적재처럼 테이블을 조금씩 읽어서 다른 곳으로 보내는 배치가 있다. 이런 배치의 쿼리가 유난히 느리고, 필요한 인덱스는 다 있는데도 `EXPLAIN ANALYZE`가 끝나지 않는다면 쿼리 모양부터 볼 만하다. 배치마다 같은 집계를 처음부터 다시 하고 있을 가능성이 크다.

이 경우 집계를 Materialized View(MV)로 빼고 `FROM` 절만 바꾸면 된다. 분 단위가 초 단위로 줄어드는 일이 흔하다. 다만 결과가 원본과 정말 같은지 검증하는 단계와, MV를 언제 갱신할지 정하는 단계를 건너뛰면 안 된다.

## 왜 느린가: LIMIT이 집계를 줄여 주지 않는다

예시로 상품을 검색 엔진에 색인하는 배치를 보자. 상품마다 리뷰 통계와 판매량을 붙여서 1만 건씩 보낸다.

```sql
SELECT p.id, p.name, p.price,
       r.review_count, r.avg_rating,
       s.sold_qty
FROM products p
LEFT JOIN (
    SELECT product_id, count(*) AS review_count, avg(rating) AS avg_rating
    FROM reviews
    GROUP BY product_id
) r ON r.product_id = p.id
LEFT JOIN (
    SELECT product_id, sum(qty) AS sold_qty
    FROM order_items
    GROUP BY product_id
) s ON s.product_id = p.id
WHERE p.id > :last_id
ORDER BY p.id
LIMIT 10000;
```

`WHERE p.id > :last_id ... LIMIT 10000`은 [키셋 페이지네이션](/posts/82/)이라 그 자체로는 빠르다. 문제는 붙어 있는 두 서브쿼리다. 플래너는 `reviews` 전체와 `order_items` 전체를 `GROUP BY` 한 뒤에 조인한다. 바깥에서 1만 건만 가져간다는 사실이 서브쿼리 안으로 전달되지 않는다.

배치가 100번 돌면 같은 전체 집계가 100번 반복된다. 원천 테이블이 수백만 행이면 배치 한 번이 분 단위가 된다.

[실행 계획](/posts/26/)에서는 이렇게 보인다.

- `HashAggregate`나 `GroupAggregate` 노드의 `rows`가 원천 테이블 전체 규모다.
- 그 아래가 `Seq Scan on reviews`, `Seq Scan on order_items`다. 인덱스가 있어도 전체를 읽어야 하니 순차 스캔이 맞는 선택이다.
- 바깥 `Limit` 노드의 startup cost가 크다. 첫 행을 내놓기 전에 집계를 다 끝내야 하기 때문이다.

인덱스를 더 만들어도 소용없는 이유가 여기 있다. 읽는 방식이 아니라 매번 전부 계산하는 구조가 문제다.

## 집계를 MV로 옮기는 순서

집계 결과는 배치가 도는 동안 거의 바뀌지 않는다. 그러면 한 번만 계산해 두고 배치는 그걸 읽으면 된다.

1. 서브쿼리를 그대로 MV로 만든다. 이 단계에서 로직을 고치지 않는다. 고치면 검증이 어려워진다.

```sql
CREATE MATERIALIZED VIEW mv_product_review_agg AS
SELECT product_id, count(*) AS review_count, avg(rating) AS avg_rating
FROM reviews
GROUP BY product_id;

CREATE MATERIALIZED VIEW mv_product_sales_agg AS
SELECT product_id, sum(qty) AS sold_qty
FROM order_items
GROUP BY product_id;
```

2. 조인 키에 UNIQUE 인덱스를 만든다. 조인 성능에도 필요하고, 나중에 `REFRESH ... CONCURRENTLY`를 쓰려면 반드시 있어야 한다.

```sql
CREATE UNIQUE INDEX ON mv_product_review_agg (product_id);
CREATE UNIQUE INDEX ON mv_product_sales_agg (product_id);
```

3. 본 쿼리는 `FROM` 절만 바꾼다. `SELECT`, `WHERE`, `ORDER BY`, `LIMIT`은 그대로 둔다.

```sql
SELECT p.id, p.name, p.price,
       r.review_count, r.avg_rating,
       s.sold_qty
FROM products p
LEFT JOIN mv_product_review_agg r ON r.product_id = p.id
LEFT JOIN mv_product_sales_agg  s ON s.product_id = p.id
WHERE p.id > :last_id
ORDER BY p.id
LIMIT 10000;
```

이제 조인은 1만 건에 대해 UNIQUE 인덱스로 한 건씩 찾아가는 일이 된다. 실행 계획에서 집계 노드가 사라지고 `Index Scan using ... on mv_product_review_agg`가 보이면 된다.

## 결과가 같은지 검증하기

속도보다 먼저 확인할 것이 결과다. MV는 원본과 같은 로직이어야 하고, 같은 시점이라면 같은 결과를 내야 한다. 검증은 `EXCEPT ALL` 양방향으로 한다.

```sql
-- 원본에만 있는 행
SELECT count(*) FROM (
  (<원본 쿼리, LIMIT 제거>)
  EXCEPT ALL
  (<MV 쿼리, LIMIT 제거>)
) a;

-- MV 쪽에만 있는 행
SELECT count(*) FROM (
  (<MV 쿼리, LIMIT 제거>)
  EXCEPT ALL
  (<원본 쿼리, LIMIT 제거>)
) b;
```

둘 다 0이면 행 단위로 같다. 몇 가지 주의할 점이 있다.

- `EXCEPT`가 아니라 [`EXCEPT ALL`](/posts/83/)을 쓴다. `EXCEPT`는 중복을 없애고 비교하니 같은 행이 두 번 나오는 차이를 놓친다.
- 행 수만 비교하면 안 된다. 개수가 같아도 내용이 다를 수 있다.
- `LIMIT`을 빼고 전체를 비교한다. 배치 한 번 분량만 보면 경계 근처의 차이를 못 본다.
- `avg()` 같은 실수 결과는 비교 전에 `round()`로 자릿수를 맞춘다. 계산 순서에 따라 마지막 자리가 달라질 수 있다.
- 검증하는 동안 원천 테이블에 쓰기가 들어오면 그만큼 차이가 생긴다. 쓰기가 적은 시간에 REFRESH 직후 비교하고, 차이가 나면 그 행이 REFRESH 이후에 바뀐 것인지부터 확인한다.

전체 비교는 무겁다. 그래도 바꾸기 전에 한 번은 해야 한다. 이 단계를 건너뛰면 빨라졌다는 것 말고는 아는 게 없다.

## MV 갱신 운영

MV는 스냅샷이다. 만든 시점 이후의 변경은 갱신하기 전까지 반영되지 않는다. 운영에서 정할 것은 세 가지다.

**언제 갱신하나.** 가장 단순하고 안전한 건 소비 직전이다. 배치 시작 단계에 `REFRESH`를 넣으면 된다. 배치와 갱신을 따로 스케줄링하면 순서가 꼬이는 날이 생긴다.

**막아도 되나.** 일반 `REFRESH MATERIALIZED VIEW`는 갱신하는 동안 그 뷰를 읽는 쿼리를 막는다. 배치 안에서 순서대로 하면 문제가 없다. 다른 곳에서도 MV를 읽는다면 `CONCURRENTLY`를 쓴다.

```sql
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_product_review_agg;
```

`CONCURRENTLY`는 UNIQUE 인덱스가 있어야 하고, 새 결과와 기존 결과의 차이를 계산하는 만큼 더 오래 걸린다. 읽는 쪽이 배치 하나뿐이면 굳이 쓸 이유가 없다.

**자동화의 함정.** "원천이 바뀌었을 때만 갱신"하는 감지 로직을 만들고 싶어진다. 예를 들면 원천 테이블의 `max(id)`가 커졌는지 보는 식이다. 이때 신호를 잘못 고르면 그 자동화는 영원히 아무것도 하지 않는다. 행을 추가하지 않고 기존 행을 고치는 변경이나, 플래그를 리셋해서 전체를 다시 처리하는 방식이면 `max(id)`는 그대로다. 감지 로직은 실제로 일어나는 변경 경로를 하나하나 대조한 뒤에 만들고, 확신이 없으면 매번 갱신하는 쪽이 낫다. 갱신이 수십 초라면 대개 그 비용을 감수할 만하다.

## MV가 맞는 경우와 안 맞는 경우

| | MV가 맞는다 | 다른 방법을 본다 |
|---|---|---|
| 원천 변경 빈도 | 배치 사이에 거의 안 바뀐다 | 배치 도중에도 계속 바뀐다 |
| 필요한 신선도 | 시간·일 단위 지연을 받아들일 수 있다 | 실시간에 가까워야 한다 |
| 집계 비용 | 전체 집계가 무겁다 | 집계 대상이 작아서 매번 해도 싸다 |
| 소비 방식 | 같은 집계를 여러 번 읽는다 | 한 번 읽고 끝난다 |

오른쪽이라면 집계 결과를 일반 테이블에 두고 원천이 바뀔 때마다 트리거나 애플리케이션에서 증분으로 갱신하는 방식이 맞다. 다만 그건 정합성을 직접 책임져야 하는 방식이라 구현 비용이 MV보다 훨씬 크다. [일반 뷰](/posts/25/)는 저장하지 않으니 이 문제를 해결하지 못한다. 매번 같은 집계를 다시 돌린다.

한 가지 기억할 것은 이 문제가 "인덱스가 다 있는데 느리다"로 나타난다는 점이다. [인덱스를 안 쓰는 이유](/posts/postgresql-planner-3-why-not-index/)를 찾다가 시간을 쓰기 전에, 배치마다 같은 일을 반복하고 있지 않은지 먼저 보면 된다.

## 자주 묻는 질문

**Q. 인덱스를 추가하면 해결되지 않나요?**

이 유형은 대개 인덱스로 풀리지 않습니다. 집계 서브쿼리는 바깥 쿼리의 LIMIT과 무관하게 GROUP BY 대상 전체를 먼저 계산하기 때문입니다. 필요한 인덱스가 이미 있는데도 느리다면, 배치마다 같은 집계를 반복하는 구조 자체를 의심해야 합니다.

**Q. Materialized View로 바꾼 결과가 원본과 같은지 어떻게 확인하나요?**

원본 쿼리와 바꾼 쿼리를 `EXCEPT ALL`로 양방향 비교해 둘 다 0행인지 봅니다. `EXCEPT`가 아니라 `EXCEPT ALL`을 써야 중복 행의 개수 차이까지 잡힙니다. 행 수 비교만으로는 같은 개수의 다른 행을 놓칩니다.

**Q. REFRESH 중에는 조회가 막히나요?**

일반 `REFRESH MATERIALIZED VIEW`는 갱신이 끝날 때까지 해당 뷰의 조회를 막습니다. 뷰에 UNIQUE 인덱스가 있으면 `CONCURRENTLY` 옵션으로 조회를 막지 않고 갱신할 수 있지만, 차이를 계산하는 만큼 더 오래 걸립니다.
