"""验收脚本：一键跑完 docs/03_acceptance.md 中的机检项。

用法：
    PYTHONPATH=src python tools/acceptance.py

返回码 0 = 全部通过；1 = 有失败项（逐条打印失败原因）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stockframe.datadict import FIELD_SPECS  # noqa: E402
from stockframe.engine import run  # noqa: E402
from stockframe.pit import LookAheadError, ProvenanceError  # noqa: E402
from stockframe.specs import DIMENSIONS  # noqa: E402
from stockframe.validate import validate_report  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_autobuild import raw_fixture  # noqa: E402  （离线夹具，不访问网络）

BANNED = ("买入", "卖出", "加仓", "减仓", "目标价", "止损", "止盈", "建仓", "清仓")

results: list[tuple[str, str, bool, str]] = []


def check(cid: str, title: str, fn) -> None:
    try:
        detail = fn() or ""
        results.append((cid, title, True, detail))
    except AssertionError as exc:
        results.append((cid, title, False, str(exc)))
    except Exception as exc:  # noqa: BLE001
        results.append((cid, title, False, f"{type(exc).__name__}: {exc}"))


def load_sample() -> dict:
    with open(ROOT / "examples" / "sample_input.json", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 机检项

def a1_unit_tests() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert proc.returncode == 0, "单元测试未通过：\n" + proc.stdout[-2000:]
    return proc.stdout.strip().splitlines()[-1]


def a2_pipeline_and_schema() -> str:
    report = run(load_sample())
    ok, errors = validate_report(report)
    assert ok, "schema 校验失败：" + "; ".join(errors[:5])
    return f"schema 校验通过，risk={len(report['risk_matrix']['risks'])} 条，维度={len(report['scorecard']['dimensions'])}"


def a3_deterministic_rerun() -> str:
    r1 = run(load_sample())
    r2 = run(load_sample())
    assert json.dumps(r1, sort_keys=True, ensure_ascii=False) == json.dumps(r2, sort_keys=True, ensure_ascii=False), \
        "同一输入两次运行结果不一致（存在非确定性）"
    with open(ROOT / "examples" / "sample_report.json", encoding="utf-8") as fh:
        committed = json.load(fh)
    assert json.dumps(committed, sort_keys=True, ensure_ascii=False) == json.dumps(r1, sort_keys=True, ensure_ascii=False), \
        "样例报告与代码输出不一致：请重跑 CLI 更新 examples/sample_report.json"
    return "重复运行一致，且与仓库内样例报告一致"


def a4_no_banned_language() -> str:
    report = run(load_sample())
    text = json.dumps(report, ensure_ascii=False)
    hits = [w for w in BANNED if w in text]
    assert not hits, f"报告出现违禁措辞：{hits}"
    md_path = ROOT / "examples" / "sample_report.md"
    if md_path.exists():
        md = md_path.read_text(encoding="utf-8")
        md_hits = [w for w in BANNED if w in md]
        assert not md_hits, f"Markdown 报告出现违禁措辞：{md_hits}"
    return f"JSON/Markdown 均无 {len(BANNED)} 类违禁措辞"


def a5_pit_guard() -> str:
    inp = load_sample()
    inp["data"]["financial"]["roe_ttm"]["available_at"] = "2099-01-01"
    try:
        run(inp)
        raise AssertionError("未来函数未被拦截：available_at > analysis_date 仍可出报告")
    except LookAheadError:
        pass
    # 来源纪律：L4 不得进入评分路径
    inp_l4 = load_sample()
    inp_l4["data"]["financial"]["roe_ttm"]["source_tier"] = "L4"
    try:
        run(inp_l4)
        raise AssertionError("L4 来源字段进入了评分路径")
    except ProvenanceError:
        pass
    # 报告内全部证据时点必须 <= 分析时点
    report = run(load_sample())
    ad = report["meta"]["analysis_date"]
    bad = [e for e in report["evidence_log"] if e["available_at"] and e["available_at"] > ad]
    assert not bad, f"evidence_log 存在晚于分析时点的字段：{[b['field_id'] for b in bad]}"
    bad_inds = []
    for d in report["scorecard"]["dimensions"]:
        for f in d["factors"]:
            for i in f["indicators"]:
                if not i["missing"] and i["evidence"]["available_at"] > ad:
                    bad_inds.append(i["id"])
    assert not bad_inds, f"评分证据存在未来时点：{bad_inds}"
    return f"未来函数/ L4 来源均被拦截；{len(report['evidence_log'])} 条证据全部 <= {ad}"


def a6_traceability() -> str:
    report = run(load_sample())
    missing_ev, no_rationale = [], []
    for d in report["scorecard"]["dimensions"]:
        for f in d["factors"]:
            for i in f["indicators"]:
                if not i["rationale"] or len(i["rationale"]) < 10:
                    no_rationale.append(i["id"])
                if i["missing"]:
                    continue
                ev = i["evidence"]
                if not ev.get("source") or not ev.get("as_of") or not ev.get("available_at"):
                    missing_ev.append(i["id"])
                if i["score"] is None or i["band"] is None:
                    missing_ev.append(i["id"] + "(缺分或档)")
    assert not no_rationale, f"指标缺 rationale：{no_rationale}"
    assert not missing_ev, f"可用指标证据链不完整：{missing_ev}"
    total = sum(len(f["indicators"]) for d in report["scorecard"]["dimensions"] for f in d["factors"])
    return f"{total} 个指标全部带 rationale 与证据四要素"


def a7_anti_piling() -> str:
    counts = []
    for d in DIMENSIONS:
        assert len(d.factors) <= 3, f"{d.id} 因子数 {len(d.factors)} > 3（指标堆砌）"
        for f in d.factors:
            assert len(f.indicators) <= 4, f"{d.id}.{f.id} 指标数 {len(f.indicators)} > 4（指标堆砌）"
            for i in f.indicators:
                if i.anchors is None and i.computed is None:
                    assert len(i.bands) >= 4, f"{i.id} 量规仅 {len(i.bands)} 档（无法区分中间态）"
                counts.append(i.id)
    return f"{len(DIMENSIONS)} 维度 / {len(counts)} 指标，均在反堆砌上限内"


def a8_conditional_conclusion() -> str:
    report = run(load_sample())
    c = report["conclusion"]
    assert c["bucket_mid"] in ("入池", "观察", "回避", "人工复核"), "分桶不在四值域内"
    assert c["decision"]["rule_id"].startswith("R"), "缺决策规则编号（不可溯源）"
    assert c["decision"]["reasons"], "缺分桶理由"
    assert len(c["maintain_conditions"]) >= 3, "维持条件少于 3 条"
    assert len(c["falsification"]) >= 3, "证伪条件少于 3 条"
    for item in c["maintain_conditions"] + c["falsification"]:
        assert item["metric"] and item["threshold"], f"条件 {item['id']} 缺可观测指标/阈值"
    assert c["valid_until"], "缺结论有效期"
    return f"维持 {len(c['maintain_conditions'])} 条 / 证伪 {len(c['falsification'])} 条，均含指标+阈值"


def a9_decision_gates() -> str:
    base = load_sample()

    def bucket(mutator) -> str:
        inp = copy.deepcopy(base)
        mutator(inp)
        return run(inp)["conclusion"]["bucket_mid"]

    assert bucket(lambda i: i["meta"]["flags"].__setitem__("st", True)) == "回避", "ST 未触发回避"
    assert bucket(lambda i: i["data"]["market"].pop("pe_ttm")) == "人工复核", "必填缺失未触发人工复核"
    assert bucket(lambda i: i["meta"].__setitem__("flags", {"restructuring": True})) == "人工复核", "重组未触发人工复核"
    return "硬红旗→回避；必填缺失→人工复核；特殊状态→人工复核"


def a10_probability_gate() -> str:
    inp = load_sample()
    inp["assumptions"]["scenario_probabilities"]["optimistic"] = 0.5
    try:
        run(inp)
        raise AssertionError("三情景概率合计≠1 仍出报告")
    except ValueError as exc:
        assert "概率" in str(exc)
    return "概率不自洽被拒绝出报告"


def a11_doc_code_consistency() -> str:
    sys.path.insert(0, str(ROOT / "tools"))
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_scoring_doc.py")],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert proc.returncode == 0, "评分说明生成失败：" + proc.stderr[-800:]
    doc = (ROOT / "docs" / "02_scoring_guide.md").read_text(encoding="utf-8")
    for d in DIMENSIONS:
        for f in d.factors:
            for i in f.indicators:
                assert i.id in doc, f"评分说明缺少指标 {i.id}（文档/代码漂移）"
    return f"文档含全部 {sum(len(f.indicators) for d in DIMENSIONS for f in d.factors)} 个指标量规"


def a12_data_dictionary_complete() -> str:
    report = run(load_sample())
    ids = {s.field_id for s in FIELD_SPECS}
    unknown = [e["field_id"] for e in report["evidence_log"] if e["field_id"] not in ids]
    assert not unknown, f"evidence_log 出现未登记字段：{unknown}"
    return f"数据字典 {len(ids)} 字段，evidence_log {len(report['evidence_log'])} 条全部登记"


def a13_ui_synced() -> str:
    """界面产物必须与样例报告同步，防止"界面展示过期结论"。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_ui.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-800:]
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "/*__SNAPSHOT__*/" not in html, "模板占位符未替换"
    assert "window.__SNAPSHOT__ =" in html, "缺少内联快照，离线打开会空白"
    assert "fetch(\"/api/analyze\"" in html or "fetch('/api/analyze'" in html, "界面未接实时分析接口"
    assert "byGroup" in html and 'data-group="' in html, \
        "缺口面板未按维度归并：同一维度（如公司判断）会并排出现两张同名卡"
    return f"web/index.html 与样例报告同步（{len(html) / 1024:.0f} KB）"


