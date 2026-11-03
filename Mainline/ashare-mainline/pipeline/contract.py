"""接口契约前置校验。

问题背景：2026-10-30 那轮有三批请求是白拉的——拉完 99 个板块才发现数据在
2026-02-13 之前截断、target 日期一行都没有、13 个板块已停更。三轮加起来 424 秒。

做法：每个数据源在用之前，先拉 **1 条样本** 做断言（字段存在、目标日期命中、
数值可解析），不通过就在 1 秒内失败并给出诊断，而不是拉到一半才发现。

契约分两层：
- 入口契约 enforce()：单样本探测，快（4 个源约 1 秒）。
- 出口断言 assert_bulk()：全量结果落地前的规约检查（板块数、命中行数、覆盖率）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .http import Client, ContractError

_CHECK = Callable[[Any], None]


@dataclass
class Contract:
    name: str
    url: str
    check: _CHECK
    hint: str
    json: bool = True
    encoding: str | None = None
    required: bool = True
    fallback: str = ""
    _ok: bool = field(default=False, init=False)


def _need(cond: bool, msg: str, hint: str = "") -> None:
    if not cond:
        raise ContractError(f"{msg}" + (f" | 修法：{hint}" if hint else ""))


def _fundflow_check(as_of: str) -> _CHECK:
    def check(j: Any) -> None:
        _need(isinstance(j, dict), "返回不是 dict")
        _need(j.get("code") in (0, "0"), f"code={j.get('code')} message={j.get('message')}",
              "报表名或参数失效，检查 reportName / filter 拼装")
        rows = ((j.get("result") or {}).get("data")) or []
        _need(len(rows) > 0, "data 为空（板块无行）",
              "板块可能已停更，或 TRADE_DATE 过滤把窗口切没了")
        row = rows[0]
        for k in ("TRADE_DATE", "CHANGE_RATE", "NET_INFLOW", "NET_INFLOW_RATIO"):
            _need(k in row and row[k] is not None, f"缺字段 {k}")
        _need(isinstance(row["CHANGE_RATE"], (int, float)), "CHANGE_RATE 非数值")
        dates = [str(r.get("TRADE_DATE", ""))[:10] for r in rows]
        _need(dates[0] == as_of, f"最新行是 {dates[0]}，不是目标日 {as_of}",
              "该交易日可能未披露，或数据源尚未更新")
        _need(len(rows) >= 100, f"仅 {len(rows)} 行，窗口不足 100 个交易日",
              "加 TRADE_DATE 下限过滤后可放宽 pageSize")
    return check


def _constituent_check() -> _CHECK:
    def check(j: Any) -> None:
        _need(isinstance(j, dict), "返回不是 dict")
        rows = ((j.get("result") or {}).get("data")) or []
        _need(len(rows) > 0, "data 为空")
        row = rows[0]
        _need("BOARD_CODE_BK" in row, f"缺 BOARD_CODE_BK，实际键={list(row)[:8]}")
        _need("SECURITY_CODE" in row, "缺 SECURITY_CODE")
        _need(len(str(row["SECURITY_CODE"])) == 6, f"SECURITY_CODE 异常：{row['SECURITY_CODE']}")
    return check


def _quote_check() -> _CHECK:
    def check(body: str) -> None:
        _need("v_" in body, f"无 v_ 前缀，原始内容：{body[:80]!r}")
        payload = body.split('"')[1] if '"' in body else ""
        f = payload.split("~")
        _need(len(f) >= 50, f"字段数仅 {len(f)}，预期 ≥50",
              "腾讯字段布局变了，需要重新校准索引")
        _need(f[3].replace(".", "", 1).isdigit(), f"现价非数值：{f[3]!r}")
        _need(float(f[3]) > 0, "现价为 0（停牌或代码错误）")
    return check


def _kline_check() -> _CHECK:
    def check(j: Any) -> None:
        _need(isinstance(j, dict), "返回不是 dict")
        data = j.get("data") or {}
        _need(bool(data), "data 为空（代码/市场前缀不匹配）")
        sym, node = next(iter(data.items()))
        bars = node.get("qfqday") or node.get("day") or []
        _need(len(bars) >= 21, f"仅 {len(bars)} 根 K 线，P2 需 ≥21 根",
              "换 kline 接口或缩短需求天数")
        _need(len(bars[0]) >= 3, f"K 线字段不足：{bars[0]}")
        float(bars[-1][2])
    return check


def _margin_check(as_of: str) -> _CHECK:
    """两融市场汇总：只验「字段齐 + 日期不越界 + 能算日变动」，滞后交给 margin.compute 判。

    滞后是常态（T 日数据 T+1 早间入库），不能当契约失败；越界（返回了 as_of 之后的行）
    才是真问题——那说明 DIM_DATE 过滤没生效，会把未来数据混进本期。
    """
    def check(j: Any) -> None:
        _need(isinstance(j, dict), "返回不是 dict")
        _need(j.get("code") in (0, "0"), f"code={j.get('code')} message={j.get('message')}",
              "reportName RPTA_RZRQ_LSHJ 可能改名，或 DIM_DATE 过滤语法变了")
        rows = ((j.get("result") or {}).get("data")) or []
        _need(len(rows) >= 2, "不足 2 行，连日变动都算不出")
        for k in ("DIM_DATE", "RZRQYE", "RZYE", "RQYE"):
            _need(k in rows[0] and rows[0][k] is not None, f"缺字段 {k}")
        dates = [str(r.get("DIM_DATE") or "")[:10] for r in rows]
        _need(dates[0] <= as_of, f"最新行 {dates[0]} 晚于基准日 {as_of}，DIM_DATE 过滤未生效",
              "把 filter 下限改成 as_of 之前的日期，且不要按 pageNumber 倒序盲取")
        _need(dates == sorted(dates, reverse=True), f"日期非降序：{dates[:5]}")
        _need(float(rows[0]["RZRQYE"]) > 0, "两融余额非正数")
    return check


FUNDFLOW_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_INDUSTRY_FUNDFLOW"
    "&columns=BOARD_CODE,BOARD_NAME,TRADE_DATE,CHANGE_RATE,NET_INFLOW,NET_INFLOW_RATIO"
    "&pageNumber=1&pageSize=200&sortColumns=TRADE_DATE&sortTypes=-1"
    "&filter=(BOARD_CODE%3D%22{board}%22)(TRADE_DATE%3E%3D%27{since}%27)"
)
MARGIN_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
    "&columns=DIM_DATE,RZYE,RQYE,RZRQYE,RZYEZB&pageNumber=1&pageSize=5"
    "&sortColumns=DIM_DATE&sortTypes=-1"
)
CONSTITUENT_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_BOARD_CONSTITUENT"
    "&columns=BOARD_CODE_BK,BOARD_NAME,SECURITY_CODE&pageNumber=1&pageSize=500"
    "&sortColumns=BOARD_CODE_BK&sortTypes=1"
)
QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"


def builtin(as_of: str, since: str) -> list[Contract]:
    return [
        Contract(
            name="东财资金流 RPT_INDUSTRY_FUNDFLOW",
            url=FUNDFLOW_URL.format(board="BK1277", since=since),
            check=_fundflow_check(as_of),
            hint="确认目标日已披露、板块未停更",
        ),
        Contract(
            name="东财板块成分 RPT_BOARD_CONSTITUENT",
            url=CONSTITUENT_URL,
            check=_constituent_check(),
            hint="确认 reportName 未改名",
        ),
        Contract(
            name="腾讯批量报价 qt.gtimg.cn",
            url=QUOTE_URL.format(codes="sh600519,sz000858,sz000568"),
            check=_quote_check(),
            hint="确认字段索引仍为 3=现价 4=昨收 31=涨跌额 32=涨跌% 37=成交额(万)",
            json=False,
            encoding="gbk",
        ),
        Contract(
            name="腾讯日 K web.ifzq.gtimg.cn",
            url=KLINE_URL.format(sym="sh600519", n=25),
            check=_kline_check(),
            hint="换备用域名或缩短窗口",
            required=False,
            fallback="本地 K 线库已有数据的股票不受影响，只有首次建库会缺",
        ),
        Contract(
            name="东财两融市场汇总 RPTA_RZRQ_LSHJ",
            url=MARGIN_URL,
            check=_margin_check(as_of),
            hint="确认 reportName 未改名；滞后 1 个交易日属正常，不要据此判失败",
            required=False,
            fallback="market_regime.leverage 整块写 null 并记入 missing，叙事不使用杠杆维度",
        ),
    ]


def enforce(
    client: Client, contracts: list[Contract], *, verbose: bool = True
) -> tuple[list[Contract], list[tuple[str, str]]]:
    """逐个跑入口契约。必需项失败立刻抛；可降级项失败只记入 degraded。"""

    def one(c: Contract) -> tuple[str, bool, str]:
        try:
            raw = client.get(c.url, encoding=c.encoding, retries=1)
            c.check(client_json(raw) if c.json else raw)
            return c.name, True, "ok"
        except Exception as e:  # noqa: BLE001
            return c.name, False, str(e)[:220]

    results = client.map(one, contracts)
    by_name = {c.name: c for c in contracts}
    bad = [(n, m) for n, ok, m in results if not ok and by_name[n].required]
    degraded = [(n, m) for n, ok, m in results if not ok and not by_name[n].required]

    if verbose:
        for name, ok, msg in results:
            c = by_name[name]
            if ok:
                print(f"  [PASS] {name}: {msg}")
            elif c.required:
                print(f"  [FAIL] {name}: {msg}")
            else:
                print(f"  [DEGR] {name}: {msg}")
                if c.fallback:
                    print(f"         降级方案：{c.fallback}")

    if bad:
        detail = "\n".join(f"  - {n}: {m}" for n, m in bad)
        raise ContractError(f"{len(bad)}/{len(contracts)} 个必需接口不通过，已中止：\n{detail}")
    for c in contracts:
        c._ok = True
    return contracts, degraded


def client_json(raw: str) -> Any:
    import json

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ContractError(f"返回非 JSON：{raw[:120]!r}") from e


def assert_bulk(
    *,
    name: str,
    got: int,
    expected_min: int,
    hint: str = "",
) -> None:
    _need(got >= expected_min, f"{name}：{got} < 预期下限 {expected_min}", hint or "检查停更板块与分页")
