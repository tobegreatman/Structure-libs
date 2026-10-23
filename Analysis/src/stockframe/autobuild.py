"""自动装配：RawStock → 分析输入（PIT 过滤 + 口径推导 + 机械假设）。

三条硬约束（对应设计文档 §4/§8）：

1. **PIT 优先**：任何 `notice_date > analysis_date` 的报告都不得参与计算。若最新报告尚未
   披露，自动回退到上一期，而不是偷看未来。
2. **不补零、不假装**：取不到的字段直接缺失，由评分引擎记 `missing` 并重归一化；
   自动生成的假设一律标注为机械外推，与"市场一致预期"严格区分。
3. **可追溯**：每个计算字段都写进 `derivations`，含公式与所用原始区间，
   报告里的分数可以一路倒推回"哪一期报表、哪一天披露"。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .datadict import FIELD_REGISTRY, FIELD_SPECS
from . import proxies
from .sources.base import SourceError, validate_raw

SRC_QUOTE = "东方财富-估值分析（日频行情+估值）"
SRC_FIN = "东方财富-主要财务指标（定期报告整理）"
SRC_BAL = "东方财富-资产负债表（定期报告整理）"
SRC_KLINE = "东方财富-日K线（前复权）"
SRC_KLINE_SINA = "新浪财经-日K线（东财限流后改用）"
SRC_FREEHOLD = "东方财富-十大流通股东（季报汇总）"
SRC_CNINFO = "巨潮资讯-年度报告原文（PDF）"
SRC_RULE = "自动规则推导"

# 只能靠人工研究判断、确实没有可复算代理口径的字段。
# 其余判断项（M1/PMI、行业增速与卡位、护城河、质押…）走 proxies.py 的代理口径，
# 逐个带公式与误差方向，宁可标"代理"也不留白。
JUDGEMENT_FIELDS: Dict[str, List[str]] = {
    "macro": ["macro.policy_stance"],
    "industry": ["industry.penetration_space"],
    "company": ["company.logic_strength", "company.logic_essence",
                "company.customer_concentration"],
}

JUDGEMENT_WHY = {
    "macro": ("政策取向：需要读政策文本并判断「政策→行业→公司」的传导是否成立，"
              "本质是研究结论，没有可复算口径"),
    "industry": "渗透空间需要行业总量与渗透率数据，A 股接口无此口径；用增速倒推会把周期反弹误判为渗透提升",
    "company": "投资逻辑强度/一句话本质是研究结论本身，没有可复算口径（客户集中度见下）",
}

# 自动模式下「可机读但刻意不取」的字段：都曾有自动抓取尝试，最后判定口径不可靠，
# 宁可留空让研究者自己框定，也不把整理口径当事实写进评分。
# 注：机构持仓变化 / 营运资本增速差 / 同业 PE 中位数 已改为自动取数（见 _freeholders /
#     wc_growth_gap / _peer_valuation），不再列在这里。
OTHER_GAPS: Dict[str, str] = {}


# ---------------------------------------------------------------- 小工具

def _r(value: Optional[float], nd: int = 2) -> Optional[float]:
    return None if value is None else round(value, nd)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _quantile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("空序列无法取分位")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def percentile_of(values: List[float], x: float) -> float:
    """x 在样本中的分位（0-100），用中位秩处理并列值。"""
    n = len(values)
    less = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return (less + 0.5 * equal) / n * 100.0


def _field(value: Any, as_of: str, available_at: str, source: str,
           tier: str = "L2", period_end: Optional[str] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "value": value, "as_of": as_of, "available_at": available_at,
        "source": source, "source_tier": tier,
    }
    if period_end:
        d["period_end"] = period_end
    return d


def _months_in(period: date) -> int:
    return period.month  # 3/6/9/12 月报的累计月数正好等于月份


# ---------------------------------------------------------------- PIT 过滤

def available_reports(raw: Dict[str, Any], asof: date) -> List[Dict[str, Any]]:
    """只保留 asof 时点已披露的报告，按报告期倒序。"""
    rows = [r for r in raw.get("reports", [])
            if r.get("report_date") and r.get("notice_date")
            and date.fromisoformat(r["notice_date"]) <= asof]
    rows.sort(key=lambda r: r["report_date"], reverse=True)
    return rows


def _pick_balance(raw: Dict[str, Any], asof: date) -> Optional[Dict[str, Any]]:
    bal = raw.get("balance")
    if not bal or not bal.get("report_date"):
        return None
    notice = bal.get("notice_date") or bal["report_date"]
    return bal if date.fromisoformat(notice) <= asof else None


def ttm(rows_by_period: Dict[str, Dict[str, Any]], latest: Dict[str, Any],
        key: str) -> Tuple[Optional[float], str, Optional[str]]:
    """返回 (TTM 值, 方法, 公式说明)。

    优先「本期 + 上年年报 - 上年同期」的加总法；缺同期数据时退化为年化，并如实标注。
    """
    period = date.fromisoformat(latest["report_date"])
    cur = latest.get(key)
    if cur is None:
        return None, "missing", None
    if period.month == 12:
        return cur, "latest_annual", f"取 {latest['report_date']} 年报累计值"
    prev_fy = rows_by_period.get(f"{period.year - 1}-12-31")
    prev_same = rows_by_period.get(f"{period.year - 1}-{period.month:02d}-{period.day:02d}")
    if prev_fy and prev_same and prev_fy.get(key) is not None and prev_same.get(key) is not None:
        val = cur + prev_fy[key] - prev_same[key]
        return val, "ttm_additive", (
            f"{latest['report_date']}（{cur:,.2f}） + {prev_fy['report_date']}（{prev_fy[key]:,.2f}）"
            f" − {prev_same['report_date']}（{prev_same[key]:,.2f}）"
        )
    months = _months_in(period)
    if months <= 0:
        return None, "missing", None
    val = cur * 12.0 / months
    return val, "annualized", f"{latest['report_date']} 累计值 {cur:,.2f} × 12/{months}（同期缺失，年化近似）"


def _annual_cagr(reports: List[Dict[str, Any]], key: str, years: int = 3) -> Tuple[Optional[float], Optional[str]]:
    annuals = sorted([r for r in reports if r["report_date"][5:] == "12-31"], key=lambda r: r["report_date"])
    if len(annuals) <= years:
        return None, None
    end, start = annuals[-1], annuals[-1 - years]
    a, b = end.get(key), start.get(key)
    if not a or not b or a <= 0 or b <= 0:
        return None, None
    cagr = (a / b) ** (1.0 / years) - 1.0
    return cagr, f"{start['report_date']}→{end['report_date']} {key} 年化 {cagr:.2%}"


# ---------------------------------------------------------------- 交易面

def trading_metrics(kline: List[Dict[str, Any]], asof: date,
                    free_shares: Optional[float] = None) -> Dict[str, Any]:
    """K 线派生的交易面指标。

    换手率优先用数据源自带值；兜底源（新浪）不给换手率，则用
    「成交量 ÷ 流通 A 股」现算——分母必须是流通口径，用总股本会系统性低估。
    """
    rows = [k for k in kline or [] if k.get("date") and date.fromisoformat(k["date"]) <= asof]
    rows.sort(key=lambda k: k["date"])
    vendors = {k.get("vendor") for k in rows if k.get("vendor")}
    out: Dict[str, Any] = {
        "as_of": rows[-1]["date"] if rows else None,
        "source": "kline",
        "vendor": sorted(vendors),
    }
    if len(rows) < 20:
        return out
    closes = [k["close"] for k in rows if k.get("close") is not None]
    highs = [k["high"] for k in rows if k.get("high") is not None]
    vols = [k["volume"] for k in rows if k.get("volume") is not None]
    if len(closes) >= 250:
        ma250 = sum(closes[-250:]) / 250.0
        out["dist_ma250"] = (closes[-1] / ma250 - 1.0) * 100.0
    if highs:
        peak = max(highs[-250:])
        if peak > 0:
            out["drawdown_52w"] = (1.0 - closes[-1] / peak) * 100.0
    if len(vols) >= 21:
        base = sum(vols[-21:-1]) / 20.0
        if base > 0:
            out["vol_ratio_20d"] = vols[-1] / base
    last = rows[-1]
    out["last_volume"] = last.get("volume")
    if last.get("turnover") is not None:
        out["turnover_pct"] = last["turnover"]
        out["turnover_method"] = "vendor"
    elif last.get("volume") and free_shares:
        out["turnover_pct"] = last["volume"] / free_shares * 100.0
        out["turnover_method"] = "volume/free_shares"
    return out


def trading_from_closes(hist: List[Dict[str, Any]], asof: date) -> Dict[str, Any]:
    """用日收盘价序列兜底算均线偏离与回撤。

    K 线接口不稳定，而估值历史本身就是逐日收盘价，用它算 MA250/回撤同样成立，
    只是拿不到成交量与换手率（这两个指标缺失时由评分引擎重归一化，不补零）。
    """
    rows = [h for h in hist if h.get("close") and h.get("date") and date.fromisoformat(h["date"]) <= asof]
    rows.sort(key=lambda h: h["date"])
    out: Dict[str, Any] = {"as_of": rows[-1]["date"] if rows else None, "source": "closes"}
    closes = [h["close"] for h in rows]
    if len(closes) >= 250:
        ma250 = sum(closes[-250:]) / 250.0
        out["dist_ma250"] = (closes[-1] / ma250 - 1.0) * 100.0
    if closes:
        peak = max(closes[-250:])
        if peak > 0:
            out["drawdown_52w"] = (1.0 - closes[-1] / peak) * 100.0
    return out


# ---------------------------------------------------------------- 主流程

def build(
    raw: Dict[str, Any],
    analysis_date: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    wacc: float = 0.09,
    terminal_growth: float = 0.025,
) -> Dict[str, Any]:
    """把取数结果装配成引擎可直接消费的输入。返回 {input, fill, stock}。"""
    validate_raw(raw)
    asof = date.fromisoformat(analysis_date) if analysis_date else date.today()
    if asof > date.today():
        raise SourceError(f"分析时点 {asof} 晚于当前日期，PIT 无意义", "BAD_DATE")

    reports = available_reports(raw, asof)
    if not reports:
        upcoming = sorted(raw.get("reports", []), key=lambda r: r.get("notice_date") or "")
        hint = ""
        if upcoming:
            hint = f"；最早可得报告 {upcoming[0].get('report_date')} 于 {upcoming[0].get('notice_date')} 披露"
        raise SourceError(
            f"分析时点 {asof} 前无已披露定期报告（不得使用未来数据）{hint}", "NO_PIT_DATA"
        )

    latest = reports[0]
    by_period = {r["report_date"]: r for r in reports}
    period = latest["report_date"]
    notice = latest["notice_date"]
    derivations: List[Dict[str, Any]] = []

    def d(field_id: str, method: str, formula: Optional[str], note: str = "") -> None:
        derivations.append({
            "field_id": field_id, "method": method,
            "formula": formula or "", "note": note,
        })

    data: Dict[str, Dict[str, Any]] = {g: {} for g in ("market", "macro", "industry", "company", "financial", "valuation")}

    # ---- 估值历史（严格截断到 asof）
    hist = [h for h in raw.get("valuation_hist", []) if h.get("date") and date.fromisoformat(h["date"]) <= asof]
    hist.sort(key=lambda h: h["date"])
    latest_hist = hist[-1] if hist else None
    quote = raw.get("quote") or {}

    price = (latest_hist or {}).get("close") or quote.get("price")
    price_date = (latest_hist or {}).get("date") or quote.get("trade_date")
    if price:
        data["market"]["price_close"] = _field(price, price_date, price_date, SRC_QUOTE, "L2")
        d("market.price_close", "direct", f"取 {price_date} 收盘价 {price}")

    if latest_hist and latest_hist.get("pe") is not None:
        data["market"]["pe_ttm"] = _field(latest_hist["pe"], price_date, price_date, SRC_QUOTE, "L2")
        d("market.pe_ttm", "direct", f"取 {price_date} PE(TTM) {latest_hist['pe']}")
    if latest_hist and latest_hist.get("pb") is not None:
        data["market"]["pb"] = _field(latest_hist["pb"], price_date, price_date, SRC_QUOTE, "L2")

    # 总市值不作为独立字段写入：它不在数据字典里、也不进入任何评分/估值口径
    # （DCF 用股本 × 价格，相对法用 PE/PB 分位），写进来只会让"已填 N/39"对不上账。

    # 十年分位：以可得历史为窗口，不足 3 年则不给（不编造）
    def _pctile(key: str, field_id: str) -> Optional[List[float]]:
        start = asof - timedelta(days=3650)
        window = [h[key] for h in hist if h.get(key) is not None and date.fromisoformat(h["date"]) >= start]
        if len(window) < 500 or not latest_hist or latest_hist.get(key) is None:
            return None
        span_years = (asof - date.fromisoformat(hist[0]["date"])).days / 365.25
        pct = percentile_of(window, latest_hist[key])
        data["market"][field_id.split(".", 1)[1]] = _field(
            round(pct, 1), price_date, price_date,
            f"{SRC_QUOTE}（窗口 {hist[0]['date']}~{price_date}，{len(window)} 个交易日）", "L2",
        )
        d(field_id, "percentile", f"当前 {latest_hist[key]:.2f} 在 {len(window)} 个交易日样本中的中位秩分位",
          f"实际窗口约 {span_years:.1f} 年" + ("（不足十年，已按可得数据标注）" if span_years < 9.5 else ""))
        return window

    pe_window = _pctile("pe", "market.pctile_pe_10y")
    _pctile("pb", "market.pctile_pb_10y")
    if pe_window:
        vals = sorted(pe_window)
        p25, p75 = _quantile(vals, 0.25), _quantile(vals, 0.75)
        data["valuation"]["pe_hist_p25"] = _field(round(p25, 2), price_date, price_date, SRC_QUOTE, "L2")
        data["valuation"]["pe_hist_p75"] = _field(round(p75, 2), price_date, price_date, SRC_QUOTE, "L2")
        d("valuation.pe_hist_p25", "quantile", f"样本 P25 = {p25:.2f}")
        d("valuation.pe_hist_p75", "quantile", f"样本 P75 = {p75:.2f}")

    # ---- 交易面（K 线优先，缺失则用日收盘价序列兜底）
    free_shares = (latest_hist or {}).get("free_shares")
    tm = trading_metrics(raw.get("kline", []), asof, free_shares)
    fb = trading_from_closes(hist, asof)
    kline_src = SRC_KLINE
    if "sina" in (tm.get("vendor") or []):
        kline_src = SRC_KLINE_SINA
    for key in ("turnover_pct", "vol_ratio_20d", "dist_ma250", "drawdown_52w"):
        value, src, day = tm.get(key), kline_src, tm.get("as_of")
        if value is None and key in ("dist_ma250", "drawdown_52w") and fb.get(key) is not None:
            value, src, day = fb[key], f"{SRC_QUOTE}（日收盘价序列）", fb.get("as_of")
        if value is not None and day:
            data["market"][key] = _field(round(value, 2), day, day, src, "L2")
            formula = _trading_formula(key, day)
            if key == "turnover_pct" and tm.get("turnover_method") == "volume/free_shares" and free_shares:
                # 必须用 as_of 当日的成交量（K 线里可能还有更晚的行，不能取末行）
                formula = (f"{day} 成交量 {tm.get('last_volume') or 0:,.0f} 股 ÷ 流通 A 股 "
                           f"{free_shares:,.0f} 股 ×100")
            d(f"market.{key}", "computed", formula,
              "成交量口径为「股」；停牌/一字板日换手率会失真" if key == "turnover_pct" else "")

    # ---- 财务：TTM 化
    np_ttm, np_method, np_formula = ttm(by_period, latest, "np")
    rev_ttm, rev_method, rev_formula = ttm(by_period, latest, "revenue")
    eps_ttm, eps_method, eps_formula = ttm(by_period, latest, "eps")
    ocf_ttm, ocf_method, ocf_formula = ttm(by_period, latest, "ocf")

    if rev_ttm is not None:
        data["financial"]["rev_growth"] = _field(round(latest.get("rev_yoy") or 0.0, 2), period, notice, SRC_FIN, "L2", period)
        d("financial.rev_growth", "direct", f"取 {period} 营业收入同比 {latest.get('rev_yoy')}%",
          "为累计同比（年初至报告期末），非单季同比")
    if latest.get("np_yoy") is not None:
        data["financial"]["np_growth"] = _field(round(latest["np_yoy"], 2), period, notice, SRC_FIN, "L2", period)

    equity = latest.get("equity")
    prev_same = by_period.get(f"{date.fromisoformat(period).year - 1}-{period[5:]}")
    avg_equity = equity
    if equity and prev_same and prev_same.get("equity"):
        avg_equity = (equity + prev_same["equity"]) / 2.0
    if np_ttm is not None and avg_equity:
        roe_ttm = np_ttm / avg_equity * 100.0
        data["financial"]["roe_ttm"] = _field(round(roe_ttm, 2), period, notice, SRC_FIN, "L2", period)
        d("financial.roe_ttm", "computed",
          f"归母净利 TTM {np_ttm / 1e8:,.2f} 亿 ÷ 平均归母权益 {(avg_equity / 1e8):,.2f} 亿 ×100 = {roe_ttm:.2f}%",
          f"净利口径：{np_formula or np_method}")

    if ocf_ttm is not None and np_ttm:
        ratio = ocf_ttm / np_ttm
        data["financial"]["ocf_np_ratio"] = _field(round(ratio, 2), period, notice, SRC_FIN, "L2", period)
        d("financial.ocf_np_ratio", "computed", f"经营现金流 TTM {ocf_ttm / 1e8:,.2f} 亿 ÷ 归母净利 TTM {np_ttm / 1e8:,.2f} 亿 = {ratio:.2f}",
          "未做季节性调整；金融类公司口径可比性弱")

    if latest.get("roic") is not None:
        months = _months_in(date.fromisoformat(period))
        roic_ttm = latest["roic"] * 12.0 / months if months else latest["roic"]
        data["financial"]["roic"] = _field(round(roic_ttm, 2), period, notice, SRC_FIN, "L2", period)
        d("financial.roic", "annualized", f"{latest['roic']:.2f}% × 12/{months} = {roic_ttm:.2f}%",
          "YTD 口径年化近似，非严格滚动 12 个月")
        data["financial"]["wacc_assumed"] = _field(round(wacc * 100, 2), period, notice,
                                                   f"{SRC_RULE}（默认 {wacc:.1%}，位于设计文档 8%-12% 区间）", "L3", period)
        d("financial.wacc_assumed", "assumption", f"默认 WACC {wacc:.1%}，须按公司资本结构人工校准")

    if latest.get("gm") is not None:
        gm_prev = prev_same.get("gm") if prev_same else None
        if gm_prev is not None:
            data["financial"]["gm_trend"] = _field(round(latest["gm"] - gm_prev, 2), period, notice, SRC_FIN, "L2", period)
            d("financial.gm_trend", "computed", f"{latest['gm']:.2f}% − {gm_prev:.2f}% = {latest['gm'] - gm_prev:+.2f}pp",
              f"对比基期为 {prev_same['report_date']}")

    bal = _pick_balance(raw, asof)
    if bal:
        debt = sum(bal.get(k) or 0.0 for k in ("short_loan", "long_loan", "bond_payable", "noncurrent_liab_1y"))
        assets = bal.get("total_assets")
        if assets:
            ratio = debt / assets * 100.0
            data["financial"]["int_debt_ratio"] = _field(round(ratio, 2), bal["report_date"], bal["notice_date"], SRC_BAL, "L2", bal["report_date"])
            d("financial.int_debt_ratio", "computed",
              f"(短借+长借+应付债券+一年内到期) {debt / 1e8:,.2f} 亿 ÷ 总资产 {assets / 1e8:,.2f} 亿 = {ratio:.2f}%")
        if bal.get("goodwill") and bal.get("parent_equity"):
            gw = bal["goodwill"] / bal["parent_equity"] * 100.0
            data["financial"]["goodwill_pct"] = _field(round(gw, 2), bal["report_date"], bal["notice_date"], SRC_BAL, "L2", bal["report_date"])
            d("financial.goodwill_pct", "computed", f"商誉 {bal['goodwill'] / 1e8:,.2f} 亿 ÷ 归母权益 {bal['parent_equity'] / 1e8:,.2f} 亿 = {gw:.2f}%")

    # ---- 营运资本增速差：应收+存货 的同比增速 − 营收同比增速（pp）
    # 正值 = 应收/存货比收入涨得快，意味着增长靠赊销或压库换来，现金含量会先恶化。
    bal_periods = (bal or {}).get("periods") or []
    if bal_periods and latest.get("rev_yoy") is not None:
        cur = next((p for p in bal_periods if p["report_date"] == period), None)
        # 上年同期：年份减一，月日保持不变（注意 period[4] 是连字符，必须显式补回）
        prev = next((p for p in bal_periods
                     if p["report_date"] == f"{int(period[:4]) - 1}-{period[5:]}"), None)
        if cur and prev:
            ar_now, inv_now = cur.get("accounts_rece"), cur.get("inventory")
            ar_prev, inv_prev = prev.get("accounts_rece"), prev.get("inventory")
            if None not in (ar_now, inv_now, ar_prev, inv_prev) and (ar_prev + inv_prev) > 0:
                wc_now, wc_prev = ar_now + inv_now, ar_prev + inv_prev
                wc_growth = (wc_now / wc_prev - 1.0) * 100.0
                gap = wc_growth - latest["rev_yoy"]
                data["financial"]["wc_growth_gap"] = _field(
                    round(gap, 2), period, notice, SRC_BAL, "L2", period)
                d("financial.wc_growth_gap", "computed",
                  f"(应收 {ar_now / 1e8:,.2f} + 存货 {inv_now / 1e8:,.2f}) ÷ "
                  f"(应收 {ar_prev / 1e8:,.2f} + 存货 {inv_prev / 1e8:,.2f}) − 1 = {wc_growth:+.2f}%，"
                  f"减营收同比 {latest['rev_yoy']:+.2f}% → {gap:+.2f}pp",
                  f"对比基期 {prev['report_date']}；未含应付账款，属「营运资本占用」单侧口径")

    if eps_ttm is not None:
        data["valuation"]["eps_ttm"] = _field(round(eps_ttm, 3), period, notice, SRC_FIN, "L2", period)
        d("valuation.eps_ttm", "ttm", eps_formula or eps_method, "EPS 采用基本每股收益口径")

    # ---- 同业 PE 中位数：相对估值的横向锚（与自身历史分位互为交叉检验）
    pv = raw.get("peer_val") or {}
    if pv.get("median_pe") is not None:
        data["valuation"]["peer_pe_median"] = _field(
            pv["median_pe"], pv["trade_date"], pv["trade_date"],
            f"{SRC_QUOTE}（{pv['board_name']} 板块 {pv['samples']} 家中位数）", "L2")
        d("valuation.peer_pe_median", "computed",
          f"{pv['board_name']}（{pv['samples']} 家，剔除亏损与 >300 倍）PE(TTM) 中位数 {pv['median_pe']}；"
          f"本公司同日 {pv.get('own_pe')}",
          "东财板块口径，与申万二级不完全一致；中位数对极值不敏感但不反映龙头溢价")

    # ---- 机构持仓变化：前十大流通股东中机构合计占流通比的变化（pp）
    fh = raw.get("free_holders") or {}
    if fh.get("chg_pp") is not None:
        data["market"]["inst_hold_chg_pct"] = _field(
            round(fh["chg_pp"], 2), fh["period"], fh["notice"], SRC_FREEHOLD, "L2", fh["period"])
        d("market.inst_hold_chg_pct", "computed",
          f"{fh['period']} 机构合计 {fh['org_ratio']}%（{fh['org_count']} 家）"
          f" − {fh['prev_period']} 的 {fh['prev_org_ratio']}% = {fh['chg_pp']:+.2f}pp",
          "口径仅前十大流通股东，新进/退出会同时改变分子分母，属方向性证据而非精确仓位")

    # ---- 判断层：能代理的自动补（每个都带公式与误差方向），补不了的留给人工
    proxied = _fill_proxies(data, raw, asof, derivations, latest, notice, wacc)
    # 客户集中度走年报原文（L1），不走代理：这一项有真实披露，不需要推断
    customer = _fill_customer_annual(data, raw, asof, derivations)

    # ---- DCF 假设（机械外推，全部标注）
    cagr, cagr_note = _annual_cagr(reports, "revenue", 3)
    g_raw = cagr if cagr is not None else (latest.get("rev_yoy") or 0.0) / 100.0
    g_base = _clamp(g_raw, -0.05, 0.30)
    ebit_margin = (latest.get("op_margin") or 12.0) / 100.0
    ebit_margin = _clamp(ebit_margin, 0.02, 0.60)
    tax_rate = _clamp(latest.get("tax_rate") or 0.15, 0.0, 0.35)
    shares = (latest_hist or {}).get("shares") or 0.0
    revenue_base = (rev_ttm or 0.0) / 1e8
    net_debt = 0.0
    if bal:
        cash = bal.get("monetaryfunds") or 0.0
        debt = sum(bal.get(k) or 0.0 for k in ("short_loan", "long_loan", "bond_payable", "noncurrent_liab_1y"))
        net_debt = (debt - cash) / 1e8

    if not (shares and revenue_base):
        raise SourceError("缺少总股本或营收，无法构建 DCF（不猜测股本）", "NO_DCF_INPUT")

    def _path(g: float, decay: Tuple[float, ...]) -> List[float]:
        return [round(g * k, 4) for k in decay]

    dcf = {
        "base": {
            "wacc": wacc, "terminal_growth": terminal_growth,
            "revenue_growth_path": _path(g_base, (1.0, 0.85, 0.70, 0.55, 0.40)),
            "ebit_margin_path": [round(ebit_margin * k, 4) for k in (1.0, 1.01, 1.02, 1.025, 1.03)],
            "tax_rate": round(tax_rate, 3), "dep_amort_pct_rev": 0.05, "capex_pct_rev": 0.07,
            "nwc_pct_rev_delta": 0.01, "revenue_base": round(revenue_base, 2),
            "net_debt": round(net_debt, 2), "shares": round(shares / 1e8, 4),
        },
        "optimistic": {
            "wacc": round(wacc - 0.002, 4), "terminal_growth": min(0.028, terminal_growth + 0.003),
            "revenue_growth_path": _path(_clamp(g_base * 1.5, -0.02, 0.35), (1.0, 0.9, 0.78, 0.65, 0.5)),
            "ebit_margin_path": [round(ebit_margin * k, 4) for k in (1.0, 1.03, 1.06, 1.08, 1.10)],
            "tax_rate": round(tax_rate, 3), "dep_amort_pct_rev": 0.05, "capex_pct_rev": 0.075,
            "nwc_pct_rev_delta": 0.01, "revenue_base": round(revenue_base, 2),
            "net_debt": round(net_debt, 2), "shares": round(shares / 1e8, 4),
        },
        "pessimistic": {
            "wacc": round(wacc + 0.012, 4), "terminal_growth": max(0.015, terminal_growth - 0.005),
            "revenue_growth_path": _path(max(-0.05, g_base * 0.4), (1.0, 0.85, 0.7, 0.55, 0.45)),
            "ebit_margin_path": [round(ebit_margin * k, 4) for k in (1.0, 0.97, 0.94, 0.92, 0.90)],
            "tax_rate": round(tax_rate, 3), "dep_amort_pct_rev": 0.05, "capex_pct_rev": 0.065,
            "nwc_pct_rev_delta": 0.012, "revenue_base": round(revenue_base, 2),
            "net_debt": round(net_debt, 2), "shares": round(shares / 1e8, 4),
        },
    }
    derivations.append({
        "field_id": "assumptions.dcf", "method": "mechanical_extrapolation",
        "formula": (f"基准增速 {g_base:.2%}（{cagr_note or '近三年营收 CAGR 不可得 → 用最新营收同比'}），"
                    f"营业利润率 {ebit_margin:.2%}，税率 {tax_rate:.1%}，WACC {wacc:.1%}，永续 {terminal_growth:.1%}"),
        "note": "机械外推，非市场一致预期；净债务/资本开支/折旧摊销为默认假设，须人工校准",
    })

    eps_base = (eps_ttm or 0.0) * (1 + _path(g_base, (1.0,))[0])
    eps_fwd = {
        "base": round(eps_base, 3),
        "optimistic": round((eps_ttm or 0.0) * (1 + _clamp(g_base * 1.5, -0.02, 0.35)), 3),
        "pessimistic": round((eps_ttm or 0.0) * (1 + max(-0.05, g_base * 0.4)), 3),
    }

    assumptions = {
        "_kind": "assumption（自动模式：机械外推假设，与实际数据严格隔离；报告中标注为假设）",
        "eps_fwd": eps_fwd,
        "scenario_probabilities": {"optimistic": 0.25, "base": 0.5, "pessimistic": 0.25},
        "dcf": dcf,
        "scenario_notes": _scenario_notes(g_base, ebit_margin, eps_ttm),
    }

    # ---- 风险与催化剂（数据驱动的最小集，宁少不编）
    risks = _auto_risks(data, latest, pe_window, latest_hist)
    catalysts = _auto_catalysts(asof)
    logic_items = _auto_logic(data)

    # ---- meta
    st_flag = "ST" in (raw["stock"].get("name") or "").upper()
    meta = {
        "stock": {
            "name": raw["stock"]["name"], "code": raw["stock"]["code"],
            "exchange": raw["stock"].get("exchange", ""), "currency": "CNY",
        },
        "analysis_date": asof.isoformat(),
        "company_type": "growth" if g_base > 0.20 else "steady_growth",
        "relative_valuation_metric": "PE" if (np_ttm or 0) > 0 else "PB",
        "generated_at": raw.get("meta", {}).get("fetched_at") or "",
        "flags": {
            "st": st_flag, "audit_qualified": False, "regulatory_investigation": False,
            "major_fraud_red_flag": False, "suspended": False, "restructuring": False,
            "new_listing_under_1y": False,
        },
        "red_flags": [],
    }

    inp: Dict[str, Any] = {
        "_note": ("由自动取数装配：财务/行情字段来自公开数据源（L2），判断类字段留空待人工补充。"
                  "所有假设为机械外推。"),
        "meta": meta,
        "data": {k: v for k, v in data.items() if v},
        "assumptions": assumptions,
        "risks": risks,
        "catalysts": catalysts,
        "logic_items": logic_items,
    }
    if overrides:
        _apply_overrides(inp, overrides)

    fill = _fill_report(inp, derivations, raw, asof, latest, np_method, rev_method)
    fill["proxied"] = proxied
    fill["customer_annual"] = customer
    if proxied.get("proxy"):
        fill["warnings"].append(
            f"{len(proxied['proxy'])} 个判断项由代理指标自动推断（不是研究结论，"
            "误差方向见 derivations 中 method=proxy 的条目）：" + "、".join(proxied["proxy"]))

    # 装配完成后再校验 DCF：此时半成品（输入 + 填充清单）已经齐备，
    # 失败时一并带出去，使用者可在界面上直接改假设重算，不必从零手抄数据。
    _ensure_dcf_feasible(
        dcf,
        payload={"stock": raw["stock"], "analysis_date": asof.isoformat(),
                 "input": inp, "fill": fill},
    )

    return {"input": inp, "stock": raw["stock"], "fill": fill}


# ---------------------------------------------------------------- 文本生成

def _trading_formula(key: str, as_of: str) -> str:
    return {
        "turnover_pct": f"{as_of} 日换手率（取自日 K 线）",
        "vol_ratio_20d": "当日成交量 ÷ 前 20 日均量",
        "dist_ma250": "收盘价 ÷ 250 日均线 − 1",
        "drawdown_52w": "1 − 收盘价 ÷ 近 250 日最高价",
    }.get(key, as_of)


def _ensure_dcf_feasible(dcf: Dict[str, Any],
                         payload: Optional[Dict[str, Any]] = None) -> None:
    """机械假设跑不出正股权价值时，明确拒绝，而不是调参硬凑。

    典型场景：薄利多销 + 重资本开支的制造业，或净债务相对盈利过高的公司。
    此时代机械外推出的 FCFF 覆盖不了债务，DCF 结论没有意义。
    与其"改参数直到能出数"，不如直接告诉使用者：这只票需要人工设定假设。
    """
    from .valuation import run_dcf

    try:
        run_dcf(dcf)
    except ValueError as exc:
        raise SourceError(
            f"自动假设下 DCF 不成立：{exc}。该标的的利润率/资本开支结构与机械外推假设不匹配，"
            "自动模式不调参硬凑——请人工设定 DCF 假设（营收增速、营业利润率、资本开支占比、净债务）后再分析，"
            "或改用相对估值口径。",
            "DCF_INFEASIBLE",
            payload=payload,
        ) from exc


def _scenario_notes(g: float, margin: float, eps_ttm: Optional[float]) -> Dict[str, Dict[str, Any]]:
    g_opt = _clamp(g * 1.5, -0.02, 0.35)
    g_pes = max(-0.05, g * 0.4)
    return {
        "optimistic": {
            "key_assumptions": [f"营收增速维持 {g_opt:.1%} 且利润率每年改善约 3%（机械外推，非一致预期）",
                                "WACC 下修 20bp，永续增长 2.8%"],
            "drivers": ["行业景气超预期", "份额提升或新产品放量"],
            "falsification": f"若下期营收同比低于 {g_opt * 100 * 0.6:.1f}% 或毛利率同比下滑，乐观情景失效",
        },
        "base": {
            "key_assumptions": [f"营收增速自 {g:.1%} 逐年递减至 {g * 0.4:.1%}",
                                f"营业利润率由 {margin:.1%} 起每年小幅改善"],
            "drivers": ["现有业务自然增长", "利润率稳定"],
            "falsification": f"若营收增速跌破 {g * 100 * 0.5:.1f}% 或利润率下行超过 2pp，基准情景失效",
        },
        "pessimistic": {
            "key_assumptions": [f"营收增速降至 {g_pes:.1%}", "利润率每年收缩约 3%，WACC 上修 120bp"],
            "drivers": ["需求走弱", "价格战/成本上行"],
            "falsification": "若营收与利润率同时低于悲观路径，需重估公司竞争力",
        },
    }


def _auto_risks(data: Dict[str, Dict[str, Any]], latest: Dict[str, Any],
                pe_window: Optional[List[Any]], latest_hist: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    risks: List[Dict[str, Any]] = []
    pct = data.get("market", {}).get("pctile_pe_10y", {}).get("value")

    if pct is not None:
        if pct >= 70:
            prob, imp, lvl = 4, 4, "高"
        elif pct >= 30:
            prob, imp, lvl = 3, 3, "中"
        else:
            prob, imp, lvl = 2, 3, "中"
        risks.append({
            "id": "R1", "name": "估值均值回归", "category": "price",
            "probability": prob, "impact": imp,
            "trigger": {"metric": "market.pctile_pe_10y", "threshold": f"> {pct + 10:.0f}% 或快速回落至 <30%"},
            "mitigation": "以 DCF 安全边际为锚，分位 >80% 时不追加；用盈利增长消化估值前不提升权重",
            "evidence": f"自动规则 R_VALUATION_PCT：当前 PE 分位 {pct}%（{pe_window and len(pe_window)} 个交易日样本），档位{lvl}",
        })

    np_yoy = latest.get("np_yoy")
    if np_yoy is not None:
        prob = 4 if np_yoy < 0 else 3
        risks.append({
            "id": f"R{len(risks) + 1}", "name": "业绩增速不及预期", "category": "demand",
            "probability": prob, "impact": 4,
            "trigger": {"metric": "financial.np_growth", "threshold": f"连续两期低于 {min(np_yoy, 0):.1f}%"},
            "mitigation": "跟踪季度营收/净利同比与一致预期差；增速转负即触发财务维度重估",
            "evidence": f"自动规则 R_EARNINGS_MOMENTUM：最近报告期（{latest['report_date']} 披露于 {latest['notice_date']}）净利同比 {np_yoy:.2f}%",
        })

    ocf_ratio = data.get("financial", {}).get("ocf_np_ratio", {}).get("value")
    if ocf_ratio is not None:
        prob, imp = (4, 4) if ocf_ratio < 0.7 else ((3, 3) if ocf_ratio < 1.0 else (2, 3))
        risks.append({
            "id": f"R{len(risks) + 1}", "name": "盈利现金含量下降", "category": "finance",
            "probability": prob, "impact": imp,
            "trigger": {"metric": "financial.ocf_np_ratio", "threshold": "< 0.7（连续两期）"},
            "mitigation": "核对应收/存货与收入增速差；现金流与利润背离时下调财务维度得分",
            "evidence": f"自动规则 R_CASH_CONVERSION：经营现金流/净利润 = {ocf_ratio}",
        })

    if not risks:
        raise SourceError("可用数据不足以生成任何风险条目（不编造）", "NO_RISK_INPUT")
    return risks


def _auto_catalysts(asof: date) -> List[Dict[str, Any]]:
    """只给法定披露节点：这是事实，不是预测。"""
    specs = [(12, 31, "年报", date(asof.year + 1, 4, 30)),
             (3, 31, "一季报", date(asof.year, 4, 30)),
             (6, 30, "中报", date(asof.year, 8, 31)),
             (9, 30, "三季报", date(asof.year, 10, 31))]
    upcoming = []
    for y in (asof.year - 1, asof.year, asof.year + 1):
        for m, dd, label, base_deadline in specs:
            deadline = base_deadline.replace(year=y + 1) if m == 12 else base_deadline.replace(year=y)
            if deadline > asof:
                upcoming.append((deadline, label, date(y, m, dd)))
    upcoming.sort(key=lambda x: x[0])
    out = []
    for i, (deadline, label, period) in enumerate(upcoming[:2], start=1):
        out.append({
            "date": deadline.isoformat(),
            "event": f"{label}披露（法定截止日，{period.year} 年 {label}）",
            "type": "earnings", "direction": "?", "strength": "中",
            "expected_impact": "定期报告是验证跟踪清单阈值的主要时点；实际披露日以公司预约公告为准",
            "priced_in": "?",
            "verify_method": "对照跟踪清单中的营收增速 / ROE / 现金流阈值逐项核验",
            "source": "交易所定期报告披露规则（法定截止日）", "source_tier": "L1",
            "warning": "自动生成：仅有法定截止日，方向与定价程度未知，需人工补入事件型催化",
        })
    return out


def _auto_logic(data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    roe = data.get("financial", {}).get("roe_ttm", {}).get("value")
    ocf = data.get("financial", {}).get("ocf_np_ratio", {}).get("value")
    items = []
    if roe is not None:
        items.append({
            "statement": f"盈利质量支撑：ROE(TTM) {roe}%" + (f"、经营现金流/净利润 {ocf}x" if ocf else "")
                         + "；若可持续，则估值中枢有基本面支撑",
            "transmission": "ROE 与现金转化率维持 → 盈利可持续性成立 → 估值折价收窄",
            "priced_in": "部分定价",
            "verify_metric": "financial.roe_ttm",
            "verify_threshold": f"≥ {roe * 0.8:.1f}%（当前 {roe}% 的八成）",
            "source": "自动规则推导（需人工复核，非研究结论）",
        })
    return items


# ---------------------------------------------------------------- 判断层代理

def _fill_proxies(data: Dict[str, Any], raw: Dict[str, Any], asof: date,
                  derivations: List[Dict[str, Any]], latest: Dict[str, Any],
                  notice: str, wacc: float) -> Dict[str, List[str]]:
    """把判断层里存在可复算代理的字段补上。

    返回 {"observed": [...], "proxy": [...]}，供缺口报告与界面区分「直接取到」与「代理推断」。
    代理同样受 PIT 与来源等级约束（L2），但必须带 proxy 标记，报告里不得与人工结论混同。
    """
    asof_s = asof.isoformat()
    buckets: Dict[str, List[str]] = {"observed": [], "proxy": []}

    def put(f: "proxies.ProxyField") -> None:
        group, key = f.field_id.split(".", 1)
        data.setdefault(group, {})
        field = _field(f.value, f.as_of, f.available_at, f.source, "L2", f.period_end)
        if f.basis == "proxy":
            field["proxy"] = True
            field["proxy_note"] = f.note
        data[group][key] = field
        derivations.append({
            "field_id": f.field_id,
            "method": "observed" if f.basis == "observed" else "proxy",
            "formula": f.formula,
            "note": f.note,
        })
        buckets.setdefault(f.basis, []).append(f.field_id)

    for f in proxies.macro_fields(raw.get("macro") or {}, asof_s, raw.get("rate_10y")):
        put(f)
    for f in proxies.industry_fields(raw.get("industry") or {}, asof_s):
        put(f)
    for f in proxies.company_fields(
        reports=raw.get("reports") or [], latest=latest,
        industry=raw.get("industry"), pledge=raw.get("pledge"),
        dividends=raw.get("dividends") or [], latest_notice=notice,
        asof=asof_s, wacc=wacc,
    ):
        put(f)
    return buckets


# ---------------------------------------------------------------- 年报客户集中度

CUSTOMER_FID = "company.customer_concentration"


def _fill_customer_annual(data: Dict[str, Any], raw: Dict[str, Any], asof: date,
                          derivations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把年报正文里的「第 1 名客户占比」写成观测值。

    三档，一条比一条保守：

    1. 有单客户占比 → 观测值（L1）。年报原文是监管强制披露的一手材料，
       比东财那种二手整理高一个来源等级，所以这里不写 L2。
    2. 只有前五名合计 → **不填值**。合计是"前五家加起来"，不是"最大一家"：
       宁德时代前五合计 38.96%、第一大客户 13.73%，把前者写进去会让
       risk.customer_conc 直接跳到高风险档。上界留给缺口说明，值继续留空。
    3. 没披露 / 解析失败 / 银行保险不适用 → 什么都不动，照旧落到人工判断项。

    返回年报披露块（供 fill 里带出溯源信息），不论第几档都返回，便于界面解释"为什么空着"。
    """
    cust = raw.get("customer")
    if not cust:
        return None

    top = cust.get("top_ratio")
    period, notice = cust.get("period"), cust.get("notice_date")
    if top is None or not period or not notice:
        return cust
    if date.fromisoformat(notice) > asof:
        # 源侧已按公告日过滤，这里是第二道闸：分析时点之前没公告的年报，一个字都不能用
        return cust

    denom = cust.get("denominator") or "年度销售总额"
    data["company"]["customer_concentration"] = _field(
        round(float(top), 2), period, notice, SRC_CNINFO, "L1", period)

    page = cust.get("page")
    form = cust.get("form")
    check = cust.get("cross_check") or {}
    if form == "table":
        rows = cust.get("rows")
        formula = (f"取 {cust.get('title') or period + ' 年报'} 第 {page} 页"
                   f"「公司前 5 大客户资料」中占比最高的一家（{cust.get('top_name') or '第 1 名'}）"
                   f"{top}%；分母：{denom}")
        note = (f"表内 {rows} 行占比之和 {check.get('sum_ratio')}% 与合计 "
                f"{check.get('total_ratio')}% 交叉校验通过，未发生列错位")
    else:
        formula = (f"取 {cust.get('title') or period + ' 年报'} 第 {page} 页文字表述"
                   f"「第一名客户占 {top}%」（原文未给完整明细表）；分母：{denom}")
        note = "文字表述而非表格，无法做行列交叉校验；分母口径以原文为准，勿与销售总额口径混用"

    derivations.append({"field_id": CUSTOMER_FID, "method": "direct",
                        "formula": formula, "note": note})
    return cust