def a14_api_parity() -> str:
    """服务端薄壳不复制逻辑：接口结果必须等于引擎直算结果，异常输入必须可读降级。"""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from stockframe.server import Handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(payload):
        req = urllib.request.Request(
            base + "/api/analyze", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        status, data = post({"input": load_sample()})
        assert status == 200 and data["ok"], f"正常输入失败：{data}"
        direct = run(load_sample())["conclusion"]
        got = data["report"]["conclusion"]
        assert (got["bucket_mid"], got["composite_score_mid"]) == \
               (direct["bucket_mid"], direct["composite_score_mid"]), "接口与引擎结果不一致（出现两套口径）"
        assert data["validation"]["valid"], "接口返回的报告未通过 schema 校验"

        bad = load_sample()
        bad["data"]["financial"]["roe_ttm"]["available_at"] = "2099-01-01"
        status, data = post({"input": bad})
        assert status == 400 and data["error"]["code"] == "LookAheadError", "接口未把未来函数转为 400 可读错误"
        with urllib.request.urlopen(base + "/") as resp:
            assert "stockframe" in resp.read().decode("utf-8")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return "接口=引擎（同分桶同分），未来函数→400，页面可服务"


def a15_autofetch_pit() -> str:
    """自动取数同样受 PIT 约束：未披露的报告不得使用。"""
    from stockframe import autobuild

    raw = raw_fixture()
    # 2026-08-10 时点，中报（2026-08-15 披露）尚未公告
    out = autobuild.build(raw, "2026-08-10")
    asof = out["fill"]["as_of"]
    assert asof["latest_report_period"] == "2026-03-31", f"越权使用了未披露报告：{asof}"
    assert asof["latest_report_notice"] <= "2026-08-10", asof

    # 全部字段的 available_at 不得晚于分析时点
    late = []
    for group, fields in out["input"]["data"].items():
        for key, val in fields.items():
            if isinstance(val, dict) and val.get("available_at", "") > "2026-08-10":
                late.append(f"{group}.{key}={val['available_at']}")
    assert not late, f"存在未来字段：{late[:5]}"

    # 时点前无已披露报告 → 必须报错，而不是"用最近的"
    blank = raw_fixture()
    for r in blank["reports"]:
        r["notice_date"] = "2027-06-01"
    try:
        autobuild.build(blank, "2026-10-23")
        raise AssertionError("无已披露报告时仍生成了报告（PIT 失效）")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "NO_PIT_DATA", exc
    return f"2026-08-10 时点回退至 {asof['latest_report_period']}（披露 {asof['latest_report_notice']}）；无披露数据时报 NO_PIT_DATA"


def a16_coverage_gate() -> str:
    """证据覆盖不足不得给正式分桶；补齐判断后必须能恢复。"""
    from stockframe import autobuild

    out = autobuild.build(raw_fixture(), "2026-10-23")
    report = run(out["input"])
    cov = report["scorecard"]["evidence_coverage"]
    assert cov["ratio"] < cov["threshold"], f"夹具应低于阈值：{cov}"
    assert report["conclusion"]["decision"]["rule_id"] == "R7_COVERAGE", report["conclusion"]["decision"]
    assert report["conclusion"]["bucket_mid"] == "人工复核"

    inp = copy.deepcopy(out["input"])
    fill = {
        "macro": {"m1_yoy": 4.0, "rate_10y_chg_yoy": -10, "pmi": 50.2, "policy_stance": 3},
        "industry": {"industry_growth": 6.0, "price_trend": 1, "stage": 4,
                     "concentration": 4, "penetration_space": 3},
        "company": {"moat": 4, "mgmt_record": 4, "logic_strength": 3, "logic_essence": "测试",
                    "pledge_pct": 0.0, "customer_concentration": 10.0},
    }
    for group, fields in fill.items():
        inp["data"].setdefault(group, {})
        for key, val in fields.items():
            inp["data"][group][key] = {"value": val, "as_of": "2026-10-23",
                                       "available_at": "2026-10-23",
                                       "source": "验收夹具", "source_tier": "L3"}
    after = run(inp)
    assert after["scorecard"]["evidence_coverage"]["ratio"] >= cov["threshold"]
    assert after["conclusion"]["decision"]["rule_id"] != "R7_COVERAGE", "补齐判断后仍被判覆盖不足"
    return (f"判断层覆盖 {cov['ratio']:.0%} < {cov['threshold']:.0%} → R7_COVERAGE（全维度 {cov['total_ratio']:.0%} 仅供参考）；"
            f"补齐三层判断后覆盖 {after['scorecard']['evidence_coverage']['ratio']:.0%} → {after['conclusion']['decision']['rule_id']}")


def a17_infeasible_returns_partial() -> str:
    """自动假设跑不出正股权价值时：必须明确拒绝，且把可编辑的半成品交还给使用者。

    最坏的做法是"调参直到能出数"——那会得到一条看似成立、实则被参数操纵的估值区间。
    次坏的做法是只报错不给料——使用者对着错误无从下手。这两者都在本项被排除。
    """
    from stockframe import autobuild
    from stockframe.sources import SourceError

    raw = raw_fixture()
    raw["balance"]["short_loan"] = 2.0e13  # 净债务远高于机械 FCFF 现值

    try:
        autobuild.build(raw, "2026-10-23")
        raise AssertionError("DCF 股权价值为负时仍生成了完整分析（应拒绝而非硬凑）")
    except SourceError as exc:
        assert exc.code == "DCF_INFEASIBLE", f"错误码不对：{exc.code}"
        assert "调参硬凑" in str(exc), "拒绝理由未告知使用者该怎么做"
        code = exc.code
        payload = getattr(exc, "payload", {})
        assert payload.get("input") and payload.get("fill"), "未交还半成品输入/缺口清单"
        assert payload["fill"]["filled_count"] > 10, payload["fill"]["filled_count"]

    # 接口层必须把半成品带出去（否则界面拿不到可编辑的输入）
    assert _partial_payload_shape_ok(payload), "payload 结构不完整"

    # 反向验证：假设可修正 → 正常分析应当恢复（说明拒绝是"这一组假设不行"，不是"这只票打死"）
    fixed = copy.deepcopy(payload["input"])
    for sc in fixed["assumptions"]["dcf"].values():
        sc["net_debt"] = -50.0  # 净现金
        sc["ebit_margin_path"] = [0.20] * 5
    report = run(fixed)
    assert report["conclusion"]["bucket_mid"], "修正假设后仍未产出结论"
    return (f"净债务过高 → {code}（不调参）；半成品含 {payload['fill']['filled_count']}/"
            f"{payload['fill']['total_fields']} 字段；修正 DCF 假设后可正常出报告（{report['conclusion']['bucket_mid']}）")


def a18_proxy_traceable_and_overridable() -> str:
    """判断层代理：每个都必须有公式、有误差方向、在证据链里标 proxy，且能被人工结论覆盖。

    这条是"禁止黑箱评分"在自动模式下的落点：代理值不是研究结论，
    既要可复算，也要能被一眼认出来，还不能顶替人工判断。
    """
    from stockframe import autobuild

    raw = raw_fixture()
    raw["macro"] = {
        "m1": {"value": 3.2, "period": "2026-09", "period_label": "2026年09月份",
               "available_at": "2026-10-15", "lag_days_assumed": 15},
        "pmi": {"value": 50.1, "period": "2026-09", "period_label": "2026年09月份",
                "available_at": "2026-10-02", "lag_days_assumed": 2},
    }
    raw["industry"] = {
        "code": "1277", "name": "白酒Ⅱ", "period": "2026-06-30", "deadline": "2026-08-31",
        "members": 11, "samples": 11, "rev_yoy_med": -15.34, "rev_yoy_prev_med": -11.24,
        "gm_med": 73.35, "gm_yoy_med": -1.67, "gm_samples": 11,
        "company_revenue": 9.23e10, "total_revenue": 1.80e11, "company_share": 0.514,
    }
    raw["pledge"] = {"ratio": 0.06, "trade_date": "2026-10-16", "pay_year": "2026-10",
                     "industry": "白酒Ⅱ", "industry_code": "1277"}
    raw["dividends"] = [{"report_date": f"{y}-12-31", "notice_date": f"{y + 1}-06-20",
                         "status": "实施分配", "cash_per_10": 200.0, "profile": "10派200元"}
                        for y in (2021, 2022, 2023, 2024, 2025)]

    out = autobuild.build(raw, "2026-10-23")
    proxied = out["fill"]["proxied"]
    assert proxied["proxy"], "判断层可代理项一个都没生成"
    assert proxied["observed"], "M1/PMI/质押属直接观测，应与代理分开统计"

    deriv = {d["field_id"]: d for d in out["fill"]["derivations"]}
    for fid in proxied["proxy"]:
        assert deriv[fid]["method"] == "proxy", f"{fid} 未标注为 proxy"
        assert len(deriv[fid]["formula"]) > 10, f"{fid} 缺可复算公式"
        assert deriv[fid]["note"], f"{fid} 缺误差方向说明"
        grp, key = fid.split(".", 1)
        assert out["input"]["data"][grp][key]["proxy"] is True, f"{fid} 字段未打 proxy 标记"

    rep = run(out["input"])
    log = {r["field_id"]: r for r in rep["evidence_log"]}
    assert all(log[f].get("proxy") is True for f in proxied["proxy"]), "证据链未标出代理字段"
    assert not log["macro.m1_yoy"].get("proxy"), "直接观测值不应被标成代理"

    cov_auto = rep["scorecard"]["evidence_coverage"]["ratio"]

    # 人工结论必须能覆盖代理，且覆盖后 proxy 标记消失（它现在是研究结论）
    out2 = autobuild.build(raw, "2026-10-23", overrides={"data": {"company": {"moat": {
        "value": 1, "as_of": "2026-10-23", "available_at": "2026-10-23",
        "source": "人工研究判断", "source_tier": "L3"}}}})
    moat = out2["input"]["data"]["company"]["moat"]
    assert moat["value"] == 1 and not moat.get("proxy"), "人工判断未能覆盖代理"
    rep2 = run(out2["input"])
    log2 = {r["field_id"]: r for r in rep2["evidence_log"]}
    assert not log2["company.moat"].get("proxy")

    return (f"代理 {len(proxied['proxy'])} 项全部带公式/误差方向/证据链标记，"
            f"观测 {len(proxied['observed'])} 项不标代理；判断层覆盖 {cov_auto:.0%}；"
            f"人工可覆盖且覆盖后 proxy 标记消失")


def a19_field_ledger_reconciles() -> str:
    """字段清单必须能对账：已填 + 待补 = 数据字典字段数，且互斥无重复。

    取数降级（行情主机限流丢 K 线）时，丢掉的字段若既不进 filled 也不进 pending，
    界面上 "N/39 + 待补" 就对不上，缺口会被藏起来——这属于"数据质量说明"失真。
    同时验证字典外字段（如总市值）不混进账本。
    """
    from stockframe import autobuild
    from stockframe.datadict import FIELD_REGISTRY

    total = len(list(FIELD_REGISTRY))
    rows = []
    for name, mutate in (
        ("完整", lambda r: r),
        ("缺K线", lambda r: {k: v for k, v in r.items() if k != "kline"}),
        ("缺宏观", lambda r: {**r, "macro": None}),
    ):
        f = autobuild.build(mutate(raw_fixture()), "2026-10-23")["fill"]
        pend = [x for g in f["pending"] for x in g["fields"]]
        assert not set(f["filled"]) & set(pend), f"{name}: 同一字段既已填又待补"
        assert len(pend) == len(set(pend)), f"{name}: 待补清单有重复"
        assert set(f["filled"]) <= set(FIELD_REGISTRY), f"{name}: 账本里出现字典外字段"
        assert f["filled_count"] + len(pend) == f["total_fields"] == total, \
            f"{name}: 对账不平（{f['filled_count']} + {len(pend)} ≠ {f['total_fields']}）"
        rows.append(f"{name} {f['filled_count']}+{len(pend)}")

    # 缺 K 线时，交易面字段必须显式落到 kind=source 的待补分组，而不是凭空消失
    f = autobuild.build({k: v for k, v in raw_fixture().items() if k != "kline"}, "2026-10-23")["fill"]
    source_gaps = {x for g in f["pending"] if g["kind"] == "source" for x in g["fields"]}
    assert {"market.turnover_pct", "market.vol_ratio_20d"} <= source_gaps, "取数失败字段未进待补清单"

    return f"{total} 字段在 {len(rows)} 种取数情形下均可对账（{'、'.join(rows)}）；取数降级字段显式列为 source 缺口"


def a20_fallback_traceable_and_peer_anchor_gated() -> str:
    """兜底源必须留痕；同业锚不可比时不得折算成单点价位。

    两条都是"不许装作有数据"的延伸：
    - 数据来自兜底源（新浪）时，字段来源必须写出来，不能仍标东财；
    - 同业中位数只在 0.5–2 倍可比区间内才折价，否则等于用一个不同生意的锚定价格。
    """
    from stockframe import autobuild
    from stockframe.valuation import run_relative

    # 1) K 线来自新浪 → 来源留痕、换手率用流通股本现算
    raw = raw_fixture()
    raw["kline"] = [{**k, "vendor": "sina", "turnover": None} for k in raw["kline"]]
    raw["valuation_hist"][-1]["free_shares"] = 4.0e8
    out = autobuild.build(raw, "2026-10-23")
    turn = out["input"]["data"]["market"].get("turnover_pct", {})
    assert "新浪" in turn.get("source", ""), f"兜底源未留痕：{turn.get('source')}"
    deriv = {x["field_id"]: x for x in out["fill"]["derivations"]}
    assert "流通 A 股" in deriv["market.turnover_pct"]["formula"], "换手率分母必须是流通股"

    # 2) 同业锚：可比时给价位，不可比时必须为 null
    near = run_relative("PE", current=28.5, pctile=42, hist_p25=22, hist_p75=35,
                        eps_ttm=1.14, peer_median=31, fwd_growth_pct=None)["peer_anchor"]
    assert near["comparable"] and near["implied_price"], "可比区间内应给出折算价位"
    far = run_relative("PE", current=11.6, pctile=0, hist_p25=22, hist_p75=35,
                       eps_ttm=1.14, peer_median=31.75, fwd_growth_pct=None)["peer_anchor"]
    assert not far["comparable"] and far["implied_price"] is None, "倍数不可比却折算了价位"

    return (f"兜底源留痕（{turn.get('source', '')[:24]}…）、换手率按流通股计算；"
            f"同业锚 28.5/31 倍可比→{near['implied_price']}，11.6/31.8 倍不可比→不给点位")


def a21_rate_10y_pit_and_no_extrapolation() -> str:
    """10Y 利率同比必须走中债日频序列：只看已发布的点、缺一年前锚点就留空。

    三个容易翻车的点各钉一条：
    - 当日曲线是日终估值，asof 当天不可用（宁可晚一天，也不能用未来数据）；
    - 一年前锚点找不到时必须留空，不能拿半年前的点冒充"一年前"；
    - 时间戳按北京时间解析，否则整条序列错一天。
    """
    from datetime import date, datetime, timedelta, timezone

    from stockframe import autobuild
    from stockframe.sources.chinabond import ChinaBondProvider

    cn = timezone(timedelta(hours=8))
    ms = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=cn).timestamp() * 1000)

    def serve(points):
        series = [[ms(d), v] for d, v in points]
        return lambda url: [{"ycDefName": "10年", "worktime": "2026-10-22",
                             "seriesData": series}]

    pts = [(date(2025, 10, 22), 1.8293), (date(2026, 10, 21), 1.6412),
           (date(2026, 10, 22), 1.6414)]
    p = ChinaBondProvider(http=serve(pts))

    # 1) 时点：asof=10-22 只能看到 10-21（10-22 的曲线次日才可用）
    d22 = p.yield_10y("2026-10-22")
    assert d22["date"] == "2026-10-21", f"用了当日尚未发布的曲线：{d22['date']}"
    assert d22["available_at"] > d22["date"], "available_at 必须晚于数据日"
    d23 = p.yield_10y("2026-10-23")
    assert d23["date"] == "2026-10-22" and d23["value"] == 1.6414, "次日应能看到最新曲线"

    # 2) 同比可复算，且一年前锚点确实来自一年前
    assert abs(d23["chg_bp"] - (d23["value"] - d23["prev_value"]) * 100) < 0.05, "chg_bp 与两点之差不等"
    assert d23["prev_date"] == "2025-10-22", f"一年前锚点错位：{d23['prev_date']}"

    # 3) 缺锚点 → 留空，不许插值
    lonely = ChinaBondProvider(http=serve([(date(2025, 3, 3), 1.9), (date(2026, 10, 21), 1.64)]))
    assert lonely.yield_10y("2026-10-23") is None, "缺一年前锚点却给了值（插值/外推）"

    # 4) 落到报告：来源为中债、归为观测值而非代理
    raw = raw_fixture()
    raw["rate_10y"] = d23
    out = autobuild.build(raw, "2026-10-23")
    assert out["input"]["data"]["macro"]["rate_10y_chg_yoy"]["value"] == d23["chg_bp"]
    assert "中债" in out["input"]["data"]["macro"]["rate_10y_chg_yoy"]["source"]
    assert "macro.rate_10y_chg_yoy" not in out["fill"]["proxied"]["proxy"], "观测值不得标为代理"

    return (f"10-22 时点只见 10-21（次日可用）；同比 {d23['chg_bp']:+.1f}bp 可复算；"
            f"缺锚点返回空不插值；报告中来源为中债、性质为观测值")


