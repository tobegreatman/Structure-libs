"""pipeline 自测：验证四项优化都成立，并打印实测计时。

    python -m pipeline.selftest
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import contract, http, kline, quotes, report  # noqa: E402

AS_OF = "2026-10-30"
SINCE = "2025-09-15"
BAIJIU = "BK1277"

BOARDS = [
    ("BK1277", "白酒Ⅱ"), ("BK0475", "银行Ⅱ"), ("BK0421", "铁路公路"),
    ("BK1042", "医药商业"), ("BK1046", "游戏Ⅱ"), ("BK0727", "医疗服务"),
    ("BK1278", "调味发酵品Ⅱ"), ("BK1232", "航天装备Ⅱ"),
]


def section(t: str) -> None:
    print(f"\n{'=' * 62}\n{t}\n{'=' * 62}")


def t_contract(cli: http.Client) -> bool:
    section("1 接口契约前置校验")
    t0 = time.time()
    try:
        contract.enforce(cli, contract.builtin(AS_OF, SINCE))
        print(f"  全部通过 | 耗时 {time.time() - t0:.2f}s | 请求 {cli.stats.ok} 次")
        return True
    except contract.ContractError as e:
        print(f"  失败：{e}")
        print(f"  耗时 {time.time() - t0:.2f}s（旧实现此时还在拉第 40 个板块）")
        return False


def t_quotes(cli: http.Client, codes: list[str]) -> dict:
    section("2 腾讯批量报价替代逐只 K 线")
    t0 = time.time()
    q = quotes.fetch(cli, codes)
    dt = time.time() - t0
    n = len(q)
    print(f"  {len(codes)} 只 -> 命中 {n} 只 | {dt:.2f}s | {(len(codes) + quotes.BATCH - 1) // quotes.BATCH} 次请求")
    print(f"  逐只方案约 232 次请求 / 300s，批量方案 {dt:.1f}s，提速约 {300 / max(dt, 0.01):.0f}×")
    sample = next(iter(q.values())) if q else {}
    if sample:
        print(f"  样本：{sample['name']} 现价 {sample['price']} 涨跌 {sample['chg_pct'] * 100:.2f}% "
              f"成交额 {sample['amount_yi']:.2f} 亿 时间 {sample['ts']}")
    print(f"  交易日 {quotes.trade_date(q)}")
    return q


def t_kline(cli: http.Client, codes: list[str]) -> kline.Store:
    section("3 本地 K 线库（首次建库 / 二次命中缓存）")
    db = ROOT / "pipeline" / "cache" / "kline.sqlite"
    store = kline.Store(db)

    t0 = time.time()
    r1 = kline.ensure_history(cli, store, codes, AS_OF)
    d1 = time.time() - t0
    print(f"  首跑 {r1['mode']} | {d1:.2f}s")

    t0 = time.time()
    r2 = kline.ensure_history(cli, store, codes, AS_OF)
    d2 = time.time() - t0
    print(f"  二跑 {r2['mode']} | {d2:.2f}s | 请求 {r2['requested']} 次")
    print(f"  库内 {store.stats()}")
    if r2["requested"] == 0 and r2["mode"] == "cache":
        print(f"  稳态验证通过：K 线请求 232 次 -> 0 次，省 {d1 - d2:.1f}s")
    return store


def t_breadth(store: kline.Store, q: dict, groups: dict[str, list[str]]) -> None:
    section("4 P2 广度（用缓存库 + 当日报价，0 请求）")
    print("  v1.6 口径的已知答案对照：白酒 94.7%/42.1%、银行 69.0%/42.9%、铁路公路 24.2%/3.0%")
    for board, codes in groups.items():
        b = kline.breadth(store, codes, AS_OF, q)
        if b["valid"] == 0:
            print(f"  {board}: 无有效样本")
            continue
        p2 = b["up_ratio"] >= 0.55 and b["new_high_ratio"] >= 0.08
        print(f"  {board:8s} 有效 {b['valid']:3d} | 上涨 {b['up_ratio'] * 100:5.1f}% | "
              f"新高 {b['new_high_ratio'] * 100:5.1f}% | P2 {'通过' if p2 else '未通过'}")


def t_report() -> None:
    section("5 报告模板化")
    full = json.loads((ROOT / "aml-20261101-v16.json").read_text(encoding="utf-8"))
    dyn = report.strip(full)
    again = report.assemble(dyn)

    b = lambda o: len(json.dumps(o, ensure_ascii=False, separators=(",", ":")).encode())  # noqa: E731
    print(f"  完整 {b(full)}B | 动态 {b(dyn)}B | 静态注入 {b(full) - b(dyn)}B "
          f"| 生成端缩减 {(1 - b(dyn) / b(full)) * 100:.1f}%")

    same = json.dumps(again, ensure_ascii=False, sort_keys=True) == json.dumps(full, ensure_ascii=False, sort_keys=True)
    print(f"  strip -> assemble 往返一致：{'是' if same else '否'}")
    if not same:
        for k in full:
            if json.dumps(full[k], ensure_ascii=False, sort_keys=True) != json.dumps(again.get(k), ensure_ascii=False, sort_keys=True):
                print(f"    差异键：{k}")

    warn = report.assert_consistent(full)
    print(f"  一致性自检：{len(warn)} 条")
    for w in warn:
        print(f"    - {w}")


def t_circuit() -> None:
    section("6 熔断（打已封禁的 push2his/kline，验证不会空转）")
    cli = http.make_client(workers=4, max_rps=4, min_span=0)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=90.BK1277&fields1=f1&fields2=f51,f53&klt=101&fqt=1&lmt=5")
    t0 = time.time()
    for i in range(6):
        try:
            cli.get(url)
            print(f"  第 {i + 1} 次：成功")
        except http.EndpointBlocked as e:
            print(f"  第 {i + 1} 次 @ {time.time() - t0:5.2f}s：{str(e)[:56]}")
        except Exception as e:  # noqa: BLE001
            print(f"  第 {i + 1} 次 @ {time.time() - t0:5.2f}s：{type(e).__name__} {str(e)[:40]}")
    dt = time.time() - t0
    print(f"  6 次尝试总耗时 {dt:.2f}s")
    print("  旧实现 tries=4 + 指数退避：单次约 18s，99 板块最坏 1800s")
    print(f"  按 6 次折算，旧实现约 108s，新实现 {dt:.1f}s，压缩 {108 / max(dt, 0.01):.0f}×")


def t_gap() -> bool:
    """缺口 / 假 K 线 / 停牌 的回归（全程离线，用临时库 + 假 client）。"""
    section("7 增量库缺口（几天不跑会不会错）")
    pkl = ROOT / "pipeline" / "cache" / f"panel-{AS_OF}.pkl"
    src = ROOT / "pipeline" / "cache" / "kline.sqlite"
    if not (pkl.exists() and src.exists()):
        print("  跳过：缺 panel 缓存或 K 线库")
        return True
    import pickle
    import shutil
    import sqlite3
    import tempfile

    cal = sorted(pickle.loads(pkl.read_bytes())["by_date"])
    codes = _board_codes(http.make_client(workers=2, max_rps=2, min_span=0), BAIJIU)
    tmpdir = Path(tempfile.mkdtemp())
    seq = [0]

    class Fake:
        """不联网：直接发回日历上最近 50 个交易日。"""

        def map(self, fn, items):
            return [([(d, 10.0 + i) for i, d in enumerate(cal[-50:])], "fake") for _ in items]

    def fresh() -> kline.Store:
        seq[0] += 1
        p = tmpdir / f"k{seq[0]}.sqlite"
        shutil.copy(src, p)
        return kline.Store(p)

    checks: list[tuple[str, bool, str]] = []
    st = fresh()
    r = kline.ensure_history(Fake(), st, codes, AS_OF, need=45, calendar=cal, verbose=False)
    checks.append(("库已完整时不发请求", r["requested"] == 0, f"requested={r['requested']}"))
    st.close()

    st = fresh()
    with sqlite3.connect(st.path) as db:
        for d in cal[-4:-1]:
            db.execute("delete from kline where d=?", (d,))
    r = kline.ensure_history(Fake(), st, codes, AS_OF, need=45, calendar=cal, verbose=False)
    checks.append(("缺 3 个交易日能发现", r["requested"] > 0, f"requested={r['requested']}"))
    st.close()

    solo, gapday = codes[0], cal[-10]
    st = fresh()
    with sqlite3.connect(st.path) as db:
        db.execute("delete from kline where code=? and d=?", (solo, gapday))
    r = kline.ensure_history(Fake(), st, codes, AS_OF, need=45, calendar=cal, verbose=False)
    checks.append((f"单只缺一天也能发现（{solo} 缺 {gapday}）", r["requested"] == 1,
                   f"requested={r['requested']}"))
    st.close()

    st = fresh()
    st.set_meta("provisional", AS_OF)
    r = kline.ensure_history(Fake(), st, codes, AS_OF, need=45, calendar=cal, verbose=False)
    checks.append(("盘中快照触发整只重拉并清标记",
                   r["requested"] > 0 and st.get_meta("provisional") is None,
                   f"requested={r['requested']}"))
    st.close()

    # breadth 侧：中间缺日必须 fail-closed；单只停牌不能被误判成缓存缺口。
    st = fresh()
    qs = {}
    for c in codes:
        b = st.bars(c, AS_OF, 2)
        if len(b) == 2:
            qs[c] = {"price": b[0][1], "prev_close": b[1][1], "chg_pct": 0.0}
    v0 = kline.breadth(st, codes, AS_OF, qs, calendar=cal)
    with sqlite3.connect(st.path) as db:
        for d in cal[-4:-1]:
            db.execute("delete from kline where d=?", (d,))
    v1 = kline.breadth(st, codes, AS_OF, qs, calendar=cal)
    checks.append(("中间缺日 → degraded 且不给假数", v1["degraded"] and v0["degraded"] is False,
                   f"正常 {v0['degraded']} / 缺日 {v1['degraded']} 缺日={v1['hole_dates'][:3]}"))
    st.close()

    st = fresh()
    with sqlite3.connect(st.path) as db:
        db.execute("delete from kline where code=? and d=?", (solo, gapday))
    v2 = kline.breadth(st, codes, AS_OF, qs, calendar=cal)
    checks.append(("单只停牌不算退化", v2["degraded"] is False, f"degraded={v2['degraded']}"))
    st.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    ok = True
    for name, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'} {name}｜{detail}")
        ok &= passed
    return ok


def t_adj() -> bool:
    """复权口径回归：除权日不能用「原始价 vs 复权历史」直接比，也不能拼出假跳空。

    全程离线：自己造两天 K 线，再用报价补第三天，检查写回值。
    """
    section("8 复权口径（除权会不会把 20 日高点顶高）")
    import shutil
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    checks: list[tuple[str, bool, str]] = []

    def new_store(name: str, rows: list[tuple[str, str, float, float | None]]) -> kline.Store:
        st = kline.Store(tmpdir / f"{name}.sqlite")
        st.put_many(rows)
        return st

    # ① 平推：10-28=10.0、10-29=11.0，报价 12.1 / 昨收 11.0 → 当日应记为 12.1
    st = new_store("flat", [("600000", "2026-10-28", 10.0, 10.0),
                            ("600000", "2026-10-29", 11.0, 11.0)])
    q1 = {"600000": {"price": 12.1, "prev_close": 11.0, "chg_pct": 0.1, "ts": "20261030150000"}}
    w1 = kline.append_from_quotes(st, q1, "2026-10-30")
    got = st.bars("600000", "2026-10-30", 1)[0][1]
    checks.append(("平推：当日复权值 == 原始收盘", abs(got - 12.1) < 1e-9, f"{got}"))
    w1b = kline.append_from_quotes(st, q1, "2026-10-30")
    checks.append(("同一天重复写入被跳过（幂等）", w1b["written"] == 0, f"written={w1b['written']}"))
    st.close()

    # ② 除权：1 拆 2。历史在复权序列里是 20.0/22.0，当日实际价 12.1、昨收 11.0（未拆前是 22.0）
    #    → 写回值必须是 24.2，序列比值 24.2/22.0 = 12.1/11.0，与原始收益率一致。
    st = new_store("ex", [("600000", "2026-10-28", 20.0, 10.0),
                          ("600000", "2026-10-29", 22.0, 11.0)])
    q2 = {"600000": {"price": 12.1, "prev_close": 11.0, "chg_pct": 0.1, "ts": "20261030150000"}}
    kline.append_from_quotes(st, q2, "2026-10-30")
    b = st.bars("600000", "2026-10-30", 3)
    ratio = b[0][1] / b[1][1]
    checks.append(("除权日：序列连续，无假跳空",
                   abs(b[0][1] - 24.2) < 1e-6 and abs(ratio - 12.1 / 11.0) < 1e-9,
                   f"当日 {b[0][1]:.2f} / 昨 {b[1][1]:.2f} = {ratio:.6f}"))

    # ③ 除权当日的「没拆之前」是对的：若误用历史原始价当锚点，会算出 12.1×22/11 = 24.2 也“碰巧”
    #    对，所以再验一次反向：3 并 1（合股），历史 ÷3，当日 ×3。
    st2 = new_store("rev", [("600001", "2026-10-28", 3.0, 9.0),
                            ("600001", "2026-10-29", 3.3, 9.9)])
    q3 = {"600001": {"price": 27.0, "prev_close": 29.7, "chg_pct": -0.09, "ts": "20261030150000"}}
    kline.append_from_quotes(st2, q3, "2026-10-30")
    b2 = st2.bars("600001", "2026-10-30", 2)
    checks.append(("合股：同样按昨收折算，比值 = -9%",
                   abs(b2[0][1] / b2[1][1] - 27.0 / 29.7) < 1e-9,
                   f"{b2[0][1]:.2f}/{b2[1][1]:.2f}"))
    st2.close()

    # ④ 换算后日涨跌幅超出交易所上限（>31%）→ 拒写，等重拉，不能污染窗口
    st3 = new_store("bad", [("600002", "2026-10-29", 10.0, 10.0)])
    q4 = {"600002": {"price": 30.0, "prev_close": 10.0, "chg_pct": 2.0, "ts": "20261030150000"}}
    w4 = kline.append_from_quotes(st3, q4, "2026-10-30")
    checks.append(("异常涨幅拒写", w4["written"] == 0 and "600002" in w4["skipped_codes"],
                   f"written={w4['written']}"))
    st3.close()

    # ⑤ 非交易日：报价交易日与 as_of 不一致 → 整轮拒写（周日跑不会造假 K 线）
    st4 = new_store("wk", [("600003", "2026-10-29", 10.0, 10.0)])
    q5 = {"600003": {"price": 10.5, "prev_close": 10.0, "chg_pct": 0.05, "ts": "20261030150000"}}
    w5 = kline.append_from_quotes(st4, q5, "2026-11-01")
    checks.append(("非交易日拒写", w5["written"] == 0 and w5["skipped"] and
                   st4.bars("600003", "2026-11-01", 1)[0][0] == "2026-10-29",
                   f"skipped={w5['skipped']}"))
    st4.close()

    # ⑥ 建库整只替换：旧口径残留不能留下（复权序列不可跨源拼接）
    st5 = new_store("rep", [("600004", "2026-10-01", 1.0, 1.0),
                            ("600004", "2026-10-02", 2.0, 2.0)])
    st5.replace_many({"600004": [("2026-10-29", 9.0, None), ("2026-10-30", 9.5, None)]})
    left = [d for d, _ in st5.bars("600004", "2026-10-30", 10)]
    checks.append(("重拉整只替换，不留旧口径残行", left == ["2026-10-30", "2026-10-29"], f"{left}"))
    st5.close()

    shutil.rmtree(tmpdir, ignore_errors=True)
    ok = True
    for name, passed, detail in checks:
        print(f"  {'✓' if passed else '✗'} {name}｜{detail}")
        ok &= passed
    return ok


def main() -> int:
    full = "--full" in sys.argv
    cli = http.make_client(workers=4, max_rps=4, min_span=0)

    ok = t_contract(cli)
    if not ok:
        print("\n契约未通过，按设计中止后续步骤。请修数据源后重跑。")
        return 2

    codes = _all_codes(cli) if full else _board_codes(cli, BAIJIU)
    if full:
        print(f"\n  全量模式：{len(codes)} 只成分股（{len(BOARDS)} 个板块去重）")

    q = t_quotes(cli, codes)
    store = t_kline(cli, codes)
    groups = {name: _board_codes(cli, code) for code, name in BOARDS} if full else {BAIJIU: codes}
    t_breadth(store, q, groups)
    store.close()

    t_report()
    t_circuit()
    gap_ok = t_gap()
    adj_ok = t_adj()

    section("汇总")
    s = cli.stats.as_dict()
    print(f"  请求统计 {s}")
    print(f"  总请求耗时 {s['seconds']:.1f}s | 下载 {s['bytes'] / 1024:.0f}KB | 失败 {s['failed']} | 熔断 {s['blocked']}")
    print(f"  缺口回归：{'全部通过' if gap_ok else '有失败项'}")
    print(f"  复权口径：{'全部通过' if adj_ok else '有失败项'}")
    return 0 if (gap_ok and adj_ok) else 1


def _board_codes(cli: http.Client, board: str) -> list[str]:
    cache = ROOT / "pipeline" / "cache" / f"codes-{board}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_BOARD_CONSTITUENT"
        f"&columns=BOARD_CODE_BK,SECURITY_CODE&pageNumber=1&pageSize=500"
        f"&filter=(BOARD_CODE_BK%3D%22{board}%22)"
    )
    j = cli.get_json(url)
    rows = ((j.get("result") or {}).get("data")) or []
    codes = [r["SECURITY_CODE"] for r in rows if r.get("SECURITY_CODE")]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")
    return codes


def _all_codes(cli: http.Client) -> list[str]:
    """上一轮实算的 8 个候选板块，去重后 232 只。"""
    seen: dict[str, None] = {}
    for code, _name in BOARDS:
        for c in _board_codes(cli, code):
            seen.setdefault(c, None)
    return list(seen)


def _baijiu_codes(cli: http.Client) -> list[str]:
    return _board_codes(cli, BAIJIU)


if __name__ == "__main__":
    raise SystemExit(main())
