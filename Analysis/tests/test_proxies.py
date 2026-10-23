"""代理口径单测：只验算档位与缺数据处理，不访问网络。

这些测试盯住三类容易悄悄错掉的事：
1. 档位边界（尤其是「越小越好」的指标方向）；
2. 缺失分项按权重重归一化，而不是补零；
3. 数据不足时返回空，不用 2-3 个样本硬算中位数。
"""

from __future__ import annotations

import pytest

from stockframe import proxies


# ---------------------------------------------------------------- 档位映射

def test_grade_ascending_band_edges():
    cuts = [0.0, 3.0, 8.0, 15.0]
    assert proxies._grade(-1.0, cuts) == 1
    assert proxies._grade(0.0, cuts) == 2
    assert proxies._grade(2.9, cuts) == 2
    assert proxies._grade(3.0, cuts) == 3
    assert proxies._grade(12.5, cuts) == 4
    assert proxies._grade(15.0, cuts) == 5
    assert proxies._grade(None, cuts) is None


def test_grade_reverse_is_for_lower_is_better():
    """质押比例、毛利率标准差：越小越好，最小值必须落在 5 档。"""
    cuts = [10.0, 20.0, 30.0, 50.0]
    assert proxies._grade(0.06, cuts, reverse=True) == 5
    assert proxies._grade(15.0, cuts, reverse=True) == 4
    assert proxies._grade(25.0, cuts, reverse=True) == 3
    assert proxies._grade(40.0, cuts, reverse=True) == 2
    assert proxies._grade(80.0, cuts, reverse=True) == 1


def test_weighted_renormalizes_instead_of_zero_filling():
    # 缺分项时按剩余权重重归一化：(4*0.4 + 5*0.3)/0.7 ≈ 4.43 → 4
    assert proxies._weighted([(4, 0.4), (None, 0.3), (5, 0.3)]) == 4
    # 全部缺失才是 None，不能拿 0 分顶替
    assert proxies._weighted([(None, 0.5), (None, 0.5)]) is None
    # 结果必须夹在 1..5
    assert proxies._weighted([(5, 1.0)]) == 5


# ---------------------------------------------------------------- 宏观

def test_macro_fields_are_observed_not_proxy():
    out = proxies.macro_fields({
        "m1": {"value": 3.2, "period": "2026-09", "period_label": "2026年09月份",
               "available_at": "2026-10-15", "lag_days_assumed": 15},
        "pmi": {"value": 50.1, "period": "2026-09", "period_label": "2026年09月份",
                "available_at": "2026-10-02", "lag_days_assumed": 2},
    }, "2026-10-23")
    by_id = {f.field_id: f for f in out}
    assert set(by_id) == {"macro.m1_yoy", "macro.pmi"}
    assert all(f.basis == "observed" for f in out)
    assert by_id["macro.m1_yoy"].available_at == "2026-10-15"
    assert "保守取月末+15 天" in by_id["macro.m1_yoy"].formula


def test_macro_policy_and_rate_are_not_proxied():
    """政策取向与 10Y 利率没有可复算口径，代理层不得凭空造值。"""
    out = proxies.macro_fields({}, "2026-10-23")
    assert out == []


# ---------------------------------------------------------------- 行业

def _industry(**over):
    base = {
        "name": "白酒Ⅱ", "period": "2026-06-30", "deadline": "2026-08-31",
        "members": 11, "samples": 11,
        "rev_yoy_med": -15.34, "rev_yoy_prev_med": -11.24,
        "gm_med": 73.35, "gm_yoy_med": -1.67, "gm_samples": 11,
        "company_revenue": 9.23e10, "total_revenue": 1.80e11, "company_share": 0.514,
    }
    base.update(over)
    return base


def test_industry_fields_produce_formulas():
    out = {f.field_id: f for f in proxies.industry_fields(_industry(), "2026-10-23")}
    assert set(out) == {"industry.industry_growth", "industry.price_trend",
                        "industry.stage", "industry.concentration"}
    # 增速是数值型，直接交给量规引擎套既有分档
    assert out["industry.industry_growth"].value == pytest.approx(-15.34)
    # 51% 份额 = 龙头卡位
    assert out["industry.concentration"].value == 5
    # 负增长 → 衰退期
    assert out["industry.stage"].value == 1
    for f in out.values():
        assert f.formula and f.note, f"{f.field_id} 缺公式或误差说明"


def test_industry_price_trend_weights_price_over_volume():
    # 价：毛利率同比 -1.67pp → 2 档；量：营收 -15.3% → 1 档；0.6/0.4 合成 → 2
    out = {f.field_id: f for f in proxies.industry_fields(_industry(), "2026-10-23")}
    assert out["industry.price_trend"].value == 2


