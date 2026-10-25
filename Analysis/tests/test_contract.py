"""输入契约、方法前门、措辞拦截的回归测试。

这些用例钉的都是"安静地错"——不报错、不崩溃，但结论是假的：
NaN 落进最差档、+Inf 落进满分档、档位 4.7 被截成 4、字段名拼错后整块证据消失、
银行股算出 +95.7% 安全边际、亏损公司输出负股价、'目标价'原样进结论首句。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from stockframe.contract import ContractError
from stockframe.datadict import InputError
from stockframe.engine import run
from stockframe.validate import validate_report
from stockframe.wording import BANNED_TERMS, WordingError

ROOT = Path(__file__).resolve().parent.parent


def _sample() -> dict:
    with open(ROOT / "examples" / "sample_input.json", encoding="utf-8") as fh:
        return json.load(fh)


def _run_with(mut) -> dict:
    inp = copy.deepcopy(_sample())
    mut(inp)
    return run(inp)


# ---------------------------------------------------------------- 数值契约

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_is_rejected(bad):
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["data"]["financial"]["roe_ttm"].update(value=bad))
    assert "roe_ttm" in str(exc.value)


def test_bool_is_not_a_number():
    with pytest.raises(ContractError):
        _run_with(lambda i: i["data"]["financial"]["roe_ttm"].update(value=True))


def test_price_must_be_positive():
    for bad in (0, -3):
        with pytest.raises(ContractError):
            _run_with(lambda i, b=bad: i["data"]["market"]["price_close"].update(value=b))


# ---------------------------------------------------------------- 档位契约

def test_qualitative_must_be_integer():
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["data"]["company"]["moat"].update(value=4.7))
    assert "整数" in str(exc.value)


@pytest.mark.parametrize("bad", [0, 6, 9])
def test_qualitative_out_of_range(bad):
    with pytest.raises(ContractError):
        _run_with(lambda i, b=bad: i["data"]["company"]["moat"].update(value=b))


def test_qualitative_string_rejected():
    with pytest.raises(ContractError):
        _run_with(lambda i: i["data"]["company"]["moat"].update(value="4"))


# ---------------------------------------------------------------- 字段名契约

def test_typo_field_is_rejected_with_suggestion():
    def typo(i):
        i["data"]["financial"]["roe_typo"] = i["data"]["financial"].pop("roe_ttm")

    with pytest.raises(ContractError) as exc:
        _run_with(typo)
    msg = str(exc.value)
    assert "roe_typo" in msg and "financial.roe_ttm" in msg


# ---------------------------------------------------------------- 文本契约

def test_text_field_must_be_string():
    with pytest.raises(ContractError):
        _run_with(lambda i: i["data"]["company"]["logic_essence"].update(value=12345))


def test_text_field_length_cap():
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["data"]["company"]["logic_essence"].update(value="长" * 201))
    assert "200" in str(exc.value)


def test_blank_text_is_missing_not_error():
    def blank(i):
        i["data"]["company"]["logic_essence"]["value"] = ""

    rep = _run_with(blank)
    assert "未填" in rep["conclusion"]["summary"]
    # 空串不能算"已提供"，否则 DQ 的完整性会被虚高
    assert "company.logic_essence" in [g["field"] for g in rep["data_quality"]["gaps"]]


# ---------------------------------------------------------------- 方法前门

def test_bank_is_not_applicable_and_gated():
    def bank(i):
        i["meta"]["company_type"] = "bank"
        i["meta"]["relative_valuation_metric"] = "PB"

    rep = _run_with(bank)
    v = rep["valuation"]
    assert v["gate"]["dcf_applicable"] is False
    assert v["gate"]["code"] == "DCF_NOT_APPLICABLE"
    assert v["dcf"]["applicable"] is False
    assert "scenario_results" not in v["dcf"], "不适用时不得留下 DCF 数字"
    assert v["synthesis"]["mode"] == "not_applicable"
    assert v["synthesis"]["combined_range"] == {"low": None, "high": None}
    assert v["synthesis"]["safety_margin_pct"] is None
    assert rep["scenarios"] == []
    assert rep["conclusion"]["decision"]["rule_id"] == "R9_MODEL_NOT_APPLICABLE"
    assert rep["conclusion"]["bucket_mid"] == "人工复核"
    ok, errs = validate_report(rep)
    assert ok, errs[:3]


def test_metric_must_match_company_type():
    def bank_pe(i):
        i["meta"]["company_type"] = "bank"   # 默认口径仍是 PE

    with pytest.raises(InputError) as exc:
        _run_with(bank_pe)
    assert "PB" in str(exc.value)


def test_unknown_company_type_rejected():
    with pytest.raises(InputError) as exc:
        _run_with(lambda i: i["meta"].update(company_type="growth"))
    assert "未登记" in str(exc.value)


@pytest.mark.parametrize("mut", [
    lambda i: i["data"]["valuation"]["eps_ttm"].update(value=-0.8),
    lambda i: i["data"]["market"]["pe_ttm"].update(value=-35.0),
])
def test_loss_making_is_gated(mut):
    rep = _run_with(mut)
    v = rep["valuation"]
    assert v["gate"]["code"] == "DCF_LOSS_MAKING"
    assert rep["meta"]["company_type_effective"] == "loss_making"
    assert v["relative"]["available"] is False, "亏损时相对估值必须不可用"
    assert v["relative"]["implied_price_range"]["low"] is None
    assert v["synthesis"]["safety_margin_pct"] is None
    ok, errs = validate_report(rep)
    assert ok, errs[:3]


def test_non_positive_range_is_rejected():
    """区间下限 ≤0 时宁可不给区间，也不输出负股价。"""
    from stockframe.valuation import synthesize

    dcf_base = {"value_per_share_low": -2.0, "value_per_share_high": 1.0}
    rel = {"available": False, "implied_price_range": {"low": None, "high": None}}
    out = synthesize(dcf_base, rel, 10.0, [], gate=None)
    assert out["mode"] == "rejected"
    assert out["combined_range"] == {"low": None, "high": None}
    assert out["safety_margin_pct"] is None


# ---------------------------------------------------------------- 数据层覆盖闸门

@pytest.mark.parametrize("group,drop", [
    ("financial", ("roic", "ocf_np_ratio", "wc_growth_gap", "np_growth", "gm_trend",
                   "int_debt_ratio", "goodwill_pct")),
    ("market", ("dist_ma250", "drawdown_52w", "vol_ratio_20d", "turnover_pct",
                "inst_hold_chg_pct")),
])
def test_thin_data_blocks_bucket(group, drop):
    def strip(i):
        for key in drop:
            i["data"][group].pop(key, None)

    rep = _run_with(strip)
    assert rep["conclusion"]["decision"]["rule_id"] == "R8_DATA_COVERAGE"
    assert rep["conclusion"]["bucket_mid"] == "人工复核"


# ---------------------------------------------------------------- 措辞拦截

def test_banned_wording_in_input_is_blocked():
    with pytest.raises(WordingError) as exc:
        _run_with(lambda i: i["data"]["company"]["logic_essence"].update(value="建议目标价 45 元买入"))
    assert "目标价" in str(exc.value)


def test_banned_terms_match_acceptance():
    """两套词表各自漂移过一次，出现过"脚本放行、验收拦下"。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("acc", ROOT / "tools" / "acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module.BANNED) == BANNED_TERMS


def test_sample_report_has_no_banned_wording():
    rep = run(_sample())
    text = json.dumps(rep["conclusion"], ensure_ascii=False)
    assert not [t for t in BANNED_TERMS if t in text]


# ---------------------------------------------------------------- 归因分解

def test_attribution_sums_to_composite():
    rep = run(_sample())
    at = rep["scorecard"]["score_attribution"]
    total = at["judgement"] + at["data"] + at["risk_catalyst"]
    assert abs(total - rep["conclusion"]["composite_score_mid"]) <= 0.02
    share = at["weight_share"]
    assert abs(sum(share.values()) - 1.0) <= 0.002


# ---------------------------------------------------------------- 假设层契约
# data 层有契约不代表假设层有：wacc=NaN 曾一路算出 NaN 区间，报告"通过 schema"
# 但输出不是合法 JSON；shares=0 直接 ZeroDivisionError。

@pytest.mark.parametrize("scenario,key", [
    ("base", "wacc"),
    ("base", "revenue_base"),
    ("optimistic", "ebit_margin_path"),
])
def test_nan_in_assumptions_is_rejected(scenario, key):
    def mut(i):
        target = i["assumptions"]["dcf"][scenario]
        if key == "ebit_margin_path":
            target[key][0] = float("nan")
        else:
            target[key] = float("nan")
    with pytest.raises(ContractError) as exc:
        _run_with(mut)
    assert scenario in str(exc.value)


def test_zero_shares_is_rejected_not_division_error():
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["assumptions"]["dcf"]["base"].update(shares=0))
    assert "shares" in str(exc.value)
    assert "除法" in str(exc.value) or "ZeroDivisionError" in str(exc.value)


