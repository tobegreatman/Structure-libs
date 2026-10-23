"""数据源适配层。

东方财富公开接口（A 股为主）+ 两个补源：
- 中债国债收益率曲线（宏观利率）；
- 巨潮资讯网年报原文（客户集中度这类只写在年报正文里的字段）。

新增数据源只需实现 `search(query) -> List[StockRef]` 与
`fetch(ref, analysis_date) -> RawStock`。
"""

from .base import RAW_REQUIRED, SourceError, StockRef, validate_raw
from .chinabond import ChinaBondProvider
from .cninfo import CninfoProvider
from .eastmoney import EastmoneyProvider

__all__ = [
    "SourceError", "StockRef", "validate_raw", "RAW_REQUIRED",
    "EastmoneyProvider", "ChinaBondProvider", "CninfoProvider",
]
