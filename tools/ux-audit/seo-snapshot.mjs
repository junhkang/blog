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
      const pick = (re) => (html.match(re) || [])[1] || null;
      const ld = [...html.matchAll(/<script type="?application\/ld\+json"?>([\s\S]*?)<\/script>/g)].map((m) => m[1].trim());
      pages[rel] = {
        canonical: pick(/<link rel="?canonical"? href="?([^" >]+)/),
        robots: pick(/<meta name="?robots"? content="?([^">]+)/),
        title: pick(/<title>([^<]*)<\/title>/),
        description: pick(/<meta name="?description"? content="([^"]*)"/),
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
