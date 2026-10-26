#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""联网检索：把「库外」的文件现抓进来，走的仍是同一套核验。

## 为什么不能直接把搜索结果喂给模型

这个系统全部价值都在「每句话能回原文核对」。搜索结果页给的是标题 + 两行摘要，
摘要本身就是二手转述，模型再转述一遍，核验时无从比对——你没法拿一句话去核验
一句摘要。那样做出来的东西比没有更危险：形式上带引号带引注，实质上是三段转述。

所以联网只做一件事：**把文件本体抓下来**。正文到手之后，切块、装配、回原文核对，
和库内文件走完全相同的路径。区别只有一个——它没进库，这次运行结束就没了，
界面上必须标出来。

## 两条通道，先精确后兜底

**通道一：城市政策库接口。** 各市的「规范性文件统一发布平台」大多有 JSON 检索接口，
返回字段里带发文字号、发布机关、施行时间，**还有源端自己的时效标记**
（`file_status`：有效 / 失效 / 失效（提前废止））。这个标记比我们从正文反推的
台账硬得多——台账是「全库没有一份文件提到它被废止」，源端标记是「发布机关说它失效了」。

实测广州：`https://www.gz.gov.cn/gz_gfxwjk/gfxwj/search` 返回 6,989 份，可按标题搜、
可分页翻全量。这个通道又快又准，命中率接近 100%。

**通道二：搜索引擎。** 没适配的城市走这条。踩过的坑写在这里：

- `site:` 限定**不管用**。实测搜 `site:www.gz.gov.cn 城市更新`，必应返回的前两条是
  国务院文件，一条广州的都没有；`site:` 是「倾向」不是「约束」。
- 搜狗直接反爬（返回 antispider 页），百度裸请求返回 1.4KB 拒绝页，只有 360 能出结果。
- 所以搜索引擎这一层只能**不带 site:，用「城市名 + 主题词」**，再在本地按域名硬筛。
  会漏，但不会错。漏掉的部分由「把城市抓进库」来解决，不是靠联网硬撑。

## 失败要说话

每个方向返回一份 `report`：走哪条通道、搜到几条、域名筛掉几条、正文抓到几条、
各自失败原因。空结果必须能回答「是没搜到，还是搜到了抓不下来」。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                  # noqa: E402
from harvest.extract import extract as extract_body      # noqa: E402
from harvest.build_index import split_blocks             # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CITIES_CSV = ROOT / "data" / "catalog" / "cities.csv"

# 搜索结果里混进来的非文件页：新闻、解读、问答、征集、图解、栏目页。
_NOISE = re.compile(
    r"一图读懂|政策解读|解读|新闻发布会|图解|问答|访谈|评论|答记者问|"
    r"在线访谈|意见征集|结果反馈|媒体报道|转载|视频|直播|专题访谈|"
    r"政府信息公开指南|政府信息公开年报|网站地图|联系我们|无障碍|"
    r"百度百科|知乎|旅游|攻略|景点")

_DOC_WORDS = ("条例", "办法", "规定", "意见", "通知", "决定", "规划", "方案",
              "细则", "规则", "批复", "公告", "通告", "导则", "指引", "标准",
              "纲要", "计划", "措施", "政策", "清单", "目录", "规程", "决定")

# 纯形态词：只说了「这是哪一类文件」，没说「管什么事」。
#
# 这套判据与 `gather._WEAK_Q` 是同一套逻辑，只是那边作用于 SQL 命中、
# 这边作用于联网召回。不抽成公共模块是因为一遍遍改的都是这一张表，
# 合在一起反而要跨文件同步；两边注释互指，改的时候一起看。
#
# 不加这个会怎样：实测省级方向用了「行动方案」这个词，召回全是
# 《广东省遏制与防治艾滋病行动方案》《广东省加快推进人工智能…行动方案》——
# 标题里确实有「行动方案」，但它和城市更新毫无关系。
_FORM_ONLY = re.compile(
    r"^(?:保护|管理|实施|试行|暂行|若干|有关|相关|进一步|加强|推进|行动|工作|"
    r"建设|发展|调整|优化|促进|提升|完善|规范|指导|编制)*"
    r"(?:条例|办法|规定|规则|细则|法|规划|通知|意见|决定|方案|政策|文件|措施)$")


