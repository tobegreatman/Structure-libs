"""六因子原始值 + 128 板块横截面百分位。

    python -m pipeline.factors --as-of 2026-10-30

读 panel-<as_of>.pkl 与 run-<as_of>.json，输出 factors-<as_of>.json。
基准（中证全指）的多周期收益由指数快照 + 面板成交额加权指数交叉校验。
"""
from __future__ import annotations

import argparse
import json
import pickle
import statistics
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day, panel  # noqa: E402

# 指数多周期收益优先读 pipeline/indices.py 真取的结果（cache/indices-<as_of>.json）。
# 下面这份只是接口不可用时的兜底：常量写死后，下一轮无论行情怎么走它都不会变，
# rs 的分母就成了假的——2026-11-01 复盘时发现它正是这么被硬编码了三轮。
FALLBACK_IDX_REF = {
    "000985": {"name": "中证全指", "chg_1d": -0.0233, "chg_5d": -0.0573,
               "chg_20d": -0.1444, "chg_60d": -0.1127},
    "000001": {"name": "上证指数", "chg_1d": -0.0144, "chg_5d": -0.0248,
               "chg_20d": -0.0744, "chg_60d": -0.0541},
    "399006": {"name": "创业板指", "chg_1d": -0.0413, "chg_5d": -0.1008,
               "chg_20d": -0.2661, "chg_60d": -0.2561},
}


def load_idx_ref(as_of: str) -> tuple[dict, bool]:
    """返回 (指数收益表, 是否真取)。真取失败时回落到 FALLBACK_IDX_REF 并返回 False。"""
    try:
        from pipeline import indices  # noqa: PLC0415

        d = indices.load(as_of)
    except Exception:  # noqa: BLE001
        d = None
    if not d or not d.get("indices"):
        return FALLBACK_IDX_REF, False
    out = {}
    for r in d["indices"]:
        key = r["code"].split(".")[0]
        if all(r.get(k) is not None for k in ("chg_1d", "chg_5d", "chg_20d", "chg_60d")):
            out[key] = {"name": r["name"], "chg_1d": r["chg_1d"], "chg_5d": r["chg_5d"],
                        "chg_20d": r["chg_20d"], "chg_60d": r["chg_60d"]}
    return (out, True) if "000985" in out else (FALLBACK_IDX_REF, False)


def compound(series: list[tuple[str, float]], n: int) -> float | None:
    """series 为 (date, pct) 升序；返回最近 n 个交易日的复合涨幅。"""
    if len(series) < n:
        return None
    tail = series[-n:]
    prod = 1.0
    for _, p in tail:
        prod *= 1.0 + p / 100.0
    return prod - 1.0


def pct_rank(values: dict[str, float], target: str) -> float:
    """在横截面里，target 的百分位（0~100，越大越靠前）。

    口径必须与 score.pct_in 完全一致，用 (below + 1) / n：分数里的 normalized 就是按它算的，
    这里若改用统计学常用的 (below + 0.5×equal) / n，同一句话里会出现「87.1×0.6 + 98.8×0.4
    = 92.19」这种自己乘不出来的算式（92.19 实际来自 87.5 与 99.2）。
    展示口径与打分口径只允许有一套。
    """
    if target not in values:
        return None
    v = values[target]
    n = len(values)
    below = sum(1 for x in values.values() if x < v)
    return round((below + 1) / n * 100, 2)