def _customer_gap_why(raw: Dict[str, Any]) -> str:
    """客户集中度留空时，说清是"哪种空"——面板上这一句话决定了研究者要不要去翻年报。"""
    cust = raw.get("customer") or {}
    if cust.get("top_ratio") is not None:
        return ""
    if cust.get("five_ratio") is not None:
        return (f"年报（{cust.get('period')}）只披露前五名客户合计 {cust['five_ratio']}%，"
                f"没给单客户占比——最大一家不超过这个数，需要读一次年报填单客户口径")
    if cust:
        return f"年报（{cust.get('period')}）没有这项披露：{cust.get('reason') or '正文未给出客户集中度'}"
    return "本次未取到年报原文（见取数告警），需要人工读一次"


# ---------------------------------------------------------------- 覆盖度报告

def _fill_report(inp: Dict[str, Any], derivations: List[Dict[str, Any]], raw: Dict[str, Any],
                 asof: date, latest: Dict[str, Any], np_method: str, rev_method: str) -> Dict[str, Any]:
    filled: List[str] = []
    for group, fields in inp["data"].items():
        for key, val in fields.items():
            if isinstance(val, dict) and val.get("value") is not None:
                filled.append(f"{group}.{key}")
    filled_set = set(filled)
    all_ids = list(FIELD_REGISTRY)

    pending = []
    for prefix, ids in JUDGEMENT_FIELDS.items():
        lack = [f for f in ids if f not in filled_set]
        if lack:
            why = JUDGEMENT_WHY[prefix]
            if CUSTOMER_FID in lack:
                extra = _customer_gap_why(raw)
                if extra:
                    why = f"{why}；{extra}"
            pending.append({"group": prefix, "fields": lack, "why": why, "kind": "judgement"})
    other = [f for f in OTHER_GAPS if f not in filled_set]
    if other:
        pending.append({"group": "other", "fields": other,
                        "why": "可机读但自动模式未取（避免把整理口径当事实）", "kind": "data"})

    # 收口：既没填、也不在上面两类的字段，必然是本次取数降级（如行情主机限流）丢掉的。
    # 必须显式进清单，否则"已填 + 待补 = 全文"的对账关系会断，缺口被藏起来。
    covered = set(f for g in pending for f in g["fields"])
    residual: Dict[str, List[str]] = {}
    for fid in all_ids:
        if fid in filled_set or fid in covered:
            continue
        residual.setdefault(fid.split(".", 1)[0], []).append(fid)
    for prefix, ids in residual.items():
        pending.append({"group": prefix, "fields": ids,
                        "why": "本次自动取数未得到：可能是数据源限流 / 缺表 / 同业样本不足，"
                               "也可能是该公司本身没有这一项（如无商誉、无应收），"
                               "看取数告警与本组字段名判断；不是判断项，确需时手动补",
                        "kind": "source"})

    warnings: List[str] = []

    # 年报口径字段（目前是客户集中度）与"最新报告期"是两把尺子，不能混。
    # 它一年只披露一次，取最近一期已公告年报即可，滞后 0~15 个月都属正常；
    # 用季频的更新周期去卡它，会得出"只有年报刚公布那几个月才算新鲜"的荒唐结论。
    annuals = [r for r in available_reports(raw, asof) if r["report_date"][5:] == "12-31"]
    latest_annual = annuals[0] if annuals else None

    if np_method == "annualized" or rev_method == "annualized":
        warnings.append("TTM 计算缺同期数据，已用年化近似（见 derivations）")
    if latest["report_date"][5:] != "12-31":
        tail = "；TTM/ROE 等累计口径按跨年拼接或年化处理"
        if latest_annual:
            tail += (f"；只在年报披露的字段（客户集中度）取最近一期年报 "
                     f"{latest_annual['report_date']}（{latest_annual['notice_date']} 公告），"
                     f"滞后一年内属正常，不随最新报告期前移")
        else:
            tail += "；尚无已公告年报，只在年报披露的字段（客户集中度）本年无可用值"
        warnings.append(f"最新可得报告为 {latest['report_date']}（{latest['report_type']}），"
                        f"非完整年度{tail}")
    if not raw.get("balance"):
        warnings.append("未取到资产负债表：净债务按 0 假设，有息负债率/商誉缺失")
    unknown_flags = ["audit_qualified", "regulatory_investigation", "major_fraud_red_flag"]
    warnings.append("以下状态未经核验，默认按未触发处理，须人工确认：" + "、".join(unknown_flags))

    accounted = len(filled_set) + sum(len(g["fields"]) for g in pending)
    if accounted != len(all_ids):   # 自检：清单必须能对账到数据字典
        warnings.append(f"字段清单对账异常：已填 {len(filled_set)} + 待补 "
                        f"{accounted - len(filled_set)} ≠ 全文字段 {len(all_ids)}")

    return {
        "filled": sorted(filled),
        "filled_count": len(filled),
        "total_fields": len(all_ids),
        "completeness": round(len(filled) / len(all_ids), 3),
        "pending": pending,
        "derivations": derivations,
        "auto_items": {
            "risks": len(inp["risks"]), "catalysts": len(inp["catalysts"]),
            "logic_items": len(inp.get("logic_items", [])),
        },
        "as_of": {"analysis_date": asof.isoformat(), "latest_report_period": latest["report_date"],
                  "latest_report_notice": latest["notice_date"],
                  "latest_annual_period": latest_annual["report_date"] if latest_annual else None,
                  "latest_annual_notice": latest_annual["notice_date"] if latest_annual else None},
        # 只在年报披露的字段：界面手填时数据期要落在最近年报期末，
        # 不能记成录入当日（否则一年前的数会被显示成今天刚拿到）。
        "annual_fields": [s.field_id for s in FIELD_SPECS if s.annual],
        "warnings": warnings,
    }


def _apply_overrides(inp: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    """把人工判断合并进自动结果（用于界面上的快速补全）。

    人工值一旦覆盖代理值，就把 proxy 标记摘掉——它现在是研究结论，不再是推断。
    """
    for group, fields in (overrides.get("data") or {}).items():
        bucket = inp["data"].setdefault(group, {})
        for key, val in (fields or {}).items():
            if isinstance(val, dict) and "value" in val:
                clean = {k: v for k, v in val.items() if k not in ("proxy", "proxy_note")}
                bucket[key] = clean
