"""催化剂日历：校验、强度/定价状态整理、兑现风险警示。

催化剂分级（event-catalyst 方法论五问：真实性/新颖度/可理解性/可外溢性/可跟踪性）。
已定价的利好是兑现风险：direction='+' 且 priced_in='已定价' → 输出 warning。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

STRENGTH_WEIGHT = {"强": 4.0, "中": 2.5, "弱": 1.0, "噪音": 0.0}
VALID_TYPES = {"earnings", "policy", "industry", "company", "overseas", "unverified_rumor"}


def _parse_event_date(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None  # 窗口描述（如 2026Q4）


def build_calendar(catalysts: List[Dict[str, Any]], analysis_date: date) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """返回 (日历列表, 评分用统计)。日历条目仅含 schema 字段；来源信息进 evidence_log。"""
    calendar: List[Dict[str, Any]] = []
    stats = {"within_12m": [], "within_30d": []}
    for c in catalysts:
        if c.get("type") not in VALID_TYPES:
            raise ValueError(f"催化剂 {c.get('event')} 的 type 非法: {c.get('type')}")
        if c.get("strength") not in STRENGTH_WEIGHT:
            raise ValueError(f"催化剂 {c.get('event')} 的 strength 非法: {c.get('strength')}")
        d = _parse_event_date(str(c.get("date", "")))
        if d is not None and d < analysis_date:
            raise ValueError(
                f"催化剂 {c.get('event')} 日期 {d} 早于分析时点 {analysis_date}：已发生事件不能作为前瞻催化"
            )
        warning = None
        if c["direction"] == "+" and c.get("priced_in") == "已定价":
            warning = "利好已定价：存在'买预期卖兑现'风险，事件落地反而可能是卖点"
        if c.get("type") == "unverified_rumor" and c["strength"] in ("强", "中"):
            warning = (warning or "") + " 未证实传闻按噪音处理，不得作为评分依据"
        item = {
            "date": str(c["date"]),
            "event": c["event"],
            "type": c["type"],
            "direction": c["direction"],
            "strength": c["strength"],
            "expected_impact": c.get("expected_impact", "—"),
            "priced_in": c.get("priced_in", "未定价"),
            "verify_method": c.get("verify_method", "—"),
            "warning": warning,
        }
        calendar.append(item)
        # 未证实传闻在评分统计中强制按噪音处理（L4 语义不得进入评分路径）
        stat_c = dict(c)
        if stat_c.get("type") == "unverified_rumor":
            stat_c["strength"] = "噪音"
        if d is not None:
            days_ahead = (d - analysis_date).days
            if days_ahead <= 365:
                stats["within_12m"].append(stat_c)
            if days_ahead <= 30:
                stats["within_30d"].append(stat_c)
        else:
            stats["within_12m"].append(stat_c)  # 窗口描述默认计入 12 个月内
    return calendar, stats


def density_score(stats: Dict[str, Any]) -> float:
    """中线催化密度：min(10, Σ 强度权重)，仅计 6-12 月窗口事件。"""
    total = sum(STRENGTH_WEIGHT.get(c.get("strength", "噪音"), 0.0) for c in stats["within_12m"])
    return round(min(10.0, total), 2)


def next_event_score(stats: Dict[str, Any]) -> float:
    """短线催化强度：30 日内最近事件 强=10/中=7.5/弱=2.5/无=0；
    利好已定价 ×0.4（兑现风险惩罚）。"""
    events = stats["within_30d"]
    if not events:
        return 0.0
    events_sorted = sorted(events, key=lambda c: str(c.get("date", "9999")))
    c = events_sorted[0]
    base = {"强": 10.0, "中": 7.5, "弱": 2.5, "噪音": 0.0}.get(c.get("strength", "噪音"), 0.0)
    if c.get("direction") == "+" and c.get("priced_in") == "已定价":
        base *= 0.4
    return round(base, 2)
