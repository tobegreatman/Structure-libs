"""结论翻转点（sensitivity）的门卫。

守的是四件事：
1. 报出来的位移**真的**翻桶，且少 0.01 就不翻（否则界面上的"−2.13"是编的）；
2. 机制命名与 decision.py 的规则表一致，尤其"维度下限 4.0 先于 R5"这条叠加事实；
3. 桶由规则决定（人工复核/回避）时不出位移，不假装能算；
4. 这段话不越 A4：只谈分数与桶，不谈价格与动作。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockframe import sensitivity as S
from stockframe.report import render_markdown
from stockframe.wording import scan

ROOT = Path(__file__).resolve().parents[1]


def _sample():
    return json.loads((ROOT / "examples" / "sample_report.json").read_text(encoding="utf-8"))


def _rows(report=None):
    r = report or _sample()
    w = r["scorecard"]["weights"]["mid_term"]
    judge = {"macro", "industry", "company"}
    return [{"id": d["id"], "name": d["name"], "weight": w[d["id"]],
             "score": d["score_mid"], "anchored": d["id"] in judge}
            for d in r["scorecard"]["dimensions"]]


def _rows_from(scores, weights=None, judge=()):
    weights = weights or {"a": 0.5, "b": 0.5}
    return [{"id": k, "name": k.upper(), "weight": weights[k], "score": v,
             "anchored": k in judge} for k, v in scores.items()]


# ------------------------------------------------------------------ 位移正确性

def test_sample_every_delta_actually_flips_and_minus_001_does_not():
    r = _sample()
    rows, bucket = _rows(r), r["conclusion"]["bucket_mid"]
    sens = r["conclusion"]["sensitivity"]
    assert sens["drivers"], "样例应至少有一个可翻维度"
    for d in sens["drivers"]:
        flipped = S.bucket_with(rows, d["id"], d["score"] - d["delta"])
        assert flipped == d["bucket_after"] != bucket, d
        # 少翻 0.01 必须仍在原桶：这是"最小位移"的定义
        assert S.bucket_with(rows, d["id"], d["score"] - d["delta"] + 0.01) == bucket, d


def test_drivers_sorted_by_delta_ascending():
    sens = _sample()["conclusion"]["sensitivity"]
    deltas = [d["delta"] for d in sens["drivers"]]
    assert deltas == sorted(deltas)


def test_distance_to_entry_line_is_reported():
    r = _sample()
    c = r["conclusion"]
    s = c["sensitivity"]
    assert s["boundary"] == 7.0 and s["next_bucket"] == "观察"
    assert s["distance"] == pytest.approx(round(c["composite_score_mid"] - 7.0, 2))


# ------------------------------------------------------------------ 机制

def test_dim_floor_precedes_signal_conflict():
    """估值 6.12：跌破 4.0 只要 −2.13，R5 要 −3.12。首个机制必须是最早那个。"""
    r = _sample()
    sens = r["conclusion"]["sensitivity"]
    val = [d for d in sens["drivers"] if d["id"] == "valuation"][0]
    assert val["mechanism"] == "dim_floor"
    assert val["score_after"] < S.DIM_MIN_FOR_ENTRY
    assert val["delta"] < val["score"] - S.R5_LINE  # 比 R5 更早


def test_any_dim_below_floor_blocks_entry_even_with_high_weight_neighbours():
    """构造：综合分 8.0 满仓入池，某一低权重维度 4.2 → 只要 −0.21 就掉出。"""
    weights = {"a": 0.8, "b": 0.1, "c": 0.1}
    rows = _rows_from({"a": 8.0, "b": 8.0, "c": 4.2}, weights)
    sens = S.analyze(rows, 8.0, "入池")
    c_row = [d for d in sens["drivers"] if d["id"] == "c"][0]
    assert c_row["mechanism"] == "dim_floor"
    assert c_row["delta"] == pytest.approx(0.21, abs=0.02)
    # 而权重最大的 a 要掉到 4.0 以下才翻，位移比 c 大得多
    a_row = [d for d in sens["drivers"] if d["id"] == "a"][0]
    assert a_row["delta"] > c_row["delta"]
    assert a_row["bucket_after"] == "观察"


def test_signal_conflict_is_never_the_first_flip():
    """R5（≤3.0）必然晚于维度下限（4.0），这条事实写进测试，防止将来误改。"""
    rows = _rows_from({k: 8.0 for k in ("valuation", "risk", "financial", "x", "y")},
                      {"valuation": 0.3, "risk": 0.1, "financial": 0.1, "x": 0.25, "y": 0.25})
    sens = S.analyze(rows, 8.0, "入池")
    for d in sens["drivers"]:
        assert d["mechanism"] != "signal_conflict"


# ------------------------------------------------------------------ 桶不可算的情形

def test_manual_review_bucket_has_no_drivers():
    r = _sample()
    sens = S.analyze(_rows(r), r["conclusion"]["composite_score_mid"], "人工复核")
    assert sens["drivers"] == []
    assert "R1–R4" in sens["note"] and "不由分数决定" in sens["note"]


def test_avoid_bucket_has_no_drivers():
    sens = S.analyze(_rows_from({"a": 3.0, "b": 3.0}), 3.0, "回避")
    assert sens["drivers"] == []
    assert sens["boundary"] is None and sens["next_bucket"] is None


def test_watch_bucket_boundary_is_avoid_line_not_five():
    """观察桶里跌破 5.0 仍是观察，真正的翻转线是 3.5。"""
    sens = S.analyze(_rows_from({"a": 6.0, "b": 6.0}), 6.0, "观察")
    assert sens["boundary"] == 3.5 and sens["next_bucket"] == "回避"


def test_zero_weight_dim_is_insensitive_and_unscored_is_separate():
    """观察桶下，权重 0 的维度再怎么掉都不改桶；未评分维度单列，不与"翻不动"混为一谈。"""
    rows = _rows_from({"a": 6.0, "b": 6.0, "z": 9.0, "n": None},
                      {"a": 0.5, "b": 0.5, "z": 0.0, "n": 0.0})
    sens = S.analyze(rows, 6.0, "观察")
    assert "z" in sens["insensitive"]
    assert sens["unscored"] == ["n"]
    assert all(d["id"] != "n" for d in sens["drivers"])


def test_zero_weight_dim_still_bites_through_dim_floor():
    """入池时维度下限看的是"所有已评维度的最低分"，与权重无关：
    权重 0 的维度掉到 4.0 以下同样把入池打成观察。这条容易被当成 bug 改掉，先钉住。"""
    rows = _rows_from({"a": 8.0, "b": 8.0, "z": 9.0},
                      {"a": 0.5, "b": 0.5, "z": 0.0})
    sens = S.analyze(rows, 8.0, "入池")
    z = [d for d in sens["drivers"] if d["id"] == "z"][0]
    assert z["mechanism"] == "dim_floor"
    assert z["delta"] == pytest.approx(z["score"] - S.DIM_MIN_FOR_ENTRY, abs=0.02)


def test_unknown_dim_raises():
    with pytest.raises(KeyError):
        S.bucket_with(_rows(), "no_such_dim", 5.0)


# ------------------------------------------------------------------ 方向：桶内位置与向上空间

def test_band_position_tells_how_close_to_the_line():
    """7.64 在入池 7.0–10 里只走了 21%：数字要能读出"刚过线"，而不是只看"距线 0.64"。"""
    s = _sample()["conclusion"]["sensitivity"]
    assert s["band"]["low"] == 7.0 and s["band"]["high"] == 10.0
    assert s["band"]["position"] == pytest.approx((7.64 - 7.0) / 3.0, abs=0.002)
    assert s["band"]["zone"] == "下沿"


def test_entry_bucket_is_top_so_there_is_no_upside_grade():
    """入池是最高档：只报向下多远会让人以为上面还有空间，必须同时说 at_top。"""
    s = _sample()["conclusion"]["sensitivity"]
    assert s["headroom"]["at_top"] is True
    assert s["headroom"]["room"] is None and s["headroom"]["next_up"] is None
    assert "向上已无更优档位" in s["note"]


def test_watch_and_avoid_buckets_report_room_to_next_grade_up():
    watch = S.analyze(_rows_from({"a": 6.0, "b": 6.0}), 6.0, "观察")
    assert watch["headroom"] == {"at_top": False, "ceiling": 7.0, "room": 1.0, "next_up": "入池"}
    assert watch["band"]["low"] == 3.5 and watch["band"]["high"] == 7.0
    assert watch["band"]["zone"] == "上沿"          # 6.0 在 3.5–7.0 里走到 71%

    avoid = S.analyze(_rows_from({"a": 3.0, "b": 3.0}), 3.0, "回避")
    assert avoid["headroom"]["ceiling"] == 3.5 and avoid["headroom"]["room"] == 0.5
    assert avoid["headroom"]["next_up"] == "观察"


def test_distance_in_anchors_uses_the_same_2_5_step():
    s = _sample()["conclusion"]["sensitivity"]
    assert s["distance_anchors"] == pytest.approx(0.64 / 2.5, abs=0.01)
    # 一档 2.5 分是锚点换算的唯一定义，改了这里必须改 rubric
    assert S.ANCHOR_STEP == 2.5


def test_manual_review_has_no_band_and_no_headroom():
    r = _sample()
    sens = S.analyze(_rows(r), r["conclusion"]["composite_score_mid"], "人工复核")
    assert sens["band"] == {"low": None, "high": None, "position": None, "zone": None}
    assert sens["headroom"] == {"at_top": False, "ceiling": None, "room": None, "next_up": None}
    assert sens["distance_anchors"] is None


# ------------------------------------------------------------------ 出口

def test_markdown_report_has_flip_table():
    md = (ROOT / "examples" / "sample_report.md").read_text(encoding="utf-8")
    assert "结论翻转点" in md
    assert "翻桶时得分" in md
    assert "维度跌破 4.0" in md


def test_flip_note_has_no_trading_instruction():
    text = " ".join([_sample()["conclusion"]["sensitivity"]["note"],
                     render_markdown(_sample())[:4000]])
    assert scan(text) == []
