/* 冒烟测试：在真实页面里跑归一化与解析层。
   由 tests/smoke.py 注入到 index.html 的副本中执行，结果写入 #probe。 */
(function(){
  const out = [];
  const ok = (name, cond, extra) => out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra === undefined ? '' : ' :: ' + extra));
  const near = (a,b) => typeof a === 'number' && Math.abs(a-b) < 1e-9;

  /* ---- 1. 脏数字收口 ---- */
  ok('toNum 百分号', toNum('+3.2%') === 3.2, toNum('+3.2%'));
  ok('toNum 千分位+亿', toNum('1,234.5亿') === 1234.5, toNum('1,234.5亿'));
  ok('toNum N/A', toNum('N/A') === null);
  ok('toNum 全角数字', toNum('１８') === 18, toNum('１８'));

  const dirty = {
    meta: { as_of:'2026-10-30', confidence: 72, data_mode:'live' },
    verdict: { headline:'x', confidence:'86', stance:'offense', points:[] },
    data_quality: { status:'ok', coverage:'71%' },
    mainlines: [{
      name:'测试主线', stage:'发酵期', score:'87.6', penalty:'3.15', stage_confidence:'78%',
      score_breakdown: [
        { key:'rs', normalized:'92', weight:'25', contribution:'23.0' },
        { key:'volume', normalized:'88', weight:'20' }
      ],
      metrics: { chg_20d:'18.32%', turnover_share:'15.2', net_inflow_5_yi:'+84.3亿', max_streak:'4', rs_20: 1.213 },
      persistence: { pulse_check: { rules_passed:'3', detail:'ok' } },
      leaders: [{ name:'A', chg_20d:'32%', turnover_yi:'88.5' }]
    }]
  };
  const n = normalize(dirty);
  const m = n.mainlines[0];
  ok('meta 置信度 72→0.72', near(n.meta.confidence, 0.72), n.meta.confidence);
  ok('verdict 置信度 "86"→0.86', near(n.verdict.confidence, 0.86), n.verdict.confidence);
  ok('字符串分数→数值', near(m.score, 87.6));
  ok('覆盖率 "71%"→0.71', near(n.data_quality.coverage, 0.71), n.data_quality.coverage);
  ok('中文阶段→枚举', m.stage === 'expansion', m.stage);
  ok('阶段置信度 "78%"→0.78', near(m.stage_confidence, 0.78));
  ok('涨幅 "18.32%"→0.1832', near(m.metrics.chg_20d, 0.1832), m.metrics.chg_20d);
  ok('份额 "15.2"→0.152', near(m.metrics.turnover_share, 0.152));
  ok('资金 "+84.3亿"→84.3', near(m.metrics.net_inflow_5_yi, 84.3));
  ok('RS 不按百分数折算', near(m.metrics.rs_20, 1.213));
  ok('缺 contribution 按公式补算', near(m.score_breakdown[1].contribution, 17.6), m.score_breakdown[1].contribution);
  ok('惩罚值保留 3.15', m.penalty === 3.15, m.penalty);
  ok('pulse 由 rules 推导', m.persistence.pulse_check.passed === true);
  ok('stance 旧写法归一', n.verdict.stance === 'offensive', n.verdict.stance);
  ok('有自动修正记录', (n.__fixes || []).length > 5, (n.__fixes||[]).length);
  ok('修正记录不参与序列化', JSON.stringify(n).indexOf('__fixes') < 0);

  /* ---- 2. 缺数据不许伪造结论 ---- */
  const bare = normalize({ mainlines:[{ name:'空壳' }] });
  const b = bare.mainlines[0];
  ok('缺 stage → null', b.stage === null);
  ok('缺 stage → 未判定', b.stage_label === '未判定', b.stage_label);
  ok('缺 score → null', b.score === null);
  ok('缺 penalty → null', b.penalty === null);
  ok('缺 dq.status → null', bare.data_quality.status === null);
  ok('缺 coverage → null', bare.data_quality.coverage === null);
  ok('缺 confidence → null', bare.verdict.confidence === null);
  ok('缺 pulse → 未检验', pulseLabel(b.persistence.pulse_check) === '未检验');
  ok('缺 stage 类名 st-none', stageClass(b.stage) === 'st-none');
  ok('格式化 null 不抛错', fixed(b.score,1) === '—' && pct(bare.data_quality.coverage) === '—');

  /* ---- 3. 解析层 ---- */
  const fenced = '以下是结果：\n```json\n' + JSON.stringify({ mainlines:[{name:'A',score:80},{name:'B',score:70},{name:'C',score:60}] }) + '\n```\n希望对你有帮助';
  const r1 = parseJsonLoose(fenced);
  ok('围栏+前言可解析', !r1.error && r1.data.mainlines.length === 3, r1.error);

  const complete = '{"mainlines":[{"name":"A","score":80},{"name":"B","score":70},{"name":"C","score":60}],"risks":[],"scenarios":[]}';
  const truncated = '{"mainlines":[{"name":"A"},{"name":"B"},{"name":"C"},{"name":"D"},{"name":"E"}],"risks":[{"title":"未写完';
  const r2 = parseJsonLoose(complete + '\n\n' + truncated);
  ok('完整 3 条优先于截断 5 条', !r2.error && r2.data.mainlines.length === 3,
     r2.error + ' count=' + (r2.data ? r2.data.mainlines.length : '-') + ' cut=' + r2.cut);

  const r3 = parseJsonLoose(truncated);
  ok('只剩截断时给出 cut 标记', r3.cut === true && r3.data.mainlines.length === 5, 'cut=' + r3.cut);

  /* ---- 4. 首屏渲染 ---- */
  ok('首屏渲染 5 行主线', document.querySelectorAll('#leaders tbody tr').length === 5,
     document.querySelectorAll('#leaders tbody tr').length);
  ok('模板占位符已替换', document.body.innerText.indexOf('{{') < 0);
  ok('内置样例无需自动修正', (normalize(EMBEDDED.sample).__fixes || []).length === 0);

  /* ---- 5. 契约体检：样例必须自洽 ---- */
  const S = normalize(EMBEDDED.sample);
  const sa = auditReport(S);
  ok('内置样例通过契约体检', sa.total === 0, 'err=' + sa.err + ' warn=' + sa.warn);
  ok('样例阶段全部可复算', S.mainlines.every(m => calcStage(m).stage === m.stage),
     S.mainlines.map(m => m.stage + '/' + calcStage(m).stage).join(' '));
  ok('样例惩罚全部可复算', S.mainlines.every(m => calcPenalty(m).value === m.penalty),
     S.mainlines.map(m => calcPenalty(m).value).join(','));
  ok('样例反脉冲条数一致',
     S.mainlines.every(m => calcPulse(m).filter(x => x.ok === true).length === m.persistence.pulse_check.rules_passed),
     S.mainlines.map(m => calcPulse(m).filter(x => x.ok === true).length).join(','));
  ok('样例 rs 的 raw 等于 metrics.rs_20',
     S.mainlines.every(m => {
       const row = m.score_breakdown.filter(b => b.key === 'rs')[0];
       return row && Math.abs(row.raw - m.metrics.rs_20) < 1e-6;
     }));

  /* ---- 6. 契约体检：脏数据必须被抓住 ---- */
  const ML = (o) => normalize(Object.assign({ mainlines: [o] }, o.__top || {}));
  const badPulse = ML({
    id: 'x', name: '谎报脉冲', score: 80, penalty: 0, stage: 'expansion',
    score_breakdown: ['rs','persistence','volume','breadth','fundflow','narrative'].map((k, i) => ({
      key: k, normalized: 80, weight: [25,20,20,15,10,10][i], contribution: 80 * [25,20,20,15,10,10][i] / 100
    })),
    metrics: { breadth_up_ratio: 0.40, new_high_ratio: 0.02, turnover_share_chg_5: -0.01, turnover_share_pct_250: 0.30 },
    persistence: { days_top10_20: 3, consecutive_top10: 0, pulse_check: { passed: true, rules_passed: 3 } }
  });
  ok('谎报反脉冲被抓出', (auditReport(badPulse).of['x'] || []).some(f => f.level === 'err' && f.text.indexOf('反脉冲实算') >= 0));

  const badPen = ML({
    id: 'y', name: '惩罚不符', penalty: 3.15,
    metrics: { turnover_share_pct_250: 0.5, max_streak: 1, crowding_pct: 0.4, chg_60d: 0.1, net_inflow_5_yi: 5 }
  });
  ok('惩罚与 R1/R2/R3 不符被抓出', (auditReport(badPen).of['y'] || []).some(f => f.text.indexOf('惩罚项实算') >= 0));

  const badGate = ML({
    id: 'g', name: '未过闸门', stage: 'expansion', stage_confidence: 0.7,
    metrics: { breadth_up_ratio: 0.4, new_high_ratio: 0.01, turnover_share_chg_5: -0.02, turnover_share_pct_250: 0.2 },
    persistence: { days_top10_20: 2, consecutive_top10: 0, pulse_check: { passed: false, rules_passed: 0 } }
  });
  ok('未过闸门被抓出', (auditReport(badGate).of['g'] || []).some(f => f.level === 'err' && f.text.indexOf('闸门') >= 0));

  const exempt = normalize({
    mainlines: [{
      id: 'f', name: '退潮主线', stage: 'fade', stage_confidence: 0.5,
      metrics: { breadth_up_ratio: 0.3, new_high_ratio: 0.01, turnover_share_chg_5: -0.02, turnover_share_pct_250: 0.2 },
      persistence: { days_top10_20: 2, consecutive_top10: 0, pulse_check: { passed: false, rules_passed: 0 } }
    }],
    rotation: { outflow: [{ name: '退潮主线', delta_pct: -1.2 }] }
  });
  const exA = auditReport(exempt);
  ok('fade 存量豁免不被判错', !(exA.of['f'] || []).some(f => f.level === 'err') && exA.notes > 0,
     'notes=' + exA.notes);

  const badProb = normalize({
    mainlines: [{ id: 'z', name: 'Z' }],
    scenarios: [{ name: 'a', probability: 0.5 }, { name: 'b', probability: 0.2 }, { name: 'c', probability: 0.2 }]
  });
  ok('情景概率和不为 1 被标出', auditReport(badProb).items.some(f => f.text.indexOf('情景概率合计') >= 0));

  const badRs = normalize({
    mainlines: [{
      id: 'r', name: 'RS 不符', metrics: { rs_20: 1.2 },
      score_breakdown: [{ key: 'rs', raw: 0.2, normalized: 80, weight: 25, contribution: 20 }]
    }]
  });
  ok('rs raw 与 metrics.rs_20 不一致被标出', (auditReport(badRs).of['r'] || []).some(f => f.text.indexOf('metrics.rs_20') >= 0));

  const amb = ML({
    id: 's', name: '双重命中',
    metrics: { chg_5d: 0.02, chg_20d: 0.30, turnover_share_pct_250: 0.92, max_streak_prev: 6,
               max_streak: 3, crowding_pct: 0.95, seal_rate: 0.80 }
  });
  ok('阶段按裁决顺序取更晚的那个', calcStage(amb.mainlines[0]).stage === 'divergence',
     calcStage(amb.mainlines[0]).stage);

  /* ---- 7. 三态与枚举收口 ---- */
  ok('非法 theme_type → null', normalize({ mainlines: [{ name: 'T', theme_type: 'liquidity' }] }).mainlines[0].theme_type === null);
  ok('中文 theme_type → 枚举', normalize({ mainlines: [{ name: 'T', theme_type: '行业型' }] }).mainlines[0].theme_type === 'industry');
  ok('非法 drivers.type → null', normalize({ mainlines: [{ name: 'T', drivers: [{ type: 'xxx' }] }] }).mainlines[0].drivers[0].type === null);
  ok('stance_label 缺失时按枚举回填',
     normalize({ verdict: { stance: 'offensive' } }).verdict.stance_label === '进攻',
     normalize({ verdict: { stance: 'offensive' } }).verdict.stance_label);
  ok('chg 在 1~3 之间折算会标注待复核',
     (normalize({ mainlines: [{ name: 'C', metrics: { chg_60d: 1.05 } }] }).__fixes || [])
       .some(f => f.path.indexOf('chg_60d') >= 0 && f.kind.indexOf('复核') >= 0));

  /* Vue 的 reactive 代理会重包装数组；__fixes 若是只读不可配置属性会触发 Proxy 不变量检查
     并抛 TypeError，整页白屏。这一条守着那个坑。 */
  ok('体检可跑在响应式代理上', (function(){
    try{ auditReport(Vue.reactive(normalize(EMBEDDED.sample))); return true; }
    catch(e){ return false; }
  })());

  const box = document.createElement('div');
  box.id = 'probe';
  box.textContent = out.join('\n');
  document.body.appendChild(box);
  document.title = out.filter(x => x.indexOf('FAIL') === 0).length + ' failed / ' + out.length;
})();