def a22_customer_annual_channel() -> str:
    """客户集中度：走年报原文，能取到什么程度、取不到怎么办，三条边界都钉住。

    这一项换过三次形态，最后落在年报通道上，所以要防的是三种回退：
    1. 又去接要付费令牌的商用 API（那是"配个环境变量就能用"的死代码，会让人以为
       已经自动化了，于是不再读年报）；
    2. 把"前五名合计"当"最大一家"填进去（宁德 38.96% vs 13.73%，会直接把风险档顶上去）；
    3. 顺手把公告日晚于分析时点的年报拿来用（PIT 直接失效）。
    """
    from stockframe import autobuild
    from stockframe.sources.cninfo import CninfoProvider
    from stockframe.sources.eastmoney import EastmoneyProvider

    FID = "company.customer_concentration"
    ASOF = "2026-10-23"

    # 1) 通道是年报原文，不是令牌接口；离线时能整体关掉
    p = EastmoneyProvider(http=lambda url: {}, bond=False, cninfo=False)
    assert getattr(p, "_cninfo", None) is None
    assert hasattr(CninfoProvider(), "annual_customer")

    def block(**kw):
        raw = raw_fixture()
        base = {"form": "table", "top_ratio": 13.73, "five_ratio": 38.96,
                "period": "2025-12-31", "notice_date": "2026-03-10",
                "title": "宁德时代 2025 年年度报告", "page": 28, "rows": 5,
                "denominator": "年度销售总额", "top_name": "第一名",
                "cross_check": {"sum_ratio": 38.96, "total_ratio": 38.96, "ok": True}}
        base.update(kw)
        raw["customer"] = base
        return raw

    # 2) 有单客户占比 → 观测值 L1，且不再出现在待补清单
    out = autobuild.build(block(), ASOF)
    f = out["input"]["data"]["company"]["customer_concentration"]
    assert f["value"] == 13.73 and f["source_tier"] == "L1", f"应为 L1 观测值：{f}"
    assert f["as_of"] == "2025-12-31" and f["available_at"] == "2026-03-10", \
        "数据期取年报期末、可用时点取年报公告日，不能记成分析当日"
    assert "巨潮" in f["source"]
    assert [g for g in out["fill"]["pending"] if FID in g["fields"]] == []
    deriv = next(d for d in out["fill"]["derivations"] if d["field_id"] == FID)
    assert "第 28 页" in deriv["formula"] and "交叉校验" in deriv["note"]

    # 3) 只有前五合计 → 不填值，把上界写进缺口说明
    out2 = autobuild.build(block(form="total_only", top_ratio=None, five_ratio=10.10,
                                 rows=0, page=11, cross_check=None), ASOF)
    assert "customer_concentration" not in out2["input"]["data"]["company"], \
        "前五合计不是最大客户，不许当值填进去"
    g2 = next(g for g in out2["fill"]["pending"] if FID in g["fields"])
    assert g2["kind"] == "judgement" and "10.1" in g2["why"] and "不超过" in g2["why"]

    # 4) 年报里根本没这项（银行/保险）→ 仍然是一条普通判断项，不是报错
    out3 = autobuild.build(block(form="absent", top_ratio=None, five_ratio=None,
                                 rows=0, page=None, reason="年报正文无该项披露"), ASOF)
    g3 = next(g for g in out3["fill"]["pending"] if FID in g["fields"])
    assert g3["kind"] == "judgement" and "没有这项披露" in g3["why"]

    # 5) PIT：公告日晚于分析时点的年报一个字都不能用
    out4 = autobuild.build(block(notice_date="2027-03-10"), ASOF)
    assert "customer_concentration" not in out4["input"]["data"]["company"]
    assert any(FID in g["fields"] for g in out4["fill"]["pending"])

    # 6) B 段勾"不适用"是唯一能拿到的"单客户 <50%"证据：只收紧上界，仍然不填值
    #    药明康德 2025 年报就是这个形态：只给前五合计 48.11%，勾了"不适用"。
    out5 = autobuild.build(block(form="total_only", top_ratio=None, five_ratio=62.0,
                                 rows=0, page=17, cross_check=None,
                                 single_50_check="not_applicable"), ASOF)
    ca = out5["fill"]["customer_annual"]
    assert ca["upper_bound_pct"] == 50.0, "上界应收到 min(前五合计, 50)"
    assert ca["upper_bound_band"] == "弱" and ca["upper_bound_score"] == 2.5
    assert "customer_concentration" not in out5["input"]["data"]["company"], \
        "上界仍然只是上界，不许当观测值填"
    g5 = next(g for g in out5["fill"]["pending"] if FID in g["fields"])["why"]
    assert "上界" in g5 and "不适用" in g5 and "最坏" in g5
    assert "需要读一次年报" not in g5, "读了也没有，这句会把人送去白跑"

    # 7) 界面上那一格不能只是一个"缺失"——否则上界就等于没算
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "ind-cap" in html and "customer_annual" in html and "bound_note" in html, \
        "评分卡风险维度没有读 fill.customer_annual.bound_note"

    return ("通道=巨潮年报原文（无令牌）；单客户占比→L1、退出待补清单、推导带页码与校验；"
            "只有前五合计→不填值、缺口写上界；B 段勾不适用→上界收到 50% 并给最坏档位；"
            "无披露→回落判断项；公告日晚于时点→不用")


