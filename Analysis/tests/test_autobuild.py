"""自动装配（RawStock → 分析输入）的单元测试。

重点不在"能不能跑通"，而在三件容易悄悄错掉的事：
1. PIT：报告未披露时必须回退，绝不能偷看下一期；
2. TTM：加总法与年化法必须分清，年化要如实标注；
3. 覆盖闸门：只填了部分维度时不得给出正式分桶。

全部用手写 fixture，不访问网络。
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from stockframe import autobuild
from stockframe.engine import run
from stockframe.sources.base import SourceError

ASOF = "2026-10-23"


def _rep(period: str, notice: str, revenue: float, np_: float, eps: float,
         ocf: float, equity: float, gm: float, rev_yoy: float, np_yoy: float,
         roic: float = 20.0, op_profit: float = 40.0, op_income: float = 200.0) -> dict:
    # 入参单位为「亿元」，内部统一换算成「元」（与真实数据源一致）
    E = 1e8
    return {
        "report_date": period, "notice_date": notice, "report_type": "中报" if period[5:] == "06-30" else "年报",
        "revenue": revenue * E, "np": np_ * E, "eps": eps, "ocf": ocf * E, "equity": equity * E,
        "total_assets": equity * 1.5 * E, "roe": 15.0, "roic": roic, "gm": gm,
        "rev_yoy": rev_yoy, "np_yoy": np_yoy,
        "op_margin": op_profit / op_income * 100.0, "tax_rate": 0.25,
    }


def raw_fixture() -> dict:
    reports = [
        _rep("2026-06-30", "2026-08-15", 1200.0, 240.0, 2.40, 260.0, 1600.0, 40.0, 8.0, 6.0),
        _rep("2026-03-31", "2026-04-25", 600.0, 120.0, 1.20, 130.0, 1520.0, 39.5, 7.0, 5.0),
        _rep("2025-12-31", "2026-04-17", 2200.0, 440.0, 4.40, 470.0, 1500.0, 39.0, 9.0, 8.0),
        _rep("2025-06-30", "2025-08-20", 1110.0, 226.0, 2.26, 240.0, 1400.0, 38.5, 6.0, 4.0),
        _rep("2024-12-31", "2025-04-10", 2000.0, 400.0, 4.00, 410.0, 1300.0, 38.0, 5.0, 3.0),
        _rep("2023-12-31", "2024-04-10", 1800.0, 360.0, 3.60, 370.0, 1200.0, 37.0, 5.0, 3.0),
        _rep("2022-12-31", "2023-04-10", 1600.0, 320.0, 3.20, 330.0, 1100.0, 36.0, 5.0, 3.0),
    ]
    start = date(2024, 1, 1)
    hist = []
    for i in range(700):
        d = start + timedelta(days=i)
        hist.append({"date": d.isoformat(), "pe": 15.0 + (i % 100) * 0.1,
                     "pb": 2.0 + (i % 50) * 0.01, "close": 50.0 + (i % 200) * 0.05,
                     "mcap": 1.2e11, "shares": 1.0e9})
    kline = []
    for i in range(300):
        d = date(2025, 9, 1) + timedelta(days=i)
        kline.append({"date": d.isoformat(), "close": 60.0 + i * 0.02, "high": 61.0 + i * 0.02,
                      "low": 59.0, "volume": 1.0e6 + (i % 10) * 1.0e4,
                      "amount": 6.0e7, "turnover": 1.2})
    return {
        "stock": {"code": "000001", "name": "测试股份", "exchange": "SZ", "secid": "0.000001", "board": "深A"},
        "quote": {"price": hist[-1]["close"], "trade_date": hist[-1]["date"]},
        "reports": reports,
        "balance": {"report_date": "2026-06-30", "notice_date": "2026-08-15", "monetaryfunds": 5.0e10,
                    "short_loan": 2.0e9, "long_loan": 1.0e9, "bond_payable": 0.0,
                    "noncurrent_liab_1y": 1.0e8, "goodwill": 2.0e9,
                    "total_assets": 2.4e11, "total_liab": 8.0e10, "parent_equity": 1.6e11},
        "valuation_hist": hist,
        "kline": kline,
        "meta": {"provider": "fixture", "fetched_at": "2026-10-23T08:00:00+08:00", "endpoints": []},
    }


# ---------------------------------------------------------------- PIT

def test_latest_report_must_be_announced():
    """2026-08-10 时点：中报（8-15 披露）尚不可得，必须回退到一季报。"""
    out = autobuild.build(raw_fixture(), "2026-08-10")
    assert out["fill"]["as_of"]["latest_report_period"] == "2026-03-31"
    assert out["fill"]["as_of"]["latest_report_notice"] == "2026-04-25"
    # 用一季报 + 2025 年报 - 2025 一季报做 TTM；2025Q1 不在 fixture 里 → 年化近似
    assert any(d["method"] == "annualized" for d in out["fill"]["derivations"])


def test_no_announced_report_raises():
    raw = raw_fixture()
    for r in raw["reports"]:
        r["notice_date"] = "2027-01-01"
    with pytest.raises(SourceError, match="无已披露定期报告") as exc:
        autobuild.build(raw, ASOF)
    assert exc.value.code == "NO_PIT_DATA"


def test_future_analysis_date_rejected():
    with pytest.raises(SourceError, match="晚于当前日期") as exc:
        autobuild.build(raw_fixture(), "2099-01-01")
    assert exc.value.code == "BAD_DATE"


def test_valuation_hist_truncated_to_asof():
    """估值历史与价格必须按分析时点截断，不能取到 asof 之后的行情。"""
    out = autobuild.build(raw_fixture(), "2026-03-01")
    px = out["input"]["data"]["market"]["price_close"]
    assert px["as_of"] <= "2026-03-01"
    assert px["available_at"] <= "2026-03-01"


# ---------------------------------------------------------------- TTM 口径

def test_ttm_additive_when_prior_period_present():
    out = autobuild.build(raw_fixture(), ASOF)
    # EPS TTM = 本期 + 上年年报 - 上年同期 = 2.40 + 4.40 - 2.26
    assert out["input"]["data"]["valuation"]["eps_ttm"]["value"] == pytest.approx(2.40 + 4.40 - 2.26, abs=0.02)
    d = {x["field_id"]: x for x in out["fill"]["derivations"]}
    assert d["valuation.eps_ttm"]["method"] == "ttm"
    assert "−" in d["valuation.eps_ttm"]["formula"]


def test_annualized_ttm_is_labelled():
    """2026-08-10 只能用一季报，且缺上年同期 → 退化为年化，并如实标注。"""
    out = autobuild.build(raw_fixture(), "2026-08-10")
    eps = out["input"]["data"]["valuation"]["eps_ttm"]["value"]
    assert eps == pytest.approx(1.20 * 12 / 3, abs=0.02)
    deriv = {x["field_id"]: x for x in out["fill"]["derivations"]}
    assert deriv["valuation.eps_ttm"]["method"] == "ttm"
    assert "年化近似" in deriv["valuation.eps_ttm"]["formula"]


# ---------------------------------------------------------------- 分位与交易面

def test_percentile_of_midrank():
    assert autobuild.percentile_of([1, 2, 3, 4], 2.5) == 50.0
    assert autobuild.percentile_of([1, 2, 3, 4], 4) == 87.5
    assert autobuild.percentile_of([5, 5, 5], 5) == 50.0


def test_percentile_needs_enough_history():
    raw = raw_fixture()
    raw["valuation_hist"] = raw["valuation_hist"][-100:]  # 样本不足
    out = autobuild.build(raw, ASOF)
    assert "market.pctile_pe_10y" not in out["input"]["data"]["market"]


def test_trading_metrics_from_kline_and_fallback():
    raw = raw_fixture()
    tm = autobuild.trading_metrics(raw["kline"], date.fromisoformat(ASOF))
    assert tm["dist_ma250"] is not None and tm["vol_ratio_20d"] is not None
    fb = autobuild.trading_from_closes(raw["valuation_hist"], date.fromisoformat(ASOF))
    assert fb["drawdown_52w"] is not None
    # K 线缺失时，均线与回撤仍可算，换手/量比则缺失（不补零）
    raw["kline"] = []
    out = autobuild.build(raw, ASOF)
    market = out["input"]["data"]["market"]
    assert "market.dist_ma250" in [f"market.{k}" for k in market]
    assert "turnover_pct" not in market


# ---------------------------------------------------------------- 输入结构与风险

def test_generated_input_passes_structure_validation():
    from stockframe.datadict import validate_structure
    out = autobuild.build(raw_fixture(), ASOF)
    validate_structure(out["input"])
    assert len(out["input"]["risks"]) >= 3
    assert len(out["input"]["catalysts"]) >= 2


def test_catalysts_only_statutory_deadlines():
    out = autobuild.build(raw_fixture(), ASOF)
    for c in out["input"]["catalysts"]:
        assert c["direction"] == "?" and c["priced_in"] == "?"
        assert "法定截止" in c["event"]
        assert c["date"] > ASOF


def test_overrides_merge_judgement():
    out = autobuild.build(raw_fixture(), ASOF, overrides={"data": {"company": {
        "moat": {"value": 5, "as_of": ASOF, "available_at": ASOF,
                 "source": "研究判断", "source_tier": "L3"}}}})
    assert out["input"]["data"]["company"]["moat"]["value"] == 5


# ---------------------------------------------------------------- 覆盖闸门

def test_coverage_gate_blocks_premature_bucket():
    """只填了财务/行情、公司层只有护城河代理时，不得给入池/观察，必须判人工复核。"""
    out = autobuild.build(raw_fixture(), ASOF)
    report = run(out["input"])
    cov = report["scorecard"]["evidence_coverage"]
    assert cov["ratio"] < cov["threshold"]
    # 宏观与行业完全缺失；公司层因年报刊可算护城河的稳定性/ROIC 分项，只算部分覆盖
    assert {d["id"] for d in cov["missing_dimensions"]} == {"macro", "industry"}
    assert 0.0 < cov["by_dimension"]["company"] < 1.0
    assert cov["total_ratio"] > cov["ratio"], "全维度覆盖应高于研究判断层覆盖（含财务/行情）"
    assert report["conclusion"]["bucket_mid"] == "人工复核"
    assert report["conclusion"]["decision"]["rule_id"] == "R7_COVERAGE"
    assert "研究判断层" in report["conclusion"]["decision"]["reasons"][0]


def test_full_coverage_restores_normal_bucket():
    out = autobuild.build(raw_fixture(), ASOF)
    inp = copy.deepcopy(out["input"])
    fill = {
        "macro": {"m1_yoy": 4.0, "rate_10y_chg_yoy": -10, "pmi": 50.2, "policy_stance": 3},
        "industry": {"industry_growth": 6.0, "price_trend": 1, "stage": 4, "concentration": 4, "penetration_space": 3},
        "company": {"moat": 4, "mgmt_record": 4, "logic_strength": 3, "logic_essence": "测试",
                    "pledge_pct": 0.0, "customer_concentration": 10.0},
    }
    for g, fields in fill.items():
        inp["data"].setdefault(g, {})
        for k, v in fields.items():
            inp["data"][g][k] = {"value": v, "as_of": ASOF, "available_at": ASOF,
                                 "source": "研究判断（测试）", "source_tier": "L3"}
    report = run(inp)
    assert report["scorecard"]["evidence_coverage"]["ratio"] >= 0.6
    assert report["conclusion"]["decision"]["rule_id"] != "R7_COVERAGE"


def test_dcf_assumptions_within_design_bands():
    out = autobuild.build(raw_fixture(), ASOF)
    for name, a in out["input"]["assumptions"]["dcf"].items():
        assert 0.08 <= a["wacc"] <= 0.12, name
        assert 0.015 <= a["terminal_growth"] <= 0.03, name
        assert len(a["revenue_growth_path"]) == 5 and len(a["ebit_margin_path"]) == 5
        assert a["revenue_base"] > 0 and a["shares"] > 0


def test_infeasible_mechanical_dcf_is_rejected_not_tuned():
    """机械外推的 FCFF 现值覆盖不了净债务时，必须明确拒绝，不得调参硬凑。"""
    raw = raw_fixture()
    raw["balance"]["short_loan"] = 2.0e13  # 2 万亿元有息负债，远非 FCFF 现值所能覆盖
    with pytest.raises(SourceError) as exc:
        autobuild.build(raw, ASOF)
    assert exc.value.code == "DCF_INFEASIBLE"
    msg = str(exc.value)
    assert "人工设定 DCF 假设" in msg and "调参硬凑" in msg


def test_zero_revenue_company_rejected():
    raw = raw_fixture()
    for r in raw["reports"]:
        r["revenue"] = 0.0
    with pytest.raises(SourceError) as exc:
        autobuild.build(raw, ASOF)
    assert exc.value.code in ("NO_DCF_INPUT", "NO_RISK_INPUT")


# ---------------------------------------------------------------- 覆盖度对账

def test_field_ledger_reconciles_with_dictionary():
    """已填 + 待补 必须恰好等于数据字典字段数。

    取数降级（如行情主机限流丢 K 线）时，丢掉的字段过去既不进 filled 也不进 pending，
    界面上"N/39 + 待补"就对不上账，缺口被悄悄藏起来。这里把它钉住。
    """
    from stockframe.datadict import FIELD_REGISTRY

    cases = {
        "完整": lambda r: r,
        "缺K线": lambda r: {k: v for k, v in r.items() if k != "kline"},
        "缺宏观": lambda r: {**r, "macro": None},
        "缺行业": lambda r: {**r, "industry": None},
    }
    for name, mutate in cases.items():
        f = autobuild.build(mutate(raw_fixture()), ASOF)["fill"]
        pend = [x for g in f["pending"] for x in g["fields"]]
        assert len(pend) == len(set(pend)), f"{name}: 待补清单有重复"
        assert not set(f["filled"]) & set(pend), f"{name}: 同一字段既已填又待补"
        assert f["filled_count"] + len(pend) == f["total_fields"] == len(list(FIELD_REGISTRY)), \
            f"{name}: 对账不平（{f['filled_count']} + {len(pend)} ≠ {f['total_fields']}）"


def test_degraded_source_fields_are_listed_as_source_gaps():
    """取数失败丢掉的字段，必须出现在 kind=source 的待补分组里，而不是凭空消失。"""
    raw = raw_fixture()
    raw.pop("kline")
    f = autobuild.build(raw, ASOF)["fill"]
    source_gaps = {x for g in f["pending"] if g["kind"] == "source" for x in g["fields"]}
    assert {"market.turnover_pct", "market.vol_ratio_20d"} <= source_gaps
    assert "market.price_close" in f["filled"], "价格仍在，不能把整层都判成缺失"


def test_auxiliary_fields_never_enter_the_ledger():
    """不在数据字典里的展示型字段（如总市值）不得混进 fill 账本，否则 N/39 失真。"""
    from stockframe.datadict import FIELD_REGISTRY

    out = autobuild.build(raw_fixture(), ASOF)
    reg = set(FIELD_REGISTRY)
    assert set(out["fill"]["filled"]) <= reg
    assert "market.market_cap" not in out["input"]["data"]["market"]


# ---------------------------------------------------------------- 新补的三类取数

def test_turnover_computed_from_free_shares_without_vendor_turnover():
    """兜底源不给换手率时用「成交量 ÷ 流通A股」现算；分母必须是流通口径。"""
    kline = [{"date": f"2026-09-{d:02d}", "close": 10.0, "high": 10.5, "low": 9.8,
              "volume": 1_000_000.0, "turnover": None} for d in range(1, 22)]
    tm = autobuild.trading_metrics(kline, date(2026, 10, 23), free_shares=50_000_000.0)
    assert tm["turnover_pct"] == pytest.approx(2.0)      # 100 万股 ÷ 5000 万股
    assert tm["turnover_method"] == "volume/free_shares"
    assert tm["vol_ratio_20d"] == pytest.approx(1.0)

    # 数据源自带换手率时以源为准，不覆盖
    kline[-1]["turnover"] = 0.34
    tm2 = autobuild.trading_metrics(kline, date(2026, 10, 23), free_shares=50_000_000.0)
    assert tm2["turnover_pct"] == 0.34 and tm2["turnover_method"] == "vendor"


def test_wc_growth_gap_uses_same_period_last_year():
    """应收+存货增速差必须与「上年同期」比，不能与上一季比。"""
    raw = raw_fixture()
    raw["balance"]["periods"] = [
        {"report_date": "2026-06-30", "notice_date": "2026-08-15",
         "accounts_rece": 2.0e9, "inventory": 5.0e10, "accounts_payable": 3.0e9},
        {"report_date": "2025-06-30", "notice_date": "2025-08-20",
         "accounts_rece": 1.6e9, "inventory": 4.4e10, "accounts_payable": 2.8e9},
    ]
    out = autobuild.build(raw, ASOF)
    gap = out["input"]["data"]["financial"]["wc_growth_gap"]
    # 营运资本 (20+500)/(16+440)-1 = +14.04%；营收同比 +8.0% → 差 +6.04pp
    assert gap["value"] == pytest.approx(6.04, abs=0.05)
    deriv = {x["field_id"]: x for x in out["fill"]["derivations"]}
    assert "2025-06-30" in deriv["financial.wc_growth_gap"]["note"]


def test_wc_growth_gap_absent_without_prior_year():
    out = autobuild.build(raw_fixture(), ASOF)   # 无 periods → 不算
    assert "wc_growth_gap" not in out["input"]["data"]["financial"]


def test_inst_hold_and_peer_pe_written_with_pit_dates():
    raw = raw_fixture()
    raw["free_holders"] = {
        "period": "2026-06-30", "notice": "2026-08-20", "org_ratio": 53.023, "org_count": 8,
        "prev_period": "2026-03-31", "prev_org_ratio": 54.447, "chg_pp": -1.424,
    }
    raw["peer_val"] = {"trade_date": "2026-10-22", "board": "016074", "board_name": "电池",
                       "median_pe": 31.75, "samples": 81, "own_pe": 11.63}
    out = autobuild.build(raw, ASOF)
    inst = out["input"]["data"]["market"]["inst_hold_chg_pct"]
    assert inst["value"] == pytest.approx(-1.42)
    assert inst["available_at"] == "2026-08-20"     # 用披露日，不用报告期末
    peer = out["input"]["data"]["valuation"]["peer_pe_median"]
    assert peer["value"] == pytest.approx(31.75)
    assert peer["as_of"] == "2026-10-22"



def test_rate_10y_fills_macro_and_leaves_pending():
    """中债 10Y 同比：填进 macro 维度，且不再出现在判断类待补清单里。"""
    raw = raw_fixture()
    raw["rate_10y"] = {
        "curve": "中债国债收益率曲线（10 年）", "value": 1.6414,
        "date": "2026-10-22", "available_at": "2026-10-23",
        "prev_value": 1.8293, "prev_date": "2025-10-22",
        "prev_offset_days": 365, "chg_bp": -18.8,
        "publish_lag_days": 1, "points": 259, "span_days": 430,
    }
    out = autobuild.build(raw, ASOF)
    assert out["input"]["data"]["macro"]["rate_10y_chg_yoy"]["value"] == pytest.approx(-18.8)

    pend = {f for g in out["fill"]["pending"] for f in g["fields"]}
    assert "macro.rate_10y_chg_yoy" not in pend

    deriv = {x["field_id"]: x for x in out["fill"]["derivations"]}
    assert "1.8293" in deriv["macro.rate_10y_chg_yoy"]["formula"]
    # 两个观测值相减，不算代理：既不标 proxy，也归在 observed 里
    assert out["fill"]["proxied"]["observed"].count("macro.rate_10y_chg_yoy") == 1
    assert "macro.rate_10y_chg_yoy" not in out["fill"]["proxied"]["proxy"]


def test_rate_10y_absent_stays_in_pending():
    """补源失败时该字段必须落进待补，不能被静默吞掉（账本对平）。"""
    out = autobuild.build(raw_fixture(), ASOF)
    pend = {f for g in out["fill"]["pending"] for f in g["fields"]}
    assert "macro.rate_10y_chg_yoy" in pend
    assert out["fill"]["filled_count"] + sum(len(g["fields"]) for g in out["fill"]["pending"]) \
        == out["fill"]["total_fields"]
