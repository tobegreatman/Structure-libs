"""把流水线产出汇总成报告所需的数值清单（不含文字）。

    python -m pipeline.brief --as-of 2026-10-30
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="2026-10-30")
    args = ap.parse_args()
    c = ROOT / "pipeline" / "cache"

    fac = json.loads((c / f"factors-{args.as_of}.json").read_text(encoding="utf-8"))
    run = json.loads((c / f"run-{args.as_of}.json").read_text(encoding="utf-8"))
    lim = json.loads((c / f"limit-{args.as_of}.json").read_text(encoding="utf-8"))
    sc = json.loads((c / f"score-{args.as_of}.json").read_text(encoding="utf-8"))
    rows = fac["rows"]

    print("=" * 70)
    print("1 份额变化榜（128 板块）")
    print("=" * 70)
    flow = [(v["name"], v["share_chg_5"] or 0, v["chg_20d"] or 0, v["share_pct_250"] or 0)
            for v in rows.values()]
    flow.sort(key=lambda x: x[1], reverse=True)
    print("  流入 top8")
    for n, d, c20, p in flow[:8]:
        print(f"    {n:<12} Δ5 {d * 100:+6.2f}pct  20日 {c20 * 100:+7.2f}%  分位 {p:.4f}")
    print("  流出 top8")
    for n, d, c20, p in flow[-8:][::-1]:
        print(f"    {n:<12} Δ5 {d * 100:+6.2f}pct  20日 {c20 * 100:+7.2f}%  分位 {p:.4f}")

    print("\n" + "=" * 70)
    print("2 20 日涨幅榜（128 板块）")
    print("=" * 70)
    for v in sorted(rows.values(), key=lambda x: x["chg_20d"] or -9, reverse=True)[:10]:
        print(f"    {v['name']:<12} 20日 {v['chg_20d'] * 100:+7.2f}%  rs {v['rs_20']:.4f}  "
              f"60日 {(v['chg_60d'] or 0) * 100:+7.2f}%")

    print("\n" + "=" * 70)
    print("3 市场情绪")
    print("=" * 70)
    m = lim["market"]
    print(f"    涨停 {m['limit_up']} / 炸板 {m['broken']} / 跌停 {m['limit_down']} / 封板率 {m['seal_rate']}")
    print(f"    最高连板 {m['max_streak']} 级 / 前一交易日涨停 {m['prev_limit_up']} 家")
    print(f"    全市场成交额 {run['market_amount_yi']} 亿 / 板块池 {run['boards_live']} 个 / 窗口 {run['span']} 日")
    print("    连板梯队：" + "、".join(
        f"{p['name']}({p['streak']}板)" for p in sorted(lim["limit_up"], key=lambda x: -(x["streak"] or 1))[:8]))

    print("\n" + "=" * 70)
    print("4 闸门矩阵（8 个候选）")
    print("=" * 70)
    for g in run["gates"]:
        print(f"    {g['name']:<12} P1 {'✓' if g['p1'] else '·'} {g['days_top10_20']:>2}/{g['consecutive_top10']:<2} "
              f"P2 {'✓' if g['p2'] else '·'} {(g['breadth_up_ratio'] or 0) * 100:>5.1f}/{(g['new_high_ratio'] or 0) * 100:>4.1f} "
              f"P3 {'✓' if g['p3'] else '·'} {(g['turnover_share_chg_5'] or 0) * 100:>+6.2f}/{g['turnover_share_pct_250']} "
              f"→ {g['gates']} 条")

    print("\n" + "=" * 70)
    print("5 板块个股明细（白酒Ⅱ）")
    print("=" * 70)
    sn = json.loads((c / "quote-snapshot.json").read_text(encoding="utf-8")) if (c / "quote-snapshot.json").exists() else {}
    if sn:
        print(f"    快照 {len(sn)} 只")
        up = sum(1 for v in sn.values() if v["price"] > (v.get("prev_close") or 0))
        print(f"    上涨 {up} / 下跌 {len(sn) - up}")

    print("\n" + "=" * 70)
    print("6 打分汇总")
    print("=" * 70)
    for r in sc["rows"]:
        flag = "进榜" if r["listed"] else "萌芽"
        print(f"    {r['name']:<12} {r['score']:>6.2f}  pen {r['penalty']:>2}  stage {str(r['stage']):<12} "
              f"闸门 {r['gates']}  [{flag}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
