"""报告模板化。

把每期完全一致的块（weights / stage_rules / glossary / method_summary / 数据源元信息 /
window.rules）抽到 templates/static.json，生成端只产出会变的部分，组装时注入。

两个收益：
1. 生成端少写约 4.3KB（占完整报告 15%），且不可能写错权重或阶段规则；
2. 组装时自动跑一致性校验（权重合计 100、score = Σ贡献 − 惩罚、六因子齐全），
   把「人写报告容易漂移」的地方变成机器断言。

用法：
    from pipeline import report
    full = report.assemble(dynamic)        # dynamic 只含变化部分
    report.assert_consistent(full)         # 组装后自检
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TPL = Path(__file__).with_name("templates") / "static.json"

TOP_KEYS = [
    "schema_version", "meta", "window", "data_quality", "market_regime", "verdict",
    "mainlines", "rotation", "risks", "scenarios", "playbook", "appendix",
]
FACTORS = ["rs", "persistence", "volume", "breadth", "fundflow", "narrative"]


def load(path: str | Path | None = None) -> dict:
    with open(path or TPL, encoding="utf-8") as f:
        return json.load(f)


def _merge_sources(tpl_sources: list[dict], dyn_sources: list[dict]) -> list[dict]:
    """动态侧只写 as_of / note 时，按位置补上 name/category/fields/reliability。

    模板外的**追加源**（如两融、央行公告）必须由动态侧整条给出，位置对齐到模板末尾之后。
    早期这里是「数量必须相等」，新增数据源就得同时改模板——但模板回答不了「本期取没取到」，
    强行对齐只会让新增源无处安放。现在放宽为「不少于模板条数」，多出来的按原样保留。
    """
    if len(dyn_sources) < len(tpl_sources):
        raise ValueError(
            f"data_quality.sources 数量 {len(dyn_sources)} 少于模板 {len(tpl_sources)} 条；"
            "要么补齐到模板条数，要么在 templates/static.json 里删掉不再使用的源"
        )
    out = []
    for i, dyn in enumerate(dyn_sources):
        if "name" in dyn or i >= len(tpl_sources):
            out.append(dyn)
        else:
            out.append({**tpl_sources[i], **dyn})
    return out


def assemble(dynamic: dict, *, static: dict | None = None) -> dict:
    """把静态块注入动态报告，返回完整 12 键对象。"""
    tpl = static or load()
    dyn = json.loads(json.dumps(dynamic, ensure_ascii=False))  # 深拷贝

    extra = set(dyn) - set(TOP_KEYS)
    if extra:
        raise ValueError(f"出现契约外的顶层键：{sorted(extra)}")

    win = dyn.setdefault("window", {})
    win.setdefault("rules", tpl["window_rules"])

    dq = dyn.setdefault("data_quality", {})
    if dq.get("sources"):
        dq["sources"] = _merge_sources(tpl["source_meta"], dq["sources"])

    ap = dyn.setdefault("appendix", {})
    ap["method_summary"] = tpl["method_summary"]
    ap["weights"] = tpl["weights"]
    ap["stage_rules"] = tpl["stage_rules"]
    ap["glossary"] = tpl["glossary"]
    ap.setdefault("changelog", [])

    return {k: dyn[k] for k in TOP_KEYS if k in dyn}


def strip(full: dict, *, static: dict | None = None) -> dict:
    """反向操作：剥掉静态块，得到生成端只需负责的那部分。用于体积对比与自测。"""
    tpl = static or load()
    dyn = json.loads(json.dumps(full, ensure_ascii=False))
    ap = dyn.get("appendix", {})
    for k in ("method_summary", "weights", "stage_rules", "glossary"):
        ap.pop(k, None)
    if dyn.get("window", {}).get("rules") == tpl["window_rules"]:
        dyn["window"].pop("rules", None)
    srcs = dyn.get("data_quality", {}).get("sources")
    if srcs:
        kept = []
        for i, s in enumerate(srcs):
            if i >= len(tpl["source_meta"]):
                kept.append(s)                     # 模板外的追加源，动态侧必须整条带回来
            else:
                kept.append({k: s[k] for k in ("as_of", "note") if k in s})
        dyn["data_quality"]["sources"] = kept
    return dyn


def assert_consistent(report: dict) -> list[str]:
    """组装后自检，返回 warning 列表（不抛异常，便于逐条修）。"""
    warn: list[str] = []
    missing = [k for k in TOP_KEYS if k not in report]
    if missing:
        warn.append(f"缺顶层键：{missing}")

    for ml in report.get("mainlines", []):
        mid = ml.get("id", "?")
        sb = ml.get("score_breakdown") or []
        keys = [x.get("key") for x in sb]
        if keys != FACTORS:
            warn.append(f"{mid}: score_breakdown 因子应为 {FACTORS}，实际 {keys}")
        if len(sb) == 6:
            wsum = sum(x.get("weight", 0) for x in sb)
            if wsum != 100:
                warn.append(f"{mid}: 权重合计 {wsum} != 100")
            total = sum(x.get("contribution", 0) for x in sb)
            pen = ml.get("penalty", 0) or 0
            calc = round(total - pen, 2)
            if abs(calc - (ml.get("score") or 0)) > 0.05:
                warn.append(f"{mid}: Σ贡献 {total:.2f} − 惩罚 {pen} = {calc} 与 score {ml.get('score')} 不符")

        m = ml.get("metrics", {})
        if sb and sb[0].get("raw") is not None and m.get("rs_20") is not None:
            if abs(float(sb[0]["raw"]) - float(m["rs_20"])) > 1e-9:
                warn.append(f"{mid}: rs.raw {sb[0]['raw']} != metrics.rs_20 {m['rs_20']}")

        pulse = (ml.get("persistence") or {}).get("pulse_check") or {}
        if pulse:
            rp = pulse.get("rules_passed", 0)
            if rp < 2 and ml in report.get("mainlines", []):
                warn.append(f"{mid}: 闸门只过 {rp} 条，按 §3 不应进 mainlines")
            if pulse.get("passed") != (rp >= 2):
                warn.append(f"{mid}: pulse_check.passed 与 rules_passed={rp} 不一致")

        name = ml.get("name", mid)
        p1 = (ml.get("persistence") or {}).get("days_top10_20", 0)
        p2u = m.get("breadth_up_ratio")
        p3c = m.get("turnover_share_chg_5")
        gate = sum([
            p1 >= 6 and (ml["persistence"].get("consecutive_top10") or 0) >= 2,
            (p2u or 0) >= 0.55 and (m.get("new_high_ratio") or 0) >= 0.08,
            (p3c or 0) >= 0.005 and (m.get("turnover_share_pct_250") or 0) >= 0.60,
        ])
        if gate != rp:
            warn.append(f"{name}: 由 metrics 重算闸门 {gate} 条，与声明的 {rp} 条不符")

    warn.extend(_check_leverage(report))

    probs = [s.get("probability") for s in report.get("scenarios", [])]
    if probs and abs(sum(probs) - 1.0) > 0.001:
        warn.append(f"情景概率合计 {sum(probs):.3f} != 1.0")

    v = report.get("verdict", {})
    if v.get("mainline_count") != len(report.get("mainlines", [])):
        warn.append(f"verdict.mainline_count {v.get('mainline_count')} != mainlines 条数")
    ids = [m.get("id") for m in report.get("mainlines", [])]
    if v.get("dominant_mainline_id") and v["dominant_mainline_id"] not in ids:
        warn.append(f"verdict.dominant_mainline_id {v['dominant_mainline_id']} 不在 mainlines 中")

    return warn


TREND_DOWN = -0.03
TREND_UP = 0.03
LEVERAGE_WORDS = ("两融", "融资余额", "融资盘", "融券余额")


def _check_leverage(report: dict) -> list[str]:
    """杠杆块的四条硬约束。

    两融的坑不是「有没有数」，而是「数有一天滞后却当成今天用」。所以这里不检查数值大小，
    只检查四件事：滞后与 as_of 自洽、趋势与 20 日变动自洽、有无数据与 missing 自洽、
    没数据时叙事有没有偷偷引用它。
    """
    warn: list[str] = []
    regime = report.get("market_regime") or {}
    lev = regime.get("leverage")
    dq = report.get("data_quality") or {}
    hit_missing = [m for m in (dq.get("missing") or []) if "两融" in (m.get("field") or "")]
    hit_source = [s for s in (dq.get("sources") or []) if "两融" in (s.get("name") or "")]

    if not isinstance(lev, dict):
        if not hit_missing:
            warn.append("market_regime.leverage 缺失且 data_quality.missing 未记录两融")
        return warn

    has_value = lev.get("balance_yi") is not None
    rep_as_of = (report.get("meta") or {}).get("as_of")

    if has_value:
        if not hit_source:
            warn.append("leverage 有数值但 data_quality.sources 无两融来源条目")
        lag = lev.get("lag_trading_days")
        lev_as_of = lev.get("as_of")
        if lag is None:
            warn.append("leverage 有数值但 lag_trading_days 为 null（无法判断这份数有多旧）")
        else:
            if lag < 0:
                warn.append(f"leverage.lag_trading_days={lag} 为负：数据日晚于报告基准日")
            elif lag > 3:
                warn.append(f"leverage.lag_trading_days={lag} > 3，按 §5 时滞规则应整块写 null")
            if lag == 0 and rep_as_of and lev_as_of != rep_as_of:
                warn.append(f"leverage lag=0 但 as_of {lev_as_of} != 报告 as_of {rep_as_of}")
            if lag >= 1 and rep_as_of and lev_as_of == rep_as_of:
                warn.append(f"leverage lag={lag} 但 as_of 与报告同为 {rep_as_of}")
            if lag >= 1 and not hit_missing:
                warn.append("leverage 存在交易日滞后，但 missing 未写明滞后原因与口径")

        c20 = lev.get("chg_20d")
        if c20 is not None and lev.get("trend"):
            want = ("deleveraging" if c20 <= TREND_DOWN
                    else "releveraging" if c20 >= TREND_UP else "stable")
            if lev["trend"] != want:
                warn.append(f"leverage.trend={lev['trend']} 与 chg_20d={c20} 的机械映射（{want}）不符")
        if c20 is not None and lev.get("trend_label") is None:
            warn.append("leverage.trend 有值但 trend_label 为 null")
    else:
        if not hit_missing:
            warn.append("leverage 无数据但 data_quality.missing 未记录两融")
        texts = []
        v = report.get("verdict") or {}
        texts += [v.get("headline") or "", v.get("core_thesis") or ""]
        texts += [p.get("detail") or "" for p in (v.get("points") or [])]
        texts += [r.get("detail") or "" for r in (report.get("risks") or [])]
        for ml in report.get("mainlines") or []:
            nar = ml.get("narrative") or {}
            texts += [nar.get("logic") or "", nar.get("bear_case") or "",
                      nar.get("sustainability") or ""]
            texts += [w.get("why") or "" for w in (ml.get("watch") or [])]
        used = [t for t in LEVERAGE_WORDS if any(t in s for s in texts)]
        if used:
            warn.append(f"leverage 为 null，但叙事引用了 {used}——无数据不得谈杠杆")
    return warn


def dumps(report: dict) -> str:
    """按 §9 排版：一个顶层键一行，对象内联。"""
    parts = []
    for k in TOP_KEYS:
        if k in report:
            parts.append(f'  "{k}": ' + json.dumps(report[k], ensure_ascii=False, separators=(",", ":")))
    return "{\n" + ",\n".join(parts) + "\n}"


def sizes(full: dict, static: dict | None = None) -> dict:
    tpl = static or load()
    dyn = strip(full, static=tpl)
    b = lambda o: len(json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode())  # noqa: E731
    return {
        "full": b(full),
        "dynamic": b(dyn),
        "static_injected": b(full) - b(dyn),
        "saving_pct": round((1 - b(dyn) / b(full)) * 100, 1),
    }
