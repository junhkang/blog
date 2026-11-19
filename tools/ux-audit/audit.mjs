// Usage: node audit.mjs --base http://localhost:1314 --out out/before [--lighthouse]
import { chromium } from 'playwright';
import AxeBuilder from '@axe-core/playwright';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => {
    if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1] && !arr[i + 1].startsWith('--') ? arr[i + 1] : true]);
    return acc;
  }, []),
);
const base = (args.base || 'http://localhost:1314').replace(/\/$/, '');
const outDir = path.resolve(args.out || 'out/current');
const cfg = JSON.parse(fs.readFileSync(new URL('./pages.json', import.meta.url)));
fs.mkdirSync(path.join(outDir, 'shots'), { recursive: true });

// 페이지 안에서 측정하는 레이아웃·가독성 지표
const measure = () => {
  const vw = document.documentElement.clientWidth; // 모바일 에뮬레이션에선 innerWidth 가 넘친 폭만큼 커진다
  const doc = document.documentElement;
  const menuItems = [...document.querySelectorAll('#menu > li')];
  const hiddenMenu = menuItems.filter((li) => {
    const r = li.getBoundingClientRect();
    return r.right > vw + 1 || r.left < -1;
  }).map((li) => li.textContent.trim());
  const overflowing = [...document.querySelectorAll('body *')].filter((el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.right <= vw + 1) return false;
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return false;
    }
    return true;
  }).slice(0, 5).map((el) => el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : ''));
  const smallTargets = [...document.querySelectorAll('a[href], button')].filter((el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || s.visibility === 'hidden') return false;
    if (el.closest('.post-content, .entry-content, .toc')) return false; // 본문 인라인 링크 제외
    return r.width < 24 || r.height < 24;
  }).length;
  const p = document.querySelector('.post-content p');
  let body = null;
  if (p) {
    const s = getComputedStyle(p);
    const fs = parseFloat(s.fontSize);
    const lh = s.lineHeight === 'normal' ? fs * 1.2 : parseFloat(s.lineHeight);
    body = {
      fontSize: fs,
      lineHeightRatio: +(lh / fs).toFixed(2),
      measureChars: Math.round(p.getBoundingClientRect().width / fs),
      wordBreak: s.wordBreak,
      fontFamily: s.fontFamily.split(',')[0].trim(),
    };
  }
  return {
    status: document.title ? 'ok' : 'empty',
    horizontalOverflow: doc.scrollWidth > vw + 1,
    scrollWidth: doc.scrollWidth,
    hiddenMenuItems: hiddenMenu,
    overflowingElements: overflowing,
    smallTapTargets: smallTargets,
    body,
    canonical: document.querySelector('link[rel=canonical]')?.href || null,
    robots: document.querySelector('meta[name=robots]')?.content || null,
    ldJsonCount: document.querySelectorAll('script[type="application/ld+json"]').length,
    pageHeight: doc.scrollHeight,
  };
};

const browser = await chromium.launch();
const results = [];
for (const theme of cfg.themes) {
  for (const vp of cfg.viewports) {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      deviceScaleFactor: 1,
      colorScheme: theme,
      isMobile: vp.width < 768,
      hasTouch: vp.width < 768,
    });
    await ctx.addInitScript((t) => localStorage.setItem('pref-theme', t), theme);
    // 광고·분석·댓글 iframe 은 매번 바뀌어 스크린샷 비교를 흔들기 때문에 막는다.
    await ctx.route('**/*', (route) => {
      const host = new URL(route.request().url()).hostname;
      return cfg.blockHosts.includes(host) ? route.abort() : route.continue();
    });
    const page = await ctx.newPage();
    for (const pg of cfg.pages) {
      const url = base + pg.path;
      const resp = await page.goto(url, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      const m = await page.evaluate(measure);
      m.httpStatus = resp.status();
      const shot = `${pg.name}--${vp.name}--${theme}.png`;
      await page.screenshot({ path: path.join(outDir, 'shots', shot), fullPage: true, animations: 'disabled' });
      let axe = null;
      if (theme === 'light' && vp.name !== 'tablet') {
        const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
        axe = r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length }));
      }
      results.push({ page: pg.name, path: pg.path, viewport: vp.name, theme, shot, ...m, axe });
      process.stdout.write(`. ${pg.name} ${vp.name} ${theme}\n`);
    }
    await ctx.close();
  }
}
await browser.close();

const lighthouse = {};
if (args.lighthouse) {
  const chromePath = chromium.executablePath();
  for (const name of cfg.lighthousePages) {
    const pg = cfg.pages.find((p) => p.name === name);
    const file = path.join(outDir, `lh-${name}.json`);
    const runs = [];
    // 광고·분석까지 실제처럼 불러온다. VM 성능 편차를 줄이려고 3회 측정 후 중앙값을 쓴다.
    for (let i = 0; i < 3; i++) {
      execFileSync('npx', ['lighthouse', base + pg.path, '--quiet', '--output=json', `--output-path=${file}`,
        '--only-categories=performance,accessibility,best-practices,seo',
        `--chrome-flags=--headless=new --no-sandbox`], { stdio: 'ignore', env: { ...process.env, CHROME_PATH: chromePath } });
      const lh = JSON.parse(fs.readFileSync(file));
      runs.push({
        performance: Math.round(lh.categories.performance.score * 100),
        accessibility: Math.round(lh.categories.accessibility.score * 100),
        bestPractices: Math.round(lh.categories['best-practices'].score * 100),
        seo: Math.round(lh.categories.seo.score * 100),
        lcpMs: Math.round(lh.audits['largest-contentful-paint'].numericValue),
        cls: +lh.audits['cumulative-layout-shift'].numericValue.toFixed(3),
        tbtMs: Math.round(lh.audits['total-blocking-time'].numericValue),
      });
    }
    const median = (k) => runs.map((r) => r[k]).sort((a, b) => a - b)[1];
    lighthouse[name] = Object.fromEntries(Object.keys(runs[0]).map((k) => [k, median(k)]));
    process.stdout.write(`lh ${name} ${JSON.stringify(lighthouse[name])}\n`);
  }
}

fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify({ base, date: new Date().toISOString(), results, lighthouse }, null, 2));
console.log(`report → ${path.join(outDir, 'report.json')}`);
