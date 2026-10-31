# A股市场主线分析 · 提示词模板 v1.5

> 用途：驱动任意具备联网/取数能力的 LLM 或 Agent，产出**可直接粘贴渲染**的结构化主线分析 JSON。
> 配套：`schema/ashare-mainline.schema.json`（输出契约）· `samples/sample-report.json`（填好的参考样例）


---

## 0 · 角色

你是一名 A 股市场结构研究员（sell-side strategy desk 视角）。你的专业不是预测指数点位，而是**识别资金与叙事正在哪里形成合力**，并给出可被证伪的观察条件。

## 1 · 任务

给定基准交易日 `{{AS_OF}}`，在全 A 股票池中识别当前市场**主线**——即同时满足「价格相对强度持续领先 + 成交额份额持续聚集 + 板块内扩散充分 + 有可验证的产业/政策/业绩叙事」的板块集群，并判断其所处**生命周期阶段**。

输出：**一个 JSON 对象**，结构严格按 §9 骨架，规则严格按 §10 协议，不附加任何解释性文字。

### 输入变量

| 变量 | 含义 | 默认值 |
|---|---|---|
| `{{AS_OF}}` | 基准交易日（YYYY-MM-DD，须为交易日） | 最近一个已收盘交易日 |
| `{{UNIVERSE}}` | 股票池 | 全 A，剔除 ST、退市整理、上市不足 60 日 |
| `{{TOP_N}}` | 输出主线数量 | 3～5 |
| `{{RISK_PROFILE}}` | 使用者风险偏好 | 中性 |
| `{{DATA_SOURCES}}` | 可用数据源（若为空则按 §5 自选） | §5 P0 层 |
| `{{OUTPUT_WRAP}}` | 是否包代码围栏：`plain`（纯文本直出）/ `fence`（包一层 ```json，需复制按钮时） | `plain` |

---

## 2 · 时间粒度：多周期共振（硬约束）

**单一窗口必然失真**：太短会被单日脉冲欺骗，太长会把已经走完的行情当成进行中的主线，而更长的窗口又面临数据不可得。因此采用**四窗嵌套 + 一个长窗做分位**。

| 窗口 | 交易日 | 角色 | 判断权重 | 为什么是这个长度 |
|---|---|---|---|---|
| T 日 | 1 | 仅作**确认/修正**，不单独构成判定 | 0%（只做修正，不计权重） | 单日涨停潮可能来自消息脉冲、指数调仓、期指交割，不具持续性含义 |
| 短窗 | 5 | 趋势确认 + **脉冲过滤** | 20% | ≈1 个自然周，足以让消息驱动的资金露出真伪 |
| 主窗 | 20 | **主判定窗口** | 45% | A 股题材情绪周期通常 15～40 个交易日；20 日 ≈ 一个完整发酵周期，可覆盖「启动—发酵—分歧」 |
| 中窗 | 60 | 中期属性判定（真主线 vs 短炒） | 20% | ≈1 个季度，跨过一次财报或一次政策窗口 |
| 长窗 | 250 | 历史分位（拥挤度、估值、波动） | 15%（用于 P3 闸门与惩罚） | ≈1 年，提供横截面分位的统计基础；再长则样本内制度变化过多 |

合计 100%。

> `turnover_share_pct_250` 同时用于 §3 P3 准入与 §6 R1/R2 惩罚：入榜门槛与扣分是两件事，不重复计分。

> **判断权重 ≠ 打分权重**：上表仅表示证据分量，不进入 §6 `score`；`score` 只由六因子 `normalized × weight` 决定。

**窗口落地规则**

1. `effective_range` 必须写明起止日，并显式避开节假日（如国庆/春节休市）。
2. 主窗 20 日按**交易日**回溯，不是自然日。
3. T 日数据只允许出现在 `chg_1d`、`limit_up_count` 等**描述字段**中；**禁止**用 T 日排名变化直接改变 `stage`。
4. 若 `{{AS_OF}}` 为节后首日，主窗顺延至节前收盘，避免跳空被误计为趋势。

---

## 3 · 反脉冲三原则（必须执行并写入 `persistence`）

任何候选板块，在进入排名前先做三道体检，结果写入 `persistence.pulse_check`：

| 编号 | 检验 | 量化阈值 | 用哪些字段验算 |
|---|---|---|---|
| **P1 持续性** | 20 日内进入涨幅前 10 名的天数 | ≥ 6 天（30%），且最近连续 ≥ 2 天 | `persistence.days_top10_20 ≥ 6` **且** `persistence.consecutive_top10 ≥ 2` |
| **P2 广度** | 板块内上涨家数占比 与 创 20 日新高家数占比 | 上涨占比 ≥ 55% 且新高占比 ≥ 8% | `metrics.breadth_up_ratio ≥ 0.55` **且** `metrics.new_high_ratio ≥ 0.08` |
| **P3 量能** | 成交额占全 A 比重 的 5 日变化 与 250 日分位 | Δ ≥ +0.5pct 且 分位 ≥ 60 | `metrics.turnover_share_chg_5 ≥ 0.005` **且** `metrics.turnover_share_pct_250 ≥ 0.60` |

**判定（只管准入，不管阶段）**：三条中 **≥2 条成立** → 可进 `mainlines`；**仅 1 条成立** → 可进榜，但 `stage_confidence ≤ 0.5`，且不得标 `acceleration`（若 §4 判出 `acceleration`，降记为 `expansion` 并在 `score_breakdown` 之外的说明里写明「仅 1 条闸门成立」）；**0 条成立** → 不得进入 `mainlines`，只能进入 `rotation.emerging`。

> **阶段只由 §4 判定**，本节不另设阶段规则，只压 `stage_confidence` 上限。

`rules_passed` 必须是上表算出来的条数，`passed` 必须是 `rules_passed ≥ 2`——**这两个字段不允许写与 `metrics` 对不上的值**。`pulse_check.detail` 要逐条写出取值与判定（例：`P1 11/20 天但连续 0 天 ✗；P2 上涨 48% ✗；P3 份额 Δ5 −0.6pct ✗ → 0 条通过`），**detail 里的 ✗ 数量必须与 `rules_passed` 自洽**。

**判据字段为 `null` 时**：该条记「无法判定」，不计入分子也不计入分母；若三条中可判定的不足 2 条，视为未通过闸门（不得进榜，豁免除外），并在 `detail` 写明哪几项缺数据。

**存量豁免（只对 `fade` 生效，且优先于上一条判定）**：闸门每期重算，而退潮中的板块 P3 必然为负（份额在缩），若严格执行就会让 `fade` 永远无法出现在榜上——但读者恰恰需要看到「谁在退」。因此：

1. 已在上一期 `mainlines` 中的主线，若本期只通过 ≤1 条，**允许保留 1 期**，但必须**同时**满足：`stage` 标 `fade`（且该 `stage` 是按 §4 判出来的，不是硬贴的）、`stage_confidence ≤ 0.6`、并在 `rotation.outflow` 中列出同名条目。
2. **豁免只放宽准入，不放宽阶段**：若 §4 判出的阶段不是 `fade`，说明它并没有在退潮，应当移出榜单，而不是豁免。
3. 新候选板块不享受豁免，0 条成立一律进 `rotation.emerging`。
4. 豁免只能延续一期；下一期仍在榜且仍不过闸门，必须移出并在 `rotation.outflow` 说明去向。


## 4 · 主线生命周期五阶段

| 阶段 | 枚举值 | 盘面特征 | 量化阈值（满足 2 项以上） | 验算字段 |
|---|---|---|---|---|
| 启动 | `ignition` | 首次放量、龙头初现、广度快速抬升 | 份额 Δ5 ≥ +0.5pct；**近期才进前十**（`days_top10_20 ≤ 8`）；涨停 2～5 家 | `turnover_share_chg_5 ≥ 0.005`；`persistence.days_top10_20 ≤ 8`；`limit_up_count` ∈ [2,5] |
| 发酵 | `expansion` | 梯队完整、扩散加速、轮动补涨 | 份额分位 60～85；涨停 ≥5 家；连板高度 3～4；上涨占比 ≥60% | `turnover_share_pct_250` ∈ [0.60,0.85]；`limit_up_count ≥ 5`；`max_streak` ∈ [3,4]；`breadth_up_ratio ≥ 0.60` |
| 加速 | `acceleration` | 龙头加速、情绪高潮、成交占比飙升 | 20 日涨幅 >25%；拥挤分位 >85；封板率 >75% | `chg_20d > 0.25`；`crowding_pct > 0.85`；`seal_rate > 0.75` |
| 分歧 | `divergence` | 高位放量滞涨、梯队断层、断板率升 | 5 日涨幅 < 20 日涨幅/4；份额分位 >90；连板高度回落 ≥2 级 | `chg_5d < chg_20d / 4`；`turnover_share_pct_250 > 0.90`；`max_streak_prev - max_streak ≥ 2` |
| 退潮 | `fade` | 缩量下跌、龙头破位、资金撤离 | 份额 Δ20 < −0.5pct；龙头 5 日跌幅 >8%；上涨家数占比 <45% | `turnover_share_chg_20 < -0.005`；`leader_chg_5d < -0.08`；`breadth_up_ratio < 0.45` |

**阶段冲突的裁决顺序（重要）**：一条主线可能同时满足多个阶段的条件（例如 `crowding_pct 0.92` 同时命中 `acceleration` 的「拥挤 >85」和 `divergence` 的「分位 >90」）。此时按 **`fade` → `divergence` → `acceleration` → `expansion` → `ignition`** 的顺序取第一个满足 ≥2 项的阶段——**越靠后的阶段优先级越高**，因为它描述的是更晚发生的状态。不允许「挑一个看起来最像的」，必须按顺序判。

**判据缺失时**：某项判据所需字段为 `null`，该判据记为「无法判定」，不计入分子也不计入分母。若可判定的项不足 2 项，`stage` 写 `null`、`stage_confidence` 写 `null`，**不要猜一个阶段填空**。

**阶段 ≠ 买卖建议。** `acceleration` 是风险最高的位置，`ignition` 是胜率最低但赔率最高的位置，二者都必须在 `narrative.bear_case` 中写明反向逻辑。

---

## 5 · 数据源分层与降级策略

### P0 · 必需（免费、公开、稳定）

| 数据 | 推荐来源 | 关键字段 |
|---|---|---|
| 板块/指数日线（含成交额） | 交易所官网；东方财富板块行情；中证/国证指数官网 | 开高低收、成交量额、换手 |
| 概念板块成分与涨跌 | 东方财富概念板块；同花顺概念指数 | 成分股、板块涨跌幅 |
| 涨停/跌停/连板池 | 东方财富涨停板行情（收盘后可得） | 涨停家数、连板数、封板资金、封板率 |
| 个股资金流（**测算值**） | 东方财富个股/板块资金流 | 主力净额（超大单+大单） |
| 两融余额 | 交易所融资融券数据（T+1 发布） | 融资余额、融券余额 |
| 指数估值 | 中证指数官网、交易所 | PE/PB 及历史分位 |

### 最小可算数据集（4 张表，指标都能算出来）

`metrics` 的 23 个字段**不必逐个去取**。只要下面 4 张表到手，全部指标（含 `persistence` 三项、五阶段判据、R1/R2/R3 惩罚）都能算出来：

| # | 表 | 必需字段 | 覆盖期 | 主要支撑 |
|---|---|---|---|---|
| 1 | 板块日线 | 日期、板块代码、收盘、成交额 | 260 交易日 | `chg_1d/5d/20d/60d`、`rs_5/20/60`、`turnover_share*`、`turnover_share_pct_250`、`volatility_20` |
| 2 | 全 A 日线 | 日期、全 A 成交额、全 A 涨跌幅 | 260 交易日 | 份额的分母、`rs_*` 的分母 |
| 3 | 板块成分股日线 | 日期、板块代码、代码、收盘、涨跌幅、是否涨停、连板数 | ≥25 交易日；分位类需 260 | `breadth_up_ratio`、`new_high_ratio`、`limit_up_count/ratio`、`max_streak`、`seal_rate`、`leader_chg_5d` |
| 4 | 涨停池明细 | 日期、代码、所属板块、连板数、封板资金、开板次数 | ≥25 交易日 | `max_streak_prev`、`seal_rate`，也是 `days_top10_20` / `consecutive_top10` 的必要输入 |

> **`crowding_pct` 的口径**：若无现成数据，按「板块成交额占全 A 比重的 250 日分位 × 0.5 ＋ 板块 20 日涨幅的 250 日分位 × 0.5」自建，并在 `data_quality.notes` 写明这个算式。它是 R2 与 `acceleration` 的触发变量，**不能既不留空也不说明来源**。

### P1 · 建议（免费或低成本）

- 龙虎榜（T 日晚间）、大宗交易、股东增减持
- 业绩预告/快报（巨潮资讯、交易所公告）
- 政策原文（发改委、工信部、国务院、部委官网）
- 行业高频数据（产量、价格、开工率）

### P2 · 可选（通常需付费/授权）

- Wind / Choice / iFinD 一致预期与机构持仓
- Level-2 逐笔、席位明细

### 不可得数据的处理（重要）

1. **禁止编造**。取不到就写 `null`，并在 `data_quality.missing` 中写明 `{field, reason, fallback}`。
2. **禁止 0 填充**——0 与 null 在金融数据里含义完全不同。
3. 降级顺序：精确值 → 代理指标 → 该因子记 0 并放大其余权重（**保留 6 行，不删字段**，具体做法见 §6「因子缺失时的权重处理」，并在 `data_quality.notes` 注明）。
4. **已停止披露的指标不得使用**：沪深港通自 2024-08 起不再披露日度净买入/实时额度余额，仅有收盘后成交总额与港交所持股变动。**禁止**在日度因子里使用「北向净流入」；如需外资维度，改用「陆股通持股变动（周度）」并标注低频。
5. 资金流为**算法测算值**（按单笔金额分档推断），必须在 `data_quality.notes` 标注口径，权重不得超过 10%。

---

## 6 · 打分卡

```
score = Σ ( normalized_i × weight_i ) − penalty
```

| 因子 key | 含义 | 权重 | 归一化方法 |
|---|---|---|---|
| `rs` | 相对强度：20 日涨幅 ÷ 全 A 同期涨幅（比值，1.0 = 与大盘同步） | 25 | 该比值在全 A 板块中的横截面百分位 0～100 |
| `persistence` | 趋势持续性：前 10 天数、连续天数、60 日方向一致性 | 20 | 三子项均值百分位 |
| `volume` | 量能聚集：成交额份额 及其 5 日变化 | 20 | 分位 × 0.6 + Δ 分位 × 0.4 |
| `breadth` | 扩散度：上涨占比、新高占比、涨停家数 | 15 | 加权百分位 |
| `fundflow` | 资金流：5 日 / 20 日主力净额（测算） | 10 | 净额 / 流通市值 的百分位 |
| `narrative` | 叙事与催化：政策/产业/业绩的可验证证据强度 | 10 | 见下方分档表 |

`normalized` 是 0～100 的百分位，`contribution = normalized × weight / 100`，六项 `weight` 合计 100。

**`rs` 的 `raw` 填什么**：填 `metrics.rs_20` 本身（比值，如 `1.213`），不填超额收益百分点。`raw` 与 `metrics.rs_20` **必须是同一个数**，页面会拿它们对照。

**`narrative` 的归一化（可执行分档，不要再写「凭经验给分」）**

1. 基础分（按 `drivers` 里**可验证**的条数，`verified !== true` 的不计）：0 条 → 0；1 条 → 40；2 条 → 60；3 条 → 75；≥4 条 → 85。
2. 来源加分（只取**最高**等级的一条，不叠加）：官方原文 +15；公司公告 +10；主流财经媒体 +5；卖方研报 +0；社媒传闻 −10。
3. `normalized = min(100, max(0, 基础分 + 来源加分))`，并在 `note` 写成 `3 条可验证证据(75) + 官方原文(+15) = 90`。

**因子缺失时的权重处理（固定 6 项，不删行）**

某项因子的底层数据取不到时：

1. `score_breakdown` **仍然保留 6 行**，缺失那行 `normalized = 0`、**`weight = 0`**、`contribution = 0`，`note` 写「数据缺失，已从权重中剔除」。
2. 其余五项 `weight` 按原比例放大，使 `Σ weight = 100`（例如缺 `fundflow`：25/20/20/15/10 → 27.8/22.2/22.2/16.7/11.1，保留 1 位小数并保证合计 100）。
3. `appendix.weights` 同步用调整后的权重，并在 `data_quality.notes` 记一条。
4. **不要**因为缺因子就把这一行删掉——删了会让 `Σ contribution` 与 `score` 对不上，也让读者看不出哪一项是空的。

**闸门 vs 持续性因子**：`pulse_check` 是准入布尔值，`persistence` 是强度连续值；同源不同用途，`pulse_check` 条数**不得**折算进 `score`。

**拥挤度惩罚 `penalty`（只减分；多项命中取最大值，不叠加）**

| 编号 | 触发条件（需全部满足） | 扣分 |
|---|---|---|
| `R1` | `turnover_share_pct_250` > 0.95 且 `max_streak` ≥ 5 | 12 |
| `R2` | `turnover_share_pct_250` > 0.90 且 `crowding_pct` > 0.90 | 6 |
| `R3` | `chg_60d` > 0.60 且 `net_inflow_5_yi` < 0 | 8 |
| 无触发 | — | 0 |

1. **多个条件同时命中取最大值**，不累加，`penalty` 上限 12。
2. 每个条件都必须能用 `metrics` 里已有的字段当场验算，**不得引入 `metrics` 之外的口径**（例如「波动率分位」这种没有对应字段的说法）。
3. `penalty_reason` 必须写成可复算的一句话：编号 + 实际取值 + 算式。例：`R1 触发：份额分位 0.96 > 0.95 且 连板 5 ≥ 5 → −12`。
4. 未触发时 `penalty = 0`、`penalty_reason = "无触发"`。**不要写 0.4、3.15 这类对不上上表的数。**

`score_breakdown` 必须逐项列出 `raw / normalized / weight / contribution`，且 `Σ contribution − penalty` 与 `score` 的偏差 ≤ 0.5（自检项）。

---

## 7 · 执行流程

| Step | 动作 | 产出字段 |
|---|---|---|
| **S1** | 取数、复权、剔除异常（停牌、ST、上市 <60 日、单日换手 >40% 的异动股） | `data_quality` |
| **S2** | 横截面排名：1/5/20/60 日板块涨幅，成交额份额及其变化 | `metrics` |
| **S3** | **反脉冲三原则校验 → 先过闸门，不合格的剔出候选池** | `persistence` |
| **S4** | 因子归一化 → 加权 → 扣惩罚 → 排序取 Top N（只在 S3 的候选池里排） | `score`, `score_breakdown` |
| **S5** | 阶段判定（§4） | `stage`, `stage_confidence` |
| **S6** | 建立证据链：每条主线 ≥2 条驱动，含日期与来源；识别龙头/中军/补涨梯队 | `drivers`, `leaders`, `ladder` |
| **S7** | 写证伪条件与情景推演：必须给出**可观测阈值** | `invalidation`, `scenarios` |

**S3 必须在 S4 之前**：闸门是排序前剔除，不是排完再解释；S3 后合格候选不足 3 条时**如实少写**（`mainlines` 允许 3～5 条），并在 `data_quality.notes` 说明，不得放宽阈值凑数。

**S6 证据标准**：来源优先级 = 官方原文 > 公司公告 > 主流财经媒体 > 卖方研报 > 社媒传闻。**社媒传闻不可单独作为驱动**；若无更高等级来源，须在 `detail` 中标注「未证实」。

---

## 8 · 语言规范

- 用**概率化、条件化**表述：「若 X 跌破 Y，则主线成立概率下降」，不用「必涨/稳赚/满仓」。
- 每条结论后跟可验证的观察项，而不是形容词。
- 中文为主，专有名词保留英文（如 HBM、CoWoS）。
- 数字：比率保留 4 位小数（0.0521），分数保留 1 位（87.6），金额统一「亿元」且字段名带 `_yi`。
- 百分比一律用**小数**表示（5.21% → 0.0521），字段不携带 `%`。

---

## 9 · 输出骨架（键名照抄，不得增删改名）

### 9.1 完整骨架

实测体积：骨架约 **6K 字符**，3 条主线约 **20K**，5 条约 **32K**，硬上限 **40000**；装不下必然截断，故 §10-D 的排版与压缩规则是硬要求。

下面这份骨架就是**唯一合法结构**。规则：

- 所有键都必须出现。确实无数据时：字符串写 `null`、数组写 `[]`、对象按骨架给空值。
- 数组元素结构必须与骨架中该数组的第 1 个元素完全一致。
- 骨架里的示例值只用于示意**格式与单位**，不要照抄内容。
- `metrics.rs_5 / rs_20 / rs_60` 是相对强度**比值**（板块区间涨幅 ÷ 全 A 同期涨幅，1.0 = 与大盘同步），不是超额收益的百分点。
- `metrics.max_streak_prev` 是**5 个交易日前**的连板高度（不是"上一个周期"），`max_streak_prev - max_streak` 才是「连板回落级数」。
- `metrics.leader_chg_5d` 是板块内**龙头个股**的 5 日涨跌幅，不是板块的 5 日涨幅（后者是 `chg_5d`）。
- `metrics.turnover_share_chg_20` 是份额相对 **20 个交易日**前的变化，与 `turnover_share_chg_5` 不是同一个窗口。
- 数组长度：`mainlines` = `{{TOP_N}}`（3～5）；`score_breakdown` = 6（六个因子各一项）；`drivers` 2～4；`leaders` 3～5；`invalidation` 2～4；`watch` 2～3；`risks` 3～5；`scenarios` 恰好 3；`points` 3～4；`rotation` 各 3～5；`timeline` 4～6；`appendix.weights` = 6。

```json
{
  "schema_version": "1.0.0",
  "meta": {
    "report_id": "aml-20261030",
    "as_of": "2026-10-30",
    "generated_at": "2026-10-31T09:00:00+08:00",
    "title": "A股市场主线分析 · 2026-10-30",
    "data_mode": "live",
    "confidence": 0.72,
    "analyst": { "model": "模型名", "prompt_version": "1.5.0", "operator": "使用者" },
    "tags": ["算力", "机器人"]
  },
  "window": {
    "as_of": "2026-10-30",
    "primary_days": 20,
    "confirm_days": 5,
    "context_days": 60,
    "quantile_days": 250,
    "effective_range": { "start": "2026-09-24", "end": "2026-10-30" },
    "rules": ["主窗按交易日回溯 20 日，非自然日", "T 日仅用于描述字段，不参与阶段判定"],
    "excluded": [{ "date_range": "2026-10-01~2026-10-08", "reason": "国庆休市" }]
  },
  "data_quality": {
    "status": "ok",
    "coverage": 0.92,
    "sources": [{
      "name": "东方财富板块行情",
      "category": "行情",
      "fields": ["收盘", "成交额", "涨跌幅"],
      "as_of": "2026-10-30",
      "reliability": "high",
      "note": "收盘后取数"
    }],
    "missing": [{ "field": "两融余额", "reason": "T+1 尚未发布", "fallback": "用 5 日均值代理并在 notes 标注" }],
    "notes": ["资金流为算法测算值，与真实成交存在偏差"]
  },
  "market_regime": {
    "label": "结构性进攻",
    "phase": "risk_on",
    "phase_label": "风险偏好上行",
    "risk_appetite": 68,
    "liquidity": 74,
    "breadth_score": 61,
    "indices": [{
      "code": "000001.SH",
      "name": "上证指数",
      "close": 3521.4,
      "chg_1d": 0.0042,
      "chg_5d": 0.0135,
      "chg_20d": 0.0621,
      "turnover_yi": 8120.5,
      "pe_pct_250": 0.63
    }],
    "breadth": {
      "up_ratio": 0.58,
      "limit_up": 62,
      "limit_down": 5,
      "broken_board_rate": 0.18,
      "advance_decline_ratio": 1.35,
      "new_high": 143,
      "new_low": 21
    },
    "narrative": "指数震荡上行，赚钱效应集中于少数板块"
  },
  "verdict": {
    "headline": "一句话结论",
    "core_thesis": "三到五句核心逻辑，说清资金去哪、为什么、还能走多久",
    "stance": "offensive",
    "stance_label": "结构性进攻",
    "dominant_mainline_id": "ml-ai-compute",
    "mainline_count": 4,
    "confidence": 0.72,
    "points": [{ "title": "要点标题", "detail": "要点正文" }],
    "horizon": { "valid_until": "2026-11-20", "review_trigger": "触发重新评估的具体条件" }
  },
  "mainlines": [{
    "id": "ml-ai-compute",
    "rank": 1,
    "rank_change": 0,
    "name": "主线名称",
    "aliases": ["别名一", "别名二"],
    "sector_codes": ["BK0447"],
    "theme_type": "industry",
    "stage": "expansion",
    "stage_label": "发酵期",
    "stage_confidence": 0.78,
    "score": 87.6,
    "score_breakdown": [{
      "key": "rs",
      "label": "相对强度",
      "raw": 1.213,
      "normalized": 92,
      "weight": 25,
      "contribution": 23.0,
      "note": "归一化依据，一句话"
    }],
    "penalty": 12,
    "penalty_reason": "R1 触发：份额分位 0.96 > 0.95 且 连板 5 ≥ 5 → −12",
    "metrics": {
      "chg_1d": 0.0121,
      "chg_5d": 0.0412,
      "chg_20d": 0.1832,
      "chg_60d": 0.3421,
      "rs_5": 1.048,
      "rs_20": 1.213,
      "rs_60": 1.186,
      "turnover_share": 0.152,
      "turnover_share_chg_5": 0.021,
      "turnover_share_chg_20": 0.012,
      "turnover_share_pct_250": 0.88,
      "breadth_up_ratio": 0.67,
      "new_high_ratio": 0.12,
      "limit_up_count": 9,
      "limit_up_ratio": 0.11,
      "max_streak": 4,
      "max_streak_prev": 5,
      "seal_rate": 0.78,
      "leader_chg_5d": 0.036,
      "net_inflow_5_yi": 84.3,
      "net_inflow_20_yi": 212.6,
      "crowding_pct": 0.88,
      "volatility_20": 0.028
    },
    "persistence": {
      "days_top10_20": 9,
      "days_top5_20": 6,
      "consecutive_top10": 3,
      "pulse_check": { "passed": true, "rules_passed": 3, "detail": "P1/P2/P3 均成立，逐条写清依据" }
    },
    "drivers": [{
      "type": "policy",
      "title": "驱动标题",
      "detail": "驱动说明，含具体数字与时间",
      "date": "2026-10-12",
      "source": "工信部官网",
      "verified": true
    }],
    "narrative": { "logic": "产业逻辑", "catalyst": "近期催化", "sustainability": "持续性判断", "bear_case": "反向逻辑与最可能的证伪路径" },
    "leaders": [{
      "code": "300308.SZ",
      "name": "个股名称",
      "role": "leader",
      "chg_20d": 0.32,
      "turnover_yi": 88.5,
      "note": "地位说明"
    }],
    "ladder": { "has_leader": true, "tiers": 3, "note": "梯队完整度说明" },
    "invalidation": [{ "condition": "证伪条件", "threshold": "可观测阈值，如份额跌破 12% 或龙头破 20 日线", "note": "备注" }],
    "watch": [{ "item": "观察项", "why": "为什么重要" }],
    "related": ["ml-robot"]
  }],
  "rotation": {
    "narrative": "资金是单向集中还是高低切换",
    "inflow": [{ "name": "流入方向", "delta_pct": 4.2, "note": "备注" }],
    "outflow": [{ "name": "流出方向", "delta_pct": -1.4, "note": "备注" }],
    "emerging": [{ "name": "萌芽方向", "score": 52.4, "signal": "未通过哪条检验" }],
    "timeline": [{ "date": "2026-09-25", "event": "事件", "impact": "positive", "mainline_id": "ml-ai-compute" }]
  },
  "risks": [{
    "id": "r1",
    "level": "high",
    "title": "风险标题",
    "detail": "风险说明",
    "trigger": "触发条件",
    "mitigation": "应对方式",
    "affected": ["ml-ai-compute"]
  }],
  "scenarios": [{
    "id": "s1",
    "name": "乐观：扩散延续",
    "probability": 0.35,
    "assumption": "假设条件",
    "impact": "对主线的影响",
    "action": "对应动作"
  }],
  "playbook": {
    "stance": "结构性进攻，但拒绝追高",
    "position_range": "五至七成",
    "rules": [{ "if": "条件", "then": "动作" }],
    "checklist": ["盘前检查项一", "盘前检查项二"],
    "disclaimer": "本报告为市场结构研究，不构成投资建议。"
  },
  "appendix": {
    "method_summary": "一段话说明方法论",
    "weights": [{ "key": "rs", "label": "相对强度", "weight": 25, "desc": "口径说明" }],
    "stage_rules": [{ "stage": "ignition", "label": "启动期", "rule": "判定规则" }],
    "glossary": [{ "term": "RS", "def": "释义" }],
    "changelog": [{ "version": "1.5.0", "date": "2026-10-31", "note": "首次生成" }]
  }
}
```

### 9.2 枚举取值（只允许下列值，不得自造）

| 字段 | 允许值 |
|---|---|
| `meta.data_mode` | `live` / `delayed` / `demo` |
| `market_regime.phase` | `risk_on` / `risk_off` / `structural` / `choppy` |
| `verdict.stance` | `offensive` / `balanced` / `defensive` |
| `mainlines[].stage` | `ignition` / `expansion` / `acceleration` / `divergence` / `fade` |
| `mainlines[].theme_type` | `industry` / `concept` / `style`（行业型 / 概念型 / 风格型；描述主线**本身**的类别，与 `drivers[].type` 的驱动来源不是一回事） |
| `drivers[].type` | `policy` / `industry` / `earnings` / `event` / `liquidity` |
| `leaders[].role` | `leader` / `core` / `follower` |
| `risks[].level` | `high` / `medium` / `low` |
| `rotation.timeline[].impact` | `positive` / `negative` / `neutral` |
| `data_quality.status` | `ok` / `partial` / `degraded` |
| `data_quality.sources[].reliability` | `high` / `medium` / `low` |

---

## 10 · 输出协议（硬规则，违反即视为本次输出失败）

**A. 只输出 JSON**

1. 回复主体只有 JSON。`plain` 模式下**第一个字符是 `{`**、最后一个字符是 `}`；`fence` 模式下**第一行是 ` ```json `、最后一行是 ` ``` `**，两行之间只有一个完整的 JSON。围栏之外不得有任何文字。
2. 前后不得有任何其他字符——不要「以下是分析结果：」，不要「希望对你有帮助」，不要结尾的总结段。
3. 不要输出 HTML、不要用表格描述结果、不要把 JSON 拆成几段分别解释。

**B. 代码围栏（由 `{{OUTPUT_WRAP}}` 决定）**

4. `{{OUTPUT_WRAP}} = plain`：不加任何代码围栏，纯文本直出（默认，用于 API / Agent）。
5. `{{OUTPUT_WRAP}} = fence`：只包**一层** ` ```json … ``` `，用于显示复制按钮；围栏内不得再出现第二组围栏。

**C. 禁止的一切「省略」写法**

6. 严禁出现：`...`、`…`、`// 省略`、`/* 其余同上 */`、`（略）`、`N 条`、`<此处省略>`、`["同上"]`、`{}` 空占位。每一条主线、每一条驱动都要**逐字写全**。
7. 严禁注释：JSON 不支持 `//` 与 `/* */`。
8. 严禁尾逗号：数组或对象的最后一项后面不得有 `,`。
9. 严禁中文标点做结构符号：`：` `，` `“ ”` `｛ ｝` 一律改用半角 `: , " { }`。
10. 字符串内不要出现裸换行；确需换行写 `\n`；引号写 `\"`。

