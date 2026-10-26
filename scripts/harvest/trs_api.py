#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TRS `/tssearch/server/query` 接口采集器。

## 为什么单独写这一个

南京市站被 `local_catalog.py` 判成 `blocked`——理由是「首页与常规路径都没有文件
栏目入口」。这个判断是错的，实际原因是：`http://www.nanjing.gov.cn/` 返回的是一个
618 字节的 JS 跳转壳（按 UA 把访问者送到 `/index.html` 或移动版），抓取器的入口
识别只认 meta refresh，认不出 `window.location.href = pc`，于是在首页就判定「没有
入口」并放弃。

进到 `/index.html` 之后能看见四个库：政策法规库、规章库、行政规范性文件库、政府文件。
但这四个页面本身也是 JS 渲染的空壳（HTML 里只有 18–21 个 `<a>`，没有一条是文件）。
真正的数据在：

    POST https://www.nanjing.gov.cn/tssearch/server/query
    {"serverName": "policy_regulatory", "pageNumber": 1, "pageSize": 500,
     "strOrder": "-pubdate",
     "filters": [{"filterName":["doctitle"], "filterValue":[""], "expression":"like"},
                 {"filterName":["fileNum"],  "filterValue":[""], "expression":"like"},
                 {"filterName":["themecat"], "filterValue":[""], "expression":"eq"},
                 {"filterName":["subcat"],   "filterValue":[""], "expression":"eq"},
                 {"filterName":["year"],     "filterValue":[""], "expression":"eq"}],
     "showFields": ["metadataid","filenum","themecat","subcat","doctitle",
                    "docpuburl","pubdate","year"]}

三个必须照抄的地方，少一个就返回 `{"msg":"请求数据异常","status":500}`：
1. `filters` 的**五个**占位项一个都不能省（空数组 `[]` 也不行——试过，报 500）；
2. `showFields` 里必须含 `doctitle` 与 `DOCPUBURL` 的字段名（`docpuburl` / `docurl`，
   大小写按站而定），否则返回的数据里没有链接；
3. `pageSize` 可以给到 500（试过 500 正常），再大没试——4 页拉完 1,624 条没必要冒险。

## 它能干什么，不能干什么

能做的是**按标题子串检索**（`doctitle` + `like`），也就是「这个站上有没有讲到
XX 的文件」。这跟全量抓取是两种东西：

- 全量抓取适合自己能翻页的静态列表，一次拿完、离线可查；
- 这个接口适合**按需求现场问**——用户问什么就查什么，不用把 1,600 条全搬回来。

本项目两种都用：全量入库供检索（`--all`），接口保留供「库里没有时回去问源站」
（`search()`，被 `app/remote.py` 调用）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "harvest"))
from local_catalog import save  # noqa: E402

# 站点 → (接口地址, serverName, referer, 地域名, 层级)
SITES: dict[str, dict] = {
    "南京市": {
        "api": "https://www.nanjing.gov.cn/tssearch/server/query",
        "server": "policy_regulatory",
        "referer": "https://www.nanjing.gov.cn/xxgkn/zcfgk/",
        "base": "https://www.nanjing.gov.cn",
        "title_field": "doctitle",
        "url_field": "DOCPUBURL",
        "no_field": "fileNum",
        "date_field": "pubdate",
    },
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _body(cfg: dict, page: int, size: int, title: str = "") -> dict:
    return {
        "serverName": cfg["server"],
        "pageNumber": page,
        "pageSize": size,
        "strOrder": "-pubdate",
        "filters": [
            {"filterName": ["doctitle"], "filterValue": [title], "expression": "like"},
            {"filterName": ["fileNum"], "filterValue": [""], "expression": "like"},
            {"filterName": ["themecat"], "filterValue": [""], "expression": "eq"},
            {"filterName": ["subcat"], "filterValue": [""], "expression": "eq"},
            {"filterName": ["year"], "filterValue": [""], "expression": "eq"},
        ],
        "showFields": ["metadataid", "filenum", "themecat", "subcat",
                       "doctitle", "docpuburl", "pubdate", "year"],
    }


def _post(cfg: dict, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        cfg["api"], data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA,
                 "Referer": cfg["referer"],
                 "Origin": cfg["base"]})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    d = json.loads(raw.decode("utf-8", "ignore"))
    if d.get("status") != 200:
        raise RuntimeError(f"接口拒绝：{d.get('msg')}")
    return d["data"]


