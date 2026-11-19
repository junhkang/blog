# ux-audit

블로그 화면을 실제 브라우저로 열어 측정하고, 개선 전후를 비교하는 도구. 사람 또는 Devin이 주기적으로 돌려 UI/UX를 고도화하는 데 쓴다.

## 측정 항목

| 구분 | 내용 |
|---|---|
| 스크린샷 | `pages.json` 의 페이지 × 모바일 390 / 태블릿 768 / 데스크톱 1366 × 라이트·다크 |
| 레이아웃 | 가로 넘침, 화면 밖으로 밀린 메뉴, 24px 미만 터치 영역, 본문 글자 크기·줄 간격·한 줄 글자 수 |
| 접근성 | axe-core (WCAG 2.1 AA), 라이트 테마 모바일·데스크톱 |
| 성능 | Lighthouse 모바일 3회 중앙값 (`--lighthouse`), 광고·분석 포함 |
| SEO 가드 | 운영 baseURL 로 빌드한 HTML 의 URL 목록, sitemap, canonical, robots, title, description, JSON-LD |

스크린샷이 흔들리지 않도록 광고·GA·Giscus 요청은 스크린샷 측정에서만 막는다.

## 실행

```bash
cd tools/ux-audit
npm ci && npx playwright install chromium   # 처음 한 번
npm run before    # origin/main 을 빌드해 out/before 에 측정
npm run after     # 현재 작업 트리를 빌드해 out/after 에 측정
npm run compare   # out/after/summary.md, out/after/diff/*.png 생성. 가드 실패 시 exit 1
```

한글 글꼴이 없는 Linux 에서는 한글이 깨져 측정이 무의미하다. `sudo apt-get install -y fonts-noto-cjk fonts-noto-color-emoji` 가 필요하다.

## 가드 (하나라도 깨지면 PR 금지)

- URL 추가·삭제, sitemap 변경, canonical·robots·title·description·JSON-LD 변경 없음
- `hugo.toml` 의 `pagerSize` 등 URL 을 바꾸는 설정은 건드리지 않음
- 숨은 메뉴·가로 넘침·작은 터치 영역·axe 위반 수가 늘지 않음
- Lighthouse SEO·접근성 점수가 떨어지지 않음, CLS +0.02 이내, LCP +15%(+200ms) 이내

## 개선 루프

1. `npm run before` 로 현재 상태 측정, `out/before/shots` 와 `report.json` 을 보고 문제를 고른다.
2. `layouts/`, `assets/css/extended/` 에서만 수정한다. `themes/hugo-PaperMod` 는 직접 고치지 않는다.
3. `npm run after && npm run compare` 로 확인한다. 가드가 통과하면 summary 표와 전후 스크린샷을 PR 에 붙인다.