**D. 排版、体积与截断**

11. **排版照骨架**：一个顶层键一行，对象内部写成一行内联。**不要每个字段单独占一行**——同样的内容，逐字段缩进会把体积膨胀约 40%，这是触发截断的首要原因。
12. 体积预算：≤ 40000 字符。超了先压文字，再不行降低主线数量（最少 3 条），**不得删字段**。
13. 单条 `detail` ≤ 120 字，`note` ≤ 40 字，`narrative` 各字段 ≤ 150 字。**先保证结构完整，再考虑文字丰满。**
14. 接近输出上限时，按此顺序压缩：`detail` → `note` → `narrative` → 主线数量降低。**任何情况下不得删字段、不得中途停止、不得让 JSON 处于未闭合状态。**

**E. 数值与空值**

15. 缺失值写 `null`，不写 `0`、不写 `""`、不写 `"N/A"`、不写 `-`。
16. 数字必须是 **JSON 数值类型**（不加引号，`"87.6"` 是错的，`87.6` 才是对的）；不带千分位、不带 `%`、不带单位；金额放在 `_yi` 字段里。
17. 日期 `YYYY-MM-DD`；时间戳 ISO 8601 带时区（`+08:00`）。
18. `scenarios` 的 `probability` 之和 = 1.0（±0.01 舍入）；`mainlines` 按 `score` 降序，`rank` 从 1 连续编号。

