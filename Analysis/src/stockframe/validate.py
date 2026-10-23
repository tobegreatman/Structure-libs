"""报告校验：jsonschema 优先，内置降级校验器兜底 + 跨字段一致性检查。

跨字段约束（JSON Schema 表达不了的）：
- 三情景概率合计 = 1
- 中线/短线维度权重各合计 = 1
- 全部 evidence available_at <= analysis_date（双保险，PIT 已在入口拦截）
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_PATH = None  # 由 cli 传入；默认相对本文件定位


def _default_schema_path() -> str:
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "schema", "report_schema.json")


# ---------------------------------------------------------------- 内置降级校验器

class BasicValidator:
    """Draft-07 子集校验：type/required/enum/const/min/max/length/pattern/items/$ref。
    仅作 jsonschema 缺失时的兜底，正式验收应安装 jsonschema。"""

    def __init__(self, schema: Dict[str, Any]) -> None:
        self.schema = schema
        self.errors: List[str] = []

    def validate(self, instance: Any) -> bool:
        self.errors = []
        self._validate(instance, self.schema, "$")
        return not self.errors

    def _resolve(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        ref = schema.get("$ref")
        if not ref:
            return schema
        node: Any = self.schema
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return node

    def _type_ok(self, v: Any, t: str) -> bool:
        if t == "object": return isinstance(v, dict)
        if t == "array": return isinstance(v, list)
        if t == "string": return isinstance(v, str)
        if t == "number": return isinstance(v, (int, float)) and not isinstance(v, bool)
        if t == "integer": return isinstance(v, int) and not isinstance(v, bool)
        if t == "boolean": return isinstance(v, bool)
        if t == "null": return v is None
        return True

    def _validate(self, v: Any, schema: Dict[str, Any], path: str) -> None:
        schema = self._resolve(schema)
        if "const" in schema and v != schema["const"]:
            self.errors.append(f"{path}: 应为常量 {schema['const']!r}，实际 {v!r}")
        if "enum" in schema and v not in schema["enum"]:
            self.errors.append(f"{path}: {v!r} 不在枚举 {schema['enum']}")
        types = schema.get("type")
        if types is not None:
            types = types if isinstance(types, list) else [types]
            if not any(self._type_ok(v, t) for t in types):
                self.errors.append(f"{path}: 类型应为 {types}，实际 {type(v).__name__}")
                return
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            for key, op in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b),
                            ("exclusiveMinimum", lambda a, b: a > b), ("exclusiveMaximum", lambda a, b: a < b)):
                if key in schema and not op(v, schema[key]):
                    self.errors.append(f"{path}: {v} 违反 {key}={schema[key]}")
        if isinstance(v, str):
            if "minLength" in schema and len(v) < schema["minLength"]:
                self.errors.append(f"{path}: 长度 < {schema['minLength']}")
            if "pattern" in schema:
                import re
                if not re.search(schema["pattern"], v):
                    self.errors.append(f"{path}: {v!r} 不匹配 {schema['pattern']}")
        if isinstance(v, dict):
            for req in schema.get("required", []):
                if req not in v:
                    self.errors.append(f"{path}: 缺少必填键 {req}")
            props = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                for k in v:
                    if k not in props:
                        self.errors.append(f"{path}: 不允许的额外键 {k}")
            for k, sub in props.items():
                if k in v:
                    self._validate(v[k], sub, f"{path}.{k}")
        if isinstance(v, list):
            if "minItems" in schema and len(v) < schema["minItems"]:
                self.errors.append(f"{path}: 条目数 < {schema['minItems']}")
            if "maxItems" in schema and len(v) > schema["maxItems"]:
                self.errors.append(f"{path}: 条目数 > {schema['maxItems']}")
            if "items" in schema:
                for i, item in enumerate(v):
                    self._validate(item, schema["items"], f"{path}[{i}]")


# ---------------------------------------------------------------- 对外接口

def validate_report(report: Dict[str, Any], schema_path: Optional[str] = None) -> Tuple[bool, List[str]]:
    """返回 (是否通过, 错误列表)。schema 校验 + 跨字段一致性。"""
    path = schema_path or _default_schema_path()
    with open(path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)

    errors: List[str] = []
    try:
        import jsonschema  # type: ignore
        validator = jsonschema.Draft7Validator(schema)
        for err in sorted(validator.iter_errors(report), key=lambda e: list(e.absolute_path)):
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            errors.append(f"[schema] {loc}: {err.message}")
    except ImportError:
        bv = BasicValidator(schema)
        bv.validate(report)
        errors = [f"[basic] {e}" for e in bv.errors]

    # 跨字段一致性
    probs = [s["probability"] for s in report.get("scenarios", [])]
    if probs and abs(sum(probs) - 1.0) > 1e-6:
        errors.append(f"[cross] 三情景概率合计 {sum(probs)} != 1")
    for key in ("mid_term", "short_term"):
        w = report.get("scorecard", {}).get("weights", {}).get(key, {})
        if w and abs(sum(w.values()) - 1.0) > 1e-6:
            errors.append(f"[cross] {key} 维度权重合计 {sum(w.values())} != 1")
    analysis_date = report.get("meta", {}).get("analysis_date")
    if analysis_date:
        ad = date.fromisoformat(analysis_date)
        for ev in report.get("evidence_log", []):
            av = date.fromisoformat(ev["available_at"])
            if av > ad:
                errors.append(f"[cross] evidence {ev['field_id']} available_at {av} > analysis_date {ad}")
        for c in report.get("conclusion", {}).get("maintain_conditions", []) + report.get("conclusion", {}).get("falsification", []):
            if not c.get("id") or not c.get("metric") or not c.get("threshold"):
                errors.append(f"[cross] 条件 {c.get('id')} 缺 metric/threshold")
    return (not errors), errors
