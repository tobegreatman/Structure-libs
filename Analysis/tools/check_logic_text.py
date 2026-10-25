"""校验「一句话本质（逻辑内核）」草稿能否直接进管线。

提示词模板（`prompts.txt` §四）写得再细，粘回来还得有人当门卫。
网页版 AI 的失败方式很固定，且都不是"看不懂"，而是这几类：

1. **超字数**：界面那个文本框 `maxlength=200`，第 201 个字起被静默截断，
   截在"若连续两个季度"中间，句子就成了半句；
2. **空话顶替阈值**：写成"受益于行业景气""估值具备安全边际"，看着像结论，
   实则对应 `logic_strength` 1 档（泛逻辑），进不了证伪条件；
3. **交易指令**：带了"目标价/加仓"，整份报告会被违禁措辞扫描拦下；
4. **`verify_metric` 编了个字段 ID**：`revenue_growth_qoq` 这种在 39 字段字典里
   不存在，对账时查无此字段——引擎不会报错（它只是把字符串拼进文本），
   所以错得很安静，只能靠人看出来。

本脚本把上面四类做成机检，逐条给 PASS/WARN/FAIL 和改法。

用法：
    python tools/check_logic_text.py --text-file draft.txt
    python tools/check_logic_text.py --text "买的是……"
    python tools/check_logic_text.py --text-file draft.txt --json logic.json

退出码：0 = 无 FAIL（WARN 不影响），1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stockframe.datadict import FIELD_REGISTRY  # noqa: E402  （字典是唯一的字段 ID 事实来源）

MAX_CHARS = 200          # 与 web/app.template.html 里 textarea 的 maxlength 对齐
SOFT_CHARS = 140         # 超过只是提醒：结论首句太长，报告读起来会闷

# 与 tools/acceptance.py 的 BANNED 必须逐字一致（tests/test_prompt_check.py 会比对）
BANNED = ("买入", "卖出", "加仓", "减仓", "目标价", "止损", "止盈", "建仓", "清仓")
BANNED_EXTRA = ("点位", "仓位", "满仓", "清空")   # 同属交易指令语域，验收扫描里没单列

# 空话：不是"语气不好"，而是**不可证伪**——每一句都必须能翻译成指标与阈值
EMPTY_TALK = (
    "受益于行业景气", "行业景气", "赛道空间广阔", "空间广阔", "龙头地位稳固",
    "护城河深", "安全边际", "尚未充分认知", "前景广阔", "稀缺标的", "长期向好",
)

# 兑现路径必须落到经营变量上，否则只是"故事"
TRANSMISSION_WORDS = (
    "份额", "单价", "毛利率", "产能", "利用率", "收入", "销量", "费用率", "良率",
    "订单", "均价", "渗透率", "客户数", "单店", "吨毛利", "产能爬坡",
)
# 证伪必须有条件词 + 阈值，不能是"逻辑破坏"
FALSIFY_WORDS = ("若", "一旦", "跌破", "低于", "高于", "连续", "不达", "转为", "低于")
THRESHOLD_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|pp|倍|元|亿|万|个|天|季度|年|x)")
# 只含小写字母/数字/下划线（可带一个点）就当作"想写字段 ID"，其余算自由指标描述
ID_LIKE_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)?")
PRICED_IN = ("已定价", "部分定价", "未定价", "未充分定价", "高估")

Result = Tuple[str, str, str]   # (level, item, detail)  level ∈ PASS / WARN / FAIL


def check_text(text: str) -> List[Result]:
    """一句话本质的机检。返回 [(级别, 检查项, 说明), ...]。"""
    out: List[Result] = []
    raw = text.strip()
    compact = re.sub(r"\s+", "", raw)
    n = len(compact)

    # 1) 字数：硬上限来自界面，不是偏好
    if n == 0:
        out.append(("FAIL", "字数", "文本为空"))
        return out
    if n > MAX_CHARS:
        out.append(("FAIL", "字数", f"{n} 字，超过界面 {MAX_CHARS} 字硬上限，会被截断成半句"))
    elif n > SOFT_CHARS:
        out.append(("WARN", "字数", f"{n} 字，偏长（建议 ≤{SOFT_CHARS}）；结论首句太长会压掉后半段信息"))
    else:
        out.append(("PASS", "字数", f"{n} 字，在 {MAX_CHARS} 字以内"))

    # 2) 骨架三段：收益来源 / 兑现路径 / 证伪条件
    #    只写"公司受益于行业景气"不算拆过 β/α——那是方向，不是来源。
    has_source = any(k in raw for k in ("买的是", "赚的是", "β", "α", "beta", "alpha")) or \
                 bool(re.search(r"(行业|需求|总量)[^。；]{0,24}(公司|份额|单价|毛利)", raw))
    out.append(("PASS" if has_source else "FAIL", "收益来源",
                "已区分行业(β)与公司(α)" if has_source else
                "没看出收益来源，也没拆 β/α：读者不知道这钱是行业给的还是公司自己挣的"))

    has_trans = any(k in raw for k in TRANSMISSION_WORDS)
    out.append(("PASS" if has_trans else "FAIL", "兑现路径",
                "已落到经营变量" if has_trans else
                "只有行业判断、没有公司经营变量（份额/毛利/产能/收入…），等于没写传导链"))

    has_falsify = any(k in raw for k in FALSIFY_WORDS) and bool(THRESHOLD_RE.search(raw))
    if has_falsify:
        out.append(("PASS", "证伪条件", "有条件词 + 阈值"))
    elif any(k in raw for k in FALSIFY_WORDS):
        out.append(("FAIL", "证伪条件", "有「若/一旦」但没有带单位的阈值，明天查不了"))
    else:
        out.append(("FAIL", "证伪条件", "没有证伪条件：写不出'什么算错'的结论不可跟踪"))

    # 3) 一句话里塞太多故事：并列断言超过 3 段就得分层
    clauses = sum(raw.count(sep) for sep in ("；", ";", "，且", "同时"))
    if clauses >= 3:
        out.append(("WARN", "断言密度", f"并列断句 {clauses} 处，可能堆了多个故事；只留一个主逻辑"))
    else:
        out.append(("PASS", "断言密度", f"并列断句 {clauses} 处"))

    # 4) 违禁措辞：会触发整份报告的合规扫描
    hit_b = [w for w in BANNED + BANNED_EXTRA if w in raw]
    out.append(("FAIL" if hit_b else "PASS", "交易指令",
                f"出现违禁措辞 {hit_b}，报告会被拦下" if hit_b else "无买卖/点位/仓位措辞"))

    # 5) 空话
    hit_e = [w for w in EMPTY_TALK if w in raw]
    out.append(("FAIL" if hit_e else "PASS", "空话",
                f"出现不可证伪表述 {hit_e}（多为 logic_strength 1 档措辞），改成指标+阈值"
                if hit_e else "未使用不可证伪的套话"))

    # 6) 数字锚：至少一个带单位的阈值
    if THRESHOLD_RE.search(raw):
        out.append(("PASS", "数字锚", "至少含一个带单位的阈值"))
    else:
        out.append(("FAIL", "数字锚", "全文没有一个带单位的数字，无法判断任何东西是否达标"))

    # 7) 时点/来源痕迹：一句话里不强求，但没有就提醒
    if re.search(r"20\d{2}|年报|季报|公告|报告期|口径", raw):
        out.append(("PASS", "时点痕迹", "带报告期/年度/公告等时点线索"))
    else:
        out.append(("WARN", "时点痕迹", "没写报告期或年度；跨年比较时容易把旧数当新数"))

    return out


def check_items(items: Sequence[Dict[str, Any]]) -> List[Result]:
    """logic_items 的机检：键齐全、字段 ID 真实存在、阈值可查。"""
    out: List[Result] = []
    if not items:
        return [("WARN", "logic_items", "未提供（可不填，但报告会缺'逻辑证伪'条件）")]
    if not isinstance(items, list):
        return [("FAIL", "logic_items", "应为 JSON 数组")]

    need = ("statement", "transmission", "verify_metric", "verify_threshold", "source")
    for i, it in enumerate(items, 1):
        tag = f"logic_items[{i}]"
        if not isinstance(it, dict):
            out.append(("FAIL", tag, "元素不是对象"))
            continue
        missing = [k for k in need if not str(it.get(k, "")).strip()]
        if missing:
            out.append(("FAIL", tag, f"缺字段 {missing}（五键齐全才能进证伪条件）"))
            continue
        vm = str(it["verify_metric"]).strip()
        vt = str(it["verify_threshold"]).strip()
        # 形如标识符（无空格无中文）就当作"想写字段 ID"处理：宁可拦下，也不放过
        # 编造的 ID——引擎只把字符串拼进文本，查无此字段是不会报错的。
        looks_id = bool(ID_LIKE_RE.fullmatch(vm))
        if looks_id:
            if vm in FIELD_REGISTRY:
                out.append(("PASS", tag, f"verify_metric={vm} 在 39 字段字典内"))
            else:
                tail = vm.split(".")[-1]
                near = [f for f in FIELD_REGISTRY
                        if tail in f or f.split(".")[-1] in vm]
                out.append(("FAIL", tag,
                            f"verify_metric={vm} 不是字典字段，对账会查无此字段"
                            + (f"；相近的是 {near[:3]}" if near else "；改成自由指标描述或换真实字段 ID")))
        else:
            out.append(("WARN", tag, f"verify_metric={vm} 是自由指标（非字段 ID），请写清口径与出处"))
        if not THRESHOLD_RE.search(vt) and not re.search(r"[<>≥≤]", vt):
            out.append(("FAIL", tag, f"verify_threshold={vt!r} 没有阈值，证伪条件永远不触发"))
        pi = it.get("priced_in")
        if pi is not None and str(pi) not in PRICED_IN:
            out.append(("WARN", tag, f"priced_in={pi!r} 不在 {list(PRICED_IN)} 内（管线不消费该键，仅供人读）"))
    return out


def _print(results: Sequence[Result]) -> int:
    width = max((len(i) for _, i, _ in results), default=8) + 2
    print("=" * 74)
    print(f"{'结果':<6}{'检查项'.ljust(width)}说明")
    print("-" * 74)
    fails = 0
    for level, item, detail in results:
        print(f"{level:<6}{item.ljust(width)}{detail}")
        if level == "FAIL":
            fails += 1
    print("=" * 74)
    print(f"{len(results)} 项检查，FAIL {fails} 项" + ("　→ 改完再进管线" if fails else "　→ 可以进管线"))
    return 0 if fails == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验一句话本质 / logic_items 草稿")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="一句话本质正文")
    g.add_argument("--text-file", help="从文件读一句话本质（UTF-8）")
    ap.add_argument("--json", help="logic_items JSON 文件（数组，或含 logic_items 键的输入 JSON）")
    a = ap.parse_args(argv)

    text = a.text if a.text is not None else Path(a.text_file).read_text(encoding="utf-8")
    results = check_text(text)
    if a.json:
        data = json.loads(Path(a.json).read_text(encoding="utf-8"))
        items = data.get("logic_items", []) if isinstance(data, dict) else data
        results += check_items(items)
    return _print(results)


if __name__ == "__main__":
    raise SystemExit(main())
