"""中债（中央结算公司）国债收益率曲线数据源。

只取一条序列：10 年期国债到期收益率（日频），用于自动计算「10Y 利率同比变化」。
页面入口：https://yield.chinabond.com.cn/cbweb-czb-web/czb/moreInfo?locale=cn_ZH&nameType=1

实际数据接口（页面 highstock 图背后的同一个 Ajax）：
    POST https://yield.chinabond.com.cn/cbweb-czb-web/czb/czbQueryYz
         ?zblx=yz&gjqx=10&startTime=YYYY-MM-DD&endTime=YYYY-MM-DD&locale=cn_ZH&qxmc=1

返回形如
    [{"ycDefId": "...", "ycDefName": "10年", "worktime": "2026-10-22",
      "seriesData": [[1792598400000, 1.6414], ...]}]
时间戳是**北京时间当日 0 点的毫秒值**（不能按 UTC 直接解析，否则整体偏移一天），
收益率单位已是 %（1.6414 即 1.6414%）。

时点纪律：中债曲线是**日终估值**，当日 18:00 后才对外可用，因此把每个数据点的
available_at 记为「数据日 + 1 天」；asof 只允许看 available_at ≤ asof 的点。
这样在盘中做历史复核时不会用到当天尚未发布的曲线。

这里只做「取数 + 对齐一年前锚点」，不做任何插值：找不到落在容差内的一年期锚点
就直接返回 None，由上层降级为缺口，而不是拿半年前的点冒充「一年前」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import SourceError

CHINABOND_YZ_URL = "https://yield.chinabond.com.cn/cbweb-czb-web/czb/czbQueryYz"
CHINABOND_REFERER = (
    "https://yield.chinabond.com.cn/cbweb-czb-web/czb/moreInfo?locale=cn_ZH&nameType=1"
)
CB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 日终估值，当日数据次日才视为可用（保守，宁晚不早）
PUBLISH_LAG_DAYS = 1
# 「一年前」锚点允许的错配天数：节假日/停市导致对不上时，10 天内的最近交易日可接受
YEAR_ANCHOR_TOLERANCE_DAYS = 10
# 抓取窗口：覆盖一年前锚点 + 若干假期冗余
DEFAULT_SPAN_DAYS = 430


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms_to_cn_date(ms: Any) -> Optional[date]:
    """毫秒时间戳 → 北京时间日期。接口给的是北京时间当日 0 点，必须按 +08:00 解析。"""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone(timedelta(hours=8))).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class ChinaBondProvider:
    """中债 10 年期国债收益率。http 可注入，便于离线测试。"""

    name = "chinabond"

    def __init__(self, http: Optional[Callable[[str], Any]] = None, timeout: float = 12.0) -> None:
        self._http = http or (lambda url: _post_json(url, timeout))
        self.timeout = timeout
        self.endpoints: List[str] = []

    def yield_10y(self, asof: str, span_days: int = DEFAULT_SPAN_DAYS) -> Optional[Dict[str, Any]]:
        """返回 asof 时点可见的 10Y 收益率与一年前对照。

        可用条件：latest 与 year_ago 两个锚点都齐备，缺一即返回 None（不猜、不外推）。
        """
        try:
            end = date.fromisoformat(str(asof)[:10])
        except ValueError as exc:
            raise SourceError(f"asof 不是合法日期：{asof}", "BAD_ASOF") from exc
        start = end - timedelta(days=max(span_days, 400))

        url = (
            f"{CHINABOND_YZ_URL}?zblx=yz&gjqx=10"
            f"&startTime={start.isoformat()}&endTime={end.isoformat()}"
            f"&locale=cn_ZH&qxmc=1"
        )
        payload = self._http(url)
        self.endpoints.append("czb_query_yz")

        points = _parse_points(payload)
        if not points:
            return None

        # PIT：只采用「发布日已到」的点
        usable = [(d, v) for d, v in points if d + timedelta(days=PUBLISH_LAG_DAYS) <= end]
        if not usable:
            return None

        d1, v1 = usable[-1]
        anchor = _year_ago(usable, d1)
        if anchor is None:
            return None
        d0, v0 = anchor

        return {
            "curve": "中债国债收益率曲线（10 年）",
            "value": v1,
            "date": d1.isoformat(),
            "available_at": (d1 + timedelta(days=PUBLISH_LAG_DAYS)).isoformat(),
            "prev_value": v0,
            "prev_date": d0.isoformat(),
            "prev_offset_days": (d1 - d0).days,
            "chg_bp": round((v1 - v0) * 100.0, 1),
            "publish_lag_days": PUBLISH_LAG_DAYS,
            "points": len(points),
            "span_days": span_days,
        }


def _parse_points(payload: Any) -> List[Tuple[date, float]]:
    """从接口返回里挑出 10 年期序列，压成 [(日期, 收益率%)]。"""
    if not isinstance(payload, list):
        return []
    series = None
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = str(row.get("ycDefName") or "")
        if "10年" in name or name.strip() in ("10", "10Y"):
            series = row.get("seriesData")
            break
    if series is None:  # 只请求了一条曲线时，直接取第一条
        first = payload[0] if payload and isinstance(payload[0], dict) else None
        series = (first or {}).get("seriesData")
    if not isinstance(series, list):
        return []

    out: List[Tuple[date, float]] = []
    for pair in series:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        day, val = _ms_to_cn_date(pair[0]), _num(pair[1])
        if day is None or val is None or val <= 0:
            continue
        out.append((day, val))
    out.sort(key=lambda x: x[0])
    return out


def _year_ago(points: List[Tuple[date, float]], latest: date) -> Optional[Tuple[date, float]]:
    """取最接近「latest 往前 365 天」的交易日；超出容差则放弃。"""
    target = latest - timedelta(days=365)
    best: Optional[Tuple[int, date, float]] = None
    for day, val in points:
        diff = abs((day - target).days)
        if diff > YEAR_ANCHOR_TOLERANCE_DAYS:
            continue
        if best is None or diff < best[0]:
            best = (diff, day, val)
    return None if best is None else (best[1], best[2])


def _post_json(url: str, timeout: float, retries: int = 2) -> Any:
    """中债接口用 POST（空 body）+ 页面 Referer；偶发断连做有限重试。"""
    import time

    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "User-Agent": CB_UA,
                "Referer": CHINABOND_REFERER,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw)
            if data in (None, [], {}):
                raise SourceError("中债接口返回空数据", "EMPTY_RESPONSE")
            return data
        except SourceError:
            raise
        except ValueError as exc:
            raise SourceError("中债接口返回非 JSON（可能被限流或改版）", "BAD_RESPONSE") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise SourceError(f"中债数据源不可达：{last}", "SOURCE_UNAVAILABLE")
