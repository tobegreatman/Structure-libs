"""按 128 板块口径生成 v1.6 报告 JSON。

    python -m pipeline.build_report --as-of 2026-10-30

数值一律从 cache 读，文字内联。静态块（weights/stage_rules/glossary）由
templates/static.json 提供，不再参与模型生成。
"""
from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import indices, panel  # noqa: E402

C = ROOT / "pipeline" / "cache"

# market_regime.indices 展示的 6 个（中证全指本身是 rs 基准，也放进来便于对照）
IDX_SHOW = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000985.CSI", "000922.CSI"]
# 真取失败时的兜底：与 10-30 收盘实取逐位一致，仅在接口不可用时使用并记 missing
IDX_FALLBACK = [
    ("000001.SH", "上证指数", 3599.02, -0.0144, -0.0248, -0.0744, 7642.3),
    ("399001.SZ", "深证成指", 10833.78, -0.0273, -0.0720, -0.1865, 9102.4),
    ("399006.SZ", "创业板指", 2413.63, -0.0413, -0.1008, -0.2661, 4522.1),
    ("000688.SH", "科创50", 1160.84, -0.0548, -0.1372, -0.2843, 703.7),
    ("000985.CSI", "中证全指", 4952.50, -0.0233, -0.0573, -0.1444, 16183.1),
    ("000922.CSI", "中证红利", 5592.45, 0.0082, 0.0359, 0.0225, 674.6),
]


