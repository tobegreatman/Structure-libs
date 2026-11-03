# 宏观月报 · 数据契约（JSON ↔ HTML）

> 本契约定义「提示词输出（JSON）」与「HTML 视图层」之间的数据接口。
> 提示词执行后**只输出 JSON**，不再直接写 HTML；HTML 由 `report-template.html` 读取 JSON 自动渲染。

---

## 1. 文件分工

| 文件 | 角色 | 说明 |
|------|------|------|
| `宏观指标解读提示词手册.md` | 执行指令 | 联网取数 + SOP 分析 → 输出符合本契约的 JSON |
| `schema/report.schema.json` | 机器契约 | JSON Schema（draft-07），字段强约束 |
| `schema/contract.md` | 本文件 | 人类可读的契约说明 |
| `report-template.html` | 视图模板 | 自包含 HTML（CSS+渲染JS），读取 JSON 渲染报告 |
| `data/YYYY-MM.json` | 数据产物 | 每期报告的 JSON 数据 |

---

## 2. 顶层结构（必填字段）

```
meta              报告元信息（标题/期数/搜索次数/完整度/缺失项/一句话图景）
indicators[]      数据总览表（每个指标一行：公布值/预期/前值/方向/标签）
signals           三组信号（领先/同步/滞后/一致性）
cpi_ppi           CPI/PPI 量化传导估算（剪刀差/传导时滞/未来CPI推算/可信度）
rate_cascade      利率三层瀑布（源头/批发价/实体价 + DR007利差 + NCD + 断流诊断）
fx                汇率三问（什么在动/为什么/传导/校准 + 中美利差 + DXY）
cross_validation  交叉验证矩阵 + 结论
cycle_gauge       周期定位四维仪表盘（每维度含分位+基准）+ 共振判定
inventory_cycle   库存周期定位（阶段 + PMI软调查 + 工业存货硬统计 + 分位）
falsification[]   证伪条件
watchlist[]       下月观察清单
sources[]         数据来源清单（{text, url} 对象）
```

**所有顶层字段均为必填**，不允许缺省；数组字段无内容时填空数组 `[]`。

---

## 3. 关键字段约定

### 3.1 方向色（direction）

| 值 | 含义 | 渲染色 |
|----|------|--------|
| `up` | 涨/升/超预期 | 红 `#ea2261`（ruby） |
| `down` | 跌/降/不及预期 | 绿 `#0e9f63` |
| `flat` | 持平/正常 | 灰 `#64748d` |

> 遵循国内惯例：红涨绿跌。

### 3.2 指标标签（label）

| 值 | 含义 | 渲染色 |
|----|------|--------|
| `lead` | 领先指标 | 淡靛蓝底 `#b9b9f9` · 深靛蓝字 `#4434d4` |
| `coin` | 同步指标 | 冷灰底 `#f6f9fc` · 灰字 `#64748d` |
| `lag` | 滞后指标 | 暖奶油底 `#f5e9d4` · 暖棕字 `#9b6829` |

### 3.3 状态（status，利率瀑布用）

| 值 | 含义 | 渲染色 |
|----|------|--------|
| `ok` | 通/正常 | 绿 |
| `no` | 断/异常 | 红 |
| `wait` | 待观察 | 灰 |

### 3.4 缺失值处理

- 数值类缺失：字符串填 `"缺失"`，渲染为灰色"缺失"。
- 预期/前值不适用：JSON 填 `null`，前端渲染为 `—`。
- **禁止用公布值冒充预期值**（铁律第2条）。

### 3.5 富文本片段

`cycle_gauge.dimensions[].meta` 字段允许嵌入少量 HTML（如 `<span class='extreme'>极端</span>`），用于标注极端状态。其余文本字段按纯文本渲染。

### 3.6 利率瀑布新增量化字段（rate_cascade）

| 字段 | 类型 | 说明 |
|------|------|------|
| `dr007_policy_spread` | number \| null | DR007 7日均值与7天逆回购政策利率的利差（bp）。正值=资金面偏紧，负值=偏松。数据缺失填 `null`。 |
| `ncd_rate` | number \| null | 同业存单（NCD）1年期发行利率（%）。银行负债成本领先指标，领先 LPR 1-2 个月。数据缺失填 `null`。 |

### 3.7 汇率归因必检旁证（fx）

