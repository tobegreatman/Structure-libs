"""生成 docs/02_scoring_guide.md：手写解释 + 由 specs.py 自动生成的量规总表。

用法：PYTHONPATH=src python tools/build_scoring_doc.py
目的：评分说明中的阈值表与代码同源，避免"文档一套、代码一套"的黑箱漂移。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from stockframe.specs import DIMENSIONS, render_scoring_tables  # noqa: E402

NARRATIVE = """# 评分说明（Scoring Guide）

> 本文件由 `tools/build_scoring_doc.py` 生成：正文为手工撰写，**附录 A 的量规表直接由 `src/stockframe/specs.py` 渲染**。
> 修改阈值只能改 specs.py 并重跑本脚本——不允许只改文档。生成命令：`PYTHONPATH=src python tools/build_scoring_doc.py`

## 1. 记分口径

| 项 | 规则 |
|---|---|
| 分值域 | 全部指标映射到 **0–10**；5 档锚定取 0 / 2.5 / 5 / 7.5 / 10 |
| 为什么是锚定而非线性 | 阈值来自方法论锚点（ROE 18%、PEG 1.0、PE 分位 30/70 等），档位差代表"结论级差异"而非等距数值差 |
| 定性指标 | 输入 1–5 档，转换 `(rating − 1) × 2.5`；档位行为描述（anchors）原文进报告，保证"同一档位同一含义" |
| 计算型指标 | 催化密度、下一事件强度、风险矩阵分本身即 0–10，直接取值；band 标签（优/良/中/弱/差）仅用于展示 |
| 方向性 | 每个指标都是"越高越好"的统一方向：越低越好的指标（如质押比例、负债率）在量规中已反向定义档位 |
| 非单调指标 | 距 250 日线、量比、换手率为"甜点区间"型，两端均扣分，量规中显式列出多段 |

## 2. 计算链路（三层加权）

```
指标得分 ──(因子内指标权重)──> 因子分 ──(维度内因子权重)──> 维度分 ──(周期权重)──> 综合分
```

- **因子分** = Σ(可用指标得分 × 指标权重) / Σ(可用指标权重)
- **维度分** = Σ(可用因子分 × 因子权重) / Σ(可用因子权重)
- **综合分** = Σ(维度分 × 周期权重) / Σ(周期权重)
- 中线（主）/ 短线（参考）共用同一套维度与因子结构，**只有维度权重不同**（§3），因此两周期可逐项对账。

### 2.1 缺失处理（禁止静默补零）

| 层级 | 行为 | 报告可见痕迹 |
|---|---|---|
| 指标 | 缺失 → `score=null`、`missing=true`，绝不填 0 或行业中位数 | 指标 `value/band/score` 为 null |
| 因子 | 部分缺失 → 在可用指标内重归一化 | `available_weight_ratio`、`renormalized=true` |
| 因子 | 全部缺失 → 该因子不参与维度计算 | `renormalized=true`、`available_weight_ratio=0` |
| 维度 | 因子全缺 → 维度分 `null`，综合分在剩余维度内重归一化 | `score_mid=null` + DQ `gaps` |
| 估值 | PE/EPS/现价缺失 → 相对估值 `available=false`，合成退化 `dcf_only` | `relative.available`、`synthesis.mode` |
| 必填字段 | 缺失 → DQ `forced_manual_review=true` → 分桶强制"人工复核" | `data_quality.notes` |

> 重归一化不是"修正数据"，而是明确声明"这一档结论建立在更少证据上"，并由置信度惩罚体现（§4）。

## 3. 双周期权重

| 维度 | 中线权重（6–12 月，主） | 短线权重（0–3 月，参考） | 设计意图 |
|---|---|---|---|
{weight_rows}

- 结构恒定、权重可换：保证同一标的在两种周期下可对比，且可回放（设计文档 §5.2）。
- 中线以**公司(0.22)+财务(0.20)+估值(0.16)** 为主轴；短线以**催化(0.30)+交易面(0.30)** 为主轴，估值与公司权重主动降为参考项。
- 短线分**只输出参考桶，不改变中线定位**，报告中两者并列展示。

## 4. 置信度模型

置信度回答的是"**这份评分有多可信**"，不是"上涨概率"。

```
conf_dim = clip(1 − 0.06 × 缺失指标数 − 来源惩罚 − 重归一化惩罚 − 时效惩罚, 0.20, 1.00)
conf_total = clip(Σ(conf_dim × 中线权重) × DQ_overall, 0, 1)
```

