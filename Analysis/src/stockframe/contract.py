"""输入契约：字段值的硬约束（先于评分，违反即报错）。

存在的理由：评分带用的是 `v >= lo and v < hi` 这类比较，而
`float('nan')` 对任何比较都返回 False、`float('inf')` 对末带永远命中。
结果是 NaN 落进最差档、+Inf 落进满分档，**两者都被当成真实分数**，
置信度也不受影响——脏数据就这样变成了一个看起来很正常的结论。

同一类"安静地错"还有三种：
- 定性档位填 4.7 → int() 截成 4（差 2.5 分）；填 9 → 静默当缺失；
- 文本字段填数字 → 原样进结论首句；
- 字段名拼错（roe_typo）→ 该字段静默消失，指标按缺失处理，分数照常输出。

契约把这些全部变成**报错**：宁可不出报告，也不出一份看起来正常的错报告。
"""

from __future__ import annotations

import difflib
import math
from typing import Any

from .datadict import FIELD_REGISTRY, InputError

TEXT_UNIT = "文本"
TEXT_MAX_CHARS = 200      # 与界面文本框 maxlength 一致；超出即会被截断成半句
QUAL_RANGE = (1, 5)
# 必须严格为正的字段：价格为 0/负会让安全边际变成 inf 或符号反转
STRICT_POSITIVE = ("market.price_close",)


class ContractError(InputError):
    """字段值不合契约（结构合法，但值不合法）。"""


def check_registered(field_id: str) -> None:
    """字段名必须在 FIELD_SPECS 里登记。拼错不许静默消失。"""
    if field_id in FIELD_REGISTRY:
        return
    near = difflib.get_close_matches(field_id, sorted(FIELD_REGISTRY), n=3, cutoff=0.55)
    hint = f"相近的已注册字段：{'、'.join(near)}。" if near else ""
    raise ContractError(
        f"未注册字段 data.{field_id}。{hint}"
        "拼错的字段不会报缺字段，只会静默失效：指标按缺失处理、分数照常输出，"
        "读者看到的是一份少了这块证据却依然有结论的报告。"
        "若确为新字段，请先在 datadict.FIELD_SPECS 登记（含更新周期与是否必填）。"
    )


def check_value(field_id: str, value: Any) -> None:
    """字段值契约。value=None 表示"该字段未提供"，由 DQ 记缺口，不在此处报错。"""
    if value is None:
        return
    spec = FIELD_REGISTRY[field_id]

    if spec.qualitative:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(
                f"字段 {field_id}（{spec.name}）是 1-5 定性档位，必须是整数，"
                f"当前 {value!r}（{type(value).__name__}）。"
                "档位按 (档-1)×2.5 直接换算成分数：小数会被截断、字符串会整字段失效。"
            )
        if not (QUAL_RANGE[0] <= value <= QUAL_RANGE[1]):
            raise ContractError(
                f"字段 {field_id}（{spec.name}）档位越界：{value}，合法范围 1-5。"
                "越界值不会报错，只会被当成缺失，让整块判断静默消失。"
            )
        return

    if spec.unit == TEXT_UNIT:
        if not isinstance(value, str):
            raise ContractError(
                f"字段 {field_id}（{spec.name}）是文本字段，当前 {value!r}（{type(value).__name__}）。"
                "数字会被原样拼进结论首句。"
            )
        # 空白串已在 pit.check_field 归一为 None（=未填），走到这里只会是非空文本
        text = value.strip()
        if len(text) > TEXT_MAX_CHARS:
            raise ContractError(
                f"字段 {field_id}（{spec.name}）{len(text)} 字，超过 {TEXT_MAX_CHARS} 字上限。"
                "界面文本框会静默截断，结论首句会断在半句上。"
            )
        return

    # 数值字段
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(
            f"字段 {field_id}（{spec.name}）必须是数值，当前 {value!r}（{type(value).__name__}）。"
        )
    if not math.isfinite(float(value)):
        raise ContractError(
            f"字段 {field_id}（{spec.name}）的值为 {value!r}（NaN/Inf）。"
            "NaN 对任何区间比较都返回 False（落进最差档），Inf 命中末带（落进满分档），"
            "两者都会被当成真实分数且不影响置信度。请补真实值，或整字段不填。"
        )
    if field_id in STRICT_POSITIVE and float(value) <= 0:
        raise ContractError(
            f"字段 {field_id}（{spec.name}）必须为正，当前 {value}。"
            "价格为 0 会让安全边际变成 inf，负价格会让所有区间符号反转。"
        )


