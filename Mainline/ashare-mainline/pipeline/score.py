"""五阶段判定 + 六因子打分 + 拥挤惩罚。

    python -m pipeline.score --as-of 2026-10-30

读 factors / run / limit 三个缓存，按 v1.6 §4 的裁决顺序
（fade → divergence → acceleration → expansion → ignition）逐条判据穷举，
阶段无法赋值者不进榜，改列 rotation.emerging。

输出 cache/score-<as_of>.json，报告生成端直接消费。
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

from pipeline import day  # noqa: E402

WEIGHTS = {"rs": 25, "persistence": 20, "volume": 20, "breadth": 15, "fundflow": 10, "narrative": 10}


def pct_in(values: list[float], v: float | None, *, higher_better: bool = True) -> float:
    """横截面百分位，口径 (below + 1) / n × 100，最高位给 100。

    与上一版 v1.6 报告一致：8 板块样本时新高 42.11 排第 2 得 87.5 = 7/8。
    """
    vals = [x for x in values if x is not None]
    if v is None or not vals:
        return 0.0
    below = sum(1 for x in vals if x < v)
    return round((below + 1) / len(vals) * 100, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--narrative", default="",
                    help="人工判定的叙事分，格式 BK1277:95,BK0475:75（0/40/60/75/85 + 来源加分）")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)
    # 先读落盘的叙事分，再用 CLI 覆盖：只靠 CLI 传参时，重跑漏一个参数就让 narrative 静默归零，
    # 白酒总分从 85.0 变 75.5 且不报错（实测踩过）。默认值必须持久化。
    narr = {}
    nfile = ROOT / "pipeline" / "templates" / "narrative.json"
    if nfile.exists():
        saved = json.loads(nfile.read_text(encoding="utf-8")).get(args.as_of) or {}
        narr = {k: float(v) for k, v in saved.items() if not k.startswith("_")}
    for part in filter(None, args.narrative.split(",")):
        k, v = part.split(":")
        narr[k.strip()] = float(v)

    fac = json.loads((ROOT / "pipeline" / "cache" / f"factors-{args.as_of}.json").read_text(encoding="utf-8"))
    run = json.loads((ROOT / "pipeline" / "cache" / f"run-{args.as_of}.json").read_text(encoding="utf-8"))
    lim = json.loads((ROOT / "pipeline" / "cache" / f"limit-{args.as_of}.json").read_text(encoding="utf-8"))

    rows = fac["rows"]
    pc = fac["pct"]

    # ---- 成分股 -> 板块，用于涨停/连板/开板 ----
    zt_by_code = {str(p["code"]).zfill(6): p for p in lim["limit_up"]}
    zb_by_code = {str(p["code"]).zfill(6): p for p in lim["broken"]}
    prev_by_code = {str(p["code"]).zfill(6): p for p in lim.get("prev_limit_up", [])}
    # 只加载本轮候选板块。cache 里会残留上一轮的 codes-BKxxxx.json，
    # glob 全目录会让这些已出局的板块一起进报价快照（多 2 次批量请求），
    # 更糟的是它们会悄悄参与任何按 members 遍历的逻辑。
    members: dict[str, list[str]] = {}
    _idx = ROOT / "pipeline" / "cache" / "codes-INDEX.json"
    _wanted = set(json.loads(_idx.read_text(encoding="utf-8"))) if _idx.exists() else None
    for f in sorted((ROOT / "pipeline" / "cache").glob("codes-*.json")):
        code = f.stem.split("-", 1)[1]
        if code == "INDEX":
            continue
        if _wanted is not None and code not in _wanted:
            continue
        members[code] = json.loads(f.read_text(encoding="utf-8"))

    # 龙头 5 日涨幅需要个股 K 线与当日成交额；K 线库已在 run 阶段建好，这里只补一次报价
    from pipeline import http, kline, quotes  # noqa: E402

    store = kline.Store(ROOT / "pipeline" / "cache" / "kline.sqlite")
    all_members = list(dict.fromkeys(str(c).zfill(6) for v in members.values() for c in v))
    # 报价快照落盘：龙头判定要按当日成交额排序，没有成交额就选不出龙头；
    # 但每改一行文案都重拉 485 只（9 次请求）既慢又碰到 WAF。同日快照一次即够，
    # 盘后价格不会变，命中缓存时不发任何请求。
    qc = ROOT / "pipeline" / "cache" / f"quotes-{args.as_of}.json"
    if qc.exists():
        snap = json.loads(qc.read_text(encoding="utf-8"))
        print(f"  报价快照命中缓存：{len(snap)} 只（0 请求）")
    else:
        snap = quotes.fetch(http.make_client(workers=4, max_rps=4, min_span=0), all_members)
        qc.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"  报价快照已落盘：{len(snap)} 只 -> {qc.name}")

    def leader_chg5(bk: str) -> float | None:
        """板块内当日成交额最大的成分股，其 5 日涨幅。"""
        codes = [str(c).zfill(6) for c in members.get(bk, [])]
        best, amt = None, -1.0
        for c in codes:
            a = (snap.get(c) or {}).get("amount_yi") or 0
            if a > amt:
                best, amt = c, a
        if not best:
            return None
        bars = store.bars(best, args.as_of, 6)
        # 库里有缺口时「6 根」跨的自然日不是 6 个交易日，5 日涨幅会被放大成 8~10 日涨幅。
        # 首根对不上当天就直接给 None，宁可缺这个字段也不要给个错数。
        if len(bars) < 6 or bars[0][0] != args.as_of:
            return None
        return round(bars[0][1] / bars[-1][1] - 1, 4)

    def pool_stats(bk: str) -> dict:
        codes = [str(c).zfill(6) for c in members.get(bk, [])]
        zt = [zt_by_code[c] for c in codes if c in zt_by_code]
        zb = [zb_by_code[c] for c in codes if c in zb_by_code]
        prev = [prev_by_code[c] for c in codes if c in prev_by_code]
        # 开板家数 = 涨停股中盘中开过板的（zbc > 0），v1.6 glossary 的板块口径
        open_n = sum(1 for x in zt if (x.get("open_times") or 0) > 0)
        # prev_limit_up 为空有两种可能：①没传 --prev（本轮没取上期池）②取到了但该板块上期无涨停。
        # 前者必须写 null（0 与「上期零家涨停」含义完全不同），否则 divergence 的
        # 「连板回落 ≥2 级」会用一个假的 0 去比，把没判据的板块判成「没回落」。
        prev_fetched = bool((lim or {}).get("prev_limit_up"))
        return {
            "limit_up_count": len(zt),
            "limit_up_names": [x["name"] for x in zt],
            "max_streak": max((x.get("streak") or 1) for x in zt) if zt else 0,
            "max_streak_prev": (max((x.get("streak") or 1) for x in prev) if prev else 0)
                                if prev_fetched else None,
            "open_count": open_n,
            "broken_count": len(zb),
            # 板块口径封板率 = 1 − 开板家数 ÷ 涨停家数（v1.6 glossary 原文定义）
            "seal_rate": round(1 - open_n / len(zt), 4) if zt else None,
            "leader_chg_5d": leader_chg5(bk),
        }

    # ---- 逐板块组装 ----
    # pool_stats 要对全部 8 个候选算，因为 breadth 的横截面百分位需要更宽的样本；
    # 只用「≥2 条闸门」的两个板块做分母，会让新高分位在 50/100 之间硬跳。
    gated = []
    for g in run["gates"]:
        f = rows.get(g["code"])
        if not f:
            continue
        gated.append({**f, "gate": g, **pool_stats(g["code"])})
    cands = [g for g in gated if g["gate"]["gates"] >= 2]

    # 横截面样本（分项各自用可得样本）
    all_rows = list(rows.values())
    S_rs = [r["rs_20"] for r in all_rows]
    S_dt = [r["days_top10_20"] for r in all_rows]
    S_cs = [r["consecutive_top10"] for r in all_rows]
    S_ud = [r["up_days_60"] for r in all_rows]
    S_sh = [r["share"] for r in all_rows]
    S_d5 = [r["share_chg_5"] for r in all_rows]
    S_fl = [r["flow5"] for r in all_rows]
    S_up = [c["gate"]["breadth_up_ratio"] for c in gated]
    S_nh = [c["gate"]["new_high_ratio"] for c in gated]
    S_zt = [c["limit_up_count"] for c in gated]
    S_g20 = [r["chg_20d"] for r in all_rows]

    out = []
    for c in gated:
        base20 = fac["base"]["chg_20d"]
        rs = (1 + c["chg_20d"]) / (1 + base20)
        crowding = 0.5 * (c["share_pct_250"] or 0) + 0.5 * pct_in(S_g20, c["chg_20d"]) / 100

        n_rs = pct_in(S_rs, c["rs_20"])
        n_pe = round((pct_in(S_dt, c["days_top10_20"]) + pct_in(S_cs, c["consecutive_top10"])
                      + pct_in(S_ud, c["up_days_60"])) / 3, 2)
        n_vo = round(pct_in(S_sh, c["share"]) * 0.6 + pct_in(S_d5, c["share_chg_5"]) * 0.4, 2)
        n_br = round(pct_in(S_up, c["gate"]["breadth_up_ratio"]) * 0.4
                     + pct_in(S_nh, c["gate"]["new_high_ratio"]) * 0.35
                     + pct_in(S_zt, c["limit_up_count"]) * 0.25, 2)
        n_ff = pct_in(S_fl, c["flow5"])
        n_na = narr.get(c["code"], 0.0)
        norm = {"rs": n_rs, "persistence": n_pe, "volume": n_vo, "breadth": n_br,
                "fundflow": n_ff, "narrative": n_na}
        contrib = {k: round(v * WEIGHTS[k] / 100, 2) for k, v in norm.items()}

        # 五阶段判据
        gate = c["gate"]
        d5, d20 = c["share_chg_5"] or 0, c["share_chg_20"] or 0
        crit = {
            "fade": [
                ("份额 Δ20 < −0.5pct", d20 < -0.005, f"{d20 * 100:+.2f}pct"),
                ("龙头 5 日跌幅 > 8%", (c["leader_chg_5d"] or 0) < -0.08,
                 f"{(c['leader_chg_5d'] or 0) * 100:+.2f}%"),
                ("上涨占比 < 45%", gate["breadth_up_ratio"] < 0.45, f"{gate['breadth_up_ratio'] * 100:.1f}%"),
            ],
            "divergence": [
                ("5 日涨幅 < 20 日涨幅/4", (c["chg_5d"] or 0) < (c["chg_20d"] or 0) / 4,
                 f"{(c['chg_5d'] or 0) * 100:+.2f}% vs {(c['chg_20d'] or 0) / 4 * 100:+.2f}%"),
                ("份额分位 > 0.90", (c["share_pct_250"] or 0) > 0.90, f"{c['share_pct_250']}"),
                # 板块自身前后对比：prev − cur ≥ 2。和全市场最高连板比是错的，
                # 首板板块会被误判成「回落」，白酒上一轮就因此被错判成 divergence。
                # prev 是 5 个交易日前的连板高度（limitpool --prev 取 10-23），不是「前一日」。
                ("连板回落 ≥ 2 级",
                 c["max_streak_prev"] is not None and c["max_streak_prev"] - c["max_streak"] >= 2,
                 f"5 个交易日前 {'无数据' if c['max_streak_prev'] is None else str(c['max_streak_prev']) + ' 级'}"
                 f" → 当日 {c['max_streak']} 级"),
            ],
            "acceleration": [
                ("20 日涨幅 > 25%", (c["chg_20d"] or 0) > 0.25, f"{(c['chg_20d'] or 0) * 100:+.2f}%"),
                ("拥挤度 > 0.85", crowding > 0.85, f"{crowding:.4f}"),
                ("封板率 > 75%", (c["seal_rate"] or 0) > 0.75,
                 f"{c['seal_rate'] if c['seal_rate'] is not None else '无涨停'}"),
            ],
            "expansion": [
                ("份额分位 0.60~0.85", 0.60 <= (c["share_pct_250"] or 0) <= 0.85, f"{c['share_pct_250']}"),
                ("涨停 ≥ 5 家", c["limit_up_count"] >= 5, f"{c['limit_up_count']} 家"),
                ("连板 3~4 级", 3 <= c["max_streak"] <= 4, f"{c['max_streak']} 级"),
                ("上涨占比 ≥ 60%", gate["breadth_up_ratio"] >= 0.60, f"{gate['breadth_up_ratio'] * 100:.1f}%"),
            ],
            "ignition": [
                ("份额 Δ5 ≥ +0.5pct", d5 >= 0.005, f"{d5 * 100:+.2f}pct"),
                ("前 10 天数 ≤ 8", c["days_top10_20"] <= 8, f"{c['days_top10_20']} 天"),
                ("涨停 2~5 家", 2 <= c["limit_up_count"] <= 5, f"{c['limit_up_count']} 家"),
            ],
        }
        # 阶段判定只对过了 ≥2 条闸门的候选做。闸门在前、阶段在后是 v1.6 的因果顺序：
        # 汽车零部件 P1 0/20、P2 上涨 7.6%，却因为「前 10 天数 ≤ 8」（它从没进过前 10）
        # 和「份额 Δ5 +0.58pct」被判成 ignition——不给闸门不足的板块判阶段，就不会出这种荒诞结果。
        hits = {k: [x for x in v if x[1]] for k, v in crit.items()}
        stage = None
        if c["gate"]["gates"] >= 2:
            for s in ("fade", "divergence", "acceleration", "expansion", "ignition"):
                if len(hits[s]) >= 2:
                    stage = s
                    break

        # 惩罚
        pen, reasons = 0, []
        if (c["share_pct_250"] or 0) > 0.95 and c["max_streak"] >= 5:
            pen = max(pen, 12)
            reasons.append("R1")
        if (c["share_pct_250"] or 0) > 0.90 and crowding > 0.90:
            pen = max(pen, 6)
            reasons.append("R2")
        if (c["chg_60d"] or 0) > 0.60 and (c["flow5"] or 0) < 0:
            pen = max(pen, 8)
            reasons.append("R3")

        total = round(sum(contrib.values()) - pen, 2)
        out.append({
            "code": c["code"], "name": c["name"], "stage": stage,
            "gates": c["gate"]["gates"],
            # 进榜条件：≥2 条闸门 AND 阶段可判定（§3 第 6 条）
            "listed": c["gate"]["gates"] >= 2 and stage is not None,
            "rs": round(rs, 4), "crowding": round(crowding, 4),
            "norm": norm, "contrib": contrib, "penalty": pen, "penalty_rules": reasons,
            "score": total,
            "pool": {k: c[k] for k in ("limit_up_count", "limit_up_names", "max_streak",
                                       "max_streak_prev", "open_count", "broken_count",
                                       "seal_rate", "leader_chg_5d")},
            "crit": {k: [(n, ok, val) for n, ok, val in v] for k, v in crit.items()},
        })

    out.sort(key=lambda x: x["score"], reverse=True)

    print("=" * 70)
    print("阶段判定（按 fade→divergence→acceleration→expansion→ignition 顺序）")
    print("=" * 70)
    for o in out:
        print(f"\n  {o['name']}（{o['code']}）")
        for s in ("fade", "divergence", "acceleration", "expansion", "ignition"):
            hi = sum(1 for _, ok, _ in o["crit"][s] if ok)
            mark = " <== 命中" if o["stage"] == s else ""
            print(f"    {s:<13} {hi}/{len(o['crit'][s])}{mark}")
            for n, ok, val in o["crit"][s]:
                print(f"        {'✓' if ok else '·'} {n:<24} {val}")
        print(f"    -> stage = {o['stage']}" if o["stage"]
              else f"    -> {'闸门不足（' + str(o['gates']) + ' 条），不做阶段判定' if o['gates'] < 2 else '阶段不可判定（进 emerging）'}")

    print("\n" + "=" * 70)
    print("六因子打分")
    print("=" * 70)
    for o in out:
        tag = "进榜" if o["listed"] else f"{o['gates']} 条闸门，进萌芽"
        print(f"\n  {o['name']}  score {o['score']}  penalty {o['penalty']} {o['penalty_rules']}  [{tag}]")
        print(f"    {'因子':<12}{'norm':>9}{'w':>5}{'贡献':>8}")
        for k, w in WEIGHTS.items():
            print(f"    {k:<12}{o['norm'][k]:>9.2f}{w:>5}{o['contrib'][k]:>8.2f}")
        print(f"    {'小计':<12}{'':>9}{'':>5}{sum(o['contrib'].values()):>8.2f}")

    dest = ROOT / "pipeline" / "cache" / f"score-{args.as_of}.json"
    dest.write_text(json.dumps({
        "as_of": args.as_of, "weights": WEIGHTS, "market": lim["market"],
        "cross_section": {"boards": len(all_rows), "p2_boards": len(cands)},
        "rows": out,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