| 惩罚项 | 计算 | 依据 |
|---|---|---|
| 缺失指标 | 每个缺失指标 −0.06（在维度内累计） | 缺失越多，维度结论的证据基础越薄 |
| 来源惩罚 | `Σ(等级惩罚 × 指标权重) / Σ指标权重`；L1=0、L2=−0.02、L3=−0.08 | 二手研报/媒体数据与原始披露不等价 |
| 重归一化 | 覆盖不足 60% 的因子，每个 −0.05 | 因子被"部分代表"时结论易偏 |
| 时效 | 超期 1.5 倍 −0.10；超期 2 倍 −0.20（取最严） | 用过期数据做当期判断是隐性未来函数 |
| 数据质量 | 总分再乘 `DQ_overall`（0.5×完整性+0.3×时效+0.2×来源） | 全局质量对全部维度同向衰减 |

分档：`high ≥ 0.75`、`medium ≥ 0.50`、`low < 0.50`。低于 0.50 时结论必须降级表述（设计文档 §5.5）。

## 5. 决策映射（先门槛后分数）

判定**严格按序**，先触发者生效——高分不能掩盖红旗：

| 序 | 条件 | 结果 | 设计理由 |
|---|---|---|---|
| 1 | 硬红旗：ST / 审计非标 / 监管立案 / 重大舞弊 | **回避** | 一次性否决，不参与打分竞争 |
| 2 | 财务红旗 ≥ 2 条 | **回避** | 红旗具有"边际影响陡增"性质（见 risk.red_flags 量规） |
| 3 | DQ 强制：`overall<0.5` 或必填字段缺失 | **人工复核** | 数据不足时不给结论，不给"看起来像结论"的中间态 |
| 4 | 特殊状态：停牌 / 重组 / 上市不足 1 年 | **人工复核** | 常规评分口径失真 |
| 5 | 信号冲突：综合分 ≥7.0 但任一维度 ≤3.0 | **人工复核** | "高分 + 单维极弱"是典型的判断分歧点（贵/风险/质量陷阱），机器不应独断 |
| 6 | 综合分 ≥7.0 且各维度 ≥4.0 | **入池** | 进入研究池，仍需人工立项 |
| 7 | 5.0 ≤ 综合分 < 7.0，或 3.5 ≤ 综合分 < 5.0，或存在维度 <4.0 | **观察** | 逻辑或验证条件待补 |
| 8 | 综合分 < 3.5 | **回避** | 多维度系统性不达标 |

> 综合分 <7.0 时不评估信号冲突：此时结果本就是"观察"，再升级为"人工复核"不会改变可操作性，只会稀释复核资源。

**四桶语义边界**：入池＝值得投入研究工时；观察＝保留跟踪、条件未满足；回避＝当前不投入；人工复核＝框架拒绝自动判定。**四者均不构成买卖建议**，报告不输出价位、仓位、时点。

## 6. 人工复核触发清单（可执行）

| 触发源 | 判定位置 | 报告字段 |
|---|---|---|
| DQ 总分 < 0.5 | `dataquality.compute_dq` | `data_quality.forced_manual_review` |
| 必填字段缺失（price_close / pe_ttm / roe_ttm / rev_growth / eps_ttm） | 同上 | `data_quality.gaps` |
| 综合分 ≥7.0 且某维度 ≤3.0 | `decision.decide` | `conclusion.bucket_mid=人工复核` + reasons |
| 停牌 / 重组 / 上市<1 年 | 同上 | reasons |
| 两法估值区间无重叠（`mode=union`） | 引擎置证伪条件 F*，需人工定夺方法分歧 | `valuation.synthesis.mode` |
| 相对估值不可用（单法） | 同上 | `synthesis.mode=dcf_only` |
| 永续终值占比 >70% | DCF 警示 | `valuation.dcf.warnings` |

## 7. 反黑箱约束（代码级强制）

| 约束 | 强制方式 |
|---|---|
| 不堆指标 | 每维度因子 ≤3、每因子指标 ≤4（`tests/test_scoring.py` 断言） |
| 不黑箱 | 每个指标必须写 `rationale`（≥10 字），否则测试失败 |
| 可追溯 | 每个指标携带 `source / source_tier / as_of / available_at`，缺一不可 |
| 无未来函数 | 全部入模字段 `available_at ≤ analysis_date`，违反抛 `LookAheadError` |
| 不补零 | 缺失只置 null + 重归一化，禁止填 0 或替代值 |
| 不给点位 | 报告任何字段不得出现买卖/目标点位措辞（`tests/test_report.py` 断言） |
| 结论条件化 | `maintain_conditions ≥3`、`falsification ≥3`，每条含可观测指标与阈值 |
| 概率自洽 | 三情景概率合计必须为 1，否则拒绝出报告 |

