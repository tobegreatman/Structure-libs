# stockframe v2 · 框架设计方案（重构蓝图）

> **一句话**：把"一个人的看法"变成"一份可回放、可证伪、可校准的证据链"。
>
> 本文是**重建规格**，不是使用说明。目标：没读过 1.0 代码的人只读本文即可重建；重建结果在语义上兼容 1.0，在已知缺陷上更好。

## 怎么用

| 场景 | 读哪几节 |
|---|---|
| 从零重建 | 全文，按 §14 的 M0→M6 顺序 |
| 只换评分体系 | §3 §4 §5 §12 |
| 只接别的数据源 | §3 §10 |
| 换领域（不限股票） | §1 §2 §5 §8 §9（把"公司/行业"换成"标的/环境"） |
| 代码评审 | §0.3 十八条不变式 + §13 债务表 |

**标记**：【照搬】1.0 已真机验证，直接沿用；【重写】1.0 有已知缺陷，按本文重做；【新增】1.0 没有。

**文档关系**：`01_design` 是 1.0 说明书，`02` 是量规表，`03` 是验收，`04` 是评估；本文是**目标态 + 重建顺序**。实现细节冲突时以 `03` 的机检为准，设计意图冲突时以本文为准。

---

## 0. 一页速览

### 0.1 三个定义

| | 内容 |
|---|---|
| 输入 | 单标的 + 单时点 + 39 个带时点的字段 + 3 段结构化判断（风险 / 催化 / 逻辑条目） |
| 处理 | 契约 → 时点 → 方法前门 → 估值 → 评分 → 归因 → 决策 → 渲染 |
| 输出 | 12 键 JSON（唯一事实源）+ 四桶 + 维持/证伪条件 + 字段级证据链 |

**不做什么**：买卖 / 加减仓 / 点位 / 仓位 / 组合优化 / 择时 / 收益预测。

### 0.2 架构（六层，单向依赖）

```
 L0 输入 ──┬─ meta（标的/时点/公司类型/红旗）
           ├─ data（39 字段，每字段 5 元组：value/as_of/available_at/source/tier）
           ├─ assumptions（DCF 三情景 + EPS 预期 + 情景概率）
           └─ risks / catalysts / logic_items
      │
 L1 契约  fields(登记) → contract(值) → pit(时点/来源)      ← 任一不合法即报错，不降级
      │
 L2 事实  registry: field_id → FieldRef{value, as_of, available_at, source, tier, proxy}
      │   derived: 派生字段注册表（公式 + 来源，可倒推）
      │
 L3 方法  gate(该不该算) → dcf / relative → synthesis(区间合成)
      │   risk(5×5) / catalyst(日历) / proxy(判断字段代理)
      │
 L4 评分  specs(声明式 8 维) → rubric(量规带 → 加权) → attribution(归因)
      │
 L5 决策  dq(质量) + coverage(覆盖) + gate + score → rules(优先级规则表) → bucket
      │
 L6 输出  report(12 键) → 结构 / 语义 / 措辞 / 有限性 四道闸 → json / md / html
```

依赖规则（违反即架构失败）：

1. 只允许 L 高依赖 L 低，禁止反向 import。
2. L1–L5 纯计算：无网络、无文件、无 `datetime.now()`、无随机、无全局可变状态；时间一律由 `analysis_date` 注入。
3. 网络 I/O 只允许出现在 `data/sources/`。
4. 表现层（web/md）不得包含任何阈值、权重、公式，只能渲染 JSON。
5. 参数（权重/阈值/闸门/惩罚系数）不得写死在控制流里，集中在 `specs/` 与 `calibration.json`。
6. 每个模块可被单独 import 并单测，不需要启动服务、不需要网络。

### 0.3 十八条不变式（重建的验收内核）

| 编号 | 不变式 | 违反后果 | 检查方式 |
|---|---|---|---|
| INV-01 | 时点：所有入模字段 `available_at ≤ analysis_date` 且 `as_of ≤ analysis_date` | 未来函数使回测全假 | 注入未来日期必抛错（A5） |
| INV-02 | 来源：L4（媒体/传闻）不得进评分路径 | 传闻被当事实 | 注入 L4 必抛 `ProvenanceError`（A9） |
| INV-03 | 登记：入模字段名必须在注册表内，拼错即报错 | 字段静默消失，报告少一块证据 | typo 字段必报错（A27） |
| INV-04 | 有限性：报告任意叶子为有限数，可被严格 JSON 解析 | NaN 写出非法 JSON | 递归断言（A30） |
| INV-05 | 量纲：字段 `unit/scale` 与量规带同量纲，禁止"百分数 vs 小数"混用 | 阈值整体偏 100 倍 | 入口 scale 校验【新增】 |
| INV-06 | 分值域：指标/因子/维度/综合分 ∈ [0,10] | 越界即伪分 | 聚合后断言 |
| INV-07 | 归一：每层权重和 = 1（维度中线/短线、因子、指标） | 分数不可比 | 启动断言（A2） |
| INV-08 | 无补零：缺失只能是 null，不得写 0，也不得按 0 加权 | 把"没数据"说成"很差" | missing 计数断言（A6） |
| INV-09 | 单调：删字段不得使综合分高于"该字段取中性分时的分数" | 删数据→加分 | 随机删字段 100 次【新增 A31】 |
| INV-10 | 解耦：覆盖只决定能否出桶，不参与分数计算 | 同一事实两套口径 | coverage 不入评分函数【新增】 |
| INV-11 | 可拆解：归因三块相加 = 综合分 ±0.01，权重份额和 = 1 | 结论不可归因 | 机检（A1） |
| INV-12 | 正性：任何价位区间下限 > 0，否则不出区间 | 负股价 | 机检（A28） |
| INV-13 | 单一口径：一份报告只允许一个 `safety_margin` 定义，且必须声明 basis | 两个安全边际并存 | 字段唯一性断言（A30） |
| INV-14 | 方法可适用：gate 判不适用时，所有价位字段为 null | 用不适用方法给出数 | 机检（A28） |
| INV-15 | 合规：全文无交易指令措辞（含风险/催化/跟踪文本） | 输出即建议 | 出口扫描（A29） |
| INV-16 | 可解释：每个结论带 `rule_id` + 可读理由 + 全量 `fired_rules` | 结论不可审计 | 机检（A1） |
| INV-17 | 同源：评分文档、界面、样例报告由代码生成，计数由脚本统计 | 文档漂移 | build `--check` |
| INV-18 | 确定性：同输入同输出（无时间/随机/字典序依赖） | 无法回放 | 两次运行逐字节比对 |

