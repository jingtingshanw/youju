#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取适配器。

一个适配器只做一件事：**给定检索词，返回一批清单项 + 一份如实的来源报告**。

四种源形态，对应四类现实
------------------------
1. `GovcnSource`  国家层。`sousuo.www.gov.cn` 有稳定的 JSON 接口，实测可用。
2. `GovSiteSource` 地方层。省市站点绝大多数是大汉版通式 CMS：栏目页服务端渲染，
                 条目形如 `<a href="/art/2026/9/24/art_19214_1613883.html" title="…">`，
                 日期就在 URL 里。这类站能抓，但要按站配栏目地址。
3. `ManualSource` 兜底。抓不到的清单由人整理成 CSV/JSONL 放进来。这不是妥协：
                 省市级规范性文件本来就有大量只挂在本级政府网站上，任何自动抓取
                 都会漏，人补一条是最省的办法。
4. `FlkSource`    已知不可用。国家法律法规数据库是 SPA，后端 `/law-search/` 未公开
                 检索接口（`/law-search/search` 等一批候选端点实测均 404）。
                 与其假装能查，不如显式报 unavailable 并说明补法。

**状态词表**（`SourceReport.status`）——这是这个模块最要紧的设计，
它保证「没抓到」永远不会伪装成「没有」：

    ok           抓到了，且命中主题
    empty        源可访问，扫描了 N 条，但没有该主题的文件
    blocked      页面取到了但解析不出条目（前端渲染 / 规则不匹配）→ 需人工补
    unavailable  已知该源不提供可用的机器接口 → 需人工补
    error        网络或解析失败（有异常原因）
    skipped      本次未参与（如未提供人工清单文件）
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import net
from .model import (CatalogItem, TOPICS, parse_date, parse_doc_no, strip_tags,
                    title_core)

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(__file__).with_name("sites.json")


# ══════════════════════════════════════════════════════════════════════════
# 一、查询与报告
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Query:
    region: str = ""
    topic: str = ""
    keyword: str = ""
    terms: list[str] = field(default_factory=list)      # 送给源端的检索词
    topic_kws: list[str] = field(default_factory=list)  # 判断「是否相关」的词
    max_per_source: int = 200
    pages: int = 2
    loose: bool = False          # True 时摘要命中也算相关（默认只看标题）
    docs_only: bool = True       # 站内全文检索会把新闻也带回来，默认只留文件类标题
    pages_override: int = 0      # >0 时强制所有源翻这么多页（命令行 --pages 指定的）

    def relevant(self, item: CatalogItem) -> bool:
        """相关性判据：主题词落在标题里。

        只看标题是有意的——政策清单的用途是「这份文件是不是讲这件事」，
        正文里顺带提一句主题词的文件混进来，清单就没法用了。
        `--loose` 放开到摘要，用于召回优先的场景。
        """
        if not self.topic_kws:
            return True
        hay = item.title if not self.loose else f"{item.title} {item.summary}"
        return any(k in hay for k in self.topic_kws)


@dataclass
class SourceReport:
    name: str
    label: str
    level: str
    region: str
    status: str = "ok"
    count: int = 0          # 本次收录（且命中主题）的条数
    scanned: int = 0        # 实际扫描过的条目数
    available: int = 0      # 源端自报的可命中总数（源提供时才有，这是「还有多少没抓」的依据）
    reason: str = ""
    ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ══════════════════════════════════════════════════════════════════════════
# 二、国家层：gov.cn 政策文件库
# ══════════════════════════════════════════════════════════════════════════

GOVCN_API = "https://sousuo.www.gov.cn/search-gov/data"
GOVCN_REFERER = "https://sousuo.www.gov.cn/"

# 标题里「发文机关」和正文之间那个「关于」之前，常带一个动词，要剥掉
_ISSUER_VERB = re.compile(r"(印发|发布|批转|转发|同意|批复|印发实施|公布)$")
_ISSUER_RE = re.compile(r"^(.{2,40}?)关于")
_ISSUER_HEAD = re.compile(
    r"^(中共中央|国务院办公厅|国务院|全国人民代表大会常务委员会|"
    r"[\u4e00-\u9fa5]{2,12}(?:部|委|局|署|办|总局|管理局))")


