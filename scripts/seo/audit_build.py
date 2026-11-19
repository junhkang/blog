#!/usr/bin/env python3
"""hugo 빌드 결과(public/) SEO 점검. 문제가 있으면 종료 코드 1.

  hugo --minify --baseURL "https://junhkang.com/" && python3 scripts/seo/audit_build.py public
"""
import collections
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import unquote

BASE = "https://junhkang.com"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.robots = self.desc = self.canonical = None
        self.title, self._in_title, self.h1, self.imgs_noalt, self.links = "", False, 0, 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "robots":
            self.robots = a.get("content", "")
        elif tag == "meta" and a.get("name") == "description":
            self.desc = a.get("content")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href")
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "img" and not a.get("alt"):
            self.imgs_noalt += 1
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def main(root):
    pages = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".html"):
                p = os.path.join(d, f)
                url = "/" + os.path.relpath(p, root).replace("\\", "/")
                url = url[: -len("index.html")] if url.endswith("index.html") else url
                pg = Page()
                pg.feed(open(p, encoding="utf-8", errors="ignore").read())
                pages[url] = pg
    problems = collections.defaultdict(list)
    titles, descs = collections.defaultdict(list), collections.defaultdict(list)
    noindex = {u for u, p in pages.items() if p.robots and "noindex" in p.robots}
    sm = os.path.join(root, "sitemap.xml")
    locs = [unquote(l) for l in re.findall(r"<loc>https://junhkang\.com(.*?)</loc>", open(sm).read())] \
        if os.path.exists(sm) else []
    if not locs:
        problems["sitemap_empty"].append(sm)
    for l in locs:
        if l in noindex:
            problems["noindex_in_sitemap"].append(l)
        elif l not in pages:
            problems["sitemap_url_missing"].append(l)
    for url, p in pages.items():
        for h in p.links:
            h = h[len(BASE):] if h.startswith(BASE) else h
            if h.startswith("/") and not h.startswith("//"):
                q = unquote(h.split("#")[0].split("?")[0]).lstrip("/")
                if not (os.path.exists(os.path.join(root, q)) or os.path.exists(os.path.join(root, q, "index.html"))):
                    problems["broken_internal_link"].append(f"{url} -> {h}")
        # 품질 점검은 사이트맵에 올린(색인 대상) 페이지만 본다.
        if url not in locs or url in noindex:
            continue
        titles[p.title.strip()].append(url)
        descs[p.desc].append(url)
        if not p.desc:
            problems["missing_description"].append(url)
        if p.canonical != BASE + url.replace(" ", "%20") and unquote(p.canonical or "") != BASE + url:
            problems["canonical_not_self"].append(f"{url} -> {p.canonical}")
        if p.h1 != 1:
            problems[f"h1_count_{p.h1}"].append(url)
        if p.imgs_noalt:
            problems["img_without_alt"].append(f"{url} ({p.imgs_noalt})")
    for t, us in titles.items():
        if len(us) > 1:
            problems["duplicate_title"].append(f"{t!r}: {us[:5]}")
    for t, us in descs.items():
        if t and len(us) > 1:
            problems["duplicate_description"].append(f"{t[:40]!r} x{len(us)}: {us[:5]}")
    report = {"pages": len(pages), "indexable": len(pages) - len(noindex), "sitemap_urls": len(locs),
              "problems": {k: v for k, v in problems.items()}}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "public"))
