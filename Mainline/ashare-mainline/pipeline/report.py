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
    """动态侧只写 as_of / note 时，按位置补上 name/category/fields/reliability。"""
    if len(dyn_sources) != len(tpl_sources):
        raise ValueError(
            f"data_quality.sources 数量 {len(dyn_sources)} 与模板 {len(tpl_sources)} 不等；"
            "若新增数据源须同时更新 templates/static.json"
        )
    out = []
    for tpl, dyn in zip(tpl_sources, dyn_sources):
        if "name" in dyn:
            out.append(dyn)
        else:
            out.append({**tpl, **dyn})
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
        for s in srcs:
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
