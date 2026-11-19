// Usage: node compare.mjs out/before out/after
// 지표 표(summary.md)와 스크린샷 차이 이미지(diff/)를 만들고, SEO·성능 가드가 깨지면 exit 1.
import fs from 'node:fs';
import path from 'node:path';
import { PNG } from 'pngjs';
import pixelmatch from 'pixelmatch';

const [beforeDir, afterDir] = process.argv.slice(2).map((d) => path.resolve(d));
const before = JSON.parse(fs.readFileSync(path.join(beforeDir, 'report.json')));
const after = JSON.parse(fs.readFileSync(path.join(afterDir, 'report.json')));
const failures = [];
const lines = [];

// 1) 페이지별 레이아웃·접근성 지표
const key = (r) => `${r.page}|${r.viewport}|${r.theme}`;
const bmap = new Map(before.results.map((r) => [key(r), r]));
const axeTotal = (r) => (r.axe || []).reduce((s, v) => s + v.nodes, 0);
const agg = (rs) => ({
  hiddenMenuItems: rs.reduce((s, r) => s + r.hiddenMenuItems.length, 0),
  horizontalOverflowPages: rs.filter((r) => r.horizontalOverflow).length,
  smallTapTargets: rs.reduce((s, r) => s + r.smallTapTargets, 0),
  axeViolationNodes: rs.reduce((s, r) => s + axeTotal(r), 0),
  non200: rs.filter((r) => r.httpStatus !== 200).length,
});
const ab = agg(before.results);
const aa = agg(after.results);
lines.push('### 레이아웃·접근성 (전체 페이지×뷰포트×테마 합계)', '', '| 지표 | 전 | 후 |', '|---|---|---|');
for (const k of Object.keys(ab)) lines.push(`| ${k} | ${ab[k]} | ${aa[k]} |`);
for (const k of Object.keys(ab)) if (aa[k] > ab[k]) failures.push(`${k} 증가 ${ab[k]} → ${aa[k]}`);

lines.push('', '### 페이지별 axe 위반', '', '| 페이지 | 뷰포트 | 전 | 후 |', '|---|---|---|---|');
for (const r of after.results.filter((r) => r.axe)) {
  const b = bmap.get(key(r));
  const fmt = (x) => (x.axe || []).map((v) => `${v.id}(${v.nodes})`).join(', ') || '-';
  if (fmt(b) !== '-' || fmt(r) !== '-') lines.push(`| ${r.page} | ${r.viewport} | ${fmt(b)} | ${fmt(r)} |`);
}

// 2) Lighthouse (있을 때만)
if (Object.keys(after.lighthouse || {}).length && Object.keys(before.lighthouse || {}).length) {
  lines.push('', '### Lighthouse 모바일 (3회 중앙값)', '', '| 페이지 | 지표 | 전 | 후 |', '|---|---|---|---|');
  for (const [pg, a] of Object.entries(after.lighthouse)) {
    const b = before.lighthouse[pg];
    if (!b) continue;
    for (const k of Object.keys(a)) lines.push(`| ${pg} | ${k} | ${b[k]} | ${a[k]} |`);
    if (a.seo < b.seo) failures.push(`${pg} Lighthouse SEO ${b.seo} → ${a.seo}`);
    if (a.accessibility < b.accessibility) failures.push(`${pg} Lighthouse 접근성 ${b.accessibility} → ${a.accessibility}`);
    if (a.cls > b.cls + 0.02) failures.push(`${pg} CLS ${b.cls} → ${a.cls}`);
    if (a.lcpMs > b.lcpMs * 1.15 + 200) failures.push(`${pg} LCP ${b.lcpMs}ms → ${a.lcpMs}ms`);
  }
}

// 3) SEO 가드: URL·sitemap·canonical·robots·title·description·JSON-LD 변화 금지
const sb = path.join(beforeDir, 'seo.json');
const sa = path.join(afterDir, 'seo.json');
if (fs.existsSync(sb) && fs.existsSync(sa)) {
  const b = JSON.parse(fs.readFileSync(sb));
  const a = JSON.parse(fs.readFileSync(sa));
  const removed = Object.keys(b.pages).filter((p) => !a.pages[p]);
  const added = Object.keys(a.pages).filter((p) => !b.pages[p]);
  const changed = Object.keys(b.pages).filter((p) => a.pages[p] && JSON.stringify(a.pages[p]) !== JSON.stringify(b.pages[p]));
  const sitemapSame = JSON.stringify(a.sitemap) === JSON.stringify(b.sitemap);
  lines.push('', '### SEO 가드', '', `- HTML 페이지 ${b.pageCount} → ${a.pageCount} (추가 ${added.length}, 삭제 ${removed.length})`,
    `- sitemap URL ${b.sitemap.length} → ${a.sitemap.length} (${sitemapSame ? '동일' : '변경됨'})`,
    `- canonical·robots·title·description·JSON-LD 가 바뀐 페이지: ${changed.length}`);
  if (removed.length || added.length || changed.length || !sitemapSame) {
    failures.push(`SEO 메타/URL 변경: 추가 ${added.slice(0, 5)}, 삭제 ${removed.slice(0, 5)}, 변경 ${changed.slice(0, 5)}`);
  }
}

// 4) 스크린샷 픽셀 차이
const diffDir = path.join(afterDir, 'diff');
fs.mkdirSync(diffDir, { recursive: true });
lines.push('', '### 화면 변화 (픽셀 차이 비율)', '', '| 스크린샷 | 변화 |', '|---|---|');
for (const r of after.results) {
  const bp = path.join(beforeDir, 'shots', r.shot);
  if (!fs.existsSync(bp)) continue;
  const A = PNG.sync.read(fs.readFileSync(bp));
  const B = PNG.sync.read(fs.readFileSync(path.join(afterDir, 'shots', r.shot)));
  const w = Math.max(A.width, B.width);
  const h = Math.max(A.height, B.height);
  const pad = (img) => {
    const out = new PNG({ width: w, height: h });
    PNG.bitblt(img, out, 0, 0, img.width, img.height, 0, 0);
    return out;
  };
  const diff = new PNG({ width: w, height: h });
  const n = pixelmatch(pad(A).data, pad(B).data, diff.data, w, h, { threshold: 0.1 });
  const pct = ((n / (w * h)) * 100).toFixed(1);
  if (n > 0) {
    fs.writeFileSync(path.join(diffDir, r.shot), PNG.sync.write(diff));
    lines.push(`| ${r.shot} | ${pct}% |`);
  }
}

lines.push('', failures.length ? `### 가드 실패\n\n${failures.map((f) => `- ${f}`).join('\n')}` : '### 가드: 통과');
fs.writeFileSync(path.join(afterDir, 'summary.md'), lines.join('\n') + '\n');
console.log(lines.join('\n'));
process.exit(failures.length ? 1 : 0);
