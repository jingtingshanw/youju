#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断抓不到的省级站：首页可达性 / 入口 anchor / 是否 SPA。

只打印证据，不下结论。用来决定哪些站能靠「换入口 / 换协议」救回来，
哪些是真的没有静态入口。
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                     # noqa: E402
import harvest.local_catalog as L                           # noqa: E402

TARGETS = [
    ("上海市", "https://www.shanghai.gov.cn"),
    ("黑龙江省", "https://www.hlj.gov.cn"),
    ("吉林省", "https://www.jl.gov.cn"),
    ("山东省", "http://www.shandong.gov.cn"),
    ("福建省", "https://www.fj.gov.cn"),
    ("湖南省", "https://www.hunan.gov.cn"),
    ("湖北省", "https://www.hubei.gov.cn"),
    ("河南省", "https://www.henan.gov.cn"),
    ("广西壮族自治区", "http://www.gxzf.gov.cn"),
    ("重庆市", "https://www.cq.gov.cn"),
    ("贵州省", "https://www.guizhou.gov.cn"),
    ("陕西省", "https://www.shaanxi.gov.cn"),
    ("广东省", "https://www.gd.gov.cn"),
    ("甘肃省", "https://www.gansu.gov.cn"),
    ("海南省", "https://www.hainan.gov.cn"),
    ("新疆维吾尔自治区", "https://www.xinjiang.gov.cn"),
    ("青海省", "https://www.qinghai.gov.cn"),
]

_KW = re.compile(r"(文件|政策|法规|规章|规范性|公开|公报)")


def fetch(url: str, ua: str = "") -> tuple[str, str]:
    """返回 (html 或 空, 错误说明)。"""
    hdr = {"User-Agent": ua or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9"}
    req = urllib.request.Request(url, headers=hdr)
    ctx = net._ssl_ctx(True)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            raw = r.read()
            try:
                return raw.decode("utf-8", "ignore"), ""
            except UnicodeDecodeError:
                return raw.decode("gbk", "ignore"), ""
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "ignore")
        return "", f"HTTP {e.code} {body[:60]}"
    except Exception as e:                                   # noqa: BLE001
        return "", f"{type(e).__name__}: {str(e)[:70]}"


def one(name: str, base: str) -> None:
    print("=" * 72)
    print(f"{name}  {base}")
    html, err = fetch(base + "/")
    if not html:
        # 退路一：http
        alt = base.replace("https://", "http://")
        if alt != base:
            h2, e2 = fetch(alt + "/")
            if h2:
                print(f"  https 失败({err[:40]}) → http 可用，{len(h2)} 字符")
                html = h2
        if not html:
            print(f"  首页不可达：{err}")
            return
    print(f"  首页 {len(html)} 字符，anchor {len(L._anchors(html))} 个")

    hits = []
    for href, t in L._anchors(html):
        t = L.strip_tags(t)
        if not t or len(t) > 24 or not _KW.search(t):
            continue
        full = L._abs(base + "/", href)
        if not L._same_site(base + "/", full):
            continue
        hits.append((t, full))
    print(f"  含文件类词的站内 anchor {len(hits)} 个：")
    seen = set()
    for t, u in hits[:14]:
        if u in seen:
            continue
        seen.add(u)
        print(f"    [{t}] {u[:95]}")

    # 是否 SPA：正文里有没有「加载中 / app / vue」
    spa = [k for k in ("加载中", "vue", "app.js", "__NUXT__", "react") if k in html]
    if spa:
        print(f"  SPA 迹象：{spa}")


for n, b in TARGETS:
    try:
        one(n, b)
    except Exception as e:                                   # noqa: BLE001
        print(f"{n} 异常 {type(e).__name__}: {e}")