def a24_annual_parser_discipline() -> str:
    """年报解析的纪律：宁可判"不认"，也不许把错的数填进评分。

    四种披露形态实测覆盖 9 家（表 5 / 文字段 1 / 只有合计 2 / 无此表 1），这里各钉一例。
    最容易出事的两处是**金额列被当成占比列**（错位）和**把前五合计当最大一家**：
    前者靠交叉校验挡，后者靠 usable 挡。
    """
    from stockframe.sources.cninfo import parse_customer_section

    table = ("公司前 5大客户资料 序号 客户名称 销售额（千元） 占年度销售总额比例 "
             "1 第一名 58,159,202 13.73% 2 第二名 47,127,609 11.12% "
             "3 第三名 30,201,701 7.13% 4 第四名 15,419,319 3.64% "
             "5 第五名 14,153,702 3.34% 合计 -- 165,061,533 38.96%")
    d = parse_customer_section(table)
    assert d and d.form == "table" and d.top_ratio == 13.73 and d.rows == 5
    assert d.cross_check["ok"] and abs(d.cross_check["sum_ratio"] - 38.96) < 0.06

    # 单位换成元/万元，占比不能跟着变（这是当初差十倍的老坑）
    for unit in ("元", "万元"):
        alt = table.replace("（千元）", "（%s）" % unit)
        assert parse_customer_section(alt).top_ratio == 13.73, "单位 %s 影响了占比" % unit

    # 表格跨页断成 4 行：最大客户仍在第 1 行，不许因为"不齐"就整张丢掉
    partial = ("公司前 5大客户资料 序号 客户名称 销售额（元） 占年度销售总额比例 "
               "1 第一名 10,119,088,327.50 5.91% 2 第二名 5,922,757,391.80 3.46% "
               "3 第三名 3,999,596,480.24 2.34% 4 第四名 2,478,843,997.90 1.45% "
               "合计 -- 24,647,540,609.21 14.40%")
    dp = parse_customer_section(partial)
    assert dp.form == "table" and dp.top_ratio == 5.91 and dp.rows == 4
    assert dp.cross_check["rule"] == "partial_rows"

    # 列错位（合计对不上）→ 判不一致，值为空
    broken = table.replace("合计 -- 165,061,533 38.96%", "合计 -- 165,061,533 83.96%")
    db = parse_customer_section(broken)
    assert db.form == "table_inconsistent" and db.top_ratio is None and not db.usable

    # 文字段：给"第一名 X%"但分母是主营业务收入，口径必须跟着走
    sent = ("本报告期内，第一名和前五名客户销售额分别占年度主营业务收入的 8.0%和 35.8%；"
            "其中前五名客户交易中无关联方销售。")
    ds = parse_customer_section(sent)
    assert ds.form == "sentence" and ds.top_ratio == 8.0 and "主营业务收入" in ds.denominator

    # 只有前五合计 → usable=False，谁都不许拿它当最大客户
    only = ("前五名客户销售额1,704,578.78万元，占年度销售总额10.10%；"
            "其中前五名客户销售额中关联方销售额0万元。")
    do = parse_customer_section(only)
    assert do.form == "total_only" and do.five_ratio == 10.1 and not do.usable

    # 只是附注里顺带提到"前五名客户"，不是集中度表 → 什么都不给
    assert parse_customer_section(
        "财务报表附注 本行前五名客户存款余额合计占各项存款的比例不重大。") is None

    # 匿名化客户名（客户1…客户5）+ 合计行带"/"占位：名称含数字也必须能解析。
    # 曾经的 bug：名称组排除数字 → 整张表匹配不上 → 静默退化成"只有前五合计 54.59%"。
    anon = ("公司前五名客户 √适用 □不适用 单位：万元 币种：人民币 "
            "序号 客户名称 销售额 占年度销售总额比例（%） 是否与上市公司存在关联关系 "
            "1 客户1 9,787.51 24.59 否 2 客户2 5,743.28 14.43 否 "
            "3 客户3 2,739.17 6.88 否 4 客户4 2,034.61 5.11 否 "
            "5 客户5 1,422.08 3.57 否 合计 / 21,726.65 54.59 否")
    da = parse_customer_section(anon)
    assert da and da.form == "table", "匿名化名称不许让整张表消失"
    assert da.top_ratio == 24.59 and da.five_ratio == 54.59 and da.rows == 5
    assert da.top_name == "客户1" and da.cross_check["rule"] == "sum==total"

    # 名称放开数字后，没有序号锚点的表仍不许硬凑
    no_seq = ("公司前 5大客户资料 序号 客户名称 销售额（元） 占年度销售总额比例 "
              "9,787.51 24.59 9,787.51 24.59 9,787.51 24.59")
    dn = parse_customer_section(no_seq)
    assert dn is None or dn.top_ratio is None, "缺序号锚点却凑出值，说明错位防线失效"

    # 英文长名 + 客户表后紧贴供应商表（阳光电源 300274 的真实版式）。
    # 曾经的 bug：名称上限 24 字符漏掉「Algihaz Contracting Company」(26)，循环 continue 跳过后
    # 把供应商五行接成客户表 → 和 25.11 撞合计 16.96 → 一份可用的年报被判 table_inconsistent。
    sungrow = ("公司主要销售客户情况 前五名客户合计销售金额（元） 15,123,390,977.62 "
               "公司前 5大客户资料 序号 客户名称 销售额（元） 占年度销售总额比例 "
               "1 Algihaz Contracting Company 4,010,035,531.92 4.50% "
               "2 中国能源建设集团有限公司 3,133,290,738.68 3.51% "
               "3 中建投租赁股份有限公司 3,035,992,813.05 3.40% "
               "4 湖北联投集团有限公司 2,574,124,517.62 2.89% "
               "5 Larsen and Toubro Limited 2,369,947,376.35 2.66% "
               "合计 -- 15,123,390,977.62 16.96% 主要客户其他情况说明 □适用 √不适用 "
               "公司主要供应商情况 前五名供应商合计采购金额（元） 11,923,973,315.00 "
               "公司前 5名供应商资料 序号 供应商名称 采购额（元） 占年度采购总额比例 "
               "1 供应商一 6,928,853,013.03 14.59% "
               "2 明阳新能源投资控股集团有限公司 1,343,123,078.47 2.83% "
               "3 供应商二 1,236,057,374.84 2.60% "
               "4 欣旺达电子股份有限公司 1,210,830,597.80 2.55% "
               "5 瑞浦兰钧能源股份有限公司 1,205,109,250.86 2.54% "
               "合计 -- 11,923,973,315.00 25.12%")
    dg = parse_customer_section(sungrow)
    assert dg and dg.form == "table", "英文长名把整张表判成不一致：%r" % (dg,)
    assert dg.top_ratio == 4.5 and dg.top_name and "Algihaz" in dg.top_name
    assert dg.cross_check["sum_ratio"] == 16.96 and dg.cross_check["ok"]
    assert dg.five_ratio == 16.96 and dg.top_ratio != 14.59, "供应商第一大被当成客户第一大"

    # 客户表只解析到 3 行时，后面的供应商表（序号同样从 1 起）绝不能被接上
    dg3 = parse_customer_section(sungrow.replace(
        "4 湖北联投集团有限公司 2,574,124,517.62 2.89% "
        "5 Larsen and Toubro Limited 2,369,947,376.35 2.66% ", ""))
    assert dg3 and dg3.rows == 3 and dg3.top_ratio == 4.5 and dg3.top_ratio != 14.59
    assert dg3.cross_check["sum_ratio"] == 11.41

    return ("表→13.73%（五行之和=合计 38.96，校验通过）；元/万元不改占比；跨页 4 行仍取第 1 名 5.91%；"
            "列错位→判不一致不填；文字段保留主营业务收入口径 8.0%；只有合计→usable=False；无关页→None；"
            "匿名化「客户1」+「合计 /」→ 24.59%（不退化为总合计）；缺序号的表→不凑；"
            "英文长名 4.50%（不误判不一致）；客户表后接供应商表→不串（14.59% 不得冒充）")


