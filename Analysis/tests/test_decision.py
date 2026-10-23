"""决策映射与催化剂/风险矩阵测试。"""

import pytest

from stockframe.catalysts import build_calendar, density_score, next_event_score
from stockframe.decision import decide
from stockframe.riskmatrix import build_matrix, matrix_score


def _dq(forced=False, notes=None):
    return {"forced_manual_review": forced, "notes": notes or []}


def _meta(flags=None, red=0):
    return {"flags": flags or {}, "red_flag_count": red}


DIMS = {"macro": 7.0, "industry": 7.0, "company": 7.0, "financial": 7.0,
        "valuation": 7.0, "catalyst": 7.0, "trading": 7.0, "risk": 7.0}


def test_score_band_pool():
    r = decide(_meta(), _dq(), dict(DIMS), 7.2, 7.2)
    assert r["bucket_mid"] == "入池"
    assert r["rule_id"] == "R6_SCORE_BAND"


def test_score_band_pool_blocked_by_weak_dimension():
    dims = dict(DIMS, valuation=3.9)
    r = decide(_meta(), _dq(), dims, 7.2, 7.2)
    assert r["bucket_mid"] == "观察"  # 有维度 <4.0 → 不满足入池


def test_score_band_watch_and_avoid():
    assert decide(_meta(), _dq(), dict(DIMS), 6.0, 6.0)["bucket_mid"] == "观察"
    assert decide(_meta(), _dq(), dict(DIMS), 3.9, 3.9)["bucket_mid"] == "观察"
    assert decide(_meta(), _dq(), dict(DIMS), 3.4, 3.4)["bucket_mid"] == "回避"


def test_hard_flag_forces_avoid():
    r = decide(_meta(flags={"st": True}), _dq(), dict(DIMS), 8.0, 8.0)
    assert r["bucket_mid"] == "回避"
    assert r["rule_id"] == "R1_HARD_FLAG"
    assert any("硬红旗" in x for x in r["reasons"])


def test_red_flags_force_avoid():
    r = decide(_meta(red=2), _dq(), dict(DIMS), 8.0, 8.0)
    assert r["bucket_mid"] == "回避"
    assert r["rule_id"] == "R2_RED_FLAGS"


def test_dq_forces_manual_review():
    r = decide(_meta(), _dq(forced=True, notes=["必填字段缺失"]), dict(DIMS), 8.0, 8.0)
    assert r["bucket_mid"] == "人工复核"
    assert r["rule_id"] == "R3_DQ_FORCED"
    assert "必填字段缺失" in r["reasons"][0]


def test_conflict_forces_manual_review():
    dims = dict(DIMS, valuation=2.5)
    r = decide(_meta(), _dq(), dims, 7.5, 7.5)
    assert r["bucket_mid"] == "人工复核"
    assert r["rule_id"] == "R5_SIGNAL_CONFLICT"
    dims2 = dict(DIMS, financial=2.5)
    r2 = decide(_meta(), _dq(), dims2, 6.0, 6.0)  # composite 6 <7 不触发主信号
    assert r2["bucket_mid"] == "观察"


def test_special_status_manual_review():
    r = decide(_meta(flags={"restructuring": True}), _dq(), dict(DIMS), 8.0, 8.0)
    assert r["bucket_mid"] == "人工复核"
    assert r["rule_id"] == "R4_SPECIAL_STATUS"


# ---------------------------------------------------------------- 风险矩阵

RISKS = [
    {"id": "R1", "name": "大客户资本开支放缓", "category": "customer_concentration",
     "probability": 3, "impact": 4, "trigger": {"metric": "资本开支", "threshold": "转负"}},
    {"id": "R2", "name": "量产不及预期", "category": "demand",
     "probability": 3, "impact": 3, "trigger": {"metric": "机器人收入增速", "threshold": "<50%"}},
    {"id": "R3", "name": "价格竞争", "category": "competition",
     "probability": 2, "impact": 3, "trigger": {"metric": "单价年降", "threshold": ">5%"}},
]


def test_risk_matrix_levels():
    m = build_matrix(RISKS)
    by_id = {r["id"]: r for r in m["risks"]}
    assert by_id["R1"]["score"] == 12 and by_id["R1"]["level"] == "中"
    assert by_id["R2"]["score"] == 9 and by_id["R2"]["level"] == "中"
    assert by_id["R3"]["score"] == 6 and by_id["R3"]["level"] == "低"
    assert m["top_risk_ids"][0] == "R1"
    assert sum(sum(row) for row in m["grid"]["counts"]) == 3


def test_matrix_score_formula():
    # P×I: 12, 9, 6 → penalty = 4+1+0 = 5 → 10×(1−5/20) = 7.5
    assert matrix_score(RISKS) == 7.5


def test_matrix_validates_inputs():
    bad = [dict(RISKS[0], probability=7)]
    with pytest.raises(ValueError):
        build_matrix(bad)


# ---------------------------------------------------------------- 催化剂

from datetime import date

CATS = [
    {"date": "2026-10-30", "event": "三季报", "type": "earnings", "direction": "+",
     "strength": "强", "priced_in": "部分定价"},
    {"date": "2026-12-15", "event": "行业峰会", "type": "industry", "direction": "+",
     "strength": "中", "priced_in": "未定价"},
    {"date": "2026-11-01", "event": "竞对发布", "type": "industry", "direction": "-",
     "strength": "弱", "priced_in": "未定价"},
]


def test_catalyst_past_date_rejected():
    cats = [dict(CATS[0], date="2026-01-01")]
    with pytest.raises(ValueError, match="早于分析时点"):
        build_calendar(cats, date.fromisoformat("2026-10-22"))


def test_priced_in_good_news_warns():
    cats = [dict(CATS[0], priced_in="已定价")]
    cal, stats = build_calendar(cats, date.fromisoformat("2026-10-22"))
    assert cal[0]["warning"] and "兑现" in cal[0]["warning"]
    assert next_event_score(stats) == 4.0  # 强 10 × 0.4


def test_density_and_next_event():
    cal, stats = build_calendar(CATS, date.fromisoformat("2026-10-22"))
    assert density_score(stats) == 7.5  # 强4+中2.5+弱1
    # 30 日内最近事件：三季报(10-30) 强 → 10
    assert next_event_score(stats) == 10.0


def test_unverified_rumor_downgraded():
    cats = [dict(CATS[0], type="unverified_rumor", strength="强")]
    cal, stats = build_calendar(cats, date.fromisoformat("2026-10-22"))
    assert density_score(stats) == 0.0  # 噪音不计分
    assert cal[0]["warning"] and "噪音" in cal[0]["warning"]
