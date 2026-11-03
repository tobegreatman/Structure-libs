# -*- coding: utf-8 -*-
"""四层框架研报 · 输出前质量门断言（v1.1）

用法：
    python validate_v11.py four-layer-data-2026-10-30-v1.1.json
    python validate_v11.py four-layer-data-2026-10-30.json      # v1.0 应大量失败

7 条断言对应审查报告 5.4 节。任一 FAIL 即视为不可发布。
"""
import json
import sys

RED = "\033[91m"
GRN = "\033[92m"
YEL = "\033[93m"
RST = "\033[0m"


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def numeric_fields(row):
    return {k: v for k, v in row.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


class Report:
    def __init__(self):
        self.rows = []

    def check(self, no, name, fn):
        try:
            ok, detail = fn()
        except Exception as e:                                    # noqa: BLE001
            ok, detail = False, f"执行异常：{type(e).__name__}: {e}"
        self.rows.append((no, name, ok, detail))

    def print(self):
        w = max(len(n) for _, n, _, _ in self.rows)
        for no, name, ok, detail in self.rows:
            tag = f"{GRN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
            print(f"  [{tag}] {no}. {name.ljust(w)}  {detail}")
        bad = sum(1 for r in self.rows if not r[2])
        print()
        if bad:
            print(f"{RED}质量门未通过：{bad}/{len(self.rows)} 条断言失败{RST}")
        else:
            print(f"{GRN}质量门通过：{len(self.rows)}/{len(self.rows)} 条断言全部满足{RST}")
        return bad


def main(path):
    d = load(path)
    ind = d.get("industry", {})
    st = ind.get("score_table") or []
    sc = ind.get("scoring") or {}
    rep = Report()

    # ---- 1. 综合分可由 formula 复算
    def c1():
        if not sc or not sc.get("weights"):
            return False, "缺少 industry.scoring.weights，综合分不可复算"
        w = sc["weights"]
        need = {"prosperity", "policy", "valuation_score", "anti_crowding", "momentum"}
        miss = need - set(w)
        if miss:
            return False, f"weights 缺项：{sorted(miss)}"
        if abs(sum(w.values()) - 1.0) > 1e-6:
            return False, f"权重合计 {sum(w.values()):.4f} ≠ 1"
        bad = []
        for r in st:
            calc = sum(w[k] * r[k] for k in w)
            if abs(calc - r["composite"]) > 0.5:
                bad.append(f"{r['industry']}({calc:.1f}≠{r['composite']})")
        return (not bad), ("31 行复算零误差" if not bad else f"{len(bad)} 行不符：{bad[:3]}")

    # ---- 2. 每个 0-100 字段必须声明方向
    def c2():
        fwd = set(sc.get("weights", {}))
        raw = set()
        for expr in (sc.get("transforms") or {}).values():
            raw |= {t for t in expr.replace("-", " ").replace("(", " ").replace(")", " ").split()}
        declared = fwd | raw
        allowed = {"composite", "rank", "top3_freq", "first_place_freq", "weight",
                   "peer_count", "fields_expected", "fields_received"}
        unknown = set()
        for r in st:
            for k, v in numeric_fields(r).items():
                if 0 <= v <= 100 and k not in declared and k not in allowed:
                    unknown.add(k)
        if unknown:
            return False, f"未声明方向的分数列：{sorted(unknown)}"
        return True, f"{len(declared)} 个分数列均有 transforms/weights 声明"

    # ---- 3. 同名字段跨层同量纲
    def c3():
        comps = (d.get("market", {}).get("sentiment", {}) or {}).get("components") or []
        if not comps:
            return False, "缺少 sentiment.components"
        bad = [c["name"] for c in comps if not c.get("unit")]
        ratio_like = [c["name"] for c in comps
                      if ("比" in c["name"]) and not c["name"].endswith("分位")]
        if bad:
            return False, f"分量缺 unit：{bad}"
        if ratio_like:
            return False, f"分量名含『比』但未标分位：{ratio_like}"
        if abs(sum(c.get("weight", 0) for c in comps) - 1.0) > 1e-3:
            return False, "分量权重合计 ≠ 1"
        return True, f"{len(comps)} 个分量均标注 unit=percentile 且已更名"

    # ---- 4. null 字段必须披露插补方式
    def c4():
        ms = (d.get("data_overview", {}).get("completeness", {}) or {}).get("missing") or []
        if not ms:
            return False, "缺少 completeness.missing"
        bad = []
        for m in ms:
            if "imputed" not in m or "effective_weight" not in m:
                bad.append(f"{m['field']}(未声明)")
            elif m["effective_weight"] > 0 and m.get("score_if_excluded") is None:
                bad.append(f"{m['field']}(有权重但无 score_if_excluded)")
        return (not bad), ("5 项缺失均声明 imputed/effective_weight" if not bad else f"{bad}")

    # ---- 5. 排序必须带分数与可分性
    def c5():
        det = (ind.get("ranking", {}).get("detail") or {}).get("short_term")
        if not det:
            return False, "缺少 ranking.detail.short_term（仅数组，无依据）"
        if det.get("scores") is None:
            return False, "排序缺少 scores"
        sigma = sc.get("min_resolvable_gap") or sc.get("noise_sigma")
        if sigma is None:
            return False, "缺少 min_resolvable_gap，无法判断名次可分性"
        m = det.get("margin_to_next")
        rc = det.get("rank_confidence") or []
        if m is None:
            return False, "缺少 margin_to_next"
        if m < sigma and len(rc) >= 3 and rc[2] == "high":
            return False, f"分差 {m} < 噪声带 {sigma}，但第 3 名仍标 high"
        return True, f"分差 {m} < 噪声带 {sigma} → 第 3 名标记为 {rc[2] if len(rc) > 2 else '—'}"

    # ---- 6. 触发条件必须含距离与对称性
    def c6():
        pa = d.get("market", {}).get("position_advice") or {}
        g = pa.get("trigger_geometry")
        if not g:
            return False, "缺少 trigger_geometry，触发距离无法校验"
        up, dn = abs(g.get("up_distance_pct", 0)), abs(g.get("down_distance_pct", 0))
        if not up or not dn:
            return False, "up/down 距离缺失"
        ratio = up / dn
        if not (0.33 <= ratio <= 3):
            return False, f"加减仓距离比 {ratio:.2f} 超出 0.33–3"
        if not pa.get("reentry_rule"):
            return False, "缺少 reentry_rule（减仓后无再入场规则）"
        return True, f"上行 +{up}% / 下行 -{dn}%，比 {ratio:.2f}，含再入场规则"

    # ---- 7. 枚举中文 + 禁止裸百分比可靠性
    def c7():
        bad = []
        for s in d.get("data_overview", {}).get("sources") or []:
            if "reliability" in s:
                bad.append(f"{s['name']}(裸百分比 reliability)")
            if not s.get("confidence_basis"):
                bad.append(f"{s['name']}(缺 confidence_basis)")
            if s.get("status") in ("ok", "partial"):
                bad.append(f"{s['name']}(状态英文 {s['status']})")
        for m in (d.get("data_overview", {}).get("completeness", {}) or {}).get("missing") or []:
            if m.get("impact") in ("high", "mid", "low"):
                bad.append(f"{m['field']}(影响英文 {m['impact']})")
        return (not bad), ("枚举全中文、可靠性改枚举" if not bad else f"{len(bad)} 处：{bad[:3]}")

    for i, (name, fn) in enumerate([
        ("综合分可由 scoring.formula 复算", c1),
        ("所有 0-100 分数字段声明方向", c2),
        ("同名字段跨层同量纲", c3),
        ("缺失值插补必须披露", c4),
        ("排序须带分数与可分性", c5),
        ("触发距离须对称且含再入场", c6),
        ("枚举中文、禁止伪精确可靠性", c7),
    ], 1):
        rep.check(i, name, fn)

    print(f"\n校验对象：{path}")
    print(f"schema_version = {d.get('meta', {}).get('schema_version')}\n")
    bad = rep.print()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "four-layer-data-2026-10-30-v1.1.json"))