def _relevance(title: str, queries: list[str]) -> tuple[float, list[str]]:
    """标题与检索词的相关度。返回 (系数, 命中的实质词)。

    系数 1.0 = 命中了实质词；0.45 = 只命中形态词（还留着，但压到后面）；
    0 = 不相关。
    """
    hit = [q for q in queries if q and q in title]
    if not hit:
        return 0.0, []
    strong = [q for q in hit if not _FORM_ONLY.match(q)]
    if strong:
        return 1.0, strong
    return 0.45, []

_MAX_BODY_CHARS = 60000
_TIMEOUT = 12
_BUDGET = 30          # 单方向联网总预算（秒）

# 正文缓存。
#
# 不是优化，是必需品。实测：同一个站连续请求几轮之后开始限流——
# 接口响应从 0.6 秒掉到 6.6 秒，再压下去就只剩超时。用户连续问两个
# 关于广州的问题，第二个会莫名其妙地「抓不到」。
#
# 有缓存之后：第一次慢（几十秒），同一个文件第二次起是毫秒级。
# 这也让「联网」这套东西的实际使用成本随时间下降，而不是每次都从零开始。
CACHE_DIR = ROOT / "data" / "webcache"
_CACHE_TTL = 86400 * 7          # 七天。政策文件不是新闻，一周内容易不会变。

# ══════════════════════════════════════════════════════════════════
# 一、城市政策库适配器
# ══════════════════════════════════════════════════════════════════
# 一项配置 = 一个城市的「规范性文件统一发布平台」检索接口。
# 加一个城市只加一条配置，不改逻辑。
#
# 广州这条是怎么找到的：首页 anchor 里 `规范性文件统一发布` 指向 `/gfxwj/`，
# 打开是一个 JS 渲染的空壳，但内联脚本里写着
#   var url = "https://www.gz.gov.cn/gz_gfxwjk/gfxwj/search?limit=10&..."
# 返回 JSONP（`/**/jsonpCallback({...})`），剥掉壳就是 JSON。
# 这套「gz_gfxwjk」是广东省统一平台的实例，省内其他市可能是 `{缩写}_gfxwjk`，
# 值得逐个试——试错的成本只有一次 HTTP。
CITY_LIBS: dict[str, dict] = {
    "广州市": {
        "name": "广州市行政规范性文件统一发布平台",
        "api": "https://www.gz.gov.cn/gz_gfxwjk/gfxwj/search",
        "referer": "https://www.gz.gov.cn/gfxwj/",
        "limit": 20,
        "host": "www.gz.gov.cn",
        # 字段映射。改城市时只改这张表。
        "f": {"id": "id", "title": "title", "url": "post_url",
              "ts": "display_publish_time", "ext": "json_ext"},
        # json_ext 里的键全部带 `_gz_zcwjk_` 前缀，不是裸的 file_status。
        # 写错名字不会报错——`.get()` 一律返回 None，表现是「时效标记 0 份、
        # 文号全空」，看起来像源站没给，实际是自己没取到。
        "ext": {"status": "_gz_zcwjk_gzgfxwj_file_status",
                "issuer": "_gz_zcwjk_issuing_agency",
                "unified": "_gz_zcwjk_gzgfxwj_unified_number",
                "end": "_gz_zcwjk_gzgfxwj_expiration_time",
                "start": "_gz_zcwjk_gzgfxwj_implementation_time"},
        # 文号在源端被拆成三段：字轨 + 年份 + 序号。
        # 单独一个 num 字段拼不出「番发改规字〔2026〕3号」。
        "doc_no_parts": ("_gz_zcwjk_file_type", "_gz_zcwjk_file_year",
                         "_gz_zcwjk_file_num"),
        # file_status 的三态映射。广州用的是「有效 / 失效 / 失效（提前废止）」，
        # 别的市可能写「现行有效 / 已废止 / 已修改」，所以只认关键词不认全串。
        "dead": ("失效", "废止", "无效", "已修改"),
        # 这个库覆盖哪些取证方向。
        # 广州市这个库是「行政规范性文件统一发布平台」，只收规范性文件；
        # 保护规划、专项规划、控规这些不以规范性文件形式发布，搜它一定空。
        # 不声明的话，规划方向会白等 40 秒然后返回 0 条——用户以为工具坏了。
        "kinds": ("reg_local", "policy_local", "case", "standard"),
    },
}

