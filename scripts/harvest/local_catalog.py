#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""省 / 市 / 县三级政府站点的目录抓取。

思路：**不给每个站写适配器，写一个能自己找路的探测器。**
31 个省 + 333 个地级市 + 2,800 个县，逐站手工配栏目是不可能的；但换个角度看，
这些站干的是同一件事——首页上一定有一个「政策文件 / 政府文件 / 规范性文件」
入口，点进去一定是一列标题。所以只要三件事做对，就能批量覆盖：

1. **找入口**：从首页 anchor 里按文字找「政策文件」「政府文件」「规范性文件」
   「法规文件」「政府规章」，取前几个候选。
2. **认条目**：不假设 URL 长什么样。只要求「有标题、标题像一份文件、链接是
   站内详情页」。放弃按 `/art/YYYY/M/D/` 判 URL——浙江的栏目是
   `/col/col…/art/2026/art_<uuid>.html`（没有月日），四川是 TRS 的
   `/10462/c110518/list_ft.shtml`，按旧规则全都解析不出来，而它们都是真的。
3. **翻页**：找页面上文字是「下一页 / 下页 / >」的链接跟过去。比猜
   `index_1.html`、`list_ft_2.shtml` 稳，换模板也不用改代码。

抓不到的站记 `blocked` 并写明原因，不静默跳过——**「这个市没抓到」和「这个市
没有文件」在结果里必须长得不一样**。

用法
----
    python scripts/harvest/local_catalog.py --probe             # 只探测入口，不抓
    python scripts/harvest/local_catalog.py --run --level 省     # 抓省级目录
    python scripts/harvest/local_catalog.py --run --level 市
    python scripts/harvest/local_catalog.py --run --only 江西省,浙江省
    python scripts/harvest/local_catalog.py --stats
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                    # noqa: E402
from catalog.model import CatalogItem, parse_date, strip_tags, title_core  # noqa: E402

DB_PATH = ROOT / "data" / "catalog.db"
SITES_DIR = ROOT / "data" / "catalog"
STATE_PATH = SITES_DIR / "local_sites.json"

# 入口文字分两级。
# 一级：直接就是文件库，点进去是一列文件。
#
# 顺序就是权重。实测 17 个省级站抓不到，逐个看首页 anchor 后发现：入口都在，
# 只是文字不在词表里——福建写「政策文件库」、湖南写「文件库」「规章库」、
# 上海写「政策发布」、吉林写「政策信息」。所以词表必须收全这批同义词。
ENTRY_KWS_1 = ("政策文件库", "文件库", "规章库", "行政规范性文件", "规范性文件",
               "政策文件", "政府规章", "法规文件", "政府文件", "省政府文件",
               "市政府文件", "政策发布", "政策信息", "最新文件", "文件发布",
               "政策法规", "规章文件", "法律法规", "政策服务")
# 「政府公报」单独一档，排在最后。
# 它曾经和上面混在一个词表里，还因为 URL 含 `zfgb` / `gongbao` 额外加分，
# 结果在地市站上把「政策文件库」挤掉了——邢台、铜川、东营、开封、河池、
# 呼和浩特、衢州、韶关都抓到公报页。公报页上是「2026 年第 3 期」这种期号
# 列表，不是文件列表，解析出来全是空的。公报本来也不是文件库，是汇编。
ENTRY_KWS_GB = ("政府公报",)
# 二级：点进去是「政务公开」这种大栏目，文件库在它下面一层。
# 只认一级词的第一版，31 个省只拿下 6 个——上海首页写的是「政策发布」、
# 广东首页只有「政务公开」，都不在词表里。所以必须往下走一层。
ENTRY_KWS_2 = ("政务公开", "信息公开", "政府信息公开", "政策发布", "政策",
               "公开目录", "法定主动公开")

# 入口排除词：这些栏目不是文件本体。放进来会拿到几百条解读稿，
# 让「这个站有多少份文件」这个数直接失真。
_ENTRY_BAD = re.compile(r"(解读|图解|问答|新闻发布会|访谈|专题|征集|视频)")

# 入口 URL 路径里的关键词。政务站的文件库路径有很强的老惯例，
# `/zhengce`、`/zcwj`、`/wjk`、`/zck`、`/fgwj` 这些比 anchor 文字更可靠——
# 文字可以写成任何样子，路径不能。
_SCHEME_KW = ("zhengce", "zcwj", "wjk", "zck", "fgwj", "gfxwj", "guizhang",
              "policy", "zcfg", "szfwj", "zhengcewenjian")

# 首页连入口都没有时，直接试这几个常见路径（政务站的老惯例）
FALLBACK_PATHS = ("/zwgk/", "/zfxxgk/", "/xxgk/", "/zhengce/", "/zcwj/")

# 标题成不了文件的 anchor：导航、栏目名、按钮
_BAD_TITLE = re.compile(
    r"^(首页|更多|查看更多|下一页|上一页|下页|尾页|末页|返回|登录|注册|"
    r"网站地图|联系我们|无障碍|关怀版|简|繁|EN|搜索|高级搜索|"
    r"[\d\s\W]{0,6})$")
_ASSET = re.compile(r"\.(?:css|js|jpe?g|png|gif|svg|ico|bmp|pdf|zip|rar|7z|"
                    r"docx?|xlsx?|pptx?|mp4|mp3|avi|woff2?|ttf|eot)(?:$|\?)", re.I)
_DETAIL_SUFFIX = re.compile(r"\.(?:html?|shtml|jhtml|xhtml)$", re.I)


def _href_ok(u: str) -> bool:
    """这条链接像不像一份文件的详情页。

    原来只认 `.html` / `.shtml` 结尾，于是把所有无后缀的详情页全排除了。
    实测：塔城地区政策文件是 `/zfxxgk/zc/gfxwj/…`（无后缀）、淮南市规范性
    文件是 `/public/column/6596035?catId=…&action=list`（无后缀），
    这些站漏斗在「站内 html」这一步就归零，被判成「入口在但解析不出」。

    改成：先排静态资源，再认「页面后缀」或「路径里带 4 位以上数字 ID」——
    后者是政务站详情页最普遍的形态。
    """
    p = urllib.parse.urlparse(u).path
    if not p or p == "/":
        return False
    if _ASSET.search(p):
        return False
    if _DETAIL_SUFFIX.search(p):
        return True
    return bool(re.search(r"\d{4,}", p))
_NEXT_TEXT = re.compile(r"^(下一页|下页|后一页|Next|&gt;|>|»|›|＞)$")
_IFRAME = re.compile(r"""<iframe[^>]+src\s*=\s*["']([^"']+)["']""", re.I)


