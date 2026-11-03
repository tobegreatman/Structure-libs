"""每日取数编排。

    python -m pipeline.run --as-of 2026-10-30

产出 pipeline/cache/run-<as_of>.json：面板摘要、P1/P3 全样本、候选板块的 P2、
三闸门矩阵，以及逐阶段耗时。报告生成端只需读这个文件，不必再碰网络。
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import contract, day, http, kline, panel, quotes  # noqa: E402

SINCE = "2025-09-15"
P1_DAYS, P1_STREAK = 6, 2
P3_CHG, P3_PCT = 0.005, 0.60
CACHE = ROOT / "pipeline" / "cache"


def estimate_requests(as_of: str) -> int:
    """计划请求数：优先用上一轮同基准日的实测值，再打 4% 余量往下取。

    为什么要往下取：`plan()` 用「剩余时间 ÷ 剩余请求数」定 gap，估计偏大就会提前
    跑完，40 秒下限落空；估计偏小只会略微拖长，方向是安全的。所以这里做**保守低估**。
    """
    prev = CACHE / f"run-{as_of}.json"
    try:
        ok = int(((json.loads(prev.read_text(encoding="utf-8")).get("requests") or {})
                  .get("ok") or 0))
    except (OSError, ValueError, TypeError):
        ok = 0
    return max(8, int(ok * 0.96)) if ok else 160


class Timer:
    def __init__(self) -> None:
        self.rows: list[tuple[str, float, str]] = []
        self._t = time.time()

    def mark(self, name: str, note: str = "") -> None:
        now = time.time()
        self.rows.append((name, now - self._t, note))
        self._t = now

    def table(self) -> list[dict]:
        return [{"stage": n, "seconds": round(s, 2), "note": k} for n, s, k in self.rows]


def p1_metrics(panel_data: dict, code: str, n: int = 20) -> dict:
    return panel.top_days(panel_data, code, n=n, top=10)


def p3_metrics(panel_data: dict, code: str) -> dict:
    series = panel.share_series(panel_data, code)
    if len(series) < 6:
        return {"turnover_share": None, "chg_5": None, "pct_250": None}
    shares = [s for _, s in series]
    cur = shares[-1]
    base = shares[-6]
    delta = cur - base
    pct = sum(1 for s in shares if s <= cur) / len(shares)
    return {
        "turnover_share": round(cur, 6),
        "chg_5": round(delta, 6),
        "pct_250": round(pct, 4),
        "samples": len(shares),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--workers", type=int, default=http.load_policy().workers)
    ap.add_argument("--rps", type=float, default=http.load_policy().max_rps)
    ap.add_argument("--min-span", type=float, default=http.load_policy().min_span,
                    help="整轮请求的最小铺开时长（秒）；0 关闭预算，只按 --rps 限速")
    ap.add_argument("--cache-ttl", type=float, default=0.0,
                    help="响应缓存 TTL（秒）。>0 时同一 URL 在 TTL 内复用，用于断点续跑；"
                         "分析口径要求全量真取，默认 0")
    ap.add_argument("--journal", default="",
                    help="逐请求 journal 落盘路径，默认 cache/http-<as_of>.jsonl")
    ap.add_argument("--no-journal", action="store_true")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)

    tm = Timer()
    jpath = (None if args.no_journal
             else Path(args.journal) if args.journal
             else CACHE / f"http-{args.as_of}.jsonl")
    if jpath:
        jpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text("", encoding="utf-8")
    cli = http.make_client(workers=args.workers, max_rps=args.rps,
                           min_span=args.min_span, cache_ttl=args.cache_ttl,
                           journal=jpath)

    print("=" * 62)
    print(f"主线取数 · {args.as_of}")
    print("=" * 62)
    print(f"  策略 并发 {cli.workers} / 上限 {cli.policy.max_rps} rps / "
          f"铺开 ≥{cli.policy.min_span:.0f}s / 缓存 TTL {cli.policy.cache_ttl:.0f}s")
    cli.plan(estimate_requests(args.as_of), note="(按上一轮实测数低估)")

    print("\n[1] 接口契约前置校验")
    t_start = time.time()
    try:
        _, degraded = contract.enforce(cli, contract.builtin(args.as_of, args.since))
    except contract.ContractError as e:
        print(f"\n{e}")
        print(f"\n在第 {time.time() - t_start:.2f} 秒中止，未发出任何全量请求。")
        return 2
    tm.mark("契约校验", f"可降级 {len(degraded)} 项" if degraded else "4 个源各 1 个样本")

    print("\n[2] 板块面板")
    p = panel.build(cli, args.as_of, args.since)
    # 面板当场落盘：下游 factors / build_report 都读这个 pkl。
    # 以前要另跑 build_panel_cache.py，等于把 128 个板块的日线再拉一遍——
    # 同一份数据两次全量请求，既浪费也是限流的主要来源。
    pkl = ROOT / "pipeline" / "cache" / f"panel-{args.as_of}.pkl"
    pkl.parent.mkdir(parents=True, exist_ok=True)
    pkl.write_bytes(pickle.dumps(p))
    tm.mark("板块面板", f"{len(p['boards'])} 个现役板块 / {p['span']} 日")

    print("\n[3] P1 / P3 全样本穷举")
    p1 = {b["code"]: p1_metrics(p, b["code"]) for b in p["boards"]}
    p3 = {b["code"]: p3_metrics(p, b["code"]) for b in p["boards"]}
    cand = []
    for b in p["boards"]:
        c = b["code"]
        m1, m3 = p1[c], p3[c]
        gate1 = m1["days_top10_20"] >= P1_DAYS and m1["consecutive_top10"] >= P1_STREAK
        gate3 = (m3["chg_5"] or 0) >= P3_CHG and (m3["pct_250"] or 0) >= P3_PCT
        if gate1 or gate3:
            cand.append({"code": c, "name": b["name"], "p1": gate1, "p3": gate3, **m1, **m3})
    cand.sort(key=lambda x: (x["p1"] and x["p3"], x["p1"], x["p3"]), reverse=True)
    print(f"  P1 通过 {sum(1 for x in cand if x['p1'])} 个 | P3 通过 {sum(1 for x in cand if x['p3'])} 个 "
          f"| 任一通过 {len(cand)} 个")
    tm.mark("P1/P3 穷举", f"{len(p['boards'])} 个板块")

    # 面板拉完，真实板块数与候选数都已确定：按「已发 + 已知剩余」重算 gap。
    # K 线那一段的请求数事前不可知（取决于库内缺口），故意不计入——低估只会让
    # 整轮略微拖长，而高估会提前跑完、让 40 秒下限失效。
    cli.plan(cli.scheduled + len(cand) + 9, note="(成分+报价，K 线另计)")

    print("\n[4] 候选板块成分股")
    cons = {}
    for c in cand:
        url = (f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
               f"reportName=RPT_BOARD_CONSTITUENT&columns=BOARD_CODE_BK,SECURITY_CODE"
               f"&pageNumber=1&pageSize=500&filter=(BOARD_CODE_BK%3D%22{c['code']}%22)")
        try:
            j = cli.get_json(url)
            rows = ((j.get("result") or {}).get("data")) or []
            cons[c["code"]] = [r["SECURITY_CODE"] for r in rows if r.get("SECURITY_CODE")]
        except Exception as e:  # noqa: BLE001
            print(f"  {c['name']} 成分拉取失败：{e}")
            cons[c["code"]] = []
    all_codes = list(dict.fromkeys(x for v in cons.values() for x in v))
    print(f"  {len(cons)} 个板块，去重后 {len(all_codes)} 只")
    # 成分表落盘：score.py 要按板块匹配涨停/炸板/连板，不能靠上一轮自测留下的旧文件。
    for k, v in cons.items():
        (ROOT / "pipeline" / "cache" / f"codes-{k}.json").write_text(
            json.dumps(v, ensure_ascii=False), encoding="utf-8")
    (ROOT / "pipeline" / "cache" / "codes-INDEX.json").write_text(
        json.dumps({c["code"]: c["name"] for c in cand}, ensure_ascii=False, indent=1), encoding="utf-8")
    tm.mark("成分股", f"{len(all_codes)} 只")

    print("\n[5] 批量报价")
    q = quotes.fetch(cli, all_codes)
    print(f"  {len(all_codes)} 只 -> 命中 {len(q)} 只 | 交易日 {quotes.trade_date(q)}")
    # 报价快照必须落盘：score.py 判龙头要按当日成交额排序，没有快照它就得自己再拉 9 次
    # （同一份数据两次全量请求）。盘后价格不变，同一轮内复用是安全的；跨轮由文件日期兜住。
    qfile = CACHE / f"quotes-{args.as_of}.json"
    qfile.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
    tm.mark("批量报价", f"{(len(all_codes) + quotes.BATCH - 1) // quotes.BATCH} 次请求")

    print("\n[6] 本地 K 线库")
    store = kline.Store(ROOT / "pipeline" / "cache" / "kline.sqlite")
    have = store.stats()["rows"]
    # 交易日历用面板口径：by_date 就是东财给的真实交易日，缺口检测全靠它比对。
    cal = sorted(p["by_date"])
    # 顺序不能反：先补历史、再落当日。当日的复权换算要用「库内最后一根」当锚点
    # （h = P × c0 / PC，PC 是报价昨收），若先写当日，最后就变成了今天自己，锚点失效。
    # ensure_history 允许 as_of 当天缺失，所以先跑它不会触发全量重拉。
    try:
        r = kline.ensure_history(cli, store, all_codes, args.as_of, need=45, calendar=cal)
    except http.EndpointBlocked as e:
        if have == 0:
            print(f"  {e}")
            print("  库为空且建库接口不可用，无法计算 P2，中止。")
            return 3
        print(f"  {e}")
        print(f"  用库内已有 {have} 行继续，P2 覆盖会相应下降")
        r = {"mode": "cache-only", "requested": 0, "filled": 0, "failed": 0}
    w = kline.append_from_quotes(store, q, args.as_of)
    if w.get("skipped"):
        print(f"  ⚠ 报价未入库：{w['skipped']}")
    else:
        tail = "（盘中快照，收盘后重跑会整只修正）" if w.get("provisional") else ""
        print(f"  当日报价入库 {w['written']} 行 @ {w['trade_date']}{tail}")
    if w.get("exdiv"):
        print(f"  检测到 {len(w['exdiv'])} 只除权（原始价/昨收偏离），已按昨收自动换算复权："
              f"{[c for c, _ in w['exdiv'][:5]]}")
    # 修补没成功的话标记还在，说明库内可能仍是盘中价——P2 不能当收盘价用。
    prov = store.get_meta("provisional")
    prov_bad = bool(prov) and prov <= args.as_of and (not cal or prov >= cal[-45])
    if prov_bad:
        print(f"  ⚠ 库内 {prov} 仍是盘中快照且未修正成功，本轮 P2 判不可用")
    tm.mark("K 线库", f"{r['mode']}，请求 {r['requested']} 次")

    print("\n[7] P2 广度")
    p2 = {}
    for c in cand:
        codes = cons.get(c["code"]) or []
        p2[c["code"]] = (kline.breadth(store, codes, args.as_of, q, calendar=cal)
                         if codes else {"valid": 0})
    # 缓存有洞的板块必须 fail-closed：宁可承认「P2 不可用」，也不要用拉伸过的窗口判过。
    gate2 = {k: (not v.get("degraded", False)) and not prov_bad
                and (v.get("up_ratio") or 0) >= 0.55
                and (v.get("new_high_ratio") or 0) >= 0.08
             for k, v in p2.items()}
    degrad = {k: v for k, v in p2.items() if v.get("degraded")}
    if degrad:
        print(f"  ⚠ {len(degrad)} 个板块的 K 线窗口有缺口，P2 判不可用（fail-closed）：")
        for k, v in degrad.items():
            nm = next((c["name"] for c in cand if c["code"] == k), k)
            print(f"    {nm}：缺日 {v.get('hole_dates', [])[:5]} 停更 {v.get('stale', [])[:5]}")
    tm.mark("P2 广度", f"{len(cand)} 个板块" + (f"，{len(degrad)} 个退化" if degrad else ""))

    print("\n" + "=" * 62)
    print("三闸门矩阵")
    print("=" * 62)
    print(f"  {'板块':<12}{'P1':>4}{'P2':>4}{'P3':>4}  判定")
    rows_out = []
    for c in cand:
        k = c["code"]
        n = sum([c["p1"], gate2.get(k, False), c["p3"]])
        v2 = p2.get(k, {})
        verdict = "进榜候选" if n >= 2 else "单条，不入榜"
        if v2.get("degraded"):
            verdict = "P2 不可用（K 线有缺口），不入榜"
        p2mark = "!" if v2.get("degraded") else ("✓" if gate2.get(k) else "·")
        print(f"  {c['name']:<12}{'✓' if c['p1'] else '·':>4}"
              f"{p2mark:>4}{'✓' if c['p3'] else '·':>4}  "
              f"{n} 条 | 上涨 {(v2.get('up_ratio') or 0) * 100:.1f}% 新高 {(v2.get('new_high_ratio') or 0) * 100:.1f}% | {verdict}")
        rows_out.append({
            "code": k, "name": c["name"],
            "p1": c["p1"], "p2": gate2.get(k), "p3": c["p3"], "gates": n,
            "p2_degraded": bool(v2.get("degraded")),
            "p2_holes": v2.get("hole_count", 0),
            "days_top10_20": c["days_top10_20"], "consecutive_top10": c["consecutive_top10"],
            "turnover_share_chg_5": c["chg_5"], "turnover_share_pct_250": c["pct_250"],
            "breadth_up_ratio": v2.get("up_ratio"), "new_high_ratio": v2.get("new_high_ratio"),
            "valid": v2.get("valid"),
        })

    passes = [r for r in rows_out if r["gates"] >= 2]
    print(f"\n  过 ≥2 条闸门：{len(passes)} 个" + ("" if passes else " —— 今日无主线"))
    store.close()
    fa = cli.audit()

    out = {
        "as_of": args.as_of,
        "since": args.since,
        "span": p["span"],
        # 面板 pkl 是 run 的中途产物，写盘早于本文件。下游要靠这个指纹判断能否复用，
        # 只比 mtime 大小会把「同一轮」误判成「过期」，白白重拉 128 个板块。
        "panel_pkl": {"mtime": pkl.stat().st_mtime, "bytes": pkl.stat().st_size,
                      "boards": len(p["boards"])},
        "boards_live": len(p["boards"]),
        "boards_stale": p["stale"],
        "market_amount_yi": round((p["market_amount"].get(args.as_of) or 0) / 1e8, 2),
        "gates": rows_out,
        "passes": passes,
        "p2_degraded": [r["name"] for r in rows_out if r["p2_degraded"]],
        "kline_calendar": {"days": len(cal), "first": cal[0] if cal else None,
                           "last": cal[-1] if cal else None},
        "kline_append": w,
        "timing": tm.table(),
        "total_seconds": round(sum(r[1] for r in tm.rows), 2),
        "requests": cli.stats.as_dict(),
        # 取数节奏必须和数字一起落盘：报告里的每个数都源自这轮请求，
        # 「并发多少、摊了多久、有没有被封」不能只靠人肉回忆。
        "fetch_policy": fa["policy"],
        "fetch_audit": fa,
    }
    dest = ROOT / "pipeline" / "cache" / f"run-{args.as_of}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 62)
    print("耗时")
    print("=" * 62)
    for r in tm.table():
        print(f"  {r['stage']:<12}{r['seconds']:>7.2f}s  {r['note']}")
    print(f"  {'合计':<12}{out['total_seconds']:>7.2f}s")

    print("\n" + "=" * 62)
    print("取数节奏")
    print("=" * 62)
    rq = fa["requests"]
    print(f"  实测跨度 {fa['span_seconds']:>6.1f}s / {rq['ok']} 次请求 / "
          f"{fa['achieved_rps']:.2f} rps / 峰值并发 {fa['peak_inflight']}/{cli.workers}")
    print(f"  下载 {rq['bytes'] / 1024:.0f}KB / 失败 {rq['failed']} / 封禁 {rq['blocked']} / "
          f"重试 {rq['retry']} / 缓存命中 {rq['cache_hit']}")
    print(f"  {'host':<32}{'次数':>5}{'KB':>8}{'平均ms':>8}{'rps':>6}")
    for h, v in fa["per_host"].items():
        print(f"  {h:<32}{v['requests']:>5}{v['kb']:>8.1f}{v['avg_ms']:>8.0f}"
              f"{v['rps_applied']:>6.1f}")
    if fa["slow_events"]:
        print("  自适应降速：" + "；".join(
            f"{e['host']} {e['from']}→{e['to']} rps（{e['why']}）" for e in fa["slow_events"][:4]))
    if fa["violations"]:
        print("  ⚠ 节奏违约：" + "；".join(fa["violations"]))
    else:
        print(f"  ✓ 节奏达标：≤{cli.policy.max_rps} rps、并发 ≤{cli.workers}、"
              f"跨度 ≥{cli.policy.min_span:.0f}s")
    print(f"\n  -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
