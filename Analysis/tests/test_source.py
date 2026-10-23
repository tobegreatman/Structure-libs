"""数据源适配器测试：用注入的假 HTTP，不访问网络。

验证点：
- 检索结果只保留 A 股，且正确解析 secid；
- 财务/估值/K 线字段归一化正确（日期截断、空值保留为 None）；
- 可选数据（K 线）失败时降级而不是整体失败；
- 数据源返回非 JSON 时明确报 BAD_RESPONSE。
"""

from __future__ import annotations

import json

import pytest

from stockframe.sources import EastmoneyProvider, SourceError, StockRef
from stockframe.sources.eastmoney import _day, _num


def _payload(rows):
    return {"result": {"pages": 1, "data": rows}}


FIN_ROW = {
    "SECUCODE": "600519.SH", "SECURITY_CODE": "600519",
    "REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-15 00:00:00",
    "REPORT_TYPE": "中报", "TOTALOPERATEREVE": 9.2e10, "PARENTNETPROFIT": 4.45e10,
    "EPSJB": 35.57, "NETCASH_OPERATE_PK": 7.06e10, "TOTAL_EQUITY_PK": 2.62e11,
    "TOTAL_ASSETS_PK": 3.09e11, "ROEJQ": 16.75, "ROIC": 16.35, "XSMLL": 89.55,
    "TOTALOPERATEREVETZ": 1.3, "PARENTNETPROFITTZ": -1.95,
    "OPERATE_PROFIT_PK": 6.1e10, "OPERATE_INCOME_PK": 9.07e10, "TAXRATE": 25.07,
}
BAL_ROW = {
    "REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-15 00:00:00",
    "MONETARYFUNDS": 5.35e10, "SHORT_LOAN": None, "LONG_LOAN": None, "BOND_PAYABLE": None,
    "NONCURRENT_LIAB_1YEAR": 5.7e7, "GOODWILL": None,
    "TOTAL_ASSETS": 3.09e11, "TOTAL_LIABILITIES": 4.7e10, "TOTAL_PARENT_EQUITY": 2.51e11,
    "ACCOUNTS_RECE": 2.0e9, "INVENTORY": 5.0e10, "ACCOUNTS_PAYABLE": 3.0e9,
}
BAL_PREV_ROW = {   # 上年同期：应收 16 亿、存货 44 亿 → 营运资本 60 亿，本期 52 亿
    "REPORT_DATE": "2025-06-30 00:00:00", "NOTICE_DATE": "2025-08-20 00:00:00",
    "MONETARYFUNDS": 4.8e10, "SHORT_LOAN": None, "LONG_LOAN": None, "BOND_PAYABLE": None,
    "NONCURRENT_LIAB_1YEAR": 5.0e7, "GOODWILL": None,
    "TOTAL_ASSETS": 2.9e11, "TOTAL_LIABILITIES": 4.4e10, "TOTAL_PARENT_EQUITY": 2.4e11,
    "ACCOUNTS_RECE": 1.6e9, "INVENTORY": 4.4e10, "ACCOUNTS_PAYABLE": 2.8e9,
}
VAL_ROWS = [
    {"TRADE_DATE": "2026-10-22 00:00:00", "PE_TTM": 20.24, "PB_MRQ": 6.56,
     "CLOSE_PRICE": 1318.49, "TOTAL_MARKET_CAP": 1.65e12, "TOTAL_SHARES": 1.25e9,
     "FREE_SHARES_A": 1.2e9, "BOARD_CODE": "BK0477", "BOARD_NAME": "酿酒行业"},
    {"TRADE_DATE": "2026-10-21 00:00:00", "PE_TTM": 20.1, "PB_MRQ": 6.5,
     "CLOSE_PRICE": 1309.69, "TOTAL_MARKET_CAP": 1.64e12, "TOTAL_SHARES": 1.25e9,
     "FREE_SHARES_A": 1.2e9, "BOARD_CODE": "BK0477", "BOARD_NAME": "酿酒行业"},
]
# 同业估值：同一 BOARD_CODE + 同一交易日，含 1 家亏损（PE 为负，必须剔除）
PEER_VAL_ROWS = [
    {"SECURITY_CODE": c, "SECURITY_NAME_ABBR": c, "PE_TTM": pe}
    for c, pe in [("600519", 20.2), ("000858", 18.0), ("600809", 25.0),
                  ("000568", 30.0), ("600779", 40.0), ("603369", 15.0),
                  ("000596", -8.0)]
]
# 十大流通股东：两期，机构合计 3.0% → 4.2%（+1.2pp）；个人股东不计入
FREEHOLD_ROWS = (
    [{"SECURITY_CODE": "600519", "END_DATE": "2026-06-30 00:00:00",
      "UPDATE_DATE": "2026-08-20 00:00:00", "HOLDER_NAME": f"机构{i}",
      "HOLD_RATIO": 1.4, "FREE_HOLDNUM_RATIO": 1.4, "IS_HOLDORG": "1"} for i in range(3)]
    + [{"SECURITY_CODE": "600519", "END_DATE": "2026-06-30 00:00:00",
        "UPDATE_DATE": "2026-08-20 00:00:00", "HOLDER_NAME": "个人甲",
        "HOLD_RATIO": 0.6, "FREE_HOLDNUM_RATIO": 0.6, "IS_HOLDORG": "0"}]
    + [{"SECURITY_CODE": "600519", "END_DATE": "2026-03-31 00:00:00",
        "UPDATE_DATE": "2026-04-25 00:00:00", "HOLDER_NAME": f"机构{i}",
        "HOLD_RATIO": 1.0, "FREE_HOLDNUM_RATIO": 1.0, "IS_HOLDORG": "1"} for i in range(3)]
)
# 新浪日线：只有 OHLCV，没有换手率（换手率要由流通股本现算）
SINA_ROWS = [{"day": f"2026-09-{d:02d}", "open": "1300", "high": "1330", "low": "1290",
              "close": "1318", "volume": "42000"} for d in range(1, 29)]
