"""总编排：输入 → PIT 守卫 → 估值 → 派生字段 → 评分 → DQ → 决策 → 结论 → 报告 dict。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from .meta import DISCLAIMER, __version__
from .catalysts import build_calendar, density_score, next_event_score
from .contract import assert_finite, check_assumptions, check_logic_items
from .datadict import validate_structure
from .dataquality import compute_dq
from .decision import COVERAGE_MIN, DATA_GATE_DIMS, decide
from .gate import resolve_gate
from .pit import FieldRef, validate_all_fields
from .riskmatrix import build_matrix, matrix_score
from .rubric import DimensionResult, RubricEngine
from .specs import DIMENSIONS, WEIGHTS_MID, WEIGHTS_SHORT
from .wording import enforce_input, enforce_report

# 闸门只看这三层：财务/行情/估值是"数据整理"，这三层才是"研究判断"
JUDGEMENT_DIMS = ("macro", "industry", "company")
DATA_DIMS = ("financial", "valuation", "trading")
RISK_DIMS = ("risk", "catalyst")
from .tracking import build_tracking
from .valuation import dcf_not_applicable, run_dcf, run_relative, synthesize

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

    # ---- 0) 入口措辞扫描（交易指令不得进入任何下游文本）----
    enforce_input(inp)

    # ---- 1) PIT 守卫（含输入契约：数值有限、档位整数、字段名已登记）----
    registry = validate_all_fields(inp["data"], analysis_date)

    # ---- 1b) 假设层契约 ----
    # data 层有契约不代表假设层有：wacc=NaN 曾一路算出 NaN 区间与 NaN 安全边际，
    # 报告"通过 schema"但输出不是合法 JSON；shares=0 直接 ZeroDivisionError。
    check_assumptions(inp["assumptions"])
    check_logic_items(inp.get("logic_items"))

    # ---- 2) 风险矩阵 & 催化剂日历（含日期合法性）----
    risk_matrix = build_matrix(inp["risks"])
    calendar, cat_stats = build_calendar(inp["catalysts"], analysis_date)

    # ---- 3) 估值（先过方法适用性前门，再决定算不算）----
    # 输入缺失不阻断管线：必需字段缺失由 DQ 标记并强制人工复核；
    # 估值侧按"可用则算、不可用则 null"降级，禁止补零与外推。
    assumptions = inp["assumptions"]
    price = _num(registry, "market.price_close")
    pe_ttm = _num(registry, "market.pe_ttm")
    eps_ttm = _num(registry, "valuation.eps_ttm")
    pctile_pe = _num(registry, "market.pctile_pe_10y")
    gate = resolve_gate(meta_in, eps_ttm, pe_ttm)

    degrade_notes: List[str] = []
    if gate.dcf_applicable:
        try:
            dcf = run_dcf(assumptions["dcf"])
        except ValueError as exc:
            # DCF 自身算不出来（股权价值为负：EBIT 路径过低或净债务超过 EV）。
            # 这类公司往往 EPS 仍为正（靠补贴/投资收益），前门不会拦；
            # 此前直接抛 ValueError，整份报告生不出来。降级为"该法不可用"，
            # 相对法照常，分桶由覆盖/冲突闸门处理。
            gate = replace(
                gate,
                dcf_applicable=False,
                code="DCF_NEGATIVE_EQUITY",
                reason=f"DCF 计算失败：{exc}",
                type_label=f"{gate.type_label}·DCF 不可用",
            )
            dcf = dcf_not_applicable(gate.reason, gate.code)
            degrade_notes.append(f"未执行 DCF（{gate.code}）：{gate.reason}")
    else:
        dcf = dcf_not_applicable(gate.reason, gate.code or "DCF_NOT_APPLICABLE")
        degrade_notes.append(f"未执行 DCF（{gate.code}）：{gate.reason}")
    if pe_ttm is None:
        degrade_notes.append("PE(TTM) 缺失 → 相对估值与 PEG 不可用，估值仅 DCF 单法")
    elif pe_ttm <= 0:
        degrade_notes.append(f"PE(TTM)={pe_ttm} ≤ 0（亏损）→ 相对估值不可用，PEG 不计算")
    if eps_ttm is None:
        degrade_notes.append("EPS(TTM) 缺失 → 相对估值不可用")
    elif eps_ttm <= 0:
        degrade_notes.append(f"EPS(TTM)={eps_ttm} ≤ 0（亏损）→ 分位带折算价位会得到负股价，相对估值不可用")
    if price is None:
        degrade_notes.append("收盘价缺失 → 安全边际与涨跌幅不可计算")

    eps_fwd = assumptions.get("eps_fwd", {})
    eps_fwd_base = float(eps_fwd.get("base", eps_ttm)) if eps_fwd.get("base", eps_ttm) is not None else None
    fwd_growth_pct = (
        (eps_fwd_base / eps_ttm - 1.0) * 100.0
        if (eps_ttm and eps_ttm > 0 and eps_fwd_base is not None) else None
    )
    relative = run_relative(
        metric=gate.metric,
        current=pe_ttm if gate.metric_supported else None,
        pctile=pctile_pe if gate.metric_supported else None,
        hist_p25=_num(registry, "valuation.pe_hist_p25"),
        hist_p75=_num(registry, "valuation.pe_hist_p75"),
        eps_ttm=eps_ttm,
        peer_median=_num(registry, "valuation.peer_pe_median"),
        fwd_growth_pct=fwd_growth_pct,
        unsupported_reason=None if gate.metric_supported else gate.metric_note,
    )
    if not gate.metric_supported:
        degrade_notes.append(f"相对估值口径 {gate.metric} 未实现：{gate.metric_note}")
    dcf_base = dcf.get("scenario_results", {}).get("base") if gate.dcf_applicable else None
    synthesis = synthesize(dcf_base, relative, price, dcf.get("warnings", []), gate=gate)

    # ---- 4) 派生字段 ----
    red_flag_count = len(meta_in.get("red_flags", []))
    roic = _num(registry, "financial.roic")
    wacc_assumed = _num(registry, "financial.wacc_assumed")
    spread = (roic - wacc_assumed) if (roic is not None and wacc_assumed is not None) else None
    dcf_central = dcf_base["value_per_share_central"] if dcf_base else None

    # DCF 单法安全边际该不该进评分，取决于这个数本身站不站得住。两个信号说明它站不住：
    #   ① 两法区间无重叠（union）——现金流折现与历史分位给出完全不同的价位；
    #   ② base 情景永续终值占比 >70%——价值主要来自"第五年之后"，那是假设不是观测。
    # 单独出现任何一个都不足以否定（终值占比高是成熟公司的常态）；两个同时出现时，
    # 这个安全边际不能再当"低估"的证据：宁德时代曾因此拿到 +45.9% → 10 分满分，
    # 而正文同页的合成安全边际是 +282%。
    tv_share_base = dcf_base.get("terminal_value_share") if dcf_base else None
    dcf_margin_block: Optional[str] = None
    if (synthesis.get("mode") == "union" and tv_share_base is not None and tv_share_base > 0.70):
        dcf_margin_block = (
            f"DCF 安全边际未计入评分：两法区间无重叠（{synthesis.get('divergence_pct')}%），"
            f"且基准情景永续终值占比 {tv_share_base:.0%} > 70%。"
            "此时 DCF 的价位主要由终值假设决定，不能用它证明'低估'；"
            "估值维度改由 PE/PB 分位单独承载，覆盖率下降会体现在置信度与覆盖闸门上。"
        )
    margin_of_safety = (
        ((dcf_central - price) / price * 100.0)
        if (dcf_central is not None and price and price > 0 and not dcf_margin_block) else None
    )
    peg: Optional[float] = None
    if (pe_ttm is not None and pe_ttm > 0 and fwd_growth_pct is not None and fwd_growth_pct > 0):
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

    engine = RubricEngine(
        DIMENSIONS, registry, derived_values, derived_sources, analysis_date,
        suppressed={"val.dcf_margin": dcf_margin_block} if dcf_margin_block else None,
    )
    dim_results: List[DimensionResult] = engine.evaluate_all()

    # ---- 5) 汇总与 DQ ----
    composite_mid = RubricEngine.composite(dim_results, "weight_mid")
    composite_short = RubricEngine.composite(dim_results, "weight_short")
    dq = compute_dq(registry, analysis_date)
    if degrade_notes:
        dq["notes"].extend(degrade_notes)
    if not relative.get("available", True) and synthesis["mode"] == "dcf_only":
        dq["notes"].append("相对估值不可用 → 估值结论按单法（DCF）处理，交叉验证缺失")
    if dcf_margin_block:
        dq["notes"].append(dcf_margin_block)
    overall_conf = RubricEngine.overall_confidence(dim_results, "weight_mid", dq["overall"])
    conf_level = "high" if overall_conf >= 0.75 else ("medium" if overall_conf >= 0.5 else "low")
    conf_cn = {"high": "高", "medium": "中", "low": "低"}[conf_level]
    dim_scores = {r.spec.id: r.score for r in dim_results}
    # 归因提前算：摘要要用它的 weight_share 说清"多少来自人的判断"，scorecard 再复用它
    attribution = _attribution(dim_results, composite_mid)

    # 证据覆盖：按指标级计算，而不是"维度里有任何一个指标就算整维度覆盖"。
    # 后者在判断项可以部分自动化之后会虚高——填了一条 M1 就宣称宏观已覆盖，
    # 反而掩盖了政策取向、渗透空间这些真正没做的判断。
    def _indicator_ratio(dim: DimensionResult) -> float:
        num = den = 0.0
        for fac in dim.factors:
            for ind in fac.indicators:
                if ind.suppressed:
                    # 方法性抑制：退出分母。否则一个高权重指标被合理排除，
                    # 会让整个维度看起来"数据不足"，把方法问题误报成缺数问题。
                    continue
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
    data_coverage = {k: ratios.get(k, 0.0) for k in DATA_GATE_DIMS}
    decision = decide(
        meta_in, dq, dim_scores, composite_mid, composite_short, coverage, missing_dims,
        data_coverage=data_coverage,
        model_gate={**gate.as_dict(), "dcf_applicable": gate.dcf_applicable},
    )
    if decision["rule_id"] in ("R8_DATA_COVERAGE", "R9_MODEL_NOT_APPLICABLE"):
        # 这两条闸门是"不出分桶"，不是"分数难看"——必须在 DQ 备注里留痕
        dq["notes"].extend(decision["reasons"][-1:])

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
    # 摘要只放"必须人看"的三件事：逻辑内核、结论与规则、以及真实降级原因。
    # 旧版把 5 个事实用"｜"串成一句、且"主要归因"取 dq.notes[0]（实测是年报口径备注），
    # 读起来像断句，也不指向任何缺口。归因改为：先报缺口/闸门，无缺口才报权重归属。
    essence = _val(registry, "company.logic_essence")
    essence_txt = (f"一句话本质：{essence}" if essence
                   else "一句话本质：（未填：需人工补一句逻辑内核，否则本报告的因果链只有数据侧）")
    ws = attribution["weight_share"]
    if dq["gaps"]:
        first_gap = dq["gaps"][0]
        tail = f"数据缺口 {len(dq['gaps'])} 项，首项 {first_gap['field']}：{first_gap['reason']}"
    elif coverage < COVERAGE_MIN:
        tail = f"判断层证据覆盖仅 {coverage:.0%}，低于 {COVERAGE_MIN:.0%} 闸门，不出正式分桶"
    else:
        tail = (f"判断层占 {ws['judgement']:.0%}、数据层占 {ws['data']:.0%}"
                f"（判断层占比越高越依赖档位选择，越需人工复核）")
    summary = (
        f"{essence_txt}。"
        f"中线 {composite_mid} 分（入池线 7.0）→ 分桶【{decision['bucket_mid']}】，规则 {decision['rule_id']}；"
        f"短线参考【{decision['bucket_short']}】；置信度 {conf_cn}（{overall_conf}）。"
        f"主要归因：{tail}"
        + ("" if gate.dcf_applicable else f"；估值不适用（{gate.code}），本报告不出价位")
    )

    # ---- 8) 情景列表 ----
    probs = assumptions.get("scenario_probabilities", {"optimistic": 0.25, "base": 0.5, "pessimistic": 0.25})
    prob_sum = sum(probs.values())
    if abs(prob_sum - 1.0) > 1e-6:
        raise ValueError(f"三情景概率合计必须为 1，当前 {prob_sum}")
    notes = assumptions.get("scenario_notes", {})
    scenarios_out: List[Dict[str, Any]] = []
    if dcf_base:
        base_central = dcf_base["value_per_share_central"]
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
            "company_type_effective": gate.company_type,
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
        "scorecard": {**_render_scorecard(dim_results, analysis_date),
                      "evidence_coverage": coverage_info,
                      "score_attribution": attribution},
        "valuation": {"gate": gate.as_dict(), "dcf": dcf, "relative": relative, "synthesis": synthesis},
        "scenarios": scenarios_out,
        "risk_matrix": risk_matrix,
        "catalyst_calendar": calendar,
        "tracking_checklist": tracking,
        "data_quality": dq,
        "evidence_log": evidence_log,
        "disclaimer": DISCLAIMER,
    }
    enforce_report(report)
    # ---- 出口总闸：不许出现 NaN/Inf ----
    # 放在措辞扫描之后、返回之前：schema 认为 NaN 是合法 number，
    # 所以"校验通过"并不能保证输出是合法 JSON。
    assert_finite(report)
    return report


def _attribution(dim_results: List[DimensionResult], composite_mid: Any) -> Dict[str, Any]:
    """把综合分拆成"判断层 / 数据层 / 风险催化层"三块贡献，三块相加等于综合分。

    这张表的用途是回答一个具体问题：**这份结论有多少来自人的判断档位**。
    判断层（宏观/行业/公司）里绝大多数指标是 1-5 档人工/代理判断，
    数据层（财务/估值/交易）是财报与行情。两者混在一个 7.2 分里，
    读者没法知道"我到底在为谁的判断付钱"。
    """
    scored = [r for r in dim_results if r.score is not None]
    den = sum(r.spec.weight_mid for r in scored)

    def part(ids: tuple) -> Optional[float]:
        if not den:
            return None
        num = sum(r.score * r.spec.weight_mid for r in scored if r.spec.id in ids)
        return round(num / den, 2)

    def share(ids: tuple) -> float:
        if not den:
            return 0.0
        return round(sum(r.spec.weight_mid for r in scored if r.spec.id in ids) / den, 3)

    return {
        "judgement": part(JUDGEMENT_DIMS),
        "data": part(DATA_DIMS),
        "risk_catalyst": part(RISK_DIMS),
        "total": composite_mid,
        "weight_share": {
            "judgement": share(JUDGEMENT_DIMS),
            "data": share(DATA_DIMS),
            "risk_catalyst": share(RISK_DIMS),
        },
        "note": ("分项贡献 = 组内(维度分×中线权重)之和 ÷ 全部已评维度权重之和，三项相加 = 综合分"
                 "（各分项保留 2 位小数，直接相加可能与综合分差 ±0.01）。"
                 "judgement 层以 1-5 档人工/代理判断为输入，data 层为财报与行情，"
                 "weight_share 为该层在已评维度中的权重占比——判断层占比越高，"
                 "结论越依赖档位选择，越需要人工复核。"),
    }


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
                    "suppressed": ind.suppressed,
                    "rationale": ind.suppress_reason or ind.spec.rationale,
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
