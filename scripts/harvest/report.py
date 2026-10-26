#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一句话回答「现在库里到底有什么」。

三层分开报，因为它们的可信度不一样：
  · 全文库：抓到的正文，逐份可追到 URL，可信度最高
  · 目录库：只有名称/文号/发布日期/链接，正文没抓，可信度看来源
  · 站点表：哪些站抓到了、哪些站解析不了、为什么——这是「覆盖率」的唯一依据

用法：python scripts/harvest/report.py [--json]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
CATALOG = ROOT / "data" / "catalog.db"
KB = ROOT / "data" / "kb.db"
FT = ROOT / "data" / "fulltext"

STATUS_CN = {"ok": "抓到", "blocked": "解析不到", "error": "不可达"}


def _q(db: sqlite3.Connection, sql: str, *a):
    try:
        return db.execute(sql, a).fetchall()
    except sqlite3.OperationalError:
        return []


def collect() -> dict:
    out: dict = {}
    if CATALOG.exists():
        c = sqlite3.connect(CATALOG, timeout=30)
        out["fulltext"] = {
            "ok": (_q(c, "SELECT COUNT(*) FROM fulltext WHERE status='ok'") or [[0]])[0][0],
            "failed": (_q(c, "SELECT COUNT(*) FROM fulltext WHERE status='failed'") or [[0]])[0][0],
            "chars": (_q(c, "SELECT SUM(chars) FROM fulltext WHERE status='ok'") or [[0]])[0][0] or 0,
        }
        out["catalog"] = {
            lv: n for lv, n in _q(
                c, "SELECT admin_level,COUNT(*) FROM items GROUP BY admin_level")
        }
        out["sites"] = {
            st: (n, items) for st, n, items in _q(
                c, "SELECT status,COUNT(*),SUM(n_items) FROM local_site GROUP BY status")
        }
        out["levels"] = {
            lv: {"total": n, "ok": ok, "items": items} for lv, n, ok, items in _q(
                c, "SELECT level,COUNT(*),SUM(status='ok'),SUM(n_items) "
                   "FROM local_site GROUP BY level")
        }
        c.close()
    if KB.exists():
        k = sqlite3.connect(KB, timeout=30)
        docs = (_q(k, "SELECT COUNT(*) FROM docs") or [[0]])[0][0]
        blk = (_q(k, "SELECT COUNT(*) FROM blocks") or [[0]])[0][0]
        k.close()
        out["index"] = {"docs": docs, "blocks": blk,
                        "size_mb": round(KB.stat().st_size / 1e6)}
    out["fulltext_dir_mb"] = round(
        sum(f.stat().st_size for f in FT.rglob("*.txt")) / 1e6) if FT.exists() else 0
    return out


def render(d: dict) -> None:
    ft = d.get("fulltext", {})
    print("═" * 60)
    print("有据 · 知识库现状")
    print("═" * 60)
    print(f"\n【全文库】{ft.get('ok', 0):,} 份　{ft.get('chars', 0) / 1e8:.2f} 亿字"
          f"　磁盘 {d.get('fulltext_dir_mb', 0):,} MB")
    if ft.get("failed"):
        print(f"          抓取失败 {ft['failed']:,} 份（原因见 gov_full.py --stats）")

    idx = d.get("index", {})
    if idx:
        print(f"\n【检索索引】{idx['docs']:,} 份 / {idx['blocks']:,} 块"
              f"　{idx['size_mb']:,} MB")
    else:
        print("\n【检索索引】未建（python scripts/harvest/build_index.py）")

    cat = d.get("catalog", {})
    print(f"\n【目录库】{sum(cat.values()):,} 条")
    for lv in ("国家", "省级", "市级", "县级"):
        if cat.get(lv):
            print(f"          {lv} {cat[lv]:,}")

    lv = d.get("levels", {})
    if lv:
        print("\n【站点覆盖】")
        for name in ("省级", "市级", "县级"):
            v = lv.get(name)
            if not v:
                continue
            rate = v["ok"] / v["total"] * 100 if v["total"] else 0
            print(f"          {name} {v['ok']:4d}/{v['total']:4d} 个站可抓"
                  f"（{rate:.0f}%）　条目 {v['items'] or 0:,}")
    sites = d.get("sites", {})
    if sites:
        print("\n          明细：" + "　".join(
            f"{STATUS_CN.get(k, k)} {v[0]}" for k, v in sites.items()))


def main() -> None:
    d = collect()
    if "--json" in sys.argv:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        render(d)


if __name__ == "__main__":
    main()