def _pick(d: dict, *names: str) -> str:
    """字段名大小写在各地不一致（DOCPUBURL / docpuburl），逐个试。"""
    for n in names:
        v = d.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def harvest(name: str, page_size: int = 500, max_pages: int = 40) -> dict:
    """全量拉一个站，返回 save() 能吃的结构。"""
    cfg = SITES[name]
    items: list[dict] = []
    total = 0
    for p in range(1, max_pages + 1):
        data = _post(cfg, _body(cfg, p, page_size))
        total = data.get("total") or total
        rows = data.get("datas") or []
        if not rows:
            break
        for d in rows:
            title = _pick(d, cfg["title_field"], "doctitle")
            url = _pick(d, cfg["url_field"], "docpuburl", "docurl", "DOCPUBURL")
            if not title or not url:
                continue
            date = _pick(d, cfg["date_field"], "pubdate").split(" ")[0]
            items.append({
                "title": re.sub(r"\s+", " ", title).strip(),
                "url": url,
                "published": date,
                "doc_no": _pick(d, cfg["no_field"], "filenum"),
                "issuer": "",
                "col": "政策法规库",
            })
        got = sum(len(x.get("datas") or []) for x in [data])
        if len(items) >= total or got == 0:
            break
        time.sleep(0.4)
    return {"status": "ok",
            "reason": f"TRS 接口自报 {total} 条，实取 {len(items)} 条",
            "entries": [{"url": cfg["referer"], "text": "政策法规库"}],
            "pages": max(1, (total + page_size - 1) // page_size),
            "items": items}


def harvest_list(name: str, entry: str, pages: int = 6, col: str = "专项规划") -> dict:
    """抓静态翻页栏目（`index.html` / `index_1.html` …）。

    南京规划局的「专项规划」栏目是这个形态。它值得单独抓一次，因为
    **街区保护规划、名城保护规划只在这里**——市政府政策法规库收的是法规
    和规范性文件，规划成果不进那个库。用户问「老门西怎么更新」，
    没有《总统府历史文化街区保护规划》这种同城同类的成果，回答是空的。
    """
    base = entry if entry.endswith("/") else entry + "/"
    seen: dict[str, dict] = {}
    for p in range(0, pages):
        url = base + ("index.html" if p == 0 else f"index_{p}.html")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=25, context=_CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                html = raw.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            break
        found = 0
        for m in re.finditer(r'<a[^>]*href="([^"]*?/[a-z]*(\d{6})/t(\d{8})_\d+\.html)"[^>]*>'
                             r'(.*?)</a>', html, re.S):
            href, _, ymd, inner = m.group(1), m.group(2), m.group(3), m.group(4)
            title = re.sub(r"<[^>]+>", "", inner)
            title = re.sub(r"\s+", " ", title).strip()
            if len(title) < 8 or title in seen:
                continue
            absu = href if href.startswith("http") else urllib.parse.urljoin(url, href)
            seen[title] = {"title": title, "url": absu,
                           "published": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                           "doc_no": "", "issuer": "", "col": col}
            found += 1
        if found == 0:
            break
        time.sleep(0.3)
    return {"status": "ok", "reason": f"静态栏目 {pages} 页，取 {len(seen)} 条",
            "entries": [{"url": entry, "text": col}], "pages": pages,
            "items": list(seen.values())}


def search(name: str, keyword: str, limit: int = 30) -> list[dict]:
    """按标题子串现场问源站。给「库里没有，但源站可能有」用。"""
    cfg = SITES.get(name)
    if not cfg:
        return []
    try:
        data = _post(cfg, _body(cfg, 1, min(limit, 100), keyword))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in data.get("datas") or []:
        title = _pick(d, cfg["title_field"], "doctitle")
        url = _pick(d, cfg["url_field"], "docpuburl", "docurl", "DOCPUBURL")
        if not title:
            continue
        out.append({"title": title, "url": url,
                    "published": _pick(d, cfg["date_field"], "pubdate").split(" ")[0],
                    "doc_no": _pick(d, cfg["no_field"], "filenum"),
                    "region": name, "level": "市级",
                    "source": f"{name}政策法规库（源站实时）",
                    "kind": "remote", "_remote": True})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="南京市")
    ap.add_argument("--page-size", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", default="", help="只按标题检索一个词，不入库")
    ap.add_argument("--list", default="", help="抓一个静态翻页栏目，如 https://…/zxgh/")
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--col", default="专项规划")
    a = ap.parse_args()

    if a.list:
        t = time.time()
        r = harvest_list(a.site, a.list, a.pages, a.col)
        print(f"{a.site} {a.col}：{r['reason']}（{time.time() - t:.0f}s）")
        for it in r["items"][:15]:
            print("  ", it["published"], it["title"][:56])
        if a.dry_run:
            return 0
        n = save("南京市", SITES.get(a.site, {}).get("base", a.list), r,
                 int((time.time() - t) * 1000))
        print(f"入库新增 {n} 条")
        return 0

    if a.probe:
        rows = search(a.site, a.probe)
        print(f"{a.site} 标题含「{a.probe}」：{len(rows)} 条")
        for r in rows[:20]:
            print(f"  {r['published'] or '          '}  {r['doc_no'] or '—':24s} {r['title'][:46]}")
        return 0

    t = time.time()
    r = harvest(a.site, a.page_size)
    print(f"{a.site}：{r['reason']}（{time.time() - t:.0f}s）")
    if a.dry_run:
        for it in r["items"][:12]:
            print("  ", it["published"], it["doc_no"][:20], it["title"][:50])
        return 0
    n = save(a.site, SITES[a.site]["base"], r, int((time.time() - t) * 1000))
    print(f"入库新增 {n} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
