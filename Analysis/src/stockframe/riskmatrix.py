"""风险矩阵：5×5 概率×影响，级别判定与网格统计。

每条风险: {id, name, category, probability 1-5, impact 1-5, trigger{metric,threshold}, mitigation, evidence}
风险值 = P×I ∈ [1,25]；级别：>=16 高 / >=9 中 / <9 低。
矩阵同时喂给：风险维度评分（risk.matrix_score 派生字段）、跟踪清单（高/中风险 trigger）。
"""

from __future__ import annotations

from typing import Any, Dict, List

CATEGORIES = {
    "demand", "price", "customer_concentration", "capacity", "policy",
    "competition", "governance", "finance", "macro", "other",
}


def risk_level(score: int) -> str:
    if score >= 16:
        return "高"
    if score >= 9:
        return "中"
    return "低"


def build_matrix(risks: List[Dict[str, Any]]) -> Dict[str, Any]:
    out_risks: List[Dict[str, Any]] = []
    for r in risks:
        p, i = int(r["probability"]), int(r["impact"])
        if not (1 <= p <= 5 and 1 <= i <= 5):
            raise ValueError(f"风险 {r.get('id')} 的 probability/impact 必须在 1-5")
        if r.get("category") not in CATEGORIES:
            raise ValueError(f"风险 {r.get('id')} 的 category 非法: {r.get('category')}")
        score = p * i
        out_risks.append({
            "id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "probability": p,
            "impact": i,
            "score": score,
            "level": risk_level(score),
            "trigger": {"metric": r["trigger"]["metric"], "threshold": r["trigger"]["threshold"]},
            "mitigation": r.get("mitigation", "—"),
            "evidence": r.get("evidence", "—"),
        })
    p_levels = [1, 2, 3, 4, 5]
    i_levels = [1, 2, 3, 4, 5]
    counts = [[0] * 5 for _ in range(5)]
    for r in out_risks:
        counts[p_levels.index(r["probability"])][i_levels.index(r["impact"])] += 1
    top = sorted(out_risks, key=lambda x: (-x["score"], x["id"]))[:5]
    return {
        "scale": "5x5_P_x_I",
        "risks": out_risks,
        "grid": {"p_levels": p_levels, "i_levels": i_levels, "counts": counts},
        "top_risk_ids": [r["id"] for r in top],
    }


def matrix_score(risks: List[Dict[str, Any]]) -> float:
    """风险维度评分公式（specs.py 中声明 rationale）：
    10 × (1 − Σ clamp(P×I−8, 0, 17) / 20)，P×I>8 的风险逐条扣减，封顶 0。"""
    penalty = sum(min(max(int(r["probability"]) * int(r["impact"]) - 8, 0), 17) for r in risks)
    score = 10.0 * (1.0 - min(penalty, 20) / 20.0)
    return round(max(0.0, min(10.0, score)), 2)