def a23_cadence_matches_disclosure_rhythm() -> str:
    """披露节奏与统计口径必须分开：季度更新的字段 vs 一年只披露一次的字段。

    为什么单列一条：把年报口径的字段按季频衡量，会得出"年报公布三个月后就超期"的结论，
    于是这个字段一年里只有几个月算"新鲜"——看起来像"取不到"，其实是尺子用错了。
    同理，EPS(TTM) 的分子来自定期报告，不能用日频去卡。
    """
    from stockframe import autobuild
    from stockframe.datadict import FIELD_SPECS
    from stockframe.engine import run

    specs = {s.field_id: s for s in FIELD_SPECS}
    FID = "company.customer_concentration"
    ASOF = "2026-10-23"

    annual = [s.field_id for s in FIELD_SPECS if s.annual]
    assert annual == [FID], f"年报口径字段登记有变，需同步本条验收：{annual}"
    assert specs[FID].update_days >= 365, \
        f"年报口径字段的周期是 {specs[FID].update_days} 天，应是 ≥365"
    for fid in ("valuation.eps_ttm", "financial.roe_ttm"):
        assert specs[fid].update_days >= 90, f"{fid} 按日频衡量会误判超期"
    assert specs["market.price_close"].update_days == 1, "行情才是真日频"

    out = autobuild.build(raw_fixture(), ASOF)
    f = out["fill"]
    ao = f["as_of"]
    assert ao["latest_report_period"] != ao["latest_annual_period"], \
        "两把尺子必须分开给：最新报告期(季) ≠ 最近一期已公告年报"
    assert ao["latest_annual_period"].endswith("12-31")
    assert ao["latest_annual_notice"] <= ASOF, "年报公告日不得晚于分析时点（前视）"
    assert f["annual_fields"] == [FID]

    warn = next(w for w in f["warnings"] if "非完整年度" in w)
    assert ao["latest_annual_period"] in warn and "滞后一年内属正常" in warn, \
        f"告警要说清年报口径字段不随季报前移：{warn[:100]}"

    inp = out["input"]
    inp["data"].setdefault("company", {})["customer_concentration"] = {
        "value": 18.5, "as_of": ao["latest_annual_period"],
        "available_at": ao["latest_annual_notice"], "period_end": ao["latest_annual_period"],
        "source": "人工研究判断（界面录入）", "source_tier": "L3",
    }
    dq = run(inp)["data_quality"]
    stale = next((n for n in dq["notes"] if n.startswith("数据超期项")), "")
    assert FID not in stale, f"年报口径被判超期：{stale[:120]}"
    assert any("只在年报披露" in n for n in dq["notes"]), "数据质量说明要标注该字段的年报口径"

    return (f"最新报告期 {ao['latest_report_period']}（季）与最近年报 {ao['latest_annual_period']} "
            f"（{ao['latest_annual_notice']} 公告）分开给；年报口径周期 {specs[FID].update_days} 天，"
            "填报后不进超期项；EPS(TTM) 92 天不再按日频误判")


def _partial_payload_shape_ok(payload: dict) -> bool:
    return all(k in payload for k in ("stock", "analysis_date", "input", "fill")) and bool(payload["stock"].get("code"))
def a25_text_judgement_roundtrip() -> str:
    """文本型判断项（一句话本质）的录入通路必须完整。

    曾经的 bug：该字段被渲染成数字输入框、提交时 Number("一句话")=NaN 被静默跳过，
    用户填了内容却收到"尚未填写任何判断项"。要守三件事：
    1. 字段登记为"文本"（前端区分文本/数值输入的唯一依据）；
    2. 界面代码有文本分支（type=text 的输入框 + 提交时不做数字转换）；
    3. engine 端字符串 value 畅通：结论首句输出原话，空串仍算未填。
    """
    from stockframe import autobuild
    from stockframe.datadict import FIELD_SPECS
    from stockframe.engine import run

    spec = next(s for s in FIELD_SPECS if s.field_id == "company.logic_essence")
    assert spec.unit == "文本", f"应为文本字段：unit={spec.unit!r}"

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'unit==="文本"' in html, "judgeRow 缺文本分支：文本字段会被渲染成数字输入框"
    assert 'data-text="1"' in html, "文本输入框缺 data-text 标记"
    assert 'dataset.text==="1"' in html, "applyJudgement 缺文本识别：文字会被 Number() 吞掉"
    assert "文本）" in html, "录入来源应标注文本形态"

    out = autobuild.build(raw_fixture(), "2026-10-23")
    inp = out["input"]
    sentence = "全球动力电池龙头，量增与储能第二曲线撑住龙头溢价"
    inp["data"].setdefault("company", {})["logic_essence"] = {
        "value": sentence, "as_of": "2026-10-23", "available_at": "2026-10-23",
        "source": "人工研究判断（界面录入·文本）", "source_tier": "L3",
    }
    rep = run(inp)
    assert sentence in rep["conclusion"]["summary"], "录入的一句话必须进结论首句"
    assert "未填：需人工补一句逻辑内核" not in rep["conclusion"]["summary"]

    # 空串不是有效录入：不能把空串当已填
    inp["data"]["company"]["logic_essence"]["value"] = ""
    rep2 = run(inp)
    assert "未填：需人工补一句逻辑内核" in rep2["conclusion"]["summary"], \
        "空串应视为未填"

    return ("logic_essence 登记为文本；界面渲染 type=text 并按字符串提交；"
            "录入后结论首句即原话；空串仍判未填")