def find_next(html: str, base: str, cur: str = "") -> str:
    """找下一页。顺序：文字「下一页」→ 分页区里的数字 +1 → 按模板推。

    第一版只认「文字正好等于下一页」，实测 5 个省翻 10 页一条没多涨——
    因为分页控件的 anchor 文字里常混着图标和空白（`<a>下一页 ></a>`），
    或者根本只有数字。所以补两条退路。
    """
    # 一、文字匹配（宽松：去掉内部空白后判断）
    for href, text in _anchors(html):
        t = re.sub(r"\s+", "", text or "")
        if not t or len(t) > 6:
            continue
        if _NEXT_TEXT.match(t):
            u = _abs(base, href)
            if u.startswith("http") and _same_site(base, u) and u != cur:
                return u

    # 二、分页区的数字链接：当前页 +1
    m = re.search(r"(?:index|list_ft|list|default)[_-](\d+)\.[a-z]+$", cur)
    cur_no = int(m.group(1)) if m else 0
    want = str(cur_no + 1)
    for href, text in _anchors(html):
        if re.sub(r"\s+", "", text or "") != want:
            continue
        u = _abs(base, href)
        if u.startswith("http") and _same_site(base, u) and u != cur:
            return u

    # 三、按模板推（只在大汉版通 / TRS 这两种确定的体例上推，不瞎猜）
    #
    # 这里原来有个自环 bug：`cur_no == 0`（首页 index.html）时返回 index.html
    # 本身，于是「翻下一页」永远翻回同一页。浙江实测 20 页只拿到 38 条，
    # 就是因为 20 次抓的都是首页。首页的下一页一定是 index_2.html。
    if re.search(r"index(?:_\d+)?\.html$", cur):
        return re.sub(r"index(?:_\d+)?\.html$", f"index_{cur_no + 1}.html", cur)
    if re.search(r"list_ft(?:_\d+)?\.shtml$", cur):
        return re.sub(r"list_ft(?:_\d+)?\.shtml$", f"list_ft_{cur_no + 1}.shtml", cur)
    return ""


# ══════════════════════════════════════════════════════════════════════════
# 一、站点清单
# ══════════════════════════════════════════════════════════════════════════

PROVINCES: list[tuple[str, str]] = [
    ("北京市", "https://www.beijing.gov.cn"),
    ("天津市", "https://www.tj.gov.cn"),
    ("河北省", "https://www.hebei.gov.cn"),
    ("山西省", "https://www.shanxi.gov.cn"),
    ("内蒙古自治区", "https://www.nmg.gov.cn"),
    ("辽宁省", "https://www.ln.gov.cn"),
    ("吉林省", "https://www.jl.gov.cn"),
    ("黑龙江省", "https://www.hlj.gov.cn"),
    ("上海市", "https://www.shanghai.gov.cn"),
    ("江苏省", "https://www.jiangsu.gov.cn"),
    ("浙江省", "https://www.zj.gov.cn"),
    ("安徽省", "https://www.ah.gov.cn"),
    ("福建省", "https://www.fj.gov.cn"),
    ("江西省", "https://www.jiangxi.gov.cn"),
    ("山东省", "http://www.shandong.gov.cn"),
    ("河南省", "https://www.henan.gov.cn"),
    ("湖北省", "https://www.hubei.gov.cn"),
    ("湖南省", "https://www.hunan.gov.cn"),
    ("广东省", "https://www.gd.gov.cn"),
    ("广西壮族自治区", "http://www.gxzf.gov.cn"),
    ("海南省", "https://www.hainan.gov.cn"),
    ("重庆市", "https://www.cq.gov.cn"),
    ("四川省", "https://www.sc.gov.cn"),
    ("贵州省", "https://www.guizhou.gov.cn"),
    ("云南省", "https://www.yn.gov.cn"),
    ("西藏自治区", "https://www.xizang.gov.cn"),
    ("陕西省", "https://www.shaanxi.gov.cn"),
    ("甘肃省", "https://www.gansu.gov.cn"),
    ("青海省", "https://www.qinghai.gov.cn"),
    ("宁夏回族自治区", "https://www.nx.gov.cn"),
    ("新疆维吾尔自治区", "https://www.xinjiang.gov.cn"),
]


def load_cities(level: str = "") -> list[tuple[str, str, str]]:
    """市 / 县清单从 `data/catalog/cities.csv` 读（地域, 门户, 所属省, 层级）。

    内置 2,491 个域名容易写错且没人核对，所以放在外部文件里（由
    `city_registry.py` 生成）：有就用，没有就只跑省级，不做假。
    `level` 传「市级」或「县级」可以只取其中一类。
    """
    p = SITES_DIR / "cities.csv"
    if not p.exists():
        return []
    import csv
    out = []
    with p.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = (row.get("门户") or "").strip()
            if not url.startswith("http"):
                continue
            if level and row.get("层级") != level:
                continue
            out.append((row.get("地域", ""), url, row.get("所属省", "")))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 二、解析
# ══════════════════════════════════════════════════════════════════════════

