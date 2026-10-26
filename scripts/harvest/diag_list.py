#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：一个站的栏目页为什么解析不出条目。

对每个站打印：入口 URL / 页面字节数 / anchor 总数 / 通过 URL 过滤的 anchor /
被判成非文件的标题样例。用来定位是「标题过滤太严」还是「列表根本不是 anchor」。
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                     # noqa: E402
from catalog.model import strip_tags, title_core            # noqa: E402
import harvest.local_catalog as L                          # noqa: E402

TARGETS = [
    ("浙江省", "https://www.zj.gov.cn"),
    ("山东省", "http://www.shandong.gov.cn"),
    ("江西省", "https://www.jiangxi.gov.cn"),
    ("四川省", "https://www.sc.gov.cn"),
    ("北京市", "https://www.beijing.gov.cn"),
]


def one(name: str, base: str) -> None:
    print("=" * 70)
    print(f"{name}  {base}")
    entries, via = L.discover(base)
    print(f"  入口来源：{via}")
    if not entries:
        return
    url, text = entries[0]
    print(f"  主入口：[{text}] {url}")
    try:
        html = L._get(url, base + "/")
    except net.FetchError as e:
        print(f"  抓取失败：{e}")
        return
    print(f"  页面 {len(html)} 字符")
    jp = L.jpge_first(html, base) if hasattr(L, "jpge_first") else L.jpage_first(html, base)
    print(f"  jpage 数据源：{jp[:110] if jp else '无'}")

    anchors = L._anchors(html)
    print(f"  anchor 总数 {len(anchors)}")

    same, href_ok, title_ok = 0, 0, 0
    samples_reject = []
    samples_accept = []
    for href, t in anchors:
        full = L._abs(url, href)
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        if not L._same_site(url, full):
            continue
        same += 1
        if not L._HREF_OK.search(full):
            continue
        href_ok += 1
        t = L.strip_tags(t) if hasattr(L, "strip_tags") else strip_tags(t)
        if L._looks_like_file_title(t):
            title_ok += 1
            if len(samples_accept) < 4:
                samples_accept.append((t, full))
        elif len(samples_reject) < 12:
            samples_reject.append(t)

    print(f"  站内 {same} → .html {href_ok} → 像文件 {title_ok}")
    print("  收录样例：")
    for t, u in samples_accept:
        print(f"    + {t[:60]}")
    print("  被拒样例（这些可能本来就是文件）：")
    for t in samples_reject:
        print(f"    - {t[:60]}")
    got = L.parse_list(html, url, text)
    print(f"  parse_list 实际返回 {len(got)} 条")


for n, b in TARGETS:
    try:
        one(n, b)
    except Exception as e:                                  # noqa: BLE001
        print(f"{n} 诊断异常：{type(e).__name__}: {e}")