def a26_qualitative_anchors_are_visible() -> str:
    """定性档位必须在界面上带含义，不能只给"1 档…5 档"。

    档位不是自由评分：rubric 把档位严格映射成档内分（(档-1)×2.5，1→0、3→5、5→10），
    所以选错一档整个因子偏 2.5 分。界面若不显示每档的行为描述，等于让用户盲选一个直接
    改分数的值——锚定量表本来就在 specs.py 里写好并进了评分证据，只是没通到前端。

    要守三件事：
    1. meta 下发的每个定性字段都带齐 1-5 档锚定，且档位与量规分数一一对应；
    2. 界面的下拉选项带锚定短语、选中后显示完整描述（档位即语义）；
    3. 文本字段与档位字段不走同一分支（一句话本质是字符串，已由 A25 钉住）。
    """
    from stockframe.rubric import IndicatorSpec
    from stockframe.server import _field_meta
    from stockframe.specs import ANCHORS, rating_bands

    # 档位 → 分数的换算必须与 rubric 一致（锚定指标不看 bands，直接 (档-1)×2.5）
    assert [b.score for b in rating_bands()] == [0.0, 2.5, 5.0, 7.5, 10.0]
    spec = IndicatorSpec(id="t", name="t", unit="1-5档", field="t",
                         bands=rating_bands(), weight=1.0, rationale="t",
                         anchors=ANCHORS["policy_stance"])
    assert [spec.anchors[r] and (r - 1) * 2.5 for r in range(1, 6)] == [0.0, 2.5, 5.0, 7.5, 10.0]

    meta = _field_meta()
    qual = [f for f in meta["fields"] if f["qualitative"]]
    assert len(qual) >= 8, f"定性字段应覆盖宏观/行业/公司三类，实际 {len(qual)}"
    missing = [f["field_id"] for f in qual if "anchors" not in f]
    assert not missing, f"这些定性字段缺锚定说明：{missing}"
    for f in qual:
        a = f["anchors"]
        assert set(a) == {"1", "2", "3", "4", "5"}, f"{f['field_id']} 锚定档位不全：{sorted(a)}"
        assert all(len(v) >= 6 for v in a.values()), f"{f['field_id']} 有档位描述过短"

    # 三个典型字段：政策取向 / 渗透空间 / 投资逻辑强度，档位语义必须能区分
    by_id = {f["field_id"]: f["anchors"] for f in qual}
    assert "抑制" in by_id["macro.policy_stance"]["1"] and "支持" in by_id["macro.policy_stance"]["5"]
    assert "70%" in by_id["industry.penetration_space"]["1"]
    assert "预期差" in by_id["company.logic_strength"]["4"], "逻辑强度的关键在预期差，4 档必须点明"

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "data-anchor=" in html, "下拉未标记档位类型，无法按档位取锚定"
    assert "data-hint=" in html, "缺少档位含义展示位"
    assert "refreshAnchorHint" in html, "选中档位后不会回填该档含义"
    assert "anchorShort(anchorOf(" in html, "选项文本没带锚定短语，等于仍是盲选"
    assert 'unit==="文本"' in html, "文本字段分支不能被档位分支覆盖"

    return (f"{len(qual)} 个定性字段全部下发 1-5 档锚定；档位↔分数 1→0/3→5/5→10 未漂移；"
            "下拉显示「档位｜短语」、选中回填整段含义（政策：抑制→支持；渗透：>70% 无空间；逻辑：4 档含预期差）")


def a27_input_contract() -> str:
    """脏数据不许变成分数。

    评分带用 `v >= lo and v < hi` 比较，而 NaN 对任何比较都为 False（落进最差档）、
    +Inf 命中末带（落进满分档）——两者都被当成真实分数，置信度也不受影响。
    同类"安静地错"还有：档位 4.7 被 int() 截成 4、档位 9 静默当缺失、
    字段名拼错后整块证据消失、文本字段塞数字直接进结论首句。
    这一项把这些全部钉成"报错"。
    """
    from stockframe.contract import ContractError

    base = load_sample()

    def expect_error(mut, keyword, label):
        inp = copy.deepcopy(base)
        mut(inp)
        try:
            run(inp)
        except ContractError as exc:
            assert keyword in str(exc), f"{label}: 报错信息未点明问题（{exc}）"
            return
        raise AssertionError(f"{label}: 未报错——脏数据会静默变成分数")

    expect_error(lambda i: i["data"]["financial"]["roe_ttm"].update(value=float("nan")), "NaN", "roe=NaN")
    expect_error(lambda i: i["data"]["financial"]["roe_ttm"].update(value=float("inf")), "NaN/Inf", "roe=+Inf")
    expect_error(lambda i: i["data"]["company"]["moat"].update(value=4.7), "整数", "档位 4.7")
    expect_error(lambda i: i["data"]["company"]["moat"].update(value=9), "越界", "档位 9")
    expect_error(lambda i: i["data"]["market"]["price_close"].update(value=0), "必须为正", "price=0")

    def typo(i):
        i["data"]["financial"]["roe_typo"] = i["data"]["financial"].pop("roe_ttm")

    expect_error(typo, "roe_ttm", "字段名拼错")
    expect_error(lambda i: i["data"]["company"]["logic_essence"].update(value=12345), "文本字段", "文本=数字")

    # 基线不受影响
    rep = run(base)
    assert rep["conclusion"]["bucket_mid"] == "入池", "样例基线被契约改动影响"

    return ("NaN/±Inf/档位 4.7/档位 9/price=0/字段名拼错/文本塞数字 共 7 组反例全部报错，"
            "且报错点名字段与原因；基线 7.64 入池不变")


def a28_applicability_gate() -> str:
    """估值方法前门：先判断"该不该算"，再谈"算多少"。

    旧行为是照样算：平安银行（000001）综合分 7.12 过入池线，DCF 给 24.58–33.28 元
    对股价 11.20，安全边际 +95.7%，EBIT 率 43.66%——唯一拦住它的是覆盖闸门，
    不是行业判断。亏损时更直接：EPS=-0.8 会输出负股价 -17.6 元并通过 schema。
    """
    from stockframe.datadict import InputError

    base = load_sample()

    def bank(i):
        i["meta"]["company_type"] = "bank"
        i["meta"]["relative_valuation_metric"] = "PB"

    inp = copy.deepcopy(base)
    bank(inp)
    rep = run(inp)
    v = rep["valuation"]
    assert v["gate"]["dcf_applicable"] is False and v["gate"]["code"] == "DCF_NOT_APPLICABLE"
    assert "scenario_results" not in v["dcf"], "不适用时不得留下 DCF 数字"
    assert v["synthesis"]["safety_margin_pct"] is None, "不适用时不得给安全边际"
    assert v["synthesis"]["combined_range"] == {"low": None, "high": None}
    assert rep["scenarios"] == [], "没有 DCF 就没有情景价位"
    assert rep["conclusion"]["decision"]["rule_id"] == "R9_MODEL_NOT_APPLICABLE"
    assert rep["conclusion"]["bucket_mid"] == "人工复核"
    ok, errs = validate_report(rep)
    assert ok, "银行报告 schema 校验失败：" + "; ".join(errs[:3])

    # 口径必须与类型自洽：银行不能声明 PE
    try:
        bad = copy.deepcopy(base)
        bad["meta"]["company_type"] = "bank"
        run(bad)
    except InputError as exc:
        assert "PB" in str(exc)
    else:
        raise AssertionError("bank + PE 应报错（PE 对银行无意义）")

    # 亏损状态：任何类型都升级为不适用
    for label, mut in (
        ("EPS=-0.8", lambda i: i["data"]["valuation"]["eps_ttm"].update(value=-0.8)),
        ("PE=-35", lambda i: i["data"]["market"]["pe_ttm"].update(value=-35.0)),
    ):
        loss = copy.deepcopy(base)
        mut(loss)
        lr = run(loss)
        assert lr["valuation"]["gate"]["code"] == "DCF_LOSS_MAKING", f"{label}: 应触发亏损前门"
        assert lr["meta"]["company_type_effective"] == "loss_making"
        assert lr["valuation"]["relative"]["implied_price_range"]["low"] is None
        assert lr["valuation"]["synthesis"]["safety_margin_pct"] is None, f"{label}: 不得给安全边际"
        ok2, errs2 = validate_report(lr)
        assert ok2, f"{label}: schema 校验失败：{'; '.join(errs2[:3])}"

    # 区间下限必须为正
    from stockframe.valuation import synthesize
    out = synthesize({"value_per_share_low": -2.0, "value_per_share_high": 1.0},
                     {"available": False, "implied_price_range": {"low": None, "high": None}},
                     10.0, [], gate=None)
    assert out["mode"] == "rejected" and out["combined_range"] == {"low": None, "high": None}

    return ("bank→DCF_NOT_APPLICABLE（无区间/无安全边际/无情景，落 R9 人工复核）；"
            "bank+PE→报错；EPS≤0 或 PE≤0→DCF_LOSS_MAKING；区间含非正值→rejected")


def a29_wording_enforced_at_runtime() -> str:
    """违禁措辞必须在导出前拦，而不只是验收时扫样例报告。

    文档曾写着"管线会扫描并拦下"，代码里却只有本脚本的 A4 扫样例——
    一条 `logic_essence="建议目标价 45 元买入"` 会原样进结论首句。
    现在改成运行期检查，且词表与 A4 的 BANNED 同源。
    """
    from stockframe import wording

    assert tuple(BANNED) == wording.BANNED_TERMS, \
        f"两套词表已漂移：验收 {BANNED} vs 运行时 {wording.BANNED_TERMS}"

    base = load_sample()
    hits = []
    for term in ("目标价", "买入", "止损"):
        inp = copy.deepcopy(base)
        inp["data"]["company"]["logic_essence"]["value"] = f"逻辑成立就{term}"
        try:
            run(inp)
        except wording.WordingError:
            hits.append(term)
        else:
            raise AssertionError(f"输入含「{term}」未被拦下")

    rep = run(base)
    text = json.dumps(rep["conclusion"], ensure_ascii=False)
    assert not [t for t in BANNED if t in text], "样例报告的结论段出现违禁措辞"

    return ("词表与 A4 同源；输入含「目标价/买入/止损」→ WordingError；"
            "样例报告结论段干净")


