// Usage: node seo-snapshot.mjs <built public dir> <out.json>
// URL 목록·sitemap·canonical·robots·JSON-LD 를 뽑아 개선 전후 빌드를 비교할 수 있게 한다.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const [dir, out] = process.argv.slice(2);
const pages = {};
const walk = (d) => {
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith('.html')) {
      const html = fs.readFileSync(p, 'utf8');
      const rel = '/' + path.relative(dir, p).replace(/index\.html$/, '');
      // minify 는 공백 없는 속성값의 따옴표를 지우고, 따옴표 안에는 '>' 가 올 수 있다. 태그·속성을 따옴표를 고려해 파싱한다.
      const tags = (name) => [...html.matchAll(new RegExp(`<${name}((?:\\s+[^\\s=>/]+(?:=(?:"[^"]*"|'[^']*'|[^\\s"'>]+))?)*)\\s*/?>`, 'g'))]
        .map((m) => Object.fromEntries([...m[1].matchAll(/([^\s=>/]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)]
          .map((x) => [x[1].toLowerCase(), x[2] ?? x[3] ?? x[4] ?? ''])));
      const metas = tags('meta');
      const meta = (n) => metas.find((t) => t.name === n)?.content ?? null;
      const ld = [...html.matchAll(/<script type="?application\/ld\+json"?>([\s\S]*?)<\/script>/g)].map((m) => m[1].trim());
      pages[rel] = {
        canonical: tags('link').find((t) => t.rel === 'canonical')?.href ?? null,
        robots: meta('robots'),
        title: (html.match(/<title>([^<]*)<\/title>/) || [])[1] || null,
        description: meta('description'),
        ldJsonHash: ld.length ? crypto.createHash('sha1').update(ld.join('\n')).digest('hex').slice(0, 12) : null,
      };
    }
  }
};
walk(dir);
const sitemapFile = path.join(dir, 'sitemap.xml');
const sitemap = fs.existsSync(sitemapFile)
  ? [...fs.readFileSync(sitemapFile, 'utf8').matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]).sort()
  : [];
fs.writeFileSync(out, JSON.stringify({ pageCount: Object.keys(pages).length, sitemap, pages }, null, 1));
console.log(`seo snapshot: ${Object.keys(pages).length} pages, ${sitemap.length} sitemap urls → ${out}`);