### 0.4 重建顺序（详见 §14）

`M0 契约骨架 → M1 评分内核 → M2 决策+输出 → M3 估值 → M4 取数 → M5 表现 → M6 校准`

每个里程碑都有可运行定义（DoD），不允许"先做界面再补逻辑"。

---

## 1. 问题与边界

### 1.1 要解决的三个问题

| 问题 | 表现 | 机制 |
|---|---|---|
| 结论不可拆 | 一个 7.2 分里混着人的判断、财报数据和风险计数，读者不知道在为谁的判断付钱 | 分数归因（§5.6） |
| 结论不可验 | 说"低估 45%"，没人能回放这个数怎么来的 | 字段级证据链 + 可回放（§9.1） |
| 结论不可错 | 没有证伪条件，错了也不知道错在哪 | 条件化结论 + 跟踪清单（§9.1） |

### 1.2 四桶语义

| 桶 | 语义 | 允许的附加信息 |
|---|---|---|
| 入池 | 证据覆盖达标且分数过线，进入深度研究候选 | 分数、条件、区间 |
| 观察 | 有信息但不足以支持入池 | 同上，且必须写明"缺什么" |
| 回避 | 硬红旗或分数显著低 | 红旗/规则，**不得**给"什么价位可以买" |
| 人工复核 | 框架**主动拒绝**出桶（数据不足 / 方法不适用 / 信号冲突） | 哪条规则拒绝、缺什么 |

**人工复核不是"待定"，是结论。** 它的意思是"本框架在这个标的上不成立"。

### 1.3 非目标（写下来防止膨胀）

组合与仓位 / 择时 / 目标价 / 一致预期 / 高频 / 期权定价 / 全行业估值模板。
v2 只新增一类行业模板（PB-ROE），不做"每个行业一套 DCF"。

### 1.4 合规（可执行，不是口号）

- 词表 + 短语白名单挖空 + 前 3 字否定豁免（"不加仓""低于卖方目标价"必须能过）；
- 出口扫描范围 = conclusion / scenarios / tracking / risk_matrix / catalyst_calendar 的**全部文本**；
- 任何价位字段必须是区间，且与"不构成投资建议"同页出现。

---

## 2. 架构

### 2.1 三个 seam（重构时最重要的三个切口）

| seam | 切在哪 | 为什么必须切 |
|---|---|---|
| S1 网络 ↔ 纯计算 | `sources/`（取数） / `build.py`（装配） | 网络不可离线测；PIT 与推导必须能反复回归 |
| S2 计算 ↔ 表现 | `pipeline/` / `render` | 界面永远不该有第二套口径 |
| S3 参数 ↔ 代码 | `specs/` + `calibration.json` / 引擎 | 调权重不该改 Python |

### 2.2 模块职责表

| 模块 | 职责 | 输入 → 输出 | 禁止 |
|---|---|---|---|
| `meta` | 版本、免责声明 | — | 依赖任何模块 |
| `fields` | 字段注册表（39） | — → `FieldSpec[]` | 含业务逻辑 |
| `units` | 量纲与换算【新增】 | 值 + unit → 标准量纲 | 静默换算 |
| `contract` | 值契约 | 值 → raise / ok | 修值、兜底 |
| `pit` | 三时点 | raw → `FieldRef` | 过滤缺失（应由 DQ 记） |
| `derive` | 派生字段注册表 | registry → `dict[str, Derived]` | 读全局状态 |
| `specs` | 8 维声明 | — → `DimensionSpec[]` | 读 registry |
| `rubric` | 量规求值 | specs + registry → results | 写报告 |
| `valuation` | 前门 / DCF / 相对 / 合成 | registry + assumptions → ranges | 猜缺失、调参凑数 |
| `risk` / `catalyst` | 矩阵 / 日历 | risks / catalysts → 结构体 | 生成价位 |
| `decision` | 规则表 | ctx → bucket + fired_rules | 用"感觉"补充规则 |
| `pipeline` | 阶段编排 | input → report | 内含业务公式 |
| `validate` / `invariants` | 四道闸 | report → ok / raise | 修报告 |
| `render` | md / html | report → 文本 | 计算任何指标 |
| `sources` | 取数 | query → `RawStock` | PIT / 评分 / 推导 |

### 2.3 管线形态【重写】

1.0 的 `engine.run()` 是近 400 行单体：契约、估值、派生、评分、覆盖、决策、结论、组装全在一个函数里，改任何一步都要读全文，也很难对单个阶段做单测。v2 改为**阶段化**：

```python
@dataclass(frozen=True)
class Context:
    raw: dict
    analysis_date: date
    registry: dict[str, FieldRef]
    derived: dict[str, Derived]
    gates: dict[str, Gate]
    dims: list[DimensionResult]
    dq: DQ
    coverage: Coverage
    decision: Decision
    notes: list[Note]          # 每条带 code / severity / detail（1.0 是自由字符串，无法机检）
    stage_log: list[StageLog]

class Stage(Protocol):
    name: str
    requires: tuple[str, ...]
    def run(self, ctx: Context) -> Context: ...

PIPELINE = (ContractStage(), GateStage(), ValuationStage(), DeriveStage(),
            RubricStage(), CoverageStage(), DecisionStage(), ConclusionStage(),
            AssembleStage(), GuardStage())
```

收益：阶段可单测、可重排、可跳过（gate 不适用就跳过估值）、可计时；`notes` 结构化后，"DQ 备注里写了什么"能被机检。

---

## 3. 数据契约

### 3.1 字段注册表（39）

| 组 | 数 | 字段 | 节奏 |
|---|---|---|---|
| market | 10 | `price_close*` `pe_ttm*` `pb` `pctile_pe_10y` `pctile_pb_10y` `turnover_pct` `vol_ratio_20d` `dist_ma250` `drawdown_52w` `inst_hold_chg_pct` | 1d（持仓 92d） |
| macro | 4 | `m1_yoy` `rate_10y_chg_yoy` `pmi` `policy_stance`(1-5) | 31d |
| industry | 5 | `industry_growth` `price_trend`(1-5) `stage`(1-5) `concentration`(1-5) `penetration_space`(1-5) | 92d |
| company | 6 | `moat`(1-5) `mgmt_record`(1-5) `pledge_pct` `logic_strength`(1-5) `logic_essence`(文本) `customer_concentration`(年报) | 92 / 365d |
| financial | 10 | `roe_ttm*` `roic` `wacc_assumed` `ocf_np_ratio` `wc_growth_gap` `rev_growth*` `np_growth` `gm_trend` `int_debt_ratio` `goodwill_pct` | 92d |
| valuation | 4 | `eps_ttm*` `peer_pe_median` `pe_hist_p25` `pe_hist_p75` | 92 / 1d |