def a30_assumption_contract_and_strict_json() -> str:
    """假设层契约 + 报告必须是合法 JSON + 安全边际只有一个口径。

    上一轮把契约挂在 data 字段上，assumptions（DCF 假设）没接线：
    `wacc=NaN` 一路算出 NaN 区间，报告"通过 schema"（jsonschema 把 NaN 当 number），
    但 node/浏览器 `JSON.parse` 直接 `Unexpected token 'N'`；
    `shares=0` 抛 ZeroDivisionError（HTTP 500），`wacc="0.08"` 抛 TypeError。
    """
    from stockframe.contract import ContractError

    base = load_sample()

    # ① 假设层的 NaN/Inf/零股本/字符串/缺块，必须报错并点名字段
    cases = []
    for key, bad in (("wacc", float("nan")), ("revenue_base", float("inf")),
                     ("shares", 0), ("tax_rate", "0.15")):
        inp = copy.deepcopy(base)
        inp["assumptions"]["dcf"]["base"][key] = bad
        try:
            run(inp)
        except (ContractError, ValueError) as exc:
            assert key in str(exc), f"{key} 报错信息未点名字段：{exc}"
            cases.append(key)
        else:
            raise AssertionError(f"assumptions.dcf.base.{key}={bad!r} 未被拦下")

    inp = copy.deepcopy(base)
    inp["assumptions"].pop("dcf")
    try:
        run(inp)
    except (ContractError, ValueError) as exc:
        assert "dcf" in str(exc)
    else:
        raise AssertionError("删掉 assumptions.dcf 未报错")

    # ② 出口：报告必须能被严格 JSON 解析（schema 通过 ≠ 合法 JSON）
    rep = run(base)
    strict = json.dumps(rep, ensure_ascii=False, allow_nan=False)

    # ③ 安全边际只有一个口径：评分卡的 val.dcf_margin 与 synthesis.dcf_margin_pct 必须同源
    syn = rep["valuation"]["synthesis"]
    dim = next(d for d in rep["scorecard"]["dimensions"] if d["id"] == "valuation")
    ind = next(x for f in dim["factors"] for x in f["indicators"] if x["id"] == "val.dcf_margin")
    assert ind["value"] == syn["dcf_margin_pct"], \
        f"评分卡 {ind['value']} 与 synthesis.dcf_margin_pct {syn['dcf_margin_pct']} 不同源"

    # ④ 两法无重叠时不得输出合成安全边际
    inp = copy.deepcopy(base)
    for name in ("base", "optimistic", "pessimistic"):
        inp["assumptions"]["dcf"][name].update(ebit_margin_path=[0.45] * 5)
    union_rep = run(inp)
    usyn = union_rep["valuation"]["synthesis"]
    if usyn["mode"] == "union":
        assert usyn["safety_margin_pct"] is None, "union 仍输出了合成安全边际"
        assert usyn["margin_basis"] == "none"
        udim = next(d for d in union_rep["scorecard"]["dimensions"] if d["id"] == "valuation")
        uind = next(x for f in udim["factors"] for x in f["indicators"] if x["id"] == "val.dcf_margin")
        assert uind["suppressed"] is True, "两法分歧+终值主导时 DCF 安全边际仍参与评分"

    # ⑤ 措辞：引用/事实/否定不该被误伤，指令仍要拦
    benign = ("股价仍低于卖方目标价中位数", "当前阶段不加仓", "大股东公告清仓式减持")
    for text in benign:
        inp = copy.deepcopy(base)
        inp["data"]["company"]["logic_essence"]["value"] = f"买的是份额兑现：{text}，若份额下滑则失效"
        run(inp)  # 不应抛 WordingError
    inp = copy.deepcopy(base)
    inp["data"]["company"]["logic_essence"]["value"] = "给予买入评级，目标价 45 元"
    try:
        run(inp)
    except Exception as exc:
        assert "WordingError" in type(exc).__name__
    else:
        raise AssertionError("「给予买入评级，目标价 45 元」未被拦下")

    return (f"假设层 {len(cases)}+1 组反例报错并点名字段；报告严格 JSON 可序列化（{len(strict)} 字节）；"
            "评分卡与 synthesis 安全边际同源；union 不出合成安全边际且抑制评分；"
            "「卖方目标价/不加仓/清仓式减持」放行、「建议买入+目标价」拦截")


def a31_flip_points_are_exact_and_engine_side() -> str:
    """结论翻转点：界面上的"−2.13"必须真的翻桶，且必须由引擎算。

    最自然的错法是在前端手算 `(综合分 − 7.0) / 该维权重`——那样样例会报"估值 −4.00"，
    而实际上任一维度跌破 4.0 的入池下限，−2.13 就已经把【入池】打成【观察】。
    所以这项同时钉两件事：位移的最小性（少 0.01 不翻），以及界面只读引擎字段。
    """
    from stockframe import sensitivity as S

    rep = json.loads((ROOT / "examples" / "sample_report.json").read_text(encoding="utf-8"))
    sens = rep["conclusion"]["sensitivity"]
    w = rep["scorecard"]["weights"]["mid_term"]
    judge = {"macro", "industry", "company"}
    rows = [{"id": d["id"], "name": d["name"], "weight": w[d["id"]],
             "score": d["score_mid"], "anchored": d["id"] in judge}
            for d in rep["scorecard"]["dimensions"]]
    bucket = rep["conclusion"]["bucket_mid"]

    assert sens["drivers"], "样例报告没有算出任何可翻维度"
    for d in sens["drivers"]:
        at = S.bucket_with(rows, d["id"], round(d["score"] - d["delta"], 2))
        assert at == d["bucket_after"] != bucket, \
            f"{d['id']}：声称 −{d['delta']} 转【{d['bucket_after']}】，实测 {at}"
        back = S.bucket_with(rows, d["id"], round(d["score"] - d["delta"] + 0.01, 2))
        assert back == bucket, f"{d['id']}：少 0.01 就已经翻桶，位移不是最小"

    # 机制必须是"最早"的那条：样例估值先跌破 4.0，而不是等到 R5 的 3.0
    val = [d for d in sens["drivers"] if d["id"] == "valuation"][0]
    assert val["mechanism"] == "dim_floor", f"估值首个机制应为维度下限，实际 {val['mechanism']}"

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    # 主线四行改成「立场 / 下一步 / 放弃线 / 可信度」后，翻转点等结构读数折进「进阶读数」，
    # 但字段来源不变：仍然是 sen.drivers / sen.band / sen.headroom，不许在前端重算。
    for label in ("结构", "翻转", "维度", "口径"):
        assert f">{label}</div>" in html, f"进阶读数缺少「{label}」行"
    assert "sen.drivers" in html and "sensitivity" in html, "界面没有读引擎的 sensitivity 字段"
    # 只报"往下多远"会让人以为上面还有空间：界面必须同时读 band（桶内位置）与 headroom（向上有无档位）
    assert "sen.band" in html and "sen.headroom" in html, \
        "界面没有读 band/headroom——方向那行会退化回「只报向下多远」"
    assert "hr.at_top" in html, "界面没有处理「入池已达最高档」这一情形"
    md = (ROOT / "examples" / "sample_report.md").read_text(encoding="utf-8")
    assert "结论翻转点" in md and "翻桶时得分" in md, "Markdown 报告缺少翻转点表"
    assert "往上已无更高档位" in md, "Markdown 没说明向上已无更高档位"

    return (f"{len(sens['drivers'])} 维各有最小位移；最敏感「{sens['drivers'][0]['name']}」"
            f"−{sens['drivers'][0]['delta']:.2f}（{sens['drivers'][0]['mechanism']}）→ "
            f"{sens['drivers'][0]['bucket_after']}；少 0.01 均不翻；"
            f"桶内位置 {sens['band']['position']:.0%}（{sens['band']['zone']}）、"
            f"向上{'无档位' if sens['headroom']['at_top'] else '差 %.2f 分' % sens['headroom']['room']}；"
            "界面四行与 MD 表均读引擎字段")



def a33_empty_field_says_which_kind_of_empty() -> str:
    """缺口原因必须可分辨：自动提取失败 / 只披露前五合计 / 压根没填，不能共用一句话。

    这条钉的是"读报告的人下一步该做什么"：前两种要去重跑或翻年报，第三种才是让你补录。
    三种都写成"未提供"时，读者只能靠猜——而这恰好是"这项是不是坏了"的来源。
    """
    from stockframe.engine import run as engine_run
    from stockframe.report import render_markdown

    inp = json.loads(json.dumps(json.loads(
        (ROOT / "examples" / "sample_input.json").read_text(encoding="utf-8"))))
    inp["data"].get("company", {}).pop("customer_concentration", None)
    note = ("本次自动提取失败：年报 PDF 下载超时（30s）。"
            "已下载的年报会缓存复用，重跑一次通常能过")
    inp["data_notes"] = {"company.customer_concentration": note}
    rep = engine_run(inp)

    gaps = [g for g in rep["data_quality"]["gaps"] if g["field"] == "company.customer_concentration"]
    assert gaps, "客户集中度留空却没有进缺口清单"
    assert gaps[0]["reason"] == note, f"缺口没有采用装配层给的原因：{gaps[0]['reason']}"
    others = [g for g in rep["data_quality"]["gaps"] if g["field"] != "company.customer_concentration"]
    assert all(g["reason"] in ("必填字段缺失", "未提供（该指标按缺失处理，不补零）") for g in others), \
        "没给原因的字段不该被别处的说明污染"

    md = render_markdown(rep)
    assert note in md, "Markdown 的缺口列表把带原因的条目截断了（前 10 条截断规则漏掉了它）"

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "esc(g.field)" in html and "esc(g.reason)" in html, \
        "数据质量页的缺口必须渲染字段名与原因，不能直接把对象塞进 esc()"
    return "失败原因进缺口（截断不吞）+ 其它字段仍走通用句 + 数据质量页渲染 field/reason"


