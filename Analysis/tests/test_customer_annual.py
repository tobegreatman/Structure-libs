"""年报客户集中度：解析器 + 填充分支。

这里的所有文本来都直接抄自真实年报（pdftotext 抽出来、空白压平后的样子），
不是照着正则反推的假数据。四类披露形态各留一份，因为它们的处理方式不同：

- 标准表：单客户占比可得 → 观测值（L1）
- 文字段：给"第一名 X%"→ 可得，但分母可能是主营业务收入
- 只有前五合计：**不是最大客户**，只是上界 → 不填值
- 无此披露：银行/保险不适用 → 合理缺失

解析器的立场是「宁可判不认，不许填错数」，所以交叉校验失败的样品专门留了一例。
"""

from __future__ import annotations

import pytest

from stockframe import autobuild
from stockframe.sources.cninfo import (
    CustomerDisclosure,
    normalize,
    parse_customer_section,
)

from test_autobuild import raw_fixture

ASOF = "2026-10-23"
FID = "company.customer_concentration"

# 宁德时代 2025 年报第 28 页（金额单位千元）
NINGDE = """
（8） 主要销售客户和主要供应商情况
公司主要销售客户情况
前五名客户合计销售金额（千元） 165,061,533
前五名客户合计销售金额占年度销售总额比例 38.96%
前五名客户销售额中关联方销售额占年度销售总额比例 0.00%
公司前 5大客户资料
序号 客户名称 销售额（千元） 占年度销售总额比例
1 第一名 58,159,202 13.73%
2 第二名 47,127,609 11.12%
3 第三名 30,201,701 7.13%
4 第四名 15,419,319 3.64%
5 第五名 14,153,702 3.34%
合计 -- 165,061,533 38.96%
主要客户其他情况说明
□适用 不适用
"""

# 海康威视 2025 年报（金额单位元，客户极度分散）
HIKVISION = """
公司主要销售客户情况
前五名客户合计销售金额（元） 1,851,486,448.34
前五名客户合计销售金额占年度销售总额比例 2.00%
公司前 5 大客户资料
序号 客户名称 销售额（元） 占年度销售总额比例
1 第一名 449,589,548.10 0.49%
2 第二名 395,185,518.54 0.43%
3 第三名 394,746,778.13 0.43%
4 第四名 315,315,592.53 0.34%
5 第五名 296,649,011.04 0.32%
合计 -- 1,851,486,448.34 2.00%
"""

# 格力电器 2025 年报：表格被分页切断，本页只有 4 行，第 5 名在下一页
GREE_PARTIAL = """
公司前 5大客户资料
序号 客户名称 销售额（元） 占年度销售总额比例
1 第一名 10,119,088,327.50 5.91%
2 第二名 5,922,757,391.80 3.46%
3 第三名 3,999,596,480.24 2.34%
4 第四名 2,478,843,997.90 1.45%
合计 -- 24,647,540,609.21 14.40%
"""

# 金山办公 2025 年报：单位写在表头（万元），且多一列「是否关联方」
KINGSOFT = """
公司主要销售客户情况
前五名客户销售额64,845.20万元，占年度销售总额10.94%；其中前五名客户销售额中关联方
销售额0万元，占年度销售总额0%。
公司前五名客户
单位：万元 币种：人民币
序号 客户名称 销售额 占年度销售总额比例（%） 是否与上市公司存在关联关系
1 第一名 23,949.60 4.04 否
2 第二名 20,479.94 3.45 否
3 第三名 9,087.05 1.53 否
4 第四名 6,199.35 1.05 否
5 第五名 5,129.26 0.87 否
合计 / 64,845.20 10.94 /
"""

# 贵州茅台 2025 年报：只有一段文字，只有前五合计，没有单客户明细
MOUTAI = """
A.公司主要销售客户及主要供应商情况
√适用 □不适用
前五名客户销售额1,704,578.78万元，占年度销售总额10.10%；其中前五名客户销售额中关联方
销售额635,829.28万元，占年度销售总额3.77%。
前五名供应商采购额348,825.84万元，占年度采购总额38.12%；其中前五名供应商采购额中关联
方采购额139,283.25万元，占年度采购总额15.22%。
"""

# 中芯国际 2025 年报：文字段里直接给了"第一名"，但分母是主营业务收入
SMIC = """
公司主要销售客户情况
√适用 □不适用
本报告期内，第一名和前五名客户销售额分别占年度主营业务收入的 8.0%和 35.8%；其中前
五名客户交易中无关联方销售。
"""

