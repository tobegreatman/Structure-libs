"""总编排：输入 → PIT 守卫 → 估值 → 派生字段 → 评分 → DQ → 决策 → 结论 → 报告 dict。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from .meta import DISCLAIMER, __version__
from .catalysts import build_calendar, density_score, next_event_score
from .datadict import validate_structure
from .dataquality import compute_dq
from .decision import COVERAGE_MIN, decide
from .pit import FieldRef, validate_all_fields
from .riskmatrix import build_matrix, matrix_score
from .rubric import DimensionResult, RubricEngine
from .specs import DIMENSIONS, WEIGHTS_MID, WEIGHTS_SHORT

# 闸门只看这三层：财务/行情/估值是"数据整理"，这三层才是"研究判断"
JUDGEMENT_DIMS = ("macro", "industry", "company")
from .tracking import build_tracking
from .valuation import run_dcf, run_relative, synthesize

SCENARIO_NAMES = ("optimistic", "base", "pessimistic")


def _val(registry: Dict[str, FieldRef], field_id: str) -> Optional[Any]:
    ref = registry.get(field_id)
    return None if ref is None else ref.value


def _num(registry: Dict[str, FieldRef], field_id: str) -> Optional[float]:
    v = _val(registry, field_id)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _condition(cid: str, condition: str, metric: str, threshold: str, source: str) -> Dict[str, str]:
    return {"id": cid, "condition": condition, "metric": metric, "threshold": threshold, "source": source}


def run(inp: Dict[str, Any]) -> Dict[str, Any]:
    validate_structure(inp)
    meta_in = inp["meta"]
    analysis_date = date.fromisoformat(meta_in["analysis_date"])

    # ---- 1) PIT 守卫 ----
    registry = validate_all_fields(inp["data"], analysis_date)

    # ---- 2) 风险矩阵 & 催化剂日历（含日期合法性）----
    risk_matrix = build_matrix(inp["risks"])
    calendar, cat_stats = build_calendar(inp["catalysts"], analysis_date)

    # ---- 3) 估值（两法）----
    # 输入缺失不阻断管线：必需字段缺失由 DQ 标记并强制人工复核；
    # 估值侧按"可用则算、不可用则 null"降级，禁止补零与外推。
    assumptions = inp["assumptions"]
    dcf = run_dcf(assumptions["dcf"])
    price = _num(registry, "market.price_close")
    pe_ttm = _num(registry, "market.pe_ttm")
    eps_ttm = _num(registry, "valuation.eps_ttm")
    pctile_pe = _num(registry, "market.pctile_pe_10y")
    degrade_notes: List[str] = []
    if pe_ttm is None:
        degrade_notes.append("PE(TTM) 缺失 → 相对估值与 PEG 不可用，估值仅 DCF 单法")
    if eps_ttm is None:
        degrade_notes.append("EPS(TTM) 缺失 → 相对估值不可用")
    if price is None:
        degrade_notes.append("收盘价缺失 → 安全边际与涨跌幅不可计算")

    eps_fwd = assumptions.get("eps_fwd", {})
    eps_fwd_base = float(eps_fwd.get("base", eps_ttm)) if eps_fwd.get("base", eps_ttm) is not None else None
    fwd_growth_pct = (
        (eps_fwd_base / eps_ttm - 1.0) * 100.0
        if (eps_ttm and eps_fwd_base is not None) else None
    )
    relative = run_relative(
        metric=meta_in.get("relative_valuation_metric", "PE"),
        current=pe_ttm,
        pctile=pctile_pe,
        hist_p25=_num(registry, "valuation.pe_hist_p25"),
        hist_p75=_num(registry, "valuation.pe_hist_p75"),
        eps_ttm=eps_ttm,
        peer_median=_num(registry, "valuation.peer_pe_median"),
        fwd_growth_pct=fwd_growth_pct,
    )
    synthesis = synthesize(dcf["scenario_results"]["base"], relative, price, dcf["warnings"])

    # ---- 4) 派生字段 ----
    red_flag_count = len(meta_in.get("red_flags", []))
    roic = _num(registry, "financial.roic")
    wacc_assumed = _num(registry, "financial.wacc_assumed")
    spread = (roic - wacc_assumed) if (roic is not None and wacc_assumed is not None) else None
    dcf_central = dcf["scenario_results"]["base"]["value_per_share_central"]
    margin_of_safety = ((dcf_central - price) / price * 100.0) if price else None
    peg: Optional[float] = None
    if pe_ttm is not None and fwd_growth_pct is not None and fwd_growth_pct > 0:
        peg = pe_ttm / fwd_growth_pct

    derived_values: Dict[str, Any] = {
        "financial.roic_wacc_spread": spread,
        "valuation.dcf_margin_of_safety": round(margin_of_safety, 1) if margin_of_safety is not None else None,
        "valuation.peg": round(peg, 2) if peg is not None else None,
        "catalyst.density_score": density_score(cat_stats),
        "catalyst.next_event_score": next_event_score(cat_stats),
        "risk.matrix_score": matrix_score(inp["risks"]),
        "risk.red_flag_count": red_flag_count,
    }
    derived_sources: Dict[str, List[str]] = {
        "financial.roic_wacc_spread": ["financial.roic", "financial.wacc_assumed"],
        "valuation.dcf_margin_of_safety": ["market.price_close"],
        "valuation.peg": ["market.pe_ttm", "valuation.eps_ttm"],
        "catalyst.density_score": [],
        "catalyst.next_event_score": [],
        "risk.matrix_score": [],
        "risk.red_flag_count": [],
    }

    engine = RubricEngine(DIMENSIONS, registry, derived_values, derived_sources, analysis_date)
    dim_results: List[DimensionResult] = engine.evaluate_all()

    # ---- 5) 汇总与 DQ ----
    composite_mid = RubricEngine.composite(dim_results, "weight_mid")
    composite_short = RubricEngine.composite(dim_results, "weight_short")
    dq = compute_dq(registry, analysis_date)
    if degrade_notes:
        dq["notes"].extend(degrade_notes)
    if not relative.get("available", True) and synthesis["mode"] == "dcf_only":
        dq["notes"].append("相对估值不可用 → 估值结论按单法（DCF）处理，交叉验证缺失")
    overall_conf = RubricEngine.overall_confidence(dim_results, "weight_mid", dq["overall"])
    conf_level = "high" if overall_conf >= 0.75 else ("medium" if overall_conf >= 0.5 else "low")
    dim_scores = {r.spec.id: r.score for r in dim_results}

    # 证据覆盖：按指标级计算，而不是"维度里有任何一个指标就算整维度覆盖"。
    # 后者在判断项可以部分自动化之后会虚高——填了一条 M1 就宣称宏观已覆盖，
    # 反而掩盖了政策取向、渗透空间这些真正没做的判断。
    def _indicator_ratio(dim: DimensionResult) -> float:
        num = den = 0.0
        for fac in dim.factors:
            for ind in fac.indicators:
                w = fac.spec.weight * ind.spec.weight
                den += w
                if not ind.missing:
                    num += w
        return (num / den) if den else 0.0

    ratios = {r.spec.id: _indicator_ratio(r) for r in dim_results}
    weight_total = sum(r.spec.weight_mid for r in dim_results)
    total_ratio = (sum(r.spec.weight_mid * ratios[r.spec.id] for r in dim_results) / weight_total
                   if weight_total else 0.0)

    # 闸门只看「研究判断层」：财务/行情/估值永远可得，把它们计入分母会稀释信号，
    # 让"只做了数据整理、没做研究"的报告也能过闸。
    judge_dims = [r for r in dim_results if r.spec.id in JUDGEMENT_DIMS]
    judge_weight = sum(r.spec.weight_mid for r in judge_dims)
    coverage = (sum(r.spec.weight_mid * ratios[r.spec.id] for r in judge_dims) / judge_weight
                if judge_weight else 0.0)
    coverage = round(coverage, 3)
    missing_dims = [r.spec.name for r in judge_dims if ratios[r.spec.id] == 0.0]
    coverage_info = {
        "ratio": coverage,
        "threshold": COVERAGE_MIN,
        "basis": "研究判断层（宏观/行业/公司）按指标权重的覆盖比例，未补零",
        "total_ratio": round(total_ratio, 3),
        "by_dimension": {r.spec.id: round(ratios[r.spec.id], 3) for r in dim_results},
        "scored_dimensions": [r.spec.id for r in dim_results if r.score is not None],
        "missing_dimensions": [
            {"id": r.spec.id, "name": r.spec.name, "weight_mid": round(r.spec.weight_mid, 3)}
            for r in judge_dims if ratios[r.spec.id] == 0.0
        ],
        "note": ("闸门口径 = 宏观/行业/公司三层的指标级覆盖；低于阈值时不出正式分桶（R7_COVERAGE）。"
                 "total_ratio 为全维度覆盖，仅供参考，不参与判定"),
    }
    if coverage < COVERAGE_MIN:
        gaps = [f"{r.spec.name} {ratios[r.spec.id]:.0%}" for r in judge_dims if ratios[r.spec.id] < 1.0]
        dq["notes"].append(
            f"研究判断层证据覆盖 {coverage:.0%}（{'、'.join(gaps)}），低于 {COVERAGE_MIN:.0%}：不出正式分桶"
        )

    # ---- 6) 决策映射 ----
    decision = decide(meta_in, dq, dim_scores, composite_mid, composite_short, coverage, missing_dims)

    # ---- 7) 条件化结论 ----
    rev_growth = _num(registry, "financial.rev_growth")
    roe_ttm = _num(registry, "financial.roe_ttm")
    pe_pctile = _num(registry, "market.pctile_pe_10y")
    sorted_risks = sorted(risk_matrix["risks"], key=lambda x: (-x["score"], x["id"]))
    top_risk = sorted_risks[0] if sorted_risks else None

    maintain: List[Dict[str, str]] = []
    if rev_growth is not None:
        maintain.append(_condition(
            "C1", f"下期营收同比增速维持 ≥ {rev_growth * 0.7:.1f}%（当前 {rev_growth:.1f}% 的七成）",
            "financial.rev_growth（定期报告）", f"≥ {rev_growth * 0.7:.1f}%", "公司定期报告（披露日后评价）"))
    if roe_ttm is not None:
        maintain.append(_condition(
            "C2", f"ROE(TTM) 维持 ≥ {max(roe_ttm - 3.0, 0):.1f}%（较当前回落不超过 3pp）",
            "financial.roe_ttm", f"≥ {max(roe_ttm - 3.0, 0):.1f}%", "公司定期报告"))
    if pe_pctile is not None:
        cap = 70.0 if pe_pctile < 70 else 80.0
        maintain.append(_condition(
            "C3", f"PE 十年分位不升破 {cap:.0f}%（当前 {pe_pctile:.0f}%）",
            "market.pctile_pe_10y", f"< {cap:.0f}%", "行情/估值数据库"))
    if top_risk is not None:
        maintain.append(_condition(
            "C4", f"最高风险 {top_risk['id']}（{top_risk['name']}）不触发：{top_risk['trigger']['metric']} {top_risk['trigger']['threshold']}",
            top_risk["trigger"]["metric"], top_risk["trigger"]["threshold"], "公告/行业数据"))

    falsification: List[Dict[str, str]] = []
    fid = 1
    for li in inp.get("logic_items", []):
        vmetric = li.get("verify_metric", "—")
        vthr = li.get("verify_threshold", "—")
        falsification.append(_condition(
            f"F{fid}",
            f"逻辑证伪：若「{vmetric}」不再满足 {vthr}，则逻辑「{li['statement']}」失效"
            f"（传导：{li.get('transmission', '见研究底稿')}）→ 公司/财务维度重估",
            vmetric, vthr, li.get("source", "研究底稿")))
        fid += 1
    if rev_growth is not None:
        falsification.append(_condition(
            f"F{fid}", f"增长失速：营收同比跌破 {rev_growth * 0.5:.1f}%（腰斩线）→ 财务/公司维度重估",
            "financial.rev_growth", f"< {rev_growth * 0.5:.1f}%", "公司定期报告"))
        fid += 1
    falsification.append(_condition(
        f"F{fid}", f"估值风险：PE 十年分位升破 80% 或 DCF 安全边际转负 → 估值维度重估",
        "market.pctile_pe_10y / valuation.dcf_margin_of_safety", ">80% 或 <0", "行情/估值数据库"))
    fid += 1
    if synthesis["mode"] == "union":
        falsification.append(_condition(
            f"F{fid}", "两法估值区间无重叠（方法分歧）→ 估值结论按低置信度处理",
            "valuation.synthesis.mode", "union", "本报告"))
        fid += 1

    valid_until = (analysis_date + timedelta(days=90)).isoformat()
    summary = (
        f"一句话本质：{_val(registry, 'company.logic_essence') if _val(registry, 'company.logic_essence') else '（未填：需人工补一句逻辑内核，否则本报告的因果链只有数据侧）'}"
        f"｜中线综合分 {composite_mid}（观察带 5.0-7.0 / 入池线 7.0），分桶【{decision['bucket_mid']}】"
        f"｜短线参考【{decision['bucket_short']}】（不改变中线定位）"
        f"｜置信度 {conf_level}({overall_conf})，主要归因：{dq['notes'][0] if dq['notes'] else '数据完整性正常'}"
    )

    # ---- 8) 情景列表 ----
    probs = assumptions.get("scenario_probabilities", {"optimistic": 0.25, "base": 0.5, "pessimistic": 0.25})
    prob_sum = sum(probs.values())
    if abs(prob_sum - 1.0) > 1e-6:
        raise ValueError(f"三情景概率合计必须为 1，当前 {prob_sum}")
    notes = assumptions.get("scenario_notes", {})
    base_central = dcf["scenario_results"]["base"]["value_per_share_central"]
    scenarios_out = []
    for name in SCENARIO_NAMES:
        sc = dcf["scenario_results"][name]
        note = notes.get(name, {})
        central = sc["value_per_share_central"]
        scenarios_out.append({
            "name": name,
            "probability": float(probs[name]),
            "key_assumptions": note.get("key_assumptions", [f"增速/利润率路径见 DCF {name} 情景"]),
            "value_per_share_range": {"low": sc["value_per_share_low"], "high": sc["value_per_share_high"]},
            "drivers": note.get("drivers", ["见 DCF 假设路径"]),
            "falsification": note.get("falsification", f"{name} 情景失效：假设路径偏离（见跟踪清单）"),
            "delta_vs_base": f"中枢 {central} 元 vs 基准 {base_central} 元（{(central / base_central - 1) * 100:+.1f}%）",
        })

    # ---- 9) 跟踪清单 ----
    tracking = build_tracking(falsification, maintain, risk_matrix["risks"], calendar)

    # ---- 10) evidence_log ----
    used_by: Dict[str, List[str]] = {}
    for r in dim_results:
        for f in r.factors:
            for ind in f.indicators:
                if ind.spec.derive is not None:
                    for src in derived_sources.get(ind.spec.derive, []):
                        used_by.setdefault(src, []).append(ind.spec.id)
                else:
                    used_by.setdefault(ind.spec.field, []).append(ind.spec.id)
    for fid_extra in ("market.price_close", "valuation.eps_ttm", "market.pe_ttm",
                      "market.pctile_pe_10y", "valuation.pe_hist_p25", "valuation.pe_hist_p75",
                      "valuation.peer_pe_median"):
        used_by.setdefault(fid_extra, []).append("valuation.relative/synthesis")
    evidence_log = []
    for f, ref in sorted(registry.items()):
        row = {
            "field_id": f,
            "source": ref.source,
            "source_tier": ref.source_tier,
            "as_of": ref.as_of,
            "available_at": ref.available_at,
            "used_by": sorted(set(used_by.get(f, ["未直接入模（DQ 统计用）"]))) or ["未直接入模"],
        }
        # 只在是代理时才写 proxy，避免给人工/直采字段增加噪声
        if getattr(ref, "proxy", False):
            row["proxy"] = True
        evidence_log.append(row)
    proxy_fields = [row["field_id"] for row in evidence_log if row.get("proxy")]
    if proxy_fields:
        dq["notes"].append(
            f"{len(proxy_fields)} 个判断字段由代理口径自动推断（非研究结论，"
            f"见 evidence_log 中 proxy=true）：{'、'.join(proxy_fields)}")

    # ---- 11) 组装 ----
    data_cutoff = max((ref.available_at for ref in registry.values()), default=analysis_date.isoformat())
    price_ref = registry.get("market.price_close")
    report: Dict[str, Any] = {
        "schema_version": "0.1.0",
        "meta": {
            "stock": meta_in["stock"],
            "analysis_date": analysis_date.isoformat(),
            "price_ref": {
                "close": price_ref.value if price_ref else None,
                "as_of": price_ref.as_of if price_ref else None,
                "source": price_ref.source if price_ref else "缺失（price_close 未提供）",
                "source_tier": price_ref.source_tier if price_ref else None,
            },
            "horizons": {"primary": "mid_term_6_12m", "reference": "short_term_0_3m"},
            "framework_version": __version__,
            "generated_at": inp.get("generated_at", analysis_date.isoformat()),
            "data_cutoff": data_cutoff,
            "company_type": meta_in.get("company_type", "steady_growth"),
            "relative_valuation_metric": meta_in.get("relative_valuation_metric", "PE"),
        },
        "conclusion": {
            "bucket_mid": decision["bucket_mid"],
            "bucket_short": decision["bucket_short"],
            "composite_score_mid": composite_mid,
            "composite_score_short": composite_short,
            "confidence": {"level": conf_level, "value": overall_conf},
            "decision": {"rule_id": decision["rule_id"], "reasons": decision["reasons"]},
            "summary": summary,
            "maintain_conditions": maintain,
            "falsification": falsification,
            "valid_until": valid_until,
            "review_cadence": "中线：季度复核或证伪条件触发即重估；短线：事件驱动复核",
        },
        "scorecard": {**_render_scorecard(dim_results, analysis_date), "evidence_coverage": coverage_info},
        "valuation": {"dcf": dcf, "relative": relative, "synthesis": synthesis},
        "scenarios": scenarios_out,
        "risk_matrix": risk_matrix,
        "catalyst_calendar": calendar,
        "tracking_checklist": tracking,
        "data_quality": dq,
        "evidence_log": evidence_log,
        "disclaimer": DISCLAIMER,
    }
    return report


def _render_scorecard(dim_results: List[DimensionResult], analysis_date: date) -> Dict[str, Any]:
    dimensions = []
    for r in dim_results:
        factors = []
        for f in r.factors:
            indicators = []
            for ind in f.indicators:
                ev = ind.evidence
                if ev is not None:
                    evidence = {
                        "source": ev.source,
                        "source_tier": ev.source_tier,
                        "as_of": ev.as_of,
                        "available_at": ev.available_at,
                    }
                else:
                    evidence = {
                        "source": "计算字段（公式见评分说明与 rationale）",
                        "source_tier": "L1",
                        "as_of": analysis_date.isoformat(),
                        "available_at": analysis_date.isoformat(),
                    }
                indicators.append({
                    "id": ind.spec.id,
                    "name": ind.spec.name,
                    "unit": ind.spec.unit,
                    "value": ind.value,
                    "band": ind.band,
                    "score": ind.score,
                    "weight": ind.spec.weight,
                    "missing": ind.missing,
                    "rationale": ind.spec.rationale,
                    "evidence": evidence,
                })
            factors.append({
                "id": f.spec.id, "name": f.spec.name, "weight": f.spec.weight,
                "renormalized": f.renormalized,
                "available_weight_ratio": f.available_weight_ratio,
                "indicators": indicators,
            })
        dimensions.append({
            "id": r.spec.id, "name": r.spec.name,
            "score_mid": r.score, "score_short": r.score,
            "confidence": {"level": r.confidence_level, "value": r.confidence},
            "factors": factors,
        })
    return {
        "scale": "0-10",
        "weights": {"mid_term": WEIGHTS_MID, "short_term": WEIGHTS_SHORT},
        "dimensions": dimensions,
    }
