"""量规引擎：声明式指标/因子/维度 + 全程可追溯评分。

评分记法（设计文档 §5.1）：
    维度得分 = Σ(因子得分 × 因子权重)
    因子得分 = Σ(指标得分 × 指标权重)  —— 缺失指标按可用权重重归一化
    指标得分 = 量规带(原始值) → 带内定值 ∈ [0,10]，带具名

反黑箱约束：
- 全部量规为阶梯带（显式区间 + 显式分数），无隐藏函数形状；
- 每个指标带 rationale（收录依据 + 阈值锚定）；
- 缺失不静默补零：score=null，因子重归一化并记录 available_weight_ratio；
- 定性指标 1-5 锚定量表，档位描述原样进报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .pit import FieldRef

# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class Band:
    """量规带：区间 [lo, hi)，升序排列，首带 lo=-inf、末带 hi=+inf。"""
    label: str
    lo: Optional[float]      # None = -inf
    hi: Optional[float]      # None = +inf
    score: float

    def contains(self, v: float) -> bool:
        if self.lo is not None and v < self.lo:
            return False
        if self.hi is not None and v >= self.hi:
            return False
        return True


@dataclass(frozen=True)
class IndicatorSpec:
    id: str
    name: str
    unit: str
    field: str                       # 数据字段（注册表键），或派生字段 id
    bands: Tuple[Band, ...]
    weight: float                    # 因子内权重
    rationale: str                   # 收录与阈值锚定依据
    derive: Optional[str] = None     # 派生字段名（DERIVED 注册表键）
    computed: Optional[str] = None   # 计算型指标（值由引擎公式给出）
    anchors: Optional[Dict[int, str]] = None  # 定性 1-5 档位描述


@dataclass(frozen=True)
class FactorSpec:
    id: str
    name: str
    weight: float                    # 维度内权重
    indicators: Tuple[IndicatorSpec, ...]


@dataclass(frozen=True)
class DimensionSpec:
    id: str
    name: str
    weight_mid: float
    weight_short: float
    update_days: int                 # 典型更新周期（时效性判断）
    factors: Tuple[FactorSpec, ...]


# ---------------------------------------------------------------- 评估结果

@dataclass
class IndicatorResult:
    spec: IndicatorSpec
    value: Any
    band: Optional[str]
    score: Optional[float]
    missing: bool
    evidence: Optional[FieldRef]           # 原始字段引用（派生字段为主字段）
    evidence_refs: List[FieldRef] = field(default_factory=list)  # 派生字段全部来源

@dataclass
class FactorResult:
    spec: FactorSpec
    indicators: List[IndicatorResult]
    score: Optional[float]
    renormalized: bool
    available_weight_ratio: float

@dataclass
class DimensionResult:
    spec: DimensionSpec
    factors: List[FactorResult]
    score: Optional[float]
    confidence: float
    confidence_level: str


# ---------------------------------------------------------------- 引擎

TIER_PENALTY = {"L1": 0.0, "L2": 0.02, "L3": 0.08}


def score_to_band_label(score: float) -> str:
    """计算型指标（0-10 连续值）的通用分档标签。"""
    if score >= 8: return "优"
    if score >= 6: return "良"
    if score >= 4: return "中"
    if score >= 2: return "弱"
    return "差"


class RubricEngine:
    """对一组 DimensionSpec 求值，产出带完整证据链的评分结果。"""

    def __init__(
        self,
        dims: Sequence[DimensionSpec],
        registry: Dict[str, FieldRef],
        derived: Dict[str, Callable[[], Any]],
        derived_sources: Dict[str, List[str]],
        analysis_date,
    ) -> None:
        self.dims = list(dims)
        self.registry = registry
        self.derived = derived
        self.derived_sources = derived_sources
        self.analysis_date = analysis_date

    # -- 单指标 ---------------------------------------------------------

    def _resolve(self, spec: IndicatorSpec) -> Tuple[Optional[Any], List[FieldRef], Optional[FieldRef]]:
        """返回 (值, 全部来源字段, 主证据字段)。值 None = 缺失。"""
        if spec.derive is not None:
            value = self.derived.get(spec.derive)
            if value is None:
                return None, [], None
            refs = [self.registry[f] for f in self.derived_sources.get(spec.derive, []) if f in self.registry]
            main = refs[0] if refs else None
            return value, refs, main
        ref = self.registry.get(spec.field)
        if ref is None:
            return None, [], None
        value = ref.value
        if value is None:
            return None, [ref], ref
        return value, [ref], ref

    def _band_of(self, spec: IndicatorSpec, value: Any) -> Tuple[Optional[str], Optional[float]]:
        if spec.anchors is not None:
            rating = int(value)
            if rating not in spec.anchors:
                return None, None
            return spec.anchors[rating], float(rating - 1) * 2.5  # 1-5 档 → 0-10 分，锚定转换
        if spec.computed is not None:
            return score_to_band_label(float(value)), float(value)  # 计算型已直接给 0-10 分
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None, None
        for band in spec.bands:
            if band.contains(v):
                return band.label, band.score
        return None, None

    def eval_indicator(self, spec: IndicatorSpec) -> IndicatorResult:
        value, refs, main = self._resolve(spec)
        if value is None:
            return IndicatorResult(spec=spec, value=None, band=None, score=None,
                                   missing=True, evidence=main, evidence_refs=refs)
        band, score = self._band_of(spec, value)
        if band is None:
            return IndicatorResult(spec=spec, value=value, band=None, score=None,
                                   missing=True, evidence=main, evidence_refs=refs)
        return IndicatorResult(spec=spec, value=value, band=band, score=score,
                               missing=False, evidence=main, evidence_refs=refs)

    # -- 因子 / 维度 ------------------------------------------------------

    def eval_factor(self, spec: FactorSpec) -> FactorResult:
        results = [self.eval_indicator(ind) for ind in spec.indicators]
        total_w = sum(ind.weight for ind in spec.indicators)
        avail_w = sum(r.spec.weight for r in results if not r.missing)
        if avail_w <= 0 or total_w <= 0:
            # 该因子全部指标缺失：不参与加权，但必须留下"已被排除"的痕迹（可追溯）
            return FactorResult(spec=spec, indicators=results, score=None,
                                renormalized=True, available_weight_ratio=0.0)
        score = sum(r.score * r.spec.weight for r in results if not r.missing) / avail_w
        return FactorResult(
            spec=spec, indicators=results, score=round(score, 2),
            renormalized=avail_w < total_w - 1e-9,
            available_weight_ratio=round(avail_w / total_w, 3),
        )

    def _timeliness_penalty(self, dim: DimensionSpec, refs: List[FieldRef]) -> float:
        pen = 0.0
        for ref in refs:
            age = ref.age_days(self.analysis_date)
            ratio = age / dim.update_days if dim.update_days else 0
            if ratio > 2:
                pen = max(pen, 0.2)
            elif ratio > 1.5:
                pen = max(pen, 0.1)
        return pen

    def eval_dimension(self, dim: DimensionSpec) -> DimensionResult:
        factors = [self.eval_factor(f) for f in dim.factors]
        total_w = sum(f.spec.weight for f in factors)
        avail_w = sum(f.spec.weight for f in factors if f.score is not None)
        score = None
        if avail_w > 0:
            score = round(sum(f.score * f.spec.weight for f in factors if f.score is not None) / avail_w, 2)

        # 置信度（设计文档 §5.5）
        all_inds = [i for f in factors for i in f.indicators]
        missing_n = sum(1 for i in all_inds if i.missing)
        refs: List[FieldRef] = []
        tier_pen_sum = 0.0
        tier_w_sum = 0.0
        for ind in all_inds:
            for ref in ind.evidence_refs:
                refs.append(ref)
                tier_pen_sum += TIER_PENALTY.get(ref.source_tier, 0.08) * ind.spec.weight
                tier_w_sum += ind.spec.weight
        tier_pen = (tier_pen_sum / tier_w_sum) if tier_w_sum else 0.0
        renorm_pen = 0.05 * sum(1 for f in factors if 0 < f.available_weight_ratio < 0.6)
        time_pen = self._timeliness_penalty(dim, refs)
        confidence = max(0.2, min(1.0, 1.0 - 0.06 * missing_n - tier_pen - renorm_pen - time_pen))
        level = "high" if confidence >= 0.75 else ("medium" if confidence >= 0.5 else "low")
        return DimensionResult(spec=dim, factors=factors, score=score,
                               confidence=round(confidence, 3), confidence_level=level)

    def evaluate_all(self) -> List[DimensionResult]:
        return [self.eval_dimension(d) for d in self.dims]

    # -- 汇总 --------------------------------------------------------------

    @staticmethod
    def composite(results: List[DimensionResult], weights_key: str) -> Optional[float]:
        num = 0.0
        den = 0.0
        for r in results:
            if r.score is None:
                continue
            w = getattr(r.spec, weights_key)
            num += r.score * w
            den += w
        return round(num / den, 2) if den > 0 else None

    @staticmethod
    def overall_confidence(results: List[DimensionResult], weights_key: str, dq_overall: float) -> float:
        num = sum(r.confidence * getattr(r.spec, weights_key) for r in results)
        return round(min(1.0, num * dq_overall), 3)
