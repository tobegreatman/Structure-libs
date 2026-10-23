"""客户集中度：年报读得到就自动填，读不到才回落成普通人工判断项。

这条链路换过一次方向，值得记下来：

- 最早接过商用 API（理杏仁逐客户明细、今日投资前五合计），都要付费令牌，
  没有令牌时字段必然留空 —— 而"配个环境变量就能用"的死代码最容易骗人：
  后来的人会以为这项已经自动化了，于是不再读年报。
- 于是把它退回成普通人工判断项，与"投资逻辑强度/一句话本质"同一条缺口。
- 后来又发现**年报正文里本来就有这张表**（证监会格式准则强制项），
  走巨潮公开接口 + PDF 解析就能拿到，全程不需要任何令牌。这才是它该有的形态。

所以现在要守的是另外三件事：
1. 通道必须是**年报原文**，不是需要令牌的商用接口（不许把付费 API 悄悄接回来）；
2. 拿不到时**仍是一条普通判断项**——没有专属字段、没有长文案，与同组并列；
3. 未填按缺失处理（不补 0），手填后进评分并记 L3。
"""

from __future__ import annotations

import os

from stockframe import autobuild
from stockframe.autobuild import FIELD_REGISTRY, JUDGEMENT_FIELDS
from stockframe.engine import run
from stockframe.sources import base as source_base
from stockframe.sources.cninfo import CninfoProvider
from stockframe.sources.eastmoney import EastmoneyProvider

from test_autobuild import raw_fixture

ASOF = "2026-10-23"
FID = "company.customer_concentration"


def _build():
    """不带年报块的取数结果：模拟"没取到年报原文"。"""
    raw = raw_fixture()
    raw.pop("customer", None)
    return autobuild.build(raw, ASOF)


def _gaps(out, fid=FID):
    return [g for g in out["fill"]["pending"] if fid in g["fields"]]


def test_falls_back_to_a_plain_judgement_field():
    """拿不到年报原文时，它就是普通判断项：同一条缺口、同一套字段，没有专属说明。"""
    assert FID in JUDGEMENT_FIELDS["company"]

    hits = _gaps(_build())
    assert len(hits) == 1, "该字段必须且只能出现一条缺口"
    g = hits[0]
    assert g["kind"] == "judgement", "判断项才会在面板渲染输入框"
    assert g["group"] == "company"
    assert set(g["fields"]) == set(JUDGEMENT_FIELDS["company"]), \
        "应与同组判断项并列在一条里，而不是单独一条"
    assert set(g) == {"group", "fields", "why", "kind"}, \
        f"不应再有专属字段（where/manual 之类）：{sorted(g)}"
    assert "最大一家" not in g["why"] and "第六节" not in g["why"], \
        "取数方法说明属于文档，面板不逐字段展开"


def test_channel_is_annual_report_not_a_token_api():
    """通道是年报原文。商用令牌接口不许复活——那正是当初被否掉的东西。"""
    assert "customer" in source_base.RAW_OPTIONAL, "年报通道应挂在取数契约上"
    p = EastmoneyProvider(http=lambda url: {}, bond=False, cninfo=False)
    assert getattr(p, "_cninfo", None) is None, "cninfo=False 时应完全关闭，便于离线跑"
    assert hasattr(CninfoProvider(), "annual_customer")
    for name in ("LIXINGER_TOKEN", "INVESTODAY_APPCODE", "INVESTODAY_BASE_URL"):
        assert not os.environ.get(name) and not hasattr(autobuild, "_lixinger"), \
            f"不该再依赖 {name} 这类商用令牌"
    assert not hasattr(autobuild, "MANUAL_FIELDS"), "专门登记表已撤销，别再引入第二条通路"


def test_missing_stays_missing_not_zero():
    """没填时按缺失计权，不能补 0——0% 会被读成"客户高度分散"。"""
    rep = run(_build()["input"])
    item = next(i for d in rep["scorecard"]["dimensions"] if d["id"] == "risk"
                for f in d["factors"] for i in f["indicators"]
                if i["id"] == "risk.customer_conc")
    assert item["value"] is None and item["missing"] is True


def test_manual_fill_is_accepted_by_engine_as_l3():
    """人工填进来的值必须被当成正常输入参与评分（L3），不能被"未取到"覆盖掉。"""
    out = _build()
    inp = out["input"]
    inp["data"].setdefault("company", {})[FID.split(".", 1)[1]] = {
        "value": 18.5, "as_of": ASOF, "available_at": ASOF,
        "source": "人工研究判断（界面录入）", "source_tier": "L3",
    }
    rep = run(inp)
    item = next(i for d in rep["scorecard"]["dimensions"] if d["id"] == "risk"
                for f in d["factors"] for i in f["indicators"]
                if i["id"] == "risk.customer_conc")
    assert item["value"] == 18.5
    assert item["missing"] is False
    assert item["score"] is not None, "18.5% 落在 [15,30) 档，必须有分数"
    assert item["evidence"]["source_tier"] == "L3"


def test_field_is_registered_and_not_proxied():
    """字段名不能写错；也不能被"代理"——这一项有真实披露，不需要推断。"""
    out = _build()
    assert FID in FIELD_REGISTRY
    assert FID not in out["fill"]["proxied"]["proxy"], "有真实披露的字段不该走代理"


# ---------------------------------------------------------------- 文本型判断项
# 一句话本质（逻辑内核）是唯一的文本字段：值是一句话，不是数字。
# 曾经的 bug：前端把它渲染成数字输入框、提交时 Number("一句话")=NaN 被静默跳过，
# 用户填了内容却收到"尚未填写任何判断项"。要守的是：
# 1. FieldSpec 登记为文本（前端据此渲染 type=text 并跳过数字转换）；
# 2. engine 端字符串 value 一路畅通：summary 输出原句，不进数值评分。

def test_logic_essence_is_declared_textual():
    """字段登记必须是"文本"——这是前端区分文本/数值输入的唯一依据。"""
    spec = FIELD_REGISTRY["company.logic_essence"]
    assert spec.unit == "文本", f"一句话本质应为文本字段，实际 unit={spec.unit!r}"


def test_text_judgement_flows_through_engine():
    """录入一句话后必须进报告结论，而不是被数字校验吞掉。"""
    out = _build()
    inp = out["input"]
    sentence = "全球动力电池龙头，量增与储能第二曲线撑住龙头溢价"
    inp["data"].setdefault("company", {})["logic_essence"] = {
        "value": sentence, "as_of": ASOF, "available_at": ASOF,
        "source": "人工研究判断（界面录入·文本）", "source_tier": "L3",
    }
    rep = run(inp)
    assert sentence in rep["conclusion"]["summary"], \
        "一句话本质应原样出现在结论首句"
    assert "未填：需人工补一句逻辑内核" not in rep["conclusion"]["summary"]


def test_blank_text_is_still_missing():
    """空字符串不是有效录入：不能把空串当已填（那会顶掉"未填"提示）。"""
    out = _build()
    inp = out["input"]
    inp["data"].setdefault("company", {})["logic_essence"] = {
        "value": "", "as_of": ASOF, "available_at": ASOF,
        "source": "人工研究判断（界面录入·文本）", "source_tier": "L3",
    }
    rep = run(inp)
    assert "未填：需人工补一句逻辑内核" in rep["conclusion"]["summary"]