def guess_issuer(title: str) -> str:
    """从标题里猜发文机关。猜不出返回空串——宁可空着，也不编一个。"""
    m = _ISSUER_RE.match(title or "")
    if m:
        cand = _ISSUER_VERB.sub("", m.group(1).strip())
        if 2 <= len(cand) <= 40:
            return cand
    m = _ISSUER_HEAD.match(title or "")
    return m.group(1) if m else ""


def _ms_date(v) -> str:
    """gov.cn 的 ptime 是毫秒时间戳，pubtimeStr 缺席时用它兜。"""
    try:
        n = int(v) / 1000.0
    except (TypeError, ValueError):
        return ""
    if n < 946684800:   # 2000-01-01，明显不合理
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(n))


class GovcnSource:
    """国家层政策文件库。kind = gw（国务院文件）/ bm（部门文件）。"""

    KINDS = {
        "gw": {"t": "zhengcelibrary_gw", "type": "gwyzcwjk",
               "label": "国务院政策文件库"},
        "bm": {"t": "zhengcelibrary_bm", "type": "bwyzcwjk",
               "label": "国务院部门文件库"},
    }

    def __init__(self, kind: str = "gw"):
        self.kind = kind
        meta = self.KINDS[kind]
        self.name = f"govcn_{kind}"
        self.label = meta["label"]
        self.level = "国家"
        self.region = "国家"
        self._q = meta

    def fetch(self, q: Query) -> tuple[list[CatalogItem], SourceReport]:
        t0 = time.perf_counter()
        rep = SourceReport(self.name, self.label, self.level, self.region)
        out: dict[str, CatalogItem] = {}
        seen = 0
        errs: list[str] = []

        terms = q.terms[:3] or [q.topic or q.keyword]
        for term in filter(None, terms):
            for page in range(1, max(1, q.pages) + 1):
                try:
                    d = net.get_json(GOVCN_API, {
                        "t": self._q["t"], "q": term, "p": page, "n": 20,
                        "type": self._q["type"], "sort": "score", "sortType": 1,
                    }, referer=GOVCN_REFERER)
                except net.FetchError as e:
                    errs.append(f"{term} p{page}: {e}")
                    break
                rows = ((d.get("searchVO") or {}).get("listVO") or [])
                if not rows:
                    break
                seen += len(rows)
                for r in rows:
                    title = strip_tags(r.get("title") or "")
                    if not title:
                        continue
                    it = CatalogItem(
                        title=title,
                        issuer=guess_issuer(title) or "",
                        doc_no=(r.get("pcode") or r.get("wenhao") or "").strip(),
                        published=parse_date(r.get("pubtimeStr") or "")
                        or _ms_date(r.get("ptime")),
                        summary=strip_tags(r.get("summary") or ""),
                        url=(r.get("url") or "").strip(),
                        source=self.name,
                        source_type="official_api",
                        source_name=self.label,
                    ).normalize()
                    # 国务院政策/部门文件库里的东西一定是国家层面的。
                    # 有些部门文件的标题以「关于…」开头，标题里没有机关名，
                    # 单靠标题推会推空——这时以来源库为准，比留空可信。
                    if not it.region:
                        it.region, it.region_src = "国家", "来源库"
                    if not q.relevant(it):
                        continue
                    if len(out) >= q.max_per_source:
                        break
                    out.setdefault(it.key, it)
                if len(rows) < 20 or len(out) >= q.max_per_source:
                    break

        rep.scanned = seen
        rep.count = len(out)
        rep.ms = int((time.perf_counter() - t0) * 1000)
        if errs and not out:
            rep.status = "error"
            rep.reason = "；".join(errs[:2])
        elif not out:
            rep.status = "empty"
            rep.reason = f"扫描 {seen} 条，标题里没有主题词的文件"
        else:
            rep.status = "ok"
            if errs:
                rep.reason = f"部分请求失败：{errs[0]}"
        return list(out.values()), rep


# ══════════════════════════════════════════════════════════════════════════
# 三、地方层：大汉版通式 CMS 栏目页
# ══════════════════════════════════════════════════════════════════════════

