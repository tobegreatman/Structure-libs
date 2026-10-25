"""本地 Web 控制台：为框架提供可视化操作界面。

定位：**薄壳**。所有计算都走 `engine.run()` 与 `validate_report()`，服务端不复制
任何评分/估值逻辑，界面层只负责呈现与输入编辑。这样"界面看到的分数"必然等于
"CLI 算出的分数"，不会出现两套口径。

用法：
    PYTHONPATH=src python -m stockframe.server            # 默认 127.0.0.1:8770
    PYTHONPATH=src python -m stockframe.server --open     # 顺便打开浏览器

安全边界：只绑定回环地址，不做公网暴露；请求体上限 8MB；不写任何文件。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .autobuild import build as build_input
from .datadict import FIELD_SPECS, InputError
from .engine import run
from .meta import FRAMEWORK_VERSION
from .pit import LookAheadError, ProvenanceError
from .sources import EastmoneyProvider, SourceError, StockRef
from .report import render_markdown
from .validate import validate_report

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"
SAMPLE_INPUT = ROOT / "examples" / "sample_input.json"
MAX_BODY = 8 * 1024 * 1024


class ApiError(Exception):
    """可回传给前端的用户错误（4xx），message 直接展示给使用者。"""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ---------------------------------------------------------------- 计算封装

def analyze(payload: Dict[str, Any]) -> Dict[str, Any]:
    """执行一次分析，返回报告 + Markdown + schema 校验结果。"""
    inp = payload.get("input")
    if not isinstance(inp, dict):
        raise ApiError("请求体需要 {'input': {...}}，且 input 为 JSON 对象")
    report = run(inp)
    ok, errors = validate_report(report)
    return {
        "ok": True,
        "report": report,
        "markdown": render_markdown(report),
        "validation": {"valid": ok, "errors": errors},
    }


_PROVIDER: Any = None


def _provider() -> Any:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = EastmoneyProvider()
    return _PROVIDER


def set_provider(provider: Any) -> None:
    """测试注入用：替换数据源，避免单测依赖网络。"""
    global _PROVIDER
    _PROVIDER = provider


def search_stocks(query: str, limit: int = 8) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise ApiError("请输入股票代码或名称")
    if len(q) > 32:
        raise ApiError("检索词过长（≤32 字符）")
    refs = _provider().search(q, limit)
    return {"ok": True, "query": q, "candidates": [r.to_dict() for r in refs]}


def _resolve(payload: Dict[str, Any]) -> StockRef:
    """支持三种入参：secid 直传、精确代码/名称、模糊检索取首个。"""
    secid = (payload.get("secid") or "").strip()
    query = (payload.get("query") or "").strip()
    if secid and "." in secid:
        # 数据源 secid 格式为「市场.代码」：1.600519 / 0.000001
        market, code = secid.split(".", 1)
        name = (payload.get("name") or code).strip()
        return StockRef(code=code, name=name,
                        exchange={"1": "SH", "0": "SZ"}.get(market, "SZ"), secid=secid)
    if not query:
        raise ApiError("缺少 query（股票代码或名称）或 secid")
    refs = _provider().search(query, 8)
    for ref in refs:
        if ref.code == query or ref.name == query:
            return ref
    return refs[0]


def autofetch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按代码/名称自动取数并装配输入（PIT 版）。不产出报告，由前端决定是否立即分析。"""
    analysis_date = (payload.get("analysis_date") or date.today().isoformat()).strip()
    try:
        asof = date.fromisoformat(analysis_date)
    except ValueError as exc:
        raise ApiError(f"analysis_date 格式应为 YYYY-MM-DD：{analysis_date}") from exc
    if asof > date.today():
        raise ApiError("分析时点不能晚于今天（否则 PIT 失去意义）")

    wacc = float(payload.get("wacc") or 0.09)
    if not (0.04 <= wacc <= 0.20):
        raise ApiError("wacc 应在 4%-20% 之间")

    ref = _resolve(payload)
    raw = _provider().fetch(ref, analysis_date)
    built = build_input(raw, analysis_date, payload.get("overrides") or {}, wacc=wacc)
    return {
        "ok": True,
        "stock": built["stock"],
        "analysis_date": analysis_date,
        "input": built["input"],
        "fill": built["fill"],
        "source_warnings": (raw.get("meta") or {}).get("warnings", []),
    }


