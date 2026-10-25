"""Markdown 渲染：把报告 dict 渲染为人类可读的研究报告（结论先行）。"""

from __future__ import annotations

from typing import Any, Dict, List


def _tbl(header: List[str], rows: List[List[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def render_markdown(report: Dict[str, Any]) -> str:
    m = report["meta"]
    c = report["conclusion"]
    L: List[str] = []
    stock = m["stock"]

    L.append(f"# 个股研究报告（研究复核）：{stock['name']}（{stock['code']}）\n")
    L.append(f"- 分析时点：**{m['analysis_date']}**（数据截止 {m['data_cutoff']}，框架 v{m['framework_version']}）")
    L.append(f"- 参考价格：{m['price_ref']['close']} {stock['currency']}（{m['price_ref']['as_of']}，{m['price_ref']['source']}）")
    L.append(f"- 主周期：中线 6-12 个月（短线 0-3 个月为参考层）\n")

    # 结论摘要（结论先行）
    L.append("## 一、结论摘要\n")
    L.append(f"**中线分桶：【{c['bucket_mid']}】**（综合分 {c['composite_score_mid']}/10，置信度 {c['confidence']['level']} {c['confidence']['value']}；命中规则 `{c['decision']['rule_id']}`）")
    L.append(f"短线参考：【{c['bucket_short']}】（综合分 {c['composite_score_short']}/10，不改变中线定位）\n")
    L.append(f"> {c['summary']}\n")
    L.append("**分桶判定理由**：" + "；".join(c["decision"]["reasons"]) + "\n")
    L.append("### 维持条件（全部成立才维持当前桶）\n")
    L.append(_tbl(["#", "条件", "指标", "阈值", "来源"],
                  [[x["id"], x["condition"], x["metric"], x["threshold"], x["source"]] for x in c["maintain_conditions"]]))
    L.append("\n### 证伪条件（任一触发 → 立即重估）\n")
    L.append(_tbl(["#", "条件", "指标", "阈值", "来源"],
                  [[x["id"], x["condition"], x["metric"], x["threshold"], x["source"]] for x in c["falsification"]]))
    L.append(f"\n有效期至 {c['valid_until']}；复核节奏：{c['review_cadence']}\n")

    # 评分卡
    L.append("## 二、多维评分卡（0-10，逐层可拆解）\n")
    sc = report["scorecard"]
    L.append(_tbl(["维度", "中线权重", "短线权重", "得分", "置信度"],
                  [[d["name"] + f"({d['id']})", sc["weights"]["mid_term"][d["id"]],
                    sc["weights"]["short_term"][d["id"]],
                    d["score_mid"] if d["score_mid"] is not None else "未评分",
                    f"{d['confidence']['level']}({d['confidence']['value']})"] for d in sc["dimensions"]]))
    L.append("")
    for d in sc["dimensions"]:
        L.append(f"### {d['name']}（{d['score_mid']}）\n")
        for f in d["factors"]:
            rn = "（指标缺失已重归一化）" if f["renormalized"] else ""
            L.append(f"**因子：{f['name']}**（权重 {f['weight']}{rn}）\n")
            L.append(_tbl(
                ["指标", "原始值", "命中量规带", "得分", "权重", "来源/时点"],
                [[i["name"], i["value"] if i["value"] is not None else "缺失",
                  i["band"] if i["band"] else "—", i["score"] if i["score"] is not None else "未评分",
                  i["weight"], f"{i['evidence']['source_tier']} {i['evidence']['available_at']}"] for i in f["indicators"]]))
            L.append("")

    # 估值
    v = report["valuation"]
    L.append("## 三、估值（两法区间，非价格点位预测）\n")
    dcf = v["dcf"]
    if not dcf.get("applicable", True):
        gate = v.get("gate", {})
        L.append(f"**本报告不出估值区间。** 前门判定：`{gate.get('code')}`（{gate.get('type_label')}）——{gate.get('reason')}\n")
        rel = v["relative"]
        # 不能直接引用 relative 的 narrative：它默认 DCF 还在算（"结论仅依赖 DCF 单法"），
        # 而这里 DCF 恰恰不适用——两句话会互相打脸。
        L.append(f"相对估值口径 {rel['metric']}："
                 f"{'价位折算可用' if rel.get('available') else '价位折算不可用'}"
                 f"（分位本身仍用于定位，见评分卡「相对位置」）。\n")
        syn = v["synthesis"]
        L.append(f"### 合成\n- {syn['note']}\n")
    else:
        L.append("### 方法一：FCFF DCF（假设均为 assumption）\n")
        L.append(_tbl(["情景", "WACC", "永续g", "每股价值低/中枢/高", "终值占比"],
                      [[k.upper(), f"{s['wacc']:.1%}", f"{s['terminal_growth']:.1%}",
                        f"{s['value_per_share_low']} / {s['value_per_share_central']} / {s['value_per_share_high']}",
                        f"{s['terminal_value_share']:.0%}"] for k, s in dcf["scenario_results"].items()]))
        if dcf["warnings"]:
            L.append("\n⚠️ " + "\n\n⚠️ ".join(dcf["warnings"]))
        g = dcf["sensitivity_grid_base"]
        L.append("\n基准情景敏感性（每股价值，行=WACC，列=永续g）：\n")
        L.append(_tbl(["WACC\\g"] + [f"{x:.2%}" for x in g["g_values"]],
                      [[f"{w:.2%}"] + [f"{val}" for val in row] for w, row in zip(g["wacc_values"], g["values"])]))
        rel = v["relative"]
        anchor = rel["peer_anchor"]
        peer_txt = (f"同业锚 {anchor['peer_median']} 倍（{anchor['note']}）"
                    if anchor.get("peer_median") is not None else f"同业锚：{anchor['note']}")
        L.append(f"\n### 方法二：相对估值（{rel['metric']} 分位带）\n")
        L.append(f"当前 {rel['current']} 倍，十年分位 {rel['percentile_10y']}%（{rel['band']}）；"
                 f"隐含区间 {rel['implied_price_range']['low']}-{rel['implied_price_range']['high']} 元"
                 f"（EPS {rel['implied_price_range']['eps_kind']}）；{peer_txt}\n")
        L.append(f"> {rel['priced_in_narrative']}\n")
        syn = v["synthesis"]
        L.append(f"### 合成\n- DCF 基准区间 [{syn['dcf_base_range']['low']}, {syn['dcf_base_range']['high']}]；"
                 f"相对法区间 [{syn['relative_range']['low']}, {syn['relative_range']['high']}]")
        if syn.get("margin_basis") == "none":
            # 两法无重叠：不给合成安全边际，改给两个单法数与分歧度。
            L.append(f"- 两法中枢相差 {syn['divergence_pct']:+.0f}%（{syn['mode']}）："
                     "并集中点没有经济含义，因此**不出合成安全边际**；"
                     f"DCF 单法 {syn['dcf_margin_pct']}%，相对法 {syn['relative_margin_pct']}%")
        else:
            margin = (f"{syn['safety_margin_pct']:+.1f}%" if syn["safety_margin_pct"] is not None else "不可计算")
            L.append(f"- 合成区间 [{syn['combined_range']['low']}, {syn['combined_range']['high']}]（{syn['mode']}），"
                     f"安全边际 {margin}，置信 {syn['confidence_level']}")
        L.append(f"- {syn['note']}\n")

    # 情景
    L.append("## 四、情景分析\n")
    if report["scenarios"]:
        L.append(_tbl(["情景", "概率", "每股价值区间", "vs 基准", "关键假设", "该情景失效条件"],
                      [[s["name"], f"{s['probability']:.0%}",
                        f"[{s['value_per_share_range']['low']}, {s['value_per_share_range']['high']}]",
                        s["delta_vs_base"], "；".join(s["key_assumptions"][:2]), s["falsification"]]
                       for s in report["scenarios"]]))
    else:
        L.append("_未生成情景价位：估值方法对该公司类型不适用（见第三节前门判定）。_")
    L.append("")

    # 风险矩阵
    rm = report["risk_matrix"]
    L.append("## 五、风险矩阵（5×5 概率×影响）\n")
    L.append(_tbl(["ID", "风险", "类别", "P", "I", "P×I", "级别", "触发器"],
                  [[r["id"], r["name"], r["category"], r["probability"], r["impact"], r["score"], r["level"],
                    f"{r['trigger']['metric']} {r['trigger']['threshold']}"] for r in rm["risks"]]))
    L.append(f"\nTop 风险：{'、'.join(rm['top_risk_ids'])}\n")

    # 催化剂日历
    L.append("## 六、催化剂日历\n")
    L.append(_tbl(["日期/窗口", "事件", "类型", "方向", "强度", "定价状态", "核验方式", "警示"],
                  [[x["date"], x["event"], x["type"], x["direction"], x["strength"], x["priced_in"],
                    x["verify_method"], x["warning"] or "—"] for x in report["catalyst_calendar"]]))
    L.append("")

    # 跟踪清单
    L.append("## 七、跟踪清单\n")
    L.append(_tbl(["#", "事项", "指标", "阈值", "方向", "频率", "触发动作", "来源", "来源类型"],
                  [[t["id"], t["item"], t["metric"], t["threshold"], t["direction"], t["cadence"],
                    t["action_on_trigger"], t["source"], t["origin"]] for t in report["tracking_checklist"]]))
    L.append("")

    # 数据质量
    dq = report["data_quality"]
    L.append("## 八、数据质量说明\n")
    L.append(f"- DQ 总分 {dq['overall']}（完整性 {dq['completeness']} / 及时性 {dq['timeliness']} / 来源等级 {dq['source_tier_score']}，最低入模等级 {dq['min_tier_used']}）")
    tier_txt = "、".join(f"{k}×{v}" for k, v in dq["tier_distribution"].items() if v)
    L.append(f"- 来源分布：{tier_txt or '—'}；强制人工复核：{'是' if dq['forced_manual_review'] else '否'}")
    if dq["gaps"]:
        L.append("- 缺口：" + "；".join(f"{x['field']}（{x['reason']}）" for x in dq["gaps"][:10]) + ("..." if len(dq["gaps"]) > 10 else ""))
    if dq["notes"]:
        L.append("- 备注：" + "；".join(dq["notes"]))
    L.append("")

    # 证据日志（摘要）
    L.append("## 九、证据日志（数据时点与来源，节选）\n")
    L.append(_tbl(["字段", "来源", "等级", "as_of", "available_at", "被用于"],
                  [[e["field_id"], e["source"], e["source_tier"], e["as_of"], e["available_at"],
                    ", ".join(e["used_by"][:4]) + ("..." if len(e["used_by"]) > 4 else "")]
                   for e in report["evidence_log"]]))
    L.append("")

    L.append("---\n")
    L.append(f"> {report['disclaimer']}\n")
    return "\n".join(L)
