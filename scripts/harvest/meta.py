#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抽 gov.cn 文件页面的元信息，重点是「时效」。

为什么要单独做这件事
--------------------
`gov_full.py` 抓正文时只存了正文，把页面上的元信息丢了。其中「时效」这一项
是整个项目最关键的输入之一：

  时 效：根据《国务院关于宣布失效一批国务院文件的决定》（国发〔2016〕38号），
        此文件已宣布失效。

库里 2016 年之前的文件有 3,849 份（1980—2015）。拿一份 1985 年已经失效的
《通知》去核验一份 2026 年的规划文本，结论是反的：工具会说「有依据」，
而那份依据二十年前就作废了。

实测覆盖率约 37%（8 份老文件抽样 3 份有）。不到一半，但够用——它不需要
覆盖全部，只需要把「明确写了已失效」的那批标出来，让闸门能把它们拦下。
没写时效的记为 unknown，不假装有效。

字段格式（gov.cn 政策文件库统一模板）
------------------------------------
    索 引 号：000014349/1988-00169
    主题分类：农业、林业、水利\\农业、畜牧业、渔业
    发文机关：国务院办公厅
    成文日期：1988年08月03日
    标 题：国务院办公厅转发……
    发文字号：国办发〔1988〕34号
    发布日期：2016年10月21日
    时 效：根据《……》，此文件已宣布失效。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                      # noqa: E402
from harvest import extract as E             # noqa: E402

DB_PATH = ROOT / "data" / "catalog.db"

# 标签必须带冒号才算，避免把正文里出现的「索引号」误当成字段。
# 全角冒号和半角冒号都要认：gov.cn 改版时两种都用过。
_FIELDS = [
    ("index_no", r"索\s*引\s*号"),
    ("topic", r"主\s*题\s*分\s*类"),
    ("issuer", r"发\s*文\s*机\s*关"),
    ("written", r"成\s*文\s*日\s*期"),
    ("title", r"标\s*题"),
    ("doc_no", r"发\s*文\s*字\s*号"),
    ("published", r"发\s*布\s*日\s*期"),
    ("effective", r"时\s*效"),
]
_LABEL_RE = re.compile(
    r"(?:" + "|".join(f"(?:{p})" for _, p in _FIELDS) + r")\s*[：:]", re.M)
# 值里不该出现换行；一个值最多 200 字（「时效」有时带整句说明）
_VALUE_MAX = 200

_DEAD_WORDS = ("已宣布失效", "已失效", "失效", "已废止", "已废除", "停止执行")
_AMEND_WORDS = ("已修改", "部分修改", "修订")


def parse_meta(text: str) -> dict:
    """从整页文本里切出元信息。找不到的字段一律缺省，不猜。"""
    hits = list(_LABEL_RE.finditer(text))
    if not hits:
        return {}
    raw: dict[str, str] = {}
    for i, m in enumerate(hits):
        label = m.group(0)
        end = hits[i + 1].start() if i + 1 < len(hits) else min(
            len(text), m.end() + _VALUE_MAX)
        val = text[m.end():end].strip()
        # 去掉值里的换行与多余空白；值太长说明切错了位置，丢掉
        val = re.sub(r"\s*\n\s*", " ", val).strip()
        if not val or len(val) > _VALUE_MAX:
            continue
        for key, pat in _FIELDS:
            if re.fullmatch(rf"(?:{pat})\s*[：:]", label):
                raw.setdefault(key, val)
                break
    return raw


def classify(effective: str) -> str:
    """把时效原文归成三档。未知就是未知，不默认有效。

    这是刻意的：宁可说「未核」，不提供一个可能错的「有效」。
    """
    if not effective:
        return "unknown"
    for w in _DEAD_WORDS:
        if w in effective:
            return "repealed"
    for w in _AMEND_WORDS:
        if w in effective:
            return "amended"
    if "有效" in effective:
        return "valid"
    return "unknown"