# 接口只返回请求的字段，顺序即 fields2 的顺序：日期,收盘,最高,最低,成交量,成交额,换手率
KLINE = "2026-10-22,1318.49,1322.00,1296.00,42491,5576216948.00,0.34"

# ---- 判断层夹具：注意 2026-10 的 M1/PMI 在 asof=2026-10-23 时尚未发布，必须被跳过
M1_ROWS = [
    {"REPORT_DATE": "2026-10-01 00:00:00", "TIME": "2026年10月份", "CURRENCY_SAME": 4.0},
    {"REPORT_DATE": "2026-09-01 00:00:00", "TIME": "2026年09月份", "CURRENCY_SAME": 3.2},
    {"REPORT_DATE": "2026-08-01 00:00:00", "TIME": "2026年08月份", "CURRENCY_SAME": 4.1},
]
PMI_ROWS = [
    {"REPORT_DATE": "2026-10-01 00:00:00", "TIME": "2026年10月份", "MAKE_INDEX": 50.5},
    {"REPORT_DATE": "2026-09-01 00:00:00", "TIME": "2026年09月份", "MAKE_INDEX": 50.1},
]
PLEDGE_ROWS = [
    # 晚于 asof 的一期必须先被排除
    {"SECURITY_CODE": "600519", "TRADE_DATE": "2026-10-30 00:00:00", "PLEDGE_RATIO": 9.9,
     "INDUSTRY": "白酒Ⅱ", "INDUSTRY_CODE": "1277", "PAYYEAR": "2026-10"},
    {"SECURITY_CODE": "600519", "TRADE_DATE": "2026-10-16 00:00:00", "PLEDGE_RATIO": 0.06,
     "INDUSTRY": "白酒Ⅱ", "INDUSTRY_CODE": "1277", "PAYYEAR": "2026-10"},
]
PEER_CODES = ["600519", "000858", "600809", "000568", "600779", "603369", "000596", "603589"]
PEER_ROWS = [{"SECURITY_CODE": c, "SECURITY_NAME_ABBR": c, "TRADE_DATE": "2026-10-16 00:00:00",
              "INDUSTRY_CODE": "1277"} for c in PEER_CODES]
