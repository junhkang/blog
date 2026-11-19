# SEO 일일 점검 (네이버 서치어드바이저)

매일 Devin 자동화가 이 문서대로 돈다. 사람이 수동으로 돌려도 같다.
GSC·GA4 는 자격 증명이 생기면 §6 에 단계를 추가한다.

## 1. 준비

```bash
pip install -q playwright          # 브라우저는 내려받지 않는다 — 이미 떠 있는 Chrome(CDP :29229)에 붙는다
hugo version                       # v0.167.0 extended
```

네이버 로그인은 조직 blueprint 의 브라우저 프로필로 복원된다. 비밀번호·OTP 는 쓰지 않는다.

## 2. 수집

```bash
python3 scripts/seo/naver_report.py --out /tmp/seo
```

- 종료 코드 `3` = 로그인 만료 → §5. `4` = 수집 실패(네이버 API 변경·장애) → 한 번 재시도 후 §5.
- 결과: `/tmp/seo/naver_report.md`(요약), `naver_findings.json`(판단용), `naver_raw.json`(원본).
- 스크립트는 네이버가 지적한 URL을 **라이브 페이지로 다시 확인**하고, 이미 고쳐졌거나 의도된 것은 뺀다.
  네이버 진단은 2~3일 늦게 갱신된다.

의도된 상태(문제 아님):
- `/tags/*` 는 `noindex, follow` 다(얇은 페이지). 네이버 `3003`(meta robots 색인 제외)에 잡혀도 정상이다.
- `/categories/sankun/`, `/categories/personal/` 은 섹션 URL 로 canonical 된다.

## 3. 분류

| 심각도 | 예 | 처리 |
|---|---|---|
| high | robots 차단, 인증서·HTTPS·사이트맵 이상, 수집 오류(4xx/5xx/타임아웃), 색인 수 10% 이상 감소, 진단 2xxx | 코드 수정 없이 **이슈**(§5). 원인이 저장소에 있으면 PR 로 제안 |
| medium | 중복·누락 description/title, H1 여러 개, alt 누락, 노출 30% 이상 감소 | §4 규칙으로 수정 |
| info | 노출은 있는데 클릭 0 인 글(`low_ctr`) | description 이 비었거나 일반적일 때만 §4-A 로 개선. 아니면 보고만 |

## 4. 수정

작업 브랜치 `devin/<timestamp>-seo-<요약>`, PR 라벨 `seo-monitor`. 같은 문제로 열린 `seo-monitor` PR 이 있으면 새로 만들지 않는다.
하루 PR 1개.

수정 전후 모두:

```bash
hugo --minify --baseURL "https://junhkang.com/" -d /tmp/pub && python3 scripts/seo/audit_build.py /tmp/pub
```

`audit_build.py` 는 사이트맵에 오른 페이지의 description·canonical·H1·alt·중복 title/description,
사이트맵-noindex 겹침, 깨진 내부 링크를 본다. main 기준보다 문제가 늘면 그 PR 은 만들지 않는다.

### 4-A. 자동 머지해도 되는 변경 (모두 만족할 때만)

- 바뀐 파일이 전부 `content/**/*.md` 이고, 바뀐 줄이 front matter 의 `description:` / `summary:` 이거나 본문 이미지 alt(`![여기](…)`) 뿐이다.
- 파일 10개 이하.
- 같은 글의 description 을 최근 30일 안에 바꾼 적이 없다(`git log --since=30.days -- <파일>`).
- description 은 글 본문에 실제로 있는 내용으로, 80~160자, 다른 페이지와 겹치지 않게 쓴다. 과장·키워드 나열 금지.
- 빌드·감사 통과.

머지 후: GitHub Pages 워크플로 성공을 확인하고, 바뀐 URL을 `curl` 로 받아 새 description 과 `index, follow` 를 확인한다.
그다음 바뀐 URL 만 네이버에 수집 요청한다(하루 10개 이하):

```bash
python3 scripts/seo/naver_report.py --request-crawl https://junhkang.com/posts/40/ …
```

### 4-B. PR 만 (머지하지 않음)

위 조건을 하나라도 벗어나면 전부. 특히:
`layouts/`, `hugo.toml`, `static/`(robots.txt 포함), `.github/`, 테마, title·slug·url·aliases·date·draft·canonicalURL·robots·tags·categories 변경,
글 삭제·이동, noindex 정책 변경. PR 설명에 네이버 진단 코드·대상 URL·라이브 확인 결과를 적는다.

### 하지 않는 것

- URL 구조 변경, 네이버 "웹 페이지 수집 제외(삭제) 요청", 사이트맵 제출 삭제.
- 태그 페이지 noindex 해제.
- 네이버 콘솔의 다른 설정 변경.

## 5. 알림

GitHub 이슈(라벨 `seo-monitor`)로 남긴다. 같은 제목의 열린 이슈가 있으면 댓글만 단다. 해결되면 닫는다.

- 로그인 만료: 제목 `네이버 서치어드바이저 로그인 만료`. 본문에 "Devin 세션에서 VM 브라우저로 searchadvisor.naver.com 에 다시 로그인한 뒤 브라우저 프로필 저장을 승인해 주세요"라고 쓴다.
- high 문제: 제목 `[SEO] <문제 제목>`, 본문에 `naver_report.md` 의 해당 부분.

## 6. 확장 자리 (GSC·GA4)

자격 증명(`GOOGLE_SA_JSON`)이 생기면 `scripts/seo/gsc_report.py` 를 추가해 §2 다음에 돌린다.
같은 `findings` 형식(`severity, code, title, detail, urls`)으로 내보내면 §3 이후는 그대로 쓴다.
