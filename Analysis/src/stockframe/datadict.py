"""数据字典：字段注册表（代码即文档）+ 输入结构校验。

字段规范见设计文档 §4.3。注册表用于：
- DQ 完整性统计（registered 中实际提供多少）
- 缺口报告（gaps）
- 必填字段核查（缺失 → 强制人工复核）
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

# 结构化输入块（非平铺字段）所需键
STRUCTURAL_KEYS = ("meta", "data", "assumptions", "risks", "catalysts")


class InputError(Exception):
    """输入结构不合法。"""


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
