#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量看 blocked 站的列表页：到底是 URL 过滤掉了，还是标题过滤掉了。

对每个站打印漏斗：anchor → 站内 .html → 长度≥8 → 像文件。
若前三步有量、最后一步为 0，问题在标题过滤；若站内 .html 就极少，是 SPA。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                     # noqa: E402
from catalog.model import strip_tags                        # noqa: E402
import harvest.local_catalog as L                           # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 20

conn = sqlite3.connect(f"file:{ROOT / 'data' / 'catalog.db'}?mode=ro",
                       uri=True, timeout=20)
rows = list(conn.execute(
    "SELECT name,base,entry FROM local_site "
    "WHERE status='blocked' AND level='市级' AND reason LIKE '%解析不出%' "
    "ORDER BY RANDOM() LIMIT ?", (LIMIT,)))
conn.close()

for name, base, entry_json in rows:
    print("=" * 70)
    print(f"{name}  {base}")
    import json
    try:
        ents = json.loads(entry_json or "[]")
    except ValueError:
        ents = []
    if not ents:
        print("  无入口记录")
        continue
    url = ents[0]["url"]
    print(f"  入口 [{ents[0].get('text')}] {url}")
    try:
        h = L._get(url, base + "/")
    except net.FetchError as e:
        print(f"  抓取失败：{str(e)[:60]}")
        continue
    anchors = L._anchors(h)
    inner, long_t, looks, rejects = [], [], 0, []
    for href, t in anchors:
        u = L._abs(url, href)
        if not L._same_site(url, u) or not L._href_ok(u):
            continue
        inner.append(u)
        t = strip_tags(t)
        if len(t) < 8:
            continue
        long_t.append(t)
        if L._looks_like_file_title(t):
            looks += 1
        elif len(rejects) < 5:
            rejects.append(t)
    print(f"  {len(h):6d} 字符 | anchor {len(anchors):4d} → 站内html {len(inner):4d}"
          f" → 长度≥8 {len(long_t):4d} → 像文件 {looks:3d}")
    for t in rejects:
        print(f"     拒 {t[:58]}")
    if not inner:
        ifs = L._IFRAME.findall(h)
        print(f"     站内 html 为 0；iframe {len(ifs)} 个；"
              f"页面可见文字 {len(strip_tags(h))} 字")