def test_industry_stage_downgrades_on_sharp_slowdown():
    fast_but_fading = _industry(rev_yoy_med=25.0, rev_yoy_prev_med=45.0)
    out = {f.field_id: f for f in proxies.industry_fields(fast_but_fading, "2026-10-23")}
    # 25% 增速本应 5 档，但较上期 -20pp 快速回落 → 降 1 档
    assert out["industry.stage"].value == 4


def test_industry_fields_empty_when_no_peers():
    assert proxies.industry_fields({}, "2026-10-23") == []
    assert proxies.industry_fields(_industry(samples=3, members=3), "2026-10-23") == []


def test_industry_fields_tolerate_missing_gm():
    """毛利率取不到时，量价只剩「量」可用，不能把缺失当 0 分。"""
    out = {f.field_id: f for f in proxies.industry_fields(
        _industry(gm_yoy_med=None, gm_samples=0), "2026-10-23")}
    assert "industry.price_trend" in out
    assert out["industry.price_trend"].value == 1  # 仅按量：-15.3% → 1 档


# ---------------------------------------------------------------- 公司

def _reports(gm=(39.0, 39.5, 40.0, 38.5, 39.0), roic=(20.0, 22.0, 21.0, 19.0, 23.0)):
    out = []
    for i, (g, r) in enumerate(zip(gm, roic)):
        out.append({"report_date": f"{2025 - i}-12-31", "gm": g, "roic": r})
    return out


def test_moat_combines_three_subfactors():
    out = {f.field_id: f for f in proxies.company_fields(
        reports=_reports(), latest={"gm": 89.55, "report_date": "2026-06-30"},
        industry=_industry(), pledge={"ratio": 0.06, "trade_date": "2026-10-16",
                                      "industry": "白酒Ⅱ"},
        dividends=[], latest_notice="2026-08-15", asof="2026-10-23")}
    moat = out["company.moat"]
    # 溢价 16.2pp → 5；标准差 0.55 → 5；ROIC 五年全 >9% → 5
    assert moat.value == 5
    assert "毛利率溢价" in moat.formula and "ROIC" in moat.formula
    assert "低估" in moat.note, "误差方向必须写清"


def test_moat_survives_without_peer_data():
    """没有同业数据时，稳定性与 ROIC 分项仍可算，按权重重归一化而不是整项放弃。"""
    out = {f.field_id: f for f in proxies.company_fields(
        reports=_reports(), latest={"gm": 89.55, "report_date": "2026-06-30"},
        industry=None, pledge=None, dividends=[], latest_notice="2026-08-15",
        asof="2026-10-23")}
    assert out["company.moat"].value == 5
    assert "重归一化" in out["company.moat"].formula


def test_moat_none_when_nothing_available():
    out = proxies.company_fields(reports=[], latest={}, industry=None, pledge=None,
                                 dividends=[], latest_notice="", asof="2026-10-23")
    assert [f for f in out if f.field_id == "company.moat"] == []


def test_pledge_is_observed_not_proxy():
    out = {f.field_id: f for f in proxies.company_fields(
        reports=[], latest={}, industry=None,
        pledge={"ratio": 42.0, "trade_date": "2026-10-16", "industry": "白酒Ⅱ"},
        dividends=[], latest_notice="", asof="2026-10-23")}
    assert out["company.pledge_pct"].basis == "observed"
    assert out["company.pledge_pct"].value == 42.0
    assert out["company.pledge_pct"].confidence == "high"


def test_mgmt_record_penalizes_high_pledge():
    divs = [{"report_date": f"{y}-12-31", "status": "实施分配", "cash_per_10": 10.0}
            for y in (2021, 2022, 2023, 2024, 2025)]
    low = {f.field_id: f for f in proxies.company_fields(
        reports=[], latest={}, industry=None,
        pledge={"ratio": 0.0, "trade_date": "2026-10-16"}, dividends=divs,
        latest_notice="", asof="2026-10-23")}
    high = {f.field_id: f for f in proxies.company_fields(
        reports=[], latest={}, industry=None,
        pledge={"ratio": 60.0, "trade_date": "2026-10-16"}, dividends=divs,
        latest_notice="", asof="2026-10-23")}
    assert low["company.mgmt_record"].value == 5
    assert high["company.mgmt_record"].value < low["company.mgmt_record"].value
    assert "未覆盖违规" in low["company.mgmt_record"].note


def test_logic_strength_is_never_proxied():
    """预期差是研究结论本身，代理层不得触碰。"""
    out = proxies.company_fields(reports=_reports(), latest={"gm": 50.0}, industry=_industry(),
                                 pledge=None, dividends=[], latest_notice="", asof="2026-10-23")
    assert "company.logic_strength" not in {f.field_id for f in out}
