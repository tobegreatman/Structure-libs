"""涨停/炸板/跌停池 + 按板块成分股口径统计。

    python -m pipeline.limitpool --as-of 2026-10-30 [--prev 2026-10-23]

push2ex 的池子接口没有出现在 push2his 的封禁范围内，实测可用。
输出 cache/limit-<as_of>.json：
  market  : 全市场涨停/炸板/跌停家数
  boards  : 每个候选板块的涨停家数、最高连板、开板家数、封板率
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day, http  # noqa: E402

POOL = ("https://push2ex.eastmoney.com/getTopic{kind}Pool"
        "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
        "&Pageindex=0&pagesize=300&sort={sort}&date={date}")
# 涨停/炸板池按首封时间排序；跌停池没有 fbt 字段，用 fbt 排序会返回 82 条 total 但 pool 为空，
# 必须换 fund:asc（实测 2026-10-30）。
SORT = {"ZT": "fbt%3Aasc", "ZB": "fbt%3Aasc", "DT": "fund%3Aasc"}

def fetch_pool(cli: http.Client, kind: str, date: str) -> list[dict]:
    url = POOL.format(kind=kind, sort=SORT[kind], date=date.replace("-", ""))
    raw = cli.get(url, retries=2)
    j = json.loads(raw)
    return (((j or {}).get("data") or {}).get("pool")) or []


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--prev", default=None,
                    help="连板高度基准日；缺省 = as_of 前 5 个交易日")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)
    args.prev = args.prev or day.prev_n(args.as_of, 5)

    cli = http.make_client(workers=2, max_rps=2.5, min_span=0)
    zt = fetch_pool(cli, "ZT", args.as_of)
    zb = fetch_pool(cli, "ZB", args.as_of)
    dt = fetch_pool(cli, "DT", args.as_of)
    prev = fetch_pool(cli, "ZT", args.prev) if args.prev else []

    def brief(p: dict) -> dict:
        return {
            "code": f"{p.get('c')}",
            "name": p.get("n"),
            "streak": p.get("lbc"),          # 连板数
            "open_times": p.get("zbc"),      # 开板次数
            "first_time": p.get("fbt"),
            "amount": p.get("amount"),
        }

    market = {
        "limit_up": len(zt),
        "broken": len(zb),
        "limit_down": len(dt),
        # 封板率 = 涨停 / (涨停 + 炸板)。炸板池是「曾涨停但收盘未封住」，
        # 所以分母是两者之和，不是涨停家数（否则会算出 0.34 这种偏低的假象）。
        "seal_rate": round(len(zt) / (len(zt) + len(zb)), 4) if zt else None,
        "max_streak": max((p.get("lbc") or 1) for p in zt) if zt else 0,
        "prev_limit_up": len(prev) if args.prev else None,
    }
    print(f"  {args.as_of} 涨停 {market['limit_up']} / 炸板 {market['broken']} / 跌停 {market['limit_down']}"
          f" / 封板率 {market['seal_rate']} / 最高连板 {market['max_streak']}")
    if args.prev:
        print(f"  {args.prev} 涨停 {market['prev_limit_up']} 家")

    out = {
        "as_of": args.as_of, "prev": args.prev, "market": market,
        "limit_up": [brief(p) for p in zt],
        "broken": [brief(p) for p in zb],
        # 上一交易日的涨停池要留下来：divergence 的「连板回落 ≥2 级」判据是
        # prev_streak − cur_streak ≥ 2（板块自身的前后对比），不是和全市场最高连板比。
        "prev_limit_up": [brief(p) for p in prev],
    }
    dest = ROOT / "pipeline" / "cache" / f"limit-{args.as_of}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
