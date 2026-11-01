"""腾讯批量报价。

qt.gtimg.cn 支持一次查 60~120 只（实测 120 只 0.24s）。这是替代「逐只拉 K 线」的关键：
232 只成分股从 232 次请求压到 4 次。

字段索引（2026-11-01 实测校准，左闭右开按 '~' 切分）：
    1 名称  2 代码  3 现价  4 昨收  5 今开  6 成交量(手)
    30 时间戳 YYYYMMDDHHMMSS  31 涨跌额  32 涨跌%
    33 最高  34 最低  36 成交量(手)  37 成交额(万元)
    38 换手率  39 PE(TTM)  44 总市值(亿)  45 流通市值(亿)
    46 PB  47 涨停价  48 跌停价  49 量比
"""
from __future__ import annotations

from .http import Client

BATCH = 60
F = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "time": 30, "chg": 31, "chg_pct": 32, "high": 33, "low": 34,
    "vol": 36, "amount_wan": 37, "turnover": 38, "pe": 39,
    "mktcap_yi": 44, "float_cap_yi": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "vol_ratio": 49,
}


def to_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code[0] in "03":
        return "sz" + code
    return "bj" + code


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse(body: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for seg in body.split(";"):
        seg = seg.strip()
        if not seg.startswith("v_") or '"' not in seg:
            continue
        payload = seg.split('"')[1]
        f = payload.split("~")
        if len(f) < 50:
            continue
        code = f[F["code"]]
        if len(code) != 6:
            continue
        price = _num(f[F["price"]])
        if price is None:
            continue
        out[code] = {
            "code": code,
            "name": f[F["name"]].replace(" ", ""),
            "price": price,
            "prev_close": _num(f[F["prev_close"]]),
            "chg": _num(f[F["chg"]]),
            "chg_pct": (_num(f[F["chg_pct"]]) or 0.0) / 100.0,
            "high": _num(f[F["high"]]),
            "low": _num(f[F["low"]]),
            "amount_yi": round((_num(f[F["amount_wan"]]) or 0.0) / 1e4, 4),
            "turnover": _num(f[F["turnover"]]),
            "pe": _num(f[F["pe"]]),
            "mktcap_yi": _num(f[F["mktcap_yi"]]),
            "pb": _num(f[F["pb"]]),
            "limit_up": _num(f[F["limit_up"]]),
            "limit_down": _num(f[F["limit_down"]]),
            "vol_ratio": _num(f[F["vol_ratio"]]),
            "ts": f[F["time"]],
            "suspended": price <= 0,
        }
    return out


def fetch(client: Client, codes: list[str]) -> dict[str, dict]:
    """按 60 只一批拉全量报价，返回 {6 位代码: 报价}。"""
    syms = [to_symbol(c) for c in codes]
    batches = [syms[i:i + BATCH] for i in range(0, len(syms), BATCH)]

    def one(group: list[str]) -> dict[str, dict]:
        url = "https://qt.gtimg.cn/q=" + ",".join(group)
        return parse(client.get(url, encoding="gbk", retries=2))

    merged: dict[str, dict] = {}
    for part in client.map(one, batches):
        if part:
            merged.update(part)
    return merged


def trade_date(quotes: dict[str, dict]) -> str | None:
    """从报价时间戳里取交易日 YYYY-MM-DD。"""
    for q in quotes.values():
        ts = q.get("ts") or ""
        if len(ts) >= 8:
            return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    return None