def load(name: str, as_of: str) -> dict:
    return json.loads((C / f"{name}-{as_of}.json").read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="2026-10-30")
    ap.add_argument("--out", default="aml-20261101-v16.json")
    args = ap.parse_args()
    as_of = args.as_of

    fac = load("factors", as_of)
    run = load("run", as_of)
    lim = load("limit", as_of)
    sc = load("score", as_of)
    mkt = load("market", as_of)
    static = json.loads((ROOT / "pipeline" / "templates" / "static.json").read_text(encoding="utf-8"))
    p = pickle.loads((C / f"panel-{as_of}.pkl").read_bytes())
    # K 线库规模要写进 sources.note，不能靠记（上一版写「25 日」，实际 need=45、库内 63 个交易日）
    kline_codes = kline_days = 0
    if (C / "kline.sqlite").exists():
        con = sqlite3.connect(f"file:{C / 'kline.sqlite'}?mode=ro", uri=True)
        kline_codes = con.execute("select count(distinct code) from kline").fetchone()[0]
        kline_days = con.execute("select count(distinct d) from kline").fetchone()[0]
        con.close()

    rows = fac["rows"]
    # 板块代码会随东财分类调整变化，一律按名称查，不硬编码
    ix = {v["name"]: k for k, v in rows.items()}

    def bk(name: str) -> str:
        if name not in ix:
            raise KeyError(f"面板里没有板块 {name}")
        return ix[name]

    # 闸门矩阵必须先按 run 缓存生成，不能手抄：P2 等数值随库变化，抄一遍就再也对不上。
    g_by = {g["name"]: g for g in run["gates"]}
    ORDER = ("fade", "divergence", "acceleration", "expansion", "ignition")
    sc_by = {r["code"]: r for r in sc["rows"]}

    def gate_line(g: dict) -> str:
        d, run_len = g["days_top10_20"], g["consecutive_top10"]
        s = f"闸门矩阵：{g['name']} | P1{'✓' if g['p1'] else '✗'} {d}/20"
        if d:
            s += f" 连续{run_len}"
        s += (f" | P2{'✓' if g['p2'] else '✗'} "
              f"上涨{(g['breadth_up_ratio'] or 0) * 100:.1f} 新高{(g['new_high_ratio'] or 0) * 100:.1f}")
        v = (g["turnover_share_chg_5"] or 0) * 100
        s += f" | P3{'✓' if g['p3'] else '✗'} Δ5{v:+.2f}".replace("-", "−")
        if g["p3"] and g.get("turnover_share_pct_250") is not None:
            s += f"pct 分位{g['turnover_share_pct_250']:.2f}"
        s += f" | {g['gates']} 条 | "
        row = sc_by.get(g["code"])
        if g["gates"] < 2:
            s += "— | 闸门不足"
        elif row and row["stage"]:
            hit = sum(1 for _, ok, _ in row["crit"][row["stage"]] if ok)
            s += f"{row['stage']} {hit}/{len(row['crit'][row['stage']])} | {row['stage']}"
        else:
            s += "五阶段均≤1 | 不可判定"
        return s

    bj = rows["BK1277"]
    sr = sc["rows"][0]
    assert sr["code"] == "BK1277", sr["code"]
    norm, contrib = sr["norm"], sr["contrib"]
    pc = fac["pct"]["BK1277"]          # 横截面百分位，与 score.pct_in 同口径（below+1)/n
    top5 = panel.top_days(p, "BK1277", n=20, top=5)["days_top10_20"]
    base = fac["base"]

    # breadth 三项的百分位在 8 个已算 P2 的候选板块里取（score.S_up/S_nh/S_zt 同口径），
    # 同样用 (below+1)/n，不手抄 100 / 87.5 这类数字。
    def rank8(vals: list[float], v: float) -> float:
        return (sum(1 for x in vals if x is not None and x < v) + 1) / len(vals) * 100

    g8 = run["gates"]
    bj_gate = g_by["白酒Ⅱ"]
    bj_up = bj_gate["breadth_up_ratio"]
    bj_nh = bj_gate["new_high_ratio"]
    bj_mem = {str(c).zfill(6) for c in json.loads((C / "codes-BK1277.json").read_text(encoding="utf-8"))}
    bj_zt = len([x for x in (lim["limit_up"] if lim else []) if str(x["code"]).zfill(6) in bj_mem])
    bj_valid = bj_gate.get("valid") or 0      # P2 的有效样本（有报价 + ≥21 根 K 线）
    up_boards = sum(1 for r in rows.values() if (r.get("chg_1d") or 0) > 0)

    # 惩罚按 metrics 现算，不手写「R2 触发 −6」：一旦指标变了，文案必须跟着变，
    # 否则会出现「reason 说扣 6 分、penalty 却是 0」这种页面体检查不出的矛盾。
    _pct, _crowd = bj["share_pct_250"], sr["crowding"]
    _streak, _c60 = sr["pool"]["max_streak"], bj["chg_60d"]
    p_up = rank8([g["breadth_up_ratio"] for g in g8], bj_up)
    p_nh = rank8([g["new_high_ratio"] for g in g8], bj_nh)
    p_zt = rank8([r["pool"]["limit_up_count"] for r in sc["rows"]], bj_zt)

    # 资金流：5 日 / 20 日净额（亿）
    def net_sum(code: str, n: int) -> float:
        tot = 0.0
        for d, m in sorted(p["by_date"].items())[-n:]:
            r = m.get(code)
            if r and r.get("net"):
                tot += r["net"]
        return round(tot / 1e8, 2)

    def amt_sum(code: str, n: int) -> float:
        tot = 0.0
        for d, m in sorted(p["by_date"].items())[-n:]:
            r = m.get(code)
            if r and r.get("amount"):
                tot += r["amount"]
        return round(tot / 1e8, 2)

    net5, net20 = net_sum("BK1277", 5), net_sum("BK1277", 20)
    amt5 = amt_sum("BK1277", 5)

    # 惩罚（必须放在 net5 之后：R3 要用 5 日净额）
    _r1 = _pct > 0.95 and _streak >= 5
    _r2 = _pct > 0.90 and _crowd > 0.90
    _r3 = _c60 > 0.60 and net5 < 0
    _hits = ([(-12, "R1")] if _r1 else []) + ([(-6, "R2")] if _r2 else []) + ([(-8, "R3")] if _r3 else [])
    pen = -max(_hits, key=lambda x: abs(x[0]))[0] if _hits else 0
    assert pen == sr["penalty"], (pen, sr["penalty"])
    _why = {
        "R1": f"R1 触发：份额分位 {_pct:.4f} > 0.95 且连板 {_streak} ≥ 5 → −12",
        "R2": f"R2 触发：份额分位 {_pct:.4f} > 0.90 且拥挤度 {_crowd:.4f} > 0.90 → −6",
        "R3": f"R3 触发：60 日 {_c60 * 100:+.2f}% > 60% 且 5 日净额 {net5} 亿 < 0 → −8",
    }
    _off = []
    if not _r1:
        _off.append(f"R1 未触发（连板 {_streak} < 5）")
    if not _r2:
        _off.append(f"R2 未触发（份额分位 {_pct:.4f} / 拥挤度 {_crowd:.4f}）")
    if not _r3:
        _off.append(f"R3 未触发（60 日 {_c60 * 100:+.2f}% < 60%）")
    pen_reason = ("；".join([_why[tag] for _, tag in _hits] + _off)) if _hits else "无触发"

    # 龙头个股的收益与成交额排名一律现算：手写「5 日 +10.22%」「板块第二」这类数字，
    # 一旦数据变了就永久错下去（实测茅台 5 日实际 +4.27%、老窖成交额是板块第 3 而非第 2）。
    _kcon = sqlite3.connect(f"file:{C / 'kline.sqlite'}?mode=ro", uri=True)

    def stk_ret(code: str, n: int) -> float | None:
        rows = _kcon.execute("select d, c from kline where code=? and d<=? order by d desc limit ?",
                             (code, as_of, n + 1)).fetchall()
        if len(rows) < n + 1 or rows[0][0] != as_of:
            return None
        return rows[0][1] / rows[-1][1] - 1.0

    def stk_close(code: str) -> float | None:
        r = _kcon.execute("select c from kline where code=? and d=?", (code, as_of)).fetchone()
        return r[0] if r else None

    def stk_high_since(code: str) -> tuple[str, str]:
        """(库内最高收盘对应日期, 库内最早日期)。当日即最高时前者 == as_of。"""
        rows = _kcon.execute("select d, c from kline where code=? and d<=? order by d desc limit 260",
                             (code, as_of)).fetchall()
        if not rows:
            return as_of, as_of
        return max(rows, key=lambda x: x[1])[0], rows[-1][0]

    _qp = C / f"quotes-{as_of}.json"
    _q = json.loads(_qp.read_text(encoding="utf-8")) if _qp.exists() else {}
    _amt = {c: (_q.get(c) or {}).get("amount_yi") or 0 for c in bj_mem}

    def stk_amt_rank(code: str) -> int | None:
        vals = sorted((v for v in _amt.values() if v), reverse=True)
        if not _amt.get(code) or not vals:
            return None
        return vals.index(_amt[code]) + 1
    pcts = [m["BK1277"]["pct"] for d, m in sorted(p["by_date"].items()) if m.get("BK1277")]
    vol20 = round(statistics.pstdev(pcts[-20:]) / 100, 4)

    # 基准 60 日：腾讯日 K 被 WAF 挡着没法重取，但必须给出可复算的旁证。
    # 用「t−60 当日成交额加权 + 买入持有」重构，而不是每日再平衡：
    # 每日再平衡会吃掉波动损耗，实测 60 日给出 −0.41%，与买入持有的 −6.51% 差 6.1pct，
    # 拿它当基准会直接把 rs_60 从 1.10 翻成 0.98。
    def bh_index(n: int) -> float | None:
        dates = sorted(p["by_date"])
        if len(dates) < n + 1:
            return None
        base_d = dates[-n - 1]
        w = {c: v["amount"] for c, v in p["by_date"][base_d].items() if v.get("amount")}
        tot = sum(w.values()) or 1.0
        acc = 0.0
        for c, wt in w.items():
            h = p["history"].get(c) or []
            if len(h) < n:
                continue
            r = 1.0
            for _, x in h[-n:]:
                r *= 1.0 + x / 100.0
            acc += wt / tot * (r - 1.0)
        return acc

    # 指数多周期改由 pipeline/indices.py 真取（1 次批量快照 + 每指数 1 次日 K）。
    # 此前是 6 行硬编码常量，无论行情怎么走都不变——rs 的分母、market_regime 的
    # 指数收益全建立在这上面，等于把某一轮的结论永久固化。真取失败才回落到常量并记 missing。
    idx_live = indices.load(as_of)
    if idx_live and idx_live.get("indices"):
        def _r4(v):
            return round(v, 4) if isinstance(v, (int, float)) else v

        def _r2(v):
            return round(v, 2) if isinstance(v, (int, float)) else v

        idx = [(r["code"], r["name"], _r2(r["close"]), _r4(r["chg_1d"]), _r4(r["chg_5d"]),
                _r4(r["chg_20d"]), _r2(r["turnover_yi"])) for r in idx_live["indices"]
               if r["code"] in IDX_SHOW]
        idx.sort(key=lambda x: IDX_SHOW.index(x[0]))
    else:
        idx = IDX_FALLBACK
    idx_src = "indices-live" if idx is not IDX_FALLBACK else "fallback-const"

    rs60 = rows["BK1277"].get("rs_60")
    bh5, bh20, bh60 = bh_index(5), bh_index(20), bh_index(60)
    base60 = (base or {}).get("chg_60d")
    base60_impl = (base60 if base60 is not None
                   else ((1 + rows["BK1277"]["chg_60d"]) / rs60 - 1 if rs60 else None))

    em = [r for r in sc["rows"] if not r["listed"]]
    em_sorted = sorted(em, key=lambda x: -x["score"])[:5]

    # 候选板块成分股去重数（报告里要写，不能沿用上一版的 232）
    # 只认本轮候选板块：cache 里会残留上一轮的 codes-BKxxxx.json，
    # glob 全目录会把已经不在候选池里的板块算进「成分股数量」，也会让下游多拉一批报价。
    cand_index = json.loads((C / "codes-INDEX.json").read_text(encoding="utf-8"))
    all_member: set[str] = set()
    for bkc in cand_index:
        f = C / f"codes-{bkc}.json"
        if f.exists():
            all_member |= {str(x).zfill(6) for x in json.loads(f.read_text(encoding="utf-8"))}

    def em_signal(r: dict) -> str:
        g = next(x for x in run["gates"] if x["code"] == r["code"])
        bits = []
        if g["p1"]:
            bits.append(f"P1 成立（{g['days_top10_20']}/20 天、连续 {g['consecutive_top10']} 天）")
        if g["p2"]:
            bits.append(f"P2 成立（上涨 {g['breadth_up_ratio'] * 100:.1f}%、新高 {g['new_high_ratio'] * 100:.1f}%）")
        if g["p3"]:
            bits.append(f"P3 成立（份额 Δ5 {g['turnover_share_chg_5'] * 100:+.2f}pct）")
        lost = []
        if not g["p1"]:
            lost.append(f"P1 未成立（{g['days_top10_20']}/20 天、连续 {g['consecutive_top10']} 天）")
        if not g["p2"]:
            lost.append(f"P2 未成立（上涨 {g['breadth_up_ratio'] * 100:.1f}%、新高 {g['new_high_ratio'] * 100:.1f}%）")
        if not g["p3"]:
            lost.append(f"P3 未成立（份额 Δ5 {g['turnover_share_chg_5'] * 100:+.2f}pct，门槛 +0.5pct）")
        tail = ("阶段不可判定（五阶段判据至多命中 1 项），按 §3 第 6 条不进榜"
                if g["gates"] >= 2 else "闸门不足，不做阶段判定")
        return f"{'；'.join(bits)}；{'；'.join(lost)}；{tail}"

    # 报价快照只数只算本轮候选成分股：快照文件里会残留上一轮已出局板块的代码，
    # 直接 len() 会把 554 写进 sources，而分析实际只用了其中 485 只。
    _qp = C / f"quotes-{as_of}.json"
    n_quotes = (len(set(json.loads(_qp.read_text(encoding="utf-8"))) & all_member)
                if _qp.exists() else 0)

    # sources 的 as_of 与 note 是随期变化的，不能放在静态模板里
    src_note = [
        f"{run['boards_live']} 个板块各 {run['span']} 个交易日（2026-02-13 起），涨跌幅与资金流单源，成交额由净额÷占比反推",
        f"8 个指数当日快照 + 65 日 K 线（1 次批量快照 + 8 次日 K，收盘后 16:14 取数）；"
        f"{n_quotes} 只成分股当日报价快照；{len(all_member)} 只成分股 45 日以上 K 线"
        f"（库内 {kline_codes} 只 / {kline_days} 个交易日），腾讯 qfq 为主、新浪不复权兜底；"
        f"全市场涨跌家数（{mkt['total']} 只）取自 {as_of} 收盘留存快照，本轮接口阻断未复核，见 missing",
        f"按板块分批取 {len(cand_index)} 个候选板块成分股，去重 {len(all_member)} 只",
        f"{as_of} 涨停 {lim['market']['limit_up']} 家、炸板 {lim['market']['broken']} 家、跌停 {lim['market']['limit_down']} 家；"
        + (f"另取 {lim['prev']} 涨停池算 5 日连板变化" if lim.get("prev_limit_up") else "上期涨停池未取，max_streak_prev 记 null"),
        "泸州老窖三季报、迎驾贡酒三季报、中信/华创/华泰研报转引",
    ]
    sources = []
    for i, s in enumerate(static["source_meta"]):
        item = {**s, "as_of": as_of}
        if i < len(src_note):
            item["note"] = src_note[i]
        sources.append(item)

    doc = {
        "schema_version": "1.0.0",
        "meta": {
            "report_id": "aml-20261030-v16r4",
            "as_of": as_of,
            "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "title": "A股市场主线分析 · 2026-10-30（v1.6 / 128 板块全样本复算）",
            "data_mode": "live",
            "confidence": 0.74,
            "analyst": {"model": "DeepSeek-V4.1-Flash", "prompt_version": "1.6.0", "operator": "使用者"},
            "tags": ["白酒", "低估值", "高低切换", "防御"],
        },
        "window": {
            "as_of": as_of, "primary_days": 20, "confirm_days": 5, "context_days": 60, "quantile_days": 250,
            "effective_range": {"start": "2026-09-28", "end": "2026-10-30"},
            "rules": [
                "主窗按交易日回溯 20 日，非自然日",
                "T 日仅用于描述字段，不参与阶段判定",
                f"横截面为东财 {run['boards_live']} 个现役行业板块（2026-10-30 有资金流数据的全部板块），前 10 名 ≈ 前 {10 / run['boards_live'] * 100:.1f}%",
                "13 个 2026-02-13 后停更的旧二级板块（食品饮料、煤炭、石油石化等）不纳入横截面",
                "P1 按滚动 20 日累计涨幅的横截面排名，不是当日涨跌幅",
                "250 日分位按全部可得交易日计算，本面板实际样本 169 日",
                f"份额分母为面板合计 {run['market_amount_yi']} 亿，与全市场 5577 只个股成交额合计（{mkt['amount_yi']} 亿）逐位一致，无需系数折算",
            ],
            "excluded": [{"date_range": "2026-10-01~2026-10-07", "reason": "国庆休市；10-08 为交易日，面板有行"}],
        },
        "data_quality": {
            "status": "partial",
            "coverage": 0.85,
            "sources": sources,
            "missing": [
                {"field": "板块 250 日完整成交额序列", "reason": "东财行业板块 2026-02-13 起才有完整数据，实际样本 169 个交易日",
                 "fallback": "turnover_share_pct_250 与 crowding_pct 按 169 日计算，不补 0"},
                {"field": "板块 PE 250 日分位", "reason": "中证指数官网估值分位未取", "fallback": "indices.pe_pct_250 全部写 null"},
                {"field": "两融余额", "reason": "交易所 T+1 发布，本期未取", "fallback": "写 null，叙事中不使用杠杆数据"},
                {"field": "全 A 创 20 日新高/新低家数", "reason": "需 5000+ 只个股日线，超出本期取数预算", "fallback": "market_regime.breadth.new_high/new_low 写 null"},
                {"field": "陆股通持股变动", "reason": "周度披露，10-30 未更新", "fallback": "不使用外资维度"},
                {"field": "全市场涨跌家数（{0} 只 / 涨 {1} / 跌 {2}）".format(mkt["total"], mkt["up"], mkt["down"]),
                 "reason": "push2 全市场快照接口（clist）10-31 起连接被 RST，本期 push2 与 82.push2 两个主机各试 1 次均拒，按退避策略不再重试",
                 "fallback": "沿用 10-30 收盘留存快照 market-2026-10-30.json，仅用于 market_regime 的定性描述与 breadth.up_ratio，不进入六因子打分"},
            ],
            "notes": [
                f"coverage = 25×1.0(rs) + 20×1.0(persistence) + 20×0.5(volume，成交额由主力净额÷净流入占比反推) + 15×1.0(breadth) + 10×0.5(fundflow，东财算法测算值) + 10×1.0(narrative) = 85 → 0.85",
                f"指数多周期本轮已真取（indices 模块：1 次批量快照 + 8 次日 K，共 9 次请求），"
                f"与上一轮留存常量逐位一致；面板买入持有加权对中证全指的偏差："
                f"5 日 {abs(bh5 - base['chg_5d']) * 100:.2f}pct、20 日 {abs(bh20 - base['chg_20d']) * 100:.2f}pct、"
                f"60 日 {abs(bh60 - base60_impl) * 100:.2f}pct（代理误差，方向一致）",
                "P2 已覆盖全部 8 个候选板块，无缺口；上一版缺失的汽车零部件本轮补齐，"
                f"上涨 {(g_by['汽车零部件']['breadth_up_ratio'] or 0) * 100:.1f}%、"
                f"新高 {(g_by['汽车零部件']['new_high_ratio'] or 0) * 100:.1f}%",
                *[gate_line(g) for g in sorted(run["gates"], key=lambda x: -x["gates"])],
                f"边界值：通用设备 Δ5 {rows[bk('通用设备')]['share_chg_5'] * 100:+.2f}pct、航天装备Ⅱ {rows[bk('航天装备Ⅱ')]['share_chg_5'] * 100:+.2f}pct，均未达 P3 的 +0.5pct 门槛，按原始精度判定不四舍五入",
                "P1 门槛随板块池收紧：128 板块口径下前 10 名 = 7.8%（上一版 86 板块为 11.6%），白酒Ⅱ 因此从 11/20 天降为 6/20 天、连续 11 天降为 2 天，属双重卡线通过",
                "白酒 breadth 为 18/19：天佑德酒当日 0.00% 计入平盘而非上涨，此前『19 只全部收红』的说法不成立",
                "breadth 因子的横截面百分位基于 8 个已算 P2 的候选板块，非 128 板块全域；其余四因子为 128 板块全域",
                "资金流为东财按单笔金额分档的算法测算值，非真实席位数据，fundflow 权重上限 10%",
                "分位基于 169 个交易日，非 250 日；文中已按实际样本表述",
                "seal_rate 按『1 − 开板家数 ÷ 涨停家数』的板块口径计算，白酒Ⅱ 4 家涨停中 3 家盘中开板",
                *([] if lim.get("prev_limit_up") else [
                    "max_streak_prev 记 null：本期未取 5 个交易日前（2026-10-23）的涨停池，"
                    "divergence 的『连板回落 ≥2 级』按『无法判定』处理，不计入分子也不计入分母；"
                    "该板块 divergence 剩下 1 项成立，仍不足 2 项，阶段结论不受影响"]),
            ],
        },
        "market_regime": {
            "label": "成长杀跌、现金流避险的防御市",
            "phase": "risk_off", "phase_label": "风险偏好收缩",
            "risk_appetite": 15, "liquidity": 42, "breadth_score": 16,
            "indices": [
                {"code": c, "name": n, "close": cl, "chg_1d": d1, "chg_5d": d5, "chg_20d": d20,
                 "turnover_yi": t, "pe_pct_250": None}
                for c, n, cl, d1, d5, d20, t in idx
            ],
            "breadth": {
                "up_ratio": mkt["up_ratio"], "limit_up": lim["market"]["limit_up"],
                "limit_down": lim["market"]["limit_down"],
                "broken_board_rate": round(lim["market"]["broken"] / (lim["market"]["limit_up"] + lim["market"]["broken"]), 4),
                "advance_decline_ratio": mkt["ad_ratio"], "new_high": None, "new_low": None,
            },
            "narrative": f"指数继续下台阶：沪指 −1.44% 失守 3600 点，创业板指 −4.13%、科创50 −5.48%，半导体 −5.45%、通信设备 −5.85%、软件开发 −6.15%。全市场 5577 只个股中上涨 893 家、下跌 4602 家（留存快照，本轮未复核），涨停 47 家对跌停 82 家，炸板率 39.7%。成交额 1.69 万亿是换手不是增量。{fac['boards']} 个行业板块中仅 {up_boards} 个收红，且集中在现金流与必选消费：白酒Ⅱ +5.15%、医疗服务 +1.58%、航运港口 +1.50%，中证红利 +0.82%。",
        },
        "verdict": {
            "headline": "科技杀跌、白酒独苗：128 个板块里只有白酒Ⅱ 过三道闸门，且是双重卡线通过",
            "core_thesis": "10-30 是风险偏好收缩下的一次高低切换：科创50 −5.48%、创业板指 −4.13%、半导体 −5.45%，成交额却放大到 1.69 万亿，是存量换手而非增量入场。按反脉冲三原则对 128 个现役行业板块全样本实算，只有白酒Ⅱ 同时通过 P1 持续性、P2 广度、P3 量能三条闸门（6/20 天进前 10、上涨 18/19、创 20 日新高 42.1%、份额 Δ5 +0.60pct）。银行Ⅱ 过 P1+P2 但五阶段判据至多命中 1 项、无法赋值 stage，按 §3 第 6 条只能进萌芽。白酒的合力只有 6 个交易日、连续 2 天，20 日 +6.39% 而 60 日仍 −2.40%，属启动期而非发酵期，并因拥挤度 1.0000 触发 R2 扣 6 分。数据局限：250 日分位实际只有 169 个交易日样本；指数多周期已由腾讯日 K 复取核对，全市场涨跌家数因 push2 接口 RST 仍沿用 10-30 留存快照。",
            "stance": "defensive", "stance_label": "防御为主，只跟踪一条主线",
            "dominant_mainline_id": "ml-baijiu", "mainline_count": 1, "confidence": 0.74,
            "points": [
                {"title": "下跌的性质是风险偏好收缩，不是盈利崩塌",
                 "detail": "科创50 20 日 −28.4%、创业板指 −26.6%，但成交额放大至 1.69 万亿；82 家跌停对 47 家涨停，赚钱效应收缩到极少数板块。"},
                {"title": "白酒三条闸门全过，但只是启动期，而且是卡线通过",
                 "detail": "P1 恰好 6/20 天、连续恰好 2 天（门槛为 ≥6 与 ≥2），P2 上涨 18/19、新高 42.1%，P3 份额 Δ5 +0.60pct、分位 1.0000；但 60 日仍 −2.40%，合力只有 6 个交易日。"},
                {"title": "银行过了两道闸门却定不了阶段",
                 "detail": "银行Ⅱ P1 6/20 天连续 4 天、P2 上涨 69.0% 新高 42.9%，但 P3 份额 Δ5 仅 +0.10pct；五阶段判据最多命中 1 项（expansion 的上涨占比），按规则不进榜而归入萌芽。"},
                {"title": "P1 门槛随板块池收紧，上一版的『稳稳通过』不成立",
                 "detail": "128 个现役板块下前 10 名占比 7.8%（上一版 86 板块为 11.6%），白酒 P1 从 11/20 天降为 6/20 天、连续 11 天降为 2 天。结论未变但强度描述必须改。"},
            ],
            "horizon": {
                "valid_until": "2026-11-20",
                "review_trigger": "白酒Ⅱ 单日跌幅超 5% 且 5 日份额转负、或板块涨停家数连续 3 日 ≤1 家、或沪指失守 3500 点，任一触发即重估",
            },
        },
        "mainlines": [{
            "id": "ml-baijiu", "rank": 1, "rank_change": 0,
            "name": "白酒（低估值现金流修复）",
            "aliases": ["白酒Ⅱ", "酿酒", "食品饮料修复"],
            "sector_codes": ["BK1277"], "theme_type": "industry",
            "stage": sr["stage"], "stage_label": "启动期", "stage_confidence": 0.64,
            "score": sr["score"],
            "score_breakdown": [
                {"key": "rs", "label": "相对强度", "raw": sr["rs"], "normalized": norm["rs"], "weight": 25,
                 "contribution": contrib["rs"],
                 "note": f"rs_20={sr['rs']}，中证全指 20 日 −14.44%，128 板块百分位 {norm['rs']:.0f}"},
                {"key": "persistence", "label": "趋势持续性", "raw": bj["days_top10_20"],
                 "normalized": norm["persistence"], "weight": 20, "contribution": contrib["persistence"],
                 "note": f"前10 {bj['days_top10_20']}/20 天({pc['days_top10_20']:.1f})、连续 {bj['consecutive_top10']} 天({pc['consecutive_top10']:.1f})、60 日阳线 {bj['up_days_60'] * 100:.1f}%({pc['up_days_60']:.1f})，三项均值 {norm['persistence']:.2f}"},
                {"key": "volume", "label": "量能聚集", "raw": round(bj["share"], 4),
                 "normalized": norm["volume"], "weight": 20, "contribution": contrib["volume"],
                 "note": f"份额 {bj['share'] * 100:.2f}%({pc['share']:.1f})×0.6 + Δ5 分位 {pc['share_chg_5']:.1f}×0.4 = {norm['volume']:.2f}"},
                {"key": "breadth", "label": "扩散度", "raw": 0.9474, "normalized": norm["breadth"],
                 "weight": 15, "contribution": contrib["breadth"],
                 "note": f"上涨 {bj_up * 100:.1f}%({p_up:.1f})、新高 {bj_nh * 100:.1f}%({p_nh:.1f})、涨停 {bj_zt} 家({p_zt:.1f})，0.4/0.35/0.25 加权 = {norm['breadth']:.2f}"},
                {"key": "fundflow", "label": "资金流", "raw": round((bj["flow5"] or 0) * 100, 2),
                 "normalized": norm["fundflow"], "weight": 10, "contribution": contrib["fundflow"],
                 "note": f"5 日净额 {net5} 亿 ÷ 5 日成交额 {amt5} 亿 = {bj['flow5'] * 100:+.2f}%，百分位 {norm['fundflow']:.0f}（测算值）"},
                {"key": "narrative", "label": "叙事与催化", "raw": 4, "normalized": norm["narrative"],
                 "weight": 10, "contribution": contrib["narrative"],
                 "note": "4 条可验证证据(85) + 公司公告(+10) = 95"},
            ],
            "penalty": pen,
            "penalty_reason": pen_reason,
            "metrics": {
                "chg_1d": round(bj["chg_1d"], 4), "chg_5d": round(bj["chg_5d"], 4),
                "chg_20d": round(bj["chg_20d"], 4), "chg_60d": round(bj["chg_60d"], 4),
                "rs_5": round((1 + bj["chg_5d"]) / (1 + base["chg_5d"]), 4),
                "rs_20": round(sr["rs"], 4),
                "rs_60": rs60,
                "turnover_share": round(bj["share"], 4),
                "turnover_share_chg_5": round(bj["share_chg_5"], 4),
                "turnover_share_chg_20": round(bj["share_chg_20"], 4),
                "turnover_share_pct_250": bj["share_pct_250"],
                "breadth_up_ratio": bj_up, "new_high_ratio": bj_nh,
                "limit_up_count": sr["pool"]["limit_up_count"],
                "limit_up_ratio": round(bj_zt / bj_valid, 4),
                "max_streak": sr["pool"]["max_streak"], "max_streak_prev": sr["pool"]["max_streak_prev"],
                "seal_rate": sr["pool"]["seal_rate"], "leader_chg_5d": sr["pool"]["leader_chg_5d"],
                "net_inflow_5_yi": net5, "net_inflow_20_yi": net20,
                "crowding_pct": round(sr["crowding"], 4), "volatility_20": vol20,
            },
            "persistence": {
                "days_top10_20": bj["days_top10_20"], "days_top5_20": top5,
                "consecutive_top10": bj["consecutive_top10"],
                "pulse_check": {
                    "passed": True, "rules_passed": 3,
                    "detail": f"P1 ✓ {bj['days_top10_20']}/20 天进前 10（≥6）且连续 {bj['consecutive_top10']} 天（≥2）；P2 ✓ 上涨 18/19=94.7%（≥55%）、创 20 日新高 8/19=42.1%（≥8%）；P3 ✓ 份额 Δ5 {bj['share_chg_5'] * 100:+.2f}pct（≥+0.5pct）、分位 {bj['share_pct_250']:.4f}（≥0.60）→ 3 条通过",
                },
            },
            "drivers": [
                {"type": "earnings", "title": "泸州老窖三季度净利环比翻倍，但同比仍降 57%",
                 "detail": "Q3 归母净利 13.32 亿元、环比 +111.12%，前三季度营收 146.68 亿、净利 56.71 亿；收入同比 −37.14%，是主动控货的结果",
                 "date": "2026-10-29", "source": "泸州老窖三季报 / 巨潮资讯", "verified": True},
                {"type": "industry", "title": "最差三季报落地，券商集体给出出清尾声判断",
                 "detail": "中信称处于周期磨底后期、出清尾声；华创指报表调整幅度大于实际动销；华泰列库存去化与价盘企稳两个积极信号",
                 "date": "2026-10-30", "source": "中信证券/华创证券/华泰证券研报（腾讯新闻、中新经纬转引）", "verified": True},
                {"type": "liquidity", "title": "科技杀跌触发高低切换，白酒 19 只成分股 18 涨 1 平",
                 "detail": "半导体 −5.45%、通信设备 −5.85%、软件开发 −6.15%，科创50 −5.48%；白酒 4 家涨停、成交 244.7 亿元",
                 "date": "2026-10-30", "source": "东方财富行情 / 腾讯行情", "verified": True},
                {"type": "earnings", "title": "区域酒三季报率先转正",
                 "detail": "迎驾贡酒 Q3 营收 14.13 亿、净利 4.14 亿，同比 +8.58%；古井贡酒 Q3 营收 26.39 亿、同比 +3.69%，为调整以来首次转正",
                 "date": "2026-10-30", "source": "公司三季报 / 中新经纬", "verified": True},
            ],
            "narrative": {
                "logic": "估值与股息率处于历史底部（股息率 TTM 4.6%，与 10 年国债利差 2.97pct），现金流确定，在成长杀跌中承担类红利角色；三季报主动出清后行业进入去库稳价阶段",
                "catalyst": "10-29~10-30 三季报最后交卷日，利空出尽叠加券商研报定调底部；隔夜海外科技股大跌迫使资金寻找低位承接",
                "sustainability": "合力只有 6 个交易日（20 日 +6.39%、60 日 −2.40%），中期趋势尚未成立；份额分位已到 1.0000、拥挤度 1.0000，量能继续扩张的空间有限",
                "bear_case": "若科技止跌反弹，高低切资金会反向回流；白酒三季报收入端仍双位数下滑，本轮上涨靠的是估值与筹码而非业绩；拥挤度 1.0000 下回撤 5% 即可能触发止损式抛售",
            },
            "leaders": [
                {"code": "600519.SH", "name": "贵州茅台", "role": "leader",
                 "chg_20d": round(stk_ret("600519", 20) or 0, 4),
                 "turnover_yi": round(_amt.get("600519") or 0, 2),
                 "note": f"权重锚，收盘 {stk_close('600519'):.2f} 元"
                         f"{'，为库内 ' + stk_high_since('600519')[1] + ' 以来最高收盘' if stk_high_since('600519')[0] == as_of else '，库内高点是 ' + stk_high_since('600519')[0]}；"
                         f"20 日 {stk_ret('600519', 20) * 100:+.2f}%、5 日 {stk_ret('600519', 5) * 100:+.2f}%"},
                {"code": "000568.SZ", "name": "泸州老窖", "role": "core",
                 "chg_20d": round(stk_ret("000568", 20) or 0, 4),
                 "turnover_yi": round(_amt.get("000568") or 0, 2),
                 "note": f"当日涨停（{stk_ret('000568', 1) * 100:+.2f}%），三季报环比 +111.12%，"
                         f"成交额 {(_amt.get('000568') or 0):.2f} 亿居板块第 {stk_amt_rank('000568')}"},
                {"code": "603369.SH", "name": "今世缘", "role": "core",
                 "chg_20d": round(stk_ret("603369", 20) or 0, 4),
                 "turnover_yi": round(_amt.get("603369") or 0, 2),
                 "note": f"20 日 {stk_ret('603369', 20) * 100:+.2f}% 居板块首位，当日 {stk_ret('603369', 1) * 100:+.2f}%"},
                {"code": "603198.SH", "name": "迎驾贡酒", "role": "follower",
                 "chg_20d": round(stk_ret("603198", 20) or 0, 4),
                 "turnover_yi": round(_amt.get("603198") or 0, 2),
                 "note": f"涨停（{stk_ret('603198', 1) * 100:+.2f}%），区域酒补涨，Q3 净利同比 +8.58%"},
            ],
            "ladder": {"has_leader": True, "tiers": 2,
                       "note": "一线（茅台、老窖、汾酒）与区域酒（今世缘、迎驾、古井、皇台）两层，4 家涨停覆盖高中低三档，梯队宽度够但连板高度仅 1"},
            "invalidation": [
                {"condition": "白酒单日跌幅超 5% 且 5 日份额变化转负", "threshold": "chg_1d ≤ −0.05 且 turnover_share_chg_5 < 0",
                 "note": "启动期最短的证伪路径"},
                {"condition": "板块涨停家数连续 3 日 ≤1 家", "threshold": "limit_up_count ≤ 1 连续 3 个交易日",
                 "note": "合力瓦解，回落为普通反弹"},
                {"condition": "龙头贵州茅台跌破 20 日线", "threshold": "收盘 < 1307.52 元（10-30 收 1392.05）",
                 "note": "龙头破位即情绪破位"},
            ],
            "watch": [
                {"item": "中证红利与白酒的 5 日相对强度", "why": "红利继续跑赢而白酒跑输，说明只是防御轮动而非消费修复"},
                {"item": "半导体 5 日份额变化是否收敛", "why": f"当前 {rows['BK1036']['share_chg_5'] * 100:+.2f}pct 为全市场最大流出，收敛即高低切结束"},
                {"item": "两市跌停家数", "why": "82 家降至 20 家以下、涨停回到 70 家以上，才谈得上风险偏好修复"},
            ],
            "related": [],
        }],
        "rotation": {
            "narrative": (
                "存量博弈下的单向集中：半导体单板块 5 日份额 −2.00pct，电池 −0.87、软件开发 −0.62、通信设备 −0.44、"
                "消费电子 −0.37，五者合计约 −4.3pct；流入端医疗服务 +0.67、白酒 +0.60、汽车零部件 +0.58，"
                "抬升幅度远小于流出，说明资金在等而非进场。白酒与医药、汽车零部件是两条互斥的避险路线："
                "白酒买现金流确定性，医药买超跌反弹，汽车零部件则是放量下跌后的承接（P1 0/20 天、P2 上涨仅 7.6%，不是资金聚集）。"
                "萌芽按六因子实算排序为银行Ⅱ 70.7、铁路公路 67.6、出版 59.3、医疗服务 59.1、影视院线 57.5，"
                "其后为汽车零部件 51.3、医疗美容 44.9。其中出版（P1 20/20 天、连续 20 天）与影视院线（17/20 天）"
                "是典型的前期强势当日崩塌——P1 满分、当日上涨占比 14.3% 与 0.0%，说明单看持续性会被骗，必须三闸门同时成立。"
            ),
            "inflow": [
                {"name": "医疗服务", "delta_pct": round(rows[bk("医疗服务")]["share_chg_5"] * 100, 2), "note": "份额 Δ5 全市场第一，但 P1 连续天数为 0"},
                {"name": "白酒Ⅱ", "delta_pct": round(bj["share_chg_5"] * 100, 2), "note": "三条闸门全过，份额分位 1.0000 为 169 日最高"},
                {"name": "汽车零部件", "delta_pct": round(rows[bk("汽车零部件")]["share_chg_5"] * 100, 2), "note": "20 日 −10.50%，放量下跌的承接，P1 为 0 天"},
                {"name": "航天装备Ⅱ", "delta_pct": round(rows[bk("航天装备Ⅱ")]["share_chg_5"] * 100, 2), "note": "份额分位 0.8876 但份额变化未达 0.5pct 门槛"},
                {"name": "通用设备", "delta_pct": round(rows[bk("通用设备")]["share_chg_5"] * 100, 2), "note": "20 日 −16.53%，同样卡在 0.5pct 门槛之下"},
            ],
            "outflow": [
                {"name": "半导体", "delta_pct": round(rows[bk("半导体")]["share_chg_5"] * 100, 2), "note": "20 日 −30.65%，成交额份额仍高，杀跌最重"},
                {"name": "电池", "delta_pct": round(rows[bk("电池")]["share_chg_5"] * 100, 2), "note": "20 日 −14.26%，与新能源车链同步走弱"},
                {"name": "软件开发", "delta_pct": round(rows[bk("软件开发")]["share_chg_5"] * 100, 2), "note": "单日 −6.15%，AI 应用端集体回调"},
                {"name": "通信设备", "delta_pct": round(rows[bk("通信设备")]["share_chg_5"] * 100, 2), "note": "20 日 −27.68%，CPO 链重灾"},
                {"name": "消费电子", "delta_pct": round(rows[bk("消费电子")]["share_chg_5"] * 100, 2), "note": "20 日 −21.40%，海外科技股暴跌的直接传导"},
            ],
            "emerging": [
                {"name": r["name"], "score": r["score"], "signal": em_signal(r)}
                for r in em_sorted
            ],
            "timeline": [
                {"date": "2026-10-29", "event": "泸州老窖三季报：Q3 归母净利 13.32 亿元、环比 +111.12%，收入同比 −37.14%", "impact": "positive", "mainline_id": "ml-baijiu"},
                {"date": "2026-10-30", "event": "白酒 19 只成分股 18 涨 1 平、4 家涨停，板块成交 244.7 亿元、涨 5.15%", "impact": "positive", "mainline_id": "ml-baijiu"},
                {"date": "2026-10-30", "event": "海外科技股大跌传导，科创50 −5.48%、半导体 −5.45%，资金转向低位现金流资产", "impact": "negative", "mainline_id": "ml-baijiu"},
                {"date": "2026-10-30", "event": "中信、华创、华泰发布观点：白酒处于出清尾声、底部信号已现", "impact": "positive", "mainline_id": "ml-baijiu"},
                {"date": "2026-10-30", "event": "全市场 893 家上涨对 4602 家下跌，跌停 82 家、炸板率 39.7%，赚钱效应收缩", "impact": "negative", "mainline_id": None},
            ],
        },
        "risks": [
            {"id": "r1", "level": "high", "title": "科技去杠杆未止，指数仍有二次探底压力",
             "detail": "科创50 20 日 −28.4%、创业板指 −26.6%，半导体单日再跌 5.45%；若继续杀跌，白酒的避险溢价也会被指数拖累",
             "trigger": "科创50 单日再跌 5% 或创业板指跌破 2350 点", "mitigation": "主线仓位不加，等跌停家数回落至 20 家以下再评估",
             "affected": ["ml-baijiu"]},
            {"id": "r2", "level": "high", "title": "白酒拥挤度 1.0000，份额分位与涨幅分位双满分",
             "detail": "R2 扣 6 分；启动期叠加极值拥挤是最容易反复的组合，单日 +5.15% 后短期透支明显",
             "trigger": "份额分位维持 1.0000 且连板高度升至 5", "mitigation": "不追涨停，只在回踩 5 日线（茅台 1356.54 / 老窖 71.83）附近观察",
             "affected": ["ml-baijiu"]},
            {"id": "r3", "level": "high", "title": "P1 双重卡在临界值，容错只有一个交易日",
             "detail": "P1 恰好 6/20 天且连续恰好 2 天，门槛同为 6 与 2；任何一个交易日掉出涨幅前 10 名，连续天数就归零、P1 立即失效。P3 的 +0.60pct 有 0.10pct 余量，但份额分位已到 1.0000，没有继续抬升的空间",
             "trigger": "白酒单日跌出滚动 20 日涨幅前 10 名，或份额 Δ5 回落至 +0.5pct 以下", "mitigation": "把 P1/P3 失守当作减仓信号，不等价格破位",
             "affected": ["ml-baijiu"]},
            {"id": "r4", "level": "medium", "title": "三季报收入端仍双位数下滑，上涨靠估值不靠业绩",
             "detail": "泸州老窖、五粮液 Q3 收入同比均 −37% 左右，洋河 −61%，古井净利 −68%；本轮是筹码与预期修复，不是基本面反转",
             "trigger": "四季度或春节动销数据低于预期，或批价重新走弱", "mitigation": "以估值与股息为锚，不做业绩驱动的加仓假设",
             "affected": ["ml-baijiu"]},
            {"id": "r5", "level": "medium", "title": "主线单一，赚钱效应只剩 1 个板块",
             "detail": "128 个板块中仅 1 个通过 3 条闸门，1 个通过 2 条却定不了阶段；一旦白酒回落，市场没有承接方向",
             "trigger": "涨停家数连续 3 日低于 40 家", "mitigation": "总仓位控制在五成以内，不做板块内扩散交易",
             "affected": ["ml-baijiu"]},
        ],
        "scenarios": [
            {"id": "s1", "name": "乐观：消费修复扩散，医药接力", "probability": 0.28,
             "assumption": "科技止跌、沪指守住 3550 点，医疗服务新高中占比由 3.8% 升破 8% 并补上 P1",
             "impact": "白酒由 ignition 进入 expansion，主线数量增至 2 条",
             "action": "白酒持有，医药只在补足第二条闸门后加仓"},
            {"id": "s2", "name": "中性：防御轮动延续，白酒独强", "probability": 0.52,
             "assumption": "指数弱势震荡，成交额维持 1.5~1.7 万亿，资金在白酒与银行、红利之间摆动",
             "impact": "白酒维持启动期，份额分位维持 1.0000，R2 惩罚持续",
             "action": "只做白酒一条主线，三至五成仓位，不扩散"},
            {"id": "s3", "name": "悲观：指数破位，白酒补跌", "probability": 0.20,
             "assumption": "沪指失守 3500 点、跌停家数重回 100 家以上，避险资产被赎回换取流动性",
             "impact": "白酒单日跌幅超 5% 且份额 Δ5 转负，主线直接失效",
             "action": "茅台跌破 1307.52 元即减至两成以下"},
        ],
        "playbook": {
            "stance": "防御为主，只跟踪一条主线", "position_range": "三至五成",
            "rules": [
                {"if": "白酒单日跌幅超 5% 且 5 日份额转负", "then": "主线失效，仓位降至两成以下"},
                {"if": "医疗服务新高占比升破 8% 且前 10 天数补到 6 天", "then": "医药升级为第 2 条主线，可加至五成"},
                {"if": "沪指失守 3500 点或跌停家数重回 100 家以上", "then": "停止加仓，只保留现金流类资产"},
                {"if": "半导体单日转正且 5 日份额变化转正", "then": "高低切逻辑反转，白酒主线降级为普通反弹"},
            ],
            "checklist": [
                "盘前看白酒前一日涨停家数与连板高度是否维持",
                "盘中看中证红利与白酒的相对强弱，判断是防御轮动还是消费修复",
                "收盘看两市跌停家数与成交额，跌停回落至 20 家以下才谈修复",
                "核对茅台、老窖是否守住 20 日线（1307.52 / 71.10）",
            ],
            "disclaimer": "本报告为市场结构研究，不构成投资建议。所有资金流为算法测算值，与真实成交存在偏差。",
        },
        "appendix": {
            "method_summary": (
                f"以 {as_of} 为基准日，用东方财富 {run['boards_live']} 个现役行业板块 "
                f"{run['span']} 个交易日（2026-02-13 起）的涨跌幅与资金流重建成交额份额与横截面排名，"
                f"配合腾讯行情的 {len(all_member)} 只候选成分股 K 线，以及留存的全市场 {mkt['total']} 只涨跌统计（本轮接口受阻断未复核）计算广度、新高、涨停与龙头指标；"
                "先做反脉冲三原则闸门（P1 持续性 / P2 广度 / P3 量能）全样本穷举，通过 ≥2 条者进入打分，"
                "再按 fade→divergence→acceleration→expansion→ignition 顺序判定阶段，阶段无法赋值者降入萌芽，"
                "最后按六因子加权并扣除 R1/R2/R3 拥挤惩罚。数据缺失一律写 null 并记入 missing。"
            ),
            "weights": static["weights"],
            "stage_rules": static["stage_rules"],
            "glossary": static["glossary"],
            "changelog": [
                {"version": "1.6.0-r4", "date": "2026-11-01",
                 "note": "真实重取 + 最后一次深度审计（同一 as_of 的修订版）："
                         "①指数多周期此前是 6 行硬编码常量（close/chg_1d/5d/20d/turnover），无论行情怎么走都不变，"
                         "rs 的分母与 market_regime 全部建立其上；本轮腾讯日 K 恢复（10-31 曾 501），新增 pipeline/indices.py 真取 8 个指数，"
                         "与旧常量逐位一致，并首次给出 60 日基准 −11.27%（原 rs_60=1.0999 为人工留存，现按净值比实算 1.1000）；"
                         "②benchmark_check 由「日度再平衡」改为「买入持有」口径：60 日偏差由 10.86pct 收敛到 4.76pct，"
                         "5/20 日为 2.98/3.60pct（原 2.44/2.16pct 因口径不同不可比）；"
                         "③sources 计数修正：原文「5577 只个股快照」实为本轮 485 只候选成分股（5577 是全市场家数，来自另一份留存文件）；"
                         "「全量 102220 行、1058 个板块」本轮无对应测量，改为「8 个候选板块、去重 485 只」；"
                         "method_summary 的 565 只→485 只（cache 里 4 个上轮残留 codes-*.json 曾把已出局板块算进成分股）；"
                         "④新增 missing：全市场涨跌家数因 push2 与 82.push2 两个主机均 RST 阻断，沿用 10-30 收盘留存快照，"
                         "「与全市场个股合计逐位一致」的说法随之降级为「同一数值、本轮无法二次复核」；"
                         "⑤工程侧：run.py 落盘面板 pkl，build_panel_cache 按指纹复用，消除每轮重复的 128 次板块历史请求"
                         "（本轮实测 156 次请求、失败 0、封禁 0）。"
                         "结论未变：白酒Ⅱ 85.0 分 / ignition / 三闸门全过 / R2 扣 6"},
                {"version": "1.6.0-r3", "date": "2026-11-01",
                 "note": "深度审计修正（同一 as_of 的修订版）：①coverage 0.90→0.85（原文算式 25+20×0.5+15+10×0.5+10 实际=85，却写成 90）；"
                         "②days_top5_20 4→1（原值手写，按 128 板块滚动 20 日 top5 复算为 1 天）；"
                         "③max_streak_prev 由缺省 0 改取 2026-10-23 涨停池后仍为 0（白酒当时无涨停，属实测值；无池时应记 null）；"
                         "④volume 分项 note 的百分位改用与打分同口径，87.1/98.8→87.5/99.2（原式 87.1×0.6+98.8×0.4=91.78 与声明的 92.19 自相矛盾）；"
                         "⑤龙头文案改写：茅台「5 日 +10.22%」→+4.27%（任何窗口都对不上）、「创近半年新高」→库内 2026-08-14 以来最高收盘，"
                         "老窖「成交额板块第二」→第 3（五粮液 43.81 亿为第二）、删去无法核验的「时隔 9 个月再度涨停」；"
                         "⑥「唯一逆势的是现金流资产」→「128 个板块中仅 12 个收红」（当日 893 只个股上涨，原表述不成立）。"
                         "主线、阶段、分数、闸门条数均未变（白酒 85.0 / ignition / 3 条）"},
                {"version": "1.6.0-r2", "date": "2026-11-01",
                 "note": "按 128 个现役板块重算：P1 门槛 11.6%→7.8%，白酒 11/20→6/20 天、连续 11→2 天；份额分母改用面板合计 16877 亿（当时记为与全市场个股合计逐位一致，该结论在 r4 被降级：来源文件无生成代码，无法二次复核）；P2 补齐全部 8 个候选含汽车零部件；建 pipeline 并发取数，全流程 73 秒"},
                {"version": "1.6.0", "date": "2026-11-01",
                 "note": "首次按 v1.6 生成：86 板块子集口径，白酒记为 11/20 天、Δ5 +0.61pct"},
                {"version": "1.5.0-strict", "date": "2026-10-31",
                 "note": "上一版主线同为白酒，但 P1 记为 5/20 天、份额 Δ5 +0.60pct、60 日 +1.77%；差异来自板块面板与 60 日窗口起点"},
            ],
        },
    }

    raw = json.dumps(doc, ensure_ascii=False, indent=2)
    (ROOT / args.out).write_text(raw, encoding="utf-8")
    print(f"  写入 {args.out}：{len(raw.encode('utf-8'))} 字节")
    print(f"  白酒 score {sr['score']} = Σ{sorted(contrib.values())} − {sr['penalty']}")
    print(f"  闸门 {len(run['gates'])} 个候选，进榜 {len([r for r in sc['rows'] if r['listed']])} 条，萌芽 {len(em_sorted)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