# 省名 → 该省政策库（暂缺，留位）
PROV_LIBS: dict[str, dict] = {}


def _jsonp(s: str) -> dict:
    """剥 JSONP 壳。有的站直接返回 JSON，两种都吃。"""
    t = s.strip()
    if t.startswith("{") or t.startswith("["):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
    i, j = t.find("("), t.rfind(")")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i + 1:j])
        except json.JSONDecodeError:
            pass
    return {}


def _ts(v) -> str:
    """时间戳 → YYYY-MM-DD。秒和毫秒都吃，字符串日期直接过。"""
    if v in (None, "", 0):
        return ""
    if isinstance(v, str):
        m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", v)
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n > 1e11:                       # 毫秒
        n /= 1000
    if n < 9e8:                        # 1970 年前后，无效值
        return ""
    return _dt.datetime.fromtimestamp(n).strftime("%Y-%m-%d")


def _doc_no(ext: dict, cfg: dict) -> str:
    """文号。源端常把它拆成「字轨＋年份＋序号」三段，得拼回去。

    广州的 json_ext 里只有 `file_type="番发改规字"`、`file_year=2026`、
    `file_num=3`，没有任何一个字段单独装着「番发改规字〔2026〕3号」。
    只认一个 doc_no 字段的话，整库文号全空——文号是本项目判「同一条规定
    的两次发布」的主要去重键，空掉会让重复条目翻倍。
    """
    parts = cfg.get("doc_no_parts")
    if parts:
        v = [str(ext.get(k) or "").strip() for k in parts]
        if all(v):
            return f"{v[0]}〔{v[1]}〕{v[2]}号"
    return str(ext.get(cfg["ext"].get("doc_no") or "") or "")


# 已知拿不到的城市门户。不是「没适配」，是站点自己挡住了。
#
# 这类站点必须在用户问起的第一时间说清。现在如果不写这张表，问合肥会先探
# 十几秒栏目、再抓一轮正文，最后返回 0 条 + 「两条通道都没有结果」——
# 用户看到的是「合肥没有相关政策」，而真相是「我们看不到」。
# 一个做合规判断的系统，把「看不到」说成「没有」，是最不该犯的错。
BLOCKED_SITES: dict[str, str] = {
    "合肥市": "合肥市政府门户全站启用加速乐（__jsl_clearance）JS 校验："
              "第一层 cookie 可从页面脚本算出，第二层是 _0x 数组混淆脚本，"
              "需要完整的浏览器环境才能执行。绕过它属于持续对抗，源站一改就失效，"
              "且不稳定，所以不做。合肥市国土空间总体规划及市级政策目前只能离线入库。",
}


def _blocked_of(name: str) -> str:
    if not name:
        return ""
    for k, v in BLOCKED_SITES.items():
        if k == name or k.rstrip("市") == name.rstrip("市"):
            return v
    return ""


def _lib_of(city: str) -> tuple[dict, str]:
    """城市名 → (适配器配置, 命中的键)。支持「广州」这种简写。"""
    if not city:
        return {}, ""
    for k, v in CITY_LIBS.items():
        if k == city or k.startswith(city.rstrip("市")) or city.rstrip("市") == k.rstrip("市"):
            return v, k
    return {}, ""


