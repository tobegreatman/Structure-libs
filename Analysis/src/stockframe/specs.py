"""8 个分析维度的声明式定义（specs）。

本文件是评分体系的唯一事实来源（single source of truth）：
- 评分说明文档的量规表由 render_scoring_tables() 生成，保证文档与代码一致；
- 任何阈值修改必须同步给出 rationale 依据（反黑箱 P5：少而精、有锚定）。

维度内部结构在中线/短线两周期下保持一致（保证可比与可回放），
双周期差异体现在维度权重层（设计文档 §5.2）。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .rubric import Band, DimensionSpec, FactorSpec, IndicatorSpec

# ---------------------------------------------------------------- 通用构造

def _b(label: str, lo, hi, score: float) -> Band:
    return Band(label=label, lo=lo, hi=hi, score=score)


def rating_bands() -> Tuple[Band, ...]:
    """1-5 定性档通用区间带（配合 anchors 使用）。"""
    return (
        _b("差", None, 2, 0.0),
        _b("弱", 2, 3, 2.5),
        _b("中", 3, 4, 5.0),
        _b("良", 4, 5, 7.5),
        _b("优", 5, None, 10.0),
    )


# 锚定量表（1-5 档位行为描述，进报告原文）
ANCHORS: Dict[str, Dict[int, str]] = {
    "policy_stance": {
        1: "明显抑制：针对该行业的收紧/限制性政策正在落地",
        2: "偏抑制：政策取向不利或有收紧信号",
        3: "中性：无明确方向性政策",
        4: "偏支持：有明确支持性政策或产业规划在途",
        5: "强支持：专项政策/补贴/国产化等强驱动正在兑现",
    },
    "price_trend": {
        1: "行业价格明显下行（-2）",
        2: "价格偏弱（-1）",
        3: "价格平稳（0）",
        4: "价格偏强（+1）",
        5: "价格明显上行/供不应求（+2）",
    },
    "stage": {
        1: "衰退期：需求趋势性收缩",
        2: "成熟后期：增长停滞、格局固化",
        3: "成熟期：低速稳态增长",
        4: "成长中期：双位数增长、格局渐清晰",
        5: "成长早中期：渗透率快速提升、空间大",
    },
    "concentration": {
        1: "分散且公司无卡位：份额低、同质竞争",
        2: "分散、公司为追赶者",
        3: "中等集中、公司处于第二梯队",
        4: "较高集中、公司为细分龙头",
        5: "高集中或强卡位：公司为行业龙头/核心供应商",
    },
    "penetration_space": {
        1: "渗透率已高（>70%），增量空间有限",
        2: "渗透率 50-70%，空间收窄",
        3: "渗透率 30-50%，仍有翻倍以上空间",
        4: "渗透率 10-30%，快速渗透期",
        5: "渗透率 <10%，从 0 到 1 的导入期",
    },
    "moat": {
        1: "无壁垒：产品同质化、客户随时可切换",
        2: "弱壁垒：有一定成本或渠道优势但可被复制",
        3: "中等壁垒：认证/资质/工艺积累构成 2-3 年跟随期",
        4: "较强壁垒：技术专利+客户认证双壁垒，切换成本高",
        5: "强壁垒：网络效应/垄断资源/不可复制的工艺组合",
    },
    "mgmt_record": {
        1: "劣迹：重大违规/频繁减持/承诺不兑现史",
        2: "存疑：指引频繁偏离、信披质量差",
        3: "中性：无明显正面或负面记录",
        4: "良好：指引兑现率高、治理规范",
        5: "优秀：长期股东回报记录优异、战略执行力被多次验证",
    },
    "logic_strength": {
        1: "泛逻辑：'受益于行业景气'类，无传导路径",
        2: "弱逻辑：方向对但传导路径不清晰",
        3: "可验证逻辑：传导路径明确但预期差有限",
        4: "强逻辑：传导路径明确且存在被低估的预期差",
        5: "极强逻辑：可证伪的具体逻辑+市场显著错判",
    },
}

# ---------------------------------------------------------------- 维度定义

DIMENSIONS: List[DimensionSpec] = [
    # ---------------- 宏观 ----------------
    DimensionSpec(
        id="macro", name="宏观", weight_mid=0.08, weight_short=0.05, update_days=31,
        factors=(
            FactorSpec(id="liquidity_rate", name="流动性与利率", weight=0.5, indicators=(
                IndicatorSpec(
                    id="macro.m1_yoy_trend", name="M1 同比趋势", unit="%", field="macro.m1_yoy",
                    weight=0.6,
                    bands=(_b("差", None, 0, 0.0), _b("弱", 0, 2, 2.5), _b("中", 2, 5, 5.0),
                           _b("良", 5, 8, 7.5), _b("优", 8, None, 10.0)),
                    rationale="M1 反映资金活化程度，经验上 >8% 对应宽松周期、<0% 收缩。",
                ),
                IndicatorSpec(
                    id="macro.rate_10y_chg", name="10Y 利率同比变化", unit="bp", field="macro.rate_10y_chg_yoy",
                    weight=0.4,
                    bands=(_b("优", None, -40, 10.0), _b("良", -40, -10, 7.5), _b("中", -10, 10, 5.0),
                           _b("弱", 10, 40, 2.5), _b("差", 40, None, 0.0)),
                    rationale=("利率下行降低贴现率与融资成本，对成长股估值中枢为顺风（反向指标）。"
                               "口径：中债国债收益率曲线 10 年期（日频），同比 = 最新值与 365 天前最近交易日之差，"
                               "单位 bp；曲线为日终估值，按次日可用。"),
                ),
            )),
            FactorSpec(id="growth_policy", name="增长与政策", weight=0.5, indicators=(
                IndicatorSpec(
                    id="macro.pmi_pos", name="PMI 荣枯线位置", unit="点", field="macro.pmi",
                    weight=0.5,
                    bands=(_b("差", None, 48, 0.0), _b("弱", 48, 49.5, 2.5), _b("中", 49.5, 50.5, 5.0),
                           _b("良", 50.5, 51.5, 7.5), _b("优", 51.5, None, 10.0)),
                    rationale="PMI 50 为荣枯线；51.5+ 对应扩张加速。",
                ),
                IndicatorSpec(
                    id="macro.policy_stance_r", name="政策取向（对本行业）", unit="1-5档",
                    field="macro.policy_stance", weight=0.5, bands=rating_bands(),
                    anchors=ANCHORS["policy_stance"],
                    rationale="政策取向需经'政策→行业→公司'传导链验证，非泛化受益。",
                ),
            )),
        ),
    ),
    # ---------------- 行业 ----------------
    DimensionSpec(
        id="industry", name="行业", weight_mid=0.14, weight_short=0.10, update_days=92,
        factors=(
            FactorSpec(id="prosperity", name="景气度", weight=0.4, indicators=(
                IndicatorSpec(
                    id="industry.growth", name="行业增速", unit="%", field="industry.industry_growth",
                    weight=0.6,
                    bands=(_b("差", None, 2, 0.0), _b("弱", 2, 5, 2.5), _b("中", 5, 8, 5.0),
                           _b("良", 8, 15, 7.5), _b("优", 15, None, 10.0)),
                    rationale="行业增速显著高于名义 GDP 增速（约 5-6%）才算高景气。",
                ),
                IndicatorSpec(
                    id="industry.price_trend_r", name="行业量价趋势", unit="-2..+2",
                    field="industry.price_trend", weight=0.4, bands=rating_bands(),
                    anchors=ANCHORS["price_trend"],
                    rationale="量价齐升为最优景气组合；价格下行侵蚀利润率。",
                ),
            )),
            FactorSpec(id="structure", name="竞争格局", weight=0.35, indicators=(
                IndicatorSpec(
                    id="industry.concentration_r", name="格局与公司卡位", unit="1-5档",
                    field="industry.concentration", weight=1.0, bands=rating_bands(),
                    anchors=ANCHORS["concentration"],
                    rationale="格局决定盈利可持续性；公司卡位决定份额弹性。",
                ),
            )),
            FactorSpec(id="space", name="成长空间", weight=0.25, indicators=(
                IndicatorSpec(
                    id="industry.stage_r", name="行业生命周期阶段", unit="1-5档",
                    field="industry.stage", weight=0.4, bands=rating_bands(),
                    anchors=ANCHORS["stage"],
                    rationale="成长早中期估值与盈利弹性最优；衰退期一票降档。",
                ),
                IndicatorSpec(
                    id="industry.penetration_r", name="渗透空间", unit="1-5档",
                    field="industry.penetration_space", weight=0.6, bands=rating_bands(),
                    anchors=ANCHORS["penetration_space"],
                    rationale="渗透率 10-30% 为戴维斯双击高发区间。",
                ),
            )),
        ),
    ),
    # ---------------- 公司 ----------------
    DimensionSpec(
        id="company", name="公司", weight_mid=0.22, weight_short=0.05, update_days=92,
        factors=(
            FactorSpec(id="moat", name="护城河", weight=0.5, indicators=(
                IndicatorSpec(
                    id="company.moat_r", name="护城河综合档", unit="1-5档",
                    field="company.moat", weight=1.0, bands=rating_bands(),
                    anchors=ANCHORS["moat"],
                    rationale="护城河类型与强度决定超额 ROE 的持续性。",
                ),
            )),
            FactorSpec(id="governance", name="治理与管理", weight=0.3, indicators=(
                IndicatorSpec(
                    id="company.mgmt_r", name="管理层记录", unit="1-5档",
                    field="company.mgmt_record", weight=0.6, bands=rating_bands(),
                    anchors=ANCHORS["mgmt_record"],
                    rationale="指引兑现率与治理记录是中线持有的前提。",
                ),
                IndicatorSpec(
                    id="company.pledge", name="大股东质押比例", unit="%", field="company.pledge_pct",
                    weight=0.4,
                    bands=(_b("优", None, 10, 10.0), _b("良", 10, 20, 7.5), _b("中", 20, 30, 5.0),
                           _b("弱", 30, 50, 2.5), _b("差", 50, None, 0.0)),
                    rationale="质押 >30% 需关注、>50% 为高风险（监管与市场惯例阈值）。",
                ),
            )),
            FactorSpec(id="logic", name="投资逻辑强度", weight=0.2, indicators=(
                IndicatorSpec(
                    id="company.logic_r", name="核心逻辑与预期差", unit="1-5档",
                    field="company.logic_strength", weight=1.0, bands=rating_bands(),
                    anchors=ANCHORS["logic_strength"],
                    rationale="预期差是超额收益来源；逻辑必须可证伪（'受益于景气'类泛逻辑不接受）。",
                ),
            )),
        ),
    ),
    # ---------------- 财务 ----------------
    DimensionSpec(
        id="financial", name="财务", weight_mid=0.20, weight_short=0.05, update_days=92,
        factors=(
            FactorSpec(id="earnings_quality", name="盈利质量", weight=0.4, indicators=(
                IndicatorSpec(
                    id="fin.roe", name="ROE(TTM)", unit="%", field="financial.roe_ttm", weight=0.4,
                    bands=(_b("差", None, 4, 0.0), _b("弱", 4, 8, 2.5), _b("中", 8, 12, 5.0),
                           _b("良", 12, 18, 7.5), _b("优", 18, None, 10.0)),
                    rationale="消费/制造龙头长期 ROE>20% 为优秀（质量增长方法论），18% 为放宽门槛。",
                ),
                IndicatorSpec(
                    id="fin.ocf_np", name="经营现金流/净利润", unit="倍", field="financial.ocf_np_ratio", weight=0.35,
                    bands=(_b("差", None, 0.5, 0.0), _b("弱", 0.5, 0.7, 2.5), _b("中", 0.7, 0.9, 5.0),
                           _b("良", 0.9, 1.1, 7.5), _b("优", 1.1, None, 10.0)),
                    rationale="净利润现金含量 ≥1 为合格线；长期 <0.7 说明利润含水量高。",
                ),
                IndicatorSpec(
                    id="fin.wc_gap", name="应收+存货增速 − 营收增速", unit="pp", field="financial.wc_growth_gap",
                    weight=0.25,
                    bands=(_b("优", None, 0, 10.0), _b("良", 0, 5, 7.5), _b("中", 5, 10, 5.0),
                           _b("弱", 10, 20, 2.5), _b("差", 20, None, 0.0)),
                    rationale="营运资本增速持续快于营收 = 渠道压货/回款恶化信号（增长质检）。",
                ),
            )),
            FactorSpec(id="growth_quality", name="增长质量", weight=0.35, indicators=(
                IndicatorSpec(
                    id="fin.rev_g", name="营收增速", unit="%", field="financial.rev_growth", weight=0.35,
                    bands=(_b("差", None, 0, 0.0), _b("弱", 0, 8, 2.5), _b("中", 8, 15, 5.0),
                           _b("良", 15, 25, 7.5), _b("优", 25, None, 10.0)),
                    rationale="与名义 GDP 及行业增速对比定位增长台阶。",
                ),
                IndicatorSpec(
                    id="fin.np_g", name="净利增速", unit="%", field="financial.np_growth", weight=0.35,
                    bands=(_b("差", None, 0, 0.0), _b("弱", 0, 8, 2.5), _b("中", 8, 15, 5.0),
                           _b("良", 15, 25, 7.5), _b("优", 25, None, 10.0)),
                    rationale="收入与利润增速需同向；利润长期快于收入需查非经常项。",
                ),
                IndicatorSpec(
                    id="fin.gm_trend", name="毛利率趋势(同比变化)", unit="pp", field="financial.gm_trend", weight=0.3,
                    bands=(_b("差", None, -2, 0.0), _b("弱", -2, -0.5, 2.5), _b("中", -0.5, 0.5, 5.0),
                           _b("良", 0.5, 2, 7.5), _b("优", 2, None, 10.0)),
                    rationale="毛利率方向是量价与竞争格局的先行指标。",
                ),
            )),
            FactorSpec(id="capital_return", name="资本回报", weight=0.25, indicators=(
                IndicatorSpec(
                    id="fin.roic_wacc", name="ROIC − WACC 差", unit="pp", field="financial.roic_wacc_spread",
                    weight=0.4, derive="financial.roic_wacc_spread",
                    bands=(_b("差", None, -2, 0.0), _b("弱", -2, 0, 2.5), _b("中", 0, 2, 5.0),
                           _b("良", 2, 5, 7.5), _b("优", 5, None, 10.0)),
                    rationale="ROIC 超 WACC 5pp 以上为优秀（质量增长方法论：真实经营优势的标尺）。",
                ),
                IndicatorSpec(
                    id="fin.int_debt", name="有息负债率", unit="%", field="financial.int_debt_ratio", weight=0.3,
                    bands=(_b("优", None, 20, 10.0), _b("良", 20, 35, 7.5), _b("中", 35, 50, 5.0),
                           _b("弱", 50, 65, 2.5), _b("差", 65, None, 0.0)),
                    rationale="制造业 >50% 财务弹性受限，>65% 高杠杆。",
                ),
                IndicatorSpec(
                    id="fin.goodwill", name="商誉/净资产", unit="%", field="financial.goodwill_pct", weight=0.3,
                    bands=(_b("优", None, 5, 10.0), _b("良", 5, 15, 7.5), _b("中", 15, 25, 5.0),
                           _b("弱", 25, 40, 2.5), _b("差", 40, None, 0.0)),
                    rationale="商誉占比 >25% 减值风险显著（并购驱动增长需打折）。",
                ),
            )),
        ),
    ),
    # ---------------- 估值 ----------------
    DimensionSpec(
        id="valuation", name="估值", weight_mid=0.16, weight_short=0.05, update_days=31,
        factors=(
            FactorSpec(id="abs_margin", name="绝对安全边际", weight=0.45, indicators=(
                IndicatorSpec(
                    id="val.dcf_margin", name="DCF 安全边际(基准情景)", unit="%", field="valuation.dcf_margin_of_safety",
                    weight=1.0, derive="valuation.dcf_margin_of_safety",
                    bands=(_b("差", None, -15, 0.0), _b("弱", -15, 0, 2.5), _b("中", 0, 15, 5.0),
                           _b("良", 15, 30, 7.5), _b("优", 30, None, 10.0)),
                    rationale="安全边际 ≥15% 才足以补偿假设不确定性（DCF 区间中枢 vs 现价）。",
                ),
            )),
            FactorSpec(id="rel_position", name="相对位置", weight=0.35, indicators=(
                IndicatorSpec(
                    id="val.pe_pctile", name="PE 十年分位", unit="%", field="market.pctile_pe_10y", weight=0.6,
                    bands=(_b("优", None, 20, 10.0), _b("良", 20, 40, 7.5), _b("中", 40, 60, 5.0),
                           _b("弱", 60, 80, 2.5), _b("差", 80, None, 0.0)),
                    rationale="历史分位 <30% 偏低、30-70% 中性、>70% 偏高（估值方法论阈值）。",
                ),
                IndicatorSpec(
                    id="val.pb_pctile", name="PB 十年分位", unit="%", field="market.pctile_pb_10y", weight=0.4,
                    bands=(_b("优", None, 20, 10.0), _b("良", 20, 40, 7.5), _b("中", 40, 60, 5.0),
                           _b("弱", 60, 80, 2.5), _b("差", 80, None, 0.0)),
                    rationale="PB 分位为 PE 的交叉验证，周期股权重应上调（可配置）。",
                ),
            )),
            FactorSpec(id="expectation_match", name="预期匹配", weight=0.2, indicators=(
                IndicatorSpec(
                    id="val.peg", name="PEG", unit="倍", field="valuation.peg", weight=1.0,
                    derive="valuation.peg",
                    bands=(_b("优", None, 0.8, 10.0), _b("良", 0.8, 1.2, 7.5), _b("中", 1.2, 1.8, 5.0),
                           _b("弱", 1.8, 2.5, 2.5), _b("差", 2.5, None, 0.0)),
                    rationale="PEG<1 为增长匹配良好；增长为负时 PEG 无意义（按缺失处理）。",
                ),
            )),
        ),
    ),
    # ---------------- 催化剂 ----------------
    DimensionSpec(
        id="catalyst", name="催化剂", weight_mid=0.06, weight_short=0.30, update_days=7,
        factors=(
            FactorSpec(id="mid_density", name="中线催化密度(6-12月)", weight=0.5, indicators=(
                IndicatorSpec(
                    id="cat.density", name="催化密度加权分", unit="0-10", field="catalyst.density_score",
                    weight=1.0, derive="catalyst.density_score", computed="density",
                    bands=(_b("差", None, 2, 0.0), _b("弱", 2, 4, 2.5), _b("中", 4, 6, 5.0),
                           _b("良", 6, 8, 7.5), _b("优", 8, None, 10.0)),
                    rationale="公式 = min(10, Σ 事件强度权重)，强=4/中=2.5/弱=1/噪音=0，只计 6-12 月窗口。",
                ),
            )),
            FactorSpec(id="short_strength", name="短线催化强度(30日内)", weight=0.5, indicators=(
                IndicatorSpec(
                    id="cat.next_event", name="下一事件强度分", unit="0-10", field="catalyst.next_event_score",
                    weight=1.0, derive="catalyst.next_event_score", computed="next_event",
                    bands=(_b("差", None, 1, 0.0), _b("弱", 1, 2.5, 2.5), _b("中", 2.5, 5, 5.0),
                           _b("良", 5, 7.5, 7.5), _b("优", 7.5, None, 10.0)),
                    rationale="公式：30 日内最近事件 强=10/中=7.5/弱=2.5/无=0；利好已定价 ×0.4（兑现风险）。",
                ),
            )),
        ),
    ),
    # ---------------- 交易面 ----------------
    DimensionSpec(
        id="trading", name="交易面", weight_mid=0.06, weight_short=0.30, update_days=1,
        factors=(
            FactorSpec(id="trend_position", name="趋势与位置", weight=0.4, indicators=(
                IndicatorSpec(
                    id="trd.dist_ma250", name="距 250 日均线", unit="%", field="market.dist_ma250", weight=0.5,
                    bands=(_b("差", None, -25, 0.0), _b("弱", -25, -10, 2.5), _b("中(回踩)", -10, 0, 5.0),
                           _b("优", 0, 15, 10.0), _b("良", 15, 30, 7.5), _b("中(过热)", 30, None, 5.0)),
                    rationale="0-15% 为健康趋势；>30% 过热；<-25% 深度破位（非单调：两端均扣分）。",
                ),
                IndicatorSpec(
                    id="trd.drawdown_52w", name="距 52 周高点回撤", unit="%", field="market.drawdown_52w", weight=0.5,
                    bands=(_b("优", None, 15, 10.0), _b("良", 15, 30, 7.5), _b("中", 30, 45, 5.0),
                           _b("弱", 45, 60, 2.5), _b("差", 60, None, 0.0)),
                    rationale="动量视角：接近 52 周高点为强势特征。",
                ),
            )),
            FactorSpec(id="volume_chips", name="量价与筹码", weight=0.35, indicators=(
                IndicatorSpec(
                    id="trd.vol_ratio", name="20 日量比", unit="倍", field="market.vol_ratio_20d", weight=0.5,
                    bands=(_b("差", None, 0.6, 0.0), _b("中", 0.6, 0.8, 5.0), _b("良", 0.8, 1.0, 7.5),
                           _b("优", 1.0, 1.5, 10.0), _b("良", 1.5, 2.0, 7.5), _b("弱", 2.0, 3.0, 2.5),
                           _b("差", 3.0, None, 0.0)),
                    rationale="温和放量(1.0-1.5)最优；>3 异常放量需查归因（非单调）。",
                ),
                IndicatorSpec(
                    id="trd.turnover", name="换手率", unit="%", field="market.turnover_pct", weight=0.5,
                    bands=(_b("差", None, 0.2, 0.0), _b("中", 0.2, 0.5, 5.0), _b("良", 0.5, 1.0, 7.5),
                           _b("优", 1.0, 5.0, 10.0), _b("良", 5.0, 10.0, 7.5), _b("弱", 10.0, 15.0, 2.5),
                           _b("差", 15.0, None, 0.0)),
                    rationale="换手过低流动性差、过高博弈过热（非单调甜点区间）。",
                ),
            )),
            FactorSpec(id="crowding", name="拥挤度", weight=0.25, indicators=(
                IndicatorSpec(
                    id="trd.inst_chg", name="机构持仓变化", unit="pp", field="market.inst_hold_chg_pct", weight=1.0,
                    bands=(_b("差", None, -5, 0.0), _b("弱", -5, -2, 2.5), _b("中", -2, 2, 5.0),
                           _b("良", 2, 5, 7.5), _b("优", 5, None, 10.0)),
                    rationale="机构增持为中线资金面确认；大幅减持需查归因。",
                ),
            )),
        ),
    ),
    # ---------------- 风险 ----------------
    DimensionSpec(
        id="risk", name="风险(可控度)", weight_mid=0.08, weight_short=0.10, update_days=31,
        factors=(
            FactorSpec(id="controllability", name="风险可控度", weight=1.0, indicators=(
                IndicatorSpec(
                    id="risk.matrix_score", name="风险矩阵加权分", unit="0-10",
                    field="risk.matrix_score", weight=0.45, derive="risk.matrix_score", computed="risk_matrix",
                    bands=(_b("差", None, 2, 0.0), _b("弱", 2, 4, 2.5), _b("中", 4, 6, 5.0),
                           _b("良", 6, 8, 7.5), _b("优", 8, None, 10.0)),
                    rationale="公式：10×(1−Σ max(0, P×I−8, 17 上限)/20)，P×I>8 的风险逐条扣减。",
                ),
                IndicatorSpec(
                    id="risk.red_flags", name="财务红旗数量", unit="个", field="risk.red_flag_count",
                    weight=0.25, derive="risk.red_flag_count",
                    bands=(_b("优", None, 1, 10.0), _b("中", 1, 2, 6.0),
                           _b("差", 2, 3, 2.0), _b("极差", 3, None, 0.0)),
                    rationale="重大红旗（非标审计/资金占用/立案）边际影响极陡：1 条已显著降分，2 条以上由决策层直接进回避候选。",
                ),
                IndicatorSpec(
                    id="risk.customer_conc", name="最大客户收入占比", unit="%", field="company.customer_concentration",
                    weight=0.3,
                    bands=(_b("优", None, 15, 10.0), _b("良", 15, 30, 7.5), _b("中", 30, 50, 5.0),
                           _b("弱", 50, 70, 2.5), _b("差", 70, None, 0.0)),
                    rationale="单一客户 >30% 依赖风险、>50% 重大集中度风险。",
                ),
            )),
        ),
    ),
]

WEIGHTS_MID = {d.id: d.weight_mid for d in DIMENSIONS}
WEIGHTS_SHORT = {d.id: d.weight_short for d in DIMENSIONS}


def anchors_by_field() -> Dict[str, Dict[int, str]]:
    """定性字段 → 1-5 档位的锚定描述。

    锚定量表本来只用于评分证据（rubric 里档位 → 档内分 + 描述），
    但界面选档时也必须有它：看不到"3 档和 4 档差在哪"，下拉就退化成盲猜，
    而档位直接换算成分数（(档-1)×2.5），猜错就是整个因子偏 2.5 分。
    """
    out: Dict[str, Dict[int, str]] = {}
    for dim in DIMENSIONS:
        for fac in dim.factors:
            for ind in fac.indicators:
                if ind.anchors:
                    out[ind.field] = dict(ind.anchors)
    return out


# ---------------------------------------------------------------- 文档生成

def render_scoring_tables() -> str:
    """从 specs 生成评分说明的量规表（保证文档与代码一致）。"""
    lines: List[str] = []
    lines.append("## 附：由 specs.py 自动生成的量规总表（与代码严格一致）\n")
    lines.append("生成命令：`python -m stockframe.cli --dump-scoring`\n")
    for dim in DIMENSIONS:
        lines.append(f"### 维度：{dim.name}（{dim.id}）\n")
        lines.append(
            f"- 中线权重 {dim.weight_mid}，短线权重 {dim.weight_short}，典型更新周期 {dim.update_days} 天\n"
        )
        for fac in dim.factors:
            lines.append(f"#### 因子：{fac.name}（{fac.id}，维度内权重 {fac.weight}）\n")
            header = "| 指标 | 权重 | 量规（区间→得分） | 依据 |"
            lines.append(header)
            lines.append("|---|---|---|---|")
            for ind in fac.indicators:
                if ind.anchors is not None:
                    bands_txt = "1-5 锚定量表：1档→0 / 2档→2.5 / 3档→5 / 4档→7.5 / 5档→10"
                else:
                    segs = []
                    for bnd in ind.bands:
                        lo = "-inf" if bnd.lo is None else f"{bnd.lo:g}"
                        hi = "+inf" if bnd.hi is None else f"{bnd.hi:g}"
                        segs.append(f"[{lo},{hi})→{bnd.label}:{bnd.score:g}")
                    bands_txt = "；".join(segs)
                lines.append(f"| {ind.name}({ind.id}) | {ind.weight} | {bands_txt} | {ind.rationale} |")
            lines.append("")
        if dim.id == "company":
            lines.append("**定性锚定量表（节选，完整见正文）**\n")
    return "\n".join(lines)