# 栏目页条目默认形状。抓不到就说明这个站不是这个体例，报 blocked 而不是装作没有。
DEFAULT_LIST_RE = r'<a[^>]+href="([^"]+)"[^>]*\btitle="([^"]{6,140})"'
_ART_URL_RE = re.compile(r"/art/(20\d{2})/(\d{1,2})/(\d{1,2})/")


def _page_path(path: str, n: int) -> str:
    """大汉版通的分页：index.html → index_1.html、index_2.html…"""
    if n <= 1:
        return path
    return re.sub(r"index\.html", f"index_{n}.html", path, count=1)


class GovSiteSource:
    """配置驱动的省市站点适配器。一个站一条配置，不改代码。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.label = cfg.get("label", self.name)
        self.region = cfg.get("region", "")
        self.level = "省级" if cfg.get("region", "").endswith(("省", "自治区")) else "市级"
        self.base = cfg["base"].rstrip("/")
        self.list_re = re.compile(cfg.get("list_re") or DEFAULT_LIST_RE)
        self.max_pages = int(cfg.get("max_pages", 2))
        self.interval = cfg.get("interval")

    def _abs(self, href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return self.base + href
        return f"{self.base}/{href.lstrip('./')}"

    def fetch(self, q: Query) -> tuple[list[CatalogItem], SourceReport]:
        t0 = time.perf_counter()
        rep = SourceReport(self.name, self.label, self.level, self.region)
        out: dict[str, CatalogItem] = {}
        scanned = 0
        parsed_any = False
        errs: list[str] = []
        cols = self.cfg.get("columns") or []

        for col in cols:
            path = col["path"]
            for page in range(1, (q.pages_override or self.max_pages) + 1):
                url = self._abs(_page_path(path, page))
                try:
                    html = net.get(url, referer=self.base + "/",
                                   encoding=self.cfg.get("encoding"),
                                   interval=self.interval)
                except net.FetchError as e:
                    if page == 1:
                        errs.append(f"{col.get('label', path)}: {e}")
                    break
                hits = self.list_re.findall(html)
                if not hits:
                    break
                parsed_any = True
                page_new = 0
                for href, title in hits:
                    title = strip_tags(title)
                    if len(title) < 8:
                        continue
                    full = self._abs(href)
                    # 只认「文章详情页」：URL 里有 /art/YYYY/M/D/ 或域名内带日期。
                    # 这一条同时挡掉了导航链接、外链和栏目标题。
                    m = _ART_URL_RE.search(full)
                    if not m:
                        continue
                    scanned += 1
                    page_new += 1
                    it = CatalogItem(
                        title=title,
                        issuer=guess_issuer(title) or self.label,
                        published=f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-"
                                  f"{int(m.group(3)):02d}",
                        url=full,
                        source=self.name,
                        source_type="official_html",
                        source_name=f"{self.label}·{col.get('label', '')}".rstrip("·"),
                    ).normalize()
                    it.region, it.region_src = _region_of(it, self.region)
                    if not q.relevant(it):
                        continue
                    if len(out) < q.max_per_source:
                        out.setdefault(it.key, it)
                if page_new == 0:
                    break

        rep.scanned = scanned
        rep.count = len(out)
        rep.ms = int((time.perf_counter() - t0) * 1000)
        if not parsed_any:
            rep.status = "blocked" if not errs else "error"
            rep.reason = (errs[0] if errs else
                          "页面可访问，但按当前规则解析不出条目（可能是前端渲染，"
                          "需要为该站单独配 list_re 或改用浏览器抓取）")
        elif not out:
            rep.status = "empty"
            rep.reason = f"栏目共扫 {scanned} 条，标题里没有主题词的文件"
        else:
            rep.status = "ok"
        return list(out.values()), rep


# ══════════════════════════════════════════════════════════════════════════
# 三·五、地方层（更可靠的那条路）：大汉版通站内检索
# ══════════════════════════════════════════════════════════════════════════
#
# 上面那个栏目页适配器只能「按栏目自上而下翻」，命不命中主题全看栏目里有没有。
# 真正好用的一条是站内检索接口：
#
#     GET {base}/jsearchfront/interfaces/cateSearch.do
#         ?websiteid=<站号>&q=<词>&p=<页码>
#     → {"result": ["<div class=jcse-result-box>…<a href=\"visit/link.do?url=…\">标题</a>…"],
#        "total": 276, "cost": 26.0}
#
# 三个要点：
#   · `total` 是**源端自报的命中总数**。有了它，覆盖度报告才能说出
#     「这个站有 276 条相关，我这次取了前 40 条」——「还有多少没抓」变成可数的，
#     而不是靠感觉。这是这个适配器比栏目页那版值钱的地方。
#   · 链接是跳转壳 `visit/link.do?url=<二次编码的真实地址>`，要解出 `url` 参数。
#   · 检索是**全站全文**，会把新闻、工作动态一起带回来，所以要按标题过滤
#     「像不像一份文件」——否则清单里一半是新闻稿。
#
# 这套接口在各省市政务站里很常见（大汉版通），所以一个站配一条 `websiteid`
# 就能接进来，不用改代码。

_DOC_SUFFIX = ("通知", "办法", "意见", "方案", "规定", "决定", "规划", "计划",
               "细则", "指南", "标准", "条例", "规则", "批复", "通告", "公告",
               "要点", "清单", "目录", "措施", "政策", "实施方案", "工作计划",
               # 2026-10-19 审计补：这四类在省市站很常见，少一个就静默丢一批
               "指引", "导则", "规程", "纲要")
_DOC_INLINE = ("关于印发", "关于批转", "关于转发", "的通知", "的实施意见",
               "的实施方案", "的若干意见")


def looks_like_doc(title: str) -> bool:
    """标题像不像一份「文件」。用来把新闻稿从政策清单里剔掉。

    先剥掉末尾括注再判：`…城市更新专项规划（2026-2035年）` 剥完是 `…专项规划`，
    不剥就会因为末尾是 `）` 而被误杀——实测丢了一份真正的规划。

    再补一条转载形态：`《支持城市更新的规划与土地政策指引（2023版）》印发`。
    站点转载时常用「《文件名》印发／发布」当标题，末尾不是文件后缀，
    但书名号已经把它说清楚了。判据要求**同时有书名号 + 印发类动词**，
    单看「印发」会把「第二批城市更新典型案例发布」这类新闻也收进来。
    """
    core = title_core(title)
    if not core:
        return False
    if core.endswith(_DOC_SUFFIX):
        return True
    if "《" in core and core.endswith(("印发", "发布", "公布", "转发", "颁布")):
        return True
    return any(k in core for k in _DOC_INLINE)


# 站内检索的结果可能是「本站转发的外地文件」，所以地域要按标题判，不能一律
# 记成站点所在地。否则一份国务院文件挂在鹰潭站上，就被写成「鹰潭市文件」。
def _region_of(it: CatalogItem, site_region: str) -> tuple[str, str]:
    if it.region_src and it.region_src != "无法判定":
        return it.region, it.region_src
    return site_region, "来源站点"


class JsearchSource:
    """大汉版通站内检索。比栏目页适配器可靠，因为它是全文检索而不是栏目遍历。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.label = cfg.get("label", self.name)
        self.region = cfg.get("region", "")
        self.level = "省级" if self.region.endswith(("省", "自治区")) else "市级"
        self.base = cfg["base"].rstrip("/")
        self.websiteid = str(cfg["websiteid"])
        self.endpoint = cfg.get("endpoint",
                                "/jsearchfront/interfaces/cateSearch.do")
        self.interval = cfg.get("interval")

    def _pages(self, q: Query) -> int:
        """翻页上限。命令行显式给的 `--pages` 优先于站点配置。"""
        if q.pages_override > 0:
            return q.pages_override
        return max(1, int(self.cfg.get("pages") or q.pages))

    def _real_url(self, href: str) -> str:
        """跳转壳 → 真实地址。取不到就返回空串（宁可丢，也不记一个假链接）。"""
        q = urllib.parse.urlparse(href).query if "?" in href else ""
        if "url=" in href:
            # 可能是主查询串，也可能嵌在另一层参数里，两种都试
            for blob in (q, urllib.parse.unquote(q)):
                u = urllib.parse.parse_qs(blob).get("url")
                if u:
                    cand = urllib.parse.unquote(u[0])
                    if cand.startswith("http"):
                        return cand
        return href if href.startswith("http") else ""

    def fetch(self, q: Query) -> tuple[list[CatalogItem], SourceReport]:
        t0 = time.perf_counter()
        rep = SourceReport(self.name, self.label, self.level, self.region)
        out: dict[str, CatalogItem] = {}
        scanned = 0
        total = 0
        errs: list[str] = []

        term = (q.terms[0] if q.terms else "") or q.topic or q.keyword
        if not term:
            rep.status = "skipped"
            rep.reason = "没有检索词"
            return [], rep

        for page in range(1, self._pages(q) + 1):
            try:
                d = net.get_json(self.base + self.endpoint,
                                 {"websiteid": self.websiteid, "q": term, "p": page},
                                 referer=self.base + "/", encoding="utf-8",
                                 interval=self.interval)
            except net.FetchError as e:
                errs.append(f"p{page}: {e}")
                break
            frags = d.get("result") or []
            if not frags:
                break
            try:
                total = max(total, int(d.get("total") or 0))
            except (TypeError, ValueError):
                pass
            for frag in frags:
                scanned += 1
                m = re.search(r'class="jcse-news-title"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>'
                              r'(.*?)</a>', frag, re.S)
                if not m:
                    continue
                href, raw_title = m.group(1), m.group(2)
                title = strip_tags(raw_title)
                if len(title) < 6:
                    continue
                real = self._real_url(href)
                dm = _ART_URL_RE.search(real) if real else None
                it = CatalogItem(
                    title=title,
                    issuer=guess_issuer(title) or "",
                    published=(f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-"
                               f"{int(dm.group(3)):02d}") if dm else "",
                    url=real,
                    summary=strip_tags(",".join(
                        re.findall(r'class="jcse-news-abs-content"[^>]*>(.*?)</div>',
                                   frag, re.S))),
                    source=self.name, source_type="official_html",
                    source_name=self.label,
                ).normalize()
                it.region, it.region_src = _region_of(it, self.region)
                if q.docs_only and not looks_like_doc(it.title):
                    continue
                if not q.relevant(it):
                    continue
                if len(out) < q.max_per_source:
                    out.setdefault(it.key, it)

        rep.scanned = scanned
        rep.available = total
        rep.count = len(out)
        rep.ms = int((time.perf_counter() - t0) * 1000)
        if not out and errs:
            rep.status = "error"
            rep.reason = "；".join(errs[:2])
        elif not out:
            rep.status = "empty"
            rep.reason = (f"源端命中 {total} 条，扫了 {scanned} 条，"
                          f"没有像文件且命中主题的" if total else "源端无命中")
        else:
            rep.status = "ok"
            if total > rep.count:
                rep.reason = f"源端命中 {total} 条，本次收录 {rep.count} 条"
        return list(out.values()), rep