def query_city_lib(city: str, queries: list[str], cap: int = 40,
                   pages: int = 1) -> tuple[list[dict], dict]:
    """通道一：按标题关键词查城市政策库。"""
    cfg, key = _lib_of(city)
    rep = {"channel": "city_lib", "lib": cfg.get("name", ""), "n_total": 0,
           "n_hit": 0, "error": "", "ms": 0}
    if not cfg:
        rep["error"] = f"{city or '本地'}没有适配的城市政策库接口"
        return [], rep
    t0 = time.time()
    f, ef = cfg["f"], cfg["ext"]
    seen: dict[str, dict] = {}

    for q in queries[:2]:
        for page in range(1, pages + 1):
            u = (f"{cfg['api']}?limit={cfg['limit']}&time_sort_field=display_publish_time"
                 f"&sort=time&page={page}&title={urllib.parse.quote(q)}")
            try:
                # 8 秒。这个接口正常 0.6 秒返回，10 秒还没响应说明站被压住了，
                # 再等下去只是让用户多站 20 秒（timeout × retries）。
                text = _http(u, timeout=8, referer=cfg["referer"])
            except Exception as e:                          # noqa: BLE001
                rep["error"] = f"{type(e).__name__}: {str(e)[:70]}"
                continue
            d = _jsonp(text)
            data = (d or {}).get("data") or {}
            if not isinstance(data, dict):
                continue
            rep["n_total"] = max(rep["n_total"], int(data.get("total") or 0))
            docs = data.get("docs") or data.get("list") or []
            if not docs:
                break
            for it in docs:
                url = it.get(f["url"]) or ""
                title = (it.get(f["title"]) or "").strip()
                if not url or not title:
                    continue
                ext = it.get(f["ext"]) or {}
                if isinstance(ext, str):
                    try:
                        ext = json.loads(ext)
                    except json.JSONDecodeError:
                        ext = {}
                # 同 city_lib.py：json_ext 实测有 [] 的情况（政策解读类条目）。
                # 不兜住就是 AttributeError，整个方向直接判失败。
                if not isinstance(ext, dict):
                    ext = {}
                st = str(ext.get(ef["status"]) or "")
                dead = any(w in st for w in cfg["dead"]) if st else None
                seen.setdefault(url, {
                    "title": title, "url": url,
                    "doc_no": _doc_no(ext, cfg),
                    "issuer": str(ext.get(ef["issuer"]) or ""),
                    "published": _ts(it.get(f["ts"])),
                    "eff_hint": ("repealed" if dead else "ok") if dead is not None else "",
                    "eff_state": st,
                    "eff_end": _ts(ext.get(ef["end"])),
                    "unified": str(ext.get(ef.get("unified") or "") or ""),
                    "query": q,
                })
    rep["n_hit"] = len(seen)
    rep["ms"] = int((time.time() - t0) * 1000)
    if not seen:
        rep["error"] = rep["error"] or f"「{cfg['name']}」里没有标题命中 {queries[:3]} 的文件"
        return [], rep

    # 相关性：接口只保证「标题含这个词」，不保证这个词有实义。
    # 「城市更新办法」的搜索会连带把「城市更新安置房管理办法」也带回来——
    # 那是同一部办法的配套件，有价值；但「行动方案」带回来的
    # 「艾滋病防治行动方案」就是纯噪声了。
    ranked = []
    for it in seen.values():
        w, strong = _relevance(it["title"], queries)
        if w <= 0:
            continue
        it["score"] = round(min(0.97, (it.get("score") or 0.5) * w + 0.06 * len(strong)), 3)
        it["hits"] = strong or [q for q in queries if q in it["title"]]
        ranked.append(it)
    if not ranked:
        rep["error"] = (f"「{cfg['name']}」命中 {len(seen)} 条，但没有一条标题里含"
                        f"实质词——检索词可能只剩「条例」「方案」这类形态词")
        return [], rep
    ranked.sort(key=lambda x: (1 if x.get("eff_hint") == "repealed" else 0,
                               -x["score"]))
    return ranked[:cap], rep