# 报告期 -> {代码: (营收, 同比, 毛利率)}
PERF_CUR = {
    "600519": (9.23e10, 1.3, 89.55), "000858": (2.84e10, 20.9, 80.29),
    "600809": (2.10e10, -12.2, 75.40), "000568": (1.05e10, -36.4, 85.35),
    "600779": (1.08e9, -27.8, 76.13), "603369": (6.60e9, 2.0, 73.00),
    "000596": (1.30e9, -10.0, 78.00), "603589": (2.20e9, 5.0, 74.00),
}
PERF_PREV = {
    "600519": (9.11e10, 9.2, 90.20), "000858": (2.35e10, 5.0, 81.00),
    "600809": (2.39e10, 5.4, 76.50), "000568": (1.65e10, 3.0, 86.00),
    "600779": (1.50e9, -5.0, 77.00), "603369": (6.47e9, 4.0, 73.50),
    "000596": (1.44e9, 6.0, 78.50), "603589": (2.10e9, 7.0, 74.50),
}
DIVIDEND_ROWS = [
    {"SECURITY_CODE": "600519", "REPORT_DATE": f"{y}-12-31 00:00:00",
     "NOTICE_DATE": f"{y + 1}-06-20 00:00:00", "ASSIGN_PROGRESS": "实施分配",
     "PRETAX_BONUS_RMB": 200.0, "IMPL_PLAN_PROFILE": "10派200元(含税)"}
    for y in (2021, 2022, 2023, 2024, 2025)
]


def _perf_rows(period: str):
    table = PERF_CUR if period == "2026-06-30" else PERF_PREV
    return [{"SECURITY_CODE": c, "SECURITY_NAME_ABBR": c, "REPORTDATE": period + " 00:00:00",
             "TOTAL_OPERATE_INCOME": rev, "YSTZ": yoy, "XSMLL": gm, "WEIGHTAVG_ROE": 10.0}
            for c, (rev, yoy, gm) in table.items()]


def _p(http):
    """单测统一关闭中债补源（避免联网），中债另有专门用例注入替身。"""
    return EastmoneyProvider(http=http, bond=False)


def fake_http(kline_ok: bool = True, sina_ok: bool = True):
    def _http(url: str):
        import urllib.parse as _up
        plain = _up.unquote_plus(url)
        if "RPT_F10_FINANCE_MAINFINADATA" in url:
            return _payload([FIN_ROW])
        if "RPT_F10_FINANCE_GBALANCE" in url:
            return _payload([BAL_ROW, BAL_PREV_ROW])
        if "RPT_VALUEANALYSIS_DET" in url:
            # 同一张表两种用法：本公司历史 vs 同业（带 BOARD_CODE 过滤）
            return _payload(PEER_VAL_ROWS if "BOARD_CODE=" in plain else VAL_ROWS)
        if "RPT_F10_EH_FREEHOLDERS" in url:
            return _payload(FREEHOLD_ROWS)
        if "RPT_ECONOMY_CURRENCY_SUPPLY" in url:
            return _payload(M1_ROWS)
        if "RPT_ECONOMY_PMI" in url:
            return _payload(PMI_ROWS)
        if "RPT_CSDC_LIST" in url:
            # 注意用过滤条件区分：质押查询的 columns 里也含 INDUSTRY_CODE，不能拿它判断
            return _payload(PEER_ROWS if "(INDUSTRY_CODE=" in plain else PLEDGE_ROWS)
        if "RPT_LICO_FN_CPD" in url:
            period = "2026-06-30" if "2026-06-30" in plain else "2025-06-30"
            return _payload(_perf_rows(period))
        if "RPT_SHAREBONUS_DET" in url:
            return _payload(DIVIDEND_ROWS)
        if "money.finance.sina.com.cn" in url:
            if not sina_ok:
                raise SourceError("模拟新浪不可达", "SOURCE_UNAVAILABLE")
            return SINA_ROWS
        if "kline" in url:
            if not kline_ok:
                raise SourceError("模拟断连", "SOURCE_UNAVAILABLE")
            return {"data": {"code": "600519", "name": "贵州茅台", "klines": [KLINE]}}
        if "suggest" in url:
            # Classify 取值并不统一：沪A=AStock、科创板=23、北交所=NEEQ，港股/期货也混在同列表里
            return {"QuotationCodeTable": {"Data": [
                {"Code": "600519", "Name": "贵州茅台", "Classify": "AStock", "QuoteID": "1.600519",
                 "SecurityTypeName": "沪A"},
                {"Code": "688981", "Name": "中芯国际", "Classify": "23", "QuoteID": "1.688981",
                 "SecurityTypeName": "科创板"},
                {"Code": "920185", "Name": "贝特瑞", "Classify": "NEEQ", "QuoteID": "0.920185",
                 "SecurityTypeName": "京A"},
                {"Code": "00700", "Name": "腾讯控股", "Classify": "HKStock", "QuoteID": "116.00700",
                 "SecurityTypeName": "港股"},
                {"Code": "SMCH7", "Name": "中芯国际期货2703", "Classify": "HKSTOCKF", "QuoteID": "130.SMCH7",
                 "SecurityTypeName": "期货"},
            ], "Status": 0}}
        raise AssertionError("未预期的 URL: " + url)
    return _http