def a32_stance_answers_the_question() -> str:
    """立场句：报告第一句话必须回答「现在该不该动手」。

    存在的理由：分桶名（观察/入池）对非专业读者不构成答案。实测把四行技术读数
    （分数结构/翻转点/维度排序/两法分歧）摆在最前面，读者的反馈是"看完还是不知道
    该不该买"。所以引擎多给一段 stance，四档与四个桶一一对应。

    要防的回退有四种：
    1. 悄悄变成第五种判定（stance 只能由 bucket 决定）；
    2. 把「入池」写成买入提示（入池 ≠ 现在买，这句必须写在 basis 里）；
    3. 这段自己长出交易指令（它是最像投资建议的一段，必须过 A4 词表）；
    4. 界面把它塞回角落（必须在八维图之前）。
    """
    from stockframe.engine import _stance, run
    from stockframe.report import render_markdown
    from stockframe.validate import validate_report
    from stockframe.wording import scan

    def fake(bucket, score=6.84, rule="R6_SCORE_BAND"):
        sens = {"boundary": 3.5, "distance": 3.34, "distance_anchors": 1.34,
                "next_bucket": "回避",
                "band": {"low": 3.5, "high": 7.0, "position": 0.95, "zone": "上沿"},
                "headroom": {"at_top": False, "ceiling": 7.0, "room": 0.16, "next_up": "入池"},
                "drivers": [], "insensitive": [], "unscored": [], "note": "单维归零不翻桶"}
        return _stance(bucket, score, rule, ["中线综合分处于观察带"], sens,
                       {"gaps": [{"field": "a.b", "reason": "未提供"}]}, 0.807,
                       {"judgement": 0.44, "data": 0.42, "risk_catalyst": 0.14},
                       {"dcf_applicable": True},
                       {"divergence_pct": 14.4, "dcf_margin_pct": 16.8, "relative_margin_pct": 3.1},
                       [{"id": "F1", "condition": "ROE 跌破 17.8% → 重估",
                         "metric": "financial.roe_ttm", "threshold": "≥ 17.8%"}])

    # 1) 四个桶 → 四档，不多不少
    for bucket, code in (("人工复核", "insufficient"), ("回避", "avoid"),
                         ("观察", "hold_off"), ("入池", "research")):
        assert fake(bucket)["code"] == code, f"{bucket} 应映射到 {code}，实际 {fake(bucket)['code']}"

    # 2) 观察档要直接回答"现在能不能动手"，并拆掉"等它跌"这个误读
    hold = fake("观察")
    assert hold["label"] == "暂不介入"
    assert "0.16" in hold["headline"] and "不是动手的位置" in hold["headline"]
    assert "不会因为股价下跌自己变高" in " ".join(hold["do_next"]), \
        "必须点明分数衡量质量与估值，不是涨跌空间"
    # 3) 入池 ≠ 现在买；人工复核 ≠ 这票很差
    assert "不包含「价格合适」" in fake("入池", 7.64)["basis"], "入池被读成买入信号"
    manual = fake("人工复核", None, "R9_MODEL_NOT_APPLICABLE")
    assert "这套尺子量不出来" in manual["headline"] and "不是「不好」" in manual["basis"]

    # 4) 四档全文过 A4 词表（立场句是最像投资建议的一段，反而最需要闸门）
    for bucket in ("人工复核", "回避", "观察", "入池"):
        st = fake(bucket)
        for text in [st["label"], st["headline"], st["basis"], st["abandon"], st["caveat"], *st["do_next"]]:
            assert scan(text) == [], f"{bucket} 的立场句含交易指令措辞：{text}"

    # 5) 样例报告：字段齐、schema 必填、MD 排在分桶之前
    inp = json.loads((ROOT / "examples" / "sample_input.json").read_text(encoding="utf-8"))
    rep = run(inp)
    st = rep["conclusion"]["stance"]
    assert st["code"] == "research" and st["label"] == "够格研究", f"样例分桶与立场不一致：{st}"
    ok, errs = validate_report(rep)
    assert ok, f"含 stance 的报告未通过 schema：{errs[:2]}"
    md = render_markdown(rep)
    assert "### 立场：" in md and md.index("### 立场：") < md.index("**中线分桶："), \
        "Markdown 里立场必须排在分桶之前"

    # 6) 界面：立场卡在八维图之前，且读的是引擎字段（不在前端另拼一句话）
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    for token in ("vt-stance", "st.label", "st.do_next", "st.abandon", "st.caveat"):
        assert token in html, f"界面没有读 stance.{token.split('.')[-1]}"
    assert html.index("现在该不该动手") < html.index("八维得分与贡献分层"), \
        "立场卡必须排在八维图之前，否则读者第一眼还是只有分数"
    assert "旧报告/降级" in html, "缺少无 stance 时的降级分支（旧报告不能白屏）"

    return ("四桶→四档（insufficient/avoid/hold_off/research）不加判定；观察档答「暂不介入·差 0.16 分」"
            "并拆穿「等跌就够格」；入池档写明不等于现在买；人工复核写成「尺子量不出来」；"
            "四档全文过 A4 词表；样例 schema 通过；MD 与界面均把立场排在分数之前")


def main() -> int:
    check("A1", "单元测试全绿", a1_unit_tests)
    check("A2", "样例管线跑通且通过 schema", a2_pipeline_and_schema)
    check("A3", "确定性重跑 & 样例报告同步", a3_deterministic_rerun)
    check("A4", "无买卖/点位措辞", a4_no_banned_language)
    check("A5", "PIT 时点与来源纪律", a5_pit_guard)
    check("A6", "评分可追溯（rationale+证据四要素）", a6_traceability)
    check("A7", "反指标堆砌（因子≤3/指标≤4/量规≥4档）", a7_anti_piling)
    check("A8", "结论条件化（维持≥3/证伪≥3）", a8_conditional_conclusion)
    check("A9", "决策门槛（红旗/必填/特殊状态）", a9_decision_gates)
    check("A10", "情景概率自洽", a10_probability_gate)
    check("A11", "文档与代码同源（评分说明）", a11_doc_code_consistency)
    check("A12", "数据字典登记完整", a12_data_dictionary_complete)
    check("A13", "界面与样例同源（无界面漂移）", a13_ui_synced)
    check("A14", "接口与引擎同口径（薄壳）", a14_api_parity)
    check("A15", "自动取数受 PIT 约束（不用未披露报告）", a15_autofetch_pit)
    check("A16", "证据覆盖闸门（缺维度不出正式分桶）", a16_coverage_gate)
    check("A17", "自动假设不可行时拒绝并交还半成品", a17_infeasible_returns_partial)
    check("A18", "判断层代理可复算且可被人工覆盖", a18_proxy_traceable_and_overridable)
    check("A19", "字段清单可对账（已填+待补=字典）", a19_field_ledger_reconciles)
    check("A20", "兜底源留痕 + 同业锚可比性闸门", a20_fallback_traceable_and_peer_anchor_gated)
    check("A21", "10Y 利率同比的时点与不可插值", a21_rate_10y_pit_and_no_extrapolation)
    check("A22", "客户集中度：年报原文自动取，取不到回落判断项", a22_customer_annual_channel)
    check("A23", "披露节奏与统计口径分离（年报字段不按季频衡量）", a23_cadence_matches_disclosure_rhythm)
    check("A24", "年报解析纪律（错位/跨页/单位/分母/只有合计）", a24_annual_parser_discipline)
    check("A25", "文本判断项录入通路（一句话本质不被当数字吞掉）", a25_text_judgement_roundtrip)
    check("A26", "定性档位可见（1-5 档必须带锚定含义）", a26_qualitative_anchors_are_visible)
    check("A27", "输入契约（脏数据不许变成分数）", a27_input_contract)
    check("A28", "估值方法前门（行业/亏损不适用，区间非正拒绝）", a28_applicability_gate)
    check("A29", "违禁措辞在导出前拦下（运行期，不只验收）", a29_wording_enforced_at_runtime)
    check("A30", "假设层契约 + 报告严格 JSON + 安全边际单一口径", a30_assumption_contract_and_strict_json)
    check("A31", "结论翻转点（位移最小性 + 引擎计算）", a31_flip_points_are_exact_and_engine_side)
    check("A32", "立场句（报告第一句回答「现在该不该动手」）", a32_stance_answers_the_question)
    check("A33", "缺口原因可分辨（失败 / 只披露合计 / 没填）", a33_empty_field_says_which_kind_of_empty)

    width = max(len(t) for _, t, _, _ in results) + 2
    print("=" * 78)
    print(f"{'ID':<5}{'检查项'.ljust(width)}结果")
    print("-" * 78)
    failed = 0
    for cid, title, ok, detail in results:
        print(f"{cid:<5}{title.ljust(width)}{'PASS' if ok else 'FAIL'}")
        if detail:
            print(f"     └─ {detail}")
        if not ok:
            failed += 1
    print("=" * 78)
    print(f"合计 {len(results)} 项，通过 {len(results) - failed}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
