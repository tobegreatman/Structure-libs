"""CLI：python -m stockframe.cli <input.json> -o <report.json> [--md out.md] [--dump-scoring]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import run
from .report import render_markdown
from .validate import validate_report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stockframe", description="个股分析框架（中线研究复核）")
    parser.add_argument("input", nargs="?", help="输入 JSON 文件路径")
    parser.add_argument("-o", "--output", help="输出报告 JSON 路径")
    parser.add_argument("--md", help="输出 Markdown 报告路径")
    parser.add_argument("--schema", help="JSON Schema 路径（默认 schema/report_schema.json）")
    parser.add_argument("--dump-scoring", action="store_true", help="导出评分量规表（Markdown）")
    args = parser.parse_args(argv)

    if args.dump_scoring:
        from .specs import render_scoring_tables
        sys.stdout.write(render_scoring_tables() + "\n")
        return 0

    if not args.input:
        parser.error("需要 input 文件或 --dump-scoring")

    with open(args.input, "r", encoding="utf-8") as fh:
        inp = json.load(fh)

    try:
        report = run(inp)
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    ok, errors = validate_report(report, args.schema)
    if not ok:
        print("[校验失败]", file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
        return 3

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"[OK] 报告已写入 {args.output}（schema 校验通过）")
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
        print(f"[OK] Markdown 已写入 {args.md}")
    if not args.output and not args.md:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
