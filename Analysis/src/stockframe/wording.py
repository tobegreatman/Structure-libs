"""导出前违禁措辞拦截：交易指令不进报告。

此前的问题：`prompts.txt` 写着"管线会扫描并拦下"，代码里却只有验收脚本扫样例报告——
一条 `logic_essence = "建议目标价 45 元买入"` 会原样进结论首句，schema 照常通过。
文档承诺与运行时不符，比没有承诺更坏：它让人以为已经守住了。

现在这条约束分两道：
1. 入口：输入里的文本字段（一句话本质）命中即报错，指回字段名；
2. 出口：组装完成的报告在**返回前**再扫一遍，覆盖框架自己生成的叙述。
词表与 `tools/acceptance.py` 的 BANNED 同源（tests 里有断言钉住），避免两套表各自漂移。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from .datadict import FIELD_REGISTRY, InputError

BANNED_TERMS: Tuple[str, ...] = (
    "买入", "卖出", "加仓", "减仓", "目标价", "止损", "止盈", "建仓", "清仓",
)

# 裸词匹配会误伤研究里最常见的两类表述：
#   引用他人观点——「当前股价低于卖方目标价中位数」
#   事实描述——「大股东公告清仓式减持」
#   否定式——「当前阶段不加仓」
# 三者都不是交易指令，却会被判违规，整个报告生成失败；被误伤的人只能删掉这句话，
# 等于把"引用"、"事实"和"否定"一起从研究语言里删掉了。
#
# 所以分两步：先把确定合法的短语整体挖空（短语级白名单），再对剩余文本做裸词匹配，
# 匹配时允许"前 3 字是否定词"的豁免。判据宁可偏窄——真正的指令
# （"建议目标价 45 元买入"）前面既不是白名单短语，也没有否定词。
EXEMPT_PHRASES: Tuple[str, ...] = (
    "卖方目标价", "买方目标价", "机构目标价", "分析师目标价", "一致预期目标价",
    "目标价中位数", "目标价上修", "目标价下调", "目标价被", "目标价上调",
    "清仓式减持", "清仓式", "减持计划", "减持公告", "买入评级被下调",
)
NEG_PREFIX_CHARS = ("不", "未", "暂", "无", "勿", "别")


class WordingError(InputError):
    """出现了交易指令措辞。"""


def scan(text: str) -> List[str]:
    """返回"未被豁免"的命中词。

    步骤：① 挖空白名单短语（"卖方目标价"等整段替换为等长占位）；
    ② 裸词匹配；③ 命中处前 3 字带否定词则放行。
    """
    t = text
    for phrase in EXEMPT_PHRASES:
        if phrase in t:
            t = t.replace(phrase, "　" * len(phrase))
    hits: List[str] = []
    for term in BANNED_TERMS:
        start = 0
        while True:
            i = t.find(term, start)
            if i < 0:
                break
            before = t[max(0, i - 3):i]
            if any(c in before for c in NEG_PREFIX_CHARS):
                start = i + len(term)
                continue
            hits.append(term)
            break
    return hits


def _hit(where: str, text: Any) -> List[str]:
    if not isinstance(text, str) or not text:
        return []
    terms = scan(text)
    return [f"{where} 含「{t}」" for t in terms]


def enforce_input(inp: Dict[str, Any]) -> None:
    """入口扫描：数据字典里的文本字段（目前只有一句话本质）。"""
    problems: List[str] = []
    for group, fields in (inp.get("data") or {}).items():
        if not isinstance(fields, dict):
            continue
        for key, raw in fields.items():
            spec = FIELD_REGISTRY.get(f"{group}.{key}")
            if spec is None or not isinstance(raw, dict):
                continue
            problems += _hit(f"data.{group}.{key}", raw.get("value"))
    if problems:
        raise WordingError(
            "输入含交易指令措辞，禁止进入报告：" + "；".join(problems)
            + "。本框架只输出「分桶 + 维持/证伪条件」，不输出买卖动作与价位。"
            "请改写为条件句（例如「若 PE 分位升破 80% 则重估」）。"
        )


def enforce_report(report: Dict[str, Any]) -> None:
    """出口扫描：结论与跟踪清单里的叙述性文本。"""
    c = report.get("conclusion") or {}
    problems = _hit("conclusion.summary", c.get("summary"))
    for key in ("maintain_conditions", "falsification"):
        for item in c.get(key) or []:
            where = f"conclusion.{key}[{item.get('id', '?')}]"
            for field in ("condition", "metric", "threshold"):
                problems += _hit(f"{where}.{field}", item.get(field))
    for item in report.get("tracking_checklist") or []:
        where = f"tracking_checklist[{item.get('id', '?')}]"
        for field in ("item", "action_on_trigger"):
            problems += _hit(f"{where}.{field}", item.get(field))
    for item in report.get("scenarios") or []:
        where = f"scenarios[{item.get('name', '?')}]"
        for field in ("drivers", "key_assumptions"):
            for text in item.get(field) or []:
                problems += _hit(f"{where}.{field}", text)
        problems += _hit(f"{where}.falsification", item.get("falsification"))
    # 风险/催化同样会进报告正文（风险矩阵表、催化日历表）。此前只扫结论与情景，
    # 于是最容易被忽略的位置——"风险描述"——反而成了唯一能写交易指令的地方。
    # 现在词表有白名单短语与否定豁免，"清仓式减持""卖方目标价"这类事实描述不会被误伤。
    for item in (report.get("risk_matrix") or {}).get("risks") or []:
        where = f"risk_matrix[{item.get('id', '?')}]"
        for field in ("name", "mitigation", "evidence"):
            problems += _hit(f"{where}.{field}", item.get(field))
        trig = item.get("trigger") or {}
        for field in ("metric", "threshold"):
            problems += _hit(f"{where}.trigger.{field}", trig.get(field))
    for i, item in enumerate(report.get("catalyst_calendar") or []):
        where = f"catalyst_calendar[{i}]"
        for field in ("event", "expected_impact", "verify_method", "warning"):
            problems += _hit(f"{where}.{field}", item.get(field))
    if problems:
        raise WordingError(
            "报告含交易指令措辞，已在导出前拦下：" + "；".join(problems)
            + "（多由输入的风险/催化/逻辑描述带出）。"
        )
