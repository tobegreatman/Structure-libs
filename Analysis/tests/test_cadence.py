"""披露节奏 vs 数据时效：两把尺子不能混。

用户问："最新可得报告为 2026-09-30（三季报），非完整年度——这样是不是只有极短时间
才可能看到相关数据？"答案是：按原先的实现，确实会。因为两处把"节奏"写错了：

1. `company.customer_concentration` 标成 92 天（季频）。它其实只在年报附注披露，
   一年一次。用 92 天去卡，年报公布三个月后就被判"超期"——只有 4~7 月那几个月算新鲜。
2. `valuation.eps_ttm` 标成 1 天（日频）。它的分子来自定期报告，季报公布两个月就
   被判"超期 2 倍"，而它其实是最新的。

这两处都不是"取不到"，而是**用错了衡量周期**。修法不是放宽阈值，而是按字段真实的
披露节奏衡量时效，并让"只在年报披露"的字段有独立的时点基准（最近一期年报期末 +
该年报公告日），不跟着最新季报走。
"""

from __future__ import annotations

from stockframe import autobuild
from stockframe.datadict import FIELD_SPECS
from stockframe.engine import run

from test_autobuild import raw_fixture

ASOF = "2026-10-23"
FID = "company.customer_concentration"
SPECS = {s.field_id: s for s in FIELD_SPECS}


def _build():
    return autobuild.build(raw_fixture(), ASOF)


def test_annual_fields_use_annual_cadence():
    """「只在年报披露」的字段必须同时满足：annual 标记 + 周期 ≥ 一年。"""
    annual = [s for s in FIELD_SPECS if s.annual]
    assert [s.field_id for s in annual] == [FID], "目前只有客户集中度是年报附注口径"
    for s in annual:
        assert s.update_days >= 365, (
            f"{s.field_id} 一年只披露一次，周期却写成 {s.update_days} 天——"
            "季频阈值会让它在年报公布三个月后就被判超期")


def test_periodic_fields_are_not_day_frequency():
    """定期报告口径的字段不能按日频衡量，否则季报刚公布就算超期。"""
    for fid in ("valuation.eps_ttm", "financial.roe_ttm", "financial.rev_growth"):
        assert SPECS[fid].update_days >= 90, f"{fid} 按日频衡量会误判为超期"
        assert not SPECS[fid].annual
    assert SPECS["market.price_close"].update_days == 1, "行情才是真日频"


def test_latest_annual_is_reported_separately_from_latest_period():
    """取数结果要同时给两把尺子：最新报告期（季）与最近一期已公告年报。"""
    f = _build()["fill"]
    assert f["as_of"]["latest_report_period"][5:] != "12-31", "本用例构造的是季报时点"
    assert f["as_of"]["latest_annual_period"].endswith("12-31")
    assert f["as_of"]["latest_annual_notice"] <= ASOF, "年报公告日不得晚于分析时点"
    assert f["annual_fields"] == [FID]


def test_annual_field_filled_with_report_period_is_not_stale():
    """按年报口径填（数据期=年报期末、可获得=年报公告日）不应被判为超期。"""
    out = _build()
    inp = out["input"]
    ao = out["fill"]["as_of"]
    inp["data"].setdefault("company", {})["customer_concentration"] = {
        "value": 18.5, "as_of": ao["latest_annual_period"],
        "available_at": ao["latest_annual_notice"], "period_end": ao["latest_annual_period"],
        "source": "人工研究判断（界面录入）", "source_tier": "L3",
    }
    dq = run(inp)["data_quality"]
    assert not any(g["field"] == FID for g in dq["gaps"]), "填了就不该还在缺口里"
    # 注意：fixture 的行情时点故意固定在 old 日期，整体 timeliness 本就是 0.6，
    # 所以这里只针对该字段——它绝不该出现在超期项里。
    stale = next((n for n in dq["notes"] if n.startswith("数据超期项")), "")
    assert FID not in stale, f"年报口径被判超期：{stale}"
    assert any("只在年报披露" in n and FID in n for n in dq["notes"]), \
        "数据质量说明里要讲清这条不是最新季报的数"


def test_annual_note_absent_when_field_not_filled():
    """没填时不该出现"年报口径"备注——没数据就不用解释数据期。"""
    dq = run(_build()["input"])["data_quality"]
    assert not any("只在年报披露" in n for n in dq["notes"])