def test_string_assumption_is_rejected_not_type_error():
    """字符串 "0.08" 会在加法里抛 TypeError，报错信息看不出是哪个字段。"""
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["assumptions"]["dcf"]["base"].update(wacc="0.08"))
    assert "数值" in str(exc.value)


@pytest.mark.parametrize("missing", ["dcf", "eps_fwd", "scenario_probabilities"])
def test_missing_assumption_block_is_named(missing):
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["assumptions"].pop(missing))
    assert missing in str(exc.value)


def test_probability_sum_is_checked_before_engine():
    with pytest.raises(ValueError, match="概率"):
        _run_with(lambda i: i["assumptions"]["scenario_probabilities"].update(base=0.4))


def test_wacc_not_above_growth_is_named_with_scenario():
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i["assumptions"]["dcf"]["base"].update(wacc=0.02, terminal_growth=0.05))
    assert "base" in str(exc.value) and "terminal_growth" in str(exc.value)


def test_report_is_strict_json_serialisable():
    """出口总闸：NaN/Inf 不得进入报告。

    jsonschema 的 number 接受 NaN，所以"schema 校验通过"不代表输出是合法 JSON；
    浏览器 res.json() 会抛 Unexpected token 'N'，用户只看到"请求失败"。
    """
    rep = run(_sample())
    json.dumps(rep, ensure_ascii=False, allow_nan=False)