| 字段 | 类型 | 说明 |
|------|------|------|
| `us_cn_10y_spread` | number \| null | 中美10年期国债收益率利差（中国−美国，bp）。负值=中美利率倒挂。汇率核心解释变量。数据缺失填 `null`。 |
| `dxy` | number \| null | 美元指数（DXY）。判断"动的是美元还是人民币"的关键参照。数据缺失填 `null`。 |

### 3.8 周期维度分位与基准（cycle_gauge.dimensions[]）

每个维度必须同时提供：
- `percentile`（number \| null）：历史分位数（0-100）。禁止主观判断"极端"而不给分位。
- `baseline`（string \| null）：分位计算基准说明，如"2008年以来沪深300 PE"。

### 3.9 库存周期定位（inventory_cycle）

| 字段 | 类型 | 说明 |
|------|------|------|
| `stage` | string | 库存四阶段：`主动补库` / `被动补库` / `主动去库` / `被动去库` |
| `pmi_raw_inventory` | number \| null | PMI 原材料库存分项（软调查） |
| `pmi_finished_inventory` | number \| null | PMI 产成品库存分项（软调查） |
| `industrial_inventory_yoy` | number \| null | 工业企业产成品存货同比 %（硬统计），与 PMI 软硬交叉验证 |
| `percentile` | number \| null | 库存周期历史分位（0=主动去库极端底部，100=主动补库极端顶部） |

### 3.10 CPI/PPI 量化传导（cpi_ppi）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ppi_yoy` | number | PPI 同比 % |
| `cpi_yoy` | number | CPI 同比 % |
| `core_cpi_yoy` | number | 核心 CPI 同比 % |
| `scissors` | number | PPI−CPI 剪刀差（pct），正值挤压中下游利润 |
| `transmission_lag_quarters` | number \| null | PPI→CPI 历史平均传导时滞（季度），通常 2-4 |
| `expected_cpi_in_quarters` | string \| null | 按当前 PPI 斜率推算的未来 CPI 区间 |
| `confidence` | string | 可信度：`高` / `中` / `低` |

### 3.11 数据来源（sources[]）

每条为对象，不再是纯字符串：

```json
{"text": "9月官方PMI → 国家统计局 · 2026-09-30", "url": "https://www.stats.gov.cn/..."}
```

- `text`（必填）：来源说明文字（数据项 → 来源机构 + 发布日期）。
- `url`（可选）：数据来源页链接，无则填 `null`。渲染为可点击超链接。

---

## 4. 渲染规则（HTML 模板约定）

### 4.0 渲染引擎

`report-template.html` 由 **Vue 3（内联自包含 runtime，无 CDN 依赖，双击离线可用）** 驱动：报告数据 `Object.freeze` 后注入只读渲染，展开/收起用 `v-show` 保持 DOM 复用。视觉体系遵循 `DESIGN.md`（Stripi 风格：渐变网格 Hero + 深靛蓝 `#533afd` 主 CTA + 300 细字重 + `tnum` 表格数字 + 药丸按钮）。

工具栏（sticky）功能：
- **复制提示词**：复制内嵌的精简速用版提示词（与手册附录 B 同源），粘贴给任意 AI / Agent 即可产出符合本契约的 JSON。
- **复制 JSON 模板**：复制空白 JSON 数据骨架（与手册附录 A 一致）。
- **导入 JSON**：选择本地 JSON 文件，先契约校验，失败弹红色 toast 且不覆盖当前数据；已有数据时二次确认。
- **导出 JSON**：将当前报告数据导出为 `YYYY-M.json` 文件。
- **示例数据**：一键重置为内置测试数据。

### 4.1 数据加载优先级

`report-template.html` 按以下顺序获取数据（先到先用）：
- **方式 A（build.js 注入）**：正式数据嵌入 `<script type="application/json" id="report-data">{ }</script>`，`node build.js data/YYYY-MM.json 报告名.html` 即采用此方式。
- **方式 B（默认测试数据）**：`#report-data` 为空时自动回退到内置 `#demo-data`（`data/2026-10.json` 快照），开箱即渲染示例报告。
- **方式 C（开发调试）**：URL `?data=xxx.json` 查询参数 fetch 外部 JSON。
- **方式 D（手动导入）**：工具栏「导入 JSON」按钮，含契约校验。

### 4.2 区域映射

