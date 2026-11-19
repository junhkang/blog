#!/usr/bin/env python3
"""네이버 서치어드바이저 일일 점검.

이미 로그인된 Chrome(CDP)에 붙어서 콘솔이 쓰는 내부 API(/api-console/*)를 호출하고,
문제 목록을 JSON·Markdown 으로 남긴다.

  python3 scripts/seo/naver_report.py --out /tmp/seo            # 점검
  python3 scripts/seo/naver_report.py --request-crawl URL [URL…] # 웹 페이지 수집 요청

종료 코드: 0 정상(문제 유무와 무관), 3 로그인 만료, 4 수집 실패.
"""
import argparse
import asyncio
import collections
import datetime as dt
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SITE = "https://junhkang.com"
CONSOLE = "https://searchadvisor.naver.com/console/site/summary?site=" + SITE.replace(":", "%3A").replace("/", "%2F")
CDP = os.environ.get("CHROME_CDP", "http://localhost:29229")
KST = dt.timezone(dt.timedelta(hours=9))
NAMES = json.loads((Path(__file__).with_name("naver_diagnosis_names.json")).read_text())

INDEXED = 1000
# 의도된 상태: 태그 목록은 noindex 로 운영한다.
EXPECTED = {3003: lambda url: "/tags/" in url}
CRAWL_ERRORS = ["dnsError", "connectTimeout", "robotDenied", "serverError", "malformedUrl",
                "accessDenied", "notFound", "invalidContent", "unknownError"]


def live_meta(url):
    """라이브 페이지의 robots·description. 네이버 진단은 며칠 늦으므로 현재 상태로 다시 확인한다."""
    import re
    import urllib.request
    try:
        html = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "seo-monitor"}),
                                      timeout=20).read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}
    get = lambda name: (re.search(rf'<meta name="?{name}"? content="([^"]*)"', html) or [None, None])[1]
    return {"noindex": "noindex" in (get("robots") or ""), "description": get("description")}


def recheck(did, urls):
    """아직 유효한 URL만 남긴다."""
    live = {u: live_meta(u) for u in urls}
    if did == 3003:
        return [u for u in urls if "error" in live[u] or live[u]["noindex"]], live
    keep = [u for u in urls if "error" in live[u] or not live[u]["noindex"]]
    if did in (4006, 4001):
        descs = collections.Counter(live[u].get("description") for u in keep)
        keep = [u for u in keep if "error" in live[u] or not live[u].get("description")
                or (did == 4006 and descs[live[u]["description"]] > 1)]
    return keep, live


class AuthExpired(Exception):
    pass


