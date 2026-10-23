"""Web 控制台服务端测试：接口契约 + 错误降级。

关键约束：服务端只是薄壳，`/api/analyze` 的结果必须与 `engine.run()` 完全一致；
任何异常输入都必须转成可读的 4xx，而不是 500 或空响应。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from stockframe.server import Handler

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read()


def _post(url: str, payload):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _sample() -> dict:
    with open(ROOT / "examples" / "sample_input.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_health(base_url):
    status, body = _get(base_url + "/api/health")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_index_served(base_url):
    status, body = _get(base_url + "/")
    assert status == 200
    html = body.decode("utf-8")
    assert "stockframe" in html
    assert "/*__SNAPSHOT__*/" not in html, "占位符未替换：界面未构建"
    assert "window.__SNAPSHOT__ =" in html, "缺少内联样例快照（file:// 模式会白屏）"


def test_sample_endpoint(base_url):
    status, body = _get(base_url + "/api/sample")
    data = json.loads(body)
    assert status == 200 and data["ok"] is True
    assert data["input"]["meta"]["stock"]["code"] == "XX0000.DEMO"


def test_meta_endpoint_has_full_dictionary(base_url):
    status, body = _get(base_url + "/api/meta")
    data = json.loads(body)
    assert status == 200
    assert len(data["fields"]) >= 35
    assert any(f["required"] for f in data["fields"])


def test_analyze_matches_engine(base_url):
    from stockframe.engine import run

    status, data = _post(base_url + "/api/analyze", {"input": _sample()})
    assert status == 200 and data["ok"] is True
    expected = run(_sample())["conclusion"]
    assert data["report"]["conclusion"]["bucket_mid"] == expected["bucket_mid"]
    assert data["report"]["conclusion"]["composite_score_mid"] == expected["composite_score_mid"]
    assert data["validation"]["valid"] is True
    assert data["markdown"].startswith("#")


def test_missing_required_field_degrades(base_url):
    inp = _sample()
    del inp["data"]["market"]["pe_ttm"]
    status, data = _post(base_url + "/api/analyze", {"input": inp})
    assert status == 200, "必填缺失应降级为人工复核，而不是报错中断"
    assert data["report"]["conclusion"]["bucket_mid"] == "人工复核"
    assert data["report"]["valuation"]["synthesis"]["mode"] == "dcf_only"


def test_pit_violation_is_400(base_url):
    inp = _sample()
    inp["data"]["financial"]["roe_ttm"]["available_at"] = "2099-01-01"
    status, data = _post(base_url + "/api/analyze", {"input": inp})
    assert status == 400
    assert data["ok"] is False and data["error"]["code"] == "LookAheadError"


def test_l4_source_rejected(base_url):
    inp = _sample()
    inp["data"]["financial"]["roe_ttm"]["source_tier"] = "L4"
    status, data = _post(base_url + "/api/analyze", {"input": inp})
    assert status == 400
    assert data["error"]["code"] == "ProvenanceError"


def test_bad_json_is_400(base_url):
    status, data = _post(base_url + "/api/analyze", b"{not json")
    assert status == 400
    assert "JSON" in data["error"]["message"]


def test_missing_input_key_is_400(base_url):
    status, data = _post(base_url + "/api/analyze", {"foo": 1})
    assert status == 400
    assert data["error"]["message"]


def test_unknown_route_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/api/nope")
    assert exc.value.code == 404


def test_static_path_traversal_blocked(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/static/../src/stockframe/meta.py")
    assert exc.value.code in (403, 404)


# ---------------------------------------------------------------- 自动取数接口
class _FakeProvider:
    """注入用的假数据源：返回 test_autobuild 的手写 fixture，不访问网络。"""

    name = "fake"

    def search(self, query: str, limit: int = 8):
        from stockframe.sources import StockRef
        if "茅台" in query or query == "600519":
            return [StockRef(code="600519", name="贵州茅台", exchange="SH", secid="1.600519", board="沪A")]
        from stockframe.sources import SourceError
        raise SourceError(f"未找到 A 股标的：{query!r}", "NOT_FOUND")

    def fetch(self, ref, analysis_date: str):
        from test_autobuild import raw_fixture
        raw = raw_fixture()
        if _RAW_SWITCH.get("infeasible"):
            # 净债务远超机械外推的 FCFF 现值 → DCF 不可行
            raw["balance"]["short_loan"] = 2.0e13
        return raw


# 供单个用例临时改写 fixture 的开关（假数据源是全局注入的，用完必须复位）
_RAW_SWITCH: dict = {}


@pytest.fixture(scope="module")
def auto_url():
    from stockframe import server
    server.set_provider(_FakeProvider())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    server.set_provider(None)


def _get_json(url: str):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_search_requires_query(auto_url):
    status, body = _get_json(auto_url + "/api/search?q=")
    assert status == 400
    assert body["error"]["code"] == "bad_request"


def test_search_returns_candidates(auto_url):
    status, data = _get_json(auto_url + "/api/search?q=%E8%8C%85%E5%8F%B0")
    assert status == 200
    assert data["ok"] and data["candidates"][0]["code"] == "600519"


def test_search_not_found_is_404(auto_url):
    status, body = _get_json(auto_url + "/api/search?q=zzzz")
    assert status == 404
    assert body["error"]["code"] == "NOT_FOUND"


def test_fetch_then_analyze_roundtrip(auto_url):
    """自动取数 → 分析，必须能直接跑通，且覆盖率不足时判人工复核。"""
    status, data = _post(auto_url + "/api/fetch", {"query": "600519", "analysis_date": "2026-10-23"})
    assert status == 200 and data["ok"]
    fill = data["fill"]
    assert fill["filled_count"] > 10
    assert fill["as_of"]["latest_report_period"] == "2026-06-30"
    assert all("why" in g and g["fields"] for g in fill["pending"])

    status2, rep = _post(auto_url + "/api/analyze", {"input": data["input"]})
    assert status2 == 200 and rep["ok"] and rep["validation"]["valid"]
    assert rep["report"]["conclusion"]["decision"]["rule_id"] == "R7_COVERAGE"


def test_fetch_future_date_is_400(auto_url):
    status, body = _post(auto_url + "/api/fetch", {"query": "600519", "analysis_date": "2099-01-01"})
    assert status == 400
    assert "晚于今天" in body["error"]["message"]


def test_fetch_bad_date_format_is_400(auto_url):
    status, _ = _post(auto_url + "/api/fetch", {"query": "600519", "analysis_date": "2026/10/23"})
    assert status == 400


def test_fetch_unknown_route_404(auto_url):
    status, _ = _post(auto_url + "/api/nope", {})
    assert status == 404


def test_resolve_parses_market_prefixed_secid():
    """数据源 secid 是「市场.代码」（1.600519），不能反向解析成 code.market。"""
    from stockframe.server import _resolve
    ref = _resolve({"secid": "1.600519", "name": "贵州茅台"})
    assert ref.code == "600519" and ref.exchange == "SH" and ref.secid == "1.600519"
    ref2 = _resolve({"secid": "0.000001", "name": "平安银行"})
    assert ref2.code == "000001" and ref2.exchange == "SZ"


def test_fetch_infeasible_dcf_returns_partial(auto_url):
    """自动假设跑不出正股权价值时：4xx + 可读理由 + 半成品，而不是 500 或死胡同。"""
    _RAW_SWITCH["infeasible"] = True
    try:
        status, data = _post(auto_url + "/api/fetch", {"query": "600519", "analysis_date": "2026-10-23"})
    finally:
        _RAW_SWITCH.pop("infeasible", None)

    assert status == 400
    assert data["ok"] is False and data["error"]["code"] == "DCF_INFEASIBLE"
    assert "调参硬凑" in data["error"]["message"]
    # 关键：半成品必须带出来，否则界面无从改起
    assert data["partial"] is True
    assert data["input"]["data"]["financial"]["roe_ttm"]["value"] > 0
    assert data["fill"]["filled_count"] > 10
    assert data["stock"]["code"] == "000001"
