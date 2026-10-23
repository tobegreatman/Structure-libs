"""决策映射：硬门槛 → 强制人工复核 → 分数分桶（设计文档 §11）。

判定顺序（先于分数，避免高分掩盖红旗）：
1. 硬红旗 → 回避（ST/审计非标/监管立案/重大财务红旗）
2. 强制人工复核（DQ 不足 / 必填缺失 / 特殊状态）
3. **证据覆盖不足 → 人工复核**（有维度整块没有可用指标时，综合分只是"局部平均"）
4. 信号冲突 → 人工复核（仅当 composite_mid >= 7.0、本应入池，但某维度 <= 3.0）
5. 分数分桶：中线综合 >=7.0 入池(且无维度<4.0)；>=5.0 观察；<3.5 回避；其余 观察(偏谨慎)
6. 短线综合分仅输出参考桶。

第 3 条是防"黑箱结论"的关键：综合分只在有数据的维度上重归一化，
若不设闸门，"只填了财务+估值"的标的也能拿到 6-7 分并被判为观察/入池，
读者会误以为宏观、行业、公司三层已经被评估过。

禁止：任何桶附带价位、仓位、买卖时点建议（引擎层不产生此类字段）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

HARD_FLAGS = ("st", "audit_qualified", "regulatory_investigation", "major_fraud_red_flag")
SPECIAL_STATUS = ("suspended", "restructuring", "new_listing_under_1y")

COVERAGE_MIN = 0.60  # 中线权重覆盖率下限：低于此值不出正式分桶

CONFLICT_MAIN_THRESHOLD = 7.0  # 主信号：中线综合分达入池线才需要"高分 vs 弱维度"的人工权衡

CONFLICT_RULES = [
    # (冲突维度, 冲突阈值, 说明) —— 仅在 composite_mid >= 7.0 时评估；
    # 综合分本身未达入池线时已是"观察"，无需再升级为人工复核。
    ("valuation", 3.0, "高分但估值维度极弱：优质公司的'贵'需人工权衡"),
    ("risk", 3.0, "高分但风险维度极弱：基本面与风险画像冲突"),
    ("financial", 3.0, "高分但财务质量差：疑似'价值陷阱'或盈利质量争议"),
]


def _bucket_from_score(score: Optional[float], dim_min: Optional[float]) -> str:
    if score is None:
        return "人工复核"
    if score >= 7.0:
        return "入池" if (dim_min is None or dim_min >= 4.0) else "观察"
    if score >= 5.0:
        return "观察"
    if score < 3.5:
        return "回避"
    return "观察"


def decide(
    meta: Dict[str, Any],
    dq: Dict[str, Any],
    dim_scores: Dict[str, Optional[float]],
    composite_mid: Optional[float],
    composite_short: Optional[float],
    coverage: Optional[float] = None,
    missing_dims: Optional[List[str]] = None,
) -> Dict[str, Any]:
    reasons: List[str] = []
    bucket: Optional[str] = None
    rule_id: Optional[str] = None

    # 1) 硬红旗 → 回避
    hard = [f for f in HARD_FLAGS if meta.get("flags", {}).get(f)]
    red_flag_count = meta.get("red_flag_count", 0)
    if hard:
        bucket, rule_id = "回避", "R1_HARD_FLAG"
        reasons.append("硬红旗触发：" + "、".join(hard))
    elif red_flag_count >= 2:
        bucket, rule_id = "回避", "R2_RED_FLAGS"
        reasons.append(f"重大财务红旗 {red_flag_count} 条（>=2 触发回避候选）")

    # 2) 强制人工复核
    if bucket is None:
        if dq["forced_manual_review"]:
            bucket, rule_id = "人工复核", "R3_DQ_FORCED"
            note_txt = "；".join(dq.get("notes") or []) or "DQ 不足"
            reasons.append("数据质量强制人工复核：" + note_txt)
        special = [s for s in SPECIAL_STATUS if meta.get("flags", {}).get(s)]
        if bucket is None and special:
            bucket, rule_id = "人工复核", "R4_SPECIAL_STATUS"
            reasons.append("特殊状态：" + "、".join(special))
        if bucket is None and coverage is not None and coverage < COVERAGE_MIN:
            bucket, rule_id = "人工复核", "R7_COVERAGE"
            gaps = "、".join(missing_dims or [])
            reasons.append(
                f"研究判断层证据不足：宏观/行业/公司的指标级覆盖 {coverage:.0%} < 下限 {COVERAGE_MIN:.0%}"
                + (f"，其中完全缺失：{gaps}" if gaps else "")
                + "。财务与行情齐备不等于研究已完成，此处不给正式分桶"
            )
        if bucket is None and composite_mid is not None and composite_mid >= CONFLICT_MAIN_THRESHOLD:
            for dim_id, dim_thr, desc in CONFLICT_RULES:
                conflict_val = dim_scores.get(dim_id)
                if conflict_val is not None and conflict_val <= dim_thr:
                    bucket, rule_id = "人工复核", "R5_SIGNAL_CONFLICT"
                    reasons.append(
                        f"信号冲突：{desc}（composite_mid={composite_mid} vs {dim_id}={conflict_val}）"
                    )
                    break

    # 3) 分数分桶
    if bucket is None:
        evaluated = [v for v in dim_scores.values() if v is not None]
        dim_min = min(evaluated) if evaluated else None
        bucket, rule_id = _bucket_from_score(composite_mid, dim_min), "R6_SCORE_BAND"
        if bucket == "观察" and composite_mid is not None and composite_mid >= 3.5:
            reasons.append(f"中线综合分 {composite_mid} 处于 5.0-7.0/3.5-5.0 观察带：核心逻辑或验证条件待补")
        elif bucket == "入池":
            reasons.append(f"中线综合分 {composite_mid} >= 7.0 且各维度 >= 4.0")
        elif bucket == "回避":
            reasons.append(f"中线综合分 {composite_mid} < 3.5")
        elif bucket == "观察" and dim_min is not None and dim_min < 4.0:
            reasons.append(f"存在维度低于 4.0（最弱 {dim_min}），入池条件不满足")

    bucket_short = _bucket_from_score(composite_short, None)
    return {
        "bucket_mid": bucket,
        "bucket_short": bucket_short,
        "rule_id": rule_id,
        "reasons": reasons,
    }
