"""市场级杠杆：两融余额的取数、口径与降级。

**为什么单列一个模块**
两融余额不是板块指标，是市场级杠杆环境；它决定「券商 / 高 beta 有没有燃料」，
但不进六因子打分（否则同一份报告里它会被重复计价）。所以放在 `market_regime.leverage`。

**为什么用东财汇总而不用交易所分市接口**
    实测 2026-11-03 08:38：
      沪市（Wind M0061608）已到 11-02，深市（M0061613）只到 10-30。
    分市接口两市披露节奏不同，合计序列会出现「半天更新」窗口——沪市滚到 T 日、深市还停在
    T-2，两者相加等于造出一个不存在的日期。东财 RPTA_RZRQ_LSHJ 是市场汇总，同日、单源、
    口径统一，且直接给融资净买入与「融资余额占流通市值比」，省去自算差分。
    代价是相对 report as_of 滞后 1 个交易日（T 日数据 T+1 早间才入库）。

**一次请求拿满整个分位窗口**
    pageSize=300 + DIM_DATE 下限过滤 = 258 行 / 260 个交易日，分位与 20/60 日变化一次算完，
    占取数预算 1 次。相比逐日拼接或按市场分三次取，这是这套数据最省的做法。

**口径纪律**
    1. 只认一个源。绝不与交易所分市数据混算（汇总比沪深分市合计高约 0.3%，是否含北交所需
       另行核实；混算会让 `as_of` 与水平值同时失去意义）。
    2. 水平值用绝对余额，趋势看 `chg_20d`，杠杆率看 `balance_to_float_pct`——
       后者做了流通市值归一，跨年份可比，不会被市值膨胀带偏。
    3. 滞后按**交易日**计，且以 `report.as_of` 为基准，不以生成时间为基准。
       as_of=11-02、数据=10-30 → lag=1，不是 2。

用法：
    python -m pipeline.margin --as-of 2026-11-02
    from pipeline import margin
    lev = margin.build(as_of)        # 取数 + 计算 + 落 cache/margin-<as_of>.json
    lev = margin.load(as_of)         # 只读缓存
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import day, http  # noqa: E402

CACHE = ROOT / "pipeline" / "cache"

# 市场汇总：融资余额 RZYE / 融券余额 RQYE / 两融余额 RZRQYE / 融资净买入 RZJME(+3D/5D/10D)
# / 融资余额占流通市值比 RZYEZB / 流通市值 LTSZ
URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
       "reportName=RPTA_RZRQ_LSHJ"
       "&columns=DIM_DATE,RZYE,RQYE,RZRQYE,RZYEZB,RZJME,RZJME3D,RZJME5D,LTSZ"
       "&pageNumber=1&pageSize={size}&sortColumns=DIM_DATE&sortTypes=-1"
       "&filter=(DIM_DATE%3E%3D%27{since}%27)")

SIZE = 300               # 覆盖 260 个交易日，一次请求
LOOKBACK_DAYS = 400      # 自然日回看，保证 trading 260 日够用
MIN_ROWS = 21            # 少于 21 行连 20 日变化都算不出，直接判不可用

# 趋势阈值：20 日两融余额变动。±3% 是慢变量的「明确方向」分界，
# 单日噪音 σ≈0.5pct，20 日累计要到 3% 才算脱离噪音（约 6σ）。
TREND_DOWN = -0.03
TREND_UP = 0.03

TREND_LABEL = {
    "deleveraging": "去杠杆",
    "stable": "杠杆平稳",
    "releveraging": "加杠杆",
}


def _pct(a: float, b: float) -> float | None:
    if not a or not b:
        return None
    return a / b - 1


def _yi(v: float | None) -> float | None:
    return None if v is None else round(v / 1e8, 1)


def _lag(as_of: str, data_as_of: str, cal: list[str] | None = None) -> int | None:
    """数据日相对报告基准日的**交易日**滞后。as_of 与数据日相同 → 0。"""
    if not as_of or not data_as_of:
        return None
    if as_of == data_as_of:
        return 0
    try:
        days = cal or day.recent_days(120)
    except Exception:                                     # noqa: BLE001
        return None
    if data_as_of not in days or as_of not in days:
        return None
    return days.index(as_of) - days.index(data_as_of)


def fetch(as_of: str, *, cli: http.Client | None = None) -> list[dict]:
    """一次请求取回 260 交易日内的市场汇总（降序，首行为最新）。"""
    since = (dt.date.fromisoformat(as_of) - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    cli = cli or http.make_client(workers=1, max_rps=2.0, min_span=0)
    j = cli.get_json(URL.format(size=SIZE, since=since))
    if not isinstance(j, dict) or j.get("code") not in (0, "0"):
        raise RuntimeError(f"两融接口返回异常：code={j.get('code') if isinstance(j, dict) else '?'}")
    rows = ((j.get("result") or {}).get("data")) or []
    out = []
    for r in rows:
        d = str(r.get("DIM_DATE") or "")[:10]
        if not d or d > as_of:                            # 报告基准日之后的行一律不要
            continue
        if r.get("RZRQYE") is None:
            continue
        out.append({"date": d, **r})
    out.sort(key=lambda x: x["date"], reverse=True)
    if len(out) < MIN_ROWS:
        raise RuntimeError(f"两融序列仅 {len(out)} 行（<{MIN_ROWS}），20 日变化不可算")
    return out


def compute(rows: list[dict], as_of: str, *, cal: list[str] | None = None) -> dict:
    """把降序序列折算成 market_regime.leverage 块。所有比值按小数（−0.0175 = −1.75%）。"""
    latest = rows[0]
    bal = [float(r["RZRQYE"]) for r in rows]

    def at(n: int) -> float | None:
        return bal[n] if len(bal) > n else None

    net5 = sum(float(r.get("RZJME") or 0) for r in rows[:5])
    net20 = sum(float(r.get("RZJME") or 0) for r in rows[:20])
    win = bal[:250]
    pct250 = None
    if len(win) >= 60:                                    # 少于 60 日不给分位，避免「3 日样本」
        pct250 = round(sum(1 for v in win if v <= bal[0]) / len(win), 4)

    chg20 = _pct(bal[0], at(20)) if at(20) else None
    if chg20 is None:
        trend = None
    elif chg20 <= TREND_DOWN:
        trend = "deleveraging"
    elif chg20 >= TREND_UP:
        trend = "releveraging"
    else:
        trend = "stable"

    lev = {
        "as_of": latest["date"],
        "lag_trading_days": _lag(as_of, latest["date"], cal),
        "scope": "cn_all",
        "balance_yi": _yi(bal[0]),
        "fin_balance_yi": _yi(latest.get("RZYE")),
        "lend_balance_yi": _yi(latest.get("RQYE")),
        "balance_to_float_pct": (round(float(latest["RZYEZB"]) / 100, 5)
                                 if latest.get("RZYEZB") is not None else None),
        "chg_1d": round(_pct(bal[0], at(1)), 4) if at(1) else None,
        "chg_5d": round(_pct(bal[0], at(5)), 4) if at(5) else None,
        "chg_20d": round(chg20, 4) if chg20 is not None else None,
        "chg_60d": round(_pct(bal[0], at(60)), 4) if at(60) else None,
        "net_buy_5d_yi": _yi(net5),
        "net_buy_20d_yi": _yi(net20),
        "balance_pct_250": pct250,
        "trend": trend,
        "trend_label": TREND_LABEL.get(trend or "", None),
        "note": (
            f"东财市场汇总（RPTA_RZRQ_LSHJ），融资余额 {_yi(latest.get('RZYE')):.1f} 亿 + "
            f"融券余额 {_yi(latest.get('RQYE')):.1f} 亿；口径含沪深北，与交易所沪深分市合计差约 +0.3%，"
            f"不与其他源混算；滞后 {_lag(as_of, latest['date'], cal)} 个交易日，"
            f"分位基于 {len(win)} 个交易日"
        ),
    }
    return lev


def build(as_of: str, *, cli: http.Client | None = None, save: bool = True) -> dict:
    rows = fetch(as_of, cli=cli)
    lev = compute(rows, as_of)
    if save:
        CACHE.mkdir(parents=True, exist_ok=True)
        dest = CACHE / f"margin-{as_of}.json"
        dest.write_text(json.dumps({"as_of": as_of, "source": "eastmoney:RPTA_RZRQ_LSHJ",
                                    "requests": 1, "rows": len(rows), "leverage": lev},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    return lev


def load(as_of: str) -> dict | None:
    p = CACHE / f"margin-{as_of}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("leverage")


def main() -> int:
    ap = argparse.ArgumentParser(description="两融余额（市场级杠杆）取数")
    day.add_as_of(ap)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)

    lev = build(args.as_of, save=not args.no_save)
    print(f"  两融余额 {lev['balance_yi']:.1f} 亿（{lev['as_of']}，lag={lev['lag_trading_days']}）")
    print(f"  融资 {lev['fin_balance_yi']:.1f} 亿 / 融券 {lev['lend_balance_yi']:.1f} 亿")
    print(f"  占流通市值 {lev['balance_to_float_pct'] * 100:.3f}%  250日分位 {lev['balance_pct_250']}")
    print("  1日 {v[chg_1d]:+.2%}  5日 {v[chg_5d]:+.2%}  20日 {v[chg_20d]:+.2%}  60日 {v[chg_60d]:+.2%}".format(
        v={k: (lev[k] if lev[k] is not None else 0.0) for k in ("chg_1d", "chg_5d", "chg_20d", "chg_60d")}))
    print(f"  融资净买入 5日 {lev['net_buy_5d_yi']:+.1f} 亿 / 20日 {lev['net_buy_20d_yi']:+.1f} 亿")
    print(f"  趋势 {lev['trend']}（{lev['trend_label']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