# ══════════════════════════════════════════════════════════════════════════
# 四、兜底：人工清单
# ══════════════════════════════════════════════════════════════════════════

# CSV 表头 → CatalogItem 字段。表头用中文，谁都能在 Excel 里维护。
MANUAL_COLS = {
    "名称": "title", "标题": "title",
    "发文机关": "issuer", "文号": "doc_no", "效力级别": "doc_type",
    "地域": "region", "主题": "topics", "发布日期": "published",
    "施行日期": "effective", "失效日期": "abolished",
    "效力状态": "status", "链接": "url", "备注": "note",
}


class ManualSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg.get("name", "manual")
        self.label = cfg.get("label", "人工清单")
        self.level = "地方"
        self.region = cfg.get("region", "")

    def fetch(self, q: Query) -> tuple[list[CatalogItem], SourceReport]:
        t0 = time.perf_counter()
        rep = SourceReport(self.name, self.label, self.level, self.region)
        path = Path(self.cfg.get("path", "data/catalog/manual.csv"))
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            rep.status = "skipped"
            rep.reason = f"未提供清单文件（{path.name}），跳过"
            rep.ms = int((time.perf_counter() - t0) * 1000)
            return [], rep
        try:
            rows = (self._read_jsonl(path) if path.suffix == ".jsonl"
                    else self._read_csv(path))
        except (OSError, ValueError) as e:
            rep.status = "error"
            rep.reason = f"读取失败：{e}"
            rep.ms = int((time.perf_counter() - t0) * 1000)
            return [], rep

        out: dict[str, CatalogItem] = {}
        for raw in rows:
            title = (raw.get("title") or "").strip()
            if not title:
                continue
            topics = raw.get("topics") or ""
            if isinstance(topics, str):
                topics = [t.strip() for t in re.split(r"[、,，;；|/]", topics) if t.strip()]
            it = CatalogItem(
                title=title,
                issuer=raw.get("issuer") or "",
                doc_no=raw.get("doc_no") or "",
                doc_type=raw.get("doc_type") or "",
                region=raw.get("region") or "",
                topics=topics,
                published=raw.get("published") or "",
                effective=raw.get("effective") or "",
                abolished=raw.get("abolished") or "",
                status=raw.get("status") or "未知",
                url=raw.get("url") or "",
                note=raw.get("note") or "",
                source=self.name, source_type="manual", source_name=self.label,
            ).normalize()
            if it.status != "未知":
                it.status_src = "人工标注"
            if q.relevant(it):
                out.setdefault(it.key, it)

        rep.scanned = len(rows)
        rep.count = len(out)
        rep.ms = int((time.perf_counter() - t0) * 1000)
        if not rows:
            rep.status = "skipped"
            rep.reason = "清单文件只有表头，尚未填内容"
            return [], rep
        rep.status = "ok" if out else "empty"
        if not out:
            rep.reason = f"清单共 {len(rows)} 条，没有命中主题的"
        return list(out.values()), rep

    @staticmethod
    def _read_csv(path: Path) -> list[dict]:
        for enc in ("utf-8-sig", "gb18030"):
            try:
                with path.open(encoding=enc, newline="") as f:
                    out = []
                    for r in csv.DictReader(f):
                        out.append({MANUAL_COLS.get((k or "").strip(), ""): (v or "").strip()
                                    for k, v in r.items() if MANUAL_COLS.get((k or "").strip())})
                    return out
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 编码无法识别（试过 utf-8 / gb18030）")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        out = []
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(json.loads(ln))
        return out


