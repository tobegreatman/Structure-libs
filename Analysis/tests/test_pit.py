"""PIT 守卫测试：三时点模型、未来函数拦截。"""

import pytest

from stockframe.pit import LookAheadError, ProvenanceError, check_field, validate_all_fields

ANALYSIS = "2026-10-22"


def _field(available_at="2026-08-28", as_of="2026-06-30", **kw):
    base = {"value": 1.0, "as_of": as_of, "available_at": available_at,
            "source": "测试来源", "source_tier": "L1"}
    base.update(kw)
    return base


def test_valid_field_passes():
    from datetime import date
    ref = check_field("financial.roe_ttm", _field(), date.fromisoformat(ANALYSIS))
    assert ref.value == 1.0
    assert ref.source_tier == "L1"


def test_lookahead_available_at_rejected():
    from datetime import date
    with pytest.raises(LookAheadError, match="未来函数"):
        check_field("financial.roe_ttm", _field(available_at="2026-12-31"), date.fromisoformat(ANALYSIS))


def test_lookahead_as_of_rejected():
    from datetime import date
    with pytest.raises(LookAheadError):
        check_field("market.price_close", _field(available_at="2026-10-25", as_of="2026-10-25"),
                    date.fromisoformat(ANALYSIS))


def test_missing_value_or_source_rejected():
    from datetime import date
    with pytest.raises(LookAheadError):
        check_field("x.y", {"as_of": ANALYSIS, "available_at": ANALYSIS}, date.fromisoformat(ANALYSIS))
    with pytest.raises(LookAheadError):
        check_field("x.y", {"value": 1, "as_of": ANALYSIS, "available_at": ANALYSIS,
                            "source": "s", "source_tier": "L9"}, date.fromisoformat(ANALYSIS))


def test_validate_all_fields_aggregates():
    from datetime import date
    data = {
        "market": {"price_close": _field(as_of=ANALYSIS, available_at=ANALYSIS)},
        "financial": {"roe_ttm": _field()},
    }
    registry = validate_all_fields(data, date.fromisoformat(ANALYSIS))
    assert set(registry) == {"market.price_close", "financial.roe_ttm"}
    # 报告期(6-30)早于可得时点(8-28)：披露滞后是正常 PIT 形态
    assert registry["financial.roe_ttm"].available_at == "2026-08-28"


def test_l4_source_rejected_for_scoring():
    """L4（媒体/传闻）不得进入评分路径——设计文档 §4.2 硬规则。"""
    from datetime import date
    with pytest.raises(ProvenanceError, match="L4"):
        check_field("financial.roe_ttm", _field(source_tier="L4"), date.fromisoformat(ANALYSIS))


def test_unknown_tier_rejected():
    from datetime import date
    with pytest.raises(LookAheadError, match="source_tier"):
        check_field("financial.roe_ttm", _field(source_tier="L9"), date.fromisoformat(ANALYSIS))
