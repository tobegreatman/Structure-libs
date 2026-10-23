"""端到端测试：样例输入 → 报告 → schema 校验；异常路径（未来函数/概率非法/必填缺失）。"""

import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from stockframe.engine import run  # noqa: E402
from stockframe.validate import validate_report  # noqa: E402

with open(os.path.join(ROOT, "examples", "sample_input.json"), encoding="utf-8") as fh:
    SAMPLE = json.load(fh)


def _sample():
    return copy.deepcopy(SAMPLE)


def test_full_pipeline_validates():
    report = run(_sample())
    ok, errors = validate_report(report)
    assert ok, errors


def test_report_invariants():
    r = run(_sample())
    # 四桶语义
    assert r["conclusion"]["bucket_mid"] in ("入池", "观察", "回避", "人工复核")
    # 决策可溯源：命中规则 + 理由
    assert r["conclusion"]["decision"]["rule_id"].startswith("R")
    assert r["conclusion"]["decision"]["reasons"]
    # 结论条件化 + 证伪
    assert len(r["conclusion"]["maintain_conditions"]) >= 3
    assert len(r["conclusion"]["falsification"]) >= 3
    # 评分可拆解：每指标带量规带与证据
    for d in r["scorecard"]["dimensions"]:
        for f in d["factors"]:
            for i in f["indicators"]:
                assert i["rationale"]
                if not i["missing"]:
                    assert i["band"] is not None and i["score"] is not None
    # 跟踪/催化剂/风险矩阵规模
    assert len(r["tracking_checklist"]) >= 4
    assert len(r["risk_matrix"]["risks"]) >= 3
    assert len(r["evidence_log"]) >= 5
    # 免责声明固定文案
    assert "不构成投资建议" in r["disclaimer"]


def test_lookahead_input_rejected():
    inp = _sample()
    inp["data"]["financial"]["roe_ttm"]["available_at"] = "2026-12-31"
    from stockframe.pit import LookAheadError
    with pytest.raises(LookAheadError, match="roe_ttm"):
        run(inp)


def test_scenario_probability_must_sum_one():
    inp = _sample()
    inp["assumptions"]["scenario_probabilities"]["optimistic"] = 0.5
    with pytest.raises(ValueError, match="概率"):
        run(inp)


def test_missing_required_field_forces_manual_review():
    inp = _sample()
    del inp["data"]["market"]["pe_ttm"]
    r = run(inp)
    assert r["data_quality"]["forced_manual_review"] is True
    assert r["conclusion"]["bucket_mid"] == "人工复核"
    # 缺失指标未静默补零：PEG 所在因子（expectation_match）应记录重归一化
    val_dim = next(d for d in r["scorecard"]["dimensions"] if d["id"] == "valuation")
    em = next(f for f in val_dim["factors"] if f["id"] == "expectation_match")
    assert em["renormalized"] is True and em["available_weight_ratio"] == 0.0
    peg_ind = next(i for f in val_dim["factors"] for i in f["indicators"] if i["id"] == "val.peg")
    assert peg_ind["missing"] is True and peg_ind["score"] is None
    # 缺失不外溢：分位类指标仍可用，所在因子不重归一化
    rel = next(f for f in val_dim["factors"] if f["id"] == "rel_position")
    assert rel["renormalized"] is False
    # 相对估值整体不可用 → 合成区间退化为 DCF 单法，且不补零
    assert r["valuation"]["relative"]["available"] is False
    assert r["valuation"]["synthesis"]["mode"] == "dcf_only"
    assert r["valuation"]["synthesis"]["relative_range"]["low"] is None


def test_hard_flag_routes_to_avoid():
    inp = _sample()
    inp["meta"]["flags"]["st"] = True
    r = run(inp)
    assert r["conclusion"]["bucket_mid"] == "回避"


def test_catalyst_past_date_rejected_end_to_end():
    inp = _sample()
    inp["catalysts"][0]["date"] = "2026-01-01"
    with pytest.raises(ValueError):
        run(inp)


def test_no_buy_sell_language_in_report():
    r = run(_sample())
    text = json.dumps(r, ensure_ascii=False)
    for banned in ("买入", "卖出", "加仓", "减仓", "目标价"):
        assert banned not in text, f"报告出现违禁操作词：{banned}"
