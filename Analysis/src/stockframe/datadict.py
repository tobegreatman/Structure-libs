"""数据字典：字段注册表（代码即文档）+ 公司类型适用性 + 输入结构校验。

字段规范见设计文档 §4.3。注册表用于：
- DQ 完整性统计（registered 中实际提供多少）
- 缺口报告（gaps）
- 必填字段核查（缺失 → 强制人工复核）

COMPANY_PROFILES 是**行业适用性前门**：它决定本框架的两种估值方法对这类公司是否成立，
而不是决定"给多少分"。没有这张表时，银行股会被当成成长股算 FCFF-DCF，
得出 +95% 的安全边际——错不在公式，在于问题本身不适用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class FieldSpec:
    field_id: str
    name: str
    unit: str
    update_days: int          # 典型更新周期（天），用于时效性判断（按「披露节奏」而非「拿到手的日期」）
    qualitative: bool = False # True=锚定量表 1-5 定性档位
    required: bool = False    # True=缺失即强制人工复核
    annual: bool = False      # True=只在年报披露（附注类口径）。手填时数据期取最近一期年报期末，
                              # available_at 取该年报的公告日——不能记成录入当日，否则一年前的数
                              # 会显示成"今天刚拿到"，时效性也被误判为最新


FIELD_SPECS: List[FieldSpec] = [
    # market（日频）
    FieldSpec("market.price_close", "收盘价", "元", 1, required=True),
    FieldSpec("market.pe_ttm", "PE(TTM)", "倍", 1, required=True),
    FieldSpec("market.pb", "PB", "倍", 1),
    FieldSpec("market.pctile_pe_10y", "PE 十年分位", "%", 1),
    FieldSpec("market.pctile_pb_10y", "PB 十年分位", "%", 1),
    FieldSpec("market.turnover_pct", "换手率", "%", 1),
    FieldSpec("market.vol_ratio_20d", "20日量比", "倍", 1),
    FieldSpec("market.dist_ma250", "距250日线", "%", 1),
    FieldSpec("market.drawdown_52w", "距52周高点回撤", "%", 1),
    FieldSpec("market.inst_hold_chg_pct", "机构持仓变化", "pp", 92),
    # macro（月频）
    FieldSpec("macro.m1_yoy", "M1 同比", "%", 31),
    FieldSpec("macro.rate_10y_chg_yoy", "10Y利率同比变化", "bp", 31),
    FieldSpec("macro.pmi", "PMI", "点", 31),
    FieldSpec("macro.policy_stance", "政策取向(对行业)", "1-5档", 31, qualitative=True),
    # industry（季频）
    FieldSpec("industry.industry_growth", "行业增速", "%", 92),
    FieldSpec("industry.price_trend", "行业量价趋势", "1-5档(-2..+2)", 92, qualitative=True),
    FieldSpec("industry.stage", "行业生命周期阶段", "1-5档", 92, qualitative=True),
    FieldSpec("industry.concentration", "格局/公司卡位", "1-5档", 92, qualitative=True),
    FieldSpec("industry.penetration_space", "渗透空间", "1-5档", 92, qualitative=True),
    # company（季/事件频）
    FieldSpec("company.moat", "护城河综合档", "1-5档", 92, qualitative=True),
    FieldSpec("company.mgmt_record", "管理层记录", "1-5档", 92, qualitative=True),
    FieldSpec("company.pledge_pct", "大股东质押比例", "%", 31),
    FieldSpec("company.logic_strength", "投资逻辑强度(预期差)", "1-5档", 92, qualitative=True),
    FieldSpec("company.logic_essence", "一句话本质（逻辑内核）", "文本", 92),
    # 年报附注口径：一年只披露一次（「前五名客户销售额占比」），按 365 天衡量时效；
    # 用季频的 92 天去卡它，会出现"年报公布后三个月就判超期"的错误提示。
    FieldSpec("company.customer_concentration", "最大客户收入占比", "%", 365, annual=True),
    # financial（季频）
    FieldSpec("financial.roe_ttm", "ROE(TTM)", "%", 92, required=True),
    FieldSpec("financial.roic", "ROIC", "%", 92),
    FieldSpec("financial.wacc_assumed", "WACC(假设)", "%", 92),
    FieldSpec("financial.ocf_np_ratio", "经营现金流/净利润", "倍", 92),
    FieldSpec("financial.wc_growth_gap", "应收+存货增速-营收增速", "pp", 92),
    FieldSpec("financial.rev_growth", "营收增速(同比)", "%", 92, required=True),
    FieldSpec("financial.np_growth", "净利增速(同比)", "%", 92),
    FieldSpec("financial.gm_trend", "毛利率趋势(同比变化)", "pp", 92),
    FieldSpec("financial.int_debt_ratio", "有息负债率", "%", 92),
    FieldSpec("financial.goodwill_pct", "商誉/净资产", "%", 92),
    # valuation
    # EPS(TTM) 的分子来自定期报告，一年只更新四次：它的"新鲜度"按报告期节奏衡量，
    # 不能按日频（否则季报公布两个月就被判"超期 2 倍"，而其实是最新的）。
    FieldSpec("valuation.eps_ttm", "EPS(TTM)", "元", 92, required=True),
    FieldSpec("valuation.peer_pe_median", "同业 PE 中位数", "倍", 92),
    FieldSpec("valuation.pe_hist_p25", "PE 十年 P25", "倍", 1),
    FieldSpec("valuation.pe_hist_p75", "PE 十年 P75", "倍", 1),
]

FIELD_REGISTRY: Dict[str, FieldSpec] = {s.field_id: s for s in FIELD_SPECS}

REQUIRED_FIELDS: Tuple[str, ...] = tuple(s.field_id for s in FIELD_SPECS if s.required)


# ---------------------------------------------------------------- 公司类型 → 方法适用性

@dataclass(frozen=True)
class CompanyProfile:
    code: str
    label: str
    dcf_applicable: bool
    relative_metrics: Tuple[str, ...]   # 该类公司可用的相对估值口径（第一个为默认）
    reason: str                          # 为什么适用/不适用（原样进报告与说明）


# 顺序即展示顺序。dcf_applicable=False 的类型，报告不出 DCF 区间、不出安全边际、
# 不出情景价位——这是"拒答"，不是"降级"：给一个数再标低置信度，读者只会记住数。
COMPANY_PROFILES: Dict[str, CompanyProfile] = {
    p.code: p for p in (
        CompanyProfile(
            "high_growth", "高成长", True, ("PE", "PEG", "EV_EBITDA"),
            "营收/利润率路径是主要价值来源，FCFF-DCF 与 PE 分位均可用。"),
        CompanyProfile(
            "steady_growth", "稳定成长", True, ("PE", "PEG", "EV_EBITDA", "PB"),
            "营收/利润率路径是主要价值来源，FCFF-DCF 与 PE 分位均可用。"),
        CompanyProfile(
            "value", "价值/低估值", True, ("PE", "PB", "EV_EBITDA"),
            "现金流稳定，DCF 与 PE/PB 分位均可用。"),
        CompanyProfile(
            "platform", "平台/轻资产", True, ("PS", "EV_EBITDA", "PE"),
            "资本开支轻，FCFF 近似自由现金流；盈利波动大时优先 PS/EV-Sales 分位。"),
        CompanyProfile(
            "cyclical", "周期", True, ("PB", "EV_EBITDA", "PE"),
            "DCF 可用，但 EBIT 率路径须落在周期中枢而非当期极值；PE 在盈利高点最低、"
            "低点最高，分位解读需结合产能周期。"),
        CompanyProfile(
            "resource", "资源", True, ("PB", "EV_EBITDA", "PE"),
            "DCF 对资源价格假设极敏感，须与储量/成本曲线交叉验证；PE 分位单独使用无意义。"),
        CompanyProfile(
            "financial", "金融（通用）", False, ("PB",),
            "金融企业的价值来自资产负债表（息差、准备金、资本充足率），不是营收×EBIT率路径；"
            "本框架的 FCFF-DCF 的净债务/资本开支口径对银行、保险不成立，故不出 DCF 区间与安全边际。"),
        CompanyProfile(
            "bank", "银行", False, ("PB",),
            "银行价值由净息差、信用成本、资本充足率决定，FCFF-DCF 的营收×EBIT率路径与净债务口径"
            "均不适用（存款是负债也是经营原料）。本框架不出银行估值，需行业模板（PB-ROE / RIM）。"),
        CompanyProfile(
            "insurance", "保险", False, ("PB",),
            "保险价值依赖内含价值（EV）与新业务价值，与营收×EBIT率无关；"
            "本框架不出保险估值，需行业模板（P/EV）。"),
        CompanyProfile(
            "reit", "REIT/持有型地产", False, ("PB",),
            "持有型地产价值来自物业净租金（NOI）与资本化率，与本框架的营收×EBIT率路径不同；"
            "需行业模板（P/FFO、NAV）。"),
        CompanyProfile(
            "loss_making", "亏损", False, ("PS", "PB"),
            "亏损状态下 EPS≤0 使 PE 无意义、EBIT 率为负使 DCF 显性期现金流为负，"
            "区间会给出负股价或荒谬值；本框架不出估值，需按 PS/EV-Sales 或扭亏路径单独建模。"),
        CompanyProfile(
            "pre_revenue", "未盈利/次新", False, ("PS",),
            "尚无稳定收入基数，营收增长路径与利润率假设无锚，DCF 只是把假设放大；"
            "本框架不出估值。"),
    )
}

COMPANY_TYPE_CODES: Tuple[str, ...] = tuple(COMPANY_PROFILES)


def company_profile(code: str) -> CompanyProfile:
    """按公司类型取方法适用性。未知类型直接报错（不静默套用默认模板）。"""
    profile = COMPANY_PROFILES.get(code)
    if profile is None:
        raise InputError(
            f"meta.company_type={code!r} 未登记。可选：{'、'.join(COMPANY_TYPE_CODES)}。"
            "公司类型决定 DCF/相对估值是否适用，不能让未分类的公司走默认模板。"
        )
    return profile

# 结构化输入块（非平铺字段）所需键
STRUCTURAL_KEYS = ("meta", "data", "assumptions", "risks", "catalysts")


class InputError(ValueError):
    """输入结构/取值不合法。

    继承 ValueError 是有意的：引擎历史上对假设不合理（wacc≤g、概率和≠1、股权价值为负）
    抛的都是 ValueError，调用方可能已经 `except ValueError` 兜底。若输入契约另立一支
    Exception，同一类"用户填错"会分成两种异常，兜底代码只接住一半。
    """


def validate_structure(inp: Dict[str, Any]) -> None:
    if not isinstance(inp, dict):
        raise InputError("输入必须是 JSON 对象")
    missing = [k for k in STRUCTURAL_KEYS if k not in inp]
    if missing:
        raise InputError(f"输入缺少顶层键: {missing}")
    meta = inp["meta"]
    for key in ("stock", "analysis_date", "company_type", "relative_valuation_metric"):
        if key not in meta:
            raise InputError(f"meta 缺少 {key}")
    profile = company_profile(meta["company_type"])
    metric = meta["relative_valuation_metric"]
    if metric not in profile.relative_metrics:
        raise InputError(
            f"meta.relative_valuation_metric={metric!r} 不适用于 company_type={profile.code}"
            f"（{profile.label}）。该类公司可用的口径：{'、'.join(profile.relative_metrics)}。"
            f"理由：{profile.reason}"
        )
    for key in ("stock",):
        for sub in ("name", "code", "exchange", "currency"):
            if sub not in meta[key]:
                raise InputError(f"meta.stock 缺少 {sub}")
    if not isinstance(inp["risks"], list) or len(inp["risks"]) < 3:
        raise InputError("risks 必须是列表且至少 3 条（风险矩阵 schema 要求）")
    if not isinstance(inp["catalysts"], list) or len(inp["catalysts"]) < 2:
        raise InputError("catalysts 必须是列表且至少 2 条")


def field_spec(field_id: str) -> FieldSpec:
    if field_id not in FIELD_REGISTRY:
        raise InputError(f"未注册字段: {field_id}（若为新字段请先登记 FIELD_SPECS）")
    return FIELD_REGISTRY[field_id]
