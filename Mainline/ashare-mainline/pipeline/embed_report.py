"""把某一天的报告注入 index.html，生成一份可直接双击打开的成品页。

    python -m pipeline.embed_report --as-of 2026-11-02

与 build_embed.py 的区别：后者把 samples/sample-report.json 当默认数据（会覆盖 index.html），
本模块只读 index.html、另存为 report-<as_of>.html，并把 STORAGE_KEY 换成按日期的键，
避免浏览器里残留的上一份报告通过 localStorage 把当天结论顶掉。
"""
from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

from pipeline import day

ROOT = Path(__file__).resolve().parent.parent


def indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + ln if ln.strip() else ln for ln in text.split("\n"))


def js_obj(o) -> str:
    return indent(json.dumps(o, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    day.add_as_of(ap)
    ap.add_argument("--src", default="")
    args = ap.parse_args()
    args.as_of = day.resolve(args.as_of)

    src = ROOT / (args.src or f"aml-{args.as_of.replace('-', '')}-v16.json")
    report = json.loads(io.open(src, encoding="utf-8").read())
    prompt = io.open(ROOT / "prompt" / "ashare-mainline-prompt.md", encoding="utf-8").read()
    schema = json.loads(io.open(ROOT / "schema" / "ashare-mainline.schema.json", encoding="utf-8").read())
    template = json.loads(io.open(ROOT / "samples" / "empty-template.json", encoding="utf-8").read())

    block = ("const EMBEDDED = {\n"
             "  sample: " + js_obj(report) + ",\n"
             "  schema: " + js_obj(schema) + ",\n"
             "  template: " + js_obj(template) + ",\n"
             "  prompt: " + json.dumps(prompt, ensure_ascii=False).replace("</", "<\\/") + "\n"
             "};")

    html = io.open(ROOT / "index.html", encoding="utf-8").read()
    pat = re.compile(r"const EMBEDDED = \{[\s\S]*?\n\};")
    if not pat.search(html):
        raise SystemExit("未找到 EMBEDDED 块")
    html = pat.sub(lambda m: block, html, count=1)
    html = html.replace("const STORAGE_KEY = 'aml-report-v1';",
                        f"const STORAGE_KEY = 'aml-report-{args.as_of}';")
    dest = ROOT / f"report-{args.as_of}.html"
    io.open(dest, "w", encoding="utf-8", newline="\n").write(html)
    print(f"  -> {dest.name}  {len(html.encode('utf-8')) / 1024:.0f} KB / "
          f"{len(report['mainlines'])} 条主线")


if __name__ == "__main__":
    main()
