"""K 线本地库体检 / 修补。

    python -m pipeline.repair_kline --as-of 2026-10-30 --check     # 只体检，不发请求
    python -m pipeline.repair_kline --as-of 2026-10-30             # 修补当前板块成分股
    python -m pipeline.repair_kline --as-of 2026-10-30 --all       # 连历史遗留的孤立代码一起补

体检口径与 run.py 一致：拿交易日历（面板 by_date）逐日比对，
「日历上该有的日子库里没有」就算缺口，而不是看总根数。
停牌造成的个股缺日不算——停牌那几天本来就没有成交。
"""
from __future__ import annotations

import argparse
import bisect
import json
import pickle
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import http, kline  # noqa: E402

CACHE = ROOT / "pipeline" / "cache"


def load_calendar(store: kline.Store, as_of: str) -> list[str]:
    pkl = CACHE / f"panel-{as_of}.pkl"
    if pkl.exists():
        with pkl.open("rb") as f:
            return sorted(pickle.load(f)["by_date"])
    print(f"  （无 {pkl.name}，交易日历退化为库内日期）")
    return sorted(store.global_dates(200))


def board_codes() -> list[str]:
    out: list[str] = []
    for f in sorted(CACHE.glob("codes-BK*.json")):
        out.extend(json.loads(f.read_text(encoding="utf-8")))
    return list(dict.fromkeys(str(c).zfill(6) for c in out))


def audit(store: kline.Store, codes: list[str], as_of: str, cal: list[str], need: int) -> dict:
    """体检口径：只要「最后一根 == as_of 或它的前一交易日」且根数够，就算达标。

    允许差一天，是因为当日的 K 线由 `append_from_quotes` 从报价写入，补历史本身
    不负责它——把当日算成缺，跑完补库还是会报一堆 stale。

    中间缺日不算错——停牌股天然缺日（新华传媒 600825 停 3 天、路畅科技 002813 停 10 天），
    次新股天然不够 45 根。真正要抓的是「最后一天停在更早」，那才是缓存断更。
    """
    need_eff = min(need, len(cal)) if cal else need
    have = store.window_dates(as_of, need_eff)
    idx = bisect.bisect_right(cal, as_of) - 1
    ok_max = {cal[i] for i in (idx, idx - 1) if i >= 0} or {as_of}
    empty, stale, short, ok = [], [], [], 0
    for c in codes:
        got = have.get(c) or set()
        if not got:
            empty.append(c)
        elif max(got) not in ok_max:
            stale.append(c)
        elif len(got) < need_eff - 1:     # 允许差当日那一根
            short.append(c)          # 次新/长期停牌：根数本来就不够
        else:
            ok += 1
    return {"total": len(codes), "ok": ok, "empty": empty,
            "stale": stale, "short": short, "need": need_eff,
            "calendar": (cal[0], cal[-1], len(cal)) if cal else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="2026-10-30")
    ap.add_argument("--need", type=int, default=45)
    ap.add_argument("--check", action="store_true", help="只体检，不发任何请求")
    ap.add_argument("--all", action="store_true", help="连库里历史遗留的孤立代码一起修")
    ap.add_argument("--rebuild", action="store_true",
                    help="清空后全量重建（换复权口径时用；旧库的 c 混了 qfq/不复权两种基准）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rps", type=float, default=3.0)
    args = ap.parse_args()

    store = kline.Store(CACHE / "kline.sqlite")
    cal = load_calendar(store, args.as_of)
    codes = board_codes()
    # 先记下库内出现过的全部代码：rebuild 清表之后这个信息就没了，
    # 而历史遗留的孤立代码（早几轮的板块成分）仍然值得补。
    legacy = [str(r[0]).zfill(6) for r in store._db.execute("SELECT DISTINCT code FROM kline")]
    if args.rebuild:
        # 旧口径的 c 是「腾讯 qfq / 新浪不复权 / 报价原始价」三种混在一起，无法靠
        # 逐行推断属于哪一种，只能整体丢弃重建。dead 名单同理，否则会被跳过。
        n = store.wipe()
        for k in ("dead:" + args.as_of, "provisional"):
            if store.get_meta(k):
                store.del_meta(k)
        print(f"⚠ 已清空 {n} 行（复权口径切换），即将全量重建")
        args.all = True
    if args.all:
        codes = list(dict.fromkeys(codes + legacy))

    stats = store.stats()
    print(f"库：{stats['rows']} 行 / {stats['codes']} 只 / {stats['from']} → {stats['to']}")
    print(f"交易日历：{cal[0]} → {cal[-1]}（{len(cal)} 日）| 待检代码 {len(codes)} 只")

    a = audit(store, codes, args.as_of, cal, args.need)
    def show(a: dict) -> None:
        print(f"达标 {a['ok']} / 断更 {len(a['stale'])} / 无历史 {len(a['empty'])} / 深度不足 {len(a['short'])}")
        if a["stale"]:
            print(f"  断更（最后一根不是 {args.as_of}）：{a['stale'][:10]}")
        if a["short"]:
            print(f"  深度不足（次新/长停）：{a['short'][:10]}")
        if a["empty"]:
            print(f"  无历史（B 股/北交所/退市，接口本身没有）：{a['empty'][:10]}")

    print(f"\n体检（截至 {args.as_of}，需 {a['need']} 根）：", end="")
    show(a)

    if args.check:
        store.close()
        return 0 if not a["stale"] else 1

    t0 = time.time()
    cli = http.make_client(workers=args.workers, max_rps=args.rps, min_span=0)
    print("\n开始修补 …")
    try:
        r = kline.ensure_history(cli, store, codes, args.as_of, need=args.need, calendar=cal)
    except http.EndpointBlocked as e:
        print(f"\n⚠ {e}")
        print("  本次进度已落库，隔几分钟再跑一次即可续上。")
        store.close()
        return 3

    a2 = audit(store, codes, args.as_of, cal, args.need)
    s2 = store.stats()
    print("\n修补后：", end="")
    show(a2)
    print(f"库：{s2['rows']} 行 / {s2['codes']} 只 / {s2['from']} → {s2['to']}")
    print(f"耗时 {time.time() - t0:.1f}s | 请求 {cli.stats.ok} 次 / "
          f"{cli.stats.bytes / 1024:.0f}KB / 失败 {cli.stats.failed} / 熔断 {cli.stats.blocked}")
    if r.get("failed_codes"):
        print(f"仍拉不到 {len(r['failed_codes'])} 只：{r['failed_codes'][:10]}")
    store.close()
    return 0 if not a2["stale"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
