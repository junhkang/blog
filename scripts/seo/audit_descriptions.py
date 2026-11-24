#!/usr/bin/env python3
"""Audit rendered descriptions: python3 scripts/seo/audit_descriptions.py public --strict.

Use 50–160 Unicode characters as an editorial target, not a search engine limit.
Korean can express more in fewer characters; flag very short copy without
requiring it to meet a typical English snippet length. Search display also
depends on pixel width, device, and query.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path


FIELDS = ("description", "og:description", "twitter:description")
MIN_LENGTH, MAX_LENGTH = 50, 160
MARKUP = re.compile(r"<(?:/?[a-z][a-z0-9]*)(?:\s+[^<>]*)?>|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|```")


class Head(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.in_head = False
        self.redirect = False
        self.noindex = False
        self.meta = defaultdict(list)
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        elif tag == "body":
            self.in_head = False
        elif self.in_head and tag == "meta":
            attrs = dict(attrs)
            name = (attrs.get("name") or "").lower()
            prop = (attrs.get("property") or "").lower()
            value = attrs.get("content") or ""
            if (attrs.get("http-equiv") or "").lower() == "refresh":
                self.redirect |= bool(re.match(r"\s*\d+\s*;\s*url\s*=", value, re.I))
            if name == "robots":
                self.noindex |= bool({"noindex", "none"} & set(re.split(r"[,\s]+", value.lower())))
            field = prop or name
            self.meta[field].append(value)

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def normalize(value):
    return " ".join(value.split()).casefold()


def page_type(url):
    parts = url.strip("/").split("/")
    if parts == [""]:
        return "home"
    if len(parts) >= 2 and parts[-2] == "page" and parts[-1].isdigit():
        return page_type("/" + "/".join(parts[:-2])) + " pagination"
    if parts[0] in {"categories", "series", "tags"}:
        return parts[0] + (" index" if len(parts) == 1 else " term")
    if parts[0] in {"posts", "personal", "sankun"}:
        return parts[0] + " section" if len(parts) == 1 else "post"
    if url in {"/about/", "/archives/", "/search/", "/guestbook/", "/404.html"}:
        return url.strip("/")
    return "other"


def audit(root):
    pages, skipped = {}, defaultdict(list)
    for path in sorted(root.rglob("*.html")):
        relative = path.relative_to(root).as_posix()
        url = "/" + relative.removesuffix("index.html") if path.name == "index.html" else f"/{relative}"
        html = path.read_text(encoding="utf-8")
        head = Head(html)
        if head.redirect:
            skipped["redirects"].append(url)
        elif html.strip() == f"naver-site-verification: {path.name}":
            skipped["verification_files"].append(url)
        else:
            pages[url] = head

    if not pages:
        raise ValueError(f"No rendered HTML pages found in {root}")

    duplicates = Counter(
        (field, normalize(value))
        for head in pages.values()
        for field in FIELDS
        for value in set(map(normalize, head.meta[field])) if value
    )
    home = pages.get("/")
    default = {normalize(value) for field in FIELDS for value in home.meta[field]} if home else set()
    rows, totals = {}, defaultdict(Counter)
    for url, head in pages.items():
        title_only = {
            normalize(candidate)
            for title in head.meta["og:title"]
            for site in ["", *head.meta["og:site_name"]]
            for candidate in [f"{title} - {site}" if site else title]
        }
        flags = defaultdict(list)
        descriptions = {field: head.meta[field] for field in FIELDS}
        for field, values in descriptions.items():
            if not values or any(not value.strip() for value in values):
                flags["missing"].append(field)
            if len(values) > 1:
                flags["repeated_tag"].append(field)
            for value in values:
                value = value.strip()
                if not value:
                    continue
                key = normalize(value)
                if duplicates[field, key] > 1:
                    flags["duplicate"].append(field)
                if len(value) > MAX_LENGTH:
                    flags["too_long"].append(field)
                if len(value) < MIN_LENGTH:
                    flags["too_short"].append(field)
                if (url != "/" and key in default) or key in title_only or MARKUP.search(value):
                    flags["fallback_or_markup"].append(field)
        mismatches = [field for field in FIELDS[1:] if descriptions[field] != descriptions["description"]]
        if mismatches:
            flags["mismatch"] = mismatches
        scope = "noindex" if head.noindex else "indexable"
        kind = page_type(url)
        rows[url] = {"type": kind, "scope": scope, "descriptions": descriptions, "issues": flags}
        for group in ["all", scope, f"{scope}: {kind}"]:
            totals[group].update(["pages", *flags])
    return {"counts": totals, "skipped": skipped, "pages": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path("public"))
    parser.add_argument("--strict", action="store_true", help="fail on any issue, including noindex pages")
    args = parser.parse_args()
    try:
        report = audit(args.root)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.strict and any(row["issues"] for row in report["pages"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