async def open_console(p):
    browser = await p.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await ctx.new_page()
    await page.goto(CONSOLE, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(1500)
    state = await page.evaluate(
        "()=>{const s=window.$nuxt&&window.$nuxt.$store&&window.$nuxt.$store.state;"
        "return s&&s.authUser&&s.authUser.enc_id?{enc:s.authUser.enc_id,csrf:s.csrfToken,"
        "items:s.diagnosisItems||null}:null}")
    if "nid.naver.com" in page.url or not state:
        await page.close()
        raise AuthExpired(page.url)
    return page, state


async def api(page, path, **params):
    q = "&".join(f"{k}={v}" for k, v in {"site": SITE, **params}.items())
    res = await page.evaluate(
        "async u=>{const r=await fetch(u,{credentials:'include'});return {s:r.status,t:await r.text()}}",
        f"/api-console/{path}?{q}")
    if res["s"] in (401, 403):
        raise AuthExpired(path)
    try:
        return json.loads(res["t"])
    except ValueError:
        raise RuntimeError(f"{path}: HTTP {res['s']} {res['t'][:200]}")


def items(resp):
    if isinstance(resp, dict):
        return resp.get("items") or []
    return resp or []


async def collect(page, enc):
    today = dt.datetime.now(KST).date()
    d = lambda days: (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    raw = {
        "summary": await api(page, f"report/seo/site/summary/{enc}"),
        "crawl": await api(page, f"report/crawl/{enc}", start_date=d(30), end_date=d(0), isAlly="false", count=5),
        "diagnosis_meta": await api(page, f"report/diagnosis/meta/{enc}", startDate=d(30), endDate=d(0)),
        "expose7": await api(page, f"report/expose/{enc}", period=7, device="d", topN=30),
        "expose30": await api(page, f"report/expose/{enc}", period=30, device="d", topN=50),
        "sitemap": await api(page, f"request/sitemap/{enc}"),
        "robots": await api(page, f"check/robots/{enc}"),
        "crawl_requests": await api(page, f"request/crawl/{enc}"),
        "backlink": await api(page, f"report/backlink/{enc}", start_date=d(30), end_date=d(0)),
    }
    meta = items(raw["diagnosis_meta"])
    base = None
    latest = (raw["diagnosis_meta"].get("meta") or {}).get("latestDate")
    if latest:
        base = dt.datetime.fromtimestamp(latest / 1000, KST).strftime("%Y%m%d")
    raw["diagnosis"] = {}
    type_count = (meta[0].get("typeCount") or {}).get("0", {}) if meta else {}
    for did in type_count:
        if int(did) == INDEXED:
            continue
        raw["diagnosis"][did] = await api(page, f"report/diagnosis/info/{enc}", diagnosisId=did,
                                          splitNum=0, baseDate=base or d(1), urlPeriod=90)
    return raw


def analyse(raw):
    findings = []  # {severity: high|medium|info, code, title, detail, urls}
    add = lambda sev, code, title, detail="", urls=None: findings.append(
        {"severity": sev, "code": code, "title": title, "detail": detail, "urls": urls or []})

    info = (items(raw["summary"]) or [{}])[0].get("info", {})
    if info.get("robots", {}).get("robotsBlock"):
        add("high", "robots_block", "robots.txt 가 네이버 수집을 막음", json.dumps(info["robots"]))
    cert = info.get("certificate", {})
    if cert.get("certError") or cert.get("hasCert") is False:
        add("high", "certificate", "보안 인증서 오류", json.dumps(cert))
    mp = info.get("multiProtocol", {})
    if mp and (mp.get("http") != "HTTP_TO_HTTPS" or mp.get("httpsStatus") != 200):
        add("high", "https_redirect", "HTTP→HTTPS 리다이렉션 이상", json.dumps(mp))
    sm = info.get("sitemap", {})
    if sm.get("result") != "REGISTERED_SITEMAP" or sm.get("statusCode") != 200:
        add("high", "sitemap", "사이트맵 상태 이상", json.dumps(sm))
    if not raw["sitemap"]:
        add("high", "sitemap_missing", "사이트맵이 제출돼 있지 않음")

    stats = (items(raw["crawl"]) or [{}])[0].get("stats", [])
    recent = stats[-7:]
    errs = {k: sum(s.get(k, 0) for s in recent) for k in CRAWL_ERRORS}
    if any(errs.values()):
        add("high", "crawl_errors", "최근 7일 수집 오류", json.dumps({k: v for k, v in errs.items() if v}))
    if stats and len(stats) >= 14 and sum(s["pageCount"] for s in stats[-14:]) == 0:
        add("medium", "crawl_stalled", "최근 14일 동안 네이버 로봇 수집 0건")

    meta = items(raw["diagnosis_meta"])
    series = meta[0].get("meta", []) if meta else []
    if len(series) >= 2:
        first, last = series[0]["stateCount"].get("1", 0), series[-1]["stateCount"].get("1", 0)
        if first and last < first * 0.9:
            add("high", "index_drop", "색인 페이지 수 감소",
                f"{series[0]['date']} {first} → {series[-1]['date']} {last}")
    for did, resp in raw["diagnosis"].items():
        did_i = int(did)
        info_ = (items(resp) or [{}])[0].get("info", {})
        urls = [u["url"] for u in info_.get("urls", [])]
        groups = [{"content": c.get("content"), "urls": [u["url"] for u in c.get("urls", [])]}
                  for c in info_.get("contents", [])]
        all_urls = urls + [u for g in groups for u in g["urls"]]
        ok = EXPECTED.get(did_i)
        if ok:
            all_urls = [u for u in all_urls if not ok(u)]
            if not all_urls:
                continue
        if did_i in (3003, 4001, 4003, 4006):
            all_urls, _ = recheck(did_i, all_urls)
            if not all_urls:
                continue
            groups = []
        name = NAMES.get(str(did_i), f"진단 {did_i}")
        sev = "high" if did_i < 3000 else "medium"
        detail = ""
        if groups:
            import base64
            detail = "\n".join(
                f"- 중복 문구 {len(g['urls'])}개: "
                f"{base64.b64decode(g['content']).decode('utf-8', 'replace')[:80] if g['content'] else ''}"
                for g in groups)
        add(sev, f"diagnosis_{did_i}", name, detail, all_urls)

    e7 = (items(raw["expose7"]) or [{}])[0].get("period", {})
    if e7 and not e7.get("emptyRatio") and e7.get("prevExposeRatio", 0) <= -30:
        add("medium", "expose_drop", "주간 노출수 30% 이상 감소",
            f"노출 {e7.get('exposeCount')} (전주 대비 {e7.get('prevExposeRatio')}%), 클릭 {e7.get('clickCount')}")
    e30 = (items(raw["expose30"]) or [{}])[0]
    low_ctr = [u for u in e30.get("urls", []) if u.get("exposeCount", 0) >= 30 and u.get("clickCount", 0) == 0]
    if low_ctr:
        add("info", "low_ctr", "30일 노출 30회 이상인데 클릭 0인 글 (제목·설명 개선 후보)",
            ", ".join(f"{u['key']}({u['exposeCount']})" for u in low_ctr), [u["key"] for u in low_ctr])

    summary = {
        "indexed": series[-1]["stateCount"] if series else None,
        "diagnosis_date": series[-1]["date"] if series else None,
        "expose7": e7,
        "crawl_7d_pages": sum(s.get("pageCount", 0) for s in recent),
        "crawl_requests_today": (raw["crawl_requests"].get("meta") or {}).get("count")
        if isinstance(raw["crawl_requests"], dict) else None,
    }
    return summary, findings


def markdown(summary, findings):
    out = [f"# 네이버 서치어드바이저 점검 {dt.datetime.now(KST):%Y-%m-%d %H:%M} KST", ""]
    e = summary["expose7"] or {}
    out.append(f"- 진단 기준일 {summary['diagnosis_date']}: 상태별 페이지 수 {summary['indexed']} "
               "(1 색인, 2 수집제한, 3 색인제외, 4 SEO)")
    out.append(f"- 최근 7일: 노출 {e.get('exposeCount')} ({e.get('prevExposeRatio')}%), "
               f"클릭 {e.get('clickCount')}, CTR {e.get('ctr')}%, 로봇 수집 {summary['crawl_7d_pages']}건")
    out.append("")
    if not findings:
        out.append("문제 없음.")
    for f in findings:
        out.append(f"## [{f['severity']}] {f['title']} (`{f['code']}`)")
        if f["detail"]:
            out.append(f["detail"])
        for u in f["urls"][:30]:
            out.append(f"- {u}")
        out.append("")
    return "\n".join(out)


async def request_crawl(page, state, urls):
    results = []
    for url in urls:
        doc = url.split(SITE, 1)[-1].lstrip("/")
        res = await page.evaluate(
            "async b=>{const r=await fetch('/api-console/request/crawl',{method:'POST',credentials:'include',"
            "headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return {s:r.status,t:await r.text()}}",
            {"user_enc_id": state["enc"], "site": SITE, "document": doc, "_csrf": state["csrf"]})
        results.append({"url": url, "status": res["s"], "body": res["t"][:300]})
    return results


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/seo")
    ap.add_argument("--request-crawl", nargs="+", metavar="URL")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        try:
            page, state = await open_console(p)
        except AuthExpired as e:
            print(f"AUTH_EXPIRED: 네이버 로그인이 풀렸음 ({e})", file=sys.stderr)
            return 3
        try:
            if args.request_crawl:
                res = await request_crawl(page, state, args.request_crawl)
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0 if all(r["status"] == 200 and '"code":0' in r["body"] for r in res) else 4
            raw = await collect(page, state["enc"])
        except AuthExpired as e:
            print(f"AUTH_EXPIRED: {e}", file=sys.stderr)
            return 3
        except Exception as e:  # noqa: BLE001
            print(f"COLLECT_FAILED: {e!r}", file=sys.stderr)
            return 4
        finally:
            await page.close()
    summary, findings = analyse(raw)
    (out / "naver_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1))
    (out / "naver_findings.json").write_text(
        json.dumps({"summary": summary, "findings": findings}, ensure_ascii=False, indent=1))
    md = markdown(summary, findings)
    (out / "naver_report.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
