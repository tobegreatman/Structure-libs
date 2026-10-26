"""结论翻转点（sensitivity）：把某一维度单独下调，分桶在哪一步改变。

为什么要放在引擎而不是界面：分桶逻辑在 `decision.py`，前端照着复刻一遍就是第二套实现，
两套迟早不一致（§16.1 薄壳约束）。所以这里不新增任何判定规则——所有阈值、分桶函数、
R5 冲突规则都从 `decision` 导入，本模块只做一件事：**沿"某一维度变差"这一个方向二分，
找出分桶第一次变化的位移**。

三条机制（都来自 decision.py 的判定顺序，不是新发明的）：
1. `score_band`：综合分跌破分界线（入池 7.0 / 观察 3.5）。注意 5.0 不是翻转边界——
   5.0 以下到 3.5 仍属"观察"，跌破 5.0 分桶不变，所以观察桶的翻转线是 3.5。
2. `dim_floor`：综合分仍 ≥ 7.0，但有维度跌破 4.0 → 入池降为观察（R6 内部条件）。
   这条常常先于综合分翻线触发：低权重维度（0.06）掉 10 分不可能，掉到 4.0 却只要 6 分。
3. `signal_conflict`：综合分 ≥ 7.0 且估值/风险/财务 ≤ 3.0 → 人工复核（R5，判定顺序在 R6 之前）。

注意一个由规则叠加导致的事实：**R5 永远不会是"第一个"翻转机制**。任何 ≤3.0 的维度必然先跌破
4.0 的入池下限，而"维度 < 4.0 → 观察"在同一个判定函数里更早成立（dim_floor 在 4.0，R5 在 3.0）。
所以 drivers 里的 mechanism 实际只会出现 score_band / dim_floor；R5 分支保留是为了让机制命名与
`decision.py` 的规则表一一对应，将来若调整下限，这里自动跟随而不是悄悄漏判。

不覆盖 R1–R4、R7–R9：这些由输入（红旗、特殊状态）与覆盖决定，不随分数变化，
所以对它们算"下调多少分会翻桶"没有意义——当前桶是人工复核/回避时，本模块直接返回空集。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from .decision import (
    CONFLICT_MAIN_THRESHOLD,
    CONFLICT_RULES,
    DIM_MIN_FOR_ENTRY,
    _bucket_from_score,
)

BUCKET_ENTRY = "入池"
BUCKET_WATCH = "观察"
BUCKET_AVOID = "回避"
BUCKET_MANUAL = "人工复核"

ENTRY_LINE = 7.0
AVOID_LINE = 3.5
ANCHOR_STEP = 2.5  # 定性档位换算：(档-1) × 2.5，故 1 档 = 2.5 分

# 每个桶的分数区间（含下界、不含上界；入池上界就是评分卡满分 10）。
# 用它回答"这个分数在桶里处在什么位置"——7.64 在 7.0–10 里只走了 21%，
# 也就是刚过线；这比"距线 +0.64"更能让人看出结论有多不宽裕。
BAND_RANGE = {
    BUCKET_ENTRY: (ENTRY_LINE, 10.0),
    BUCKET_WATCH: (AVOID_LINE, ENTRY_LINE),
    BUCKET_AVOID: (0.0, AVOID_LINE),
}

# 桶内位置三等分，纯描述（下沿/中段/上沿），不是对标的的评价。
# 阈值刻意等分：任何"更聪明的"分界都要有依据，而这里没有依据可取。
_ZONE_CUTS = (1 / 3, 2 / 3)
_ZONE_NAMES = ("下沿", "中段", "上沿")

# R5 覆盖的维度与阈值，从 decision 的规则表取，不在本文件里重写一遍
R5_DIMS = tuple(d for d, _thr, _desc in CONFLICT_RULES)
R5_LINE = min(thr for _d, thr, _desc in CONFLICT_RULES)

MECHANISM_CN = {
    "score_band": "综合分跌破分界线",
    "dim_floor": "维度跌破 4.0 的入池下限",
    "signal_conflict": "R5 信号冲突（≤3.0 转人工复核）",
}


def _scored(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows if r.get("score") is not None]


def _composite_with(rows: Sequence[Dict[str, Any]], idx: int, new_score: float) -> Optional[float]:
    """把第 idx 维的得分替换为 new_score 后的综合分（与 rubric.composite 同口径：只在已评维度上归一）。

    返回未取整的值：翻转点判定要的是边界那侧，提前 round 到 2 位会在 7.00 附近抖动。
    """
    num = den = 0.0
    for j, r in enumerate(rows):
        s = new_score if j == idx else r.get("score")
        if s is None:
            continue
        w = r.get("weight") or 0.0
        num += s * w
        den += w
    return (num / den) if den > 0 else None


def _dim_min_with(rows: Sequence[Dict[str, Any]], idx: int, new_score: float) -> Optional[float]:
    vals = [new_score if j == idx else r.get("score") for j, r in enumerate(rows)]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def _bucket_with(rows: Sequence[Dict[str, Any]], idx: int, new_score: float) -> str:
    """按 decision.decide 的分数段顺序（R5 → R6）重算分桶，只看受影响的那一维。"""
    comp = _composite_with(rows, idx, new_score)
    if comp is None:
        return BUCKET_MANUAL
    comp_r = round(comp, 2)
    dim_id = rows[idx]["id"]
    if comp_r >= CONFLICT_MAIN_THRESHOLD and dim_id in R5_DIMS and new_score <= R5_LINE:
        return BUCKET_MANUAL
    return _bucket_from_score(comp_r, _dim_min_with(rows, idx, new_score))


def bucket_with(rows: Sequence[Dict[str, Any]], dim_id: str, new_score: float) -> str:
    """对外：把某维度改成 new_score 后的分桶（测试与界面解释共用的唯一入口）。"""
    for j, r in enumerate(rows):
        if r["id"] == dim_id:
            return _bucket_with(rows, j, new_score)
    raise KeyError(f"未知维度 {dim_id}")


def _mechanism(rows: Sequence[Dict[str, Any]], idx: int, new_score: float) -> str:
    comp = _composite_with(rows, idx, new_score)
    comp_r = round(comp, 2) if comp is not None else None
    if comp_r is not None and comp_r >= CONFLICT_MAIN_THRESHOLD:
        if rows[idx]["id"] in R5_DIMS and new_score <= R5_LINE:
            return "signal_conflict"
        if (_dim_min_with(rows, idx, new_score) or 0.0) < DIM_MIN_FOR_ENTRY:
            return "dim_floor"
    return "score_band"


def _flip_delta(rows: Sequence[Dict[str, Any]], idx: int, bucket_now: str) -> Optional[float]:
    """二分出"该维度下调多少分，分桶第一次变化"。

    单调性：只下调一维得分，综合分与维度最低值都不会上升，分桶只可能变差（入池→观察→回避），
    所以谓词 `桶 != 当前桶` 在 [0, score] 上是单调的，二分成立。
    零分仍不翻桶 → 返回 None（该维度对结论没有决定权）。
    """
    s0 = rows[idx].get("score")
    if s0 is None:
        return None
    if _bucket_with(rows, idx, 0.0) == bucket_now:
        return None
    lo, hi = 0.0, float(s0)
    for _ in range(60):
        mid = (lo + hi) / 2
        if _bucket_with(rows, idx, s0 - mid) == bucket_now:
            lo = mid
        else:
            hi = mid
    delta = math.ceil(round(hi, 6) * 100) / 100  # 向上取到 0.01：报出来的数必须真的翻桶
    while delta <= s0 and _bucket_with(rows, idx, s0 - delta) == bucket_now:
        delta += 0.01
    return round(delta, 2) if delta <= s0 else None


def _boundary(composite: Optional[float], bucket: str) -> Dict[str, Any]:
    """当前桶"往下一个桶"的分界：入池看 7.0，观察看 3.5（5.0 不改变分桶），回避无。"""
    if composite is None:
        return {"boundary": None, "distance": None, "next_bucket": None}
    if bucket == BUCKET_ENTRY:
        return {"boundary": ENTRY_LINE, "distance": round(composite - ENTRY_LINE, 2),
                "next_bucket": BUCKET_WATCH}
    if bucket == BUCKET_WATCH:
        return {"boundary": AVOID_LINE, "distance": round(composite - AVOID_LINE, 2),
                "next_bucket": BUCKET_AVOID}
    return {"boundary": None, "distance": None, "next_bucket": None}


def _band(composite: Optional[float], bucket: str) -> Dict[str, Any]:
    """分数在本桶区间里的相对位置（0 = 刚够格，1 = 触到本桶天花板）。

    存在的意义：`distance`（距下方线的分差）单独看没有尺度感——0.64 分是宽还是窄？
    除以桶高（入池 3 分）得到 21%，才读得出"刚过线"。
    """
    rng = BAND_RANGE.get(bucket)
    if composite is None or rng is None:
        return {"low": None, "high": None, "position": None, "zone": None}
    low, high = rng
    if high <= low:
        return {"low": low, "high": high, "position": None, "zone": None}
    pos = (composite - low) / (high - low)
    pos = max(0.0, min(1.0, pos))
    zone = _ZONE_NAMES[0] if pos < _ZONE_CUTS[0] else (
        _ZONE_NAMES[1] if pos < _ZONE_CUTS[1] else _ZONE_NAMES[2])
    return {"low": low, "high": high, "position": round(pos, 3), "zone": zone}


def _headroom(composite: Optional[float], bucket: str) -> Dict[str, Any]:
    """往好里走还有没有档位可升、还差多少分。

    这是"方向"的另一半：只报向下多远，读者会以为上面还有空间。
    实际上入池就是最高档——对它而言向上无档可升，结论只能谈"守不守得住"。
    """
    if composite is None:
        return {"at_top": False, "ceiling": None, "room": None, "next_up": None}
    if bucket == BUCKET_ENTRY:
        return {"at_top": True, "ceiling": None, "room": None, "next_up": None}
    if bucket == BUCKET_WATCH:
        return {"at_top": False, "ceiling": ENTRY_LINE,
                "room": round(ENTRY_LINE - composite, 2), "next_up": BUCKET_ENTRY}
    if bucket == BUCKET_AVOID:
        return {"at_top": False, "ceiling": AVOID_LINE,
                "room": round(AVOID_LINE - composite, 2), "next_up": BUCKET_WATCH}
    return {"at_top": False, "ceiling": None, "room": None, "next_up": None}


def analyze(
    rows: Sequence[Dict[str, Any]],
    composite: Optional[float],
    bucket: str,
) -> Dict[str, Any]:
    """rows 元素：{id, name, weight, score, anchored}。

    anchored 表示该维度由定性档位映射而来（宏观/行业/公司），只有它们才谈得上"几档"。
    """
    rows = [
        {
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "weight": float(r.get("weight") or 0.0),
            "score": None if r.get("score") is None else float(r["score"]),
            "anchored": bool(r.get("anchored")),
        }
        for r in rows
    ]
    base = _boundary(composite, bucket)
    band = _band(composite, bucket)
    headroom = _headroom(composite, bucket)
    distance_anchors = (None if base["distance"] is None
                        else round(base["distance"] / ANCHOR_STEP, 2))
    drivers: List[Dict[str, Any]] = []
    insensitive: List[str] = []
    unscored: List[str] = []

    score_driven = bucket in (BUCKET_ENTRY, BUCKET_WATCH)
    for idx, r in enumerate(rows):
        if r["score"] is None:
            unscored.append(r["id"])
            continue
        delta = _flip_delta(rows, idx, bucket) if score_driven else None
        if delta is None:
            insensitive.append(r["id"])
            continue
        after_score = round(r["score"] - delta, 2)
        drivers.append({
            "id": r["id"],
            "name": r["name"],
            "weight": round(r["weight"], 3),
            "score": round(r["score"], 2),
            "delta": delta,
            "score_after": after_score,
            "anchors": round(delta / ANCHOR_STEP, 2) if r["anchored"] else None,
            "bucket_after": _bucket_with(rows, idx, r["score"] - delta),
            "mechanism": _mechanism(rows, idx, r["score"] - delta),
        })
    drivers.sort(key=lambda d: (d["delta"], d["id"]))

    if not score_driven:
        note = (f"当前分桶【{bucket}】由规则与覆盖决定（R1–R4 / R7–R9），不由分数决定，"
                "单维得分下调不会改变分桶，因此不出翻转点；改输入或补覆盖后重跑再谈位移。")
    elif not drivers:
        note = "任何单一维度归零都不改变分桶：结论由多条维度共同支撑，不存在单点失效。"
    else:
        first = drivers[0]
        if headroom["at_top"]:
            up = "向上已无更优档位（本桶为最高档），这个结论只谈守不守得住，不谈还能更好"
        elif headroom["room"] is not None:
            up = f"向上需再得 {headroom['room']:.2f} 分才进【{headroom['next_up']}】"
        else:
            up = "向上无档位可升"
        note = (f"向下 {first['delta']:.2f} 分即转【{first['bucket_after']}】"
                f"（最敏感的是「{first['name']}」，{MECHANISM_CN[first['mechanism']]}）；{up}。"
                "本表只覆盖分数驱动的机制（综合分跌破分界线 / 维度跌破 4.0 / R5 信号冲突），"
                "R1–R4、R7–R9 由输入与覆盖决定；档数按每档 2.5 分折算，仅对判断层（宏观/行业/公司）成立。")

    return {
        **base,
        "band": band,
        "headroom": headroom,
        "distance_anchors": distance_anchors,
        "drivers": drivers,
        "insensitive": insensitive,
        "unscored": unscored,
        "note": note,
    }