`*` = 必填（缺失即强制人工复核）。定性字段必须是 int 1–5，按 `(档−1)×2.5` 换算。

```python
@dataclass(frozen=True)
class FieldSpec:
    id: str
    name: str
    unit: str                     # "%" | "倍" | "元" | "pp" | "bp" | "点" | "1-5档" | "文本" | "个"
    scale: Literal["pct", "ratio", "level", "text", "count"]   # 【新增】量纲语义
    cadence_days: int             # 披露节奏（不是"我什么时候拿到的"）
    annual: bool = False          # 一年一披露 → 数据期取最近年报期末
    required: bool = False
    qualitative: bool = False
    neutral: float | None = None  # 【新增】缺失时的中性分（默认 5.0；定性=5.0）
```

`scale` 解决 1.0 最隐蔽的一类错：`rev_growth` 用百分数（7.0），DCF 用小数（0.07），同一份报告两种量纲，接口一换就错 100 倍。v2 规则：**入口声明 scale，引擎内部一律 ratio，展示层统一乘 100**；传 0.07 给 `rev_growth` 必须报错而不是算出一个 0.049% 的阈值。

### 3.2 三时点【照搬】

`period_end` / `available_at` / `as_of`。判定只用 `available_at ≤ analysis_date`；**`period_end` 绝不用于可用性判断**——Q2 期末 6-30 的财报 8 月底才可得，用期末日判可用就是未来函数。

两条容易错的口径：

- 年报字段（客户集中度）：数据期 = 年报期末，`available_at` = 年报公告日，节奏 365d。用 92d 去卡会出现"年报公布三个月即超期"。
- 宏观：按惯例发布日保守后推（PMI 月末+2、M1 月末+15、中债曲线 T+1）。

### 3.3 来源分级【照搬】

| 级 | 含义 | 能否入模 | DQ 分 |
|---|---|---|---|
| L1 | 公告 / 监管披露 | 是 | 1.0 |
| L2 | 交易所 / 数据商整理 | 是 | 0.85 |
| L3 | 第三方估算 / 代理 | 是，但降 DQ 与置信度 | 0.6 |
| L4 | 媒体 / 传闻 | **否** | 0 |

L3 可以入模是刻意的：允许不完美，不允许隐瞒。

### 3.4 缺失的三态【重写】

1.0 只有一个 `missing=True`，把三件不同的事混成一件（`suppressed` 是后加的补丁，且必须"退出覆盖率分母"才不误伤）。v2 把三者变成一等公民：

| 状态 | 含义 | 进分母 | 分数 | 报告呈现 |
|---|---|---|---|---|
| `absent` | 没取到 / 没填 | 进（中性填充） | `neutral` | 缺口面板 + DQ |
| `suppressed` | 有数但方法上不成立（如两法分歧 + 终值主导的 DCF 安全边际） | **不进** | — | 显式理由 + 界面标记 |
| `not_applicable` | 整类方法不适用（银行股 DCF） | 不进 | — | gate 理由 + 所有价位 null |

表达方式必须结构化（字段级枚举），不能靠"分数是 null"来暗示——同一种 null 不能同时表示"没有"和"不该用"。

### 3.5 入口四道闸【照搬 + 加固】

| # | 闸 | 检查 | 拒绝 |
|---|---|---|---|
| 1 | 结构 | 顶层 5 键 / meta 5 键 / risks≥3 / catalysts≥2 | `InputError` |
| 2 | 登记 | `field_id ∈ registry`，拼错给 difflib 相近提示 | `ContractError` |
| 3 | 值 | 有限数、定性 int 1–5、文本 str≤200、价格 >0、scale 匹配【新增】 | `ContractError` |
| 4 | 时点 | `available_at/as_of ≤ analysis_date`，`tier ≠ L4` | `LookAheadError` / `ProvenanceError` |

**假设层用同一套闸**。1.0 曾漏掉：`wacc=NaN` 一路算出 NaN 区间，报告"通过 schema"（jsonschema 把 NaN 当 number），写出的文件任何严格解析器都读不了。假设层契约 = 三情景 × 9 标量 × 2 路径 + `wacc>g` + 概率和=1 + `shares>0` + 路径非空。

错误信息的标准格式：**点名（哪个字段）+ 原因（为什么错）+ 后果或做法（会怎样 / 该填什么）**。缺一条就不合格——用户看到的应该是"哪错了"，不是堆栈。

---

## 4. 证据分级

| 层 | 判定标准 | 例 | 报告标记 |
|---|---|---|---|
| 观测 | 数据源直接给出，无需推断 | M1、PMI、质押比例、年报客户集中度 | 来源 + as_of |
| 代理 | 公开数据按**固定公式**推档位，公式与误差方向都写明 | 护城河、生命周期、行业卡位、管理层记录 | `proxy: true` |
| 判断 | 结论本身，机器不能替 | 政策取向、渗透空间、逻辑强度、一句话本质 | `kind=judgement` |

三条代理纪律【照搬】：只计算不联网；数据不足返回 `None`（不补零、不默认、不猜）；一律落到与人工相同的 1–5 档，便于直接覆盖。

**字段账本恒等式**（A19）：`已填 N + 缺口 M ≡ 39`；缺口分四类 `judgement / data / source / other`。取数降级丢掉的字段必须显式列出，不能凭空消失。

**覆盖率的正确分母**【重写】：分母 = 该层全部指标权重和，且**剔除 suppressed**。1.0 的教训：一个高权重指标被合理排除，会让维度看起来"缺数"，把方法问题误报成数据问题。

---

## 5. 评分内核

### 5.1 声明式结构【照搬】

`Dimension(8) → Factor(≤3) → Indicator(≤4) → Band[]`。

硬约束（由测试强制，防止"为凑数收录指标"）：每维度因子 ≤3、每因子指标 ≤4、每指标必须带 `basis`（阈值依据编号，指向 docs 或外部文献）。

### 5.2 量规带规则【照搬】

- 阶梯带，区间 `[lo, hi)`，首带 `lo=None`、末带 `hi=None`；
- 带分只允许 0 / 2.5 / 5 / 7.5 / 10 五档（离散化，避免"看起来精确"）；
- 带必须有业务名（优/良/中/弱/差 或具体描述），不得只写数字；
- 无隐藏函数形状（不许在带内做线性插值），这是"可回放"的前提。

