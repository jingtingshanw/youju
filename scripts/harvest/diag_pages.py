#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对「入口找到但解析不出条目」的站，判断它属于哪一类动态列表。

判据：
  - dataproxy / jpage  → 宏景 CMS，可走数据源
  - .json 路径         → 预生成数据，可直接取
  - iframe             → 列表在子页里
  - 站内 .html anchor 极少但页面很大 → 纯 SPA，静态抓不到
  - 有 anchor 但标题不像文件 → 标题过滤或列表结构问题
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                     # noqa: E402
import harvest.local_catalog as L                           # noqa: E402

TARGETS = [
    ("上海市", "https://www.shanghai.gov.cn", "https://www.shanghai.gov.cn/zhengce/list"),
    ("黑龙江省", "https://www.hlj.gov.cn", "https://www.hlj.gov.cn/hlj/c108369/zfxxgk.shtml"),
    ("山东省", "http://www.shandong.gov.cn", "http://www.shandong.gov.cn/col/col320658/index.html"),
    ("福建省", "https://www.fj.gov.cn", "https://www.fj.gov.cn/zck/"),
    ("河南省", "https://www.henan.gov.cn", "https://www.henan.gov.cn/zwgk/fgwj/szfl/"),
    ("重庆市", "https://www.cq.gov.cn", ""),
    ("贵州省", "https://www.guizhou.gov.cn", ""),
    ("陕西省", "https://www.shaanxi.gov.cn", ""),
]


def diag(name: str, base: str, page: str) -> None:
    print("=" * 72)
    print(f"{name}")
    if not page:
        ents, via = L.discover(base)
        if not ents:
            print(f"  无入口（{via}）")
            return
        page = ents[0][0]
        print(f"  入口 [{ents[0][1]}] {page}")
    else:
        print(f"  指定 {page}")
    try:
        h = L._get(page, base + "/")
    except net.FetchError as e:
        print(f"  抓取失败 {e}")
        return
    print(f"  {len(h)} 字符")

    flags = []
    if "dataproxy.jsp" in h:
        flags.append("dataproxy(jpage)")
    if re.search(r'["\'][^"\']*\.json', h):
        flags.append(".json")
    ifs = re.findall(r'<iframe[^>]+src=["\']([^"\']+)', h, re.I)
    if ifs:
        flags.append(f"iframe×{len(ifs)}")
    if "totalrecord" in h:
        flags.append("totalrecord")
    for k in ("__NUXT__", "vue", "加载中", "app.js"):
        if k in h:
            flags.append(k)
    print(f"  特征：{flags or '无'}")

    anchors = L._anchors(h)
    inner = []
    for href, t in anchors:
        u = L._abs(page, href)
        if L._same_site(page, u) and L._HREF_OK.search(u):
            inner.append(t)
    print(f"  anchor {len(anchors)} → 站内 .html {len(inner)}")
    looks = [t for t in inner if L._looks_like_file_title(L.strip_tags(t))]
    print(f"  像文件 {len(looks)}")
    if inner[:6]:
        print("  站内 link 样例：")
        for t in inner[:6]:
            print(f"    · {L.strip_tags(t)[:56]}")
    # 页面里的候选接口
    api = set(re.findall(r'["\'](/[A-Za-z0-9_\-/\.]*(?:list|query|search|data|page)[A-Za-z0-9_\-/\.]*)["\']', h, re.I))
    api = [a for a in api if len(a) > 6][:10]
    if api:
        print("  接口候选：", api[:8])


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    for n, b, p in TARGETS:
        if only and only not in n:
            continue
        try:
            diag(n, b, p)
        except Exception as e:                               # noqa: BLE001
            print(f"{n} 异常 {type(e).__name__}: {e}")
