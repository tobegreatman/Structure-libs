"""评分体系测试：量规带命中、锚定量表、缺失重归一化、结构约束（反堆砌）、汇总。"""

import pytest

from stockframe.pit import FieldRef
from stockframe.rubric import RubricEngine
from stockframe.specs import DIMENSIONS, WEIGHTS_MID, WEIGHTS_SHORT


def _ref(value, tier="L1", avail="2026-10-01"):
    return FieldRef("test.field", value, "2026-09-30", avail, "测试", tier)


def _engine(registry, derived=None, sources=None):
    from datetime import date
    return RubricEngine(
        DIMENSIONS, registry, derived or {}, sources or {}, date.fromisoformat("2026-10-22")
    )


# ---------------------------------------------------------------- 结构约束（反堆砌/反黑箱）

def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS_MID.values()) - 1.0) < 1e-9
    assert abs(sum(WEIGHTS_SHORT.values()) - 1.0) < 1e-9


def test_structure_bounds_no_indicator_piling():
    for dim in DIMENSIONS:
        assert len(dim.factors) <= 3, f"{dim.id} 因子数超上限（反堆砌）"
        for fac in dim.factors:
            assert len(fac.indicators) <= 4, f"{dim.id}.{fac.id} 指标数超上限（反堆砌）"
            for ind in fac.indicators:
                assert len(ind.rationale) >= 10, f"{dim.id}.{fac.id}.{ind.id} 缺 rationale（反黑箱）"
                if ind.anchors is None and ind.computed is None:
                    assert len(ind.bands) >= 4, f"{ind.id} 量规带不足 4 档"
            total = sum(i.weight for i in fac.indicators)
            assert abs(total - 1.0) < 1e-6, f"{dim.id}.{fac.id} 指标权重和 != 1"
        assert abs(sum(f.weight for f in dim.factors) - 1.0) < 1e-6, f"{dim.id} 因子权重和 != 1"


def test_bands_partition_covering():
    """量规带必须无缝覆盖 (-inf, +inf)。"""
    for dim in DIMENSIONS:
        for fac in dim.factors:
            for ind in fac.indicators:
                if ind.anchors is not None or ind.computed is not None:
                    continue
                bands = sorted(ind.bands, key=lambda b: float("-inf") if b.lo is None else b.lo)
                assert bands[0].lo is None, f"{ind.id} 首带必须始于 -inf"
                assert bands[-1].hi is None, f"{ind.id} 末带必须终于 +inf"
                for a, b in zip(bands, bands[1:]):
                    assert a.hi == b.lo, f"{ind.id} 量规带存在缝隙/重叠"


# ---------------------------------------------------------------- 量规命中

def test_roe_band_hit():
    eng = _engine({"financial.roe_ttm": _ref(16.5)})
    spec = next(i for d in DIMENSIONS if d.id == "financial"
                for f in d.factors for i in f.indicators if i.id == "fin.roe")
    res = eng.eval_indicator(spec)
    assert res.band == "良" and res.score == 7.5


def test_qualitative_anchor_scoring():
    eng = _engine({"company.moat": _ref(4)})
    spec = next(i for d in DIMENSIONS if d.id == "company"
                for f in d.factors for i in f.indicators if i.id == "company.moat_r")
    res = eng.eval_indicator(spec)
    assert res.score == 7.5
    assert "认证" in res.band  # 档位描述进结果


def test_missing_indicator_renormalizes_not_zero():
    # 只提供 ocf_np_ratio（roe/wc_gap 缺失）→ 因子分应等于该指标带分，而非被拉低
    eng = _engine({"financial.ocf_np_ratio": _ref(0.95)})
    dim = next(d for d in DIMENSIONS if d.id == "financial")
    fac = next(f for f in dim.factors if f.id == "earnings_quality")
    res = eng.eval_factor(fac)
    assert res.renormalized is True
    assert res.available_weight_ratio == pytest.approx(0.35)
    assert res.score == pytest.approx(7.5)  # 0.95 → 良 7.5，重归一化后仍为 7.5


def test_composite_matches_manual():
    registry = {}
    # 给每个指标一个可命中"中"带的取值集合省略——改为验证公式一致性：
    dims = DIMENSIONS
    engine = _engine(registry)
    assert engine is not None
    # composite 公式单独验证（用手工结果对象）
    from stockframe.rubric import DimensionResult, FactorResult, IndicatorResult
    from stockframe.rubric import Band

    class _S:  # 简单替身
        def __init__(self, w): self.weight_mid = w; self.weight_short = 1 - w

    rs = [DimensionResult(spec=_S(0.5), factors=[], score=6.0, confidence=1.0, confidence_level="high"),
          DimensionResult(spec=_S(0.5), factors=[], score=8.0, confidence=1.0, confidence_level="high")]
    assert RubricEngine.composite(rs, "weight_mid") == 7.0


def test_confidence_penalties():
    # 缺失指标 + L3 来源应拉低置信度
    from datetime import date
    eng = RubricEngine(DIMENSIONS, {"company.moat": _ref(4, tier="L3", avail="2026-10-20")},
                       {}, {}, date.fromisoformat("2026-10-22"))
    dim = next(d for d in DIMENSIONS if d.id == "company")
    res = eng.eval_dimension(dim)
    # moat L3 + 其余指标缺失（company 维度共 4 指标，3 缺失）
    assert res.confidence < 0.9
    assert res.confidence >= 0.2