def ref() -> StockRef:
    return StockRef(code="600519", name="贵州茅台", exchange="SH", secid="1.600519", board="沪A")


def test_helpers():
    assert _num(None) is None and _num("-") is None and _num("1.5") == 1.5
    assert _day("2026-06-30 00:00:00") == "2026-06-30"
    assert _day(None) is None


def test_search_keeps_all_a_share_boards():
    """沪A / 科创板 / 北交所都要能检索到；港股、期货必须被排除。"""
    p = _p(http=fake_http())
    refs = p.search("测试")
    assert [r.code for r in refs] == ["600519", "688981", "920185"]
    assert [r.exchange for r in refs] == ["SH", "SH", "BJ"]
    assert refs[0].secid == "1.600519"


def test_search_not_found():
    p = _p(http=lambda url: {"QuotationCodeTable": {"Data": []}})
    with pytest.raises(SourceError) as exc:
        p.search("不存在")
    assert exc.value.code == "NOT_FOUND"


def test_fetch_normalizes_fields():
    p = _p(http=fake_http())
    raw = p.fetch(ref(), "2026-10-23")
    assert raw["stock"]["code"] == "600519"
    assert raw["quote"]["price"] == 1318.49 and raw["quote"]["trade_date"] == "2026-10-22"
    rep = raw["reports"][0]
    assert rep["report_date"] == "2026-06-30" and rep["notice_date"] == "2026-08-15"
    assert rep["op_margin"] == pytest.approx(6.1e10 / 9.07e10 * 100, abs=0.01)
    assert rep["tax_rate"] == pytest.approx(0.2507)
    assert raw["balance"]["short_loan"] == 0.0  # None 归零仅限「无该负债」型字段
    assert raw["balance"]["goodwill"] is None   # 缺失保留 None，不补零
    assert len(raw["valuation_hist"]) == 2
    assert raw["kline"][0]["turnover"] == 0.34


def test_kline_failure_degrades_not_raises():
    """两个 K 线源都不可用时才降级；降级后交易面缺失但不影响整体取数。"""
    p = _p(http=fake_http(kline_ok=False, sina_ok=False))
    raw = p.fetch(ref(), "2026-10-23")
    assert raw["kline"] == []
    assert any("K线" in w for w in raw["meta"]["warnings"])


def test_kline_falls_back_to_sina():
    """东财 K 线被限流时切新浪：成交量有、换手率没有（留给 autobuild 用流通股本算）。"""
    raw = _p(http=fake_http(kline_ok=False)).fetch(ref(), "2026-10-23")
    assert len(raw["kline"]) == 28
    assert raw["kline"][0]["vendor"] == "sina"
    assert raw["kline"][0]["turnover"] is None
    assert raw["kline"][0]["volume"] == 42000.0
    assert "sina_kline" in raw["meta"]["endpoints"]


def test_peer_valuation_and_freeholders():
    raw = _p(http=fake_http()).fetch(ref(), "2026-10-23")
    pv = raw["peer_val"]
    assert pv["board"] == "BK0477" and pv["samples"] == 6      # 亏损的那家被剔除
    assert pv["median_pe"] == pytest.approx(22.6, abs=0.05)   # 15,18,20.2,25,30,40 → 中位 (20.2+25)/2
    assert pv["trade_date"] == "2026-10-22"
    assert pv["own_pe"] == 20.24

    fh = raw["free_holders"]
    assert fh["period"] == "2026-06-30" and fh["prev_period"] == "2026-03-31"
    assert fh["org_ratio"] == pytest.approx(4.2)
    assert fh["prev_org_ratio"] == pytest.approx(3.0)
    assert fh["chg_pp"] == pytest.approx(1.2)
    assert fh["scope"].startswith("前十大流通股东")