# ══════════════════════════════════════════════════════════════════════════
# 五、已知不可用：国家法律法规数据库
# ══════════════════════════════════════════════════════════════════════════

class FlkSource:
    """`flk.npc.gov.cn` 有接口，但参数与风控没打通。这里显式报 blocked。

    核查记录（2026-10-19 两轮实测）：
      · 第一轮：站点是 Vue SPA，`/law-search/` 下一批候选端点（`/search`、`/list`、
        `/query`）全返回 Spring 的 404；直接 POST `/api/` 被 nginx 拦成 405。
      · 第二轮：下载前端 bundle（1.3 MB）后从中提取出 **17 个真实接口路径**，
        前缀确实是 `/law-search/`：
            law-search/search/list          ← 列表检索，最想要的
            law-search/search/flfgDetails   ← 单部详情（含效力状态）
            law-search/search/hitDisplay / xgzl / xgzlDetails / recommend
            law-search/highSearch/highSearch / hitDisplay / xgzl
            law-search/index/aggregateData / fljc / wjConfig / captchaImage
            law-search/download/batch / pc
            law-search/amazonFile/previewLink
      · `law-search/search/list` 只接受 POST，但用最小参数体打过去返回
        `{"msg":"系统异常","code":500}`——缺的不是方法，是参数结构（很可能是
        加密载荷或前置 token）。同时 `index/captchaImage` 的存在说明站点有风控。
      · 结论：**这不是「没有接口」，是「接口在、没解开」**。状态从 `unavailable`
        改判 `blocked`，因为它现在需要的是人工逆向，而不是换一个源。

    要接进来，三条路：
      ① 官方导出（下载 Excel / PC 下载）后走 `--import`——今天就能用；
      ② 逆向 `search/list` 的参数体（值得投一天，这是全国法规最权威的一份）；
      ③ 带浏览器渲染的适配器（MVP 不引入浏览器依赖，所以没做）。
    """

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.name = cfg.get("name", "flk_npc")
        self.label = cfg.get("label", "国家法律法规数据库")
        self.level = "国家"
        self.region = "国家"
        self.note = cfg.get("note", "")

    def fetch(self, q: Query) -> tuple[list[CatalogItem], SourceReport]:
        rep = SourceReport(self.name, self.label, self.level, self.region)
        rep.status = "blocked"
        rep.reason = self.note or ("检索接口路径已提取，但参数体打不通（返回「系统异常」），"
                                   "且站点带验证码风控；需人工逆向或走官方导出后 --import")
        return [], rep


