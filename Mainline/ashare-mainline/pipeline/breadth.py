"""全市场涨跌家数（breadth）备用通道。

背景：东财 push2 系列域名（push2 / push2delay / 82.push2）在部分网络下 TCP 层被 RST，
无法取全市场快照，导致 market_regime.breadth.up_ratio 只能写 null。

方案：走新浪财经 Market_Center.getHQNodeData（node=hs_a），分页拉全 A 快照，
本地按收盘涨跌幅自行统计。相比「涨跌家数」计数接口，本方案的优势是：
  1) 每一只股票的取值都在本地，任何数字可复算、可审计；
  2) 可同时得到涨跌分布、分市场统计、全市场成交额，交叉验证板块代理口径。

口径（写进报告 data_quality.notes）：
  - 有效样本 = 有成交（volume>0）且 price>0 的个股，停牌股不计入分子分母；
  - up/down/flat 按 changepercent 严格 >0 / <0 / ==0 划分；
  - up_ratio = up / valid；advance_decline_ratio = up / max(down,1)。

用法：
    python -m pipeline.breadth --as-of 2026-11-02
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.parse
import urllib.request

from pipeline import day

CACHE = pathlib.Path(__file__).resolve().parent / "cache"
BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData")
COUNT = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "Market_Center.getHQNodeStockCount")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _get(url: str, tries: int = 3) -> str:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://finance.sina.com.cn/",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                     # noqa: BLE001
            last = e
            time.sleep(1.2 * (2 ** i))
    raise RuntimeError(f"fetch failed: {url[:80]} -> {type(last).__name__}: {last}")


def total_count() -> int:
    txt = _get(COUNT + "?node=hs_a")
    return int(txt.strip().strip('"'))


def fetch_all(page_size: int = 100, gap: float = 0.25) -> list[dict]:
    """按 symbol 升序分页拉全 A，返回原始行。"""
    n = total_count()
    pages = (n + page_size - 1) // page_size
    rows: list[dict] = []
    seen: set[str] = set()
    for p in range(1, pages + 1):
        q = urllib.parse.urlencode({
            "page": p, "num": page_size, "sort": "symbol", "asc": "1", "node": "hs_a",
        })
        txt = _get(f"{BASE}?{q}")
        try:
            chunk = json.loads(txt)
        except json.JSONDecodeError:
            chunk = []                              # 越界页返回 null
        if not chunk:
            break
        for r in chunk:
            c = str(r.get("code") or "").zfill(6)
            if c not in seen:
                seen.add(c)
                rows.append(r)
        if p % 10 == 0:
            print(f"  page {p}/{pages} rows={len(rows)}", flush=True)
        time.sleep(gap)
    print(f"  total rows {len(rows)} (declared {n})", flush=True)
    return rows


def board_of(code: str, symbol: str) -> str:
    s = (symbol or "").lower()
    if s.startswith("bj"):
        return "北交所"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if code.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "其他"


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def stats(rows: list[dict]) -> dict:
    valid, up, down, flat, suspended = [], 0, 0, 0, 0
    amount_yi = 0.0
    dist = {"gt9": 0, "5to9": 0, "2to5": 0, "0to2": 0, "0to-2": 0,
            "-2to-5": 0, "-5to-9": 0, "lt-9": 0}
    by_board: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("code") or "").zfill(6)
        price = _f(r.get("trade"))
        vol = _f(r.get("volume"))
        pct = _f(r.get("changepercent")) / 100.0
        if price <= 0 or vol <= 0:
            suspended += 1
            continue
        valid.append(code)
        amount_yi += _f(r.get("amount")) / 1e8
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        k = "gt9" if pct >= 0.098 else ("5to9" if pct >= 0.05 else
             ("2to5" if pct >= 0.02 else ("0to2" if pct > 0 else
             ("0to-2" if pct > -0.02 else ("-2to-5" if pct > -0.05 else
             ("-5to-9" if pct > -0.098 else "lt-9"))))))
        if pct == 0:
            k = "0to2"
        dist[k] += 1
        b = board_of(code, r.get("symbol", ""))
        d = by_board.setdefault(b, {"valid": 0, "up": 0, "down": 0, "flat": 0})
        d["valid"] += 1
        d["up" if pct > 0 else ("down" if pct < 0 else "flat")] += 1

    n = len(valid)
    return {
        "universe": len(rows),
        "valid": n,
        "suspended": suspended,
        "up": up,
        "down": down,
        "flat": flat,
        "up_ratio": round(up / n, 4) if n else None,
        "down_ratio": round(down / n, 4) if n else None,
        "advance_decline_ratio": round(up / down, 2) if down else None,
        "amount_yi": round(amount_yi, 1),
        "distribution": dist,
        "by_board": by_board,
    }


def verify_against_tencent(rows: list[dict], extra: int = 5) -> dict:
    """随机抽样与腾讯行情对逐只比对，作为「单一快照源」的独立校验。

    腾讯 q= 接口字段（~ 分隔）：[3] 现价、[32] 涨跌幅%、[37] 成交额（万元）。
    """
    import random

    valid = [r for r in rows if _f(r.get("trade")) > 0 and _f(r.get("volume")) > 0]
    random.seed(7)
    sample = random.sample(valid, min(12, len(valid)))
    # 补几只权重股与北交所，避免样本全落在小市值
    by = {str(r.get("code")).zfill(6): r for r in rows}
    for c in ("600519", "000858", "300750", "688111", "920000"):
        if c in by and by[c] not in sample:
            sample.append(by[c])

    worst = {"price": 0.0, "pct": 0.0, "amt": 0.0}
    ok_n = 0
    for r in sample:
        try:
            req = urllib.request.Request(
                f"https://qt.gtimg.cn/q={r['symbol'].lower()}",
                headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                f = resp.read().decode("gbk", "replace").split('"')[1].split("~")
            t_price, t_pct, t_amt = _f(f[3]), _f(f[32]), _f(f[37]) / 1e4
            s_price, s_pct = _f(r.get("trade")), _f(r.get("changepercent"))
            s_amt = _f(r.get("amount")) / 1e8
            dp = abs(s_price - t_price) / t_price * 100 if t_price else 0.0
            dc = abs(s_pct - t_pct)
            da = abs(s_amt - t_amt) / t_amt * 100 if t_amt else 0.0
            worst["price"] = max(worst["price"], dp)
            worst["pct"] = max(worst["pct"], dc)
            worst["amt"] = max(worst["amt"], da)
            if dp < 0.5 and dc < 0.05 and da < 2:
                ok_n += 1
        except Exception:                          # noqa: BLE001
            continue
    return {"sampled": len(sample), "matched": ok_n,
            "max_price_diff_pct": round(worst["price"], 3),
            "max_pct_diff_pct": round(worst["pct"], 3),
            "max_amount_diff_pct": round(worst["amt"], 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--cache", action="store_true", help="已有全量缓存则跳过抓取")
    ap.add_argument("--verify", action="store_true", help="抽样与腾讯行情交叉校验")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)
    CACHE.mkdir(exist_ok=True)
    raw_p = CACHE / f"quotes-all-{args.as_of}.json"
    out_p = CACHE / f"breadth-{args.as_of}.json"

    if raw_p.exists() and args.cache:
        rows = json.loads(raw_p.read_text(encoding="utf-8"))
        print(f"  cache hit {raw_p.name} rows={len(rows)}")
    else:
        rows = fetch_all()
        raw_p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    s = stats(rows)
    s["as_of"] = args.as_of
    if args.verify:
        v = verify_against_tencent(rows)
        s["cross_check"] = v
        print(f"  交叉校验：抽 {v['sampled']} 只对腾讯，一致 {v['matched']} 只，"
              f"最大价差 {v['max_price_diff_pct']}%、涨跌幅差 {v['max_pct_diff_pct']}pct、"
              f"成交额差 {v['max_amount_diff_pct']}%")
    s["source"] = {
        "name": "新浪财经全A快照（node=hs_a）",
        "reliability": "high",
        "note": (f"全量 {s['universe']} 只，有效（有成交）{s['valid']} 只，"
                 f"停牌/无成交 {s['suspended']} 只；涨跌按收盘 changepercent 严格划分"),
    }
    out_p.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {args.as_of} 上涨 {s['up']} / 下跌 {s['down']} / 平盘 {s['flat']} / 停牌 {s['suspended']}")
    print(f"  up_ratio {s['up_ratio']}  涨跌比 {s['advance_decline_ratio']}  成交额 {s['amount_yi']} 亿")
    for b, d in s["by_board"].items():
        print(f"    {b:<5} valid {d['valid']:>5} up {d['up']:>5} down {d['down']:>5} "
              f"up_ratio {d['up'] / d['valid']:.4f}")
    print(f"  -> {out_p.name}")


if __name__ == "__main__":
    main()