## 8. 阈值来源索引

| 阈值 | 取值 | 方法论出处 |
|---|---|---|
| ROE 优秀线 | 18%（严格 20%） | 质量增长：长期 ROE>20% 为优秀，18% 为放宽门槛 |
| ROIC − WACC | 5pp 为优秀 | 质量增长：真实经营优势的标尺 |
| OCF/净利润 | 1.0 为合格 | 盈利质量：现金含量检验 |
| 营收/利润增速 | 25%+ / 15% / 8% | 与名义 GDP（5–6%）及行业增速对比定位 |
| PE 分位 | <30% 低 / 30–70% 中 / >70% 高 | 估值方法论：分位带 |
| PEG | <1 匹配、>2.5 显著偏离 | 增长与估值匹配度 |
| 安全边际 | ≥15% | DCF 区间中枢 vs 现价 |
| WACC / 永续 g | 8–12% / 2–3% | 贴现率合理区间；g 不得超长期 GDP 增速 |
| 终值占比 | >70% 警示 | 终值主导时 DCF 可信度降档 |
| 质押比例 | >30% 关注、>50% 高风险 | 监管与市场惯例 |
| 商誉/净资产 | >25% 关注、>40% 高风险 | 减值风险 |
| 客户集中度 | >30% 依赖、>50% 重大 | 单一大客户风险 |
| PMI | 50 荣枯线、51.5 扩张加速 | 宏观景气 |
| M1 同比 | >8% 宽松、<0 收缩 | 流动性 |
| 事件影响分 | P×I >8 逐条扣减、风险分上限 20 | 风险矩阵（概率×影响） |
| 催化强度 | 强 4 / 中 2.5 / 弱 1 / 未证实传闻 0 | 催化分级；传闻不计分（噪音） |
| 利好已定价 | 下一事件强度 ×0.4 | 兑现风险折价 |

## 9. 评分卡阅读顺序（给复核人）

1. 先看 `conclusion.bucket_mid` 与 `confidence`——若为"人工复核"，后续数字只作参考；
2. 再看 `data_quality`——完整性/时效/来源是否支撑这份结论；
3. 逐维度看 `score_mid` 与 `confidence`，找**最弱维度**（决定桶的往往是最弱项而非平均）；
4. 对最弱维度下钻到因子→指标，核对 `band` 是否与原始 `value` 一致（量规可对账）；
5. 看 `falsification` 与 `tracking_checklist`——**下一季度用哪几个数字验证/证伪**；
6. 看 `valuation` 两法区间是否重叠、`mode` 是否 union/dcf_only；
7. 最后看 `risk_matrix` 的 P×I 与触发条件，确认最大风险是否已被跟踪项覆盖。
"""


def main() -> int:
    weight_rows = []
    for d in DIMENSIONS:
        intent = {
            "macro": "判断流动性与政策是否提供顺风，不单独决定个股结论",
            "industry": "景气与格局决定盈利弹性上限",
            "company": "护城河/治理/逻辑强度是中线持有的核心前提",
            "financial": "增长质量与资本回报是逻辑的量化验证",
            "valuation": "安全边际与预期匹配度决定风险收益比",
            "catalyst": "事件驱动强度与密度",
            "trading": "趋势位置、量价筹码与拥挤度",
            "risk": "风险矩阵与红旗的可控度",
        }.get(d.id, "")
        weight_rows.append(
            f"| {d.name}（`{d.id}`） | {d.weight_mid:.2f} | {d.weight_short:.2f} | {intent} |"
        )

    body = NARRATIVE.replace("{weight_rows}", "\n".join(weight_rows))
    tables = render_scoring_tables()
    # 去掉渲染器自带的一级标题（文档已有"附录 A"标题），保留"生成命令"说明
    if tables.startswith("## 附："):
        tables = tables.split("\n", 1)[1].lstrip("\n")
    doc = body + "\n\n---\n\n# 附录 A：量规总表（自动生成，勿手改）\n\n" + tables + "\n"

    out = os.path.join(ROOT, "docs", "02_scoring_guide.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"[OK] 已生成 {out}（{len(doc.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
