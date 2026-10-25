"""门卫的回归测试：`tools/check_logic_text.py` 拦不拦得住网页版 AI 的四类典型坏稿。

为什么要给它写测试：这个脚本的价值**全在误报与漏报**上。

- 漏报（坏稿放行）→ 半句话被截断、空话进结论、编造的字段 ID 混进证伪条件，
  而引擎不会报错（它只把 `verify_metric` 当字符串拼进文本），错得很安静；
- 误报（好稿被判死）→ 用户会绕开这道门，等于没有门。

所以这里各钉一个正样本和四类负样本，外加一条"违禁词表必须与验收脚本同源"——
两套词表各自漂移过一次（`点位/仓位` 只在验收侧被隐含覆盖），才加的这条。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("check_logic_text", ROOT / "tools" / "check_logic_text.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

GOOD = (
    "买的是人形机器人零部件从 0→1 的份额兑现：β 是整机量产带来的需求，α 取决于能否守住 30% "
    "以上份额与年降条款；兑现看机器人业务季度收入增速 ≥50%、毛利率环比 +0.3pp；若连续两个季度"
    "收入增速跌破 30% 或毛利率环比转负，则逻辑失效、公司维度重估。"
)


def _levels(results):
    return {item: level for level, item, _ in results}


def test_good_sample_has_no_fail():
    levels = _levels(chk.check_text(GOOD))
    assert "FAIL" not in levels.values(), levels


def test_over_limit_text_fails():
    """超 200 字必须在进管线前拦下：界面 maxlength 会把句子截成半句。"""
    long_text = GOOD * 3
    res = dict((i, (l, d)) for l, i, d in chk.check_text(long_text))
    assert res["字数"][0] == "FAIL"
    assert str(chk.MAX_CHARS) in res["字数"][1]


def test_empty_talk_fails_on_substance_not_tone():
    """空话样本：字数合格、也没有违禁词，但四类实质检查必须同时红。"""
    bad = ("公司受益于行业景气，作为龙头地位稳固，护城河深，估值具备安全边际，"
           "市场尚未充分认知其价值，未来两年有望迎来业绩与估值双升。")
    levels = _levels(chk.check_text(bad))
    for item in ("兑现路径", "证伪条件", "空话", "数字锚"):
        assert levels[item] == "FAIL", (item, levels)
    # "公司受益于行业景气"只是方向不是来源，不能算拆过 β/α
    assert levels["收益来源"] == "FAIL", levels


def test_trading_instruction_fails():
    levels = _levels(chk.check_text(GOOD + "，建议目标价 45 元买入"))
    assert levels["交易指令"] == "FAIL"


def test_logic_items_reject_fabricated_field_id():
    """编出来的字段 ID 是最安静的错误：引擎照单全收，只是对上不账。"""
    bad = [{"statement": "s", "transmission": "t", "verify_metric": "revenue_growth_qoq",
            "verify_threshold": "≥ 20%", "source": "AI 草稿"}]
    res = chk.check_items(bad)
    assert any(l == "FAIL" and "不是字典字段" in d for l, _, d in res), res

    ok = [{"statement": "s", "transmission": "t", "verify_metric": "financial.rev_growth",
           "verify_threshold": "≥ 20%", "source": "研究底稿"}]
    assert all(l != "FAIL" for l, _, _ in chk.check_items(ok)), chk.check_items(ok)


def test_logic_items_require_verifiable_threshold():
    items = [{"statement": "s", "transmission": "t", "verify_metric": "毛利率环比",
              "verify_threshold": "增长较快", "source": "草稿"}]
    assert any(l == "FAIL" and "没有阈值" in d for l, _, d in chk.check_items(items))


def test_banned_words_in_sync_with_acceptance():
    """两套违禁词表必须逐字一致，否则会出现"脚本放行、验收拦下"的分裂。"""
    src = (ROOT / "tools" / "acceptance.py").read_text(encoding="utf-8")
    m = re.search(r"BANNED = \(([^)]*)\)", src)
    assert m, "acceptance.py 里找不到 BANNED 常量"
    assert tuple(re.findall(r'"([^"]+)"', m.group(1))) == chk.BANNED