def _field_meta() -> Dict[str, Any]:
    # 定性字段必须带锚定量表：档位不是自由评分，每档对应固定分数（(档-1)×2.5），
    # 界面只给"1 档/2 档"而不给含义，等于让用户盲选一个直接改分数的值。
    from .specs import anchors_by_field

    anchors = anchors_by_field()
    return {
        "framework_version": FRAMEWORK_VERSION,
        "today": date.today().isoformat(),
        "fields": [
            {
                "field_id": s.field_id,
                "name": s.name,
                "unit": s.unit,
                "update_days": s.update_days,
                "qualitative": s.qualitative,
                "required": s.required,
                "annual": s.annual,
                "group": s.field_id.split(".")[0],
                **({"anchors": {str(n): d for n, d in anchors[s.field_id].items()}}
                   if s.field_id in anchors else {}),
            }
            for s in FIELD_SPECS
        ],
        "anchors": {k: {str(n): d for n, d in v.items()} for k, v in anchors.items()},
    }


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- HTTP 处理

class Handler(BaseHTTPRequestHandler):
    server_version = "stockframe-ui/" + FRAMEWORK_VERSION
    protocol_version = "HTTP/1.1"

    # ---- 基础输出 ----
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._json(exc.status, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
            return
        if isinstance(exc, SourceError):
            status = {"SOURCE_UNAVAILABLE": 502, "BAD_RESPONSE": 502, "NOT_FOUND": 404}.get(exc.code, 400)
            body: Dict[str, Any] = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
            # 失败但仍有可用半成品（如 DCF 假设不成立）：一并返回，界面可载入后人工修正
            if exc.payload:
                body.update(exc.payload)
                body["partial"] = True
            self._json(status, body)
            return
        known = (InputError, LookAheadError, ProvenanceError, ValueError)
        if isinstance(exc, known):
            self._json(400, {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}})
            return
        self._json(500, {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}})

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/api/health":
                self._json(200, {"ok": True, "version": FRAMEWORK_VERSION, "web_built": (WEB_DIR / "index.html").exists(), "autofetch": True})
            elif path == "/api/meta":
                self._json(200, {"ok": True, **_field_meta()})
            elif path.startswith("/api/search"):
                qs = parse_qs(urlparse(self.path).query)
                self._json(200, search_stocks((qs.get("q") or [""])[0]))
            elif path == "/api/sample":
                self._json(200, {"ok": True, "input": _load_json(SAMPLE_INPUT)})
            elif path == "/api/sample-report":
                p = ROOT / "examples" / "sample_report.json"
                self._json(200, {"ok": True, "report": _load_json(p), "validation": {"valid": True, "errors": []}}) \
                    if p.exists() else self._json(404, {"ok": False, "error": {"code": "not_found", "message": "样例报告不存在"}})
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._json(404, {"ok": False, "error": {"code": "not_found", "message": f"未知路径 {path}"}})
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path not in ("/api/analyze", "/api/fetch"):
                raise ApiError(f"未知接口 {path}", status=404)
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ApiError("请求体为空")
            if length > MAX_BODY:
                raise ApiError("请求体超过 8MB 上限", status=413)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ApiError(f"请求体不是合法 JSON：{exc}") from exc
            if path == "/api/fetch":
                self._json(200, autofetch(payload))
            else:
                self._json(200, analyze(payload))
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    # ---- 静态文件 ----
    def _serve_static(self, rel: str) -> None:
        safe = Path(rel)
        if safe.is_absolute() or ".." in safe.parts:
            raise ApiError("非法路径", status=403)
        target = (WEB_DIR / safe).resolve()
        if WEB_DIR.resolve() not in target.parents:
            raise ApiError("非法路径", status=403)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._serve_file(target, ctype)

    def _serve_file(self, path: Path, ctype: str) -> None:
        if not path.exists():
            raise ApiError(
                "界面文件缺失：先运行 `python tools/build_ui.py` 生成 web/index.html",
                code="ui_not_built", status=500,
            )
        self._send(200, path.read_bytes(), ctype)

    def log_message(self, fmt: str, *args) -> None:  # 安静模式，避免刷屏
        if "--verbose" in sys.argv:
            super().log_message(fmt, *args)


def serve(host: str = "127.0.0.1", port: int = 8770, open_browser: bool = False) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"stockframe 控制台已启动：{url}")
    print(f"  界面文件：{WEB_DIR / 'index.html'}（{'已构建' if (WEB_DIR / 'index.html').exists() else '未构建'}）")
    print("  Ctrl+C 停止")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stockframe.server", description="个股分析框架 · 本地 Web 控制台")
    ap.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅回环）")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--verbose", action="store_true", help="打印访问日志")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[警告] 绑定到非回环地址会把研究数据暴露到局域网，请确认这是你的本意", file=sys.stderr)
    serve(args.host, args.port, args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