### 5.3 定性档位【照搬 + 加固】

1–5 档 → `(档−1)×2.5`。每档必须有锚定描述，且**锚定描述必须含可观测判据**。1.0 的部分档位只有形容词（"较强壁垒"），3 档和 4 档的分歧就是 2.5 分；v2 要求每档至少一个可核对的观察点，否则该指标不得收录。

界面上选档必须显示锚定短语，选中后展开完整描述（否则下拉退化成盲猜，猜错就是整个因子偏 2.5 分）。

### 5.4 聚合【重写】

1.0：

```
factor = Σ wᵢ·scoreᵢ / Σ wᵢ（仅非缺失）
```

分母随缺失收缩。实测后果：基线 7.64 → 删财务 7.81 → 只剩 2 个字段 8.67。**删掉差指标，剩下的好指标权重变大，分数奖励"不知道"。** R8/R3 只能挡分桶，改不了分数，于是同一份报告里"分更高、桶更保守"。

v2：

```
scoreᵢ    = absent ? neutralᵢ : band(valueᵢ)
factor    = Σ wᵢ · scoreᵢ / Σ wᵢ          ← 分母恒为全权重
dim       = Σ w_f · factor_f / Σ w_f
composite = Σ W_d · dim_d / Σ W_d
```

并同时输出三个数：`composite`（含中性填充）、`raw_composite`（仅实测指标）、`imputed_weight_share`（中性填充占比）。

性质：

- 全缺失 ⇒ 综合分 = 中性基线（默认 5.0），而不是 8.67；
- "删一个指标"的分数上界 = "该指标取中性分时的分数"，可精确计算（INV-09 可机检）；
- 分数与覆盖彻底解耦（INV-10）：分数回答"已知部分怎么看"，覆盖回答"已知部分够不够下结论"。1.0 用"重归一化 + 覆盖闸门"两个机制做同一件事，才出现自相矛盾。

为什么不用 0 兜底：0 会把"没有证据"和"证据很差"混为一谈，并制造"补数据 = 扣分"的荒谬激励。

### 5.5 权重与证据强度【重写】

1.0 实测：护城河的有效权重 0.11（**点一档 = 综合分 +0.27**），ROE 的有效权重 0.032（**ROE 从 8% 走到 20% 只 +0.16**）。人点一下 > 数据走一大段，这是"判断主导"的隐性事实，却没有任何约束。

v2 三条可机检的约束：

- `contribᵢ = W_dim × w_factor × w_ind`，要求 `max(contribᵢ) ≤ 0.06`（单指标最多贡献 0.6 分）；
- 同一维度内 `max(contrib)/min(contrib) ≤ 3`；
- 判断类指标权重合计 ≤ 该维度的 60%。

归因表必须直接印出 `judgement_share`，让"这份报告有多少来自人的档位"成为一等字段，而不是脚注。

### 5.6 归因【照搬 + 扩展】

三层：`judgement`（宏观/行业/公司）/ `data`（财务/估值/交易）/ `risk_catalyst`。
`part = Σ(组内 dim×W) / Σ(全部已评 W)`，三块相加 = 综合分（±0.01，分项各自 round 2 的必然误差；1.0 曾写"必须相等"，实际差 0.01）。

v2 增加两行：`proxy_share`（代理在判断层的占比）、`imputed_weight_share`（中性填充占比）——"多少是机器推的""多少是空的"也必须可见。

---

## 6. 估值

### 6.1 先门后算【照搬】

| 情形 | code | DCF | 相对法 | 价位字段 | 分桶 |
|---|---|---|---|---|---|
| 金融（bank / insurance / reit / financial） | `DCF_NOT_APPLICABLE` | 不算 | 声明口径未实现 → 不可用 | 全 null | R9 人工复核 |
| 亏损（EPS≤0 或 PE≤0） | `DCF_LOSS_MAKING` | 不算 | 不可用 | 全 null | R9 |
| 个体 DCF 股权价值 ≤0 | `DCF_NEGATIVE_EQUITY` | 该法不可用 | 照常 | DCF 部分 null | 不进 R9 |
| 正常 | — | 算 | PE 分位 | 区间 | R6 |

判据在 `COMPANY_PROFILES`（12 类，含"该类可用哪些相对估值口径"），不在 if/else 里。
`FULL_REJECT_CODES = {DCF_NOT_APPLICABLE}`：只有"整类公司没有口径"才拒绝出桶；个体算不出来不等于方法不成立。

### 6.2 DCF 契约【照搬】

```
5 年显性期：FCFF = EBIT×(1−tax) + D&A − Capex − ΔNWC
终值：     TV   = FCFF₅×(1+g)/(WACC−g)
股权：     equity = EV − net_debt
每股：     per_share = equity / shares
```

- 三情景 × 9 标量（`wacc / g / tax / dep_amort / capex / nwc / revenue_base / net_debt / shares`）+ 2 条 5 年路径；三情景概率和 = 1。
- 敏感性：WACC ±1pp 得区间；WACC−g 过小时区间退化为中枢点并告警。
- 告警（进 warnings，不阻断）：TV 占比 >70%、g>3%、WACC 不在 8–12%、WACC−g ≤0.5pp。
- 契约区间（入口强制）：WACC ∈ (0,0.5]、g ∈ [−0.05,0.12]、tax ∈ [0,0.6]、capex/rev ∈ [0,2]、shares>0。

**唯一允许的"不调参"原则**：DCF 算不出正股权价值时，不调假设凑数，而是把已装配的半成品返回（`partial: true`），让人改假设重跑。任何"调到能出数"都是编。

### 6.3 相对估值【照搬 + 插件化新增】

1.0 只有 PE 分位带（`P25×EPS` ~ `P75×EPS`）；PB / PS / PEG / EV_EBITDA 只在 profile 里声明、代码里不支持——**"声明了口径但不算"比"不支持"更危险**，因为读者以为交叉验证做了。

```python
class RelativeMethod(Protocol):
    id: str                        # "PE" | "PB" | "PS" | "EV_EBITDA" | "PEG"
    def applicable(self, profile, registry) -> Gate: ...
    def compute(self, registry, assumptions) -> RelativeResult: ...
```

要求：**只要在 profile 里声明可用，就必须有实现或显式标 `planned`**，启动时断言，把"声明与实现不一致"提前到 import 时。首批实现 PE 分位 + PB 分位（银行/周期）；PS、EV_EBITDA 留接口。