# ---------------------------------------------------------------- 判断层取数

def test_macro_uses_conservative_publish_date():
    """宏观数据没有发布日字段 → 按惯例保守后推；asof 未到的月份必须跳过。"""
    raw = _p(http=fake_http()).fetch(ref(), "2026-10-23")
    macro = raw["macro"]
    # 2026-10 的 M1 要到 11-15 才"发布"，此刻只能用 9 月
    assert macro["m1"]["value"] == 3.2
    assert macro["m1"]["period"] == "2026-09"
    assert macro["m1"]["available_at"] == "2026-10-15"
    assert macro["pmi"]["value"] == 50.1
    assert macro["pmi"]["available_at"] == "2026-10-02"

    # 把时点提前到 10-10：9 月 PMI（10-02 发布）可用；9 月 M1 要到 10-15，必须回退到 8 月
    early = _p(http=fake_http()).fetch(ref(), "2026-10-10")
    assert early["macro"]["pmi"]["period"] == "2026-09"
    assert early["macro"]["m1"]["period"] == "2026-08"
    assert early["macro"]["m1"]["value"] == 4.1
    assert early["macro"]["m1"]["available_at"] <= "2026-10-10"


def test_pledge_and_industry_snapshot():
    raw = _p(http=fake_http()).fetch(ref(), "2026-10-23")
    # asof 之后的质押快照（10-30）不得使用
    assert raw["pledge"]["ratio"] == 0.06
    assert raw["pledge"]["trade_date"] == "2026-10-16"
    assert raw["pledge"]["industry"] == "白酒Ⅱ"

    ind = raw["industry"]
    assert ind["samples"] == 8 and ind["period"] == "2026-06-30"
    assert ind["deadline"] == "2026-08-31"
    assert ind["rev_yoy_med"] == pytest.approx(-4.35, abs=0.01)
    assert ind["gm_med"] == pytest.approx(77.065, abs=0.01)
    assert ind["gm_yoy_med"] == pytest.approx(-0.65, abs=0.01)
    assert ind["company_share"] == pytest.approx(9.23 / 16.338, rel=1e-3)


def test_peer_shortage_degrades_without_guessing():
    """同业不足 5 家时，行业块必须缺失，而不是用 2-3 家硬算中位数。"""
    def thin(url: str):
        if "RPT_CSDC_LIST" in url and "INDUSTRY_CODE" in url:
            return _payload(PEER_ROWS[:3])
        return fake_http()(url)

    raw = _p(http=thin).fetch(ref(), "2026-10-23")
    assert raw["industry"] is None
    assert any("同业" in w for w in raw["meta"]["warnings"])


def test_dividend_records_truncated_by_asof():
    rows = [r for r in DIVIDEND_ROWS if r["NOTICE_DATE"] <= "2026-10-23"]
    raw = _p(http=fake_http()).fetch(ref(), "2026-10-23")
    assert len(raw["dividends"]) == len(rows)
    assert all(d["notice_date"] <= "2026-10-23" for d in raw["dividends"])


def test_non_json_raises_bad_response():
    def boom(url):
        raise SourceError("数据源返回非 JSON（可能被限流或改版）", "BAD_RESPONSE")
    p = _p(http=boom)
    with pytest.raises(SourceError) as exc:
        p.fetch(ref(), "2026-10-23")
    assert exc.value.code == "BAD_RESPONSE"


def test_missing_secid_rejected():
    p = _p(http=fake_http())
    with pytest.raises(SourceError) as exc:
        p.fetch(StockRef(code="600519", name="贵州茅台", exchange="SH", secid=""), "2026-10-23")
    assert exc.value.code == "BAD_REF"


def test_json_serializable():
    """RawStock 必须可 JSON 序列化（要能落 fixture / 传给前端）。"""
    p = _p(http=fake_http())
    raw = p.fetch(ref(), "2026-10-23")
    json.dumps(raw, ensure_ascii=False)


# ---------------------------------------------------------------- 中债 10Y

from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz

from stockframe.sources.chinabond import ChinaBondProvider

_CN = _tz(_td(hours=8))