# ══════════════════════════════════════════════════════════════════
# 二、站点栏目探测（没有政策库接口的城市走这条）
# ══════════════════════════════════════════════════════════════════
# 这里砍掉了一条本来想做的路：接公开搜索引擎。
#
# 实测四家全不成立——必应无视 `site:`（搜广州返回国务院文件）、搜狗直接反爬、
# 百度裸请求返回 1.4KB 拒绝页、360 能出结果但链接全是加密跳转壳，解不出真实 URL。
# 更根本的问题是即使拿到链接，中文搜索对「政策文件」的召回质量也很差，
# 首屏常是百科、旅游攻略、新闻转载。
#
# 所以退回到最笨也最稳的那条：直接去该市政府门户，按「政策文件 / 规范性文件 /
# 政府规章」这些栏目名找到列表页，从上往下读标题。这套逻辑已经离线跑了
# 2,491 个站（`scripts/harvest/local_catalog.py`），覆盖率和失败原因都是实测过的。
# 它给的每一条都是「这个站上真有一份叫这个名字的文件」，不是搜索猜的。
def probe_site_city(city: str, queries: list[str], cap: int = 20) -> tuple[list[dict], dict]:
    """通道二：现探目标市政府门户的文件栏目，按检索词筛标题。"""
    t0 = time.time()
    rep = {"channel": "probe", "site": "", "n_scan": 0, "n_doc": 0,
           "error": "", "ms": 0, "status": ""}
    dom = domain_of(city)
    if not dom:
        rep["error"] = f"不知道「{city}」的政府门户域名（cities.csv 里没有）"
        rep["ms"] = int((time.time() - t0) * 1000)
        return [], rep
    rep["site"] = dom
    # 先探首页：3 秒不通就直接判失败。有的市站有 JS challenge（合肥返回 521
    # 加一段算 cookie 的脚本）或整站挂掉，直接进栏目探测会卡满所有超时。
    try:
        _http("https://" + dom + "/", timeout=4)
    except Exception as e:                              # noqa: BLE001
        rep["error"] = (f"「{dom}」首页取不到：{type(e).__name__}。"
                        f"该站可能有反爬（JS 校验）或需要先离线抓入库")
        rep["ms"] = int((time.time() - t0) * 1000)
        return [], rep
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "harvest"))
        from harvest import local_catalog as lc        # noqa: E402
        # 只翻 1 页、只展开 1 个栏目：运行时探测要控在 20 秒内，
        # 全量抓取是离线任务该干的事，不该让用户等。
        res = lc.probe_site(city, "https://" + dom, max_pages=1, max_cols=1)
    except Exception as e:                              # noqa: BLE001
        rep["error"] = f"探测失败：{type(e).__name__}: {str(e)[:70]}"
        rep["ms"] = int((time.time() - t0) * 1000)
        return [], rep

    rep["status"] = res.get("status") or ""
    items = res.get("items") or []
    rep["n_scan"] = len(items)
    if not items:
        rep["error"] = (f"「{dom}」的文件栏目探不出来："
                        f"{(res.get('reason') or '未知原因')[:90]}")
        rep["ms"] = int((time.time() - t0) * 1000)
        return [], rep

    qs = [q for q in queries if len(q) >= 2]
    hit = []
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        if not _looks_like_doc(title):
            continue
        w, strong = _relevance(title, qs)
        if w <= 0:
            continue
        hit.append({
            "title": title, "url": it.get("url") or "",
            "published": (it.get("published") or "")[:10],
            "doc_no": it.get("doc_no") or "", "issuer": it.get("issuer") or "",
            "score": round(min(0.90, (0.52 + 0.10 * len(strong)) * w), 3),
            "query": (strong or [q for q in qs if q in title])[0],
        })
    rep["n_doc"] = len(hit)
    rep["ms"] = int((time.time() - t0) * 1000)
    if not hit:
        rep["error"] = (f"「{dom}」栏目翻了 {rep['n_scan']} 条，没有一条标题含"
                        f" {qs[:3]} —— 该站可能把政策放在别的栏目，或需要先离线抓入库")
    hit.sort(key=lambda x: -x["score"])
    return hit[:cap], rep


def _decode(raw: bytes, enc: str) -> str:
    if enc:
        try:
            return raw.decode(enc, "ignore")
        except LookupError:
            pass
    for e in ("utf-8", "gb18030"):
        try:
            return raw.decode(e)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _http(url: str, timeout: int = _TIMEOUT, referer: str = "") -> str:
    """统一的取页入口，复用 `scripts/catalog/net`。

    自己写 urllib 试过一版，在 www.gz.gov.cn 上稳定挂死 30 秒。原因不是站慢：
    这个站的证书配置老旧，走默认 SSL 上下文会卡在旧版重协商上，`net.get`
    用 `insecure=True` 建了放宽的上下文才通。这个坑之前抓江西、安徽、海南、
    湖南时都踩过一轮，已经写在 `net.py` 的注释里——所以这里不再重复造轮子。
    顺带白拿限速、重试、gzip 解压。
    """
    # interval 拉到 1.6 秒。默认 0.8 秒在 gz.gov.cn 上连续打几轮就开始变慢，
    # 宁可慢一点也不要被限流——被限流之后的表现是「整个方向都抓不到」，
    # 那比慢几十秒糟得多。
    return net.get(url, timeout=timeout, retries=1, referer=referer,
                   interval=1.6, insecure=True)