# 工商银行 2025 年报：正文里出现"前五名客户"的页其实是财务报表附注，不是客户表
ICBC_UNRELATED = """
中国工商银行股份有限公司 财务报表附注（续）
四、 财务报表主要项目注释（续）
25. 其他权益工具（续）
经相关监管机构批准，本行于2025年5月9日发行了总规模为人民币400亿元的无固定期限资本债券。
本行前五名客户存款余额合计占各项存款的比例不重大。
"""


# ---------------------------------------------------------------- 解析器

def test_standard_table_gives_first_customer_share():
    d = parse_customer_section(NINGDE)
    assert d is not None and d.form == "table"
    assert d.top_ratio == 13.73, "最大客户是第 1 名，不是前五合计"
    assert d.five_ratio == 38.96
    assert d.rows == 5 and d.amount_unit == "千元"
    assert d.cross_check["ok"] is True
    assert d.cross_check["sum_ratio"] == 38.96 == d.cross_check["total_ratio"]
    assert d.usable is True


@pytest.mark.parametrize("text,unit", [(NINGDE, "千元"), (HIKVISION, "元"), (KINGSOFT, "万元")])
def test_amount_unit_never_affects_the_ratio(text, unit):
    """金额单位有元/千元/万元三种写法，占比永远是百分数——不能去换算它。"""
    d = parse_customer_section(text)
    assert d is not None and d.form == "table"
    assert d.amount_unit == unit
    assert 0 < d.top_ratio < 100


def test_extra_column_does_not_break_the_row():
    """金山办公的表多一列「是否关联方」，行尾的「否」不能被当成分隔符吃掉。"""
    d = parse_customer_section(KINGSOFT)
    assert d.top_ratio == 4.04 and d.five_ratio == 10.94
    assert d.cross_check["ok"] is True


# 长盈通 2025 年报第 62 页：客户被匿名化成「客户1…客户5」，名称里带数字。
# 曾经的 bug：行正则的名称组写成 [^\d]，把数字排除在外，整张表一行都匹配不上，
# 于是静默退化成"只有前五合计 54.59%"——表格明明在那里，却当作没披露单客户。
CHANGYINGTONG = """
公司前五名客户
√适用 □不适用
单位：万元 币种：人民币
序号 客户名称 销售额 占年度销售总额比例（%） 是否与上市公司存在关联关系
1 客户1 9,787.51 24.59 否
2 客户2 5,743.28 14.43 否
3 客户3 2,739.17 6.88 否
4 客户4 2,034.61 5.11 否
5 客户5 1,422.08 3.57 否
合计 / 21,726.65 54.59 否
"""


def test_anonymized_customer_names_with_digits_are_parsed():
    """匿名化的「客户1」也必须是可解析的名称——不能因为含数字就当表不存在。"""
    d = parse_customer_section(CHANGYINGTONG)
    assert d is not None, "表就在正文里，不该退化成 total_only"
    assert d.form == "table"
    assert d.top_ratio == 24.59, "最大客户是客户1 的 24.59%，不是前五合计 54.59%"
    assert d.five_ratio == 54.59
    assert d.rows == 5 and d.amount_unit == "万元"
    assert d.top_name == "客户1"
    assert d.cross_check["ok"] is True and d.cross_check["rule"] == "sum==total"
    assert d.usable is True


def test_missing_name_column_does_not_shift_the_numbers():
    """名称列允许含数字之后，仍不能被"缺名称"的行骗到：序号必须严格递增。"""
    broken = """
公司前 5大客户资料
序号 客户名称 销售额（元） 占年度销售总额比例
9,787.51 24.59
9,787.51 24.59
9,787.51 24.59
"""
    d = parse_customer_section(broken)
    assert d is None or d.top_ratio is None, f"没有序号锚点的表不许硬凑：{d}"



def test_truncated_table_still_yields_the_top_customer():
    """格力那张表跨页断了，只有 4 行。最大客户在第 1 行，照样要给出来。"""
    d = parse_customer_section(GREE_PARTIAL)
    assert d.form == "table"
    assert d.top_ratio == 5.91
    assert d.rows == 4
    assert d.cross_check["rule"] == "partial_rows" and d.cross_check["ok"] is True


def test_inconsistent_table_is_rejected_not_guessed():
    """五行之和与合计对不上 → 判定解析错位，宁可留空也不把错的数写进评分。"""
    broken = NINGDE.replace("合计 -- 165,061,533 38.96%", "合计 -- 165,061,533 83.96%")
    d = parse_customer_section(broken)
    assert d is not None
    assert d.form == "table_inconsistent"
    assert d.top_ratio is None and d.usable is False


def test_sentence_form_keeps_its_own_denominator():
    """中芯国际的分母是「主营业务收入」，不能一律标成「年度销售总额」。"""
    d = parse_customer_section(SMIC)
    assert d.form == "sentence"
    assert d.top_ratio == 8.0 and d.five_ratio == 35.8
    assert "主营业务收入" in d.denominator


