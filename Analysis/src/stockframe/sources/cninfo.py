"""巨潮资讯网：年度报告原文 → 最大客户收入占比。

为什么必须走年报 PDF
--------------------
"最大客户收入占比"的数值来源只有年报正文那张表：

    公司主要销售客户情况
    前五名客户合计销售金额（千元）              165,061,533
    前五名客户合计销售金额占年度销售总额比例          38.96%
    公司前 5大客户资料
    序号  客户名称   销售额（千元）  占年度销售总额比例
    1     第一名      58,159,202        13.73%      ← 要的就是这个
    ...
    合计  --         165,061,533        38.96%

这是证监会《公开发行证券的公司信息披露内容与格式准则第 2 号》的强制项，A 股年报
几乎必有。但它是 PDF 里的排版表格，不是结构化字段：东财数据中心没有客户表
（RPT_F10_*CUSTOMER* 系列全是 9501），新浪 vCI_CustomerSupplier 返回空页，
理杏仁/今日投资要付费令牌。所以唯一不花钱的通路是「公告原文 + 文本解析」。

三个接口全部无需令牌（实测 2026-10 可用）
----------------------------------------
1. 代码 → orgId   POST /new/information/topSearch/detailOfQuery?keyWord=CODE
2. 年报检索       POST /new/hisAnnouncement/query   category=category_ndbg_szsh / _bj
3. PDF 原文       GET  https://static.cninfo.com.cn/{adjunctUrl}
   必须带 Referer；urllib 的默认头会被挡成 HTTP 500，要伪装成完整浏览器请求。

披露形态与处理（实测 10 家 A 股 2025 年报，9 家有效）
----------------------------------------------------
- 标准表（5/9）：单客户占比直接可得 → 观测值。版式高度统一，只差金额单位
  （元/千元/万元）；**占比永远是百分数，不参与单位换算**，躲开了按数值猜单位的老坑。
- 文字段给"第一名 X%"（1/9）：如中芯国际"第一名和前五名客户销售额分别占年度主营
  业务收入的 8.0% 和 35.8%" → 可得，但**分母是主营业务收入**，必须随值标注。
- 只有前五合计（2/9）：如茅台"前五名销售额 1,704,578.78 万元，占销售总额 10.10%"。
  这不是最大客户，**只是上界**，任何"合计÷5"之类的摊派都是编的，一律不填值。
- 无此表（1/9）：银行/保险不适用该披露项，属合理缺失。

两条稳健性设计
--------------
1. **只认第 1 行**。最大客户必然是序号 1，不需要五行齐全。格力 2025 年报的表格
   跨页断裂、只解析到 4 行，5.91% 照样拿得到。
2. **交叉校验**。五行占比之和应等于合计占比（实测 38.96=38.96、5.75≈5.74、
   2.01≈2.00，误差 ≤0.01pp）。这个校验能挡住"把销售额列当占比列"的错位——
   错位的表宁可判定不一致、退回人工，也不许把错的数写进评分。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import SourceError

CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_ORG_URL = "https://www.cninfo.com.cn/new/information/topSearch/detailOfQuery"
CNINFO_STATIC = "https://static.cninfo.com.cn/"
CNINFO_REFERER = "https://www.cninfo.com.cn/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 交叉校验容差（百分点）：五行占比各自四舍五入到两位，累积误差不会超过这个量级
CROSS_CHECK_TOL = 0.06
# 表行区块的搜索窗口：锚点之后这么多字符内找序号 1..5
TABLE_WINDOW = 900
# 缓存的 PDF 上限（份）：年报 2~6MB，留 40 份约 100MB
CACHE_LIMIT = 40

_FORM_TABLE = "table"
_FORM_SENTENCE = "sentence"
_FORM_TOTAL_ONLY = "total_only"


# ---------------------------------------------------------------- 解析结果

@dataclass
class CustomerDisclosure:
    """一份年报里读到的客户集中度披露。"""

    form: str                       # table | sentence | total_only
    top_ratio: Optional[float]      # 最大客户占年度销售总额（%）；total_only 时为 None
    five_ratio: Optional[float]     # 前五名合计占比（%）
    denominator: str                # 分母口径：年度销售总额 / 年度主营业务收入 ...
    top_name: Optional[str] = None  # 年报原文的称呼（"第一名"/"客户一"），多为匿名
    amount_unit: Optional[str] = None   # 金额列单位（元/千元/万元），仅用于说明，不参与换算
    rows: int = 0                   # 成功解析到的客户行数
    cross_check: Optional[Dict[str, Any]] = None
    quote: str = ""                 # 原文片段，供审计

    @property
    def usable(self) -> bool:
        """只有拿到单客户占比才算可用；前五合计不是最大客户。"""
        return self.top_ratio is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 文本工具

def normalize(text: str) -> str:
    """压成单行：表格被 pdftotext 拉平后靠空白分隔列，必须先把空白统一。"""
    t = (text or "").replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", t)


_ANCHOR_TABLE = re.compile(r"公司前\s*5\s*大客户资料|公司前\s*五\s*名客户(?!销售额)|前\s*5\s*大客户资料")
# 金额单位有两种写法：「销售额（千元）」与表头上的「单位：万元 币种：人民币」
_UNIT = re.compile(r"销售额\s*[（(]\s*(元|千元|万元|百万元)\s*[)）]|单位[：:]\s*(元|千元|万元|百万元)")
_TOTAL_ROW = re.compile(r"合计\s*[-—–]{0,3}\s*([\d,]{3,}(?:\.\d+)?)\s+(\d{1,3}\.\d{1,2})")
# 行：序号 | 名称（不含数字、1~20 字） | 金额（含千分位） | 占比（1~2 位小数）
_ROW = re.compile(
    r"(?<![\d.])([1-5])\s+([^\d]{1,20}?)\s+([\d,]{3,}(?:\.\d+)?)\s+(\d{1,3}\.\d{1,2})(?![\d])"
)
_SENT_TOTAL = re.compile(
    r"前五名客户销售额[^。；]{0,50}?占(年度销售总额|年度营业收入|年度主营业务收入|营业收入)"
    r"\s*(\d{1,3}\.\d{1,2})\s*%"
)
_SENT_FIRST = re.compile(
    r"第一名(?:和|与|及)?前五名客户销售额?分别占"
    r"(年度销售总额|年度营业收入|年度主营业务收入|营业收入|主营业务收入)?的?\s*"
    r"(\d{1,3}\.\d{1,2})\s*%\s*(?:和|与|及)\s*(\d{1,3}\.\d{1,2})\s*%"
)


def _parse_table(t: str) -> Optional[CustomerDisclosure]:
    """标准表：序号/客户名称/销售额/占比。"""
    m = _ANCHOR_TABLE.search(t)
    if not m:
        return None
    seg = t[m.end(): m.end() + TABLE_WINDOW]

    unit_m = _UNIT.search(t[max(0, m.start() - 160): m.end() + 200])
    unit = (unit_m.group(1) or unit_m.group(2)) if unit_m else None

    rows: List[Tuple[int, str, str, float]] = []
    for hit in _ROW.finditer(seg):
        no, name, amt, ratio = int(hit.group(1)), hit.group(2), hit.group(3), float(hit.group(4))
        # 序号必须递增且从 1 开始，防止把金额误当成序号
        if no != len(rows) + 1:
            continue
        rows.append((no, name, amt, ratio))
        if len(rows) == 5:
            break
    if not rows:
        return None

    total_m = _TOTAL_ROW.search(seg) or _SENT_TOTAL.search(t)
    total_ratio = None
    if total_m:
        total_ratio = float(total_m.group(2)) if total_m.lastindex and total_m.lastindex >= 2 else None

    ratios = [r[3] for r in rows]
    top = max(ratios)
    top_row = rows[ratios.index(top)]

    check = _cross_check(ratios, total_ratio, len(rows))
    if not check["ok"]:
        # 校验不过就不认这张表：错位的解析比缺失更危险
        return CustomerDisclosure(
            form="table_inconsistent", top_ratio=None, five_ratio=total_ratio,
            denominator="年度销售总额", rows=len(rows), cross_check=check,
            amount_unit=unit, quote=seg[:160],
        )

    return CustomerDisclosure(
        form=_FORM_TABLE,
        top_ratio=top,
        five_ratio=total_ratio if total_ratio is not None else round(sum(ratios), 2),
        denominator="年度销售总额",
        top_name=top_row[1],
        amount_unit=unit,
        rows=len(rows),
        cross_check=check,
        quote=seg[:160],
    )


def _cross_check(ratios: List[float], total: Optional[float], rows: int) -> Dict[str, Any]:
    """占比之和 vs 合计。

    行数不足 5（表格跨页断裂）时不做等值校验——缺的行会让和必然偏小，
    改用"逐行 ≤ 合计、合计 > 0"的弱条件，这样格力那种 4 行的表仍能取出第 1 行。
    """
    s = round(sum(ratios), 2)
    if total is None:
        # 没有合计可比：至少要求最大值不超过 100%（单客户占比的物理上限）
        return {"sum_ratio": s, "total_ratio": None, "rows": rows,
                "ok": all(0 < r <= 100 for r in ratios), "rule": "no_total"}
    if rows >= 5:
        ok = abs(s - total) <= CROSS_CHECK_TOL and all(r <= total + CROSS_CHECK_TOL for r in ratios)
        return {"sum_ratio": s, "total_ratio": total, "rows": rows, "ok": ok, "rule": "sum==total"}
    ok = s <= total + CROSS_CHECK_TOL and all(0 < r <= total + CROSS_CHECK_TOL for r in ratios)
    return {"sum_ratio": s, "total_ratio": total, "rows": rows, "ok": ok, "rule": "partial_rows"}


def _parse_sentence(t: str) -> Optional[CustomerDisclosure]:
    """文字段：给不给单客户覆盖率因公司而异。"""
    first_m = _SENT_FIRST.search(t)
    total_m = _SENT_TOTAL.search(t)
    if not first_m and not total_m:
        return None

    # 分母必须跟着具体那句话走：中芯国际写的是"占年度主营业务收入"，
    # 一律标成"年度销售总额"会把两种不同口径的百分比混为一谈。
    denominator = (
        (first_m.group(1) if first_m and first_m.group(1) else None)
        or (total_m.group(1) if total_m else None)
        or "年度销售总额"
    )
    five = float(total_m.group(2)) if total_m else None
    top = float(first_m.group(2)) if first_m else None
    if first_m and five is None:
        five = float(first_m.group(3))

    if top is not None and five is not None and top > five:
        top = None  # 两个数的关系不自洽，宁可不要

    anchor = first_m or total_m
    start = max(0, anchor.start() - 40)
    return CustomerDisclosure(
        form=_FORM_SENTENCE if top is not None else _FORM_TOTAL_ONLY,
        top_ratio=top,
        five_ratio=five,
        denominator=denominator,
        rows=0,
        quote=t[start: anchor.end() + 60][:200],
    )


def parse_customer_section(page_text: str) -> Optional[CustomerDisclosure]:
    """从一页（或跨页拼接的）年报正文里抽客户集中度。

    先试标准表、再试文字段；两者都有时以表为准（表能自校验，文字段不能）。
    """
    t = normalize(page_text)
    if not t:
        return None
    if not re.search(r"客户", t):
        return None
    table = _parse_table(t)
    if table is not None and table.top_ratio is not None:
        return table
    sentence = _parse_sentence(t)
    if sentence is not None:
        return sentence
    return table  # 表存在但不自洽：带出去，由上层决定是否降级


# ---------------------------------------------------------------- Provider

class CninfoProvider:
    """年报原文补源。http 可注入，PDF 文本抽取可替换，缓存落在本地磁盘。"""

    name = "cninfo"

    def __init__(
        self,
        http_text: Optional[Callable[[str, Optional[bytes]], bytes]] = None,
        pdf_text: Optional[Callable[[bytes], List[str]]] = None,
        cache_dir: Optional[Path] = None,
        timeout: float = 30.0,
        max_cache: int = CACHE_LIMIT,
    ) -> None:
        self._http_text = http_text or (lambda url, body: _request(url, body, timeout))
        self._pdf_text = pdf_text
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".stockframe" / "cninfo"
        self.timeout = timeout
        self.max_cache = max_cache
        self.endpoints: List[str] = []

    # ------------------------------------------------------------ 入口
    def annual_customer(self, code: str, asof: str) -> Optional[Dict[str, Any]]:
        """返回 asof 时点可见的最近一期年报里的客户集中度。

        公告日必须 ≤ asof——年报正文里全是未来数据，偷看一年的代价是 PIT 完全失效。
        """
        try:
            asof_d = date.fromisoformat(str(asof)[:10])
        except ValueError as exc:
            raise SourceError(f"asof 不是合法日期：{asof}", "BAD_ASOF") from exc

        org_id, plate = self._org(code)
        ann = self._latest_annual(code, org_id, plate, asof_d)
        if ann is None:
            return None

        pages = self._pages(ann["adjunctUrl"])
        self.endpoints.append("annual_pdf")

        # 一页页找锚点，逐页解析取第一个能用的：同一份年报里"前五名客户"可能先出现
        # 在财务报表附注（如永续债条款）里，那一页并不是客户集中度表，不能见了锚点就用。
        best: Optional[CustomerDisclosure] = None
        hit_page = None
        for i, text in enumerate(pages):
            if not re.search(r"前\s*5\s*[大]?\s*客户|前五名客户", text):
                continue
            # 表格常被分页切断，把下一页拼上再解析
            merged = text + " " + (pages[i + 1] if i + 1 < len(pages) else "")
            found = parse_customer_section(merged)
            if found is None:
                continue
            if hit_page is None:
                hit_page, best = i + 1, found
            if found.usable:
                hit_page, best = i + 1, found
                break

        if hit_page is None or best is None:
            return {
                "form": "absent", "top_ratio": None, "five_ratio": None,
                "period": ann["period"], "notice_date": ann["notice_date"],
                "title": ann["title"], "page": None, "rows": 0,
                "reason": "年报正文无「前五名客户」披露（银行/保险等不适用，或正文为扫描件无文本层）",
            }

        out = best.to_dict()
        out.update({
            "period": ann["period"],
            "notice_date": ann["notice_date"],
            "title": ann["title"],
            "page": hit_page,
            "url": CNINFO_STATIC + ann["adjunctUrl"],
            "pages_scanned": len(pages),
        })
        return out

    # ------------------------------------------------------------ 检索
    def _org(self, code: str) -> Tuple[str, str]:
        url = f"{CNINFO_ORG_URL}?keyWord={urllib.parse.quote(code)}&maxSecNum=10&maxListNum=10"
        raw = self._http_text(url, b"")
        self.endpoints.append("org_lookup")
        try:
            rows = json.loads(raw.decode("utf-8", "ignore")).get("keyBoardList") or []
        except (ValueError, AttributeError) as exc:
            raise SourceError("巨潮 orgId 查询返回非 JSON（改版或被限流）", "BAD_RESPONSE") from exc
        for row in rows:
            if row.get("code") == code:
                return str(row.get("orgId") or ""), str(row.get("plate") or "szse")
        if not rows:
            raise SourceError(f"巨潮无 {code} 的证券档案", "NOT_FOUND")
        return str(rows[0].get("orgId") or ""), str(rows[0].get("plate") or "szse")

    def _latest_annual(self, code: str, org_id: str, plate: str,
                       asof: date) -> Optional[Dict[str, Any]]:
        """按「公告日 ≤ asof」取最近一期年报全文（排除摘要/英文版/H股公告）。"""
        column = {"szse": "szse", "sse": "sse", "bj": "bj"}.get(plate, "szse")
        category = "category_ndbg_bj" if plate == "bj" else "category_ndbg_szsh"
        body = urllib.parse.urlencode({
            "pageNum": 1, "pageSize": 30, "column": column, "tabName": "fulltext",
            "stock": f"{code},{org_id}", "seDate": "", "searchkey": "", "secid": "",
            "plate": "", "category": category, "trade": "", "isHLtitle": "true",
        }).encode()
        raw = self._http_text(CNINFO_QUERY_URL, body)
        self.endpoints.append("annual_query")
        try:
            anns = json.loads(raw.decode("utf-8", "ignore")).get("announcements") or []
        except (ValueError, AttributeError) as exc:
            raise SourceError("巨潮公告检索返回非 JSON（改版或被限流）", "BAD_RESPONSE") from exc

        best: Optional[Dict[str, Any]] = None
        for a in anns:
            title = str(a.get("announcementTitle") or "")
            if "年度报告" not in title or any(x in title for x in ("摘要", "英文", "H股", "已取消", "更正")):
                continue
            said = str(a.get("announcementTime") or "")
            day = date.fromtimestamp(int(said) / 1000).isoformat() if said.isdigit() else ""
            if not day or day > asof.isoformat():
                continue  # PIT：公告日之后披露的一律不看
            period = _period_of(title, day)
            if period is None:
                continue
            if best is None or period > best["period"]:
                best = {"period": period, "notice_date": day, "title": title,
                        "adjunctUrl": str(a.get("adjunctUrl") or "")}
        if best and not best["adjunctUrl"]:
            return None
        return best

    # ------------------------------------------------------------ PDF
    def _pages(self, adjunct: str) -> List[str]:
        """下载并抽取文本；PDF 与文本各存一份缓存，同一份年报只付一次代价。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = re.sub(r"[^0-9A-Za-z_.-]", "_", Path(adjunct).name or "annual")
        txt_path = self.cache_dir / f"{key}.txt"
        pdf_path = self.cache_dir / f"{key}.pdf"

        if txt_path.exists():
            cached = json.loads(txt_path.read_text(encoding="utf-8"))
            if cached.get("pages"):
                return list(cached["pages"])

        if pdf_path.exists():
            blob = pdf_path.read_bytes()
        else:
            # PDF 走 GET：传 b"" 会让 urllib 改成 POST，静态库回 405
            blob = self._http_text(CNINFO_STATIC + adjunct, None)
            if not blob[:4] == b"%PDF":
                raise SourceError("巨潮静态库返回的不是 PDF（可能被重定向到验证页）", "BAD_PDF")
            pdf_path.write_bytes(blob)

        pages = self._extract(blob)
        if not pages:
            raise SourceError(
                "PDF 无文本层（扫描件），无法解析客户表", "NO_TEXT_LAYER")
        txt_path.write_text(json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8")
        self._trim()
        return pages

    def _extract(self, blob: bytes) -> List[str]:
        if self._pdf_text is not None:
            return self._pdf_text(blob)
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # 不装 pypdf 也能跑主流程，只是这项留空
            raise SourceError(
                "缺 pypdf，无法解析年报 PDF：pip install pypdf", "MISSING_DEP") from exc
        import io

        try:
            reader = PdfReader(io.BytesIO(blob))
        except Exception as exc:  # pypdf 对损坏/加密文件抛的异常类型很杂
            raise SourceError(f"PDF 打开失败：{type(exc).__name__}", "BAD_PDF") from exc
        out: List[str] = []
        for page in reader.pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:
                out.append("")
        return out

    def _trim(self) -> None:
        """缓存按修改时间留最近若干份，避免无限增长。"""
        try:
            files = sorted(self.cache_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[self.max_cache:]:
            for suf in (".pdf", ".txt"):
                p = old.with_suffix(suf)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass


def _period_of(title: str, notice_day: str) -> Optional[str]:
    """从标题里取报告期（"2025年年度报告" → 2025-12-31）。

    标题没写年份时退回「公告日所在年 − 1」——A 股年报在次年上半年披露，这个推定是
    格式准则下的法定节奏；宁可标成推定值，也不要把报告期猜成公告年。
    """
    m = re.search(r"(20\d{2})\s*年", title)
    if m:
        return f"{m.group(1)}-12-31"
    if re.match(r"20\d{2}-\d{2}-\d{2}", notice_day):
        return f"{int(notice_day[:4]) - 1}-12-31"
    return None


def _request(url: str, body: Optional[bytes], timeout: float, retries: int = 2) -> bytes:
    """巨潮对 urllib 的默认头很敏感（直接 500），统一伪装成浏览器；偶发断连有限重试。

    body 为 None 时发 GET（PDF 静态库只接受 GET，POST 回 405），否则发 POST。
    """
    import time

    headers = {
        "User-Agent": UA,
        "Referer": CNINFO_REFERER,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (403, 429):
                raise SourceError(f"巨潮限流（HTTP {exc.code}），稍后重试", "RATE_LIMITED") from exc
            if attempt >= retries:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise SourceError(f"巨潮不可达：{last}", "SOURCE_UNAVAILABLE")