def test_assert_finite_points_at_field_path():
    from stockframe.contract import assert_finite
    with pytest.raises(ContractError) as exc:
        assert_finite({"a": [{"b": float("nan")}]}, "report")
    assert "report.a[0].b" in str(exc.value)


# ---------------------------------------------------------------- 逻辑条目契约
# verify_metric 会被原样拼进「F1 逻辑证伪」：编造的字段 ID 不会报错，只会安静地进报告。

def test_logic_item_without_statement_is_rejected():
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i.update(logic_items=[{"verify_metric": "financial.rev_growth"}]))
    assert "statement" in str(exc.value)


def test_invented_verify_metric_is_rejected():
    with pytest.raises(ContractError) as exc:
        _run_with(lambda i: i.update(logic_items=[
            {"statement": "份额兑现", "verify_metric": "financial.revenue_growth_qoq"}]))
    assert "rev_growth" in str(exc.value)


def test_free_form_verify_metric_is_allowed():
    """自由指标（带口径、不带字段段前缀）是合法的，不能一刀切拦掉。"""
    rep = _run_with(lambda i: i.update(logic_items=[
        {"statement": "份额兑现", "verify_metric": "大客户排产同比", "verify_threshold": "≥ 20%"}]))
    assert rep["conclusion"]["bucket_mid"]


# ---------------------------------------------------------------- 措辞豁免

@pytest.mark.parametrize("text", [
    "买的是份额兑现：股价仍低于卖方目标价中位数，若份额下滑则失效",
    "买的是份额兑现：当前阶段不加仓，若份额跌破 20% 则失效",
    "大股东公告清仓式减持计划，占总股本 8%",
])
def test_legitimate_research_wording_is_not_blocked(text):
    rep = _run_with(lambda i: i["data"]["company"]["logic_essence"].update(value=text))
    assert rep["conclusion"]["bucket_mid"] in {"入池", "观察", "人工复核", "回避"}


@pytest.mark.parametrize("text,term", [
    ("给予买入评级，目标价 45 元", "买入"),
    ("跌破 10 日线则止损", "止损"),
    ("不看好，建议卖出", "卖出"),
])
def test_imperative_wording_is_still_blocked(text, term):
    with pytest.raises(WordingError) as exc:
        _run_with(lambda i: i["data"]["company"]["logic_essence"].update(value=text))
    assert term in str(exc.value)


def test_trading_order_hidden_in_risk_description_is_blocked():
    """风险描述会原样进风险矩阵表：此前出口只扫结论与情景，这里成了唯一能写指令的地方。"""
    def mut(i):
        i["risks"].append({
            "id": "R9", "name": "跌破均线即刻卖出止损", "category": "finance",
            "probability": 3, "impact": 3,
            "trigger": {"metric": "收盘价", "threshold": "跌破 250 日线"},
            "mitigation": "跟踪均线", "evidence": "行情",
        })
    with pytest.raises(WordingError) as exc:
        _run_with(mut)
    assert "risk_matrix" in str(exc.value)


# ---------------------------------------------------------------- 安全边际口径

def test_union_mode_emits_no_combined_margin():
    """两法无重叠时，并集中点没有经济含义，不能当安全边际。

    宁德时代曾因此出现正文 +282%，而同一份报告评分卡里的 DCF 单法是 +45.9%。
    """
    def mut(i):
        # 抬高 DCF 三条情景的利润率路径，让 DCF 区间整体高于相对法区间，制造无重叠
        for name in ("base", "optimistic", "pessimistic"):
            sc = i["assumptions"]["dcf"][name]
            sc.update(ebit_margin_path=[0.45] * 5)
    rep = _run_with(mut)
    syn = rep["valuation"]["synthesis"]
    if syn["mode"] == "union":
        assert syn["safety_margin_pct"] is None
        assert syn["margin_basis"] == "none"
        assert syn["dcf_margin_pct"] is not None
        assert syn["divergence_pct"] is not None


def test_dcf_margin_suppressed_when_methods_diverge_and_terminal_dominates():
    """两法分歧 + 终值占 75%：DCF 安全边际不再进评分，覆盖率分母也要剔除它。"""
    rep = _run_with(lambda i: None)
    dim = next(d for d in rep["scorecard"]["dimensions"] if d["id"] == "valuation")
    ind = next(x for f in dim["factors"] for x in f["indicators"] if x["id"] == "val.dcf_margin")
    syn = rep["valuation"]["synthesis"]
    if syn["mode"] == "union" and syn["margin_basis"] == "none":
        assert ind["missing"] is True
        assert ind["suppressed"] is True
        # 抑制不计入覆盖率分母：否则方法问题会被误报成缺数问题（触发 R8）
        assert rep["conclusion"]["decision"]["rule_id"] != "R8_DATA_COVERAGE"