def test_total_only_disclosure_is_not_usable():
    """茅台只给了前五合计 10.10%。这不是最大客户，usable 必须为 False。"""
    d = parse_customer_section(MOUTAI)
    assert d.form == "total_only"
    assert d.five_ratio == 10.10
    assert d.top_ratio is None and d.usable is False


def test_unrelated_mention_of_customers_yields_nothing():
    """工行那页只是附注里提到"前五名客户存款"，不是集中度表，不许硬凑一个数。"""
    assert parse_customer_section(ICBC_UNRELATED) is None


def test_quote_is_kept_for_audit():
    d = parse_customer_section(NINGDE)
    assert "客户名称" in d.quote or "第一名" in d.quote
    assert normalize("  a\u3000 b ").strip() == "a b"


# ---------------------------------------------------------------- 填充分支

def _raw_with_customer(**block):
    raw = raw_fixture()
    base = {"form": "table", "top_ratio": 13.73, "five_ratio": 38.96,
            "period": "2025-12-31", "notice_date": "2026-03-10",
            "title": "宁德时代 2025 年年度报告", "page": 28, "rows": 5,
            "denominator": "年度销售总额", "top_name": "第一名",
            "cross_check": {"sum_ratio": 38.96, "total_ratio": 38.96, "ok": True}}
    base.update(block)
    raw["customer"] = base
    return raw


def _item(rep):
    return next(i for d in rep["scorecard"]["dimensions"] if d["id"] == "risk"
                for f in d["factors"] for i in f["indicators"]
                if i["id"] == "risk.customer_conc")


def test_annual_disclosure_fills_as_observed_l1():
    out = autobuild.build(_raw_with_customer(), ASOF)
    field = out["input"]["data"]["company"]["customer_concentration"]
    assert field["value"] == 13.73
    assert field["source_tier"] == "L1", "年报原文是一手材料，不该按二手整理的 L2 标"
    assert field["as_of"] == "2025-12-31", "数据期必须是年报期末，不是分析日"
    assert field["available_at"] == "2026-03-10", "可用时点是年报公告日"
    assert "巨潮" in field["source"]

    gaps = [g for g in out["fill"]["pending"] if FID in g["fields"]]
    assert gaps == [], "自动填了就不该再出现在待补清单"

    deriv = next(d for d in out["fill"]["derivations"] if d["field_id"] == FID)
    assert "第 28 页" in deriv["formula"] and "13.73" in deriv["formula"]
    assert "交叉校验" in deriv["note"]
    assert out["fill"]["customer_annual"]["page"] == 28


def test_total_only_never_gets_filled():
    """只有前五合计时填任何值都是编的：宁可不填，把上界写进缺口说明。"""
    out = autobuild.build(_raw_with_customer(
        form="total_only", top_ratio=None, five_ratio=10.10, rows=0,
        title="贵州茅台 2025 年年度报告", page=11, cross_check=None), ASOF)
    assert "customer_concentration" not in out["input"]["data"]["company"]

    g = next(g for g in out["fill"]["pending"] if FID in g["fields"])
    assert g["kind"] == "judgement"
    assert "10.1" in g["why"] and "不超过" in g["why"], \
        f"缺口说明要交代只给了上界：{g['why']}"


def test_absent_disclosure_explains_itself():
    out = autobuild.build(_raw_with_customer(
        form="absent", top_ratio=None, five_ratio=None, rows=0, page=None,
        reason="年报正文无「前五名客户」披露"), ASOF)
    g = next(g for g in out["fill"]["pending"] if FID in g["fields"])
    assert "没有这项披露" in g["why"]


def test_report_published_after_asof_is_never_used():
    """PIT 第二道闸：年报公告日晚于分析时点，一个字都不能用。"""
    out = autobuild.build(_raw_with_customer(notice_date="2027-03-10"), ASOF)
    assert "customer_concentration" not in out["input"]["data"]["company"]
    assert any(FID in g["fields"] for g in out["fill"]["pending"])


def test_filled_value_actually_scores():
    """填进去就要能算分，否则等于没接。13.73% 落在 [10,15) 档。"""
    from stockframe.engine import run

    rep = run(autobuild.build(_raw_with_customer(), ASOF)["input"])
    item = _item(rep)
    assert item["value"] == 13.73 and item["missing"] is False
    assert item["score"] is not None
    assert item["evidence"]["source_tier"] == "L1"


def test_disclosure_dataclass_contract():
    d = CustomerDisclosure(form="table", top_ratio=1.0, five_ratio=2.0,
                           denominator="年度销售总额")
    assert d.usable is True and d.to_dict()["top_ratio"] == 1.0