def weighted_index(panel_data: dict, n: int) -> float | None:
    """成交额加权指数（买入持有口径），用来复核基准的量级。

    权重取 **t−n 当日** 成交额，然后一直持有到 t，中途不换手。
    早期版本是每日按前一日成交额再平衡，那会把波动损耗算进去：60 日实测 −0.41%，
    而买入持有 −6.51%、中证全指真值 −11.27%。差值 10.86pct 全是再平衡噪声，
    拿它去判「基准对不对」必然误判。
    """
    dates = sorted(panel_data["by_date"])
    if len(dates) < n + 1:
        return None
    base_d = dates[-n - 1]
    w = {c: v["amount"] for c, v in panel_data["by_date"][base_d].items()
         if v.get("amount") and v.get("pct") is not None}
    if not w:
        return None
    tot = sum(w.values())
    out = 0.0
    for c, wt in w.items():
        r = 1.0
        for d in dates[-n:]:
            row = panel_data["by_date"][d].get(c)
            if row and row.get("pct") is not None:
                r *= 1.0 + row["pct"] / 100.0
        out += wt / tot * (r - 1.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)
    IDX_REF, IDX_LIVE = load_idx_ref(args.as_of)

    p = pickle.loads((ROOT / "pipeline" / "cache" / f"panel-{args.as_of}.pkl").read_bytes())
    run = json.loads((ROOT / "pipeline" / "cache" / f"run-{args.as_of}.json").read_text(encoding="utf-8"))
    boards = {b["code"]: b["name"] for b in p["boards"]}

    # 基准复核
    print("=" * 62)
    print("基准（中证全指）复核")
    print("=" * 62)
    for n, key in ((5, "chg_5d"), (20, "chg_20d"), (60, "chg_60d")):
        est = weighted_index(p, n)
        ref = IDX_REF["000985"].get(key)
        gap = "" if ref is None or est is None else f" | 差 {abs(est - ref) * 100:.2f}pct"
        print(f"  {n:>2} 日：面板成交额加权 {est * 100:6.2f}% vs 腾讯 {ref * 100 if ref is not None else float('nan'):6.2f}%{gap}")

    # 全样本因子
    print("\n" + "=" * 62)
    print("128 板块横截面因子")
    print("=" * 62)
    rows = {}
    for code, name in boards.items():
        hist = p["history"].get(code) or []
        if len(hist) < 61:
            continue
        chg = {n: compound(hist, n) for n in (1, 5, 20, 60)}
        # 60 日阳线占比
        tail60 = hist[-60:]
        up_days = sum(1 for _, x in tail60 if x > 0) / len(tail60)
        m1 = panel.top_days(p, code, n=20, top=10)
        series = panel.share_series(p, code)
        shares = [s for _, s in series]
        cur = shares[-1] if shares else None
        d5 = cur - shares[-6] if len(shares) >= 6 else None
        d20 = cur - shares[-21] if len(shares) >= 21 else None
        pct = sum(1 for s in shares if s <= cur) / len(shares) if shares else None
        # 资金流：5 日净额 / 5 日成交额
        nets, amts = [], []
        for d, m in sorted(p["by_date"].items())[-5:]:
            r = m.get(code)
            if r and r.get("net") is not None and r.get("amount"):
                nets.append(r["net"])
                amts.append(r["amount"])
        flow5 = sum(nets) / sum(amts) if amts else None
        rows[code] = {
            "name": name, "code": code,
            "chg_1d": chg[1], "chg_5d": chg[5], "chg_20d": chg[20], "chg_60d": chg[60],
            "up_days_60": round(up_days, 4),
            "days_top10_20": m1["days_top10_20"], "consecutive_top10": m1["consecutive_top10"],
            "share": cur, "share_chg_5": d5, "share_chg_20": d20, "share_pct_250": round(pct, 4) if pct else None,
            "flow5": round(flow5, 5) if flow5 is not None else None,
        }

    base20 = IDX_REF["000985"]["chg_20d"]
    base5 = IDX_REF["000985"]["chg_5d"]
    base60 = IDX_REF["000985"]["chg_60d"]
    for r in rows.values():
        r["rs_5"] = round((1 + r["chg_5d"]) / (1 + base5), 4) if r["chg_5d"] is not None else None
        r["rs_20"] = round((1 + r["chg_20d"]) / (1 + base20), 4) if r["chg_20d"] is not None else None
        r["rs_60"] = (round((1 + r["chg_60d"]) / (1 + base60), 4)
                      if r["chg_60d"] is not None and base60 is not None else None)

    # 横截面百分位
    def col(k):
        return {c: v[k] for c, v in rows.items() if v.get(k) is not None}

    rs20 = col("rs_20")
    dt = col("days_top10_20")
    cs = col("consecutive_top10")
    ud = col("up_days_60")
    sh = col("share")
    d5c = col("share_chg_5")
    fl = col("flow5")
    g20 = col("chg_20d")

    out = {"as_of": args.as_of, "boards": len(rows), "base": IDX_REF["000985"], "rows": rows,
           "base_source": "indices-live" if IDX_LIVE else "fallback-const",
           "pct": {}, "benchmark_check": {str(n): weighted_index(p, n) for n in (5, 20, 60)}}
    for code in rows:
        out["pct"][code] = {
            "rs_20": pct_rank(rs20, code),
            "days_top10_20": pct_rank(dt, code),
            "consecutive_top10": pct_rank(cs, code),
            "up_days_60": pct_rank(ud, code),
            "share": pct_rank(sh, code),
            "share_chg_5": pct_rank(d5c, code),
            "flow5": pct_rank(fl, code),
            "chg_20d": pct_rank(g20, code),
        }

    # 打印白酒与关键候选
    focus = ["BK1277", "BK0475", "BK0727", "BK0481", "BK0421", "BK1253", "BK1222", "BK1218"]
    print(f"  {'板块':<12}{'chg1d':>8}{'chg5d':>8}{'chg20d':>8}{'chg60d':>8}{'rs20':>8}"
          f"{'P1天':>5}{'连':>4}{'阳%':>7}{'份额%':>8}{'Δ5':>8}{'流5%':>8}")
    for c in focus:
        r = rows.get(c)
        if not r:
            print(f"  {c} 无数据")
            continue
        print(f"  {r['name']:<12}{r['chg_1d'] * 100:>8.2f}{r['chg_5d'] * 100:>8.2f}"
              f"{r['chg_20d'] * 100:>8.2f}{r['chg_60d'] * 100:>8.2f}"
              f"{(r['rs_20'] or 0):>8.4f}{r['days_top10_20']:>5}{r['consecutive_top10']:>4}"
              f"{r['up_days_60'] * 100:>7.1f}{(r['share'] or 0) * 100:>8.2f}"
              f"{(r['share_chg_5'] or 0) * 100:>8.2f}{(r['flow5'] or 0) * 100:>8.2f}")

    print(f"\n  横截面百分位（{len(rows)} 个板块）")
    for c in focus:
        pc = out["pct"].get(c)
        if not pc:
            continue
        print(f"  {boards[c]:<12} rs20 {pc['rs_20']:>6.1f} | P1天 {pc['days_top10_20']:>6.1f} "
              f"连 {pc['consecutive_top10']:>6.1f} 阳 {pc['up_days_60']:>6.1f} | "
              f"份额 {pc['share']:>6.1f} Δ5 {pc['share_chg_5']:>6.1f} | 流 {pc['flow5']:>6.1f}")

    dest = ROOT / "pipeline" / "cache" / f"factors-{args.as_of}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
