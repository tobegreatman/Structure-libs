"""stockframe: 中线(6-12个月)研究复核为主、短线兼顾的个股分析框架。

设计文档: docs/01_design.md
输出契约: schema/report_schema.json
"""

from .meta import DISCLAIMER, __version__  # noqa: F401


def run(inp):  # 延迟导入，避免循环依赖
    from .engine import run as _run
    return _run(inp)