def _looks_like_doc(title: str) -> bool:
    if _NOISE.search(title):
        return False
    t = re.sub(r"[（(][^）)]{0,30}[）)]$", "", title)
    return any(w in t for w in _DOC_WORDS) or len(t) >= 16



# ══════════════════════════════════════════════════════════════════
# 三、抓正文
# ══════════════════════════════════════════════════════════════════
def _ckey(url: str) -> str:
    import hashlib
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:20]


def _cache_read(url: str) -> dict | None:
    f = CACHE_DIR / (_ckey(url) + ".json")
    if not f.exists():
        return None
    try:
        if time.time() - f.stat().st_mtime > _CACHE_TTL:
            return None
        d = json.loads(f.read_text(encoding="utf-8"))
        d["cached"] = True
        return d
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(url: str, body: dict) -> None:
    if body.get("state") not in ("ok", "thin"):
        return                      # 失败不缓存，下次还要试
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / (_ckey(url) + ".json")).write_text(
            json.dumps({**body, "url": url}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def fetch_body(url: str, use_cache: bool = True) -> dict:
    """抓一份文件正文并切块。四态：ok / thin / empty / failed。"""
    if use_cache:
        hit = _cache_read(url)
        if hit:
            return hit
    t0 = time.time()
    try:
        html = _http(url, timeout=_TIMEOUT)
    except Exception as e:                                  # noqa: BLE001
        return {"state": "failed", "reason": f"{type(e).__name__}: {str(e)[:60]}",
                "chars": 0, "blocks": [], "ms": int((time.time() - t0) * 1000)}
    try:
        text, method, why = extract_body(html)
    except Exception as e:                                  # noqa: BLE001
        return {"state": "failed", "reason": f"抽取异常：{str(e)[:60]}",
                "chars": 0, "blocks": [], "ms": int((time.time() - t0) * 1000)}
    text = (text or "").strip()[:_MAX_BODY_CHARS]
    if len(text) < 200:
        return {"state": "empty",
                "reason": f"正文仅 {len(text)} 字（{why or method or '未命中正文容器'}）",
                "chars": len(text), "blocks": [], "ms": int((time.time() - t0) * 1000)}
    blocks = split_blocks(text)
    n_art = sum(1 for k, _, _ in blocks if k == "art")
    body = {"state": "ok" if len(text) >= 800 else "thin",
            "reason": "", "chars": len(text), "n_art": n_art,
            "blocks": [{"label": lb, "text": tx, "kind": kd}
                       for kd, lb, tx in blocks],
            "ms": int((time.time() - t0) * 1000)}
    _cache_write(url, body)
    return body


# ══════════════════════════════════════════════════════════════════
# 四、对外：一个方向
# ══════════════════════════════════════════════════════════════════
def search(task: dict, city: str = "", province: str = "",
           topk: int = 10, workers: int = 6) -> tuple[list[dict], dict]:
    """对一条取证指令做联网检索。

    返回 (items, report)。items 与 gather 的库内条目同构，多三个字段：
      `where="web"`（来源）、`_blocks`（正文块，给 fetch_bodies 直接用）、
      `eff_hint`（源端时效：repealed / ok / 空）。
    """
    t0 = time.time()
    queries = [q for q in (task.get("queries") or []) if len(q) >= 2][:4]
    kind = task.get("kind") or ""
    if not queries:
        return [], {"kind": kind, "channel": "-", "error": "这一方向没有可用检索词",
                    "n_body": 0, "ms": 0, "queries": []}

    # 站点归口：本地层查本市，省层查省，国家层不限城市
    if kind in ("reg_nation", "policy_nation", "standard"):
        place, lib_city = "", ""
    elif kind in ("reg_prov", "policy_prov"):
        place, lib_city = province, province
    else:
        place, lib_city = city, city

    items: list[dict] = []
    rep: dict = {"kind": kind, "queries": queries, "channel": "", "n_body": 0}

    # 站点被挡的城市：立刻返回真实原因，不探测、不假装。
    # 先短路再谈通道，否则一个注定失败的探查要占掉十几秒预算。
    _blk = _blocked_of(place)
    if _blk:
        rep["error"] = _blk
        rep["blocked"] = True
        rep["ms"] = int((time.time() - t0) * 1000)
        return [], rep

    # 通道一：政策库接口（只在本地层用；省层的库还没适配）
    _lib, _ = _lib_of(lib_city) if lib_city else ({}, "")
    _lib_kinds = _lib.get("kinds") or ()
    if lib_city and (not _lib_kinds or kind in _lib_kinds):
        got, r1 = query_city_lib(lib_city, queries, cap=topk * 2)
        rep.update({k: v for k, v in r1.items() if k not in rep})
        rep["channel"] = "city_lib" if got else ""
        items += got

    if lib_city and _lib_kinds and kind not in _lib_kinds:
        rep["error"] = (f"「{_lib.get('name', lib_city)}」不收录这一类文件"
                        f"（该库只收规范性文件，规划/技术标准走别的渠道）")

    # 通道二：现探站点栏目（通道一没结果时才走，避免慢）
    #
    # 只对「本市」用。省层探省级门户实测一次能跑三十秒以上——省级站栏目
    # 层级深、翻页链长，而探出来的东西也解决不了「省人大法规不在库」这个
    # 真实边界。国家层同理（部委几十个域名，探完毫无意义）。
    # 与其让用户为一个注定失败的探查等半分多钟，不如直接说清缺什么。
    if not items and place and kind not in ("reg_prov", "policy_prov",
                                            "reg_nation", "policy_nation",
                                            "standard"):
        got, r2 = probe_site_city(place, queries, cap=topk)
        rep.setdefault("engine_report", r2)
        if got:
            rep["channel"] = rep["channel"] or "probe"
            rep["error"] = ""                       # 兜底成功，清掉通道一的报错
        items += got

    if not items:
        rep["error"] = rep.get("error") or "两条通道都没有结果"
        rep["ms"] = int((time.time() - t0) * 1000)
        return [], rep

    # 抓正文之前先按「源端时效」排一次，只抓前面这些。
    #
    # 这一步不能省。实测搜「广州城市更新」，源端标了「失效」的老文件（2015—2019
    # 那批旧的更新办法、安置房管理办法）在召回里占多数，直接抓 20 份、按字数排，
    # 首屏全是失效文件——用户拿这个去写方案会引用一堆已经作废的规定。
    # 失效的仍然保留（旧版怎么规定有时也要知道），但排在后面。
    items.sort(key=lambda x: (1 if x.get("eff_hint") == "repealed" else 0,
                              -(x.get("score") or 0)))
    items = items[:topk + 4]

    # 抓正文。无论哪条通道来的，都必须抓到本体才有核验资格。
    #
    # 必须有总预算，而且**不能写 `with ThreadPoolExecutor(...)`**。
    # `with` 退出时调的是 `shutdown(wait=True)`，会把还没跑完的线程等完——
    # 预算形同虚设，实测一个方向能拖两分半。这里显式 shutdown(wait=False)，
    # 到点就走，剩下的标「超时未取」。
    left = max(6, _BUDGET - (time.time() - t0))
    done: dict[str, dict] = {}
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(fetch_body, it["url"]): it for it in items}
        try:
            for f in as_completed(futs, timeout=left):
                it = futs[f]
                try:
                    done[it["url"]] = f.result()
                except Exception as e:                  # noqa: BLE001
                    done[it["url"]] = {"state": "failed", "reason": str(e)[:60],
                                       "chars": 0, "blocks": []}
        except TimeoutError:
            pass
        for f, it in futs.items():
            if it["url"] not in done:
                done[it["url"]] = {"state": "failed",
                                   "reason": f"{_BUDGET} 秒预算内没取完，已跳过",
                                   "chars": 0, "blocks": []}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    out = []
    for it in items:
        b = done.get(it["url"]) or {}
        st = b.get("state") or "failed"
        out.append({
            "title": it["title"], "region": place or "国家", "level": "",
            "doc_no": it.get("doc_no") or "", "published": it.get("published") or "",
            "url": it["url"],
            "source": "联网·" + ("城市政策库" if rep.get("channel") == "city_lib"
                                else "搜索引擎"),
            "doc_type": "web", "hits": [it.get("query") or ""], "bucket": "related",
            "score": it.get("score") or 0.3,
            "where": "web", "geo_why": "", "task_kind": kind,
            "has_body": st in ("ok", "thin") and bool(b.get("blocks")),
            "chars": b.get("chars") or 0, "n_art": b.get("n_art") or 0,
            "body_state": st, "body_note": b.get("reason") or "",
            "eff_hint": it.get("eff_hint") or "", "eff_state": it.get("eff_state") or "",
            "issuer": it.get("issuer") or "",
            "_blocks": b.get("blocks") or [],
        })

    # 有正文且能引条号的排前面。联网场景下「能读到」比「标题像」值钱得多：
    # 标题像的文件遍地都是，读到的那份才是证据。
    # 第一键是时效：现行有效的先给，失效的垫底。
    out.sort(key=lambda x: (1 if x.get("eff_hint") == "repealed" else 0,
                            0 if x["n_art"] else 1, 0 if x["has_body"] else 1,
                            -(x["chars"] or 0), -(x["score"] or 0)))
    rep["n_body"] = sum(1 for x in out if x["has_body"])
    rep["n_art"] = sum(1 for x in out if x["n_art"])
    rep["n_cached"] = sum(1 for x in out if x.get("body_state") and
                          (done.get(x["url"]) or {}).get("cached"))
    rep["ms"] = int((time.time() - t0) * 1000)
    if not rep["n_body"]:
        rep["error"] = (f"找到 {len(out)} 份像文件的页面，但一份正文都没抓下来"
                        f"（多为图件、JS 渲染页或被反爬拦截）")
    return out[:topk], rep


# ── 地名 → 门户域名 ────────────────────────────────────────────────
_dom_cache: dict[str, str] = {}
_dom_lock = threading.Lock()


def domain_of(place: str) -> str:
    """城市/省名 → 政府门户域名。查 cities.csv（2,491 条），查不到返回空。"""
    if not place:
        return ""
    with _dom_lock:
        if place in _dom_cache:
            return _dom_cache[place]
    out = ""
    if CITIES_CSV.exists():
        import csv
        core = re.sub(r"(省|市|自治区|自治州|地区|盟|县|区|旗)$", "", place)
        best = ""
        with CITIES_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("地域") or "").strip()
                url = (row.get("门户") or "").strip()
                if not name or not url:
                    continue
                if name in (place, core, core + "市", core + "省"):
                    best = url
                    break
                if core and core in name and (not best or len(name) < len(best)):
                    best = url
        out = re.sub(r"^https?://", "", best).split("/")[0]
    with _dom_lock:
        _dom_cache[place] = out
    return out