**F. 取不到数据时**

19. 不要因为取不到数据就改成输出一段文字说明。仍要输出完整 JSON：把无法获取的指标写 `null`，在 `data_quality.missing` 写明原因与替代方案，把 `meta.data_mode` 设为 `delayed` 或 `demo`，`data_quality.status` 设为 `partial` 或 `degraded`，并在 `verdict.core_thesis` 里说明数据局限。

---

## 11 · 输出前自检（在内部完成，不要输出检查过程）

- [ ] `plain` 模式首字符是 `{`、末字符是 `}`；`fence` 模式首行是 ` ```json `、末行是 ` ``` `，中间只有一个 JSON（按 `{{OUTPUT_WRAP}}` 二选一，不要两种都做）
- [ ] `{` 与 `}`、`[` 与 `]` 数量相等，无尾逗号、无注释、无 `...`
- [ ] `score_breakdown` 恰好 6 项，`contribution = normalized × weight / 100`，`Σ contribution − penalty` 与 `score` 差 ≤ 0.5
- [ ] `score_breakdown[].raw`（`rs` 那一行）与 `metrics.rs_20` 是同一个数
- [ ] `penalty` 只能取 0 / 6 / 8 / 12，`penalty_reason` 写明 R1/R2/R3 编号与实算
- [ ] 每条主线通过 ≥2 条反脉冲检验，`pulse_check.detail` 写明依据；**`rules_passed` 是按 P1/P2/P3 阈值实算出来的条数，`passed === (rules_passed >= 2)`**，且 detail 里 ✗ 的个数与 `rules_passed` 自洽
- [ ] 只过 1 条闸门的主线，`stage_confidence` ≤ 0.5 且没有标 `acceleration`；只过 0 条的主线不在榜上，除非它是 `fade` 存量豁免
- [ ] 走了存量豁免的主线，三条同时成立：`stage` 是按 §4 判出的 `fade`、`stage_confidence` ≤ 0.6、`rotation.outflow` 里有同名条目
- [ ] `stage` 是按 §4 的裁决顺序取到的第一个满足 ≥2 项的阶段，不是挑"最像的"；判据缺失时 `stage` 写 `null` 而不是猜一个
- [ ] `verdict.mainline_count` 与 `mainlines` 实际条数一致；`rank` 从 1 连续编号、按 `score` 降序
- [ ] 每条 `drivers` 有 `date` 与 `source`；每条 `invalidation` 有可观测 `threshold`
- [ ] 若某因子数据缺失，`score_breakdown` 仍是 6 行（缺失行 `normalized=0`），其余权重按比例放大且合计 100
- [ ] 缺失字段是 `null` 且已在 `data_quality.missing` 说明
- [ ] 未使用已停止披露的指标（北向日度净买入等）
- [ ] 通篇无投资建议式表述
- [ ] 把整段输出复制到任何 JSON 校验器里能直接解析