同业锚只报折溢价、不给点位：倍数差在 0.5–2 倍内才折算价位，超出只给百分比（代工、整机、材料混在同一板块时，倍数差来自生意不同，不是折价）。

### 6.4 区间合成状态机【照搬，语义固定】

| mode | 条件 | combined_range | 安全边际 |
|---|---|---|---|
| `not_applicable` | gate 不适用 | null | null |
| `rejected` | 交集/并集下限 ≤0 | null | null（拒答） |
| `intersection` | 两法区间有重叠 | 交集 | 有 |
| `union` | 无重叠但两法都可用 | 并集 | **null** |
| `dcf_only` | 相对法不可用 | DCF 区间 | 有（受抑制约束） |
| `relative_only` | DCF 不可用 | 相对法区间 | 有 |

1.0 的教训：union 时正文给"合成安全边际"（宁德 +282%），评分卡用 DCF 单法（+45.9%），同一份报告两个安全边际，读者只会挑对自己有利的那个。

v2 铁律（INV-13）：`safety_margin` 只有一个，且必须带 `margin_basis ∈ {dcf, relative, none}`；union / rejected 时为 null，另给 `dcf_margin_pct` / `relative_margin_pct` / `divergence_pct` 三个诊断数（并集中点没有经济含义）。

### 6.5 方法性抑制【照搬】

两法无重叠 **且** 基准情景 TV 占比 >70% ⇒ `val.dcf_margin` 标记 `suppressed`：不参与加权，**且退出覆盖率分母**。

理由：此时"低估"是终值假设的算术结果，不是证据。两个条件必须同时成立才抑制——成熟公司 TV 高是常态，单看一个会误伤。

### 6.6 价位字段的 null 语义【照搬】

`null` = 本框架对该标的/该情形不给这个数。任何情况下不得写 0（0 会被读成"价值为零"）。gate 不适用时 `scenarios` 必须为 `[]`，不得给三档价位。

---

## 7. 风险与催化

### 7.1 风险矩阵【照搬】

每条 `{id, name, category(10 类), P 1–5, I 1–5, trigger{metric, threshold}, mitigation, evidence}`；`score = P×I`；≥16 高 / ≥9 中 / 其余低；同页输出 5×5 计数网格 + top5。

### 7.2 风险维度评分【重写】

1.0：`10 × (1 − min(Σ clamp(PI−8, 0, 17), 20)/20)` —— **计数型 + 封顶**。两个后果：

- **边际灾难免费**：总和一旦触顶（20），第 3 条灾难性风险不再扣分；
- **轻微风险线性叠加**：10 条 P3×I3（都只是"中"）能把矩阵分压到 5.0，而它们的共同点只是"条数多"。

v2：主项取 max，只有"高"级别条目才累加：

```
penᵢ  = clamp(Pᵢ×Iᵢ − 8, 0, 17)
main  = max(penᵢ)
tail  = 0.15 × Σ_{i ≠ argmax, Pᵢ×Iᵢ ≥ 16} penᵢ
risk  = 10 × (1 − min(main + tail, 25)/25)
```

`matrix_score`（同一指标层）对照：

| 风险组合 | 1.0 | v2 |
|---|---|---|
| 1 条 P5×I5 | 1.5 | 3.2 |
| 2 条 P5×I5 | 0.0（封顶，第 2 条无边际） | 2.2（第 2 条仍有边际） |
| 5 条 P3×I3 | 7.5 | 9.6（不因条数堆叠） |
| 10 条 P3×I3 | 5.0 | 9.6 |

"最坏的那一条"决定风险分，额外的灾难才加罚——这才符合"风险是尾部事件"的经济含义。
注：`04_review.md` §9.3 里"1 条 P5×I5 得 8.0 分"是**维度分**（还含红旗数量、波动等指标加权），与上表不同层，不可直接比。
同时输出 `risks_truncated: bool`（是否存在未列出的风险），因为"只写 3 条风险"本身就是信息。

### 7.3 催化【照搬 + 去计数化】

日历：`{date, event, category, importance 1–5, priced_in, verify}`；按分析时点分"已发生 / 未来 90 天 / 90 天后"。

1.0：密度分（6–12 月事件数）+ 临近度分；中线权重 0.06 / 短线 0.30。

v2：`catalyst = max(importance × proximity × (1 − priced_in)) + 0.2 × 次强`，已定价事件权重 ×0.3。
理由同风险：事件的价值在"未被定价"，不在"数量多"。

---

## 8. 决策

### 8.1 规则表【重写】

1.0 是 if/elif 长链，编号与执行顺序不一致（R9 排在 R7 前、R4 与 R3 交错），"R 编号 = 优先级"的直觉是错的，改一条规则要重排整段控制流。

v2：显式 `priority`，编号只作标签；求值器**跑完全部规则**，输出 `fired_rules[]`（审计）+ 最高优先级结果（结论）。

```python
@dataclass(frozen=True)
class Rule:
    id: str
    priority: int
    when: Callable[[Context], bool]
    bucket: str
    reason: Callable[[Context], str]

RULES = (
    Rule("R1_HARD_FLAG",            10, hard_flag,                "回避", ...),
    Rule("R2_RED_FLAGS",            20, red_flags_ge_2,           "回避", ...),
    Rule("R3_DQ_FORCED",            30, dq_forced,                "人工复核", ...),
    Rule("R4_SPECIAL_STATUS",       40, special_status,            "人工复核", ...),
    Rule("R9_MODEL_NOT_APPLICABLE", 50, gate_full_reject,          "人工复核", ...),
    Rule("R7_COVERAGE",             60, judgement_coverage_lt_060, "人工复核", ...),
    Rule("R8_DATA_COVERAGE",        70, data_coverage_lt_060,      "人工复核", ...),
    Rule("R5_SIGNAL_CONFLICT",      80, conflict,                  "人工复核", ...),
    Rule("R6_SCORE_BAND",           90, always,    "入池/观察/回避", ...),
)
```

规则输入必须来自**同一个 Context**，不得读"可能不存在所以默认 0"的可选键。1.0 的 R2 就是这样在生产里失效的：`decide` 读 `meta["red_flag_count"]`，而输入里只有 `red_flags` 列表；单元测试直接传 count，所以一直是绿的。**测试通过 ≠ 生产生效。**

### 8.2 判定顺序的意义【照搬】

