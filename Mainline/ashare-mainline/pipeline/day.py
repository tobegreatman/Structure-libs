"""交易日自动推导：让流水线不再依赖手填 --as-of。

为什么需要它：`--as-of 2026-11-03` 这种写法明天就会错。9 个模块曾各自硬编码 default
（10-30 与 11-02 混着），即使手填对主参数，下游组装器仍会指向旧日期。

判定不依赖节假日表，靠**行情自身**回答「哪天有过交易」：

1. 取上证指数日 K 最近若干根，得到真实交易日序列 `days`；
2. `days[-1] == 今天` 且当前时间已过收盘 → 今天就是最近已收盘交易日；
3. `days[-1] == 今天` 但还没收盘（盘中）→ 取 `days[-2]`，因为当天 K 线是未完成的；
4. `days[-1] < 今天` → 说明今天是周末/节假日，`days[-1]` 即最近交易日。

这样国庆、春节、临时休市都不用维护日历；台钟漂移或时区不同也只影响 1 天。

用法：
    from pipeline import day
    as_of = day.resolve(None)              # auto：最近已收盘交易日
    as_of = day.resolve("2026-11-03")      # 显式指定，且校验它确实是交易日
    prev  = day.prev_n(as_of, 5)           # 5 个交易日前（涨停池连板基准用）
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
         "?param=sh000001,day,,,{n},qfq")

# 收盘后多久算「已收盘、日线可信」：15:00 收盘，15:05 给交易所与数据源留缓冲
CLOSE_TIME = dt.time(15, 5)
# 涨停池、板块资金流等收盘后加工的字段，通常 17:30 后才全部披露
DATA_READY = dt.time(17, 30)

_cache: dict[str, list[str]] = {}

_HERE = pathlib.Path(__file__).resolve().parent
DAYS_CACHE = _HERE / "cache" / "trading-days.json"
KLINE_DB = _HERE / "cache" / "kline.sqlite"
TTL = 600            # 交易日序列的磁盘缓存有效期（秒）


def _fetch(n: int) -> list[str]:
    req = urllib.request.Request(KLINE.format(n=n),
                                 headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    node = j["data"]["sh000001"]
    bars = node.get("qfqday") or node.get("day") or []
    days = sorted(b[0] for b in bars if b and b[0])
    if not days:
        raise RuntimeError("取不到上证指数日 K，无法推导交易日")
    return days


def _from_kline_db() -> list[str]:
    """最后一道降级：本地 K 线库里出现过的日期就是真实交易日。"""
    if not KLINE_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{KLINE_DB}?mode=ro", uri=True)
        rows = con.execute("select distinct d from kline order by d").fetchall()
        con.close()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def recent_days(n: int = 80) -> list[str]:
    """上证指数最近 n 个**真实**交易日（升序）。

    三层取数：内存 → 磁盘缓存（10 分钟）→ 行情接口；接口被 WAF 拦时退到本地 K 线库。
    必须缓存到磁盘：一次 daily 会起 9 个子进程，逐个请求等于把日 K 接口打到限速（实测 501）。
    """
    if _cache.get("days"):
        return _cache["days"]

    disk = None
    if DAYS_CACHE.exists():
        try:
            disk = json.loads(DAYS_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            disk = None
    if disk and time.time() - float(disk.get("fetched_at") or 0) < TTL and disk.get("days"):
        _cache["days"] = disk["days"]
        return disk["days"]

    try:
        days = _fetch(min(max(n, 5), 120))
        DAYS_CACHE.parent.mkdir(exist_ok=True)
        DAYS_CACHE.write_text(json.dumps({"fetched_at": time.time(), "days": days}),
                              encoding="utf-8")
    except Exception as e:                                # noqa: BLE001
        days = (disk or {}).get("days") or _from_kline_db()
        if not days:
            raise RuntimeError(f"交易日序列不可得（{type(e).__name__}: {e}）且无本地缓存") from e
        print(f"  [warn] 行情日 K 不可用（{type(e).__name__}），改用本地序列，末位 {days[-1]}")
        try:                                              # 降级结果也缓存，避免每次白打一次失败请求
            DAYS_CACHE.parent.mkdir(exist_ok=True)
            DAYS_CACHE.write_text(json.dumps({"fetched_at": time.time(), "days": days,
                                              "source": "kline"}), encoding="utf-8")
        except OSError:
            pass
    _cache["days"] = days
    return days


def latest_closed(now: dt.datetime | None = None) -> str:
    """最近一个**已收盘**的交易日（YYYY-MM-DD）。"""
    now = now or dt.datetime.now()
    days = recent_days()
    today = now.strftime("%Y-%m-%d")
    # 先按当前时间截断：模拟历史时点（或台钟偏差）下，晚于今天的 K 线必须忽略，
    # 否则「周日跑」会拿到下周一那根不存在的 K 线。
    upto = [d for d in days if d <= today]
    if not upto:
        raise RuntimeError(f"{today} 早于可得数据起点 {days[0]}，无法确定交易日")

    if upto[-1] == today:
        if now.time() >= CLOSE_TIME:
            return today                                  # 今天已收盘，日线可信
        if len(upto) >= 2:
            return upto[-2]                               # 盘中：当天 K 线尚未完成
        raise RuntimeError(f"今天 {today} 是首个有数据的日子且未收盘，无法确定上一交易日")
    return upto[-1]                                       # 周末/节假日：最后一根即最近交易日


def is_trading_day(d: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    return d in recent_days() or d == now.strftime("%Y-%m-%d")


def prev_n(as_of: str, n: int) -> str:
    """as_of 之前第 n 个交易日；as_of 不在序列里时按「最近的不晚于 as_of」处理。"""
    days = [d for d in recent_days() if d <= as_of]
    if not days:
        raise RuntimeError(f"{as_of} 早于本地可得的最早交易日 {recent_days()[0]}")
    if len(days) <= n:
        raise RuntimeError(f"{as_of} 之前只有 {len(days) - 1} 个交易日，不足 {n} 个")
    return days[-1 - n]


def resolve(spec: str | None, now: dt.datetime | None = None) -> str:
    """把 --as-of 的取值解析成确定日期。

    None / "auto" / ""  → 自动推导；"today" → 今天（若今天没交易则报错）
    显式日期            → 原样返回，但若不是交易日会给出提示（不阻断，便于补历史）
    """
    now = now or dt.datetime.now()
    if spec in (None, "", "auto"):
        return latest_closed(now)
    s = str(spec).strip()
    if s.lower() == "today":
        t = now.strftime("%Y-%m-%d")
        if not is_trading_day(t, now):
            raise RuntimeError(f"{t} 不是交易日（行情无该日 K 线）")
        return t
    # 显式日期只校验格式，**不联网**：daily 会把同一日期传给 9 个子进程，
    # 每个子进程都验一次交易日等于多打 9 次行情接口，实测直接把日 K 接口打到 501。
    try:
        dt.datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise RuntimeError(f"--as-of 需为 YYYY-MM-DD，收到 {s!r}") from e
    return s


def warn_if_early(now: dt.datetime | None = None) -> str | None:
    """盘中/收盘后数据未齐时给出提示，返回提示文本（无提示返回 None）。"""
    now = now or dt.datetime.now()
    if now.time() < DATA_READY:
        return (f"当前 {now:%H:%M} 早于 {DATA_READY:%H:%M}：涨停池、封板率、板块资金流可能尚未披露，"
                f"建议收盘后重跑")
    return None


def add_as_of(ap, required: bool = False) -> None:
    """给 argparse 挂统一口径的 --as-of（缺省 auto）。"""
    ap.add_argument("--as-of", default=None, required=required,
                    help="基准交易日 YYYY-MM-DD；缺省或 auto = 最近一个已收盘交易日")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="打印自动推导的交易日")
    p.add_argument("--now", default="", help="模拟当前时间，如 2026-11-02T14:00（测试用）")
    a = p.parse_args()
    fake = dt.datetime.fromisoformat(a.now) if a.now else None
    d = latest_closed(fake)
    print(f"  最近已收盘交易日：{d}")
    print(f"  最近 8 个交易日：{recent_days()[-8:]}")
    print(f"  5 个交易日前：{prev_n(d, 5)}")
    w = warn_if_early(fake)
    if w:
        print(f"  [warn] {w}")
