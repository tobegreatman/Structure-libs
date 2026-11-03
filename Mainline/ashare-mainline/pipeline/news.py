"""东财资讯检索：按关键词取最近 N 天新闻，输出 日期/媒体/标题/摘要。"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

BASE = "https://search-api-web.eastmoney.com/search/jsonp"


def fetch(keyword: str, size: int = 20) -> list[dict]:
    param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": size,
            "preTag": "", "postTag": ""}},
    }
    url = f"{BASE}?cb=cb&param={urllib.parse.quote(json.dumps(param, ensure_ascii=False))}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://so.eastmoney.com/"})
    body = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
    body = body[body.index("(") + 1:body.rindex(")")]
    j = json.loads(body)
    return ((j.get("result") or {}).get("cmsArticleWebOld")) or []


def main() -> None:
    since = sys.argv[1]
    for kw in sys.argv[2:]:
        print("=" * 78)
        print(f"关键词：{kw}")
        try:
            rows = fetch(kw, 30)
        except Exception as e:  # noqa: BLE001
            print("  ERR", type(e).__name__, e)
            continue
        n = 0
        for r in rows:
            d = (r.get("date") or "")[:10]
            if d < since:
                continue
            n += 1
            txt = (r.get("content") or "").replace("\n", " ")
            print(f"  [{d}] {r.get('mediaName')} | {r.get('title')}")
            print(f"          {txt[:150]}")
            if n >= 12:
                break
        if n == 0:
            print("  （窗口内无）")


if __name__ == "__main__":
    main()