先"能不能判"（R1/R2 硬事实），再"有没有资格判"（R3/R4/R9/R7/R8），最后才是"判成什么"（R5/R6）。顺序颠倒会让高分掩盖红旗，或让"数据不足"被写成"观察"。

默认阈值（全部须在 M6 校准）：入池 ≥7.0 且无维度 <4.0；观察 5.0–7.0；回避 <3.5；判断层覆盖 ≥60%；数据层覆盖 ≥60%；冲突检测仅当 ≥7.0 时评估。

### 8.3 两条禁令【照搬，代码级】

- 任何桶不得携带价位 / 仓位 / 买卖时点（schema 里根本没有这些字段，不是"不输出"而是"没有位置"）。
- 人工复核必须写明"哪条规则拒绝、缺什么"，不能只写"需人工复核"。

---

## 9. 输出契约

### 9.1 报告骨架（12 键，顺序即阅读顺序）

```
schema_version / meta / conclusion / scorecard / valuation / scenarios /
risk_matrix / catalyst_calendar / tracking_checklist / data_quality /
evidence_log / disclaimer
```

- `conclusion`：`bucket_mid / bucket_short / composite_score_mid / composite_score_short / confidence / decision{rule_id, reasons, fired_rules} / summary / maintain_conditions(≥3) / falsification(≥3) / valid_until / review_cadence`
- `scorecard`：`dimensions[8] → factors → indicators{value, band, score, weight, missing, suppressed, rationale, evidence{source, source_tier, as_of, available_at}}` + `evidence_coverage` + `score_attribution`
- `evidence_log`：39 行，每行 `field_id / source / source_tier / as_of / available_at / used_by[] / proxy?`

结论必须"结论先行"：桶 + 分 + 条件 + 有效期在最前，明细在后。

### 9.2 四道出口闸【照搬 + 顺序固定】

| # | 闸 | 拦什么 | 归属 |
|---|---|---|---|
| 1 | 结构 | schema（enum / required / type） | `validate` |
| 2 | 语义 | 概率和=1、权重和=1、PIT 复核、区间正性、单一口径、归因相加 | `invariants`【新增独立函数】 |
| 3 | 措辞 | 违禁指令词（含风险/催化/跟踪） | `wording` |
| 4 | 有限性 | 递归 NaN / Inf | `contract.assert_finite` |

顺序不能换：措辞在有限性之前（否则 NaN 拼进字符串会把类型错误伪装成措辞错误）；有限性必须最后（它是"能不能写出去"的最后一关）。

### 9.3 null 的语义【照搬】

`null` = "本框架不给这个数"，与"缺失"同义，与 0 无关。所有区间字段的 null 必须能在 `valuation.gate` 或 `data_quality` 里找到理由——没有理由的 null 等同于 bug。

---

## 10. 取数层

### 10.1 seam

`Provider.query() → RawStock`（可 JSON 序列化）。Provider **不做** PIT、不做 TTM、不做评分、不做补零。
`build()` 负责：PIT 过滤（未披露则回退上一期）→ 口径推导（TTM / 同比 / 分位 / 份额）→ 代理 → 机械假设（标注 `assumption`）→ 缺口报告（四类）。

### 10.2 兜底与留痕【照搬】

| 项 | 主源 | 兜底 | 留痕要点 |
|---|---|---|---|
| K 线 | 东财 | 新浪 | "东财限流，改用新浪" |
| 换手率 | 直接给 | 成交量 ÷ 流通 A 股 | 分母必须是流通口径（用总股本会低估一倍） |
| 机构持仓 | 无专表 | 十大流通股东 `IS_HOLDORG` 合计 | "只覆盖前十大，方向性证据" |
| 10Y 收益率 | 东财不收录 | 中债曲线日频，T+1 | 365 天窗口内最近交易日，错位 >10 天留空，不插值 |
| 客户集中度 | F10 无此表 | 巨潮年报 PDF 解析第 1 名 | 只认序号 1；五行之和须等于合计（容差 0.06pp） |
| 同业 PE | 同板块中位数 | — | 倍数差 0.5–2 倍内才折算价位 |

客户表的三个坑（写进解析器注释，否则下次还会踩）：跨页断裂只剩 4 行；匿名客户名里的数字不能被当噪声排掉；只有"前五合计"时**不填值**，只在上界写进缺口说明。

### 10.3 三条拒绝路径【照搬】

| code | 含义 | 界面表现 |
|---|---|---|
| `NOT_FOUND` | 检索不到标的 | 提示换代码/名称 |
| `NO_PIT_DATA` | 分析时点前没有任何已披露报告 | 提示改时点 |
| `DCF_INFEASIBLE` | 数据到了，但机械外推的 DCF 跑不出正股权价值 | 带 `partial: true` 与半成品输入，载入编辑器让人改假设 |

原则：**宁可拒绝，不给半份数据**。取数降级丢掉的字段必须显式列出（A19）。

---

## 11. 表现层

三条纪律【照搬】：薄壳（无第二套公式）；同一份 JSON（界面分数恒等于 CLI 分数，A14）；构建期同步（模板 + 样例快照 → `index.html`，`--check` 失败即拒绝发布）。

界面必须有的四个"诚实组件"：

1. **覆盖条**：`已填 N / 39` + 缺口四类，且 N + M ≡ 39。
2. **归因卡**：判断层 / 数据层 / 风险催化三层贡献 + 权重份额。
3. **拒绝卡**：gate 不适用时给理由，不给灰掉的数字（灰掉的数字仍会被读成数字）。
4. **抑制卡**：suppressed 指标显示"有数但不计入"与原因。

【新增】v2 把"观测 / 代理 / 判断"的占比做成页面级指标，而不只是字段级琥珀色标记。

【新增】可读性同样进纪律，不做"后调样式"：

1. **文字色有下限**：承载信息的四级色（text / text-2 / muted / faint）在全部底色上 ≥4.5:1；装饰单独一个 `--ghost`。
   现有实现 `--faint #98a2af` 只有 2.6:1 却用在 35 处（含权重与指标读数），是这套界面唯一不合规项。
2. **结论条只放三件事**：一句话本质（两行，可截断）+ 数字（分桶 / 中线 / 短线 / 置信度）+ chip（规则 / 归因份额 / 覆盖 / 估值口径 / 缺口）。
   禁止用分隔符把五件事拼成一句长摘要——读者只能读第一遍，第二遍就开始跳读。