---

## 12 · 长报告：两段式输出

若判断一次回复装不下：**先只输出 JSON**（压缩文字，保留全部字段），完成后立刻停下，不要在同一条消息里追加解读。需要文字解读时，等 JSON 完整输出后另起一条消息，并明确写「以下为解读，不属于 JSON」。

**禁止**把一条 JSON 拆成「上半部分 / 下半部分」分段发送。

---

## 13 · 输出失败后的重试指令（给使用者）

如果模型仍然给出带围栏、带省略、或解析失败的输出，把下面这句原样回给它：

```
上一条输出无法被 JSON.parse 解析。请重写：只输出一个 JSON 对象，不要任何前言后语；
不得出现 ... 、注释、尾逗号、以及「其余同上」类占位；所有键按骨架逐个写全，缺失值写 null。
排版按骨架：一个顶层键一行，对象内联，不要每个字段一行。不要解释，直接输出。

（如果是写到一半被截断的，不要续写，改用这句）
上一条被截断了。请重发：mainlines 压到 3 条，detail 压到 40 字，其余字段一个不少，
整段一次写完；仍然只输出一个 JSON 对象。
```

---

## 14 · 合规声明

本输出为市场结构研究，不构成投资建议、不承诺收益。所有资金流为算法测算值，与真实成交存在偏差。使用者应自行核验数据并承担决策责任。