def _ms(d: _date) -> int:
    """把日期转成接口给的那种「北京时间当日 0 点的毫秒值」。"""
    return int(_dt(d.year, d.month, d.day, tzinfo=_CN).timestamp() * 1000)


def _bond_http(points):
    """points: [(date, 10Y%)] → 伪造 czbQueryYz 的返回。"""
    series = [[_ms(d), v] for d, v in points]

    def _http(url):
        assert "czbQueryYz" in url and "gjqx=10" in url
        return [{"ycDefId": "x", "ycDefName": "10年", "worktime": "2026-10-22",
                 "seriesData": series}]
    return _http


def test_chinabond_timestamp_is_beijing_time():
    """接口时间戳是北京时间 0 点；按 UTC 解析会整体错一天。"""
    pts = [(_date(2025, 10, 22), 1.8293), (_date(2026, 10, 22), 1.6414)]
    out = ChinaBondProvider(http=_bond_http(pts)).yield_10y("2026-10-23")
    assert out["date"] == "2026-10-22" and out["prev_date"] == "2025-10-22"


def test_chinabond_uses_conservative_publish_lag():
    """当日曲线次日才可用：asof=2026-10-22 只能看到 10-21 的值。"""
    pts = [(_date(2025, 10, 21), 1.8357), (_date(2026, 10, 21), 1.6412),
           (_date(2026, 10, 22), 1.6414)]
    p = ChinaBondProvider(http=_bond_http(pts))
    d22 = p.yield_10y("2026-10-22")
    d23 = p.yield_10y("2026-10-23")
    assert d22["date"] == "2026-10-21" and d22["value"] == 1.6412
    assert d22["available_at"] == "2026-10-22"
    assert d23["date"] == "2026-10-22" and d23["value"] == 1.6414


def test_chinabond_yoy_uses_nearest_trading_day():
    """一年前是假期时用最近交易日，并如实记录错位天数。"""
    pts = [(_date(2025, 10, 20), 1.84), (_date(2026, 10, 21), 1.64)]
    out = ChinaBondProvider(http=_bond_http(pts)).yield_10y("2026-10-23")
    assert out["prev_date"] == "2025-10-20"
    assert out["prev_offset_days"] == 366
    assert out["chg_bp"] == pytest.approx(-20.0, abs=0.05)


def test_chinabond_returns_none_when_anchor_too_far():
    """一年前没有落在容差内的点 → 返回 None，不拿半年前的点冒充。"""
    pts = [(_date(2025, 3, 3), 1.9), (_date(2026, 10, 21), 1.64)]
    assert ChinaBondProvider(http=_bond_http(pts)).yield_10y("2026-10-23") is None


def test_chinabond_returns_none_when_future_only():
    """窗口内全是「发布日未到」的点 → None。"""
    pts = [(_date(2026, 10, 22), 1.6414), (_date(2025, 10, 22), 1.8293)]
    assert ChinaBondProvider(http=_bond_http(pts)).yield_10y("2026-10-22") is None


def test_fetch_attaches_rate_10y():
    """补源结果挂到 raw["rate_10y"]，且 10Y 失败不影响主流程。"""
    class Stub:
        name = "chinabond"
        endpoints = []
        def yield_10y(self, asof, span_days=0):
            return {"curve": "中债国债收益率曲线（10 年）", "value": 1.6414,
                    "date": "2026-10-22", "available_at": "2026-10-23",
                    "prev_value": 1.8293, "prev_date": "2025-10-22",
                    "prev_offset_days": 365, "chg_bp": -18.8,
                    "publish_lag_days": 1, "points": 259, "span_days": 430}

    raw = EastmoneyProvider(http=fake_http(), bond=Stub()).fetch(ref(), "2026-10-23")
    assert raw["rate_10y"]["chg_bp"] == pytest.approx(-18.8)

    class Boom:
        name = "chinabond"
        endpoints = []
        def yield_10y(self, asof, span_days=0):
            raise SourceError("模拟断连", "SOURCE_UNAVAILABLE")

    raw2 = EastmoneyProvider(http=fake_http(), bond=Boom()).fetch(ref(), "2026-10-23")
    assert raw2["rate_10y"] is None
    assert any("10Y" in w for w in raw2["meta"]["warnings"])
