"""宽基指数取数：快照 1 次 + 每指数 1 次日 K。

    python -m pipeline.indices --as-of 2026-10-30

为什么要单独成模块：指数的 1/5/20/60 日收益此前是**硬编码**在 factors.py（IDX_REF）
和 build_report.py（idx 元组）里的常量，来自某一轮人工核对。常量一旦写死，
下一轮无论行情怎么走它都不会变，`rs` 因子的分母就成了假的。

取数成本：7 个指数 = 1 次批量快照 + 7 次日 K，共 8 次请求。
2026-10-31 腾讯日 K 曾被 WAF 拦（501），本轮（11-01）已恢复，故改为真取；
接口不可用时写明确原因并回落到 IDX_REF，由 build_report 记进 data_quality.missing。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day, http  # noqa: E402

CACHE = ROOT / "pipeline" / "cache"

# 报告里要展示的 6 个 + rs 基准（中证全指）。顺序即报告 indices 数组顺序。
INDEX_LIST = [
    ("000001.SH", "上证指数", "sh000001"),
    ("399001.SZ", "深证成指", "sz399001"),
    ("399006.SZ", "创业板指", "sz399006"),
    ("000688.SH", "科创50", "sh000688"),
    ("000300.SH", "沪深300", "sh000300"),
    ("000905.SH", "中证500", "sh000905"),
    ("000985.CSI", "中证全指", "sh000985"),
    ("000922.CSI", "中证红利", "sh000922"),
]

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def snapshot(client: http.Client) -> dict[str, dict]:
    """一次批量快照：现价、昨收、涨跌幅、成交额（万元）。"""
    syms = ",".join(sym for _, _, sym in INDEX_LIST)
    body = client.get(f"https://qt.gtimg.cn/q={syms}", encoding="gbk", retries=2)
    out: dict[str, dict] = {}
    for seg in body.split(";"):
        seg = seg.strip()
        if not seg.startswith("v_") or '"' not in seg:
            continue
        f = seg.split('"')[1].split("~")
        if len(f) < 50:
            continue
        sym = seg[2:].split("=")[0]
        out[sym] = {
            "price": _f(f[3]),
            "prev_close": _f(f[4]),
            "chg_pct": round((_f(f[32]) or 0.0) / 100.0, 4),
            "amount_yi": round((_f(f[37]) or 0.0) / 1e4, 2),
            "ts": f[30],
        }
    return out


def closes(client: http.Client, sym: str, n: int = 65) -> list[tuple[str, float]]:
    """日 K 收盘序列，升序。复权字段可能叫 qfqday 也可能叫 day（指数无复权）。"""
    j = client.get_json(KLINE_URL.format(sym=sym, n=n), retries=2)
    d = (j.get("data") or {}).get(sym) or {}
    rows = d.get("qfqday") or d.get("day") or []
    out = []
    for r in rows:
        if len(r) >= 3:
            c = _f(r[2])
            if c:
                out.append((r[0], c))
    out.sort()
    return out


def ret(series: list[tuple[str, float]], n: int) -> float | None:
    """最近 n 个交易日的复合收益；不足 n+1 根返回 None。"""
    if len(series) < n + 1:
        return None
    return round(series[-1][1] / series[-(n + 1)][1] - 1, 4)


def build(client: http.Client, as_of: str) -> dict:
    snap = snapshot(client)
    rows, errors = [], []
    for code, name, sym in INDEX_LIST:
        s = snap.get(sym) or {}
        try:
            ser = closes(client, sym)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name} 日K失败：{type(e).__name__}")
            ser = []
        if not ser:
            errors.append(f"{name} 无日K")
        last_date = ser[-1][0] if ser else None
        rows.append({
            "code": code, "name": name, "symbol": sym,
            "trade_date": last_date,
            "close": s.get("price") if s.get("price") else (ser[-1][1] if ser else None),
            "chg_1d": s.get("chg_pct"),
            "chg_5d": ret(ser, 5), "chg_20d": ret(ser, 20), "chg_60d": ret(ser, 60),
            "turnover_yi": s.get("amount_yi"),
            "bars": len(ser),
        })
    return {"as_of": as_of, "snapshot_ts": next((v.get("ts") for v in snap.values()), None),
            "indices": rows, "errors": errors}


def load(as_of: str) -> dict | None:
    p = CACHE / f"indices-{as_of}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--rps", type=float, default=3.0)
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)

    # 8 个请求不值得起并发；2 workers 让快照与日 K 能小范围重叠，仍受全局 2 rps 约束。
    cli = http.make_client(workers=2, max_rps=min(args.rps, 2.0), min_span=0,
                           jitter=(0.25, 0.55))
    try:
        d = build(cli, args.as_of)
    except http.EndpointBlocked as e:
        print(f"  指数接口被拒，保留上一轮留存值：{e}")
        return 3

    dest = CACHE / f"indices-{args.as_of}.json"
    dest.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {'指数':<10}{'收盘':>10}{'1日':>9}{'5日':>9}{'20日':>9}{'60日':>9}{'成交额亿':>10}  日K")
    for r in d["indices"]:
        def p(v):
            return "null" if v is None else f"{v * 100:+.2f}%"
        print(f"  {r['name']:<10}{(r['close'] or 0):>10.2f}{p(r['chg_1d']):>9}{p(r['chg_5d']):>9}"
              f"{p(r['chg_20d']):>9}{p(r['chg_60d']):>9}{(r['turnover_yi'] or 0):>10.1f}{r['bars']:>5}")
    if d["errors"]:
        print("  ⚠", "；".join(d["errors"]))
    print(f"  快照时间戳 {d['snapshot_ts']} | 请求 {cli.stats.ok} 次 / 失败 {cli.stats.failed}")
    print(f"  -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
