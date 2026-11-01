"""统一 HTTP 层：全局平滑限速 + host 限速 + 速率预算 + 自适应降速 + 分级重试 + 熔断。

## 为什么还要一层「全局」限速

旧实现只有「每个 host 一条 gap」，全局并发 12、没有任何总速率上限。2026-10-30 那轮
156 次请求 39.9 秒跑完，看着不慢，但节奏完全由 host 队列决定：面板 129 次集中在
32 秒里冲东财，两个 host 同时开工时瞬时速率是两者之和。对上游而言这就是一次突发——
WAF 501 和连接层 RST 都由此而来。所以现在有四层闸门：

1. **全局槽位**：所有 host 共用一个 `next_slot`，整轮按固定间隔铺开，任意窗口都不超
   `max_rps`。这是「摊开」的唯一保障。
2. **host 槽位**：单 host 另有更严的上限（腾讯日 K 2.5 rps、push2his 2 rps），
   实际等待点取 `max(全局槽, host 槽)`。
3. **速率预算 `plan()`**：给定计划请求数 N 与 `min_span` S，gap 取
   `max(1/max_rps, (deadline−now)/(N_rem−1))`，让**最后一次请求正好落在 deadline 上**。
   超前就自动放慢，落后才允许追到 rps 上限。于是「156 次摊到 40 秒以上」成为可断言
   的性质，而不是靠调参碰运气。
4. **自适应降速（AIMD）**：上游开始抱怨（429/503/5xx/WAF 页）时把**该 host** 的 rps
   砍半（下限 0.5），此后每 20 次成功回升 15%，不超过初始值。只惩罚出问题的 host：
   腾讯 K 线被拦时，东财的节奏不该跟着变慢。

`Retry-After` 优先于指数退避——服务端说等多久就等多久，别自己猜。

## 可观测性

每次请求都写一行 journal（`cache/http-<as_of>.jsonl`）：偏移时刻、host、HTTP 码、
耗时、字节数。`audit()` 由此算出**实测跨度、实测 rps、峰值并发、逐 host 分布**，
并按策略给出 `violations`。上一轮的「39.9 秒 / 156 次」只能靠人肉回忆，
现在可以逐条复核。

## 熔断仍按 host 隔离

连接层被 RST（HTTP 000）时零重试——重试对封禁无效，只会空转；连续 3 次即熔断
300 秒。腾讯挂了不代表整轮要中止，所以状态必须是 `dict[host]`。
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import requests

PIPELINE = Path(__file__).resolve().parent
POLICY_PATH = PIPELINE / "templates" / "fetch_policy.json"
CACHE_DIR = PIPELINE / "cache"

BLOCK_COOLDOWN = 300.0
BLOCK_STREAK = 3
CONNECT_PROBE_DELAY = 0.5
SLOW_FACTOR = 0.5          # 挨批时 rps 直接砍半
SLOW_FLOOR = 0.5           # 但不再低于 0.5 rps
RECOVER_EVERY = 20         # 每 20 次成功回升一次
RECOVER_FACTOR = 1.15

WAF_MARKS = ("waf.tencent.com", "waf.eastmoney", "captcha", "访问过于频繁", "验证码")

# host 上限的兜底值（fetch_policy.json 缺失或新增 host 时用）。
HOST_RPS = {
    "datacenter-web.eastmoney.com": 3.5,
    "qt.gtimg.cn": 3.0,
    "web.ifzq.gtimg.cn": 2.5,
    "push2his.eastmoney.com": 2.0,
    "push2ex.eastmoney.com": 2.0,
    "money.finance.sina.com.cn": 2.0,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class EndpointBlocked(RuntimeError):
    """端点被应用层封禁（连接被 RST）。重试无意义。"""


class ContractError(RuntimeError):
    """接口返回结构与预期不符。"""


@dataclass(frozen=True)
class Policy:
    """取数节奏。字段与 templates/fetch_policy.json 一一对应。"""

    workers: int = 4
    max_rps: float = 4.0
    min_span: float = 40.0
    jitter: tuple[float, float] = (0.02, 0.18)
    timeout: float = 20.0
    retries: int = 2
    adaptive: bool = True
    cache_ttl: float = 0.0
    max_workers_hard: int = 8
    host_rps: dict[str, float] = field(default_factory=lambda: dict(HOST_RPS))

    def as_dict(self) -> dict:
        return {
            "workers": self.workers, "max_rps": self.max_rps, "min_span_seconds": self.min_span,
            "jitter_seconds": list(self.jitter), "timeout_seconds": self.timeout,
            "retries": self.retries, "adaptive": self.adaptive,
            "cache_ttl_seconds": self.cache_ttl, "host_rps": dict(self.host_rps),
        }


def load_policy(path: str | Path | None = None) -> Policy:
    p = Path(path) if path else POLICY_PATH
    if not p.exists():
        return Policy()
    raw = json.loads(p.read_text(encoding="utf-8"))
    return Policy(
        workers=int(raw.get("workers", 4)),
        max_rps=float(raw.get("max_rps", 4.0)),
        min_span=float(raw.get("min_span_seconds", 40)),
        jitter=tuple(raw.get("jitter_seconds") or (0.02, 0.18)),
        timeout=float(raw.get("timeout_seconds", 20)),
        retries=int(raw.get("retries", 2)),
        adaptive=bool(raw.get("adaptive", True)),
        cache_ttl=float(raw.get("cache_ttl_seconds", 0)),
        max_workers_hard=int(raw.get("max_workers_hard", 8)),
        host_rps={**HOST_RPS, **(raw.get("host_rps") or {})},
    )


def _host_of(url: str) -> str:
    return url.split("/")[2] if "//" in url else url


class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ok = 0
        self.retry = 0
        self.failed = 0
        self.blocked = 0
        self.cache_hit = 0
        self.bytes = 0
        self.seconds = 0.0

    def add(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "retry": self.retry, "failed": self.failed,
            "blocked": self.blocked, "cache_hit": self.cache_hit, "bytes": self.bytes,
            "seconds": round(self.seconds, 2),
        }


class ResponseCache:
    """按 URL 缓存响应体（默认关闭，`cache_ttl>0` 才启用）。

    用途是**断点续跑**：整轮跑完 128 个板块要 129 次请求，中途崩在第 120 个就全丢，
    重跑又是一整轮。给响应体一个短 TTL（同一天内），重跑等于零请求。
    分析口径要求「全量真实取数」时把 ttl 设 0（默认），缓存不参与。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else CACHE_DIR / "httpcache.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS resp("
            "k TEXT PRIMARY KEY, ts REAL, host TEXT, bytes INT, body TEXT);"
        )
        self._db.commit()

    def get(self, key: str, ttl: float) -> str | None:
        if ttl <= 0:
            return None
        with self._lock:
            row = self._db.execute("SELECT ts, body FROM resp WHERE k=?", (key,)).fetchone()
        if not row:
            return None
        return row[1] if time.time() - row[0] <= ttl else None

    def put(self, key: str, host: str, body: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO resp(k,ts,host,bytes,body) VALUES(?,?,?,?,?)",
                (key, time.time(), host, len(body), body),
            )
            self._db.commit()

    def purge(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM resp").fetchone()[0]
            self._db.execute("DELETE FROM resp")
            self._db.commit()
        return int(n)

    def close(self) -> None:
        with self._lock:
            self._db.close()


class Client:
    def __init__(
        self,
        workers: int = 4,
        max_rps: float = 4.0,
        jitter: tuple[float, float] = (0.02, 0.18),
        timeout: float = 20.0,
        host_rps: dict[str, float] | None = None,
        *,
        policy: Policy | None = None,
        min_span: float | None = None,
        cache_ttl: float | None = None,
        adaptive: bool | None = None,
        journal: str | Path | None = None,
        cache_path: str | Path | None = None,
    ) -> None:
        base = policy or Policy()
        self.policy = replace(
            base,
            workers=workers,
            max_rps=max_rps,
            jitter=tuple(jitter),
            timeout=timeout,
            min_span=base.min_span if min_span is None else min_span,
            cache_ttl=base.cache_ttl if cache_ttl is None else cache_ttl,
            adaptive=base.adaptive if adaptive is None else adaptive,
            host_rps={**base.host_rps, **(host_rps or {})},
        )
        if self.policy.workers > self.policy.max_workers_hard:
            raise ValueError(
                f"并发 {self.policy.workers} 超过硬上限 {self.policy.max_workers_hard}："
                "对上游来说并发就是瞬时速率，堆 worker 只会把压力从一个源转到另一个源"
            )
        self.workers = self.policy.workers
        self._sem = threading.BoundedSemaphore(self.workers)
        self._gate = threading.Lock()
        self._t0 = time.monotonic()
        self._next_global = 0.0
        self._hosts: dict[str, float] = {}
        self._done = 0
        self._budget_total = 0
        self._deadline = 0.0
        self._gap_budget = 0.0
        self._host_rps = dict(self.policy.host_rps)
        self._host_base = dict(self.policy.host_rps)
        self._ok_streak: dict[str, int] = {}
        self._local = threading.local()
        self._state = threading.Lock()
        self._streak: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        # 观测量
        self._inflight = 0
        self._peak = 0
        self._last_end = 0.0
        self._host_stats: dict[str, dict] = {}
        self._slow_events: list[dict] = []
        self._journal_path = Path(journal) if journal else None
        self._journal: list[dict] = []
        self._jlock = threading.Lock()
        self._cache = (ResponseCache(cache_path) if self.policy.cache_ttl > 0 else None)
        self.stats = Stats()

    # ---------------- 节奏 ----------------

    def plan(self, total: int, *, min_span: float | None = None, note: str = "") -> float:
        """声明「本轮计划 total 次请求」，据此把 gap 拉到能铺满 min_span。

        返回实际采用的 gap（秒）。可以多次调用（面板拉完就知道了真实板块数），
        每一次都用**当前剩余时间 ÷ 剩余请求数**重算，所以越往后越准。
        """
        with self._gate:
            if min_span is not None:
                self.policy = replace(self.policy, min_span=min_span)
            if not self._deadline:
                self._deadline = self._t0 + self.policy.min_span
            # 允许**下调**：面板拉完就知道真实板块数了，必须按真实数重算，
            # 否则按偏大的估计收尾会提前跑完，40 秒下限就成了空话。
            self._budget_total = max(self._done + 2, int(total))
            remain = self._budget_total - self._done
            now = time.monotonic()
            floor_gap = 1.0 / self.policy.max_rps
            gap_budget = 0.0
            if remain >= 2 and self._deadline > now:
                # 最后一槽正好落在 deadline 上：n 个请求之间只有 n−1 个间隔
                gap_budget = (self._deadline - now) / (remain - 1)
            self._gap_budget = max(floor_gap, gap_budget)
            gap = self._gap_budget
        print(f"  · 节奏计划：{total} 次 × {gap:.3f}s ≈ {total * gap:.1f}s"
              f"（≥{self.policy.min_span:.0f}s 目标，{1 / gap:.2f} rps，并发 {self.workers}）"
              + (f" {note}" if note else ""))
        return gap

    def _gap(self) -> float:
        """当前应采用的基础间隔（不含 host 约束）。"""
        return max(1.0 / self.policy.max_rps, self._gap_budget)

    def _throttle(self, url: str) -> None:
        host = _host_of(url)
        with self._gate:
            now = time.monotonic()
            gap = self._gap()
            h_gap = 1.0 / self._host_rps.get(host, self.policy.max_rps)
            slot = max(now, self._next_global, self._hosts.get(host, 0.0))
            self._done += 1
            self._next_global = slot + gap
            self._hosts[host] = slot + h_gap
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if self.policy.jitter[1] > 0:
            time.sleep(random.uniform(*self.policy.jitter))

    def _slow_down(self, host: str, reason: str) -> None:
        if not self.policy.adaptive:
            return
        with self._gate:
            cur = self._host_rps.get(host, self.policy.max_rps)
            new = max(SLOW_FLOOR, cur * SLOW_FACTOR)
            if new < cur:
                self._host_rps[host] = new
                self._slow_events.append({"host": host, "from": round(cur, 2),
                                          "to": round(new, 2), "why": reason[:80]})
                self._ok_streak[host] = 0

    def _speed_up(self, host: str) -> None:
        if not self.policy.adaptive:
            return
        with self._gate:
            n = self._ok_streak.get(host, 0) + 1
            self._ok_streak[host] = n
            if n % RECOVER_EVERY:
                return
            base = self._host_base.get(host, self.policy.max_rps)
            cur = self._host_rps.get(host, base)
            self._host_rps[host] = min(base, cur * RECOVER_FACTOR)

    def current_host_rps(self) -> dict[str, float]:
        with self._gate:
            return {h: round(v, 2) for h, v in self._host_rps.items()}

    @property
    def scheduled(self) -> int:
        """已排期（含缓存命中）的请求数，用于中途重算节奏。"""
        with self._gate:
            return self._done

    def _check_circuit(self, url: str) -> None:
        host = _host_of(url)
        with self._state:
            until = self._blocked_until.get(host, 0.0)
            if until > time.monotonic():
                raise EndpointBlocked(
                    f"熔断中（连续 {BLOCK_STREAK} 次被拒，剩余 "
                    f"{until - time.monotonic():.0f}s）：{host}"
                )

    def _note_block(self, blocked: bool, host: str = "") -> None:
        with self._state:
            if blocked:
                n = self._streak.get(host, 0) + 1
                self._streak[host] = n
                if n >= BLOCK_STREAK:
                    self._blocked_until[host] = time.monotonic() + BLOCK_COOLDOWN
            else:
                self._streak[host] = 0

    def blocked_hosts(self) -> list[str]:
        now = time.monotonic()
        with self._state:
            return [h for h, t in self._blocked_until.items() if t > now]

    # ---------------- 观测 ----------------

    def _note(self, host: str, *, ms: float, nbytes: int, status: int, ok: bool,
              attempt: int) -> None:
        now = time.monotonic()
        with self._state:
            self._last_end = max(self._last_end, now)
            h = self._host_stats.setdefault(
                host, {"ok": 0, "failed": 0, "bytes": 0, "ms": 0.0, "n": 0})
            h["n"] += 1
            h["ms"] += ms
            if ok:
                h["ok"] += 1
                h["bytes"] += nbytes
            else:
                h["failed"] += 1
            rec = {"t": round(now - self._t0, 3), "host": host, "status": status,
                   "ms": round(ms * 1000, 1), "bytes": nbytes, "try": attempt}  # 毫秒
        if self._journal_path:
            with self._jlock:
                self._journal.append(rec)
                with self._journal_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def audit(self) -> dict:
        """实测节奏 + 策略违背清单。报告与审计端都读这个。"""
        span = round(self._last_end - self._t0, 2) if self._last_end else 0.0
        ok = self.stats.ok + self.stats.cache_hit
        rps = round(ok / span, 3) if span > 0 and ok else 0.0
        per_host = {}
        with self._state:
            for h, v in sorted(self._host_stats.items()):
                per_host[h] = {
                    "requests": v["n"], "ok": v["ok"], "failed": v["failed"],
                    "kb": round(v["bytes"] / 1024, 1),
                    "avg_ms": round(1000 * v["ms"] / v["n"], 0) if v["n"] else 0,
                    "rps_applied": self._host_rps.get(h),
                }
        violations = []
        # 只有声明过计划的 client 才受 min_span 约束：selftest / limitpool 这类
        # 三五个请求的脚本也调 plan() 的话，40 秒下限会变成纯粹的等待。
        if self._budget_total and span and span + 0.5 < self.policy.min_span:
            violations.append(
                f"整轮跨度 {span:.1f}s 未达目标 {self.policy.min_span:.0f}s"
                f"（{ok} 次请求，gap {self._gap_budget:.3f}s）")
        if rps > self.policy.max_rps * 1.02:
            violations.append(f"实测 {rps:.2f} rps 超过上限 {self.policy.max_rps}")
        if self._peak > self.workers:
            violations.append(f"峰值并发 {self._peak} 超过 {self.workers}")
        return {
            "policy": self.policy.as_dict(),
            "span_seconds": span,
            "achieved_rps": rps,
            "peak_inflight": self._peak,
            "gap_seconds": round(self._gap_budget, 4),
            "planned_total": self._budget_total,
            "requests": self.stats.as_dict(),
            "per_host": per_host,
            "slow_events": self._slow_events[:8],
            "violations": violations,
        }

    def assert_budget(self) -> None:
        v = self.audit()["violations"]
        if v:
            raise RuntimeError("取数节奏违约：" + "；".join(v))

    # ---------------- 传输 ----------------

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.workers, pool_maxsize=self.workers, max_retries=0
            )
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.s = s
        return s

    @staticmethod
    def _looks_like_waf(body: str) -> bool:
        head = body[:600].lower()
        return any(m in head for m in WAF_MARKS)

    @staticmethod
    def _retry_after(r: Any) -> float:
        """服务端说了算：429/503 带 Retry-After 时按它等，别自己指数退避。"""
        raw = (r.headers.get("Retry-After") or "").strip()
        if not raw:
            return 0.0
        try:
            return max(0.0, min(60.0, float(raw)))
        except ValueError:
            return 0.0

    def get(
        self,
        url: str,
        *,
        encoding: str | None = None,
        retries: int | None = None,
        base: float = 1.0,
        fresh: bool = False,
    ) -> str:
        host = _host_of(url)
        key = f"{url}|{encoding or ''}"
        if self._cache is not None and not fresh:
            hit = self._cache.get(key, self.policy.cache_ttl)
            if hit is not None:
                self.stats.add(cache_hit=1, ok=1)
                self._note(host, ms=0.0, nbytes=len(hit), status=200, ok=True, attempt=0)
                return hit

        self._check_circuit(url)
        tries = self.policy.retries if retries is None else retries
        last: Exception | None = None
        for attempt in range(tries + 1):
            self._throttle(url)
            t0 = time.monotonic()
            with self._sem:
                with self._state:
                    self._inflight += 1
                    self._peak = max(self._peak, self._inflight)
                try:
                    r = self._session().get(url, timeout=self.policy.timeout)
                    elapsed = time.monotonic() - t0
                    if self._looks_like_waf(r.text):
                        self._note(host, ms=elapsed, nbytes=0, status=r.status_code,
                                   ok=False, attempt=attempt)
                        self._note_block(True, host)
                        self._slow_down(host, f"WAF {r.status_code}")
                        self.stats.add(blocked=1, failed=1)
                        raise EndpointBlocked(
                            f"WAF 拦截（HTTP {r.status_code}，返回风控页，通常是频率过高）：{host}"
                        )
                    if r.status_code == 200:
                        if encoding:
                            r.encoding = encoding
                        body = r.text
                        self._note(host, ms=elapsed, nbytes=len(body), status=200,
                                   ok=True, attempt=attempt)
                        self._note_block(False, host)
                        self._speed_up(host)
                        self.stats.add(ok=1, bytes=len(body), seconds=elapsed)
                        if self._cache is not None:
                            self._cache.put(key, host, body)
                        return body
                    self._note(host, ms=elapsed, nbytes=0, status=r.status_code,
                               ok=False, attempt=attempt)
                    if r.status_code in (429, 503) or r.status_code >= 500:
                        self._slow_down(host, f"HTTP {r.status_code}")
                        self.stats.add(retry=1)
                        last = RuntimeError(f"HTTP {r.status_code}")
                        wait = self._retry_after(r) or base * (2 ** attempt) + random.uniform(0, 0.4)
                        time.sleep(min(60.0, wait))
                        continue
                    raise RuntimeError(f"HTTP {r.status_code}: {url[:120]}")
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError) as e:
                    elapsed = time.monotonic() - t0
                    self._note(host, ms=elapsed, nbytes=0, status=0, ok=False, attempt=attempt)
                    self.stats.add(seconds=elapsed)
                    last = e
                    if attempt == 0:
                        time.sleep(CONNECT_PROBE_DELAY)
                        continue
                    self._note_block(True, host)
                    self._slow_down(host, "RST")
                    self.stats.add(blocked=1, failed=1)
                    raise EndpointBlocked(f"连接被拒（RST）：{host}") from e
                except requests.exceptions.Timeout as e:
                    self._note(host, ms=time.monotonic() - t0, nbytes=0, status=0,
                               ok=False, attempt=attempt)
                    self._slow_down(host, "timeout")
                    self.stats.add(retry=1)
                    last = e
                    time.sleep(base * (2 ** attempt) + random.uniform(0, 0.4))
                finally:
                    with self._state:
                        self._inflight -= 1
        self.stats.add(failed=1)
        raise RuntimeError(f"重试耗尽：{url[:120]} ({last})")

    def get_json(self, url: str, **kw: Any) -> Any:
        body = self.get(url, **kw)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ContractError(f"返回非 JSON（前 120 字符）：{body[:120]!r}") from e

    def map(
        self,
        fn: Callable[[Any], Any],
        items: Sequence[Any],
        *,
        on_error: Callable[[Any, Exception], None] | None = None,
    ) -> list[Any]:
        """并发执行 fn(item)，保持输入顺序，失败项记 None。"""
        out: list[Any] = [None] * len(items)
        if not items:
            return out
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(fn, it): i for i, it in enumerate(items)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    out[i] = fut.result()
                except EndpointBlocked:
                    for f in futures:
                        f.cancel()
                    raise
                except Exception as e:  # noqa: BLE001
                    if on_error:
                        on_error(items[i], e)
                    out[i] = None
        return out

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()


def make_client(**overrides: Any) -> Client:
    """按 templates/fetch_policy.json 建 client，overrides 覆盖个别字段。"""
    pol = overrides.pop("policy", None) or load_policy()
    return Client(
        workers=int(overrides.pop("workers", pol.workers)),
        max_rps=float(overrides.pop("max_rps", pol.max_rps)),
        jitter=tuple(overrides.pop("jitter", pol.jitter)),
        timeout=float(overrides.pop("timeout", pol.timeout)),
        host_rps=overrides.pop("host_rps", None),
        policy=pol,
        **overrides,
    )


_default: Client | None = None
_default_lock = threading.Lock()


def default_client() -> Client:
    global _default
    with _default_lock:
        if _default is None:
            _default = make_client()
        return _default
