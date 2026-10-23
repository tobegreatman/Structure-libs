"""构建 Web 控制台：把样例报告快照注入模板，产出可直接双击打开的单文件界面。

为什么要"构建"而不是直接写 HTML：
1. 界面在 `file://` 下无法 fetch 本地 JSON（浏览器 CORS 限制），必须把样例快照内联；
2. 快照必须与 `examples/sample_report.json` 一致，否则界面展示的就是过期结论——
   用脚本生成 + `--check` 比对，从机制上防止"界面漂移"（与 A11 文档漂移检查同思路）。

用法：
    python tools/build_ui.py            # 构建 web/index.html
    python tools/build_ui.py --check    # 只校验是否与样例报告同步（CI 用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from stockframe.datadict import FIELD_SPECS  # noqa: E402
from stockframe.meta import FRAMEWORK_VERSION  # noqa: E402

TEMPLATE = ROOT / "web" / "app.template.html"
OUTPUT = ROOT / "web" / "index.html"
MARKER = "/*__SNAPSHOT__*/"


def _field_meta() -> dict:
    return {
        "framework_version": FRAMEWORK_VERSION,
        "fields": [
            {
                "field_id": s.field_id, "name": s.name, "unit": s.unit,
                "update_days": s.update_days, "qualitative": s.qualitative,
                "required": s.required, "annual": s.annual,
                "group": s.field_id.split(".")[0],
            }
            for s in FIELD_SPECS
        ],
    }


def build() -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in tpl:
        raise SystemExit(f"模板缺少快照占位符 {MARKER}")
    with open(ROOT / "examples" / "sample_report.json", encoding="utf-8") as fh:
        report = json.load(fh)
    with open(ROOT / "examples" / "sample_input.json", encoding="utf-8") as fh:
        sample_input = json.load(fh)
    snap = {"report": report, "input": sample_input, "meta": _field_meta()}
    payload = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    # 防止文本中出现 </script> 提前闭合脚本块
    payload = payload.replace("</", "<\\/")
    return tpl.replace(MARKER, "window.__SNAPSHOT__ = " + payload + ";")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 stockframe Web 控制台")
    ap.add_argument("--check", action="store_true", help="仅校验产物是否与样例报告同步")
    args = ap.parse_args(argv)

    html = build()
    if args.check:
        if not OUTPUT.exists():
            print("[FAIL] web/index.html 不存在，请运行 python tools/build_ui.py", file=sys.stderr)
            return 1
        if OUTPUT.read_text(encoding="utf-8") != html:
            print("[FAIL] web/index.html 与样例报告/模板不同步，请重新运行 python tools/build_ui.py", file=sys.stderr)
            return 1
        print(f"[PASS] web/index.html 已同步（{len(html) / 1024:.0f} KB）")
        return 0

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"已生成 {OUTPUT.relative_to(ROOT)}（{len(html) / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
