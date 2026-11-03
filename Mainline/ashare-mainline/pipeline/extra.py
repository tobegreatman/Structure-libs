"""补算「仅通过 1 条闸门」板块的 P2（供 rotation.emerging 使用）。

    python -m pipeline.extra --as-of 2026-11-02 --boards 数字媒体,电视广播Ⅱ,医疗美容,医疗服务

run.py 只为 P1/P3 已通过的板块拉成分与 K 线；P2 未算的板块不能算作「未通过」，
要进 rotation.emerging 就必须把 P2 真算出来。本模块只补这一块，输出
cache/extra-<as_of>.json：每个板块的 up_ratio / new_high_ratio / valid / degraded。
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day, http, kline, quotes  # noqa: E402

CACHE = ROOT / "pipeline" / "cache"
CONST_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
             "reportName=RPT_BOARD_CONSTITUENT&columns=BOARD_CODE_BK,SECURITY_CODE"
             "&pageNumber=1&pageSize=500&filter=(BOARD_CODE_BK%3D%22{code}%22)")


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--fresh", action="store_true", help="丢弃已有结果，只保留本次板块")
    ap.add_argument("--boards", required=True, help="逗号分隔的板块名称")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)

    fac = json.loads((CACHE / f"factors-{args.as_of}.json").read_text(encoding="utf-8"))
    ix = {v["name"]: v["code"] for v in fac["rows"].values()}
    names = [n.strip() for n in args.boards.split(",") if n.strip()]
    missing = [n for n in names if n not in ix]
    if missing:
        raise SystemExit(f"面板里没有：{missing}")
    pairs = [(ix[n], n) for n in names]

    cli = http.make_client(workers=3, max_rps=3.0, min_span=0)
    cons: dict[str, list[str]] = {}
    for code, _ in pairs:
        j = cli.get_json(CONST_URL.format(code=code))
        rows = ((j.get("result") or {}).get("data")) or []
        cons[code] = [str(r["SECURITY_CODE"]).zfill(6) for r in rows if r.get("SECURITY_CODE")]
        (CACHE / f"codes-{code}.json").write_text(
            json.dumps(cons[code], ensure_ascii=False), encoding="utf-8")

    codes = list(dict.fromkeys(c for v in cons.values() for c in v))
    qpath = CACHE / f"quotes-{args.as_of}.json"
    snap = json.loads(qpath.read_text(encoding="utf-8")) if qpath.exists() else {}
    need = [c for c in codes if c not in snap]
    if need:
        snap.update(quotes.fetch(cli, need))
        qpath.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    dest = CACHE / f"extra-{args.as_of}.json"
    p = pickle.loads((CACHE / f"panel-{args.as_of}.pkl").read_bytes())
    cal = sorted(p["by_date"])
    store = kline.Store(CACHE / "kline.sqlite")
    try:
        r = kline.ensure_history(cli, store, codes, args.as_of, need=45, calendar=cal)
        print(f"  K 线补历史：{r}")
    except http.EndpointBlocked as e:
        print(f"  ⚠ 建库接口不可用，用库内数据继续：{e}")
    w = kline.append_from_quotes(store, {c: snap[c] for c in codes if c in snap}, args.as_of)
    print(f"  当日报价入库 {w.get('written')} 行")

    # 增量写：只补新板块时不能把上一批结果冲掉，否则 rotation.emerging 会悄悄变样。
    # 需要整体重算时用 --fresh。
    out = {"as_of": args.as_of, "rows": {}}
    if dest.exists() and not args.fresh:
        prev = json.loads(dest.read_text(encoding="utf-8"))
        out["rows"].update(prev.get("rows") or {})
        print(f"  沿用已有 {len(prev.get('rows') or {})} 个板块结果，本次覆盖同名项")
    for code, name in pairs:
        b = kline.breadth(store, cons[code], args.as_of, snap, calendar=cal)
        out["rows"][code] = {"name": name, **{k: b.get(k) for k in
                            ("up_ratio", "new_high_ratio", "valid", "degraded",
                             "hole_count", "stale")}}
        print(f"  {name:<10} 上涨 {(b.get('up_ratio') or 0) * 100:5.1f}% "
              f"新高 {(b.get('new_high_ratio') or 0) * 100:5.1f}% 有效 {b.get('valid')} "
              f"退化 {bool(b.get('degraded'))}")
    store.close()
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
