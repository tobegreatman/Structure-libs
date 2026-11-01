"""成分股日线本地库 + 增量更新 + P2 广度计算。

核心思路：P2 广度只需要两个量——「当日上涨占比」和「当日收盘是否高于前 20 个交易日
最高收盘」。前者批量报价直接给，后者需要 21 天历史。

所以：
- 首次建库：逐只拉 50 日 K 线，并发 8~12（一次性成本）
- 之后每日：批量报价 4 次请求（约 1 秒）把当日收盘追加进库，**不再拉 K 线**

历史一旦建好，K 线请求就从每日 232 次降为 0 次。

## 增量库的四个坑

1. **缺口不会被回补**（2026-11-01 修）。原判据是 `count(code, as_of) < need`，
   而 count 只增不减——跳过 3 天再跑，库里就永久缺这 3 天，`bars()` 取「最近 21 条」
   于是 20 日窗口被悄悄向后拉伸成「跨 N 个自然周」。实测 5 日缺口：汽车零部件
   新高 0.73%→7.27%（10 倍），银行Ⅱ 42.86%→57.14%，而白酒Ⅱ 反而 42.11%→31.58%
   ——**方向都不一致，无法事后修正**。现在改为拿交易日历逐日比对，缺一天就整只重拉。
2. **日期不能直接写 as_of**（2026-11-01 修）。原 `append_from_quotes` 用 `as_of`
   当 K 线日期，非交易日跑分析会把周五收盘写成周日的假 K 线，此时 prev_high 恰好
   等于同一价格，`close > prev_high` 恒假——实测白酒Ⅱ 新高 42.11%→**0.00%**。
   现在取报价自带的交易日，与 as_of 不符就拒写；盘中跑（时间戳 < 15:00）打
   provisional 标记，下次同一日期整只重拉修正。
3. **上涨占比那一半是安全的**。`prev_close` 来自腾讯报价，永远是真实前一交易日收盘，
   与本地库无关。实测各缺口下 up_ratio 逐位不变——污染只发生在 new_high_ratio 上。
4. **价格基准混用**（2026-11-01 修）。原库混了三种口径：腾讯 `qfq` 前复权、新浪
   不复权、每日报价的原始收盘。每次除权，qfq 的锚点就整体平移一次（实测工行
   200 日窗口内 qfq/raw 从 0.9592 漂到 0.9596），拼接处出现假跳空，20 日高点被高估。
   现在库内只存**连续复权序列**（`c`）+ 原始收盘（`r`），见下节。

## 复权口径：为什么不需要因子表

库内 `c` 是**连续复权序列**，只保证同一只股票内部可比（P2 要的就是这个），
`r` 是最近一次写入时的原始收盘，仅供人工核对，建库时为 NULL。

关键观察：**除权那天的交易所昨收已经调整过**。设库内最后一根为 d0（收盘 c0），
今天报价的 `prev_close` 是 PC（已是除权后口径），则

    c(t) = P × c0 / PC

无除权时 PC 就是 d0 的实际收盘，退化为等比平推；有除权时 PC = 实际收盘/s，
自动补上 s 倍。所以**不需要复权因子表、不需要除权日历**，滚动一行公式就够。

代价是「重拉必须整只清空重写」：腾讯 qfq 的锚点是拉取当日，新浪兜底则是不复权，
两个源的序列只差一个等比因子——若不清理就覆盖，同一只股票会同时存在两种基准。
`Store.replace_many()` 保证了这一点，也就顺带解决了跨源混用。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import quotes as q
from .http import Client, EndpointBlocked

# r（原始收盘）是后加的列，旧库靠 ALTER 迁移，见 Store.__init__。
DDL = """
CREATE TABLE IF NOT EXISTS kline (
  code TEXT NOT NULL,
  d    TEXT NOT NULL,
  c    REAL NOT NULL,
  r    REAL,
  PRIMARY KEY (code, d)
);
CREATE INDEX IF NOT EXISTS idx_kline_d ON kline(d);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