SCHEMA_PATCH = """
CREATE TABLE IF NOT EXISTS doc_meta (
    key        TEXT PRIMARY KEY,
    index_no   TEXT DEFAULT '',
    topic      TEXT DEFAULT '',
    issuer     TEXT DEFAULT '',
    written    TEXT DEFAULT '',
    doc_no     TEXT DEFAULT '',
    published  TEXT DEFAULT '',
    effective  TEXT DEFAULT '',
    eff_status TEXT DEFAULT 'unknown',   -- valid / amended / repealed / unknown
    method     TEXT DEFAULT '',
    fetched_at REAL
);
CREATE INDEX IF NOT EXISTS i_m_eff ON doc_meta(eff_status);
"""


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=60)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
    except sqlite3.OperationalError:
        pass
    c.executescript(SCHEMA_PATCH)
    return c


def _one(row: tuple) -> dict:
    key, url, title = row
    try:
        raw = net.get(url, timeout=25, retries=0,
                      referer="https://www.gov.cn/", interval=0)
    except net.FetchError as e:
        return {"key": key, "error": str(e)[:100]}
    text = E.html_to_text(raw)
    meta = parse_meta(text)
    eff = meta.get("effective", "")
    return {"key": key, "meta": meta, "effective": eff,
            "eff_status": classify(eff), "title": title}


def run(limit: int = 0, workers: int = 4, redo: bool = False) -> None:
    conn = _db()
    # 只处理有全文的：其余 308 份没正文、页面只有元信息，单独一轮再跑
    sql = ("SELECT f.key, f.url, f.title FROM fulltext f "
           "WHERE f.status='ok' ORDER BY f.published")
    rows = list(conn.execute(sql))
    if not redo:
        done = {r[0] for r in conn.execute("SELECT key FROM doc_meta")}
        rows = [r for r in rows if r[0] not in done]
    if limit:
        rows = rows[:limit]
    print(f"待抽元信息 {len(rows)} 份，并发 {workers}")
    if not rows:
        _stats(conn)
        conn.close()
        return

    ok = no_eff = err = 0
    stat: dict[str, int] = {}
    t0 = time.time()
    conn.execute("BEGIN")
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_one, r): r for r in rows}
            for i, fu in enumerate(as_completed(futs), 1):
                res = fu.result()
                if res.get("error"):
                    err += 1
                    continue
                m = res["meta"]
                st = res["eff_status"]
                stat[st] = stat.get(st, 0) + 1
                no_eff += (st == "unknown")
                conn.execute(
                    "INSERT OR REPLACE INTO doc_meta (key,index_no,topic,issuer,"
                    "written,doc_no,published,effective,eff_status,method,fetched_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (res["key"], m.get("index_no", ""), m.get("topic", ""),
                     m.get("issuer", ""), m.get("written", ""),
                     m.get("doc_no", ""), m.get("published", ""),
                     res["effective"], st,
                     "govcn-meta" if m else "none", time.time()))
                ok += 1
                if i % 500 == 0:
                    conn.commit()
                    el = time.time() - t0
                    print(f"  {i}/{len(rows)}　{stat}　{el / i:.2f}s/份"
                          f"　剩余 {(len(rows) - i) * el / i / 60:.1f} 分钟")
    finally:
        conn.commit()
    print(f"\n完成 {ok} 份（无时效 {no_eff}，网络失败 {err}），"
          f"{ (time.time() - t0) / 60:.1f} 分钟")
    _stats(conn)
    conn.close()


def _stats(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM doc_meta").fetchone()[0]
    if not n:
        print("doc_meta 为空")
        return
    print(f"\ndoc_meta {n:,} 份")
    for st, c in conn.execute(
            "SELECT eff_status,COUNT(*) FROM doc_meta GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {st:10s} {c:6d}")
    print("\n已宣告失效的样例：")
    for t, e in conn.execute(
            "SELECT f.title, m.effective FROM doc_meta m "
            "JOIN fulltext f ON f.key = m.key WHERE m.eff_status='repealed' "
            "LIMIT 3"):
        print(f"  {t[:44]}\n      {e[:90]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="抽 gov.cn 元信息与时效")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.stats:
        _stats(_db())
        return
    run(a.limit, a.workers, a.redo)


if __name__ == "__main__":
    main()