# ══════════════════════════════════════════════════════════════════════════
# 五·五、把「缺失」也建模成一个源
# ══════════════════════════════════════════════════════════════════════════

class NoteSource:
    """配置里写死的「已知不可用」。用来说明为什么某一级这次没有数据。

    它不返回任何条目，只返回一条状态。存在的意义是：**让缺失可见**。
    没有它，市级抓到了、省级没抓到，报告上只会少一行，用户不会注意到省级是空的。
    """

    def __init__(self, cfg: dict):
        self.name = cfg["name"]
        self.label = cfg.get("label", self.name)
        self.region = cfg.get("region", "")
        self.level = ("省级" if self.region.endswith(("省", "自治区"))
                      else "市级" if self.region else "国家")
        self._status = cfg.get("status", "blocked")
        self._reason = cfg.get("reason", "")

    def fetch(self, q: Query) -> tuple[list[CatalogItem], SourceReport]:
        rep = SourceReport(self.name, self.label, self.level, self.region)
        rep.status = self._status
        rep.reason = self._reason
        return [], rep


# ══════════════════════════════════════════════════════════════════════════
# 六、装配
# ══════════════════════════════════════════════════════════════════════════

BUILDERS = {
    "govcn": lambda c: GovcnSource(c.get("kind", "gw")),
    "govsite": GovSiteSource,
    "jsearch": JsearchSource,
    "manual": ManualSource,
    "flk": FlkSource,
    "note": NoteSource,
}


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {"regions": {}, "sources": []}
    return json.loads(p.read_text(encoding="utf-8"))