SCHEMA = "2"


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(DDL)
        self._migrate()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

    def _migrate(self) -> None:
        """给 v1 的库补 r 列。旧 c 是混合基准，只能当原始价的近似值。"""
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(kline)")}
        if "r" not in cols:
            self._db.execute("ALTER TABLE kline ADD COLUMN r REAL")
            self._db.execute("UPDATE kline SET r=c WHERE r IS NULL")
        self._db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema',?)", (SCHEMA,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def bars(self, code: str, as_of: str, n: int = 25) -> list[tuple[str, float]]:
        """最近 n 根（降序），价格为连续复权口径 c，可直接做跨日比较。"""
        with self._lock:
            cur = self._db.execute(
                "SELECT d, c FROM kline WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, as_of, n),
            )
            return cur.fetchall()

    def count(self, code: str, as_of: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) FROM kline WHERE code=? AND d<=?", (code, as_of)
            )
            return int(cur.fetchone()[0])

    def dates(self, code: str, as_of: str, n: int) -> list[str]:
        with self._lock:
            cur = self._db.execute(
                "SELECT d FROM kline WHERE code=? AND d<=? ORDER BY d DESC LIMIT ?",
                (code, as_of, n),
            )
            return [r[0] for r in cur.fetchall()]

    def window_dates(self, as_of: str, n: int) -> dict[str, set[str]]:
        """一次扫描拿到每只股票最近 n 个日期，避免 863 次单查。"""
        out: dict[str, set[str]] = {}
        with self._lock:
            cur = self._db.execute(
                "SELECT code, d FROM kline WHERE d<=? ORDER BY code ASC, d DESC", (as_of,)
            )
            for code, d in cur:
                s = out.get(code)
                if s is None:
                    s = out[code] = set()
                if len(s) < n:
                    s.add(d)
        return out

    def last_bars(self, codes: list[str] | None = None) -> dict[str, tuple[str, float, float | None]]:
        """每只股票最后一根 (d, c, r)。append 要拿它当复权锚点，一次查完。"""
        sql = ("SELECT k.code, k.d, k.c, k.r FROM kline k JOIN "
               "(SELECT code, MAX(d) AS md FROM kline GROUP BY code) m "
               "ON k.code=m.code AND k.d=m.md")
        want = set(codes) if codes else None
        out: dict[str, tuple[str, float, float | None]] = {}
        with self._lock:
            for code, d, c, r in self._db.execute(sql):
                if want is None or code in want:
                    out[code] = (d, c, r)
        return out

    def global_dates(self, n: int) -> list[str]:
        """库内出现过的最近 n 个日期（降序）。交易日历的兜底来源。"""
        with self._lock:
            cur = self._db.execute(
                "SELECT d FROM kline GROUP BY d ORDER BY d DESC LIMIT ?", (n,)
            )
            return [r[0] for r in cur.fetchall()]

    def put_many(self, rows: list[tuple[str, str, float, float | None]]) -> int:
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO kline(code,d,c,r) VALUES(?,?,?,?)", rows
            )
            self._db.commit()
        return len(rows)

    def replace_many(self, by_code: dict[str, list[tuple[str, float, float | None]]]) -> int:
        """整只替换：先删光旧行再写。

        复权序列不能跨源拼接（qfq 与不复权只差一个等比因子，混着放就会出现假断层），
        所以每次重拉都整只覆盖。拉取失败的不在 by_code 里，旧数据因此得以保留。
        """
        if not by_code:
            return 0
        n = 0
        with self._lock:
            for code, bars in by_code.items():
                if not bars:
                    continue
                self._db.execute("DELETE FROM kline WHERE code=?", (code,))
                self._db.executemany(
                    "INSERT INTO kline(code,d,c,r) VALUES(?,?,?,?)",
                    [(code, d, c, r) for d, c, r in bars],
                )
                n += len(bars)
            self._db.commit()
        return n

    def wipe(self) -> int:
        """清空行情（换复权口径重建时用），保留 meta。"""
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
            self._db.execute("DELETE FROM kline")
            self._db.commit()
        return int(n)

    def last_date(self, code: str) -> str | None:
        with self._lock:
            cur = self._db.execute(
                "SELECT MAX(d) FROM kline WHERE code=?", (code,)
            )
            r = cur.fetchone()
            return r[0] if r and r[0] else None

    def get_meta(self, k: str) -> str | None:
        with self._lock:
            cur = self._db.execute("SELECT v FROM meta WHERE k=?", (k,))
            r = cur.fetchone()
            return r[0] if r else None

    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
            self._db.commit()

    def del_meta(self, k: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM meta WHERE k=?", (k,))
            self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
            codes = self._db.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0]
            d0, d1 = self._db.execute("SELECT MIN(d), MAX(d) FROM kline").fetchone()
        return {"rows": n, "codes": codes, "from": d0, "to": d1}


