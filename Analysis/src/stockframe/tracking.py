"""跟踪清单生成（设计文档 §13）。

来源四类：
- falsification: 结论证伪条件 → 监控项
- risk_trigger: 高/中风险 trigger → 监控项
- catalyst_verify: 催化剂 verify_method → 到期核验项
- disclosure: 下期财务披露核验（对齐 PIT：下期 available_at 之后才评价）

direction 语义：指标朝该方向越过阈值即"风险/证伪方向"，不是行情涨跌方向。
"""

from __future__ import annotations

from typing import Any, Dict, List


def _clip(text: str, limit: int = 60) -> str:
    """按语义边界截断（逗号/分号/箭头/括号），避免表格里出现半句话。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("；", "，", "→", "（"):
        idx = head.rfind(sep)
        if idx >= limit * 0.5:
            return text[:idx] + "…"
    return head + "…"


def bad_direction(threshold: str, default: str = "above") -> str:
    """由阈值写法推断"证伪/触发方向"：确认门槛为 ≥ 时，证伪方向是 below。"""
    has_lt = "<" in threshold
    has_gt = ">" in threshold
    if has_lt and not has_gt:
        return "below"
    if has_gt and not has_lt:
        return "above"
    return default


def build_tracking(
    falsification: List[Dict[str, Any]],
    maintain: List[Dict[str, Any]],
    risks: List[Dict[str, Any]],
    calendar: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    n = 1

    for f in falsification:
        # 证伪条件里的门槛是"确认门槛"（如 ≥50%），证伪方向取其反面
        direction = "below" if any(t in f["threshold"] for t in ("≥", ">=")) else (
            "above" if any(t in f["threshold"] for t in ("≤", "<=")) else bad_direction(f["threshold"], "below")
        )
        items.append({
            "id": f"T{n}", "item": f"证伪条件 {f['id']}：{_clip(f['condition'])}",
            "metric": f["metric"], "threshold": f["threshold"],
            "direction": direction, "cadence": "weekly",
            "action_on_trigger": "立即重估结论（非降档处理）",
            "source": f["source"], "origin": "falsification",
        })
        n += 1

    for r in risks:
        if r["level"] in ("高", "中"):
            items.append({
                "id": f"T{n}", "item": f"风险 {r['id']}（{r['name']}，{r['level']}风险）触发监控",
                "metric": r["trigger"]["metric"], "threshold": r["trigger"]["threshold"],
                "direction": bad_direction(r["trigger"]["threshold"], "above"),
                "cadence": "weekly",
                "action_on_trigger": "评估是否触发证伪/情景切换",
                "source": r.get("evidence", "—"), "origin": "risk_trigger",
            })
            n += 1

    for c in calendar:
        if c["strength"] in ("强", "中") and c["type"] != "unverified_rumor":
            items.append({
                "id": f"T{n}", "item": f"催化剂核验：{_clip(c['event'], 40)}（{c['date']}）",
                "metric": c["verify_method"] if c["verify_method"] != "—" else c["event"],
                "threshold": "兑现/不及预期 二分核验",
                "direction": "above", "cadence": "event",
                "action_on_trigger": "按情景分支更新（超预期→乐观权重上调；不及预期→悲观权重上调）",
                "source": "事件公告", "origin": "catalyst_verify",
            })
            n += 1

    items.append({
        "id": f"T{n}", "item": "下期定期报告披露核验（营收/净利增速、ROE、OCF/净利、营运资本差）",
        "metric": "financial.rev_growth / np_growth / roe_ttm / ocf_np_ratio / wc_growth_gap",
        "threshold": "对照 maintain_conditions C1/C2 阈值", "direction": "below",
        "cadence": "quarterly", "action_on_trigger": "不达阈值→重估财务维度并更新分桶",
        "source": "公司定期报告（披露日之后评价，PIT 对齐）", "origin": "disclosure",
    })
    return items