def build_sources(cfg: dict | None = None,
                  only: list[str] | None = None) -> list:
    """按配置造适配器。`only` 给名字列表就只造这几个（调试单源用）。"""
    cfg = cfg if cfg is not None else load_config()
    out = []
    for sc in cfg.get("sources", []):
        if only and sc.get("name") not in only:
            continue
        b = BUILDERS.get(sc.get("type", ""))
        if not b:
            continue
        try:
            out.append(b(sc))
        except (KeyError, TypeError):
            continue
    return out


def region_chain(cfg: dict, region: str) -> tuple[list[str], str]:
    """把用户写的「鹰潭」展开成 [鹰潭市, 江西省, 国家] + 认到的规范名。

    展开规则来自配置（`sites.json` 的 regions 段），不做全国行政区划推断——
    猜省份很容易猜错，而配一条是多写一行的事。
    """
    regions = cfg.get("regions", {})
    for canon, meta in regions.items():
        names = {canon, *meta.get("aliases", [])}
        if region in names:
            return list(meta.get("chain") or [canon, "国家"]), canon
    if region:
        return [region, "国家"], region
    return ["国家"], "国家"


def topic_terms(topic: str, keyword: str = "") -> tuple[list[str], list[str]]:
    """返回 (送给源端的检索词, 判断相关性的词表)。"""
    kw = TOPICS.get(topic, [])
    if keyword:
        terms = [keyword]
        kws = list(dict.fromkeys([keyword, *kw])) if kw else [keyword]
        return terms, kws
    if topic:
        # 源端只送主题名本身：多送几个词会把请求数翻倍，而国家库召回已经很宽
        return [topic], list(dict.fromkeys([topic, *kw]))
    return [], []


__all__ = [
    "Query", "SourceReport", "GovcnSource", "GovSiteSource", "JsearchSource",
    "ManualSource", "FlkSource", "NoteSource", "load_config", "build_sources",
    "region_chain", "topic_terms", "guess_issuer", "looks_like_doc", "CONFIG_PATH",
]
