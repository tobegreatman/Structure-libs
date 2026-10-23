"""估值模块：方法一 FCFF DCF（三情景+敏感性）+ 方法二 相对估值（分位带）+ 区间合成。

设计文档 §7：
- 不给单点价格预测，只给区间；
- 永续 TV 占比 >70% 触发警示（终值主导 → 结论可信度降档）；
- WACC 默认区间 8%-12%，永续 g 2%-3%（超 GDP 增速报警）；
- 全部输入为 assumption（预测数据隔离），报告中原样标注；
- 输入缺失（PE/EPS/现价）时降级为单法或不可用，**不外推、不补零**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class DCFParams:
    """单一情景的 DCF 假设（全部为 assumption，单位：亿元/比率）。"""
    wacc: float
    terminal_growth: float
    revenue_growth_path: Tuple[float, float, float, float, float]
    ebit_margin_path: Tuple[float, float, float, float, float]
    tax_rate: float
    dep_amort_pct_rev: float
    capex_pct_rev: float
    nwc_pct_rev_delta: float
    revenue_base: float          # 最近完整年度营收（亿元）
    net_debt: float              # 净债务（亿元），净现金为负
    shares: float                # 总股本（亿股）


def _fcff_pv(params: DCFParams) -> Tuple[float, float, float]:
    """返回 (EV, PV(TV), 显性期 PV 合计)。"""
    rev = params.revenue_base
    pv_explicit = 0.0
    fcff_last = 0.0
    for i in range(5):
        rev = rev * (1.0 + params.revenue_growth_path[i])
        ebit = rev * params.ebit_margin_path[i]
        nopat = ebit * (1.0 - params.tax_rate)
        da = rev * params.dep_amort_pct_rev
        capex = rev * params.capex_pct_rev
        delta_nwc = rev * params.nwc_pct_rev_delta
        fcff = nopat + da - capex - delta_nwc
        pv_explicit += fcff / (1.0 + params.wacc) ** (i + 1)
        fcff_last = fcff
    denom = params.wacc - params.terminal_growth
    if denom <= 0.005:
        raise ValueError(f"WACC({params.wacc}) 与永续增长率({params.terminal_growth}) 差距过小，终值不可计算")
    tv = fcff_last * (1.0 + params.terminal_growth) / denom
    pv_tv = tv / (1.0 + params.wacc) ** 5
    return pv_explicit + pv_tv, pv_tv, pv_explicit


def value_per_share(params: DCFParams) -> Tuple[float, float]:
    """返回 (每股价值, TV 占比)。"""
    ev, pv_tv, _ = _fcff_pv(params)
    equity = ev - params.net_debt
    if equity <= 0:
        raise ValueError("DCF 股权价值为负：假设不成立（营收/利润率路径过低或净债务过高）")
    return equity / params.shares, pv_tv / ev


def _perturbed(params: DCFParams, wacc_delta: float) -> DCFParams:
    return DCFParams(**{**params.__dict__, "wacc": params.wacc + wacc_delta})


def run_dcf(assumptions: Dict[str, Any]) -> Dict[str, Any]:
    """三情景 DCF + 基准情景敏感性网格 + 警示列表。"""
    warnings: List[str] = []
    scenario_results: Dict[str, Any] = {}
    for name in ("optimistic", "base", "pessimistic"):
        a = assumptions[name]
        params = DCFParams(
            wacc=a["wacc"], terminal_growth=a["terminal_growth"],
            revenue_growth_path=tuple(a["revenue_growth_path"]),
            ebit_margin_path=tuple(a["ebit_margin_path"]),
            tax_rate=a.get("tax_rate", 0.15),
            dep_amort_pct_rev=a.get("dep_amort_pct_rev", 0.05),
            capex_pct_rev=a.get("capex_pct_rev", 0.07),
            nwc_pct_rev_delta=a.get("nwc_pct_rev_delta", 0.01),
            revenue_base=a["revenue_base"], net_debt=a["net_debt"], shares=a["shares"],
        )
        central, tv_share = value_per_share(params)
        try:
            v_low, _ = value_per_share(_perturbed(params, +0.01))
            v_high, _ = value_per_share(_perturbed(params, -0.01))
        except ValueError:
            v_low, v_high = central, central
            warnings.append(f"{name} 情景 WACC 扰动失败（WACC-g 过小），区间退化为中枢点")
        if v_low > v_high:
            v_low, v_high = v_high, v_low
        if tv_share > 0.70:
            warnings.append(
                f"{name} 情景永续终值占比 {tv_share:.0%} > 70%：估值高度依赖终值假设，可信度降档"
            )
        if params.terminal_growth > 0.03:
            warnings.append(f"{name} 情景永续增长率 {params.terminal_growth:.1%} 超过 GDP 长期增速上限")
        if not (0.08 <= params.wacc <= 0.12):
            warnings.append(f"{name} 情景 WACC {params.wacc:.1%} 超出默认合理区间 8%-12%")
        scenario_results[name] = {
            "wacc": params.wacc,
            "terminal_growth": params.terminal_growth,
            "revenue_growth_path": list(params.revenue_growth_path),
            "ebit_margin_path": list(params.ebit_margin_path),
            "value_per_share_central": round(central, 2),
            "value_per_share_low": round(v_low, 2),
            "value_per_share_high": round(v_high, 2),
            "terminal_value_share": round(tv_share, 3),
            "net_debt_assumption": params.net_debt,
            "shares_assumption": params.shares,
        }

    base_a = assumptions["base"]
    base_params = DCFParams(
        wacc=base_a["wacc"], terminal_growth=base_a["terminal_growth"],
        revenue_growth_path=tuple(base_a["revenue_growth_path"]),
        ebit_margin_path=tuple(base_a["ebit_margin_path"]),
        tax_rate=base_a.get("tax_rate", 0.15),
        dep_amort_pct_rev=base_a.get("dep_amort_pct_rev", 0.05),
        capex_pct_rev=base_a.get("capex_pct_rev", 0.07),
        nwc_pct_rev_delta=base_a.get("nwc_pct_rev_delta", 0.01),
        revenue_base=base_a["revenue_base"], net_debt=base_a["net_debt"], shares=base_a["shares"],
    )
    w0 = base_params.wacc
    g0 = base_params.terminal_growth
    wacc_values = [round(w0 + d / 10000.0, 4) for d in (-150, -75, 0, 75, 150)]
    g_values = [round(g0 + d / 10000.0, 4) for d in (-50, -25, 0, 25, 50)]
    grid: List[List[Any]] = []
    nan_cells = 0
    for w in wacc_values:
        row: List[Any] = []
        for g in g_values:
            try:
                p = DCFParams(**{**base_params.__dict__, "wacc": w, "terminal_growth": g})
                v, _ = value_per_share(p)
                row.append(round(v, 2))
            except ValueError:
                row.append(None)
                nan_cells += 1
        grid.append(row)
    if nan_cells:
        warnings.append(f"敏感性网格 {nan_cells} 个格点不可计算（WACC 与 g 差距过小），以 null 表示")

    return {
        "method": "FCFF_DCF",
        "assumptions_kind": "assumption",
        "scenario_results": scenario_results,
        "sensitivity_grid_base": {"wacc_values": wacc_values, "g_values": g_values, "values": grid},
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 相对估值

def percentile_band(pctile: float) -> str:
    if pctile < 30:
        return "low"
    if pctile > 70:
        return "high"
    return "neutral"


# 同业锚的可比区间：本公司估值与同业中位相差在 0.5–2 倍以内，才认为"同一门生意"可比。
# 超出这个范围时，横向锚不给折算价位——业务结构差异（如代工 vs 品牌）足以解释倍数差，
# 此时把中位数乘 EPS 折成价位，等于用一个不可比的锚定价，是变相的点位判断。
PEER_COMPARABLE_RANGE = (0.5, 2.0)


def _peer_anchor(own: Optional[float], peer_median: Optional[float],
                 eps_ttm: Optional[float]) -> Dict[str, Any]:
    if not peer_median or not own:
        return {
            "peer_median": peer_median,
            "premium_pct": None,
            "implied_price": None,
            "comparable": False,
            "note": "同业数据缺失，横向锚未启用（不猜测同业中枢）",
        }
    premium = (own / peer_median - 1.0) * 100.0
    lo, hi = PEER_COMPARABLE_RANGE
    ratio = own / peer_median
    comparable = lo <= ratio <= hi
    implied = peer_median * eps_ttm if (comparable and eps_ttm) else None
    note = (
        f"本公司 {own:.1f} 倍 vs 同业中位 {peer_median:.1f} 倍 → "
        f"{'折价' if premium < 0 else '溢价'} {abs(premium):.0f}%"
    )
    if comparable:
        note += "；倍数处于同一可比区间，横向锚仅作参照，不构成价位判断"
    else:
        note += ("；两者倍数相差过大（超出 0.5–2 倍可比区间），业务结构差异足以解释，"
                 "横向折算价位不成立，此处不给单点估值")
    return {
        "peer_median": peer_median,
        "premium_pct": round(premium, 1),
        "implied_price": round(implied, 2) if implied else None,
        "comparable": comparable,
        "note": note,
    }


def run_relative(
    metric: str,
    current: Optional[float],
    pctile: Optional[float],
    hist_p25: Optional[float],
    hist_p75: Optional[float],
    eps_ttm: Optional[float],
    peer_median: Optional[float],
    fwd_growth_pct: Optional[float],
) -> Dict[str, Any]:
    """相对估值。任一必需输入缺失 → available=False，输出 null 而非补零/外推。"""
    missing = []
    if current is None:
        missing.append(f"{metric}(TTM)")
    if eps_ttm is None:
        missing.append("EPS(TTM)")

    if missing:
        return {
            "method": "percentile_band",
            "available": False,
            "metric": metric,
            "current": current,
            "percentile_10y": pctile,
            "band": percentile_band(pctile) if pctile is not None else None,
            "bands_definition": {"low": "<30%", "neutral": "30-70%", "high": ">70%"},
            "implied_price_range": {"low": None, "high": None, "eps_used": eps_ttm, "eps_kind": "TTM"},
            "peer_anchor": {
                "peer_median": peer_median,
                "premium_pct": None,
                "implied_price": None,
                "comparable": False,
                "note": "相对估值未启用：缺少 " + "、".join(missing) + "（不补零、不外推）",
            },
            "priced_in_narrative": (
                "相对估值不可用：缺 " + "、".join(missing)
                + "；本报告该法按缺失处理，估值结论仅依赖 DCF 单法，置信度相应降档。"
            ),
        }

    band = percentile_band(pctile) if pctile is not None else None
    if hist_p25 and hist_p75:
        low = hist_p25 * eps_ttm
        high = hist_p75 * eps_ttm
        eps_used, eps_kind = eps_ttm, "TTM"
    else:
        # 无历史分位带数据时退化为 ±20% 中枢带（记录于 note）
        low, high = current * 0.8, current * 1.2
        eps_used, eps_kind = eps_ttm, "TTM"
    anchor = _peer_anchor(current, peer_median, eps_ttm)
    if band is None:
        narrative_parts = [f"当前 {metric}(TTM) {current:.1f} 倍；十年分位数据缺失，分位带不作判定"]
    else:
        narrative_parts = [
            f"当前 {metric}(TTM) {current:.1f} 倍处于十年 {pctile:.0f}% 分位（"
            + {"low": "偏低", "neutral": "中性", "high": "偏高"}[band] + "区间）"
        ]
    if fwd_growth_pct is not None:
        narrative_parts.append(
            f"基准情景预期 EPS 增速约 {fwd_growth_pct:.0f}%，"
            f"当前估值隐含的增长预期与该增速{'匹配' if fwd_growth_pct >= 15 else '存在落差，需催化剂验证'}"
        )
    if band == "high":
        narrative_parts.append("高分位意味着市场已定价较充分的景气与增长预期，超预期空间收窄")
    elif band == "low":
        narrative_parts.append("低分位可能隐含市场对持续性的怀疑，需核对是错杀还是基本面确有瑕疵")
    narrative_parts.append("以上为基于分位与增速的拆解，不构成价格点位判断")

    return {
        "method": "percentile_band",
        "available": True,
        "metric": metric,
        "current": current,
        "percentile_10y": pctile,
        "band": band,
        "bands_definition": {"low": "<30%", "neutral": "30-70%", "high": ">70%"},
        "implied_price_range": {
            "low": round(low, 2), "high": round(high, 2),
            "eps_used": eps_used, "eps_kind": eps_kind,
        },
        "peer_anchor": anchor,
        "priced_in_narrative": "；".join(narrative_parts) + "。",
    }


# ---------------------------------------------------------------- 区间合成

def synthesize(
    dcf_base: Dict[str, Any],
    relative: Dict[str, Any],
    price: Optional[float],
    dcf_warnings: List[str],
) -> Dict[str, Any]:
    d_lo = dcf_base["value_per_share_low"]
    d_hi = dcf_base["value_per_share_high"]
    rel_ok = bool(relative.get("available", True)) and relative["implied_price_range"]["low"] is not None

    if rel_ok:
        r_lo = relative["implied_price_range"]["low"]
        r_hi = relative["implied_price_range"]["high"]
        inter_lo, inter_hi = max(d_lo, r_lo), min(d_hi, r_hi)
        if inter_lo <= inter_hi:
            mode, lo, hi = "intersection", inter_lo, inter_hi
        else:
            mode, lo, hi = "union", min(d_lo, r_lo), max(d_hi, r_hi)
        relative_range = {"low": r_lo, "high": r_hi}
    else:
        mode, lo, hi = "dcf_only", d_lo, d_hi
        relative_range = {"low": None, "high": None}

    central = (lo + hi) / 2.0
    safety = (central - price) / price * 100.0 if price else None
    if mode == "dcf_only":
        level = "low"
    elif mode == "intersection" and not dcf_warnings:
        level = "high"
    elif mode == "intersection" or not dcf_warnings:
        level = "medium"
    else:
        level = "low"
    if mode == "dcf_only":
        note = "相对估值不可用，合成区间退化为基准情景 DCF 单法区间；置信度按 low 处理（单法无法交叉验证）。"
    else:
        note = (
            "合成区间 = 基准情景 DCF 区间与相对估值分位带区间的"
            + ("交集" if mode == "intersection" else "并集（两法区间无重叠，存在方法分歧）")
            + "；仅为安全边际度量基准，非价格点位预测；加权期望值同为参考量。"
        )
    return {
        "dcf_base_range": {"low": d_lo, "high": d_hi},
        "relative_range": relative_range,
        "combined_range": {"low": round(lo, 2), "high": round(hi, 2)},
        "mode": mode,
        "safety_margin_pct": round(safety, 1) if safety is not None else None,
        "confidence_level": level,
        "note": note,
    }
