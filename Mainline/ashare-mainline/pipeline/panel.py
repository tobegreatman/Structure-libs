"""板块面板：涨跌幅 / 资金流 / 反推成交额 / 份额分位。

push2his 的 kline/get 被应用层封禁后，板块日线成交额没有干净来源
（RPT_BOARD_DAILY 不存在，push2 clist 只有当日快照，腾讯没有东财板块映射）。
唯一可用的路径是 RPT_INDUSTRY_FUNDFLOW，用「主力净额 ÷ 净流入占比」反推成交额：
5 个分档（主力/超大/大/中/小）各给一个商，取中位数。实测与快照对比
中位误差 0.0000%，P90 13.9%——尾巴来自净流入占比趋近 0 的横盘日。
"""
from __future__ import annotations

import statistics

from .http import Client

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
COLS = (
    "BOARD_CODE,BOARD_NAME,TRADE_DATE,CHANGE_RATE,NET_INFLOW,NET_INFLOW_RATIO,"
    "SUPERDEAL_NET,SUPERDEAL_NET_RATIO,BIGDEAL_NET,BIGDEAL_NET_RATIO,"
    "MIDDEAL_NET,MIDDEAL_NET_RATIO,SMALLDEAL_NET,SMALLDEAL_NET_RATIO"
)


def _url(flt: str, page: int = 1, size: int = 500, sort: str = "TRADE_DATE") -> str:
    return (
        f"{BASE}?reportName=RPT_INDUSTRY_FUNDFLOW&columns={COLS}"
        f"&pageNumber={page}&pageSize={size}&sortColumns={sort}&sortTypes=-1"
        f"&filter={flt}"
    )


def amount_from_flow(row: dict) -> float | None:
    """用各档净额 ÷ 占比反推成交额，取中位数（单位：元）。"""
    pairs = [
        (row.get("NET_INFLOW"), row.get("NET_INFLOW_RATIO")),
        (row.get("SUPERDEAL_NET"), row.get("SUPERDEAL_NET_RATIO")),
        (row.get("BIGDEAL_NET"), row.get("BIGDEAL_NET_RATIO")),
        (row.get("MIDDEAL_NET"), row.get("MIDDEAL_NET_RATIO")),
        (row.get("SMALLDEAL_NET"), row.get("SMALLDEAL_NET_RATIO")),
    ]
    vals = []
    for net, ratio in pairs:
        if net is None or ratio is None:
            continue
        try:
            net = float(net)
            ratio = float(ratio)
        except (TypeError, ValueError):
            continue
        if abs(ratio) < 0.05 or net == 0:
            continue
        vals.append(net / (ratio / 100.0))
    if not vals:
        return None
    return statistics.median(vals)


def board_list(client: Client, as_of: str) -> list[dict]:
    """某个交易日有资金流数据的全部板块（一次请求）。"""
    flt = f"(TRADE_DATE%3D%27{as_of}%27)"
    j = client.get_json(_url(flt, size=500, sort="BOARD_CODE"))
    rows = ((j.get("result") or {}).get("data")) or []
    out = []
    for r in rows:
        code = r.get("BOARD_CODE")
        if not code:
            continue
        out.append({
            "code": code,
            "name": r.get("BOARD_NAME"),
            "pct": r.get("CHANGE_RATE"),
            "amount": amount_from_flow(r),
            "net": r.get("NET_INFLOW"),
        })
    return out


def board_history(client: Client, code: str, since: str) -> list[dict] | None:
    """单板块日线（涨跌幅 + 资金流 + 反推成交额），按日期降序。"""
    flt = f"(BOARD_CODE%3D%22{code}%22)(TRADE_DATE%3E%3D%27{since}%27)"
    j = client.get_json(_url(flt, size=500))
    rows = ((j.get("result") or {}).get("data")) or []
    if not rows:
        return None
    out = []
    for r in rows:
        d = str(r.get("TRADE_DATE", ""))[:10]
        if not d:
            continue
        out.append({
            "d": d,
            "pct": r.get("CHANGE_RATE"),
            "amount": amount_from_flow(r),
            "net": r.get("NET_INFLOW"),
        })
    out.sort(key=lambda x: x["d"], reverse=True)
    return out