# 前复权：param 末位写 qfq 时返回 data[sym]["qfqday"]，序列在除权处天然连续，
# 可直接当库内的 c。锚点是「拉取当日」，但重拉会整只覆盖，不会产生拼接断层。
TENCENT_QFQ = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={sym},day,,,{n},qfq"
)
# 腾讯 web.ifzq 的 WAF 会返回 501 + waf.tencent.com 风控页（2026-10-30 实测，
# 232 只 @10rps 即触发）。新浪 money.finance.sina 是结构完全不同的兜底源，
# 返回 [{day, open, high, low, close, volume}]，不复权——所以重拉必须整只替换。
SINA = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}"
)


def _bars_tencent(client: Client, sym: str, days: int) -> list[tuple[str, float]] | None:
    j = client.get_json(TENCENT_QFQ.format(sym=sym, n=days), retries=1)
    data = (j or {}).get("data") or {}
    node = data.get(sym) or {}
    bars = node.get("qfqday") or node.get("day") or []
    out = []
    for b in bars:
        try:
            out.append((b[0], float(b[2])))
        except (IndexError, TypeError, ValueError):
            continue
    return out or None


def _bars_sina(client: Client, code: str, days: int) -> list[tuple[str, float]] | None:
    sym = q.to_symbol(code)
    j = client.get_json(SINA.format(sym=sym, n=days), retries=1)
    if not isinstance(j, list):
        return None
    out = []
    for b in j:
        try:
            out.append((str(b["day"])[:10], float(b["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def _fetch_kline(client: Client, code: str, days: int) -> tuple[list[tuple[str, float]], str] | None:
    """返回 ([(日期, 收盘)], 源名)。收盘是单一基准的连续序列。

    EndpointBlocked 必须在这里吞掉：熔断按 host 生效，腾讯挂了不代表整轮取数要中止，
    否则一个可降级的源会拖死整个流程。
    """
    try:
        got = _bars_tencent(client, q.to_symbol(code), days)
        if got and len(got) >= 2:
            return got, "tencent-qfq"
    except EndpointBlocked:
        pass
    except Exception:  # noqa: BLE001
        pass
    try:
        got = _bars_sina(client, code, days)
        if got and len(got) >= 2:
            return got, "sina-raw"
    except EndpointBlocked:
        raise
    except Exception:  # noqa: BLE001
        pass
    return None


def _dead_list(store: Store, as_of: str) -> set[str]:
    raw = store.get_meta(f"dead:{as_of}")
    return set(json.loads(raw)) if raw else set()


def _calendar(store: Store, as_of: str, need: int, calendar: list[str] | None) -> list[str]:
    """截至 as_of 的交易日序列（升序）。

    优先用调用方传入的日历——`panel.build()` 的 by_date 就是东财口径的真实交易日，
    比分位数推算可靠。没传就退化成库内出现过的日期，冷启动时为空。
    """
    base = calendar if calendar is not None else store.global_dates(max(need * 3, 60))
    return sorted(d for d in base if d <= as_of)


def ensure_history(
    client: Client,
    store: Store,
    codes: list[str],
    as_of: str,
    *,
    need: int = 45,
    calendar: list[str] | None = None,
    verbose: bool = True,
) -> dict:
    """补齐缺失/不连续的历史。库里已连续、以及当日已判定拉不到的，一个请求都不发。

    判据从「总根数够不够」换成「日历上该有的日子有没有」：旧判据永远看不出缺口，
    因为 count() 只增不减，跳过的交易日既不入库也不触发重拉。

    `as_of` 当天那根由 `append_from_quotes` 从报价写入，这里**允许它缺失**——
    否则每天都要因为「库里还没有今天」而把 485 只全量重拉，增量设计直接作废。
    """
    cal = _calendar(store, as_of, need, calendar)
    need_eff = min(need, len(cal)) if cal else need
    expected = set(cal[-need_eff:]) if cal else set()
    # 当天由报价负责，不算缺；非交易日时 as_of 本来就不在日历里。
    expected_need = expected - {as_of}
    have = store.window_dates(as_of, need_eff)
    dead = _dead_list(store, as_of)
    # 盘中快照标记是单值（最近一次盘中写入的日期），不是按日建键：
    # 按日建键时，第二天跑就换了 key，前一天那根错价永远没人来修。
    prov = store.get_meta("provisional")
    prov_hit = bool(prov) and prov <= as_of and (not cal or prov >= cal[-need_eff])

    missing: list[str] = []
    skipped = 0
    for c in codes:
        got = have.get(c) or set()
        if c in dead:
            skipped += 1
            continue
        # 盘中快照（provisional）必须整只重拉：那根 K 线是 14:30 的价格，不是收盘价。
        if prov_hit or len(got) < len(expected_need) or (expected_need - got):
            missing.append(c)

    holes = sum(1 for c in missing if have.get(c))
    if verbose:
        note = f"（另有 {skipped} 只当日已判定无数据，跳过）" if skipped else ""
        why = f"，其中 {holes} 只是库里有数据但窗口有缺口/不足" if holes else ""
        warn = f"，库内 {prov} 那根是盘中快照，需整只修正" if prov_hit else ""
        print(f"  K 线历史：{len(codes)} 只中 {len(missing)} 只需要补{why}{warn}{note}")
    if not missing:
        return {"requested": 0, "filled": 0, "failed": 0, "mode": "cache"}

    depth = max(need_eff + 5, 30)
    blocked: list[str] = []

    def one(code: str):
        # 不能在这里抛 EndpointBlocked：一抛 client.map 就 cancel 掉全部 futures，
        # 已经拉好的几百只全丢。先记下来，等落库之后再抛。
        try:
            return _fetch_kline(client, code, depth)
        except EndpointBlocked as e:
            blocked.append(str(e))
            return None
        except Exception:  # noqa: BLE001
            return None

    results = client.map(one, missing)
    by_code: dict[str, list[tuple[str, float, float | None]]] = {}
    failed: list[str] = []
    sources: dict[str, int] = {}
    for code, got in zip(missing, results):
        if not got:
            failed.append(code)
            continue
        bars, src = got
        # 整只清空重写：复权序列不能跨源拼接，先删后写才是唯一安全的覆盖方式。
        by_code[code] = [(d, c, None) for d, c in bars]
        sources[src] = sources.get(src, 0) + 1
    rows_n = store.replace_many(by_code)

    if blocked:
        ok = len(missing) - len(failed)
        raise EndpointBlocked(
            f"K 线建库中断（{blocked[0]}）。本次已落入 {rows_n} 行 / {ok} 只，"
            f"剩余下次从断点继续。"
        )

    short = [c for c in missing
             if c not in failed and store.count(c, as_of) < len(expected_need)]
    if short:
        failed.extend(short)

    if failed:
        store.set_meta(f"dead:{as_of}", json.dumps(sorted(dead | set(failed))))
    if prov_hit:
        # 重拉完成，那根盘中价已经被真实收盘价覆盖。
        store.del_meta("provisional")
    if verbose:
        ok_n = len(missing) - len(failed)
        src = "、".join(f"{k} {v}" for k, v in sorted(sources.items()))
        print(f"  补齐 {ok_n}/{len(missing)} 只，整只重写 {rows_n} 行（{src or '无'}）")
        if failed:
            tail = [c for c in failed if c in short]
            note = f"（其中 {len(tail)} 只补后仍不足 {len(expected_need)} 根，多为次新/停牌）" if tail else ""
            print(f"  当日标记无数据：{len(failed)} 只 {failed[:8]}{note}")
    return {"requested": len(missing), "filled": len(missing) - len(failed),
            "failed": len(failed), "failed_codes": failed, "mode": "kline",
            "calendar": len(cal), "need": need_eff, "rows": rows_n,
            "sources": sources}


def append_from_quotes(store: Store, quotes: dict[str, dict], as_of: str) -> dict:
    """把已拿到的当日报价追加进库，0 请求。

    日期取自报价自带的交易日，**不是 as_of**：非交易日跑分析（如 2026-11-01 周日）
    会把周五收盘写成周日的假 K 线，那天 prev_high 恰好等于同一价格，新高占比归零。

    价格用 `h = P × c0 / PC` 换算到库内复权口径（c0 为最后一根，PC 为报价昨收）。
    除权日交易所给的 PC 已经调整过，所以这一个式子同时处理了「平推」和「除权」，
    不需要复权因子表。
    """
    if not quotes:
        return {"written": 0, "trade_date": None, "skipped": "无报价"}
    td = q.trade_date(quotes)
    if td and td != as_of:
        return {"written": 0, "trade_date": td,
                "skipped": f"报价交易日 {td} 与 as_of {as_of} 不一致，本轮不写库"}

    ts = next((v.get("ts") for v in quotes.values()
               if len(v.get("ts") or "") >= 12), "") or ""
    intraday = bool(ts) and ts[8:12] < "1500"
    day = td or as_of

    codes = [c for c, v in quotes.items() if (v.get("price") or 0) > 0]
    last = store.last_bars(codes)
    rows: list[tuple[str, str, float, float]] = []
    skip: list[str] = []
    exdiv: list[tuple[str, float]] = []
    for code in codes:
        qd = quotes[code]
        p, pc = qd["price"], qd.get("prev_close")
        lb = last.get(code)
        if not lb or not pc or pc <= 0:
            # 无历史（新股首日）或昨收缺失：只能按原始价落一根，因子取 1。
            rows.append((code, day, p, p))
            continue
        d0, c0, r0 = lb
        if d0 >= day:
            skip.append(code)          # 已经写过这一天，幂等跳过，避免重复乘因子
            continue
        h = p * c0 / pc
        # 换算后的日涨跌幅超出交易所限制（北交所 30%）→ 数据错位，宁可空着等重拉。
        if abs(h / c0 - 1) > 0.31:
            skip.append(code)
            continue
        if r0 and abs(r0 / pc - 1) > 1e-4:
            exdiv.append((code, round(r0 / pc, 6)))
        rows.append((code, day, h, p))

    n = store.put_many(rows)
    if intraday:
        store.set_meta("provisional", day)
    elif store.get_meta("provisional"):
        store.del_meta("provisional")
    return {"written": n, "trade_date": day, "provisional": intraday, "ts": ts,
            "skipped": None if rows else "全部跳过（已有当日或数据异常）",
            "skipped_codes": skip[:8], "exdiv": exdiv[:8]}


def breadth(
    store: Store,
    codes: list[str],
    as_of: str,
    quotes: dict[str, dict],
    *,
    window: int = 20,
    calendar: list[str] | None = None,
) -> dict:
    """P2 广度：上涨占比 + 创 20 日新高占比。

    新高口径（v1.6）：当日收盘 > 前 window 个交易日（不含当日）的最高收盘。

    当天收盘直接取库内 `bars[0]`，与历史同口径：`append_from_quotes` 已把它换算到
    复权序列上，这里再拿报价的原始价去比历史价就会在除权股上比错。

    涨跌判定必须用 **现价 vs 昨收** 的原始价格比较（两者同为不复权口径，可比），
    不能用报价里的 chg_pct：批量报价的涨跌幅只保留两位小数，±0.005% 以内的波动
    会被截断成 0.000%，把「微涨」误判成「平」。实测吉比特 603444 当日 +0.003%，
    报价显示 0.00%，用 chg_pct 判定会让游戏Ⅱ 的上涨占比从 4.2% 掉到 0.0%。

    窗口校验：给了 calendar 就逐日比对，但**不能用「某只股票缺某天」当判据**——
    停牌股天然缺日（新华传媒 600825 停 3 天、路畅科技 002813 停 10 天），那是个股属性。
    真正的缓存缺口是「一整天对全板块所有股票同时消失」。所以按日期聚合：
    某天被过半成分股同时缺 → degraded，调用方必须 fail-closed；
    只被一两只缺 → 停牌，照常计入（与旧口径逐位一致）。
    历史本来就不足 21 根的（次新）直接跳过。
    """
    need_dates: set[str] | None = None
    if calendar:
        cal = sorted(d for d in calendar if d <= as_of)
        if len(cal) >= window + 1:
            need_dates = set(cal[-(window + 1):])

    valid = up = high = 0
    detail: list[dict] = []
    stale: list[str] = []
    miss_by_date: dict[str, int] = {}
    for code in codes:
        qd = quotes.get(code)
        if not qd or not qd.get("price"):
            continue
        bars = store.bars(code, as_of, window + 1)
        if len(bars) < window + 1:
            continue
        ds = [d for d, _ in bars]
        if ds[0] != as_of:
            # 最后一根不是当天：停牌（当日无成交）或缓存断更。都不该当成「今天」判新高。
            stale.append(code)
            continue
        if need_dates is not None:
            for d in need_dates - set(ds):
                miss_by_date[d] = miss_by_date.get(d, 0) + 1
        close = bars[0][1]            # 复权口径，与历史同基准
        prev = qd.get("prev_close")
        prev_high = max(c for d, c in bars[1:])
        valid += 1
        if prev:
            if qd["price"] > prev:     # 原始价比较，避开两位小数截断
                up += 1
        elif (qd.get("chg_pct") or 0) > 0:
            up += 1
        is_high = close > prev_high
        if is_high:
            high += 1
        detail.append({"code": code, "close": round(close, 4), "raw": qd["price"],
                       "prev_close": prev, "prev_high": round(prev_high, 4),
                       "new_high": is_high})

    # 判退化的两种情形：①某天对过半成分股同时消失（缓存漏了一段，不是停牌）；
    # ②过半股票最后一根不是当天（整段缓存停更）。都按「不可用」处理，不给假结论。
    gate_n = max(2, int(valid * 0.5))
    hole_dates = sorted(d for d, n in miss_by_date.items() if n >= gate_n)
    degraded = bool(hole_dates) or (bool(stale) and len(stale) >= max(2, len(codes) * 0.5))
    if valid == 0:
        return {"valid": 0, "up_ratio": None, "new_high_ratio": None,
                "degraded": bool(codes), "hole_dates": hole_dates[:6],
                "stale": stale[:8], "hole_count": len(hole_dates)}
    return {
        "valid": valid,
        "up_ratio": round(up / valid, 4),
        "new_high_ratio": round(high / valid, 4),
        "up_count": up,
        "new_high_count": high,
        "degraded": degraded,
        "hole_dates": hole_dates[:6],
        "stale": stale[:8],
        "hole_count": len(hole_dates) + (len(stale) if degraded else 0),
        "detail": detail,
    }
