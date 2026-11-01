"""把完整报告压成「可直接粘贴」的单行块格式（提示词 §10-D 第 11 条）。

    python -m pipeline.compact --src aml-20261101-v16.json --dst aml-20261101-v16.compact.json

规则：一个顶层键一行，对象内部全部内联（separators 无空格）。
逐字段缩进会把体积膨胀约 40%，这是触发截断的首要原因。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent


def compact(doc: dict) -> str:
    lines = [
        f'  "{k}": ' + json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        for k, v in doc.items()
    ]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="aml-20261101-v16.json")
    ap.add_argument("--dst", default="aml-20261101-v16.compact.json")
    args = ap.parse_args()

    doc = json.loads((ROOT / args.src).read_text(encoding="utf-8"))
    text = compact(doc)
    # 压缩后必须仍能被解析，且与原文语义一致：只改排版，不改内容。
    if json.loads(text) != doc:
        print("压缩后语义与原文不一致，放弃写入")
        return 2
    (ROOT / args.dst).write_text(text, encoding="utf-8")
    print(f"  {args.src} -> {args.dst}：{len(text)} 字符（上限 40000）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