def build(client: Client, as_of: str, since: str, *, verbose: bool = True) -> dict:
    """拉全样本面板，返回 {boards, by_date, market_amount}。"""
    listed = board_list(client, as_of)
    if verbose:
        print(f"  当日板块 {len(listed)} 个，开始拉历史…")

    codes = [b["code"] for b in listed]
    histories = client.map(lambda c: board_history(client, c, since), codes)

    boards, dropped = [], []
    for meta, hist in zip(listed, histories):
        if not hist:
            dropped.append(meta["code"])
            continue
        boards.append({**meta, "hist": hist})

    if not boards:
        raise RuntimeError("面板为空：所有板块历史都拉不到")

    # 停更的判据是「最后一行不在目标日」，不是「行数最短」。
    # 东财 2026-02-13 调整行业分类后，白酒Ⅱ 这类板块的历史只有 169 天，
    # 但它在 10-30 仍有数据，属于现役；食品饮料、煤炭等才是真的不再更新。
    live, stale = [], []
    for b in boards:
        last = b["hist"][0]["d"] if b["hist"] else ""
        (live if last == as_of else stale).append(b)
    if not live:
        raise RuntimeError(f"没有板块在 {as_of} 有数据，检查交易日是否正确")

    spans = sorted(len(b["hist"]) for b in live)
    full_span = spans[-1]
    common_span = spans[max(0, int(len(spans) * 0.1))]
    if verbose:
        print(f"  现役 {len(live)} 个（最长 {full_span} 日 / 共同 {common_span} 日），"
              f"停更 {len(stale)} 个，拉空 {len(dropped)} 个")
        if stale:
            print("  停更：" + "、".join(b["name"] for b in stale[:14]) + ("…" if len(stale) > 14 else ""))
        short = [b["name"] for b in live if len(b["hist"]) <= common_span][:6]
        if short and common_span < full_span:
            print(f"  历史偏短（分类调整前无数据）：{'、'.join(short)}")

    by_date: dict[str, dict[str, dict]] = {}
    history: dict[str, list[tuple[str, float]]] = {}
    for b in live:
        for row in b["hist"]:
            by_date.setdefault(row["d"], {})[b["code"]] = row
        history[b["code"]] = [(r["d"], r["pct"]) for r in reversed(b["hist"]) if r["pct"] is not None]

    market = {}
    for d, m in by_date.items():
        total = sum(v["amount"] for v in m.values() if v.get("amount"))
        market[d] = total or None

    return {
        "as_of": as_of,
        "since": since,
        "span": common_span,
        "span_full": full_span,
        "boards": [{k: v for k, v in b.items() if k != "hist"} | {"bars": len(b["hist"])} for b in live],
        "by_date": by_date,
        "history": history,
        "market_amount": market,
        "dropped": dropped,
        "stale": [b["name"] for b in stale],
    }


def rolling_return(panel: dict, n: int = 20) -> dict[str, dict[str, float]]:
    """每个板块在每个交易日的前 n 日累计涨幅。

    P1 的「进前 10 的天数」是按 **滚动 n 日累计涨幅** 的横截面排名算的，
    不是按当日涨跌幅。当日排名波动太大，白酒 2026-10-28 单日 −2.33% 排全样本末位，
    按当日算不可能「连续 11 天进前 10」。
    """
    out: dict[str, dict[str, float]] = {}
    for code, series in panel.get("history", {}).items():
        asc = series
        if len(asc) < n:
            continue
        acc: dict[str, float] = {}
        for i in range(n - 1, len(asc)):
            prod = 1.0
            for j in range(i - n + 1, i + 1):
                prod *= 1.0 + asc[j][1] / 100.0
            acc[asc[i][0]] = prod - 1.0
        out[code] = acc
    return out


def top_days(panel: dict, code: str, n: int = 20, top: int = 10) -> dict:
    """近 n 个交易日里，该板块滚动 n 日涨幅进入全样本前 top 的天数与连续天数。"""
    roll = panel.get("_roll") or rolling_return(panel)
    panel["_roll"] = roll
    dates = sorted(panel["by_date"], reverse=True)[:n]
    hits = streak = 0
    broken = False
    for d in dates:
        board = {c: acc[d] for c, acc in roll.items() if d in acc}
        if not board:
            continue
        ranked = sorted(board.items(), key=lambda x: x[1], reverse=True)
        in_top = code in {c for c, _ in ranked[:top]}
        if in_top:
            hits += 1
            if not broken:
                streak += 1
        else:
            broken = True
    return {"days_top10_20": hits, "consecutive_top10": streak}


def share_series(panel: dict, code: str, market_scale: float = 1.0) -> list[tuple[str, float]]:
    """单板块的成交额份额序列（分母为当日全市场，按 market_scale 校准）。"""
    out = []
    for d, m in panel["by_date"].items():
        total = panel["market_amount"].get(d)
        row = m.get(code)
        if not total or not row or not row.get("amount"):
            continue
        out.append((d, row["amount"] / (total * market_scale)))
    out.sort()
    return out