_ANCHOR = re.compile(
    r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_ATTR_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_ATTR_TITLE = re.compile(r"""title\s*=\s*["']([^"']*)["']""", re.I)
_TAG = re.compile(r"<[^>]+>")


def _anchors(html: str) -> list[tuple[str, str]]:
    """(href, 文字)。文字优先取 title 属性，但**空 title 不能吞掉正文**。

    这里原来写的是 `tm.group(1) if tm else inner`，只要 anchor 上有
    `title=""` 就走空分支，正文整条丢掉。实测「绍兴嵊州」的政策文件栏目：
    97 个站内链接，标题长度全部为 0，漏斗在「长度≥8」这一步归零，
    整站被判成「入口在但解析不出」。这种空 title 在很多模板里是常态。
    """
    out = []
    for m in _ANCHOR.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        hm = _ATTR_HREF.search(attrs)
        if not hm:
            continue
        tm = _ATTR_TITLE.search(attrs)
        text = strip_tags(tm.group(1)) if tm else ""
        if not text:
            text = strip_tags(_TAG.sub(" ", inner))
        out.append((hm.group(1), text))
    return out


def _abs(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def _same_site(base: str, url: str) -> bool:
    h1 = urllib.parse.urlparse(base).netloc.replace("www.", "")
    h2 = urllib.parse.urlparse(url).netloc.replace("www.", "")
    return h1 == h2


# 这类标题也以「目录 / 指南 / 标准」结尾，但它们是栏目说明页，不是政策文件。
# 实测市级清单里混进「政府信息公开目录」「市级重大行政决策目录标准」——
# 数量不多，但只要出现过一次，用户翻清单时就会觉得「这库不干净」。
_NOT_A_DOC = re.compile(
    r"(政府信息公开(目录|指南|年报|规定|制度)|信息公开指南|网站地图|"
    r"联系我们|无障碍|办事指南|服务指南|权力清单|责任清单|"
    r"重大行政决策目录|决策事项目录|主动公开基本目录|"
    r"网站工作年度报表|政府网站年度报表)")


def _looks_like_file_title(t: str) -> bool:
    if len(t) < 8 or _BAD_TITLE.match(t):
        return False
    if _NOT_A_DOC.search(t):
        return False
    core = title_core(t)
    if len(core) < 6:
        return False
    # 有文号 / 有书名号 / 以公文后缀结尾 —— 三个信号任一即可
    if re.search(r"〔\s*20\d{2}\s*〕\s*\d+\s*号", t):
        return True
    if "《" in t and "》" in t:
        return True
    return bool(re.search(
        r"(通知|办法|意见|规定|决定|规划|计划|细则|指南|导则|规程|规范|标准|"
        r"条例|规则|批复|通告|公告|要点|清单|目录|措施|纲要|方案|指引)$", core))


def _date_near(html: str, pos: int) -> str:
    """条目后面 200 字内常跟发布日期（列表结构：标题 <td>2026-10-10</td>）。"""
    seg = html[pos:pos + 400]
    m = re.search(r"(20\d{2})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})", seg)
    return parse_date(m.group(0)) if m else ""


def parse_list(html: str, base: str, col_label: str = "") -> list[dict]:
    """从栏目页解析条目。只认「站内 + 像文件」的链接。"""
    out, seen = [], set()
    for m in _ANCHOR.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        hm = _ATTR_HREF.search(attrs)
        if not hm:
            continue
        href = hm.group(1).strip()
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        full = _abs(base, href)
        if not _same_site(base, full):
            continue
        if not _href_ok(full):
            continue
        tm = _ATTR_TITLE.search(attrs)
        text = strip_tags(tm.group(1) if tm else _TAG.sub(" ", inner))
        if not _looks_like_file_title(text):
            continue
        if full in seen:
            continue
        seen.add(full)
        d = _date_near(html, m.end())
        if not d:
            d = parse_date(full)
        out.append({"title": text, "url": full, "published": d,
                    "col": col_label})
    return out


# ══════════════════════════════════════════════════════════════════════════
# 二·五、两条「不翻页也能拿全量」的路
# ══════════════════════════════════════════════════════════════════════════
#
# 静态 anchor 解析只能覆盖「列表直接写在 HTML 里」的站。实测剩下的站分成两类：
#
# **一类：栏目页里嵌着全量 JSON 的地址。** 江西省政府是典型——它的每个栏目
# 都预生成了一份 `/<站点前缀>/<栏目>/<栏目>-articleList.json`，里面是该栏目
# 的全部条目（标题、摘要、发布日期、发文机关、详情页路径），一个都不少。
# 这比翻页稳得多：没有分页状态、没有翻页丢失、一次请求拿全。
# 关键在于**一个栏目页里可能嵌着全站上千个栏目路径**（江西那页 1,289 个），
# 不按路径相关性筛，就会去抓「领导分工」「机构职能」这些非文件栏目。
#
# **二类：栏目下面还有一层子栏目。** 浙江「法规文件」页面上有 tab：政府规章 /
# 政府文件 / 行政规范性文件 / 省政府办公厅 / 各市政府文件，各自是独立栏目
# （col1545734 / col1228964496 / …）。只抓总栏目，默认 tab 只有 38 条规章，
# 看起来像「这个站没东西」，其实是没点进去。

_JSON_HREF = re.compile(r"""["']([^"'\s]{6,300}?\.json)(?:\?[^"'\s]*)?["']""")
_JSON_HINT = re.compile(r"(articlelist|list|data|index|search|result|page)", re.I)

# 子栏目文字：只认这些，避免把「政策解读」「图解」「新闻发布会」收进来——
# 解读稿不是文件本体，混进目录会让「这个库有多少份文件」这个数失真。
_COL_KWS = ("政府规章", "政府文件", "规范性文件", "行政规范性文件", "地方性法规",
            "政策文件", "法规文件", "省政府文件", "省政府办公厅文件",
            "市政府文件", "市政府办公厅文件", "部门文件", "政府办文件",
            "文件发布", "最新文件", "规章")
_COL_BAD = re.compile(r"(解读|图解|问答|新闻发布会|媒体报道|视频|访谈|"
                      r"意见征集|征集|调查|年度报告|公报简介)")


def _path_segs(u: str) -> set[str]:
    """URL 路径切成词段集合，用来判「这个 JSON 是不是这个栏目的」。"""
    p = urllib.parse.urlparse(u).path.lower()
    return {s for s in re.split(r"[/_\-\.]+", p) if len(s) >= 3}


def find_json_sources(html: str, base: str, entry_url: str,
                      limit: int = 3) -> list[str]:
    """从栏目页里发现全量条目 JSON，按与入口路径的相关性排序。

    相关性 = 入口路径与 JSON 路径的公共词段数。江西入口是
    `/jxsrmzf/szfwj/pc/list.html`，`/jxsrmzf/szfwj/szfwj-articleList.json`
    公共段 2 个，其它 1,288 个栏目只有 1 个，自然排在最前。
    """
    segs = _path_segs(entry_url)
    cand: list[tuple[int, str]] = []
    for m in _JSON_HREF.finditer(html):
        p = m.group(1)
        if not p.startswith(("/", "http")):
            continue
        if not _JSON_HINT.search(p):
            continue
        u = _abs(base, p)
        if not _same_site(base, u) or u == entry_url:
            continue
        cand.append((len(segs & _path_segs(u)), u))
    seen, out = set(), []
    for _, u in sorted(cand, key=lambda x: -x[0]):
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def parse_json_list(text: str, base: str, col_label: str = "") -> list[dict]:
    """JSON → 条目。兼容 list[dict] 和 {data|list|rows|result: [...]} 两种外壳。

    江西那份的特殊之处：详情页地址在 `urls` 字段里，而且是个**被转义的 JSON 串**
    （`"{\\"pc\\":\\"/jxsrmzf/…\\"}"`），所以要先二次解析。
    """
    try:
        d = json.loads(text)
    except ValueError:
        return []
    if isinstance(d, dict):
        for k in ("data", "list", "rows", "result", "results", "items",
                  "records", "articles"):
            v = d.get(k)
            if isinstance(v, list):
                d = v
                break
            if isinstance(v, dict):
                for k2 in ("list", "rows", "data", "records", "items"):
                    if isinstance(v.get(k2), list):
                        d = v[k2]
                        break
    if not isinstance(d, list):
        return []

    out, seen = [], set()
    for it in d:
        if not isinstance(it, dict):
            continue
        title = strip_tags(str(it.get("title") or it.get("articleTitle")
                               or it.get("name") or ""))
        if not _looks_like_file_title(title):
            continue

        url = ""
        uu = it.get("urls")
        if isinstance(uu, str):
            try:
                uu = json.loads(uu)
            except ValueError:
                uu = None
        if isinstance(uu, dict):
            url = (uu.get("pc") or uu.get("url") or uu.get("wap") or "")
        url = url or it.get("url") or it.get("link") or it.get("href") or ""
        if not url or not isinstance(url, str):
            continue
        full = _abs(base, url)
        if not full.startswith("http") or full in seen:
            continue
        seen.add(full)

        pub = str(it.get("pubDate") or it.get("publishTime") or it.get("publishDate")
                  or it.get("date") or it.get("createTime") or "")
        meta = it.get("metadata") if isinstance(it.get("metadata"), dict) else {}
        issuer = str(meta.get("Source") or meta.get("source") or "")
        out.append({"title": title, "url": full, "published": parse_date(pub),
                    "col": col_label, "issuer": issuer})
    return out


def expand_columns(html: str, base: str, entry_url: str,
                   limit: int = 4) -> list[tuple[str, str]]:
    """栏目页里的下一层子栏目。返回 [(url, 文字)]。

    只认文字含文件类词、且是站内 .html 的链接；排除解读/图解/征集这类栏目。
    """
    out, seen = [], {entry_url}
    for href, text in _anchors(html):
        t = strip_tags(text)
        if not t or len(t) > 20 or _COL_BAD.search(t):
            continue
        if not any(k in t for k in _COL_KWS):
            continue
        full = _abs(base, href)
        if not full.startswith("http") or full in seen:
            continue
        if not _same_site(base, full) or not _href_ok(full):
            continue
        seen.add(full)
        out.append((full, t))
        if len(out) >= limit:
            break
    return out


def _entry_score(text: str, url: str, kws: tuple[str, ...]) -> int:
    """入口打分。文字命中词表 + URL 命中路径关键词，两者相加。

    为什么不能只看文字：上海的 anchor 文字是「政策发布」、URL 是 `/zhengce/list`；
    山东的「政策文件」是 `/col/col320658/index.html`（纯编号，看不出是什么）。
    只看文字会漏掉前一类，只看 URL 会漏掉后一类。
    """
    if not text or _ENTRY_BAD.search(text):
        return -1
    s = 0
    for i, kw in enumerate(kws):
        if kw in text:
            s = 100 - i
            break
    low = url.lower()
    if any(k in low for k in _SCHEME_KW):
        s += 40
    return s


# ══════════════════════════════════════════════════════════════════════════
# 二·六、POST JSON 接口（少数站，只能一站一配）
# ══════════════════════════════════════════════════════════════════════════
#
# 上海统一政策发布平台是个 Vue 应用：`/zhengce/list` 只有 4KB 应用壳，
# 一条文件都没有。数据在 `POST /gwk/policy/page`，body 是
# `{"pageNum":1,"pageSize":150}`，返回 `data.totalCount = 10,607`。
# 接口地址不是猜的，是从它自己的 bundle `/zhengce/assets/index-*.js` 里挖出来的，
# 里面明确写着 `policyPage:{url:"/gwk/policy/page",method:"post",requestBody:"json"}`。
#
# 这类站没有通用解 —— body 的参数名每个站都不一样，只能试。所以做成表，
# 一站点一条，比在代码里写 if 分支好核对。

POST_APIS: dict[str, dict] = {
    "上海市": {
        "url": "https://www.shanghai.gov.cn/gwk/policy/page",
        "referer": "https://www.shanghai.gov.cn/zhengce/list",
        # 参数名必须是 pageNo。写 pageNum 接口不报错，但每次都返回第 1 页——
        # 实测 pageNum=2 和 pageNum=1 返回同一条，翻页静默失效。
        # 这个是靠「返回体里字段叫 pageNo」反推的，不是文档写的。
        "body": lambda p, n: {"pageNo": p, "pageSize": n},
        "size": 150,
        "path": ("data", "records"),
        "title": "title",
        "date": "publishDate",
        "issuer": "docType",
        "note": "上海统一政策发布平台",
    },
}


def _post_json(url: str, body: dict, referer: str, timeout: int = 40) -> dict:
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/plain, */*",
                 "Accept-Language": "zh-CN,zh;q=0.9",
                 "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36"),
                 "Referer": referer})
    ctx = net._ssl_ctx(True)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "ignore") or "{}")


def fetch_post_api(name: str, cfg: dict, max_pages: int) -> tuple[list[dict], int, int]:
    """顺 POST 接口翻页。返回 (条目, 页数, 源端自报总数)。"""
    items: dict[str, dict] = {}
    pages = 0
    total = 0
    for page in range(1, max_pages + 1):
        try:
            d = _post_json(cfg["url"], cfg["body"](page, cfg["size"]),
                           cfg["referer"])
        except Exception:                                    # noqa: BLE001
            break
        cur = d
        for k in cfg["path"]:
            cur = (cur or {}).get(k) if isinstance(cur, dict) else None
        if not isinstance(cur, list):
            break
        pages += 1
        total = max(total, int(((d.get("data") or {}).get("totalCount")) or 0))
        fresh = 0
        for r in cur:
            if not isinstance(r, dict):
                continue
            title = strip_tags(str(r.get(cfg["title"]) or ""))
            if not title or len(title) < 6:
                continue
            # 这类接口不给详情页地址（link / originUrl 都是空串），
            # 只能拿索引号或 ID 当锚。不编造 URL。
            key = str(r.get("id") or r.get("businessId") or title)
            if key in items:
                continue
            items[key] = {
                "title": title,
                "url": f'{cfg["referer"]}#{key}',
                "published": parse_date(str(r.get(cfg["date"]) or "")),
                "col": cfg.get("note", ""),
                "issuer": str(r.get(cfg["issuer"]) or ""),
                "doc_no": str(r.get("indexNo") or ""),
            }
            fresh += 1
        if fresh == 0:
            break
        if total and pages * cfg["size"] >= total:
            break
    return list(items.values()), pages, total


# ── TRS WAS5 检索服务 ────────────────────────────────────────────────
# 国内政务站里最常见的 CMS。它的检索服务会直接吐 JSON：
#   /was5/web/search?channelid=NNN&templet=docs.jsp&searchword=&page=1&perpage=100
# 返回 {"count": 总数, "pagenum": 总页数, "docs": [...]}，每条带
# title / url / time / **fileno（发文字号）** / puborg / EFFTYPE（效力类型）。
#
# 为什么这条路值得单开：福建政策文件库是 Vue 应用，通用探测器全无效；
# 但它的底层就是 WAS5，`searchword=闽政` 一次就返回 7,571 条正式文件，
# 页码能翻到 500 页以上、perpage 支持 100。这比在 SPA 里一页 20 条、
# 翻到 30 页就静默重置好一个数量级。
#
# channelid 必须给，不给返回的是 HTML 404 而不是 JSON。它不能从首页猜出来，
# 只能从站点自己页面里的 `/was5/web/search?channelid=` 链接上取，所以做成表。
WAS5_SITES: dict[str, dict] = {
    "福建省": {
        "api": "https://www.fujian.gov.cn/was5/web/search",
        "channelid": "291575",            # chnlname「省政府」根频道
        # 文号前缀是最好的分片键：既覆盖全，又天然只命中红头文件。
        # 「闽政」7,571 条、「闽政办」2,846 条、「福建省人民政府令」若干。
        "words": ("闽政", "闽政办", "福建省人民政府令"),
        "note": "福建省政府文件（WAS5）",
    },
}

# 有发文字号的才算红头文件；少数没有文号但标题是规范文件的也留。
_POLICY_TITLE = re.compile(
    r"(办法|规定|意见|通知|决定|批复|方案|规划|细则|条例|公告|通告|令|规则|标准)")

# 但有文号 ≠ 政策。福建第一次跑下来的 6,340 条里混着大量人事任免、表彰、
# 政协提案答复函——它们都有正规文号，可在「政策效力核验」里是纯噪声：
# 拿一条任免通知当依据，比找不到依据更糟。所以这里要反向排除。
_NOISE_TITLE = re.compile(
    r"(任免|免职|任职|职务|表彰|奖励|慰问|贺信|致辞|讲话|讣告|招聘|录用|"
    r"答复|提案|建议办理|公示|名单|讣|唁电|感谢信)")
_NOISE_EXEMPT = re.compile(r"(办法|规定|条例|细则|实施细则|规划|方案|实施意见)")


def fetch_was5(name: str, cfg: dict, max_pages: int,
               perpage: int = 100) -> tuple[list[dict], int, int]:
    """按文号前缀分片取全量。返回 (条目, 请求页数, 源端自报总数)。"""
    items: dict[str, dict] = {}
    pages = 0
    total = 0
    api = cfg["api"]
    for word in cfg["words"]:
        first = 1
        while pages < max_pages:
            q = urllib.parse.urlencode({
                "channelid": cfg["channelid"], "templet": "docs.jsp",
                "searchword": word, "page": first, "perpage": perpage})
            try:
                raw = _get(f"{api}?{q}", cfg.get("referer", api), timeout=40)
                d = json.loads(raw, strict=False)   # 正文里有裸换行，严格模式会炸
            except Exception:                                   # noqa: BLE001
                break
            pages += 1
            cnt = int(d.get("count") or 0)
            total = max(total, cnt)
            docs = [x for x in (d.get("docs") or [])
                    if isinstance(x, dict) and x.get("title") != "文章标题"]
            if not docs:
                break
            for x in docs:
                url = str(x.get("url") or "").strip()
                title = strip_tags(str(x.get("title") or "")).strip()
                if not url.startswith("http") or len(title) < 6:
                    continue
                fileno = strip_tags(str(x.get("fileno") or "")).strip()
                if not fileno and not _POLICY_TITLE.search(title):
                    continue
                if _NOISE_TITLE.search(title) and not _NOISE_EXEMPT.search(title):
                    continue
                items.setdefault(url, {
                    "title": title,
                    "url": url,
                    "published": parse_date(str(x.get("time") or "")),
                    "col": cfg.get("note", ""),
                    "issuer": strip_tags(str(x.get("puborg") or "")).strip(),
                    "doc_no": fileno,
                })
            if cnt and first * perpage >= cnt:
                break
            first += 1
    return list(items.values()), pages, total


def _match_entries(html: str, base: str, kws: tuple[str, ...]) -> list[tuple[str, str]]:
    """在给定页面里，按词表找入口链接。返回 [(url, 文字)]，按相关度排序。"""
    cand: list[tuple[int, str, str]] = []
    for href, text in _anchors(html):
        if not text or len(text) > 24:
            continue
        href = href.strip()
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        if not any(kw in text for kw in kws):
            continue
        full = _abs(base, href)
        if not _same_site(base, full) or not full.startswith("http"):
            continue
        sc = _entry_score(text, full, kws)
        if sc <= 0:
            continue
        cand.append((sc, full, text))
    seen, out = set(), []
    for _, url, text in sorted(cand, key=lambda x: -x[0]):
        if url in seen:
            continue
        seen.add(url)
        out.append((url, text))
    return out


def find_entries(html: str, base: str) -> list[tuple[str, str]]:
    """首页 → 文件栏目候选。只做一级匹配，兼容旧调用。"""
    return _match_entries(html, base, ENTRY_KWS_1)[:3]


# ══════════════════════════════════════════════════════════════════════════
# 三、抓一个站
# ══════════════════════════════════════════════════════════════════════════

_HTTP_FALLBACK = {True: None}

# meta refresh 跳转。老政务站大量在用：
#   <meta http-equiv="Refresh" content="0;URL=./zfl/" />
# 陕西「省政府文件」栏目实测就返回 311 个字符的这种空壳，抓下来什么都解析不出。
# 这不是 SPA，跟过去就是正常列表页。
_META_REDIR = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*"""
    r"""["'][^"']*url\s*=\s*([^"';]+)""", re.I)


def _get_once(url: str, referer: str, timeout: int = 20) -> str:
    """抓一次。https 握手失败时自动退回 http。

    湖南省政府是实测案例：`https://www.hunan.gov.cn` 报
    `SSL: BAD_ECPOINT`（服务端用的曲线 Python/OpenSSL 3 不收），但
    `http://www.hunan.gov.cn` 内容完全一样。不开这条退路，整个省就判死了。
    """
    try:
        return net.get(url, timeout=timeout, retries=1, interval=0.5,
                       referer=referer, insecure=True)
    except net.FetchError as e:
        msg = str(e)
        if url.startswith("https://") and ("SSL" in msg or "BAD_ECPOINT" in msg
                                           or "CERTIFICATE" in msg.upper()):
            _HTTP_FALLBACK[True] = True
            return net.get("http://" + url[len("https://"):], timeout=timeout,
                           retries=1, interval=0.5, referer=referer,
                           insecure=True)
        raise


def _get(url: str, referer: str, timeout: int = 20) -> str:
    """抓页面，自动跟随 meta refresh（最多 3 跳）。

    timeout 默认 20 秒。原来写死 60 秒，是因为江苏的 dataproxy 单页要 19.8 秒；
    但对县级 1,775 个站，一个站超时就是 60 秒，几百个站卡住能把整轮拖到几小时。
    需要长时间等的只有 jpage，那一处单独传 60。
    """
    html = _get_once(url, referer, timeout)
    for _ in range(3):
        if len(html) > 2000:
            break
        m = _META_REDIR.search(html)
        if not m:
            break
        nxt = _abs(url, m.group(1).strip())
        if not nxt.startswith("http") or nxt == url:
            break
        url = nxt
        html = _get_once(url, referer, timeout)
    return html


def discover(base: str) -> tuple[list[tuple[str, str]], str]:
    """三级发现：首页一级入口 → 政务公开下一层 → 常见路径。

    返回 (入口候选, 说明)。说明写「在哪一层找到的」，探测报告里能看出这个站
    的健康度：常在第二三层才找到的站，以后改版了也最先坏。
    """
    try:
        home = _get(base + "/", "")
    except net.FetchError as e:
        return [], f"首页不可达：{e}"

    hit = _match_entries(home, base, ENTRY_KWS_1)
    if hit:
        return hit[:4], "首页"

    lv2 = _match_entries(home, base, ENTRY_KWS_2)
    for url, text in lv2[:3]:
        try:
            page = _get(url, base + "/")
        except net.FetchError:
            continue
        hit = _match_entries(page, base, ENTRY_KWS_1)
        if hit:
            return hit[:4], f"二级（{text}）"

    for p in FALLBACK_PATHS:
        try:
            page = _get(base + p, base + "/")
        except net.FetchError:
            continue
        hit = _match_entries(page, base, ENTRY_KWS_1)
        if hit:
            return hit[:4], f"常规路径 {p}"

    # 最后一档：政府公报。多数站公报页是期号列表，解析不出东西，
    # 但总比直接判「无入口」好——有一批站的公报页就是文件列表。
    hit = _match_entries(home, base, ENTRY_KWS_GB)
    if hit:
        return hit[:2], "政府公报（兜底）"

    return [], "首页与常规路径都没有文件栏目入口"


# ══════════════════════════════════════════════════════════════════════════
# 三·五、jpage 数据源（政务站最普遍的一种动态列表）
# ══════════════════════════════════════════════════════════════════════════
#
# 栏目页里藏着这么一段：
#   <script type="text/xml"><datastore><nextgroup><![CDATA[
#     <a href="/module/web/jpage/dataproxy.jsp?page=1&columnid=84242&unitid=…">
#   ]]></nextgroup></datastore></script>
# 请求它会返回：
#   <datastore><totalrecord>5238</totalrecord><totalpage>53</totalpage>
#     <nextgroup><![CDATA[<a href="./dataproxy.jsp?page=2&…">]]></nextgroup>
#     <recordset><record><![CDATA[ <li><a href="…/art/…" title="…">…
# 这是「宏景 / jpage」那套政务 CMS，江苏、广东、安徽等大量站点在用。
# 它有 totalrecord —— 正好是「这个栏目一共多少份文件」这个最该知道的数。

_JPAGE = re.compile(r"""href=["']([^"']*dataproxy\.jsp\?page=\d+[^"']*)["']""")
_TOTALPAGE = re.compile(r"<totalpage>(\d+)</totalpage>")
_TOTALREC = re.compile(r"<totalrecord>(\d+)</totalrecord>")
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


def jpage_first(html: str, base: str) -> str:
    m = _JPAGE.search(html)
    if not m:
        return ""
    return _abs(base, _html_unescape(m.group(1)))


def _html_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


def fetch_jpage(first: str, base: str, max_pages: int,
                col_label: str = "") -> tuple[list[dict], int, int]:
    """顺 dataproxy 往下翻，返回 (条目, 页数, 源端自报总数)。"""
    items: dict[str, dict] = {}
    total_rec = 0
    total_page = 0
    n = 0
    idle = 0
    for page in range(1, max_pages + 1):
        u = re.sub(r"page=\d+", f"page={page}", first, count=1)
        try:
            x = _get(u, base + "/", timeout=60)
        except net.FetchError:
            break
        n += 1
        m = _TOTALPAGE.search(x)
        if m:
            total_page = int(m.group(1))
        m = _TOTALREC.search(x)
        if m:
            total_rec = int(m.group(1))
        fresh = 0
        for cd in _CDATA.findall(x):
            for it in parse_list(cd, base, col_label):
                if it["url"] not in items:
                    items[it["url"]] = it
                    fresh += 1
        # 连续两页没有新条目就停：dataproxy 在页码越界时常常回第 1 页内容，
        # 不设这条会一直翻到 max_pages，每页还要等 20 秒。
        idle = idle + 1 if fresh == 0 else 0
        if idle >= 2:
            break
        if total_page and page >= total_page:
            break
    return list(items.values()), n, total_rec


def probe_site(name: str, base: str, max_pages: int = 3,
               max_cols: int = 3) -> dict:
    """抓一个站的文件栏目。返回 {status, entries, items, reason, entry, via, route}。

    三条路并联，按「一次能拿多少」排序：
      ① 全量 JSON    一次请求拿完一个栏目（江西式），最省事也最全
      ② jpage        数据源分页、自带 totalrecord（江苏式）
      ③ 静态列表     解析 anchor + 翻页（浙江 / 北京式）
    再展开下一层子栏目（浙江「法规文件」里挂着政府规章 / 政府文件 / 行政规范性
    文件三个 tab，各是独立栏目）。不展开就只有默认 tab 的 38 条，会误判成
    「这个站没东西」——这是第一版最大的漏。
    """
    res = {"name": name, "base": base, "status": "error", "reason": "",
           "entries": [], "items": [], "scanned": 0, "pages": 0, "via": "",
           "available": 0, "route": ""}

    # ⓿ 已知的 POST JSON 接口站，先走这条。翻页上限放到 300，
    # 实际停下来靠接口自报的 totalCount，不靠这个数。
    pcfg = POST_APIS.get(name)
    if pcfg:
        got, pages, total = fetch_post_api(name, pcfg, 300)
        res.update(status="ok" if got else "blocked", route="post-api",
                   pages=pages, available=total, items=got,
                   scanned=len(got), via="POST 接口")
        res["reason"] = (f"源端自报 {total} 条，本次取 {len(got)} 条（POST 接口）"
                         if got else "接口无数据")
        return res

    # ⓿' TRS WAS5 检索服务（福建式）。max_pages 按「请求页数」记，
    # 所以放宽到 200 页 —— 一页 100 条，够把文号前缀全翻完。
    wcfg = WAS5_SITES.get(name)
    if wcfg:
        got, pages, total = fetch_was5(name, wcfg, max(200, max_pages))
        res.update(status="ok" if got else "blocked", route="was5",
                   pages=pages, available=total, items=got,
                   scanned=len(got), via="WAS5 检索服务",
                   entries=[{"url": wcfg["api"], "text": "WAS5"}])
        res["reason"] = (f"WAS5 文号前缀分片取 {len(got)} 条"
                         f"（同频道共 {total} 条）" if got else "WAS5 无数据")
        return res

    entries, via = discover(base)
    res["via"] = via
    res["entries"] = [{"url": u, "text": t} for u, t in entries]
    if not entries:
        res["status"] = "blocked"
        res["reason"] = via
        return res

    items: dict[str, dict] = {}
    ok_pages = 0
    errs: list[str] = []
    budget = max(8, max_pages * 2 + max_cols)   # 请求预算，防单站拖死整轮
    tried = 0
    # 入口试 3 个：实测有一批站的第一个候选是坏链（404），第二个才是真的。
    # 只试 2 个会把这类站判成 error，看起来像「这站不可达」，其实是选错了门。
    queue: list[tuple[str, str]] = [(u, t) for u, t in entries[:3]]
    queued = {u for u, _ in queue}
    n_col = 0
    routes: list[str] = []

    while queue and tried < budget:
        url, label = queue.pop(0)
        try:
            html = _get(url, base + "/")
        except net.FetchError as e:
            errs.append(f"{label}: {e}")
            continue
        tried += 1
        ok_pages += 1

        # ① 子栏目入队
        if n_col < max_cols:
            for cu, ct in expand_columns(html, base, url,
                                         limit=max_cols - n_col):
                if cu in queued:
                    continue
                queued.add(cu)
                queue.append((cu, ct))
                n_col += 1

        # ①·五 iframe 入队。政务站很常见的一种做法：栏目页只是个壳，
        # 真正的一列文件在 iframe 里。山东「省政府及省政府办公厅文件」就是
        # 这样——外层 11 个 anchor 全是导航，列表在 iframe src 指向的页面。
        # 不跟进去，这个站就只能是「入口在但解析不出」。
        for src in _IFRAME.findall(html)[:2]:
            fu = _abs(url, src)
            if not fu.startswith("http") or fu in queued or not _same_site(base, fu):
                continue
            queued.add(fu)
            queue.append((fu, label + "｜子页"))

        # ② jpage 数据源
        jp = jpage_first(html, base)
        if jp:
            got, n, total = fetch_jpage(jp, base, max_pages, label)
            ok_pages += max(0, n - 1)
            tried += max(0, n - 1)
            res["available"] = max(res["available"], total)
            res["scanned"] += len(got)
            for it in got:
                items.setdefault(it["url"], it)
            if got:
                routes.append("jpage")
            continue

        # ③ 全量 JSON
        got_json = 0
        for ju in find_json_sources(html, base, url, limit=3):
            if tried >= budget:
                break
            try:
                txt = _get(ju, base + "/")
            except net.FetchError:
                continue
            tried += 1
            for it in parse_json_list(txt, base, label):
                if it["url"] not in items:
                    items[it["url"]] = it
                    got_json += 1
        if got_json:
            res["scanned"] += got_json
            routes.append("json")
            continue

        # ④ 静态列表 + 翻页
        page, n = url, 1
        while page and n <= max_pages and tried < budget:
            if n > 1:
                try:
                    html = _get(page, base + "/")
                except net.FetchError as e:
                    errs.append(f"{label} p{n}: {e}")
                    break
                ok_pages += 1
                tried += 1
            got = parse_list(html, page, label)
            res["scanned"] += len(got)
            fresh = 0
            for it in got:
                if it["url"] not in items:
                    items[it["url"]] = it
                    fresh += 1
            # 早停：翻到一页全是已见过的条目就收手。
            # 两种真实情况都靠这条兜住：① 分页参数无效、每页都返回首页内容；
            # ② 栏目本来就只有两三页。不加这条，深翻 10 页就是在同一页上
            # 反复请求，市级 426 个站会白跑十几分钟。
            if n > 1 and fresh == 0:
                break
            nxt = find_next(html, page, page)
            if not nxt or nxt == page:
                break
            page = nxt
            n += 1

    res["route"] = "+".join(dict.fromkeys(routes))
    res["pages"] = ok_pages
    res["items"] = list(items.values())

    if not items:
        res["status"] = "blocked" if ok_pages else "error"
        res["reason"] = (f"入口在{via}，抓了 {ok_pages} 页，解析不出条目"
                         if ok_pages else ("；".join(errs[:2]) or "栏目页不可达"))
    else:
        res["status"] = "ok"
        if res["available"]:
            res["reason"] = (f"源端自报 {res['available']} 条，本次取 {len(items)} 条"
                             f"（{res['route']}）")
        else:
            res["reason"] = f"{len(items)} 条（{res['route']}，{ok_pages} 页）"
    return res


# ══════════════════════════════════════════════════════════════════════════
# 四、落库
# ══════════════════════════════════════════════════════════════════════════

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=60)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
    except sqlite3.OperationalError:
        pass
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS local_site (
        name TEXT PRIMARY KEY, base TEXT, level TEXT, region TEXT,
        status TEXT, reason TEXT, entry TEXT, n_items INTEGER DEFAULT 0,
        n_pages INTEGER DEFAULT 0, checked_at REAL, ms INTEGER DEFAULT 0
    )""")
    c.commit()
    return c


def _level_of(name: str) -> str:
    """和 `city_registry.level_of` 保持一致，避免两处判级不一样。"""
    r = name.strip()
    if r.endswith(("省", "自治区")) or r in ("北京市", "天津市", "上海市", "重庆市"):
        return "省级"
    if re.search(r"(区|县|旗)$", r) or "自治县" in r:
        return "县级"
    return "市级"


def save(name: str, base: str, r: dict, ms: int) -> int:
    """把抓到的条目写进 items。**批量直接 INSERT OR IGNORE**，
    不走 Store.upsert 的四路查找——两万条一起走那条路会把书名号 LIKE 全表扫爆，
    实测会卡到几分钟一条。跨源去重留到最后的 `--dedupe` 单独做。
    """
    conn = _conn()
    region = name
    level = _level_of(name)
    rows = []
    for it in r["items"]:
        # 统一在落库这一处挡噪声。人事任免、表彰、提案答复函在各站的列表页里
        # 都长得像政策文件（有文号、有日期、标题也规整），只有靠标题词区分。
        if (_NOISE_TITLE.search(it["title"])
                and not _NOISE_EXEMPT.search(it["title"])):
            continue
        ci = CatalogItem(
            title=it["title"], published=it.get("published", ""),
            issuer=it.get("issuer", ""), doc_no=it.get("doc_no", ""),
            region=region, region_src="来源站点",
            url=it["url"], source=f"site:{urllib.parse.urlparse(base).netloc}",
            source_type="official_html",
            source_name=f"{name}·{it.get('col', '')}".rstrip("·"),
        ).normalize()
        d = ci.to_row()
        d["admin_level"] = level
        d["sources"] = ci.source
        d["first_seen"] = d["last_seen"] = time.time()
        rows.append(d)
    new = 0
    for d in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (key,title,issuer,doc_no,doc_type,"
            "doc_type_src,region,region_src,admin_level,topics,published,"
            "effective,abolished,status,status_src,source,sources,source_type,"
            "source_name,url,summary,note,fetched_at,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["key"], d["title"], d["issuer"], d["doc_no"], d["doc_type"],
             d["doc_type_src"], d["region"], d["region_src"], d["admin_level"],
             d["topics"], d["published"], d["effective"], d["abolished"],
             d["status"], d["status_src"], d["source"], d["sources"],
             d["source_type"], d["source_name"], d["url"], d["summary"],
             d["note"], d["fetched_at"], d["first_seen"], d["last_seen"]))
        new += cur.rowcount
    conn.execute(
        "INSERT INTO local_site (name,base,level,region,status,reason,entry,"
        "n_items,n_pages,checked_at,ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET base=excluded.base,status=excluded.status,"
        "reason=excluded.reason,entry=excluded.entry,n_items=excluded.n_items,"
        "n_pages=excluded.n_pages,checked_at=excluded.checked_at,ms=excluded.ms",
        (name, base, level, region, r["status"], r["reason"],
         json.dumps(r["entries"], ensure_ascii=False), len(r["items"]),
         r["pages"], time.time(), ms))
    conn.commit()
    conn.close()
    return new


def cmd_run(names: list[tuple[str, str, str]], max_pages: int,
            only: list[str] | None = None, probe_only: bool = False,
            workers: int = 4, skip_done: bool = False,
            max_cols: int = 3, retry_failed: bool = False,
            only_ok: bool = False) -> None:
    """并发抓多个站。

    为什么必须并发：jpage 那种数据源单页要 20 秒（江苏的 dataproxy 每页 251KB、
    服务端现场拼 XML）。31 个省顺着跑要十几个小时，并发 4 路（各自不同域名，
    不会互相限速）压到 3 小时以内。入库仍在主线程串行做，避免 SQLite 锁。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    stat = {"ok": 0, "blocked": 0, "error": 0, "items": 0}
    todo = [(n, u, p) for n, u, p in names if not only or n in only]
    if skip_done:
        # 断点续跑：已经有条目录进来的站不再抓一遍。
        # 市级那一轮实测跑到第 5 分钟撞了 database is locked 直接退出，
        # 没有这个开关就得从第 1 个站重来。
        conn0 = _conn()
        done = {r[0] for r in conn0.execute(
            "SELECT name FROM local_site WHERE status='ok' AND n_items>0")}
        conn0.close()
        before = len(todo)
        todo = [x for x in todo if x[0] not in done]
        print(f"跳过已完成 {before - len(todo)} 个站")
    if retry_failed:
        # 只重跑上一轮 blocked / error 的站。修完解析器之后要能定向复验，
        # 否则每改一处都要把 685 个站全部重跑一遍。
        conn0 = _conn()
        bad = {r[0] for r in conn0.execute(
            "SELECT name FROM local_site WHERE status IN ('blocked','error')")}
        conn0.close()
        before = len(todo)
        todo = [x for x in todo if x[0] in bad]
        print(f"只重跑失败站 {len(todo)} 个（筛掉 {before - len(todo)} 个）")
    if only_ok:
        # 只重跑已经抓到条目的站，用来「深翻」：先浅跑一遍探清哪些站能抓，
        # 再对这些站加大页数，比一开始就对全部站深翻省一半时间。
        conn0 = _conn()
        good = {r[0] for r in conn0.execute(
            "SELECT name FROM local_site WHERE status='ok' AND n_items>0")}
        conn0.close()
        before = len(todo)
        todo = [x for x in todo if x[0] in good]
        print(f"只深翻已可抓站 {len(todo)} 个（筛掉 {before - len(todo)} 个）")
    print(f"目标 {len(todo)} 个站，并发 {workers}，每站最多 {max_pages} 页 / "
          f"{max_cols} 个子栏目")

    # 滑动窗口：只保持 workers*2 个在飞。2,491 个站一次性全 submit 出去，
    # 结果堆在 future 里、日志乱序刷屏，而且跑县级时一旦某站卡住，
    # 整个队列都堵在内存里看不见进度。
    from concurrent.futures import FIRST_COMPLETED, wait
    it = iter(todo)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        running = {}

        def submit_one() -> bool:
            try:
                name, base, _prov = next(it)
            except StopIteration:
                return False
            running[ex.submit(probe_site, name, base, max_pages, max_cols)] = (
                name, base, time.time())
            return True

        for _ in range(workers * 2):
            if not submit_one():
                break

        while running:
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for fu in done:
                name, base, t0 = running.pop(fu)
                try:
                    r = fu.result()
                except Exception as e:                   # noqa: BLE001
                    r = {"status": "error", "reason": f"{type(e).__name__}: {e}",
                         "items": [], "entries": [], "pages": 0, "scanned": 0,
                         "via": "", "available": 0, "route": ""}
                ms = int((time.time() - t0) * 1000)
                stat[r["status"]] = stat.get(r["status"], 0) + 1
                n_new = 0 if probe_only else save(name, base, r, ms)
                stat["items"] += len(r["items"])
                state[name] = {**r, "ms": ms, "saved": n_new}
                mark = {"ok": "✔", "blocked": "✗", "error": "!"}.get(r["status"], "?")
                print(f"{mark} {name:12s} {r['status']:8s} 站点 {len(r['items']):5d} 条"
                      f"（新增 {n_new:5d}）{ms / 1000:.0f}s  {r['reason'][:52]}",
                      flush=True)
                submit_one()
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    print(f"\n合计：可抓 {stat['ok']}，解析不到 {stat['blocked']}，"
          f"不可达 {stat['error']}，站点条目 {stat['items']} 条")


def cmd_stats() -> None:
    conn = _conn()
    print("站点状态")
    for r in conn.execute("SELECT status,COUNT(*),SUM(n_items) FROM local_site "
                          "GROUP BY status ORDER BY 2 DESC"):
        print(f"  {r[0]:10s} {r[1]:3d} 个站　{r[2] or 0:6d} 条")
    print("\n按层级（目录库 items）")
    for r in conn.execute("SELECT admin_level,COUNT(*) FROM items "
                          "GROUP BY admin_level ORDER BY 2 DESC"):
        print(f"  {r[0] or '（空）':8s} {r[1]:7d}")
    print("\n省级明细")
    for r in conn.execute("SELECT name,status,n_items,n_pages,reason FROM local_site "
                          "WHERE level='省级' ORDER BY n_items DESC"):
        print(f"  {r[0]:12s} {r[1]:8s} {r[2]:5d} 条 / {r[3]} 页  {r[4][:50]}")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只探测入口，不入库")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--level", default="省",
                    choices=["省", "市", "县", "全部"])
    ap.add_argument("--only", default="")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--cols", type=int, default=3,
                    help="每站最多展开几个子栏目（浙江「法规文件」下有 5 个 tab）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only-ok", action="store_true",
                    help="只重跑已经抓到条目的站（深翻）")
    ap.add_argument("--retry-failed", action="store_true",
                    help="只重跑上一轮 blocked / error 的站")
    ap.add_argument("--skip-done", action="store_true",
                    help="跳过上一轮已经抓到条目的站（断点续跑）")
    a = ap.parse_args()

    if a.stats or not (a.run or a.probe):
        cmd_stats()
        return
    names: list[tuple[str, str, str]] = []
    if a.level in ("省", "全部"):
        names += [(n, u, n) for n, u in PROVINCES]
    if a.level in ("市", "全部"):
        names += load_cities("市级")
    if a.level in ("县", "全部"):
        names += load_cities("县级")
    if not names:
        print("没有可跑的站点")
        return
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    cmd_run(names, a.pages, only=only, probe_only=a.probe, workers=a.workers,
            skip_done=a.skip_done, max_cols=a.cols,
            retry_failed=a.retry_failed, only_ok=a.only_ok)


if __name__ == "__main__":
    main()
