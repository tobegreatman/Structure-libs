"""三时点模型与 PIT(时点纪律)守卫。

反未来函数核心：
- 每个数据字段携带 period/as_of/available_at/source/source_tier；
- 分析时点 analysis_date 下只允许 available_at <= analysis_date 的数据入模；
- 违规触发 LookAheadError（含字段名与两个时点），直接终止，不静默降级。

三时点：
- as_of        数据取值基准时点（报告期/统计截止/交易日）
- available_at 数据实际可得时点（披露日/发布日/交易日）
- period_end   数据所属报告期（仅财务类，用于展示）
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

TIERS = ("L1", "L2", "L3", "L4")


class LookAheadError(Exception):
    """未来函数：数据可得时点晚于分析时点，或字段结构非法。"""


class ProvenanceError(Exception):
    """来源等级违规：L4（媒体/传闻）不得进入评分路径。"""


class FieldRef:
    """一个已通过 PIT 校验的数据字段引用。"""

    __slots__ = ("field_id", "value", "as_of", "available_at", "source", "source_tier",
                 "period_end", "proxy")

    def __init__(
        self,
        field_id: str,
        value: Any,
        as_of: str,
        available_at: str,
        source: str,
        source_tier: str,
        period_end: Optional[str] = None,
        proxy: bool = False,
    ) -> None:
        self.field_id = field_id
        self.value = value
        self.as_of = as_of
        self.available_at = available_at
        self.source = source
        self.source_tier = source_tier
        self.period_end = period_end
        # proxy=True 表示这是「按固定公式推断的判断档位」，不是研究结论。
        # 它仍受 PIT 与来源等级约束，但必须在报告里与人工判断区分开。
        self.proxy = proxy

    def age_days(self, analysis_date: date) -> int:
        return (analysis_date - date.fromisoformat(self.available_at)).days


def _parse_date(field_id: str, key: str, raw: Any) -> date:
    if not isinstance(raw, str):
        raise LookAheadError(f"字段 {field_id} 的 {key} 必须是 YYYY-MM-DD 字符串，当前: {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise LookAheadError(f"字段 {field_id} 的 {key} 日期非法: {raw!r}") from exc


def check_field(field_id: str, raw: Dict[str, Any], analysis_date: date) -> FieldRef:
    """校验单个字段结构 + PIT 合法性，返回 FieldRef。"""
    if not isinstance(raw, dict) or "value" not in raw:
        raise LookAheadError(f"字段 {field_id} 缺少 value 或结构非法: {raw!r}")
    as_of = _parse_date(field_id, "as_of", raw.get("as_of"))
    available_at = _parse_date(field_id, "available_at", raw.get("available_at"))
    source = raw.get("source")
    source_tier = raw.get("source_tier")
    if not isinstance(source, str) or not source:
        raise LookAheadError(f"字段 {field_id} 缺少 source")
    if source_tier not in TIERS:
        raise LookAheadError(f"字段 {field_id} 的 source_tier 非法: {source_tier!r}（应为 L1-L4）")
    if source_tier == "L4":
        raise ProvenanceError(
            f"来源等级违规：字段 {field_id} 为 L4（媒体/传闻），不得进入评分路径。"
            "请替换为 L1-L3 来源；若仅为线索，应移入 catalysts 并标注 unverified_rumor。"
        )
    if available_at > analysis_date:
        raise LookAheadError(
            f"未来函数：字段 {field_id} 的 available_at={available_at} 晚于 "
            f"analysis_date={analysis_date}（as_of={as_of}）。"
            "请检查是否误用了尚未披露的数据。"
        )
    if as_of > analysis_date:
        raise LookAheadError(
            f"未来函数：字段 {field_id} 的 as_of={as_of} 晚于 analysis_date={analysis_date}"
        )
    period_end = raw.get("period_end")
    if period_end is not None:
        _parse_date(field_id, "period_end", period_end)
    return FieldRef(
        field_id=field_id,
        value=raw["value"],
        as_of=raw["as_of"],
        available_at=raw["available_at"],
        source=source,
        source_tier=source_tier,
        period_end=period_end,
        proxy=bool(raw.get("proxy")),
    )


def validate_all_fields(data: Dict[str, Any], analysis_date: date) -> Dict[str, FieldRef]:
    """遍历 data.<group>.<field>，全部过 PIT 校验，返回 field_id -> FieldRef 注册表。"""
    registry: Dict[str, FieldRef] = {}
    if not isinstance(data, dict):
        raise LookAheadError("输入 data 必须是按组组织的对象")
    for group, fields in data.items():
        if not isinstance(fields, dict):
            raise LookAheadError(f"data.{group} 必须是字段对象")
        for key, raw in fields.items():
            field_id = f"{group}.{key}"
            registry[field_id] = check_field(field_id, raw, analysis_date)
    return registry
