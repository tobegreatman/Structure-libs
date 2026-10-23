"""判断类字段的代理口径（proxy）。

判断项（政策取向、行业格局、护城河…）本质上是研究结论，机器不能"判断"。
但其中相当一部分存在公认的可复算代理：同业毛利率中位数、公司份额、ROIC 持续性……
把这些代理写出来，能显著提高自动化程度，代价是口径变粗、误差方向固定。

三条纪律（与设计文档 §19 一致）：
1. **只做计算，不碰网络**。输入是 raw 里已取到的事实，输出必须能被人手工复算；
2. **数据不足返回 None**。不补零、不用默认值、不猜——缺失就是缺失；
3. **一律落到与人工锚定相同的 1-5 档尺度**，便于人工结论直接覆盖，不需要换算。

每个代理都带 `basis`：
- `observed`：直接取到的观测值（如 M1 同比、质押比例），不是推断；
- `proxy`：由观测值按固定公式推断的判断档位，**不是研究结论**，误差方向在 note 里写明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

WACC_DEFAULT = 0.09
MIN_PEER_SAMPLES = 5


@dataclass
class ProxyField:
    """一个判断字段的代理结果。"""

    field_id: str
    value: Any
    as_of: str
    available_at: str
    source: str
    formula: str
    note: str                 # 误差方向 / 口径局限，给读者判断可信度
    basis: str = "proxy"      # observed | proxy
    confidence: str = "medium"
    period_end: Optional[str] = None


# ---------------------------------------------------------------- 档位工具

def _grade(value: Optional[float], cuts: Sequence[float], reverse: bool = False) -> Optional[int]:
    """按分界点把数值映射到 1-5 档，返回值恒在 1..5。

    cuts 是 4 个升序分界点，把实数轴切成 5 段：
      (-inf, c1) → 1 档, [c1, c2) → 2 档, [c2, c3) → 3 档, [c3, c4) → 4 档, [c4, inf) → 5 档

    reverse=True 用于「越小越好」的指标（毛利率标准差、质押比例）：档位整体翻转，
    最小的值落在 5 档（最优）。
    """
    if value is None:
        return None
    for i, cut in enumerate(cuts):
        if value < cut:
            return (5 - i) if reverse else (i + 1)
    return 1 if reverse else 5


def _round(value: float) -> int:
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def _weighted(parts: List[tuple], missing_to_none: bool = True) -> Optional[int]:
    """按 (档位, 权重) 加权取整；缺失分项按权重重归一化，而非补零。"""
    usable = [(g, w) for g, w in parts if g is not None]
    if not usable:
        return None
    total_w = sum(w for _, w in usable)
    if total_w <= 0:
        return None
    raw = sum(g * w for g, w in usable) / total_w
    return max(1, min(5, _round(raw)))


# ---------------------------------------------------------------- 宏观

def macro_fields(macro: Dict[str, Any], asof: str,
                 rate_10y: Optional[Dict[str, Any]] = None) -> List[ProxyField]:
    """M1 同比、PMI、10Y 利率同比变化是观测值，不是推断；政策取向不在本函数内（要读文本）。"""
    out: List[ProxyField] = []
    src = "东方财富·宏观月度序列（人行/统计局）"

    m1 = macro.get("m1") or {}
    if m1.get("value") is not None:
        out.append(ProxyField(
            field_id="macro.m1_yoy", value=round(float(m1["value"]), 2),
            as_of=m1.get("period", asof)[:7] + "-01" if m1.get("period") else asof,
            available_at=m1["available_at"], source=src, basis="observed", confidence="high",
            formula=(f"M1 同比 = {m1['value']:.1f}%（{m1.get('period_label', '')}）；"
                     f"发布日按次月 10-15 日的惯例保守取月末+{m1.get('lag_days_assumed', 15)} 天"),
            note="直接观测值；M1 口径 2025 年起含个人活期与非银支付备付金，与历史序列不完全可比",
        ))

    pmi = macro.get("pmi") or {}
    if pmi.get("value") is not None:
        out.append(ProxyField(
            field_id="macro.pmi", value=round(float(pmi["value"]), 2),
            as_of=pmi.get("period", asof)[:7] + "-01" if pmi.get("period") else asof,
            available_at=pmi["available_at"], source=src, basis="observed", confidence="high",
            formula=(f"制造业 PMI = {pmi['value']:.1f}（{pmi.get('period_label', '')}）；"
                     f"发布日按月末/次月 1 日惯例保守取月末+{pmi.get('lag_days_assumed', 2)} 天"),
            note="直接观测值；PMI 是环比扩散指数，只反映方向与力度，不代表同比增速",
        ))

    # 10Y 国债收益率同比变化：两个观测值相减，属算术而非推断，因此仍标 observed
    r = rate_10y or {}
    if r.get("chg_bp") is not None:
        out.append(ProxyField(
            field_id="macro.rate_10y_chg_yoy", value=round(float(r["chg_bp"]), 1),
            as_of=r["date"], available_at=r.get("available_at", r["date"]),
            source="中债·国债收益率曲线（10 年期，日频）", basis="observed", confidence="high",
            formula=(f"{r['date']} 10Y {r['value']:.4f}% − {r['prev_date']} {r['prev_value']:.4f}%"
                     f" = {r['chg_bp']:+.1f}bp（一年前取 365 天窗口内最近交易日，错位 "
                     f"{r.get('prev_offset_days', 365) - 365:+d} 天）"),
            note=("两个观测值相减，不是推断；中债曲线为日终估值，按次日可用。"
                  "bp 为正=利率上行（贴现率与融资成本上升，对估值逆风）；"
                  "节假日前后一年前锚点可能错位 1-3 天，对 bp 量级影响通常在 ±2bp 内"),
            period_end=r["date"],
        ))
    return out


# ---------------------------------------------------------------- 行业

def industry_fields(industry: Dict[str, Any], asof: str) -> List[ProxyField]:
    """同业对比口径：申万二级行业成员的财报中位数。"""
    if not industry or not industry.get("samples"):
        return []
    samples = int(industry["samples"])
    if samples < MIN_PEER_SAMPLES:
        return []

    name = industry.get("name") or "同业"
    period = industry.get("period") or ""
    avail = industry.get("deadline") or asof
    src = f"东方财富·业绩报表 + 中登行业归属（申万二级「{name}」，{samples} 家）"
    out: List[ProxyField] = []

    g = industry.get("rev_yoy_med")
    g_prev = industry.get("rev_yoy_prev_med")
    gm_yoy = industry.get("gm_yoy_med")

    # 行业增速：直接给数值，让量规引擎套用既有分档（不另立阈值）
    if g is not None:
        out.append(ProxyField(
            field_id="industry.industry_growth", value=round(float(g), 2),
            as_of=period, available_at=avail, source=src, period_end=period,
            formula=(f"行业营收增速 = {samples} 家同业 {period} 营收同比的中位数 = {g:.2f}%"
                     f"（披露截止 {avail}）"),
            note="口径是「同业上市公司营收增速中位数」，不等于行业增加值；"
                 "未上市份额大的行业会被龙头带偏",
            confidence="medium",
        ))

    # 量价趋势：价（毛利率同比）× 0.6 + 量（营收增速）× 0.4
    s_price = _grade(gm_yoy, [-2.0, -0.5, 0.5, 2.0])
    s_vol = _grade(g, [0.0, 2.0, 8.0, 15.0])
    trend = _weighted([(s_price, 0.6), (s_vol, 0.4)])
    if trend is not None:
        out.append(ProxyField(
            field_id="industry.price_trend", value=trend,
            as_of=period, available_at=avail, source=src, period_end=period,
            formula=(f"价：同业毛利率同比中位数 {gm_yoy:+.2f}pp → {s_price} 档（权重 0.6）；"
                     f"量：同业营收增速中位数 {g:.2f}% → {s_vol} 档（权重 0.4）；"
                     f"合成 {trend} 档"
                     if (gm_yoy is not None and g is not None) else
                     f"仅用可得分项合成 {trend} 档"),
            note="用毛利率代理「价」：成本端同步上行时，价格上行可能被低估；"
                 "量价背离时以价为主（0.6 权重）",
        ))

    # 生命周期：增速水平为主，增速快速回落降一档
    if g is not None:
        stage = _grade(g, [0.0, 5.0, 12.0, 20.0])
        if stage is not None:
            dropped = ""
            if stage >= 4 and g_prev is not None and (g - g_prev) <= -10.0:
                stage = max(1, stage - 1)
                dropped = f"；增速较上期 {g - g_prev:+.1f}pp 快速回落，降 1 档"
            out.append(ProxyField(
                field_id="industry.stage", value=stage,
                as_of=period, available_at=avail, source=src, period_end=period,
                formula=f"同业营收增速 {g:.2f}% → {stage} 档（分界 0/5/12/20）{dropped}",
                note="用增速代理阶段：无法区分「成熟但周期向上」与「成长中期」，"
                     "渗透率绝对水平未见，高估风险大于低估",
                confidence="low",
            ))

    # 公司卡位：营收份额
    share = industry.get("company_share")
    if share is not None:
        conc = _grade(float(share), [0.03, 0.08, 0.15, 0.30])
        if conc is not None:
            own = industry.get("company_revenue") or 0.0
            total = industry.get("total_revenue") or 0.0
            out.append(ProxyField(
                field_id="industry.concentration", value=conc,
                as_of=period, available_at=avail, source=src, period_end=period,
                formula=(f"公司营收份额 = {own / 1e8:,.1f} 亿 ÷ 同业合计 {total / 1e8:,.1f} 亿 "
                         f"= {share:.1%} → {conc} 档（分界 3%/8%/15%/30%）"),
                note="份额高≠有壁垒，但锚定本身就是「公司为龙头/核心供应商」，方向一致；"
                     "同业集合按申万二级取，跨行业竞争未覆盖",
            ))
    return out


# ---------------------------------------------------------------- 公司

def _annual(reports: List[Dict[str, Any]], field: str, years: int = 5) -> List[tuple]:
    """取最近 N 个年报的某字段，返回 [(年份, 值)]，按年份降序。"""
    rows = []
    for r in reports:
        period = r.get("report_date") or ""
        if not period.endswith("12-31"):
            continue
        val = r.get(field)
        if val is None:
            continue
        rows.append((period, val))
    rows.sort(reverse=True)
    return rows[:years]


def _stdev(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return var ** 0.5


def company_fields(reports: List[Dict[str, Any]], latest: Dict[str, Any],
                   industry: Optional[Dict[str, Any]], pledge: Optional[Dict[str, Any]],
                   dividends: List[Dict[str, Any]], latest_notice: str,
                   asof: str, wacc: float = WACC_DEFAULT) -> List[ProxyField]:
    """护城河、管理层记录、质押比例。逻辑强度/逻辑内核/客户集中度不在此处（无口径）。"""
    out: List[ProxyField] = []
    gm_annual = _annual(reports, "gm", 5)
    roic_annual = _annual(reports, "roic", 5)
    gm_peer = (industry or {}).get("gm_med")
    period = (industry or {}).get("period") or latest.get("report_date") or asof
    avail = (industry or {}).get("deadline") or latest_notice or asof

    # ---- 护城河：毛利率溢价（同报告期对比）+ 毛利率稳定性 + ROIC 持续性
    gm_own = latest.get("gm")
    spread = (gm_own - gm_peer) if (gm_own is not None and gm_peer is not None) else None
    a = _grade(spread, [0.0, 3.0, 8.0, 15.0])
    sd = _stdev([v for _, v in gm_annual])
    b = _grade(sd, [1.5, 3.0, 5.0, 8.0], reverse=True)
    if roic_annual:
        hits = sum(1 for _, v in roic_annual if v > wacc * 100.0)
        c = _grade(hits / len(roic_annual), [0.3, 0.5, 0.7, 0.9])
        persist = hits / len(roic_annual)
    else:
        c, persist = None, None
    moat = _weighted([(a, 0.4), (b, 0.3), (c, 0.3)])
    if moat is not None:
        parts = []
        if a is not None:
            parts.append(f"毛利率溢价 {spread:+.1f}pp（公司 {gm_own:.1f}% vs 同业同报告期中位 "
                         f"{gm_peer:.1f}%）→ {a} 档，权重 0.4")
        if b is not None:
            parts.append(f"近 {len(gm_annual)} 年年报毛利率标准差 {sd:.2f}pp → {b} 档，权重 0.3")
        if c is not None:
            parts.append(f"近 {len(roic_annual)} 年年报 ROIC>{wacc:.0%} 的年份占比 {persist:.0%}"
                         f" → {c} 档，权重 0.3")
        out.append(ProxyField(
            field_id="company.moat", value=moat,
            as_of=period, available_at=avail, source="自动代理：同业毛利率与 ROIC 持续性",
            period_end=period,
            formula="；".join(parts) + f"；合成 {moat} 档（缺失分项按权重重归一化）",
            note="代理偏「高毛利 + 稳定」型护城河；对牌照、网络效应、低成本等类型会低估，"
                 "对高毛利但即将被替代的赛道会高估；资本密集的代工/制造企业 ROIC 天然偏低，"
                 "代理容易把「重资产」误读成「无壁垒」，这类标的的护城河请以人工判断为准",
            confidence="medium",
        ))

    # ---- 管理层记录：分红连续性 + 质押比例
    paid, a2 = None, None
    if dividends:
        years = {}
        for d in dividends:
            rd = d.get("report_date") or ""
            if not rd or d.get("status") != "实施分配" or (d.get("cash_per_10") or 0) <= 0:
                continue
            years[int(rd[:4])] = True
        window = [int(asof[:4]) - i for i in range(1, 6)]
        paid = sum(1 for y in window if years.get(y))
        a2 = _grade(float(paid), [1, 2, 3, 5])  # 0→1, 1→2, 2→3, 3-4→4, 5→5

    pledge_ratio = (pledge or {}).get("ratio")
    b2 = _grade(pledge_ratio, [10.0, 20.0, 30.0, 50.0], reverse=True)
    mgmt = _weighted([(a2, 0.5), (b2, 0.5)])
    if mgmt is not None:
        parts2 = []
        if a2 is not None:
            parts2.append(f"近 5 个完整年度中现金分红 {paid} 年 → {a2} 档，权重 0.5")
        if b2 is not None:
            parts2.append(f"大股东质押 {pledge_ratio:.2f}% → {b2} 档，权重 0.5")
        out.append(ProxyField(
            field_id="company.mgmt_record", value=mgmt,
            as_of=(pledge or {}).get("trade_date") or period,
            available_at=(pledge or {}).get("trade_date") or avail,
            source="自动代理：分红连续性与质押比例",
            formula="；".join(parts2) + f"；合成 {mgmt} 档",
            note="「未覆盖违规、承诺不兑现、减持」—— 这三项是管理层记录里最关键的部分，"
                 "代理只能看到「分钱是否持续、质押是否高」，判断强度明显弱于人工",
            confidence="low",
        ))

    # ---- 质押比例：直接观测值
    if pledge_ratio is not None:
        out.append(ProxyField(
            field_id="company.pledge_pct", value=round(float(pledge_ratio), 2),
            as_of=pledge.get("trade_date", asof), available_at=pledge.get("trade_date", asof),
            source="中国结算·股权质押统计（东方财富转载）", basis="observed", confidence="high",
            formula=(f"大股东质押比例 = {pledge_ratio:.2f}%（中登 {pledge.get('trade_date')} 期，"
                     f"行业 {pledge.get('industry', '—')}）"),
            note="直接观测值；质押比例是「存量/总股本」口径，不反映质押用途与平仓线",
        ))

    return out