3. **归因不取数组首项**：摘要里的"主要归因"必须是可解释的量（缺口优先，其次权重份额），不能是 `notes[0]` 这种副作用排序。
4. **一份事实画一次**：雷达与条形榜同源时留一个；"综合得分"不许在顶部与环形图各出现一次。
   （1.0 已修：雷达保留但只画"得分形状"，条形榜改画"贡献=得分×权重"，同一个数不再两处编码；第三个环形由"中线得分 0.764"改为"判断层覆盖"；见 §13 D11）

---

## 12. 质量体系与校准

### 12.1 三层验收【照搬 + 扩展】

| 层 | 内容 | 频率 |
|---|---|---|
| 机检 | 30 项覆盖不变式（A1–A30） | 每次改动 |
| 单测 | 235 例（PIT / 评分 / 估值 / 决策 / 契约 / 措辞 / 取数 / 提示词锚文对账） | 每次改动 |
| 反向样本 | 未来函数、L4、NaN、档位 4.7、负 EPS、删字段、两法分歧 | 每次改动 |
| 真机 | 3 类标的（银行/消费/成长）全流程 + 人工复核结论 | 每轮 |
| 校准【新增】 | 回测：分桶前瞻收益单调性、阈值敏感性 | 每次改阈值 |

### 12.2 防漂移机制【照搬，1.0 最值得抄的设计】

- 评分文档由 `specs.py` 生成，不手写；
- 界面由模板 + 样例报告构建，`--check` 比对；
- 交付物计数（项数 / 例数 / 字节数）由脚本统计，不写死在文档里；
- 违禁词表在验收脚本与运行期模块同源，且有断言钉住。

### 12.3 回测与校准【新增，v2 的核心增量】

1.0 的缺口：7.0 / 5.0 / 3.5 全是约定，0 行回测代码。这让系统停在"可追溯"，没到"可验证"。

```
backtest/
├─ snapshots/     # 每个 as-of 一份输入（PIT 重放生成，禁止手改）
├─ run.py         # 对每个快照跑 pipeline，记录 bucket / score / coverage
├─ evaluate.py    # 前瞻 3/6/12M 收益、回撤、分桶单调性、命中率
└─ calibrate.py   # 阈值网格搜索 → calibration.json（版本化、可 diff）
```

判定"更好"的硬标准：

- **单调性**：入池 > 观察 > 回避 的前瞻 6–12M 收益中位数，且 bootstrap 置信区间不重叠；
- **稳定性**：同一标的相邻两季的桶迁移率（过高说明噪声主导）；
- **区分度**：分桶间收益分布的 KS 统计量；
- **代价**：达到上述指标所需的最小覆盖闸门（闸门太松则噪声进池）。

阈值写进 `calibration.json`，代码只读不写；每次改阈值必须附一份回测 diff。

### 12.4 判定"更好"的 6 个指标

| # | 指标 | 1.0 现状 | v2 目标 |
|---|---|---|---|
| 1 | 阈值有依据比例 | 约一半 | ≥90%（每条带依据编号） |
| 2 | 回测样本 | 0 | ≥200 个 as-of 快照，分桶单调 |
| 3 | 删字段不加分 | 违反（7.64→8.67） | 100% 通过（INV-09） |
| 4 | 单指标最大贡献 | 0.11 | ≤0.06 |
| 5 | 结论可机检比例 | 部分 | 100%（每条带可观测指标 + 阈值） |
| 6 | 非法输出拦截率 | 100% | 100% |

---

## 13. 从 1.0 到 2.0：必须一并解决的设计债务

| # | 现象 | 根因 | v2 方案 | 验收 |
|---|---|---|---|---|
| D1 | 删数据分数上升（7.64→8.67） | 重归一化让分母收缩 | 中性填充 + 分数/覆盖解耦（§5.4） | INV-09 随机删字段 100 次 |
| D2 | 风险/催化计数型 | Σ 惩罚 + 封顶 | max 主项 + 尾部加罚（§7.2/7.3） | 单条灾难 < 五条中风险 |
| D3 | 权重与证据强度反向 | 权重手填、无预算约束 | 贡献上限 + 维度内比例（§5.5） | `max contrib ≤ 0.06` |
| D4 | 阈值无校准 | 无回测 | `calibration.json` + 网格搜索（§12.3） | 分桶单调性通过 |
| D5 | 相对法只实现 PE | 声明与实现脱节 | 方法插件 + 启动断言（§6.3） | 声明口径全部有实现或标 `planned` |
| D6 | `confidence` 不是概率 | 命名误导（算法是完整度） | 拆成 `evidence_completeness` + `agreement` → `reliability` | 报告不再出现伪概率 |
| D7 | `company_type` 断代 | 未登记值 + 无迁移 | 启动校验 + `migrate.py`（旧值→新值 + 警告） | 旧输入可直接跑 |
| D8 | R2 红旗规则生产失效（1.0 已修，见 `04_review.md` §11） | 规则读不存在的键、测试直接构造 | 规则只吃 Context（§8.1），从类型上消掉"可选键 + 默认值" | 真机注入 2 条红旗 → 回避 |
| D9 | 门面矛盾（`relative_only` 仍提 DCF） | 文案分支未跟 mode | 文案由 mode 单向生成，禁止手写分支 | 机检：mode=relative_only 全文无 DCF 结论句 |
| D10 | 单指标量纲不一（% vs 小数） | 无 `scale` 声明 | `FieldSpec.scale` + 单位断言（§3.1） | 传 0.07 给 `rev_growth` 必须报错 |
| D11 | 同一组数画两遍（雷达/条形、环形/顶部）【1.0 已修】 | 每个视图各自找"好看的表达"，无人对总量负责 | 一份事实一个视图；环形改画"判断/代理/覆盖"三个不同量（§11） | 页面内任意数字只出现一次 |

---

## 14. 重建路线

| 阶段 | 交付 | 完成定义（DoD） |
|---|---|---|
| M0 骨架 | `meta / fields / contract / pit` + 1 维 1 指标 + CLI | 非法输入报错且点名字段；INV-01/02/03 绿 |
| M1 评分 | `specs`(8 维) + `rubric` + 归因 + 中性填充 | INV-05/06/07/08/09/10/11 绿；删字段不加分 |
| M2 输出 | report 12 键 + schema + 四道闸 + 规则表 | INV-04/15/16/18 绿；三只真机出报告 |
| M3 估值 | gate + DCF + relative + synthesis | INV-12/13/14 绿；银行 / 亏损 / 正常 三类分别 R9 / R9 / R6 |
| M4 取数 | sources + build + proxies + 拒绝路径 | 离线夹具全覆盖；真机 3 只填 35/39；账本对平 |
| M5 表现 | 模板 + 构建 + `--check` | INV-17 绿：界面分数 = CLI 分数 |
| M6 校准 | snapshots + evaluate + calibrate | 分桶单调；阈值有 diff |

