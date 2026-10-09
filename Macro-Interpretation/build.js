// 构建工具：将 data/YYYY-MM.json 注入 report-template.html，生成最终报告 HTML
// 在注入前执行数据契约校验，防止残缺数据生成空壳报告。
const fs = require('fs');
const path = require('path');

const dataFile = process.argv[2] || 'data/2026-09.json';
const outFile = process.argv[3] || '中国宏观月报-2026年9月.html';

const tpl = fs.readFileSync('report-template.html', 'utf8');
const data = fs.readFileSync(dataFile, 'utf8');

// 校验 JSON 可解析
const parsed = JSON.parse(data);

/* ── 数据契约校验（与 report-template.html 的 validateReportData 保持一致） ── */
function validate(d) {
  const REQUIRED_TOP = ['meta','indicators','signals','rate_cascade','fx','cross_validation','cycle_gauge','inventory_cycle','cpi_ppi','falsification','watchlist','sources'];
  const VALID_LABEL = ['lead','coin','lag'];
  const VALID_DIRECTION = ['up','down','flat'];
  const VALID_STATUS = ['ok','no','wait'];
  const VALID_CONCLUSION = ['进攻','中性','防御'];
  const VALID_INV_STAGE = ['主动补库','被动补库','主动去库','被动去库'];
  const VALID_CONF = ['高','中','低'];

  if (!d || typeof d !== 'object') return '数据不是合法对象';
  for (const k of REQUIRED_TOP) if (!(k in d)) return '缺少必填顶层字段：' + k;

  if (!d.meta || typeof d.meta !== 'object') return 'meta 字段无效';
  for (const k of ['title','period','data_cutoff','generated_at','search_count','completeness','missing','hero_quote'])
    if (!(k in d.meta)) return 'meta 缺少字段：' + k;

  if (!Array.isArray(d.indicators) || d.indicators.length === 0) return 'indicators 不能为空数组';
  for (const it of d.indicators) {
    for (const k of ['name','period','published','label']) if (!(k in it)) return 'indicators 条目缺少字段：' + k;
    if (!VALID_LABEL.includes(it.label)) return 'indicators label 非法：' + it.label;
    if (it.direction && !VALID_DIRECTION.includes(it.direction)) return 'indicators direction 非法：' + it.direction;
  }

  for (const k of ['source','wholesale','retail','diagnosis','dr007_policy_spread','ncd_rate'])
    if (!(k in d.rate_cascade)) return 'rate_cascade 缺少字段：' + k;
  for (const k of ['source','wholesale','retail'])
    if (!VALID_STATUS.includes(d.rate_cascade[k].status)) return 'rate_cascade.' + k + '.status 非法';

  for (const k of ['what_moves','why_moves','transmission','calibration','us_cn_10y_spread','dxy'])
    if (!(k in d.fx)) return 'fx 缺少字段：' + k;

  if (!Array.isArray(d.cycle_gauge.dimensions) || d.cycle_gauge.dimensions.length !== 4) return 'cycle_gauge.dimensions 必须为 4 项';
  if (!VALID_CONCLUSION.includes(d.cycle_gauge.conclusion)) return 'cycle_gauge.conclusion 非法：' + d.cycle_gauge.conclusion;
  for (let i = 0; i < d.cycle_gauge.dimensions.length; i++) {
    const dim = d.cycle_gauge.dimensions[i];
    for (const k of ['name','meta','fill','hot','percentile','baseline'])
      if (!(k in dim)) return 'cycle_gauge.dimensions[' + i + '] 缺少字段：' + k;
  }

  for (const k of ['stage','pmi_raw_inventory','pmi_finished_inventory','industrial_inventory_yoy','percentile'])
    if (!(k in d.inventory_cycle)) return 'inventory_cycle 缺少字段：' + k;
  if (!VALID_INV_STAGE.includes(d.inventory_cycle.stage)) return 'inventory_cycle.stage 非法：' + d.inventory_cycle.stage;

  for (const k of ['ppi_yoy','cpi_yoy','core_cpi_yoy','scissors','transmission_lag_quarters','expected_cpi_in_quarters','confidence'])
    if (!(k in d.cpi_ppi)) return 'cpi_ppi 缺少字段：' + k;
  if (!VALID_CONF.includes(d.cpi_ppi.confidence)) return 'cpi_ppi.confidence 非法：' + d.cpi_ppi.confidence;

  return null;
}

const err = validate(parsed);
if (err) {
  console.error('❌ 数据校验未通过：', err);
  process.exit(1);
}
console.log('✅ JSON 校验通过: title=%s, indicators=%d', parsed.meta.title, parsed.indicators.length);

const out = tpl.replace(
  '<script type="application/json" id="report-data">{ }</script>',
  '<script type="application/json" id="report-data">' + data + '</script>'
);

fs.writeFileSync(outFile, out, 'utf8');
console.log('✅ Written:', outFile, 'size:', out.length);