def coverage() -> dict:
    """联网能力的真实边界，给界面显示。"""
    return {"city_libs": sorted(CITY_LIBS),
            "bulk_loaded": ["广州市", "上海市"],
            "blocked": {k: v.split("。")[0] for k, v in BLOCKED_SITES.items()},
            "fallback": "现探目标市政府门户的文件栏目（复用 local_catalog 的探测逻辑）",
            "gov_domain_only": True,
            "note": "公开搜索引擎这条路已放弃：必应无视 site:、搜狗反爬、"
                    "360 链接加密、百度拒绝裸请求，且中文搜索对政策文件的召回质量差"}


def self_test(city: str = "广州市", query: str = "城市更新") -> None:
    dom = domain_of(city)
    print(f"{city} → {dom or '(未解析到域名)'}")
    print(f"适配城市库：{sorted(CITY_LIBS)}")
    task = {"id": "t1", "kind": "reg_local", "queries": [query]}
    items, rep = search(task, city=city, topk=8)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    for it in items:
        tag = f"{it['n_art']}条" if it["n_art"] else "无条号"
        print(f"  [{it['body_state']:6s}] {it['chars']:>6}字 {tag:5s} "
              f"{it['eff_state']:>10s} {it['title'][:38]}")
        print(f"       {it['url'][:88]}")


if __name__ == "__main__":
    self_test(sys.argv[1] if len(sys.argv) > 1 else "广州市",
              sys.argv[2] if len(sys.argv) > 2 else "城市更新")