顺序不可颠倒的理由：M1 之前做估值，会为了"看起来有结论"而先写渲染；M4 之前做界面，会把取数噪声当成评分 bug。**先证明评分可回放，再证明取数可复现，最后才做能看的界面。**

工作量经验排序：取数（40%）> 评分（20%）> 估值（15%）> 界面（15%）> 契约与闸门（10%）。取数最容易失控，所以 seam 必须先划。

---

## 15. 目录与关键接口

包名可沿用 `stockframe`；下面按**职责分层**重排，而不是按 1.0 的"一个功能一个文件"平铺。

```
src/stock/
├─ core/                # 纯计算（无 I/O）
│  ├─ meta.py  units.py  fields.py  contract.py  pit.py
│  ├─ derive.py         # 派生字段注册表（替代 dict[str, Any]）
│  ├─ rubric.py
│  ├─ specs/            # 一维一文件：macro.py industry.py … risk.py
│  ├─ valuation/        # gate.py dcf.py relative/ combine.py
│  ├─ risk.py  catalyst.py
│  ├─ decision.py       # 规则表 + 求值器
│  └─ pipeline.py       # 阶段编排
├─ data/                # 唯一有网络的地方
│  ├─ sources/{base,eastmoney,chinabond,cninfo}.py
│  ├─ build.py  proxies.py
├─ out/
│  ├─ assemble.py  validate.py  invariants.py  wording.py  render_md.py  server.py
└─ backtest/{run,evaluate,calibrate}.py

schema/report_schema.json   calibration.json   docs/   tests/   tools/   web/
```

关键接口（签名即契约）：

```python
def check_value(spec: FieldSpec, value: Any) -> None: ...       # raise，不返回 bool
def validate_all(data: dict, as_of: date) -> dict[str, FieldRef]: ...
def resolve_gate(meta, eps, pe) -> Gate: ...                     # 只判方法适用性
def run_dcf(a: Assumptions) -> DCFResult: ...                    # 可抛，可降级
def combine(results: list[ValuationResult], gate: Gate) -> Synthesis: ...
def evaluate(specs, registry, derived) -> list[DimensionResult]: ...
def decide(ctx: Context) -> Decision: ...                        # 纯函数，无 I/O
def assemble(ctx: Context) -> dict: ...                          # 唯一产出 report 的地方
def render(report: dict) -> str: ...                             # 不计算任何指标
```

三条实现纪律：错误一律"点名 + 原因 + 怎么办"；所有 `None` 必须在报告里有对应理由；任何函数不得同时做"判断"和"呈现"。

---

## 16. 附录

### A 默认参数（重建起点，全部须在 M6 校准）

| 参数 | 默认 | 依据 |
|---|---|---|
| 维度权重（中线） | .08 / .14 / .22 / .20 / .16 / .06 / .06 / .08 | 约定 |
| 维度权重（短线） | .05 / .10 / .05 / .05 / .05 / .30 / .30 / .10 | 约定 |
| 档位换算 | `(档−1)×2.5` | 离散五档 |
| 入池 / 观察 / 回避 | 7.0 / 5.0 / 3.5 | 约定 |
| 入池维度下限 | 4.0 | 约定 |
| 判断层覆盖下限 | 0.60 | 约定（实测判断层 81% 才落观察） |
| 数据层覆盖下限 | 0.60 | 约定 |
| DQ 权重 | 0.5 完整 + 0.3 时效 + 0.2 来源 | 约定 |
| DQ 强制复核线 | 0.5 | 约定 |
| 置信度惩罚 | 0.06/缺失项；L2 −0.02、L3 −0.08；重归一小项 −0.05；超期 −0.1 / −0.2 | 约定 |
| 风险惩罚 | `main=max`、`tail=0.15`、上限 25 | v2 新 |
| 催化 | `max + 0.2×次强`，已定价 ×0.3 | v2 新 |
| WACC 合理区 | 8–12% | 经验 |
| 永续 g 上限 | 3% | 长期 GDP |
| TV 占比警示 | 70% | 经验 |

**"约定" = 没有外部证据，只有可解释性。** 这类参数必须带 `source: "convention"` 标记；在 M6 被回测替换或证实之前，不得写进"结论句"，只能写进"评分"。

### B 重建检查清单

- [ ] 非法输入 8 类（未来函数 / L4 / NaN / 档位小数 / 档位越界 / typo / 价格≤0 / 文本塞数字）全部报错且点名字段
- [ ] 三只真实标的（银行 / 消费 / 成长）出报告，且严格 JSON 可解析
- [ ] 删任意字段，综合分不高于"该字段取中性分"的上界
- [ ] 银行股：DCF 不执行、价位全 null、分桶人工复核（R9）
- [ ] 亏损股：PE 口径拒答、区间不出现负值
- [ ] 两法分歧：只有一个安全边际（或 null），诊断三数齐全
- [ ] 归因三块相加 = 综合分 ±0.01
- [ ] 字段账本 N + M ≡ 39
- [ ] 界面分数 = CLI 分数（同输入）
- [ ] 全文无违禁指令词
- [ ] 评分文档由代码生成，`--check` 通过
- [ ] 回测：分桶单调，且阈值改动有 diff

### C 术语

| 术语 | 含义 |
|---|---|
| PIT | Point-In-Time：只用分析时点之前可得的数据 |
| TV 占比 | 永续终值占企业价值的比例，越高越依赖假设 |
| 判断层 | 宏观 / 行业 / 公司三维，主要输入是 1–5 档判断 |
| 代理 | 用公开数据按固定公式推出来的判断档位，不是结论 |
| 抑制 | 有数但方法上不成立，不参与评分且退出覆盖率分母 |
| gate | 方法适用性前门：先判"该不该算" |
| fired_rules | 本次判定命中的全部规则（不止最终生效的那条） |
| 中性填充 | 缺失指标按中性分计入，使分母恒定、分数可比 |
| 覆盖 | 指标级证据覆盖率，只决定能否出桶 |

### D 一页结论

这套框架的价值不在"算出一个分"，而在**它敢在什么时候不算**：数据不够（R7/R8）、方法不成立（R9）、两法打架且靠终值（suppression）、输入不合法（contract）。四条拒绝路径比任何一条评分规则都重要——因为一个从不拒绝的模型，必然会在某个标的上给出一个看起来正常的错数字。
