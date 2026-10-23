"""东方财富公开接口适配器（A 股）。

只做三件事：HTTP 取数、字段归一化、包装错误。所有口径推导（TTM、分位、年化）
都在 autobuild 里完成，以便用手写 fixture 回归 PIT 行为。

接口说明（均为公开 JSON 接口，无需令牌）：
- 代码/名称检索  searchapi.eastmoney.com/api/suggest/get
- 主要财务指标    datacenter-web.eastmoney.com  RPT_F10_FINANCE_MAINFINADATA（含 NOTICE_DATE 公告日）
- 资产负债表      datacenter-web.eastmoney.com  RPT_F10_FINANCE_GBALANCE
- 估值历史        datacenter-web.eastmoney.com  RPT_VALUEANALYSIS_DET（日频 PE/PB/市值）
- 日 K 线         push2his.eastmoney.com/api/qt/stock/kline/get

数据源本身是「二手整理数据」，统一按 L2 标注；关键论点仍应回到 L1（公司公告）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Union

from .base import SourceError, StockRef, validate_raw
from .chinabond import ChinaBondProvider
from .cninfo import CninfoProvider

SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) stockframe/0.2"

MKT_EXCHANGE = {"1": "SH", "0": "SZ"}
# 检索接口的 Classify 取值不统一（沪/深A="AStock"、科创板="23"、北交所="NEEQ"），
# 因此不按 Classify 过滤，改用 secid 市场号 + 6 位数字代码判定 A 股。
BOARD_EXCHANGE = {"沪A": "SH", "深A": "SZ", "京A": "BJ", "科创板": "SH", "创业板": "SZ"}

FIN_COLUMNS = (
    "SECUCODE,SECURITY_CODE,REPORT_DATE,NOTICE_DATE,REPORT_TYPE,"
    "TOTALOPERATEREVE,PARENTNETPROFIT,EPSJB,NETCASH_OPERATE_PK,"
    "TOTAL_EQUITY_PK,TOTAL_ASSETS_PK,ROEJQ,ROIC,XSMLL,"
    "TOTALOPERATEREVETZ,PARENTNETPROFITTZ,OPERATE_PROFIT_PK,OPERATE_INCOME_PK,TAXRATE"
)
BALANCE_COLUMNS = (
    "REPORT_DATE,NOTICE_DATE,MONETARYFUNDS,SHORT_LOAN,LONG_LOAN,BOND_PAYABLE,"
    "NONCURRENT_LIAB_1YEAR,GOODWILL,TOTAL_ASSETS,TOTAL_LIABILITIES,TOTAL_PARENT_EQUITY,"
    # 营运资本增速差（应收+存货 vs 营收）要用到这三个科目，取多期以便同比
    "ACCOUNTS_RECE,INVENTORY,ACCOUNTS_PAYABLE"
)
VALUE_COLUMNS = ("TRADE_DATE,PE_TTM,PB_MRQ,CLOSE_PRICE,TOTAL_MARKET_CAP,TOTAL_SHARES,"
                 "FREE_SHARES_A,BOARD_CODE,BOARD_NAME")
PLEDGE_COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,PLEDGE_RATIO,INDUSTRY,INDUSTRY_CODE,PAYYEAR"
PEER_COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,INDUSTRY_CODE"
PERF_COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,UPDATE_DATE,"
                "TOTAL_OPERATE_INCOME,YSTZ,XSMLL,WEIGHTAVG_ROE")
DIVIDEND_COLUMNS = ("SECURITY_CODE,REPORT_DATE,NOTICE_DATE,ASSIGN_PROGRESS,"
                    "PRETAX_BONUS_RMB,IMPL_PLAN_PROFILE")
FREEHOLD_COLUMNS = ("SECURITY_CODE,END_DATE,UPDATE_DATE,HOLDER_NAME,HOLDER_RANK,"
                    "HOLD_RATIO,FREE_HOLDNUM_RATIO,IS_HOLDORG,HOLDER_TYPE")
# 东方财富行情主机（push2/push2his）对自动化访问限流严重，K 线改由新浪财经兜底。
# 两个源都在盘后给出同一交易日的日线，口径一致（成交量单位为「股」）。
SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                  "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")

# 宏观数据的「保守发布日」假设（天）。公开接口不带发布日期，只能按惯例后推：
# 统计局 PMI 月末/次月 1 日发布；人行 M1/M2 一般次月 10-15 日发布。
# 一律取偏晚的假设——宁可晚用一天，也不要提前使用未发布的数据。
MACRO_PUBLISH_LAG_DAYS = {"pmi": 2, "m1": 15}

# 同业样本少于 5 家时，中位数没有统计意义，行业维度宁可留空也不硬猜
MIN_PEER_SAMPLES = 5

# 同业 PE 中位数的清洗口径：负 PE（亏损）与极端值（>300 倍）会把中位数拉成噪声
PE_SANE_RANGE = (0.0, 300.0)



def _num(value: Any) -> Optional[float]:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> Optional[str]:
    """'2026-06-30 00:00:00' → '2026-06-30'。"""
    if not value:
        return None
    text = str(value)[:10]
    return text if len(text) == 10 and text[4] == "-" else None


def _month_end(day: str) -> str:
    """所在月最后一天。'2026-09-01' → '2026-09-30'。"""
    y, m = int(day[:4]), int(day[5:7])
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def _period_deadline(period: str) -> str:
    """报告期的法定披露截止日。

    同业数据（业绩报表）不带可用的公告日，因此只能用「最晚必须披露日」做 PIT 判断：
    该日之前，这份报告一定已经公开。方向保守——宁可用得晚，也不提前使用。
    """
    y, md = int(period[:4]), period[5:10]
    if md == "03-31":
        return f"{y}-04-30"
    if md == "06-30":
        return f"{y}-08-31"
    if md == "09-30":
        return f"{y}-10-31"
    if md == "12-31":
        return f"{y + 1}-04-30"
    return f"{y}-04-30"


def _latest_period(asof: str) -> Optional[str]:
    """asof 时点下，已经过了法定披露截止日的最新报告期。"""
    y = int(asof[:4])
    for year in (y, y - 1, y - 2):
        for md in ("12-31", "09-30", "06-30", "03-31"):
            period = f"{year}-{md}"
            if period <= asof and _period_deadline(period) <= asof:
                return period
    return None


def _prev_year_period(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}"


def _median(values: List[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0



class EastmoneyProvider:
    """东方财富数据源。http 可注入，便于离线测试。"""

    name = "eastmoney"

    def __init__(
        self,
        http: Optional[Callable[[str], Dict[str, Any]]] = None,
        timeout: float = 15.0,
        bond: Union[ChinaBondProvider, bool, None] = None,
        cninfo: Union[CninfoProvider, bool, None] = None,
    ) -> None:
        """bond / cninfo：两个补源。默认自建；传 False 关闭（单测/离线场景）；也可注入替身。"""
        self._http = http or (lambda url: _get_json(url, timeout))
        self.timeout = timeout
        self._bond: Optional[ChinaBondProvider] = (
            ChinaBondProvider() if bond is None else (bond or None)
        )
        self._cninfo: Optional[CninfoProvider] = (
            CninfoProvider() if cninfo is None else (cninfo or None)
        )
        self.endpoints: List[str] = []

    # ------------------------------------------------------------ 检索
    def search(self, query: str, limit: int = 8) -> List[StockRef]:
        q = (query or "").strip()
        if not q:
            raise SourceError("检索词为空", "BAD_QUERY")
        url = (
            f"{SUGGEST_URL}?input={urllib.parse.quote(q)}&type=14"
            f"&token={SUGGEST_TOKEN}&count={max(1, min(limit, 20))}"
        )
        payload = self._http(url)
        self.endpoints.append("suggest")
        rows = (((payload or {}).get("QuotationCodeTable") or {}).get("Data")) or []
        out: List[StockRef] = []
        for row in rows:
            code, name = row.get("Code"), row.get("Name")
            secid = row.get("QuoteID") or ""
            if not code or not name or "." not in secid:
                continue
            market, _, scode = secid.partition(".")
            # A 股：沪深/北交所市场号 + 6 位数字代码；港股（5 位）、期货、基金自然被排除
            if market not in ("0", "1") or not (scode.isdigit() and len(scode) == 6):
                continue
            board = row.get("SecurityTypeName") or ""
            out.append(
                StockRef(
                    code=scode,
                    name=name,
                    exchange=BOARD_EXCHANGE.get(board, MKT_EXCHANGE.get(market, "SZ")),
                    secid=secid,
                    board=board,
                )
            )
            if len(out) >= limit:
                break
        if not out:
            raise SourceError(f"未找到 A 股标的：{q!r}", "NOT_FOUND")
        return out

    # ------------------------------------------------------------ 取数
    def fetch(self, ref: StockRef, analysis_date: str) -> Dict[str, Any]:
        if not ref.secid or "." not in ref.secid:
            raise SourceError("标的缺少 secid，无法取数", "BAD_REF")
        secucode = f"{ref.code}.{ref.exchange}"
        self.endpoints = ["suggest"]

        reports = self._reports(secucode)
        if not reports:
            raise SourceError(f"{ref.display} 无财务指标数据", "NO_DATA")

        balance = self._balance(secucode)
        hist = self._valuation_history(ref.code)
        warnings: List[str] = []
        try:
            kline = self._kline(ref.secid)
        except SourceError as exc:
            # 交易面属可选输入：取不到就降级，由评分引擎按缺失处理（不补零）
            kline = []
            warnings.append(f"日K线获取失败，交易面指标缺失：{exc}")
        if not hist:
            warnings.append("估值历史为空：PE/PB 及十年分位缺失，相对估值不可用")
        if not balance:
            warnings.append("资产负债表为空：净债务按 0 假设，有息负债率/商誉缺失")

        latest = hist[0] if hist else {}

        # ---- 判断层输入（宏观 / 行业 / 质押 / 分红）：全部可选，取不到就降级为缺失
        macro: Dict[str, Any] = {}
        try:
            macro = self._macro(analysis_date)
        except SourceError as exc:
            warnings.append(f"宏观数据获取失败，宏观维度按缺失处理：{exc}")

        pledge = None
        try:
            pledge = self._pledge(ref.code, analysis_date)
        except SourceError as exc:
            warnings.append(f"质押/行业归属获取失败：{exc}")
        if not pledge:
            warnings.append("中登质押表无该标的记录：行业归属与质押比例无法自动获取")

        industry = None
        if pledge and pledge.get("industry_code"):
            try:
                industry = self._industry_block(ref.code, pledge, analysis_date)
            except SourceError as exc:
                warnings.append(f"同业对比失败，行业维度按缺失处理：{exc}")
            else:
                if industry.get("warning"):
                    warnings.append(industry["warning"])

        dividends: List[Dict[str, Any]] = []
        try:
            dividends = self._dividends(ref.code, analysis_date)
        except SourceError as exc:
            warnings.append(f"分红记录获取失败，管理层记录代理降级：{exc}")

        free_holders = None
        try:
            free_holders = self._freeholders(ref.code, analysis_date)
        except SourceError as exc:
            warnings.append(f"十大流通股东获取失败，机构持仓变化缺失：{exc}")

        peer_val = None
        try:
            peer_val = self._peer_valuation(ref.code, analysis_date)
        except SourceError as exc:
            warnings.append(f"同业估值获取失败，同业 PE 中位数缺失：{exc}")

        # 10Y 国债收益率走中债（东财数据中心无该序列），失败只降级不阻断
        rate_10y = None
        if self._bond is not None:
            try:
                rate_10y = self._bond.yield_10y(analysis_date)
            except SourceError as exc:
                warnings.append(f"中债 10Y 国债收益率获取失败，利率同比按缺失处理：{exc}")
            if rate_10y is None:
                warnings.append("中债 10Y 序列缺失一年前锚点或窗口内无数据：利率同比留空，未做插值")

        # 客户集中度只在年报正文（PDF），走巨潮原文；银行/保险没这项披露属正常缺失，
        # 所以这里只降级、不阻断，也不把"取不到"写成错误。
        customer = None
        if self._cninfo is not None:
            try:
                customer = self._cninfo.annual_customer(ref.code, analysis_date)
                self.endpoints.extend(self._cninfo.endpoints)
            except SourceError as exc:
                warnings.append(f"年报客户集中度未能自动提取：{exc}")
            if customer and customer.get("top_ratio") is None:
                five = customer.get("five_ratio")
                if five is not None:
                    warnings.append(f"年报客户集中度：只披露前五名客户合计 {five}%，"
                                    "未披露单客户占比（最大一家不超过该值）")
                else:
                    warnings.append(f"年报客户集中度：{customer.get('reason') or '年报未披露该项'}")

        raw: Dict[str, Any] = {
            "stock": ref.to_dict(),
            "quote": {
                "price": latest.get("close"),
                "trade_date": latest.get("date"),
            } if latest else None,
            "reports": reports,
            "balance": balance,
            "valuation_hist": hist,
            "kline": kline,
            "macro": macro,
            "industry": industry,
            "pledge": pledge,
            "dividends": dividends,
            "free_holders": free_holders,
            "peer_val": peer_val,
            "rate_10y": rate_10y,
            "customer": customer,
            "meta": {
                "provider": self.name,
                "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "endpoints": sorted(set(self.endpoints)),
                "analysis_date_requested": analysis_date,
                "warnings": warnings,
            },
        }
        validate_raw(raw)
        return raw

    # ------------------------------------------------------------ 各接口
    def _dc(self, report_name: str, columns: str, filter_expr: str, page_size: int,
            sort: str = "REPORT_DATE") -> List[Dict[str, Any]]:
        url = (
            f"{DC_URL}?reportName={report_name}&columns={columns}"
            + (f"&filter={urllib.parse.quote(filter_expr)}" if filter_expr else "")
            + f"&pageNumber=1&pageSize={page_size}&sortTypes=-1&sortColumns={sort}"
        )
        payload = self._http(url)
        self.endpoints.append(report_name)
        result = (payload or {}).get("result") or {}
        rows = result.get("data") or []
        return rows if isinstance(rows, list) else []

    def _reports(self, secucode: str) -> List[Dict[str, Any]]:
        rows = self._dc("RPT_F10_FINANCE_MAINFINADATA", FIN_COLUMNS, f'(SECUCODE="{secucode}")', 16)
        out: List[Dict[str, Any]] = []
        for r in rows:
            period = _day(r.get("REPORT_DATE"))
            notice = _day(r.get("NOTICE_DATE"))
            if not period or not notice:
                continue
            revenue = _num(r.get("TOTALOPERATEREVE"))
            op_profit = _num(r.get("OPERATE_PROFIT_PK"))
            op_income = _num(r.get("OPERATE_INCOME_PK"))
            tax = _num(r.get("TAXRATE"))
            out.append({
                "report_date": period,
                "notice_date": notice,
                "report_type": r.get("REPORT_TYPE") or "",
                "revenue": revenue,
                "np": _num(r.get("PARENTNETPROFIT")),
                "eps": _num(r.get("EPSJB")),
                "ocf": _num(r.get("NETCASH_OPERATE_PK")),
                "equity": _num(r.get("TOTAL_EQUITY_PK")),
                "total_assets": _num(r.get("TOTAL_ASSETS_PK")),
                "roe": _num(r.get("ROEJQ")),
                "roic": _num(r.get("ROIC")),
                "gm": _num(r.get("XSMLL")),
                "rev_yoy": _num(r.get("TOTALOPERATEREVETZ")),
                "np_yoy": _num(r.get("PARENTNETPROFITTZ")),
                "op_margin": (op_profit / op_income * 100.0) if (op_profit is not None and op_income) else None,
                "tax_rate": (tax / 100.0) if tax is not None else None,
            })
        return out

    def _balance(self, secucode: str) -> Optional[Dict[str, Any]]:
        rows = self._dc("RPT_F10_FINANCE_GBALANCE", BALANCE_COLUMNS, f'(SECUCODE="{secucode}")', 8)
        periods: List[Dict[str, Any]] = []
        latest: Optional[Dict[str, Any]] = None
        for r in rows:
            period, notice = _day(r.get("REPORT_DATE")), _day(r.get("NOTICE_DATE"))
            if not period:
                continue
            periods.append({
                "report_date": period,
                "notice_date": notice or period,
                "accounts_rece": _num(r.get("ACCOUNTS_RECE")),
                "inventory": _num(r.get("INVENTORY")),
                "accounts_payable": _num(r.get("ACCOUNTS_PAYABLE")),
            })
            if latest is None:
                latest = {
                    "report_date": period,
                    "notice_date": notice or period,
                    "monetaryfunds": _num(r.get("MONETARYFUNDS")),
                    "short_loan": _num(r.get("SHORT_LOAN")) or 0.0,
                    "long_loan": _num(r.get("LONG_LOAN")) or 0.0,
                    "bond_payable": _num(r.get("BOND_PAYABLE")) or 0.0,
                    "noncurrent_liab_1y": _num(r.get("NONCURRENT_LIAB_1YEAR")) or 0.0,
                    "goodwill": _num(r.get("GOODWILL")),
                    "total_assets": _num(r.get("TOTAL_ASSETS")),
                    "total_liab": _num(r.get("TOTAL_LIABILITIES")),
                    "parent_equity": _num(r.get("TOTAL_PARENT_EQUITY")),
                    "periods": periods,
                }
        return latest

    def _valuation_history(self, code: str) -> List[Dict[str, Any]]:
        rows = self._dc("RPT_VALUEANALYSIS_DET", VALUE_COLUMNS, f'(SECURITY_CODE="{code}")', 3000, "TRADE_DATE")
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = _day(r.get("TRADE_DATE"))
            if not d:
                continue
            out.append({
                "date": d,
                "pe": _num(r.get("PE_TTM")),
                "pb": _num(r.get("PB_MRQ")),
                "close": _num(r.get("CLOSE_PRICE")),
                "mcap": _num(r.get("TOTAL_MARKET_CAP")),
                "shares": _num(r.get("TOTAL_SHARES")),
                # 流通 A 股：换手率 = 成交量 ÷ 流通股，必须用流通口径而不是总股本
                "free_shares": _num(r.get("FREE_SHARES_A")),
                "board_code": r.get("BOARD_CODE") or "",
                "board_name": r.get("BOARD_NAME") or "",
            })
        return out

    # ------------------------------------------ 判断层输入（宏观 / 行业 / 公司）
    def _macro(self, asof: str) -> Dict[str, Any]:
        """M1 同比与制造业 PMI。

        公开接口不带发布日期，因此按惯例给「保守发布日」：只采用保守发布日 ≤ asof 的最新一期。
        方向是宁晚不早——绝不出现「用还没公布的数据做判断」。
        """
        out: Dict[str, Any] = {}
        specs = (
            ("m1", "RPT_ECONOMY_CURRENCY_SUPPLY", "REPORT_DATE,TIME,CURRENCY_SAME", "CURRENCY_SAME"),
            ("pmi", "RPT_ECONOMY_PMI", "REPORT_DATE,TIME,MAKE_INDEX", "MAKE_INDEX"),
        )
        for key, report, cols, value_field in specs:
            try:
                rows = self._dc(report, cols, "", 48, "REPORT_DATE")
            except SourceError:
                continue
            lag = MACRO_PUBLISH_LAG_DAYS[key]
            for row in rows:
                day, val = _day(row.get("REPORT_DATE")), _num(row.get(value_field))
                if not day or val is None:
                    continue
                published = (date.fromisoformat(_month_end(day)) + timedelta(days=lag)).isoformat()
                if published > asof:
                    continue  # 尚未发布
                out[key] = {
                    "value": val,
                    "period": day[:7],
                    "period_label": row.get("TIME") or day[:7],
                    "available_at": published,
                    "lag_days_assumed": lag,
                }
                break
        return out

    def _pledge(self, code: str, asof: str) -> Optional[Dict[str, Any]]:
        """中登质押表的最新一期（≤ asof）。这张表同时带申万二级行业归属。"""
        rows = self._dc("RPT_CSDC_LIST", PLEDGE_COLUMNS, f'(SECURITY_CODE="{code}")', 40, "TRADE_DATE")
        for row in rows:
            trade = _day(row.get("TRADE_DATE"))
            if not trade or trade > asof:
                continue
            return {
                "ratio": _num(row.get("PLEDGE_RATIO")),
                "trade_date": trade,
                "pay_year": str(row.get("PAYYEAR") or ""),
                "industry": row.get("INDUSTRY") or "",
                "industry_code": str(row.get("INDUSTRY_CODE") or ""),
            }
        return None

    def _peers(self, industry_code: str, asof: str, limit: int = 600) -> List[str]:
        """同行业（申万二级）成员代码，取 ≤ asof 的最新一期快照。"""
        flt = f'(INDUSTRY_CODE="{industry_code}")(TRADE_DATE<=\'{asof}\')'
        try:
            rows = self._dc("RPT_CSDC_LIST", PEER_COLUMNS, flt, limit, "TRADE_DATE")
        except SourceError:
            rows = self._dc("RPT_CSDC_LIST", PEER_COLUMNS,
                            f'(INDUSTRY_CODE="{industry_code}")', limit, "TRADE_DATE")
        pairs = []
        for row in rows:
            trade, code = _day(row.get("TRADE_DATE")), row.get("SECURITY_CODE")
            if trade and code and trade <= asof:
                pairs.append((trade, code))
        if not pairs:
            return []
        latest = max(t for t, _ in pairs)
        return sorted({c for t, c in pairs if t == latest})

    def _peer_financials(self, codes: List[str], period: str) -> Dict[str, Dict[str, Any]]:
        """一批公司在指定报告期的营收/增速/毛利率（一次请求）。"""
        if not codes:
            return {}
        quoted = '","'.join(codes)
        flt = f"(REPORTDATE='{period}')(SECURITY_CODE in (\"{quoted}\"))"
        rows = self._dc("RPT_LICO_FN_CPD", PERF_COLUMNS, flt, 500, "SECURITY_CODE")
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            code = row.get("SECURITY_CODE")
            if not code:
                continue
            out[code] = {
                "name": row.get("SECURITY_NAME_ABBR") or "",
                "revenue": _num(row.get("TOTAL_OPERATE_INCOME")),
                "rev_yoy": _num(row.get("YSTZ")),
                "gm": _num(row.get("XSMLL")),
                "roe": _num(row.get("WEIGHTAVG_ROE")),
            }
        return out

    def _industry_block(self, code: str, pledge: Dict[str, Any], asof: str) -> Dict[str, Any]:
        """同业对比：增速中位数、毛利率中位数及其同比、公司份额。"""
        period = _latest_period(asof)
        if not period:
            raise SourceError("近期无已过法定披露截止日的报告期", "NO_PEER_PERIOD")
        peers = self._peers(pledge["industry_code"], asof)
        if len(peers) < MIN_PEER_SAMPLES:
            raise SourceError(
                f"同业样本不足（{len(peers)} 家 < {MIN_PEER_SAMPLES} 家），行业维度按缺失处理",
                "PEER_TOO_FEW",
            )
        cur = self._peer_financials(peers, period)
        prev = self._peer_financials(peers, _prev_year_period(period))

        gm_yoy = []
        for c, v in cur.items():
            p = prev.get(c) or {}
            if v.get("gm") is not None and p.get("gm") is not None:
                gm_yoy.append(v["gm"] - p["gm"])

        revenues = [v["revenue"] for v in cur.values() if v.get("revenue")]
        total = sum(revenues) if revenues else None
        own = (cur.get(code) or {}).get("revenue")
        warning = None
        if code not in cur:
            warning = f"本标的 {period} 业绩报表缺失，行业份额改用同口径财报值或留空"
        return {
            "code": pledge["industry_code"],
            "name": pledge.get("industry") or "",
            "period": period,
            "deadline": _period_deadline(period),
            "members": len(peers),
            "samples": len(cur),
            "rev_yoy_med": _median([v["rev_yoy"] for v in cur.values() if v.get("rev_yoy") is not None]),
            "rev_yoy_prev_med": _median(
                [v["rev_yoy"] for v in prev.values() if v.get("rev_yoy") is not None]),
            "gm_med": _median([v["gm"] for v in cur.values() if v.get("gm") is not None]),
            "gm_yoy_med": _median(gm_yoy) if gm_yoy else None,
            "gm_samples": len(gm_yoy),
            "company_revenue": own,
            "total_revenue": total,
            "company_share": (own / total) if (own and total) else None,
            "warning": warning,
        }

    def _dividends(self, code: str, asof: str) -> List[Dict[str, Any]]:
        rows = self._dc("RPT_SHAREBONUS_DET", DIVIDEND_COLUMNS, f'(SECURITY_CODE="{code}")',
                        60, "REPORT_DATE")
        out: List[Dict[str, Any]] = []
        for row in rows:
            notice = _day(row.get("NOTICE_DATE"))
            if not notice or notice > asof:
                continue
            out.append({
                "report_date": _day(row.get("REPORT_DATE")),
                "notice_date": notice,
                "status": row.get("ASSIGN_PROGRESS") or "",
                "cash_per_10": _num(row.get("PRETAX_BONUS_RMB")),
                "profile": row.get("IMPL_PLAN_PROFILE") or "",
            })
        return out

    def _freeholders(self, code: str, asof: str) -> Optional[Dict[str, Any]]:
        """十大流通股东里的机构持股合计（季度），用于「机构持仓变化」。

        口径必须说清楚：这里只覆盖**前十大流通股东**，不是全部机构持仓，
        因此它是方向性证据，不是精确的机构仓位。PIT 用 UPDATE_DATE（披露日），
        不用 END_DATE（报告期）——报告期末的数据在披露前并不公开。
        """
        rows = self._dc("RPT_F10_EH_FREEHOLDERS", FREEHOLD_COLUMNS,
                        f'(SECURITY_CODE="{code}")', 60, "END_DATE")
        by_period: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            end, upd = _day(r.get("END_DATE")), _day(r.get("UPDATE_DATE"))
            ratio = _num(r.get("FREE_HOLDNUM_RATIO"))
            if not end or not upd or ratio is None:
                continue
            slot = by_period.setdefault(end, {"end": end, "notice": upd, "org": 0.0, "n": 0, "total": 0.0})
            slot["notice"] = max(slot["notice"], upd)
            slot["total"] += ratio
            if str(r.get("IS_HOLDORG")) == "1":
                slot["org"] += ratio
                slot["n"] += 1
        # 只用「已披露」且报告期 ≤ asof 的期次
        usable = [v for v in by_period.values() if v["end"] <= asof and v["notice"] <= asof]
        # END_DATE 有时是「股东变动日」而非报告期（如 2026-08-05）。做期间对比必须用
        # 标准报告期，否则"两期之差"会把不同长度的时间段相减，方向都可能反。
        quarter = [v for v in usable if v["end"][5:] in ("03-31", "06-30", "09-30", "12-31")]
        usable = quarter if len(quarter) >= 2 else usable
        usable.sort(key=lambda v: v["end"], reverse=True)
        if len(usable) < 2 or usable[0]["n"] == 0:
            return None
        cur, prev = usable[0], usable[1]
        return {
            "period": cur["end"],
            "notice": cur["notice"],
            "org_ratio": round(cur["org"], 3),
            "org_count": cur["n"],
            "top10_ratio": round(cur["total"], 3),
            "prev_period": prev["end"],
            "prev_org_ratio": round(prev["org"], 3),
            "chg_pp": round(cur["org"] - prev["org"], 3),
            "scope": "前十大流通股东中机构合计",
        }

    def _peer_valuation(self, code: str, asof: str) -> Optional[Dict[str, Any]]:
        """同业 PE 中位数：同一东财行业板块、同一交易日。

        用东财自己的板块口径（BOARD_CODE）保证「同业」与 PE 同源；
        若换成申万二级，两套行业划分混用会让中位数失去可比性。
        """
        own = self._dc("RPT_VALUEANALYSIS_DET", "SECURITY_CODE,TRADE_DATE,PE_TTM,BOARD_CODE,BOARD_NAME",
                       f'(SECURITY_CODE="{code}")', 5, "TRADE_DATE")
        row = None
        for r in own:
            d = _day(r.get("TRADE_DATE"))
            if d and d <= asof and _num(r.get("PE_TTM")) is not None:
                row = r
                break
        if not row:
            return None
        trade, board = _day(row.get("TRADE_DATE")), row.get("BOARD_CODE") or ""
        if not board:
            return None
        peers = self._dc("RPT_VALUEANALYSIS_DET", "SECURITY_CODE,SECURITY_NAME_ABBR,PE_TTM",
                         f'(BOARD_CODE="{board}")(TRADE_DATE=\'{trade}\')', 500, "TRADE_DATE")
        lo, hi = PE_SANE_RANGE
        vals = [_num(p.get("PE_TTM")) for p in peers]
        vals = [v for v in vals if v is not None and lo < v <= hi]
        if len(vals) < MIN_PEER_SAMPLES:
            return None
        return {
            "trade_date": trade,
            "board": board,
            "board_name": row.get("BOARD_NAME") or "",
            "median_pe": round(_median(vals), 2),
            "samples": len(vals),
            "own_pe": _num(row.get("PE_TTM")),
        }

    def _kline(self, secid: str, limit: int = 300) -> List[Dict[str, Any]]:
        """日 K 线：东财优先，失败（限流）时切新浪。

        两个源都只提供 OHLCV；换手率东财自带、新浪没有（由 autobuild 用流通股本算）。
        每一行都带 vendor，报告里会写明这一批数据实际来自哪个源——不把兜底当原源。
        """
        rows: List[Dict[str, Any]] = []
        try:
            rows = self._kline_em(secid, limit)
        except SourceError:
            rows = []
        if rows:
            for r in rows:
                r["vendor"] = "eastmoney"
            return rows
        rows = self._kline_sina(secid, limit)
        for r in rows:
            r["vendor"] = "sina"
        return rows

    def _kline_em(self, secid: str, limit: int = 300) -> List[Dict[str, Any]]:
        url = (
            f"{KLINE_URL}?secid={secid}&fields1=f1,f2,f3"
            f"&fields2=f51,f53,f54,f55,f56,f57,f61&klt=101&fqt=1&end=20500101&lmt={limit}"
        )
        payload = self._http(url)
        self.endpoints.append("kline")
        data = (payload or {}).get("data") or {}
        out: List[Dict[str, Any]] = []
        for line in data.get("klines") or []:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            out.append({
                "date": parts[0],
                "close": _num(parts[1]),
                "high": _num(parts[2]),
                "low": _num(parts[3]),
                "volume": _num(parts[4]),
                "amount": _num(parts[5]),
                "turnover": _num(parts[6]),
            })
        return out

    def _kline_sina(self, secid: str, limit: int = 300) -> List[Dict[str, Any]]:
        market, _, code = secid.partition(".")
        # 北交所（43/83/87/88/92 开头）在各大门户一律用 bj 前缀，东财却把它归到市场号 0
        prefix = "bj" if code[:2] in ("43", "83", "87", "88", "92") else MKT_EXCHANGE.get(market, "SZ").lower()
        n = max(30, min(int(limit), 300))
        url = SINA_KLINE_URL.format(sym=f"{prefix}{code}", n=n)
        payload = self._http(url)
        self.endpoints.append("sina_kline")
        out: List[Dict[str, Any]] = []
        for r in payload if isinstance(payload, list) else []:
            day = r.get("day")
            if not day:
                continue
            out.append({
                "date": str(day)[:10],
                "close": _num(r.get("close")),
                "high": _num(r.get("high")),
                "low": _num(r.get("low")),
                "volume": _num(r.get("volume")),   # 股
                "amount": None,
                "turnover": None,                  # 新浪不给换手率，交由 autobuild 按流通股本算
            })
        if not out:
            raise SourceError("日K线两个源都不可用（东财限流、新浪无返回）", "SOURCE_UNAVAILABLE")
        return out


def _get_json(url: str, timeout: float, retries: int = 2) -> Dict[str, Any]:
    """取 JSON。公开接口偶发断连/限流，做有限重试后仍失败才报错。"""
    import time

    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", "ignore")
            return json.loads(payload)
        except ValueError as exc:
            raise SourceError("数据源返回非 JSON（可能被限流或改版）", "BAD_RESPONSE") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise SourceError(f"数据源不可达：{last}", "SOURCE_UNAVAILABLE")
