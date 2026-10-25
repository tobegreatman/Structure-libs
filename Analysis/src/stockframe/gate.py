"""估值前门：先判断"该不该算"，再谈"算多少"。

两种拒答，与"降级"不同——降级仍然会印出一个数，读者只会记住数：

1. 行业不适用（bank/insurance/reit/financial/…）：FCFF-DCF 的营收×EBIT率路径
   与净债务口径对金融企业不成立。旧行为是照样算：平安银行算出 24.58–33.28 元、
   安全边际 +95.7%，唯一拦住它的是覆盖闸门而不是行业判断。
2. 状态不适用（亏损/未盈利）：EPS≤0 时 PE 无意义；EBIT 率为负时 DCF 显性期为负，
   区间可以输出负股价（-17.6 元）。

前门只决定"方法是否成立"，不决定分数、不决定分桶。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .datadict import company_profile


@dataclass(frozen=True)
class ValuationGate:
    company_type: str
    type_label: str
    dcf_applicable: bool
    code: Optional[str]          # None / DCF_NOT_APPLICABLE / DCF_LOSS_MAKING
    reason: str
    metric: str
    metric_supported: bool       # 本框架是否真有该口径的区间算法（目前只有 PE）
    metric_note: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "company_type": self.company_type,
            "type_label": self.type_label,
            "dcf_applicable": self.dcf_applicable,
            "code": self.code,
            "reason": self.reason,
            "relative_metric": {
                "requested": self.metric,
                "supported": self.metric_supported,
                "note": self.metric_note,
            },
        }


LOSS_REASON = (
    "当前为亏损状态（EPS≤0 或 PE≤0）：PE 分位无意义，且 EBIT 率为负会让 DCF 显性期"
    "自由现金流为负、区间可能给出负股价。本框架此时不出估值，需按 PS/EV-Sales 或"
    "扭亏路径单独建模。"
)


def resolve_gate(
    meta: Dict[str, Any],
    eps_ttm: Optional[float],
    pe_ttm: Optional[float],
) -> ValuationGate:
    """判定 DCF 与相对估值是否成立。亏损信号会把任何类型升级为"状态不适用"。"""
    code_in = meta.get("company_type") or "steady_growth"
    profile = company_profile(code_in)
    metric = meta.get("relative_valuation_metric") or profile.relative_metrics[0]

    applicable = profile.dcf_applicable
    code = None if applicable else "DCF_NOT_APPLICABLE"
    reason = "" if applicable else profile.reason
    label = profile.label

    loss_signal = (eps_ttm is not None and eps_ttm <= 0) or (pe_ttm is not None and pe_ttm <= 0)
    if applicable and loss_signal:
        applicable, code, reason, label = False, "DCF_LOSS_MAKING", LOSS_REASON, "亏损"

    metric_supported = metric == "PE"
    if metric_supported:
        note = "按 PE 十年分位带折算隐含价位（PE × EPS(TTM)）。"
    else:
        note = (
            f"请求口径 {metric}：本框架仅实现 PE 分位带（PB/PS 需每股净资产/每股收入，"
            "字段未登记，无法折算价位），该法按不可用处理，不外推。"
        )

    return ValuationGate(
        company_type="loss_making" if code == "DCF_LOSS_MAKING" else code_in,
        type_label=label,
        dcf_applicable=applicable,
        code=code,
        reason=reason,
        metric=metric,
        metric_supported=metric_supported,
        metric_note=note,
    )