def check_series_price_range(low: Any, high: Any, where: str) -> None:
    """区间输出的最后一道闸：下限必须为正。"""
    if low is None or high is None:
        return
    if float(low) <= 0 or float(high) <= 0:
        raise ContractError(f"{where} 的区间 [{low}, {high}] 含非正值：股价不可能为负，此类区间不得输出")


# ---------------------------------------------------------------- 假设层

DCF_SCENARIOS = ("base", "optimistic", "pessimistic")
DCF_SCALARS = (
    "wacc", "terminal_growth", "tax_rate", "dep_amort_pct_rev",
    "capex_pct_rev", "nwc_pct_rev_delta", "revenue_base", "net_debt", "shares",
)
DCF_PATHS = ("revenue_growth_path", "ebit_margin_path")


def _num_in(field: str, value: Any, where: str, lo: float, hi: float, lo_open: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(
            f"{where}.{field} 必须是数值，当前 {value!r}（{type(value).__name__}）。"
            "假设层不做隐式转换：字符串 \"0.08\" 会在 DCF 里抛 TypeError，"
            "报错信息只会说 unsupported operand，看不出是哪个字段。"
        )
    v = float(value)
    if not math.isfinite(v):
        raise ContractError(
            f"{where}.{field} 的值为 {value!r}（NaN/Inf）。"
            "假设层此前完全没有校验：NaN 会把 DCF 区间与安全边际全部污染成 NaN，"
            "报告能通过 schema（jsonschema 把 NaN 当 number），"
            "但输出不是合法 JSON——浏览器 JS 与任何严格解析器都会整份失败。"
        )
    too_low = (v <= lo) if lo_open else (v < lo)
    if too_low or v > hi:
        raise ContractError(
            f"{where}.{field} = {v} 超出合理区间"
            f"{'(' if lo_open else '['}{lo}, {hi}]。假设可以激进，但要能被读懂；"
            "填错量级（如把 8% 写成 8）会让 DCF 区间整体失真而不报错。"
        )
    return v


def check_assumptions(assumptions: Any) -> None:
    """DCF/预测假设层的契约。

    这一层长期没有校验：只挂了 data 字段（pit.validate_all_fields），
    于是 `wacc=NaN` 一路算出 NaN 区间、`shares=0` 抛 ZeroDivisionError、
    `revenue_base=None` 抛 TypeError，而报告仍被判"schema 校验通过"。
    """
    if not isinstance(assumptions, dict):
        raise ContractError("assumptions 必须是对象，且包含 dcf / eps_fwd / scenario_probabilities")
    for key in ("dcf", "eps_fwd", "scenario_probabilities"):
        if key not in assumptions:
            raise ContractError(
                f"assumptions 缺少 {key}。引擎会直接 KeyError('{key}')，"
                "用户看到的是堆栈位置而不是缺了哪个假设块。"
            )

    dcf = assumptions["dcf"]
    if not isinstance(dcf, dict):
        raise ContractError("assumptions.dcf 必须是对象，含 base/optimistic/pessimistic 三个情景")
    for name in DCF_SCENARIOS:
        where = f"assumptions.dcf.{name}"
        sc = dcf.get(name)
        if not isinstance(sc, dict):
            raise ContractError(f"{where} 缺失或不是对象：三情景必须齐全，缺一个就会在情景表处 KeyError")
        for key in DCF_SCALARS:
            if key not in sc:
                raise ContractError(f"{where} 缺少 {key}")
        wacc = _num_in("wacc", sc["wacc"], where, 0.0, 0.50, lo_open=True)
        g = _num_in("terminal_growth", sc["terminal_growth"], where, -0.05, 0.12)
        if wacc <= g:
            raise ContractError(
                f"{where}.wacc({wacc}) 必须大于 terminal_growth({g})："
                "否则终值分母 ≤0，永续价值无意义（引擎会抛 ValueError，但错误里没有情景名）"
            )
        _num_in("tax_rate", sc["tax_rate"], where, 0.0, 0.60)
        _num_in("dep_amort_pct_rev", sc["dep_amort_pct_rev"], where, 0.0, 1.0)
        _num_in("capex_pct_rev", sc["capex_pct_rev"], where, 0.0, 2.0)
        _num_in("nwc_pct_rev_delta", sc["nwc_pct_rev_delta"], where, -0.5, 1.0)
        _num_in("revenue_base", sc["revenue_base"], where, 0.0, 1e7, lo_open=True)
        _num_in("net_debt", sc["net_debt"], where, -1e7, 1e7)
        # 股本为 0 是除零（每股价值 = 股权价值 / 股本）；为负会得到负股价
        if isinstance(sc["shares"], bool) or not isinstance(sc["shares"], (int, float)) \
                or not math.isfinite(float(sc["shares"])) or float(sc["shares"]) <= 0:
            raise ContractError(
                f"{where}.shares = {sc['shares']!r} 非法：股本必须为正数（单位与 revenue_base 一致）。"
                "股本为 0 会抛 ZeroDivisionError（HTTP 500，用户只看到 float division by zero）；"
                "为负会让每股价值变成负数。"
            )
        _num_in("shares", sc["shares"], where, 0.0, 1e7, lo_open=True)
        for key in DCF_PATHS:
            path = sc.get(key)
            if not isinstance(path, list) or not path:
                raise ContractError(f"{where}.{key} 必须是非空数组（显性期逐年假设），当前 {path!r}")
            for i, x in enumerate(path):
                _num_in(f"{key}[{i}]", x, where, -1.0 if key.endswith("margin_path") else -1.0, 5.0)

    probs = assumptions["scenario_probabilities"]
    if not isinstance(probs, dict) or set(probs) != set(DCF_SCENARIOS):
        raise ContractError("assumptions.scenario_probabilities 必须恰含 base/optimistic/pessimistic 三个键")
    total = 0.0
    for name in DCF_SCENARIOS:
        total += _num_in(name, probs[name], "assumptions.scenario_probabilities", 0.0, 1.0)
    if abs(total - 1.0) > 1e-6:
        raise ContractError(f"assumptions.scenario_probabilities 三情景概率合计 {total}，必须等于 1.0")

    eps_fwd = assumptions["eps_fwd"]
    if not isinstance(eps_fwd, dict):
        raise ContractError("assumptions.eps_fwd 必须是对象（含 base/optimistic/pessimistic）")
    for name in DCF_SCENARIOS:
        if name in eps_fwd:
            _num_in(name, eps_fwd[name], "assumptions.eps_fwd", -1e5, 1e5)


_LOGIC_GROUPS = sorted({f.split(".")[0] for f in FIELD_REGISTRY})


def check_logic_items(items: Any) -> None:
    """逻辑条目契约（logic_items）。

    这些条目会被直接拼进「逻辑证伪」条件与跟踪清单，是报告里最"像结论"的一段文字。
    此前没有任何校验：
    - 缺 statement → KeyError，用户看到堆栈；
    - verify_metric 写成 `revenue_growth_qoq` 这种"看起来像字段 ID"的编造值 →
      引擎原样拼进证伪条件，schema 通过、报告正常，对账时才发现查无此字段。
    """
    if items is None:
        return
    if not isinstance(items, list):
        raise ContractError("logic_items 必须是数组；单条请包成 [ {...} ]")
    for i, li in enumerate(items):
        where = f"logic_items[{i}]"
        if not isinstance(li, dict):
            raise ContractError(f"{where} 必须是对象")
        stmt = li.get("statement")
        if not isinstance(stmt, str) or not stmt.strip():
            raise ContractError(
                f"{where} 缺少 statement（一句话逻辑）。它会成为证伪条件的主语，"
                "空值会让报告出现「逻辑「」失效」这种半截话。"
            )
        vm = li.get("verify_metric")
        if isinstance(vm, str):
            group, _, rest = vm.partition(".")
            if rest and group in _LOGIC_GROUPS and vm not in FIELD_REGISTRY:
                near = difflib.get_close_matches(vm, sorted(FIELD_REGISTRY), n=3, cutoff=0.5)
                raise ContractError(
                    f"{where}.verify_metric = {vm!r} 不在字段字典内，但写法像字段 ID。"
                    + (f"相近的是：{'、'.join(near)}。" if near else "")
                    + "引擎不校验这个字符串，会把它原样拼进「F1 逻辑证伪」，"
                    "报告照样通过 schema——错得很安静。要么改用已登记字段 ID，"
                    "要么写成带口径的自由指标（如「大客户排产同比」，此时不要带字段段前缀）。"
                )


def assert_finite(obj: Any, path: str = "report") -> None:
    """出口总闸：报告里不许出现 NaN/Inf。

    schema 拦不住这个——jsonschema 的 "number" 接受 NaN，`validate_report` 会判通过，
    CLI 会打印"schema 校验通过"，而写出的文件不是合法 JSON（`NaN` 不是 JSON 字面量）。
    浏览器 `res.json()` 抛 `Unexpected token 'N'`，用户只看到"请求失败"。
    """
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ContractError(
                f"报告字段 {path} 的值为 {obj!r}：报告不得包含 NaN/Inf。"
                "这通常意味着某个假设或数据缺口被当成了数值参与计算，请在输入侧补齐或整字段留空。"
            )
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_finite(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            assert_finite(v, f"{path}[{i}]")
