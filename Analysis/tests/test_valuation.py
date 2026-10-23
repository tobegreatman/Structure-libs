"""估值模块测试：DCF 手算对拍、TV 占比警示、分位带边界、区间合成。"""

import pytest

from stockframe.valuation import (
    DCFParams, percentile_band, run_dcf, run_relative, synthesize, value_per_share,
)


def _params(**kw):
    base = dict(
        wacc=0.10, terminal_growth=0.02,
        revenue_growth_path=(0.10, 0.10, 0.10, 0.10, 0.10),
        ebit_margin_path=(0.20, 0.20, 0.20, 0.20, 0.20),
        tax_rate=0.25, dep_amort_pct_rev=0.04, capex_pct_rev=0.06,
        nwc_pct_rev_delta=0.01, revenue_base=100.0, net_debt=50.0, shares=10.0,
    )
    base.update(kw)
    return DCFParams(**base)


def test_dcf_hand_computed():
    # FCFF_t = rev_t × 0.12；PV(显性) = 0.12×500 = 60；TV = 161.051×0.12×1.02/0.08
    # PV(TV) = 153.0；EV = 213；equity = 163；每股 = 16.3
    v, tv_share = value_per_share(_params())
    assert v == pytest.approx(16.30, abs=0.01)
    assert tv_share == pytest.approx(153.0 / 213.0, abs=0.002)


def test_tv_warning_fires():
    dcf = run_dcf({
        "base": {"wacc": 0.10, "terminal_growth": 0.02,
                 "revenue_growth_path": [0.10] * 5, "ebit_margin_path": [0.20] * 5,
                 "tax_rate": 0.25, "dep_amort_pct_rev": 0.04, "capex_pct_rev": 0.06,
                 "nwc_pct_rev_delta": 0.01, "revenue_base": 100, "net_debt": 50, "shares": 10},
        "optimistic": {"wacc": 0.09, "terminal_growth": 0.03,
                       "revenue_growth_path": [0.30, 0.25, 0.22, 0.18, 0.15],
                       "ebit_margin_path": [0.17, 0.185, 0.20, 0.21, 0.22],
                       "tax_rate": 0.15, "dep_amort_pct_rev": 0.045, "capex_pct_rev": 0.07,
                       "nwc_pct_rev_delta": 0.012, "revenue_base": 18.5, "net_debt": -2.5, "shares": 1.62},
        "pessimistic": {"wacc": 0.105, "terminal_growth": 0.02,
                        "revenue_growth_path": [0.12, 0.08, 0.06, 0.05, 0.04],
                        "ebit_margin_path": [0.15, 0.145, 0.14, 0.135, 0.13],
                        "tax_rate": 0.15, "dep_amort_pct_rev": 0.045, "capex_pct_rev": 0.06,
                        "nwc_pct_rev_delta": 0.01, "revenue_base": 18.5, "net_debt": -2.5, "shares": 1.62},
    })
    assert any("终值占比" in w for w in dcf["warnings"])
    assert set(dcf["scenario_results"]) == {"optimistic", "base", "pessimistic"}
    grid = dcf["sensitivity_grid_base"]
    assert len(grid["wacc_values"]) == 5 and len(grid["values"]) == 5


def test_wacc_g_guard():
    with pytest.raises(ValueError):
        value_per_share(_params(wacc=0.021, terminal_growth=0.02))


def test_percentile_band_boundaries():
    assert percentile_band(29.9) == "low"
    assert percentile_band(30) == "neutral"
    assert percentile_band(70) == "neutral"
    assert percentile_band(70.1) == "high"


def test_relative_implied_range():
    rel = run_relative("PE", current=28.5, pctile=42, hist_p25=22, hist_p75=35,
                       eps_ttm=1.14, peer_median=31, fwd_growth_pct=21.05)
    assert rel["implied_price_range"]["low"] == pytest.approx(25.08, abs=0.01)
    assert rel["implied_price_range"]["high"] == pytest.approx(39.9, abs=0.01)
    assert rel["band"] == "neutral"
    assert "不构成价格点位判断" in rel["priced_in_narrative"]


def test_relative_unavailable_when_inputs_missing():
    rel = run_relative("PE", current=None, pctile=42, hist_p25=22, hist_p75=35,
                       eps_ttm=1.14, peer_median=31, fwd_growth_pct=None)
    assert rel["available"] is False
    assert rel["implied_price_range"]["low"] is None
    assert "不补零" in rel["peer_anchor"]["note"] or "缺失" in rel["priced_in_narrative"]


def test_peer_anchor_gives_price_only_when_comparable():
    """同业锚只在倍数可比（0.5–2 倍）时折算价位；否则不折算，避免用不可比的锚倒推点位。"""
    near = run_relative("PE", current=28.5, pctile=42, hist_p25=22, hist_p75=35,
                        eps_ttm=1.14, peer_median=31, fwd_growth_pct=None)
    a = near["peer_anchor"]
    assert a["comparable"] is True
    assert a["implied_price"] == pytest.approx(35.34, abs=0.01)
    assert a["premium_pct"] == pytest.approx(-8.1, abs=0.1)

    far = run_relative("PE", current=11.6, pctile=0, hist_p25=22, hist_p75=35,
                       eps_ttm=1.14, peer_median=31.75, fwd_growth_pct=None)
    b = far["peer_anchor"]
    assert b["comparable"] is False
    assert b["implied_price"] is None, "倍数相差 2.7 倍时不得折算成单点价位"
    assert b["premium_pct"] == pytest.approx(-63.4, abs=0.2)
    assert "不可比" in b["note"] or "不给单点" in b["note"]

    # 同业数据缺失：锚不启用，但字段结构保持一致（不返回 None 让下游崩）
    none = run_relative("PE", current=28.5, pctile=42, hist_p25=22, hist_p75=35,
                        eps_ttm=1.14, peer_median=None, fwd_growth_pct=None)
    assert none["peer_anchor"]["implied_price"] is None
    assert none["peer_anchor"]["comparable"] is False


def test_synthesis_intersection_and_union():
    dcf_base = {"value_per_share_low": 33.13, "value_per_share_high": 44.43}
    rel = {"implied_price_range": {"low": 25.08, "high": 39.9}}
    s1 = synthesize(dcf_base, rel, price=32.5, dcf_warnings=[])
    assert s1["mode"] == "intersection"
    assert s1["combined_range"] == {"low": 33.13, "high": 39.9}
    assert s1["confidence_level"] == "high"

    rel2 = {"implied_price_range": {"low": 45.0, "high": 50.0}}
    s2 = synthesize(dcf_base, rel2, price=32.5, dcf_warnings=["x"])
    assert s2["mode"] == "union"
    assert s2["confidence_level"] == "low"
    assert "方法分歧" in s2["note"]
