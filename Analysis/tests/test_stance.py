"""立场句：把分桶翻译成"现在该不该动手"。

这一层的存在理由很具体：读者拿到报告问的第一个问题是"那到底该不该买"，
而分桶名（观察/入池/回避/人工复核）对非专业读者不构成答案。于是引擎在
conclusion 里多给一段 stance，四档与四个桶一一对应。

三条不能破的边界，全部钉在这里：
1. **它不新增判定**。stance.code 只能由 bucket 决定，不允许出现"第五种桶"。
2. **它不含交易指令**。整段必须过 wording.scan（A4 同源词表），
   这是最像投资建议的一段，反而最需要闸门。
3. **它必须诚实**。"入池"不许说成买点；"观察"必须点明"分数不会因为股价下跌自己变高"。
"""

from __future__ import annotations

from stockframe.engine import _stance
from stockframe.wording import scan


def sens(distance=3.34, room=0.16, zone="上沿", at_top=False, boundary=3.5,
         next_bucket="回避", next_up="入池"):
    return {
        "boundary": boundary,
        "distance": distance,
        "distance_anchors": 1.34,
        "next_bucket": next_bucket,
        "band": {"low": 3.5, "high": 7.0, "position": 0.954, "zone": zone},
        "headroom": {"at_top": at_top, "ceiling": 7.0 if not at_top else 10.0,
                     "room": None if at_top else room,
                     "next_up": None if at_top else next_up},
        "drivers": [],
        "insensitive": [],
        "unscored": [],
        "note": "任何单一维度归零都不改变分桶",
    }


def dq(n=4):
    return {"gaps": [{"field": f"x.{i}", "reason": "未提供"} for i in range(n)]}


FALSIFY = [{"id": "F1", "condition": "逻辑证伪：若 ROE 不再满足 ≥17.8% → 财务维度重估",
            "metric": "financial.roe_ttm", "threshold": "≥ 17.8%"}]


def build(bucket, score=6.84, rule="R6_SCORE_BAND", reasons=None, **kw):
    return _stance(
        bucket, score, rule, reasons or ["中线综合分 6.84 处于 5.0-7.0 观察带"],
        sens(**kw), dq(), 0.807, {"judgement": 0.44, "data": 0.42, "risk_catalyst": 0.14},
        {"dcf_applicable": True}, {"divergence_pct": 14.4, "dcf_margin_pct": 16.8,
                                   "relative_margin_pct": 3.1},
        FALSIFY,
    )


# ---------------------------------------------------------------- 四档对应

def test_four_buckets_map_to_four_stances():
    """四个桶一一对应四档立场，不出现第五种。"""
    pairs = [("人工复核", "insufficient"), ("回避", "avoid"),
             ("观察", "hold_off"), ("入池", "research")]
    for bucket, code in pairs:
        assert build(bucket)["code"] == code, f"{bucket} 应映射到 {code}"


def test_hold_off_answers_the_question_directly():
    """观察档必须把"现在该不该动手"答成一句明确的话，而不是只报分数。"""
    st = build("观察")
    assert st["label"] == "暂不介入"
    assert "0.16" in st["headline"] and "不是动手的位置" in st["headline"]
    assert "继续看着" in st["basis"] and "可以动手" in st["basis"]


def test_hold_off_kills_the_wait_for_a_dip_fallacy():
    """最常见的误读是"等跌了分数就够格"——分数衡量质量与估值，不是涨跌空间。"""
    st = build("观察")
    joined = " ".join(st["do_next"])
    assert "不会因为股价下跌自己变高" in joined


def test_research_never_reads_as_a_buy_signal():
    """入池 ≠ 现在买。这句必须写进 basis，否则整段会被当成买入提示。"""
    st = build("入池", score=7.64)
    assert st["label"] == "够格研究"
    assert "不包含「价格合适」" in st["basis"] or "不代表" in st["basis"]
    assert "研究顺序" in st["basis"]


def test_avoid_states_the_line_it_broke():
    """回避档要说清"跌破哪条线"，并明确这不是"便宜"。"""
    st = build("回避", score=3.1, distance=-0.4)
    assert st["label"] == "按框架规避"
    assert "3.5" in st["headline"]
    assert "不是「便宜」" in st["basis"]


def test_manual_review_says_the_ruler_does_not_fit():
    """人工复核档最容易让人误会成"这票很差"，必须写成"这套尺子量不出来"。"""
    st = build("人工复核", score=None, rule="R9_MODEL_NOT_APPLICABLE",
               reasons=["银行/保险没有可比估值口径，本框架不出价位"])
    assert st["label"] == "本框架不下结论"
    assert "这套尺子量不出来" in st["headline"]
    assert "不是「不好」" in st["basis"]
    assert "—" not in st["headline"], "没分数就不要在标题里留一个破折号占位"


# ---------------------------------------------------------------- 边界

def test_abandon_points_at_the_falsification_id():
    """"什么时候必须重看"要给出可追溯的编号，而不是一句"注意风险"。"""
    st = build("观察")
    assert "3.5" in st["abandon"] and "F1" in st["abandon"]


def test_caveat_explains_why_there_is_no_price_call():
    """能力边界必须写：读者才知道该拿它做什么用，而不是以为系统在打太极。"""
    for bucket in ("人工复核", "回避", "观察", "入池"):
        st = build(bucket)
        assert "回测" in st["caveat"] and "涨跌概率" in st["caveat"]
        assert "够不够格" in st["caveat"]


def test_all_stance_texts_pass_the_wording_gate():
    """立场句是最像投资建议的一段，四档全文都必须过 A4 词表。"""
    for bucket in ("人工复核", "回避", "观察", "入池"):
        st = build(bucket)
        texts = [st["label"], st["headline"], st["basis"], st["abandon"], st["caveat"]] + st["do_next"]
        for t in texts:
            assert scan(t) == [], f"{bucket} 的立场句含交易指令措辞：{t}"


def test_stance_is_required_by_the_schema():
    """缺了它就退回"只报分数"，所以 schema 里必须是必填项。"""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schema" / "report_schema.json").read_text(encoding="utf-8"))
    concl = schema["properties"]["conclusion"]
    assert "stance" in concl["required"]
    assert concl["properties"]["stance"]["additionalProperties"] is False
    assert set(concl["properties"]["stance"]["required"]) == {
        "code", "label", "headline", "basis", "do_next", "abandon", "caveat"}


def test_markdown_report_renders_the_stance_first():
    """报告正文也要先给立场，再给分桶——否则导出 MD 的人又回到"只看到 6.84 分"。"""
    import json
    from pathlib import Path

    from stockframe.engine import run
    from stockframe.report import render_markdown

    root = Path(__file__).resolve().parents[1]
    inp = json.loads((root / "examples" / "sample_input.json").read_text(encoding="utf-8"))
    md = render_markdown(run(inp))
    assert "### 立场：" in md
    assert md.index("### 立场：") < md.index("**中线分桶："), "立场必须排在分桶之前"
    assert "什么时候必须重看" in md and "能力边界" in md
