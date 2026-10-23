"""数据源适配层的公共契约。

职责边界（很重要）：
- Provider 只负责「取数 + 归一化」，返回可 JSON 序列化的 RawStock；
- **不做 PIT 过滤、不做 TTM 推导、不做评分**——这些属于 autobuild / engine 的职责。

这样做的原因：取数逻辑依赖网络，几乎无法离线测试；而 PIT 过滤与推导是正确性的核心，
必须能用手写 fixture 反复回归。seam 划在「网络 I/O」与「纯计算」之间。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


class SourceError(Exception):
    """数据源错误。code 由接口层翻译成 HTTP 语义。

    payload 用于「失败但仍有可用中间产物」的场景：例如取数成功、字段也装配好了，
    只是机械外推的 DCF 假设不成立——此时把半成品一并带出去，使用者可以在界面上
    改假设重算，而不是对着一条报错无从下手。payload 只承载数据，不代表分析已成立。
    """

    def __init__(self, message: str, code: str = "SOURCE_ERROR",
                 payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload: Dict[str, Any] = payload or {}


@dataclass(frozen=True)
class StockRef:
    """已解析的标的。secid 为数据源内部标识（如 1.600519）。"""

    code: str
    name: str
    exchange: str
    secid: str
    board: str = ""

    @property
    def display(self) -> str:
        return f"{self.name}（{self.code}.{self.exchange}）"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# RawStock 的结构（全部可 JSON 序列化，便于落 fixture）：
#
# {
#   "stock": {"code","name","exchange","secid","board"},
#   "quote": {"price","trade_date"} | null,
#   "reports": [{"report_date","notice_date","report_type",
#                "revenue","np","eps","ocf","equity","total_assets",
#                "roe","roic","gm","rev_yoy","np_yoy","op_margin","tax_rate"}],
#   "balance": {"report_date","notice_date","monetaryfunds","short_loan","long_loan",
#               "bond_payable","noncurrent_liab_1y","goodwill","total_assets",
#               "total_liab","parent_equity"} | null,
#   "valuation_hist": [{"date","pe","pb","close","mcap","shares"}],
#   "kline": [{"date","close","high","low","volume","amount","turnover"}],
#   "rate_10y": {...} | null,
#   "customer": {"form","top_ratio","five_ratio","period","notice_date","page","...} | null,
#   "meta": {"provider","fetched_at","endpoints":[...]}
# }

RAW_REQUIRED = ("stock", "reports", "valuation_hist")
RAW_OPTIONAL = ("quote", "balance", "kline", "macro", "industry", "pledge", "dividends",
                "free_holders", "peer_val", "rate_10y", "customer", "meta")


def validate_raw(raw: Dict[str, Any]) -> None:
    """结构校验：宁可早失败，也不要把半截数据喂给评分引擎。"""
    if not isinstance(raw, dict):
        raise SourceError("RawStock 必须是 JSON 对象", "BAD_RAW")
    missing = [k for k in RAW_REQUIRED if k not in raw]
    if missing:
        raise SourceError(f"RawStock 缺少键: {missing}", "BAD_RAW")
    for key in ("reports", "valuation_hist"):
        if not isinstance(raw[key], list):
            raise SourceError(f"RawStock.{key} 必须是列表", "BAD_RAW")
    if not isinstance(raw["stock"], dict) or not raw["stock"].get("code"):
        raise SourceError("RawStock.stock 缺少 code", "BAD_RAW")


def names_of(raw: Dict[str, Any]) -> List[str]:
    return sorted(raw.keys())
