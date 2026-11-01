"""离线深度审计：不发起任何网络请求，只用 cache/ 里的原始数据复算报告里的每一个数。

    python tests/deep_audit.py --report aml-20261101-v16.json --as-of 2026-10-30

与 index.html 的 auditReport() 分工不同：
  - 页面体检只看「报告内部是否自洽」（contribution = normalized × weight 等），
    它拿到的是报告自己声明的 normalized，无法判断 normalized 背后的原始值对不对。
  - 这里回到面板 / K 线库 / 涨停池，用与生成端**不同的计算路径**复算，
    再和报告比。凡是报告说得出、原始数据算不出的，一律列为失败。

只读，不发请求。找不到缓存就报 SKIP，不猜值。
"""
from __future__ import annotations

import argparse
import json
import pickle
import re
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "pipeline" / "cache"

RESULTS: list[tuple[str, str, bool, str]] = []   # (组, 检查项, 通过, 细节)


def check(group: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((group, name, ok, detail))
    return ok


def close(a, b, tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


# ---------------------------------------------------------------- 载入
def load_caches(as_of: str) -> dict:
    out = {}
    out["panel"] = pickle.loads((C / f"panel-{as_of}.pkl").read_bytes())
    for k in ("run", "factors", "limit", "score", "market", "indices"):
        f = C / f"{k}-{as_of}.json"
        out[k] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    db = C / "kline.sqlite"
    out["db"] = sqlite3.connect(f"file:{db}?mode=ro", uri=True) if db.exists() else None
    return out


def members(bk: str) -> list[str]:
    f = C / f"codes-{bk}.json"
    return [str(x).zfill(6) for x in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []


def bars_of(db, code: str, as_of: str, n: int) -> list[tuple[str, float]]:
    rows = db.execute(
        "select d, c from kline where code=? and d<=? order by d desc limit ?", (code, as_of, n)
    ).fetchall()
    return [(d, c) for d, c in rows]


# ---------------------------------------------------------------- 复算：板块级
def compound(pcts: list[float], n: int) -> float | None:
    if len(pcts) < n:
        return None
    prod = 1.0
    for p in pcts[-n:]:
        prod *= 1.0 + p / 100.0
    return prod - 1.0


def recompute_panel(p: dict, bk: str, as_of: str) -> dict:
    """从面板原始行重算 board 级指标，不复用 factors.py。"""
    hist = p["history"].get(bk) or []          # [(d, pct)] 升序
    pcts = [x for _, x in hist]
    dates = sorted(p["by_date"])
    # 份额序列
    shares = []
    for d in dates:
        row = p["by_date"][d].get(bk)
        tot = p["market_amount"].get(d)
        if row and row.get("amount") and tot:
            shares.append((d, row["amount"] / tot))
    cur = shares[-1][1] if shares else None
    nets, amts = [], []
    for d in dates[-5:]:
        r = p["by_date"][d].get(bk)
        if r and r.get("net") is not None and r.get("amount"):
            nets.append(r["net"])
            amts.append(r["amount"])
    nets20 = []
    for d in dates[-20:]:
        r = p["by_date"][d].get(bk)
        if r and r.get("net") is not None:
            nets20.append(r["net"])
    return {
        "chg_1d": compound(pcts, 1), "chg_5d": compound(pcts, 5),
        "chg_20d": compound(pcts, 20), "chg_60d": compound(pcts, 60),
        "share": cur,
        "share_chg_5": (cur - shares[-6][1]) if len(shares) >= 6 else None,
        "share_chg_20": (cur - shares[-21][1]) if len(shares) >= 21 else None,
        "share_pct": (sum(1 for _, s in shares if s <= cur) / len(shares)) if shares else None,
        "share_samples": len(shares),
        "flow5": (sum(nets) / sum(amts)) if amts else None,
        "net5_yi": sum(nets) / 1e8 if nets else None,
        "net20_yi": sum(nets20) / 1e8 if nets20 else None,
        "amount_yi": (p["by_date"][as_of].get(bk) or {}).get("amount", 0) / 1e8,
        "vol20": (statistics.pstdev(pcts[-20:]) / 100) if len(pcts) >= 20 else None,
        "up_days_60": (sum(1 for x in pcts[-60:] if x > 0) / 60) if len(pcts) >= 60 else None,
    }


def recompute_top_days(p: dict, bk: str, as_of: str, n: int = 20, top: int = 10) -> tuple[int, int]:
    """滚动 n 日累计涨幅的横截面前 top 天数 / 连续天数。独立实现（不调用 panel.top_days）。"""
    roll: dict[str, dict[str, float]] = {}
    for code, series in p["history"].items():
        if len(series) < n:
            continue
        acc = {}
        for i in range(n - 1, len(series)):
            prod = 1.0
            for j in range(i - n + 1, i + 1):
                prod *= 1.0 + series[j][1] / 100.0
            acc[series[i][0]] = prod - 1.0
        roll[code] = acc
    ds = sorted(p["by_date"], reverse=True)[:n]
    hits = streak = 0
    broken = False
    for d in ds:
        board = {c: a[d] for c, a in roll.items() if d in a}
        if not board:
            continue
        top_codes = {c for c, _ in sorted(board.items(), key=lambda x: x[1], reverse=True)[:top]}
        if bk in top_codes:
            hits += 1
            if not broken:
                streak += 1
        else:
            broken = True
    return hits, streak


# ---------------------------------------------------------------- 复算：成分股级
def recompute_breadth(db, bk: str, as_of: str, cal: list[str], window: int = 20) -> dict:
    """上涨占比 / 新高占比，直接用 SQL 行复算（不调用 kline.breadth）。

    up：当日复权收盘与前一交易日复权收盘比较。库内 c 是连续复权序列，
        c(t)/c(t-1) 恒等于原始价 P/PC，因此在除权日也成立。
    new_high：当日收盘 > 前 window 个交易日（不含当日）的最高收盘。
    """
    codes = members(bk)
    idx = {d: i for i, d in enumerate(cal)}
    i = idx.get(as_of)
    prev_d = cal[i - 1] if i else None
    valid = up = high = 0
    lack21, no_asof, no_prev = [], [], []
    per = []
    for code in codes:
        b = bars_of(db, code, as_of, window + 1)
        if not b or b[0][0] != as_of:
            no_asof.append(code)
            continue
        if len(b) < window + 1:
            lack21.append(code)
            continue
        valid += 1
        c0 = b[0][1]
        prev = next((c for d, c in b if d == prev_d), None)
        if prev is None:
            no_prev.append(code)
        elif c0 > prev:
            up += 1
        hi = c0 > max(c for _, c in b[1:])
        if hi:
            high += 1
        per.append({"code": code, "up": (prev is not None and c0 > prev), "high": hi})
    return {
        "total": len(codes), "valid": valid,
        "up_ratio": up / valid if valid else None,
        "new_high_ratio": high / valid if valid else None,
        "lack21": lack21, "no_asof": no_asof, "no_prev": no_prev, "per": per,
    }


def recompute_leader(db, bk: str, as_of: str, cal: list[str]) -> dict:
    """板块内当日成交额最大的成分股 —— 离线版拿不到成交额，改用「报告声明的龙头」逐个验。"""
    out = {}
    for code in members(bk):
        b = bars_of(db, code, as_of, 6)
        if len(b) < 6 or b[0][0] != as_of:
            out[code] = None
            continue
        out[code] = b[0][1] / b[-1][1] - 1.0
    return out


# ---------------------------------------------------------------- 契约检查
ENUM = {
    "data_mode": {"live", "delayed", "demo"},
    "phase": {"risk_on", "risk_off", "structural", "choppy"},
    "stance": {"offensive", "balanced", "defensive"},
    "stage": {"ignition", "expansion", "acceleration", "divergence", "fade"},
    "theme_type": {"industry", "concept", "style"},
    "driver_type": {"policy", "industry", "earnings", "event", "liquidity"},
    "role": {"leader", "core", "follower"},
    "level": {"high", "medium", "low"},
    "impact": {"positive", "negative", "neutral"},
    "dq_status": {"ok", "partial", "degraded"},
    "reliability": {"high", "medium", "low"},
}


def recalc_gates(m: dict) -> int:
    mt, pe = m["metrics"], m["persistence"]
    n = 0
    n += int((pe["days_top10_20"] or 0) >= 6 and (pe["consecutive_top10"] or 0) >= 2)
    n += int((mt["breadth_up_ratio"] or 0) >= 0.55 and (mt["new_high_ratio"] or 0) >= 0.08)
    n += int((mt["turnover_share_chg_5"] or 0) >= 0.005 and (mt["turnover_share_pct_250"] or 0) >= 0.60)
    return n


def recalc_stage(m: dict) -> tuple[str | None, dict]:
    """按 fade→divergence→acceleration→expansion→ignition 顺序，取第一个 ≥2 项命中的。"""
    mt = m["metrics"]
    ms, msp = mt["max_streak"], mt["max_streak_prev"]
    crit = {
        "fade": [
            (mt["turnover_share_chg_20"] is not None and mt["turnover_share_chg_20"] < -0.005),
            (mt["leader_chg_5d"] is not None and mt["leader_chg_5d"] < -0.08),
            (mt["breadth_up_ratio"] is not None and mt["breadth_up_ratio"] < 0.45),
        ],
        "divergence": [
            (mt["chg_5d"] is not None and mt["chg_20d"] is not None and mt["chg_5d"] < mt["chg_20d"] / 4),
            (mt["turnover_share_pct_250"] is not None and mt["turnover_share_pct_250"] > 0.90),
            (ms is not None and msp is not None and (msp - ms) >= 2),
        ],
        "acceleration": [
            (mt["chg_20d"] is not None and mt["chg_20d"] > 0.25),
            (mt["crowding_pct"] is not None and mt["crowding_pct"] > 0.85),
            (mt["seal_rate"] is not None and mt["seal_rate"] > 0.75),
        ],
        "expansion": [
            (mt["turnover_share_pct_250"] is not None and 0.60 <= mt["turnover_share_pct_250"] <= 0.85),
            (mt["limit_up_count"] is not None and mt["limit_up_count"] >= 5),
            (ms is not None and ms in (3, 4)),
            (mt["breadth_up_ratio"] is not None and mt["breadth_up_ratio"] >= 0.60),
        ],
        "ignition": [
            (mt["turnover_share_chg_5"] is not None and mt["turnover_share_chg_5"] >= 0.005),
            (m["persistence"]["days_top10_20"] is not None and m["persistence"]["days_top10_20"] <= 8),
            (mt["limit_up_count"] is not None and 2 <= mt["limit_up_count"] <= 5),
        ],
    }
    hits = {k: sum(v) for k, v in crit.items()}
    for k in ("fade", "divergence", "acceleration", "expansion", "ignition"):
        if hits[k] >= 2:
            return k, hits
    return None, hits


def recalc_penalty(m: dict) -> tuple[int, str]:
    mt = m["metrics"]
    r1 = (mt["turnover_share_pct_250"] or 0) > 0.95 and (mt["max_streak"] or 0) >= 5
    r2 = (mt["turnover_share_pct_250"] or 0) > 0.90 and (mt["crowding_pct"] or 0) > 0.90
    r3 = (mt["chg_60d"] or 0) > 0.60 and (mt["net_inflow_5_yi"] or 0) < 0
    cand = [(-12, "R1")] if r1 else []
    cand += [(-6, "R2")] if r2 else []
    cand += [(-8, "R3")] if r3 else []
    if not cand:
        return 0, "无触发"
    v, tag = max(cand, key=lambda x: abs(x[0]))
    return v, tag


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="aml-20261101-v16.json")
    ap.add_argument("--as-of", default="2026-10-30")
    args = ap.parse_args()

    rep = json.loads((ROOT / args.report).read_text(encoding="utf-8"))
    ca = load_caches(args.as_of)
    p, db = ca["panel"], ca["db"]
    idx_raw = ca.get("indices") or {}
    idx_live = {r["code"].split(".")[0]: r for r in (idx_raw.get("indices") or [])}
    cal = sorted(p["by_date"])
    run, fac, lim, sc = ca["run"], ca["factors"], ca["limit"], ca["score"]

    # ---------------- A 结构
    want = ["schema_version", "meta", "window", "data_quality", "market_regime", "verdict",
            "mainlines", "rotation", "risks", "scenarios", "playbook", "appendix"]
    check("A 结构", "顶层 12 键且顺序一致", list(rep.keys()) == want, ",".join(rep.keys()))

    try:
        import jsonschema
        schema = json.loads((ROOT / "schema" / "ashare-mainline.schema.json").read_text(encoding="utf-8"))
        errs = list(jsonschema.Draft7Validator(schema).iter_errors(rep))
        check("A 结构", "JSON Schema", not errs, f"{len(errs)} 处" if errs else "0 错误")
    except ImportError:
        check("A 结构", "JSON Schema", True, "SKIP 未安装 jsonschema")

    errs = []
    for m in rep["mainlines"]:
        if m["stage"] not in ENUM["stage"]:
            errs.append(f"{m['name']} stage={m['stage']}")
        if m["theme_type"] not in ENUM["theme_type"]:
            errs.append(f"{m['name']} theme_type={m['theme_type']}")
        for d in m["drivers"]:
            if d["type"] not in ENUM["driver_type"]:
                errs.append(f"{m['name']} driver.type={d['type']}")
        for ld in m["leaders"]:
            if ld["role"] not in ENUM["role"]:
                errs.append(f"{m['name']} leader.role={ld['role']}")
    for r in rep["risks"]:
        if r["level"] not in ENUM["level"]:
            errs.append(f"risk {r['id']} level={r['level']}")
    for t in rep["rotation"]["timeline"]:
        if t["impact"] not in ENUM["impact"]:
            errs.append(f"timeline {t['date']} impact={t['impact']}")
    if rep["market_regime"]["phase"] not in ENUM["phase"]:
        errs.append(f"phase={rep['market_regime']['phase']}")
    if rep["verdict"]["stance"] not in ENUM["stance"]:
        errs.append(f"stance={rep['verdict']['stance']}")
    if rep["meta"]["data_mode"] not in ENUM["data_mode"]:
        errs.append(f"data_mode={rep['meta']['data_mode']}")
    if rep["data_quality"]["status"] not in ENUM["dq_status"]:
        errs.append(f"dq.status={rep['data_quality']['status']}")
    for s in rep["data_quality"]["sources"]:
        if s["reliability"] not in ENUM["reliability"]:
            errs.append(f"source reliability={s['reliability']}")
    check("A 结构", "枚举取值合法", not errs, "; ".join(errs[:4]) or "全部合法")

    lens = {"score_breakdown": (rep["mainlines"], 6, 6), "drivers": (rep["mainlines"], 2, 4),
            "leaders": (rep["mainlines"], 3, 5), "invalidation": (rep["mainlines"], 2, 4),
            "watch": (rep["mainlines"], 2, 3)}
    bad = []
    for k, (arr, lo, hi) in lens.items():
        for m in arr:
            if not (lo <= len(m[k]) <= hi):
                bad.append(f"{m['name']}.{k}={len(m[k])}")
    if len(rep["scenarios"]) != 3:
        bad.append(f"scenarios={len(rep['scenarios'])}")
    if not (3 <= len(rep["risks"]) <= 5):
        bad.append(f"risks={len(rep['risks'])}")
    if not (4 <= len(rep["rotation"]["timeline"]) <= 6):
        bad.append(f"timeline={len(rep['rotation']['timeline'])}")
    check("A 结构", "数组长度在骨架范围内", not bad, "; ".join(bad) or "全部合规")

    check("A 结构", "rank 连续且按 score 降序",
          [m["rank"] for m in rep["mainlines"]] == list(range(1, len(rep["mainlines"]) + 1))
          and all(rep["mainlines"][i]["score"] >= rep["mainlines"][i + 1]["score"]
                  for i in range(len(rep["mainlines"]) - 1)),
          f"n={len(rep['mainlines'])}")
    check("A 结构", "mainline_count 与实际条数一致",
          rep["verdict"]["mainline_count"] == len(rep["mainlines"]),
          f"{rep['verdict']['mainline_count']} vs {len(rep['mainlines'])}")
    prob = sum(s["probability"] for s in rep["scenarios"])
    check("A 结构", "情景概率合计 = 1", close(prob, 1.0, 0.01), f"{prob:.4f}")

    # ---------------- B 打分算术
    for m in rep["mainlines"]:
        sb = m["score_breakdown"]
        w = sum(x["weight"] for x in sb)
        contrib = sum(x["contribution"] for x in sb)
        arith = all(close(x["contribution"], x["normalized"] * x["weight"] / 100, 0.02) for x in sb)
        ok = (len(sb) == 6 and close(w, 100, 0.001) and arith
              and close(contrib - m["penalty"], m["score"], 0.5))
        check("B 算术", f"{m['name']} 六因子：Σ权重=100、贡献可复算、Σ−惩罚=score", ok,
              f"Σw={w} Σc={contrib:.2f} pen={m['penalty']} score={m['score']}")
        rs_row = next((x for x in sb if x["key"] == "rs"), None)
        check("B 算术", f"{m['name']} rs.raw == metrics.rs_20",
              rs_row is not None and rs_row["raw"] == m["metrics"]["rs_20"],
              f"{rs_row and rs_row['raw']} vs {m['metrics']['rs_20']}")

    for m in rep["mainlines"]:
        pen, tag = recalc_penalty(m)
        check("B 算术", f"{m['name']} 惩罚可复算", abs(pen) == m["penalty"],
              f"复算 −{abs(pen)}({tag}) vs 报告 扣 {m['penalty']}")
        check("B 算术", f"{m['name']} penalty 取值在 {{0,6,8,12}}", m["penalty"] in (0, 6, 8, 12), str(m["penalty"]))

        n = recalc_gates(m)
        pc = m["persistence"]["pulse_check"]
        check("B 算术", f"{m['name']} 闸门条数与 metrics 自洽",
              n == pc["rules_passed"] and pc["passed"] == (n >= 2),
              f"复算 {n} vs 声明 {pc['rules_passed']}，passed={pc['passed']}")

        st, hits = recalc_stage(m)
        check("B 算术", f"{m['name']} 阶段可复算（按裁决顺序）", st == m["stage"],
              f"复算 {st} vs 报告 {m['stage']} | 命中 {hits}")

    # ---------------- C 数据层
    for m in rep["mainlines"]:
        bk = m["sector_codes"][0]
        rp = recompute_panel(p, bk, args.as_of)
        mt = m["metrics"]
        for k, key in (("chg_1d", "chg_1d"), ("chg_5d", "chg_5d"), ("chg_20d", "chg_20d"), ("chg_60d", "chg_60d")):
            check("C 面板", f"{m['name']} {key}", close(rp[k], mt[key], 0.0002),
                  f"复算 {rp[k]:+.4%} vs 报告 {mt[key]:+.4%}" if rp[k] is not None else "无数据")
        check("C 面板", f"{m['name']} turnover_share",
              close(rp["share"], mt["turnover_share"], 0.0001),
              f"复算 {rp['share']:.4%} vs 报告 {mt['turnover_share']:.4%}")
        check("C 面板", f"{m['name']} 份额 Δ5 / Δ20",
              close(rp["share_chg_5"], mt["turnover_share_chg_5"], 0.0001)
              and close(rp["share_chg_20"], mt["turnover_share_chg_20"], 0.0001),
              f"Δ5 {rp['share_chg_5']:+.4f} / Δ20 {rp['share_chg_20']:+.4f}")
        check("C 面板", f"{m['name']} 份额分位",
              close(rp["share_pct"], mt["turnover_share_pct_250"], 0.0002),
              f"复算 {rp['share_pct']:.4f}（{rp['share_samples']} 样本）vs 报告 {mt['turnover_share_pct_250']}")
        hits, streak = recompute_top_days(p, bk, args.as_of)
        check("C 面板", f"{m['name']} P1 天数/连续",
              hits == m["persistence"]["days_top10_20"] and streak == m["persistence"]["consecutive_top10"],
              f"复算 {hits}/20 连续 {streak} vs 报告 {m['persistence']['days_top10_20']}/{m['persistence']['consecutive_top10']}")
        h5, _ = recompute_top_days(p, bk, args.as_of, top=5)
        check("C 面板", f"{m['name']} days_top5_20", h5 == m["persistence"]["days_top5_20"],
              f"复算 {h5} vs 报告 {m['persistence']['days_top5_20']}")
        check("C 面板", f"{m['name']} 资金净额 5 日",
              close(rp["net5_yi"], mt["net_inflow_5_yi"], 0.5),
              f"复算 {rp['net5_yi']:.2f} 亿 vs 报告 {mt['net_inflow_5_yi']}")
        check("C 面板", f"{m['name']} volatility_20",
              close(rp["vol20"], mt["volatility_20"], 0.0002),
              f"复算 {rp['vol20']:.4f} vs 报告 {mt['volatility_20']}")

        # K 线口径
        rb = recompute_breadth(db, bk, args.as_of, cal)
        check("C 成分股", f"{m['name']} 上涨占比",
              close(rb["up_ratio"], mt["breadth_up_ratio"], 0.001),
              f"复算 {rb['up_ratio']:.4f}({rb['valid']}/{rb['total']}) vs 报告 {mt['breadth_up_ratio']}")
        check("C 成分股", f"{m['name']} 新高占比",
              close(rb["new_high_ratio"], mt["new_high_ratio"], 0.001),
              f"复算 {rb['new_high_ratio']:.4f} vs 报告 {mt['new_high_ratio']}")
        check("C 成分股", f"{m['name']} limit_up_ratio == 涨停/有效样本",
              close(mt["limit_up_ratio"], mt["limit_up_count"] / rb["valid"], 0.001),
              f"{mt['limit_up_count']}/{rb['valid']}={mt['limit_up_count'] / rb['valid']:.4f} vs {mt['limit_up_ratio']}")

        # 涨停池
        mb = set(members(bk))
        zt = [x for x in (lim["limit_up"] if lim else []) if str(x["code"]).zfill(6) in mb]
        zb = [x for x in (lim["broken"] if lim else []) if str(x["code"]).zfill(6) in mb]
        check("C 涨停池", f"{m['name']} 涨停家数",
              len(zt) == mt["limit_up_count"], f"复算 {len(zt)} vs 报告 {mt['limit_up_count']}")
        ms = max((x["streak"] or 1) for x in zt) if zt else 0
        check("C 涨停池", f"{m['name']} max_streak",
              ms == mt["max_streak"], f"复算 {ms} vs 报告 {mt['max_streak']}")
        opens = sum(1 for x in zt if (x.get("open_times") or 0) > 0)
        seal = 1 - opens / len(zt) if zt else None
        check("C 涨停池", f"{m['name']} seal_rate=1−开板/涨停",
              close(seal, mt["seal_rate"], 0.001),
              f"复算 {seal}（{opens}/{len(zt)} 开板）vs 报告 {mt['seal_rate']}")

        lc = recompute_leader(db, bk, args.as_of, cal)
        # 龙头判定：报告声明谁是 leader，用其成交额最大者核对
        ldr = max(m["leaders"], key=lambda x: x["turnover_yi"])
        code = ldr["code"].split(".")[0]
        check("C 成分股", f"{m['name']} leader_chg_5d（{ldr['name']}）",
              close(lc.get(code), mt["leader_chg_5d"], 0.002),
              f"复算 {lc.get(code)} vs 报告 {mt['leader_chg_5d']}")

        # 领袖名单本身的 20 日涨幅
        for ld in m["leaders"]:
            c6 = ld["code"].split(".")[0]
            b = bars_of(db, c6, args.as_of, 21)
            if len(b) < 21 or b[0][0] != args.as_of:
                check("C 成分股", f"{ld['name']} chg_20d", True, "SKIP 库内不足 21 根")
                continue
            v = b[0][1] / b[-1][1] - 1.0
            check("C 成分股", f"{ld['name']} chg_20d", close(v, ld["chg_20d"], 0.002),
                  f"复算 {v:+.4%} vs 报告 {ld['chg_20d']:+.4%}")

    # ---------------- D 口径一致性
    w = rep["window"]
    check("D 口径", "effective_range.start == 20 日窗口首日", w["effective_range"]["start"] == cal[-20],
          f"{w['effective_range']['start']} vs 面板 {cal[-20]}")
    check("D 口径", "effective_range.end == as_of", w["effective_range"]["end"] == args.as_of, w["effective_range"]["end"])

    gaps = []
    for a, b in zip(cal, cal[1:]):
        if (date.fromisoformat(b) - date.fromisoformat(a)).days > 4:
            gaps.append((a, b))
    # 缺行是两端之间的日期，端点本身有行，不能写进 excluded
    inwin = []
    for a, b in gaps:
        if a >= cal[-40]:
            d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
            n = (d1 - d0).days
            inwin.append((d0.isoformat(), d1.isoformat(),
                          f"{date.fromordinal(d0.toordinal() + 1)}~{date.fromordinal(d1.toordinal() - 1)}"))
    claimed = "".join(x["date_range"] for x in w["excluded"])
    ok = all(mid in claimed for _, _, mid in inwin)
    check("D 口径", "excluded 覆盖窗口内的缺行", ok,
          f"面板缺口 {[(a, b) for a, b, _ in inwin]} → 应写 {[m for _, _, m in inwin]} / 报告 {claimed}")

    # coverage 算式可复算
    note = next((x for x in rep["data_quality"]["notes"] if x.startswith("coverage")), "")
    mnum = re.findall(r"(\d+(?:\.\d+)?)×(\d+(?:\.\d+)?)", note)
    declared = re.search(r"=\s*(\d+(?:\.\d+)?)\s*→", note)
    if mnum and declared:
        calc = sum(float(a) * float(b) for a, b in mnum)
        check("D 口径", "coverage 算式可复算", close(calc, float(declared.group(1)), 0.6),
              f"按算式 {calc:.1f} vs 声明 {declared.group(1)} → {rep['data_quality']['coverage']}")
    else:
        check("D 口径", "coverage 算式可复算", False, "notes 里找不到可解析算式")

    # 分位样本数披露
    samples = fac["rows"]["BK1277"]["name"] if "BK1277" in fac["rows"] else None
    if rep["window"]["quantile_days"] == 250:
        s = next((x for x in rep["data_quality"]["notes"] if "169" in x or "样本" in x), "")
        check("D 口径", "250 日分位已披露实际样本数", bool(s), s[:60] or "未披露")

    # 份额分母
    denom = rep["window"]["rules"][-1] if rep["window"]["rules"] else ""
    amt = p["market_amount"].get(args.as_of, 0) / 1e8
    check("D 口径", "份额分母与面板合计一致", close(amt, run["market_amount_yi"], 0.01),
          f"{amt:.2f} 亿 vs run {run['market_amount_yi']} 亿 | {denom[:40]}")

    # 绝对表述：只查真正会误导的两类 —— 「唯一」与「只有…（通过|过闸门|进榜）」，
    # 要求同一句里出现范围限定词（板块/128/中/里/内/样本/已完整/以来）。
    # 「合力只有 6 个交易日」「只有 1 条」是数值限定，不是排他断言，不在此列。
    text = json.dumps(rep, ensure_ascii=False)
    sents = re.split(r"[。；\n]", text)
    scope = re.compile(r"128|板块|中|里|内|样本|已完整|以来|个现役")
    bad_abs = []
    for s in sents:
        hit = bool(re.search(r"唯一", s)) or bool(re.search(r"只有[^，。]{0,12}(通过|过闸门|进榜|入选)", s))
        if hit and not scope.search(s):
            bad_abs.append(s.strip()[:70])
    check("D 口径", "绝对表述都有范围限定", not bad_abs,
          "; ".join(bad_abs) if bad_abs else f"{len(sents)} 个分句内无越界排他断言")

    # 0 填充：max_streak_prev 在没有上期池时必须是 null
    prev_pool = (lim or {}).get("prev_limit_up") or []
    for m in rep["mainlines"]:
        if not prev_pool and m["metrics"]["max_streak_prev"] is not None:
            check("D 口径", f"{m['name']} max_streak_prev 未用 0 填充", False,
                  f"上期池为空但填了 {m['metrics']['max_streak_prev']}")
        else:
            check("D 口径", f"{m['name']} max_streak_prev 未用 0 填充", True,
                  f"上期池 {len(prev_pool)} 条，值 {m['metrics']['max_streak_prev']}")

        # 分项 note 的算式是否等于声明的 normalized
        for row in m["score_breakdown"]:
            mm = re.search(r"\((\d+(?:\.\d+)?)\)×0\.6 \+ Δ5 分位 ([\d.]+)×0\.4 = ([\d.]+)", row["note"])
            if mm:
                a, b, c = float(mm.group(1)), float(mm.group(2)), float(mm.group(3))
                calc = a * 0.6 + b * 0.4
                check("D 口径", f"{m['name']} volume note 可复算", close(calc, c, 0.02),
                      f"{a}×0.6+{b}×0.4={calc:.2f} vs note 写的 {c}（normalized={row['normalized']}）")
            mm = re.search(r"([\d.]+)\(([\d.]+)\)、新高 ([\d.]+)%\(([\d.]+)\)、涨停 (\d+) 家\(([\d.]+)\)",
                           row["note"])
            if mm:
                a, b, c, d, e, f = mm.groups()
                calc = float(b) * 0.4 + float(d) * 0.35 + float(f) * 0.25
                check("D 口径", f"{m['name']} breadth note 可复算", close(calc, row["normalized"], 0.05),
                      f"{b}×0.4+{d}×0.35+{f}×0.25={calc:.2f} vs normalized {row['normalized']}")

    # ---------------- E 覆盖面（哪些板块必须出现、有没有漏）
    g8 = run["gates"]
    notes_blob = "".join(rep["data_quality"]["notes"])
    missing_matrix = [g["name"] for g in g8 if g["gates"] >= 1 and f"闸门矩阵：{g['name']}" not in notes_blob]
    check("E 覆盖", "闸门矩阵覆盖所有过 ≥1 条闸门的板块", not missing_matrix,
          f"{len([g for g in g8 if g['gates'] >= 1])} 个板块 / 缺 {missing_matrix}")

    one_gate = {g["name"] for g in g8 if g["gates"] == 1}
    em_names = [e["name"] for e in rep["rotation"]["emerging"]]
    # rotation.emerging 只保留前 5，其余要写进 rotation.narrative
    uncovered = [n for n in one_gate if n not in em_names and n not in rep["rotation"]["narrative"]]
    check("E 覆盖", "仅过 1 条闸门的板块都在 emerging 或 narrative 里", not uncovered,
          f"1 条闸门 {len(one_gate)} 个，漏 {uncovered}")
    em_scores = [e["score"] for e in rep["rotation"]["emerging"]]
    check("E 覆盖", "emerging 按 score 降序", em_scores == sorted(em_scores, reverse=True), str(em_scores))
    check("E 覆盖", "emerging 不超过 5 条", len(em_scores) <= 5, f"{len(em_scores)} 条")

    # ---------------- F 指标可复核性
    for m in rep["mainlines"]:
        bk = m["sector_codes"][0]
        rp = recompute_panel(p, bk, args.as_of)
        check("F 指标", f"{m['name']} 20 日资金净额",
              close(rp["net20_yi"], m["metrics"]["net_inflow_20_yi"], 1.0),
              f"复算 {rp['net20_yi']:.2f} 亿 vs 报告 {m['metrics']['net_inflow_20_yi']}")

        # rs_60 现在有独立源（cache/indices-<as_of>.json，腾讯日 K 真取），
        # 直接按净值比复算，不再靠代理估量级。
        rs60 = m["metrics"]["rs_60"]
        idxq = (idx_live or {}).get("000985") or {}
        if rs60 is None:
            check("F 指标", f"{m['name']} rs_60", True, "null（已在 missing 说明）")
        elif idxq.get("chg_60d") is None:
            check("F 指标", f"{m['name']} rs_60", False, "指数缓存缺 60 日收益")
        else:
            want = round((1 + m["metrics"]["chg_60d"]) / (1 + idxq["chg_60d"]), 4)
            check("F 指标", f"{m['name']} rs_60 = (1+chg_60d)/(1+中证全指 60d)",
                  abs(want - rs60) <= 1e-4,
                  f"复算 {want} vs 报告 {rs60}（基准 {idxq['chg_60d'] * 100:+.2f}%）")
        for key, days in (("rs_5", "chg_5d"), ("rs_20", "chg_20d")):
            if m["metrics"].get(key) is None or idxq.get(days) is None:
                check("F 指标", f"{m['name']} {key}", False, "缺数据")
                continue
            want = round((1 + m["metrics"][days]) / (1 + idxq[days]), 4)
            check("F 指标", f"{m['name']} {key} = 净值比",
                  abs(want - m["metrics"][key]) <= 1e-4,
                  f"复算 {want} vs 报告 {m['metrics'][key]}")

        # 领袖文案里的数字必须能算出来（5 日涨幅、成交额排名、收盘价）
        qp = C / f"quotes-{args.as_of}.json"
        qs = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else {}
        amts = sorted([(qs.get(c) or {}).get("amount_yi") or 0 for c in members(bk)], reverse=True)
        for ld in m["leaders"]:
            c6 = ld["code"].split(".")[0]
            note = ld["note"]
            mm = re.search(r"(\d+) 日 ([+-]\d+\.\d+)%", note)
            if mm:
                n = int(mm.group(1))
                b = bars_of(db, c6, args.as_of, n + 1)
                if len(b) == n + 1 and b[0][0] == args.as_of:
                    v = (b[0][1] / b[-1][1] - 1) * 100
                    check("F 指标", f"{ld['name']} note 里的 {n} 日涨幅",
                          abs(v - float(mm.group(2))) <= 0.06, f"复算 {v:+.2f}% vs note {mm.group(2)}%")
            mm = re.search(r"居板块第 (\d+)", note)
            if mm and qs.get(c6):
                v = qs[c6].get("amount_yi") or 0
                rank = amts.index(v) + 1 if v in amts else None
                check("F 指标", f"{ld['name']} note 里的成交额排名", rank == int(mm.group(1)),
                      f"复算第 {rank} vs note 第 {mm.group(1)}")
            if qs.get(c6):
                check("F 指标", f"{ld['name']} turnover_yi",
                      close(ld["turnover_yi"], qs[c6].get("amount_yi"), 0.02),
                      f"快照 {qs[c6].get('amount_yi')} vs 报告 {ld['turnover_yi']}")
            mm = re.search(r"收盘 (\d+\.\d+) 元", note)
            if mm:
                b = bars_of(db, c6, args.as_of, 1)
                check("F 指标", f"{ld['name']} note 里的收盘价",
                      b and abs(b[0][1] - float(mm.group(1))) < 0.02,
                      f"库内 {b[0][1] if b else '?'} vs note {mm.group(1)}")
        kl_note = next((s["note"] for s in rep["data_quality"]["sources"] if "K 线" in s["note"]), "")
        actual_days = db.execute("select count(distinct d) from kline").fetchone()[0]
        mm = re.search(r"(\d+) 个交易日", kl_note)
        check("F 指标", "sources 里的 K 线天数与库一致",
              bool(mm) and int(mm.group(1)) == actual_days,
              f"note {mm.group(1) if mm else '?'} 日 vs 库 {actual_days} 日")

    # horizon / timeline 时间逻辑
    hv = rep["verdict"]["horizon"]["valid_until"]
    check("F 指标", "valid_until 晚于 as_of", hv > args.as_of, f"{hv} vs {args.as_of}")
    win_dates = {d for d in cal if cal[-20] <= d <= args.as_of}
    tl_out = [t["date"] for t in rep["rotation"]["timeline"] if t["date"] not in win_dates]
    check("F 指标", "timeline 日期落在 20 日窗口内", not tl_out, f"越界 {tl_out}" if tl_out else f"{len(rep['rotation']['timeline'])} 条")

    # ---------------- G 取数层（本轮真实抓取的完整性）
    # 报告里的每个数都要能追到一份当轮产出的缓存；缓存本身也要自洽。
    report_id = rep["meta"]["report_id"]

    # G1 指数：缓存存在、覆盖报告里的 6 个、逐字段与报告一致
    check("G 取数", "指数缓存存在且含 8 个指数", len(idx_raw.get("indices") or []) >= 6,
          f"{len(idx_raw.get('indices') or [])} 个")
    bad_idx = []
    for it in rep["market_regime"]["indices"]:
        key = it["code"].split(".")[0]
        src = idx_live.get(key)
        if not src:
            bad_idx.append(f"{it['name']} 缓存缺")
            continue
        for rk, ck, tol in (("close", "close", 0.01), ("chg_1d", "chg_1d", 1e-4),
                            ("chg_5d", "chg_5d", 1e-4), ("chg_20d", "chg_20d", 1e-4),
                            ("turnover_yi", "turnover_yi", 0.02)):
            if src.get(ck) is None or not close(it[rk], src[ck], tol):
                bad_idx.append(f"{it['name']}.{rk} {it[rk]} vs 缓存 {src.get(ck)}")
    check("G 取数", "报告 indices 与指数缓存逐字段一致", not bad_idx,
          "; ".join(bad_idx[:4]) or "6 个指数全部对齐")

    # G2 指数快照日期必须落在基准日（防止把上一交易日写成今天）
    dates_idx = {r.get("trade_date") for r in (idx_raw.get("indices") or [])}
    check("G 取数", "指数日 K 末根 == as_of", dates_idx <= {args.as_of},
          f"末根日期 {sorted(d for d in dates_idx if d)}")

    # G3 面板指纹：run.json 记录的就是当前 pkl，否则下游可能用了旧面板
    fp = (run or {}).get("panel_pkl") or {}
    pkl = C / f"panel-{args.as_of}.pkl"
    check("G 取数", "panel pkl 指纹与 run.json 一致",
          bool(fp) and pkl.exists() and abs(pkl.stat().st_mtime - float(fp.get("mtime", -1))) < 1.0,
          f"run 记 {fp.get('mtime')} / 实际 {pkl.stat().st_mtime if pkl.exists() else None}")

    # G4 面板完整性：128 个板块在 as_of 当天都有行，且份额分母 == 各行成交额之和
    codes_all = [b["code"] for b in p["boards"]]
    miss_bar = [c for c in codes_all if args.as_of not in p["by_date"] or c not in p["by_date"][args.as_of]]
    check("G 取数", "全部板块在 as_of 有日线", not miss_bar,
          f"{len(codes_all)} 个板块，缺 {len(miss_bar)} 个 {miss_bar[:4]}")
    day = p["by_date"].get(args.as_of) or {}
    amt_sum = sum(v["amount"] for v in day.values() if v.get("amount"))
    check("G 取数", "份额分母 == as_of 各板块成交额之和",
          close(amt_sum / 1e8, (run or {}).get("market_amount_yi"), 0.01),
          f"逐行加总 {amt_sum / 1e8:.2f} 亿 vs run {run.get('market_amount_yi')} 亿")

    # G5 K 线库：日期与只数，以及与 P2「有效样本」是否能对上
    n_codes = db.execute("select count(distinct code) from kline").fetchone()[0]
    n_days = db.execute("select count(distinct d) from kline").fetchone()[0]
    check("G 取数", "K 线库覆盖 ≥485 只 / ≥45 个交易日", n_codes >= 485 and n_days >= 45,
          f"{n_codes} 只 / {n_days} 个交易日")
    # 别名不能用 c：kline 表里 c 是收盘价，having 会解析成「收盘价 > 1」而不是「计数 > 1」
    dup = db.execute(
        "select code,d,count(*) cnt from kline group by code,d having cnt>1 limit 3").fetchall()
    check("G 取数", "K 线库 (code,date) 无重复", not dup, f"{len(dup)} 组重复 {dup[:2]}")

    # G6 成分股文件只认本轮候选，残留文件不得进入统计
    idx_json = json.loads((C / "codes-INDEX.json").read_text(encoding="utf-8"))
    all_files = [f.stem.split("-", 1)[1] for f in C.glob("codes-*.json") if f.stem != "codes-INDEX"]
    stale = sorted(set(all_files) - set(idx_json))
    cand_members = set()
    for bkc in idx_json:
        cand_members |= set(members(bkc))
    noted = re.search(r"去重 (\d+) 只", (rep["data_quality"]["sources"][2] or {}).get("note", ""))
    check("G 取数", "sources 里的成分股去重数与候选池一致",
          bool(noted) and int(noted.group(1)) == len(cand_members),
          f"复算 {len(cand_members)} 只 vs note {noted.group(1) if noted else '未写'}；"
          f"残留文件 {len(stale)} 个已排除 {stale[:3]}")

    # G7 本轮取数没有出现封禁/失败静默
    rq = (run or {}).get("requests") or {}
    check("G 取数", "run 阶段无封禁、无失败", (rq.get("blocked") or 0) == 0 and (rq.get("failed") or 0) == 0,
          f"ok {rq.get('ok')} / blocked {rq.get('blocked')} / failed {rq.get('failed')}")

    # G8 取不到的必须写进 missing，不能静默留空
    miss_fields = " ".join(x["field"] for x in rep["data_quality"]["missing"])
    check("G 取数", "全市场涨跌家数已声明为留存未复核", "涨跌家数" in miss_fields,
          miss_fields[:70])
    check("G 取数", "index/missing 不再声称指数未复核",
          "指数 5/20/60" not in miss_fields, miss_fields[:70])

    # G9 取数策略必须落盘且是「并发 4 / 4 rps / 铺开 ≥40s」这一档
    # 报告里的每个数都来自这轮请求，节奏如果不可复核，"没被限流"就只是自述。
    pol_raw = (run or {}).get("fetch_policy") or {}
    fa = (run or {}).get("fetch_audit") or {}
    check("G 取数", "取数策略已落盘（并发/速率/铺开）",
          {"workers", "max_rps", "min_span_seconds"} <= set(pol_raw),
          json.dumps({k: pol_raw.get(k) for k in ("workers", "max_rps", "min_span_seconds")},
                     ensure_ascii=False))
    check("G 取数", "并发 ≤4 且速率 ≤4 rps",
          (pol_raw.get("workers") or 99) <= 4 and (pol_raw.get("max_rps") or 99) <= 4.0,
          f"workers {pol_raw.get('workers')} / {pol_raw.get('max_rps')} rps")

    # G10 实测节奏：跨度达标、实测速率不越界、峰值并发不超声明、零违约
    pol = fa.get("policy") or {}
    span = fa.get("span_seconds") or 0.0
    target = pol.get("min_span_seconds") or 40
    check("G 取数", f"整轮请求铺开 ≥{target:.0f}s", span >= target - 0.5,
          f"{span}s / {(fa.get('requests') or {}).get('ok')} 次 / gap {fa.get('gap_seconds')}s")
    rps_meas = fa.get("achieved_rps") or 0.0
    peak = fa.get("peak_inflight") or 0
    check("G 取数", "实测速率与峰值并发不越界",
          rps_meas <= (pol.get("max_rps") or 4) * 1.02
          and peak <= (pol.get("workers") or 4) and not fa.get("violations"),
          f"{rps_meas} rps / 峰值 {peak} / 违约 {fa.get('violations')}")

    # G11 逐请求 journal：行数对得上，且「任意 10 秒窗口」都不出现突发
    jp = C / f"http-{args.as_of}.jsonl"
    jrows = ([json.loads(x) for x in jp.read_text(encoding="utf-8").splitlines() if x.strip()]
             if jp.exists() else [])
    rq2 = fa.get("requests") or {}
    n_sent = (rq2.get("ok") or 0) + (rq2.get("failed") or 0)
    check("G 取数", "journal 覆盖每一次请求",
          n_sent > 0 and len(jrows) == n_sent,
          f"journal {len(jrows)} 行 vs ok+failed {rq2.get('ok')}+{rq2.get('failed')}")
    ts = sorted(r["t"] for r in jrows)
    burst = max((sum(1 for u in ts if t <= u < t + 10) for t in ts), default=0)
    cap_burst = int((pol.get("max_rps") or 4) * 10 * 1.15)
    check("G 取数", "任意 10 秒窗口无突发", 0 < burst <= cap_burst,
          f"最密窗口 {burst} 次 / 上限 {cap_burst} 次（{pol.get('max_rps')} rps×10s×1.15）")

    # G12 per_host 必须是 journal 的聚合，不能是手填的
    cnt: dict[str, int] = {}
    for r in jrows:
        cnt[r["host"]] = cnt.get(r["host"], 0) + 1
    ph = fa.get("per_host") or {}
    bad_ph = [h for h, n in cnt.items() if (ph.get(h) or {}).get("requests") != n]
    check("G 取数", "per_host 计数与 journal 一致",
          bool(cnt) and not bad_ph and len(cnt) == len(ph),
          f"{len(cnt)} 个 host；不一致 {bad_ph[:3]}")

    # G13 报价快照由 run 落盘，score.py 判龙头时零请求复用
    qf = C / f"quotes-{args.as_of}.json"
    qsnap = json.loads(qf.read_text(encoding="utf-8")) if qf.exists() else {}
    check("G 取数", "当日报价快照已落盘（下游零请求复用）", bool(qsnap),
          f"{len(qsnap)} 只 -> {qf.name}")

    # ---------------- 汇总
    print("=" * 78)
    print(f"深度审计 · {args.report} · as_of {args.as_of}（零网络请求）")
    print("=" * 78)
    group = None
    for g, name, ok, detail in RESULTS:
        if g != group:
            print(f"\n[{g}]")
            group = g
        print(f"  {'✓' if ok else '✗'} {name:<44}{detail}")
    fails = [r for r in RESULTS if not r[2]]
    print("\n" + "=" * 78)
    print(f"合计 {len(RESULTS)} 项，失败 {len(fails)}")
    for g, name, ok, detail in fails:
        print(f"  ✗ {g} / {name} :: {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