| JSON 字段 | 渲染区域 |
|-----------|---------|
| `meta` + `cycle_gauge.conclusion` | 渐变网格带上的深海军蓝 Hero 精选卡（期数徽章/标题/本期结论/chips/一句话图景） |
| `indicators` | 数据总览表（红涨绿跌 + 领先/同步/滞后药丸标签，超过 8 行折叠） |
| `signals` | 三组信号三栏卡片 + 一致性 callout（靛蓝左边线） |
| `cpi_ppi` | CPI/PPI 量化传导估算三栏卡（剪刀差/传导时滞/未来CPI） |
| `rate_cascade` | **深海军蓝应用壳面板**：三层状态圆点（发光语义色）+ DR007利差 + NCD + 断流诊断暗卡 |
| `fx` | 汇率三问三栏卡片 + 中美利差 + DXY 展示卡 |
| `cross_validation` | 交叉验证表 + 结论 |
| `cycle_gauge` | **暖奶油色插曲带**上的四维进度条仪表盘（靛蓝渐变填充，偏冷极端转 ruby 渐变）+ 判定结论 |
| `inventory_cycle` | 库存周期定位三栏卡（阶段/PMI分项/工业存货同比） |
| `falsification` | 证伪条件列表 |
| `watchlist` | 下月观察清单列表 |
| `sources` | 页脚数据来源（仅 http/https 链接渲染为可点击超链接，其余按纯文本） |

### 4.3 进度条（cycle_gauge）
- `fill`：0-100 整数，控制进度条宽度百分比。
- `hot=true`：渲染为 ruby→magenta 渐变（偏冷极端）。
- `hot=false`：渲染为靛蓝渐变（正常）。

### 4.4 可读性层（模板自动行为，无需数据端配合）
- **结论前置徽章**：Hero 区自动读取 `cycle_gauge.conclusion` 渲染"本期结论"徽章及通俗含义（进攻/中性/防御 三态映射，深色面板上的高亮语义色）。
- **模块大白话副标题**：每个模块标题下自动显示一句通俗说明；库存周期模块的副标题由 `inventory_cycle.stage` 四阶段动态映射。
- **术语悬停解释**：Vue 渲染完成后自动扫描正文文本，命中内置术语词典（社融/LPR/DR007/被动补库等 30 条）的词自动加虚线下划线，悬停显示通俗解释（纯 CSS tooltip）；触屏设备改为点按切换显示。表格首列（指标名）与链接内文本不做术语包装。词典内置于模板，与数据无关。
- **数据总览折叠**：指标数超过 8 项时，默认只显示前 8 行预览（`v-show` 复用 DOM，折叠状态不丢术语提示），下方显示"展开全部（共 N 项）"按钮；不超过 8 项时直接全部显示。

---

## 5. 提示词输出约束

1. 输出**仅为一个 JSON 对象**，前后不允许有解释性文字或 Markdown 代码围栏（除非用户明确要求对话简版）。
2. 所有字符串使用 UTF-8，不得出现未转义的控制字符。
3. `meta.generated_at` 必须是执行当天日期（YYYY-MM-DD）。
4. `indicators` 数组顺序遵循：领先组 → 同步组 → 滞后组 → 利率/汇率/估值（与手册第3.7节标签表一致）。
5. 每个 `indicator` 必须包含 `direction` 和 `direction_text`，即使方向为 `flat`。
6. `sources` 每条为 `{text, url}` 对象，`text` 必须包含「数据项 + 来源机构 + 发布日期」三要素，`url` 无则填 `null`。
7. `rate_cascade` 必须包含 `dr007_policy_spread` 和 `ncd_rate`（可填 `null`，不可缺字段）。
8. `fx` 必须包含 `us_cn_10y_spread` 和 `dxy`（可填 `null`，不可缺字段）。
9. `cycle_gauge.dimensions[]` 每维必须包含 `percentile` 和 `baseline`。

---

## 6. 自检清单（交付前）

- [ ] JSON 可被 `JSON.parse` 正常解析
- [ ] 通过 `report.schema.json` 校验（无必填缺失、枚举值合法）
- [ ] `indicators` 三值齐全或明确标注缺失
- [ ] `rate_cascade` 含 `dr007_policy_spread` + `ncd_rate`
- [ ] `fx` 含 `us_cn_10y_spread` + `dxy`
- [ ] `cycle_gauge.dimensions[]` 每维含 `percentile` + `baseline`
- [ ] `inventory_cycle` 软硬数据对照完整（PMI 分项 + 工业存货同比）
- [ ] `cpi_ppi` 含传导时滞 + 未来 CPI 推算
- [ ] 所有数值均有对应 `sources` 条目
- [ ] 无精确点位预测话术（铁律第5条）
- [ ] `cycle_gauge.conclusion` ∈ {进攻, 中性, 防御}
