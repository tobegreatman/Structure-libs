"""数据质量（DQ）：完整性 / 及时性 / 来源等级 三要素（设计文档 §6）。

DQ 总分 = 0.5×completeness + 0.3×timeliness + 0.2×tier_score
强制人工复核：overall < 0.5，或必填字段缺失。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .datadict import FIELD_SPECS, REQUIRED_FIELDS
from .pit import FieldRef

TIER_SCORE = {"L1": 1.0, "L2": 0.85, "L3": 0.6, "L4": 0.0}


def compute_dq(
    registry: Dict[str, FieldRef],
    analysis_date: date,
    field_notes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    registered = [s.field_id for s in FIELD_SPECS]
    present = [f for f in registered if f in registry and registry[f].value is not None]
    completeness = len(present) / len(registered)

    # 及时性：按字段更新周期判断，取最差档
    timeliness = 1.0
    stale: List[str] = []
    for spec in FIELD_SPECS:
        ref = registry.get(spec.field_id)
        if ref is None or ref.value is None:
            continue
        age = ref.age_days(analysis_date)
        ratio = age / spec.update_days if spec.update_days else 0
        if ratio > 2:
            stale.append(f"{spec.field_id}(超期{ratio:.1f}倍)")
            timeliness = min(timeliness, 0.6)
        elif ratio > 1.5:
            stale.append(f"{spec.field_id}(超期{ratio:.1f}倍)")
            timeliness = min(timeliness, 0.8)

    # 来源等级：入模字段加权
    used = [registry[f] for f in present]
    tier_dist: Dict[str, int] = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
    for ref in used:
        tier_dist[ref.source_tier] += 1
    if used:
        tier_score = sum(TIER_SCORE[r.source_tier] for r in used) / len(used)
        min_tier = min((r.source_tier for r in used), key=lambda t: ["L1", "L2", "L3", "L4"].index(t))
    else:
        tier_score, min_tier = 0.0, "L3"

    overall = 0.5 * completeness + 0.3 * timeliness + 0.2 * tier_score

    # 缺口原因：默认一句"未提供"，但装配层若知道为什么空（只披露前五合计 /
    # 正文没这项 / 自动提取失败），必须用它的说法覆盖——否则三种情况在报告里
    # 长得一模一样，读者无法判断该去翻年报还是该重跑一次。
    notes_map = field_notes or {}
    gaps = [
        {"field": s.field_id,
         "reason": notes_map.get(s.field_id)
         or ("必填字段缺失" if s.field_id in REQUIRED_FIELDS else "未提供（该指标按缺失处理，不补零）")}
        for s in FIELD_SPECS if s.field_id not in registry or registry[s.field_id].value is None
    ]
    missing_required = [f for f in REQUIRED_FIELDS if f not in registry or registry[f].value is None]
    forced = overall < 0.5 or bool(missing_required)

    notes: List[str] = []
    if missing_required:
        notes.append("必填字段缺失：" + "、".join(missing_required) + " → 强制人工复核")
    if stale:
        notes.append("数据超期项：" + "；".join(stale))
    if tier_dist["L4"] > 0:
        notes.append("存在 L4 来源字段：仅作线索，不进入评分")
    # 年报口径字段（客户集中度）：明确它不是"最新季报的数"，滞后属正常，不是失效。
    annual_used = []
    for spec in FIELD_SPECS:
        if not spec.annual:
            continue
        ref = registry.get(spec.field_id)
        if ref is not None and ref.value is not None:
            annual_used.append(f"{spec.field_id}（{ref.as_of} 年报口径）")
    if annual_used:
        notes.append("只在年报披露的字段，数据期为最近一期年报、不随季报前移："
                     + "、".join(annual_used))

    return {
        "overall": round(overall, 3),
        "completeness": round(completeness, 3),
        "timeliness": round(timeliness, 3),
        "source_tier_score": round(tier_score, 3),
        "min_tier_used": min_tier,
        "tier_distribution": tier_dist,
        "gaps": gaps,
        "forced_manual_review": forced,
        "notes": notes,
    }
