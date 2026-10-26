#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目录层的落库与查询（SQLite，标准库，零依赖）。

设计上只做三件事：**合并同一份文件的多个来源**、**按地域×主题查**、**记住每个源
最后一次抓取的结果**。第三件事是为了让「这一次哪些源没抓到」能被如实显示出来。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from .model import CatalogItem, apply_ledger

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "data" / "catalog.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    issuer      TEXT DEFAULT '',
    doc_no      TEXT DEFAULT '',
    doc_type    TEXT DEFAULT '',
    doc_type_src TEXT DEFAULT '',
    region      TEXT DEFAULT '',
    region_src  TEXT DEFAULT '',
    admin_level TEXT DEFAULT '',
    topics      TEXT DEFAULT '[]',
    published   TEXT DEFAULT '',
    effective   TEXT DEFAULT '',
    abolished   TEXT DEFAULT '',
    status      TEXT DEFAULT '未知',
    status_src  TEXT DEFAULT '无依据',
    source      TEXT DEFAULT '',
    sources     TEXT DEFAULT '',
    source_type TEXT DEFAULT '',
    source_name TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    fetched_at  TEXT DEFAULT '',
    first_seen  REAL,
    last_seen   REAL
);
CREATE INDEX IF NOT EXISTS i_region  ON items(region);
CREATE INDEX IF NOT EXISTS i_level   ON items(admin_level);
CREATE INDEX IF NOT EXISTS i_docno   ON items(doc_no);
CREATE INDEX IF NOT EXISTS i_pub     ON items(published);

CREATE TABLE IF NOT EXISTS source_state (
    source      TEXT PRIMARY KEY,
    status      TEXT DEFAULT '',
    count       INTEGER DEFAULT 0,
    reason      TEXT DEFAULT '',
    checked_at  REAL,
    ms          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL,
    region      TEXT DEFAULT '',
    topic       TEXT DEFAULT '',
    keyword     TEXT DEFAULT '',
    n_items     INTEGER DEFAULT 0,
    coverage    TEXT DEFAULT '{}'
);
"""

# 清单的排序：国家 → 省级 → 市级（写「政策依据」章的常规顺序），同级按发布日期倒序
LEVEL_RANK = {"国家": 0, "省级": 1, "市级": 2}

# 「这个字段是怎么来的」的可信度排序。合并两条同源记录时，依据更强的胜出：
# 按标题判成「规范性文件（依据：标题）」，后来抓到文号能判成「部门规范性文件
# （依据：文号）」，后者更可信，应该覆盖前者而不是让旧的留着。
SRC_RANK = {
    "文号": 5,
    "时效台账": 4,
    "来源库": 3, "源标注": 3, "来源站点": 3, "来源栏目": 3, "发文机关": 3,
    "标题或机关": 3, "人工标注": 3,
    "标题": 2,
    "推断": 1,
    "无法判定": 0, "无依据": 0, "": 0,
}


def _src_rank(s: str) -> int:
    return SRC_RANK.get((s or "").strip(), 1)


_BOOK = re.compile(r"《([^》]{6,60})》")
_NOISE = re.compile(r"[\s\u3000（）()《》〈〉“”‘’\"'·、，,。.：:；;—\-－_]+")


def _book_key(title: str) -> str:
    """取标题里书名号中的文件名，做跨源去重用。

    为什么光靠标题不够：同一份文件从两个源进来，转载标题不一样——
      · 国家级源：「自然资源部办公厅关于印发《支持城市更新的规划与土地政策
        指引（2023版）》的通知」（带文号）
      · 市级站转载：「《支持城市更新的规划与土地政策指引（2023版）》印发」（无文号）
    两个 key、两个文号状态（一个有、一个空），`_find` 的三条路都撞不上，
    于是同一份文件在库里存了两条，还分别挂在「国家」和「鹰潭市」两级下。
    实测就是这么冒出来的。

    书名号里的字是最稳的那一段：公文的转载标题几乎一定保留原文件名。
    只认 6 字以上，避免「XX办法」这种短名误合并。
    """
    m = _BOOK.search(title or "")
    if not m:
        return ""
    return _NOISE.sub("", m.group(1))


def _rank(level: str) -> int:
    return LEVEL_RANK.get(level, 9)


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60)
        # WAL：允许「一个进程写、多个进程读」。目录抓取和全文抓取是两个进程，
        # 实测并发跑时后启动的那个会直接抛 database is locked 死掉。
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=60000")
        except sqlite3.OperationalError:
            pass
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── 写入 ────────────────────────────────────────────────────────────
    def upsert(self, items: list[CatalogItem]) -> dict[str, int]:
        """合并写入。返回 {新增, 合并}。

        合并规则：**已有的非空字段不被空白覆盖**，来源累加，后到的只补空缺。
        这样 gov.cn 拿到的文号、地方政府站拿到的发布日期可以拼成一条完整记录。
        """
        now = time.time()
        new = merged = 0
        for it in items:
            it.normalize()
            row = self._find(it)
            if row is None:
                d = it.to_row()
                d["sources"] = it.source
                d["first_seen"] = d["last_seen"] = now
                cols = ",".join(d)
                ph = ",".join("?" * len(d))
                self.conn.execute(
                    f"INSERT INTO items ({cols}) VALUES ({ph})", list(d.values()))
                new += 1
                continue

            # 合并：只补空，不覆盖已有值
            upd: dict[str, object] = {}
            for f in ("issuer", "doc_no", "published", "effective", "abolished",
                      "url", "summary", "note", "source_name", "source_type"):
                cur = (row[f] or "").strip()
                new_v = getattr(it, f, "") or ""
                if not cur and new_v:
                    upd[f] = new_v
            # doc_type / region 必须连「依据」一起换。只换值不换依据，
            # 会出现「规范性文件（依据：无法判定）」这种自相矛盾的记录——
            # 实测就是这么冒出来一条的。
            for f, src_f in (("doc_type", "doc_type_src"), ("region", "region_src")):
                cur_v, cur_s = (row[f] or "").strip(), (row[src_f] or "").strip()
                new_v = getattr(it, f, "") or ""
                new_s = getattr(it, src_f, "") or ""
                if not new_v:
                    continue
                if not cur_v or _src_rank(new_s) > _src_rank(cur_s):
                    upd[f] = new_v
                    upd[src_f] = new_s
            if row["status"] == "未知" and it.status != "未知":
                upd["status"] = it.status
                upd["status_src"] = it.status_src
            # 主题取并集
            try:
                old_topics = json.loads(row["topics"] or "[]")
            except (TypeError, ValueError):
                old_topics = []
            union = list(dict.fromkeys([*old_topics, *it.topics]))
            if union != old_topics:
                upd["topics"] = json.dumps(union, ensure_ascii=False)
            srcs = [s for s in (row["sources"] or "").split(",") if s]
            if it.source and it.source not in srcs:
                srcs.append(it.source)
                upd["sources"] = ",".join(srcs)
            upd["last_seen"] = now
            sets = ",".join(f"{k}=?" for k in upd)
            self.conn.execute(f"UPDATE items SET {sets} WHERE key=?",
                              [*upd.values(), row["key"]])
            merged += 1
        self.conn.commit()
        return {"new": new, "merged": merged}

    def _find(self, it: CatalogItem) -> sqlite3.Row | None:
        """四条路找同一份文件：去重键 → 文号 → 完全同标题 → 书名号里的文件名。

        最后一条是 2026-10-19 审计补的：同一份文件从两个源进来时，转载标题不同、
        一个有文号一个没有，前三条都撞不上，结果同一份文件存了两条，
        还分别挂在「国家」和「鹰潭市」两级下——清单重复、条数虚高。
        """
        r = self.conn.execute("SELECT * FROM items WHERE key=?", (it.key,)).fetchone()
        if r:
            return r
        if it.doc_no:
            r = self.conn.execute(
                "SELECT * FROM items WHERE doc_no=? AND doc_no<>'' LIMIT 1",
                (it.doc_no,)).fetchone()
            if r:
                return r
        r = self.conn.execute(
            "SELECT * FROM items WHERE title=? LIMIT 1", (it.title,)).fetchone()
        if r:
            return r
        bk = _book_key(it.title)
        if bk:
            for cand in self.conn.execute(
                    "SELECT * FROM items WHERE title LIKE ? LIMIT 5", (f"%《%》%",)):
                if _book_key(cand["title"]) == bk:
                    return cand
        return None

    # ── 查询 ────────────────────────────────────────────────────────────
    def query(self, regions: list[str] | None = None,
              topics: list[str] | None = None,
              keyword: str = "",
              levels: list[str] | None = None,
              status: str = "",
              limit: int = 300) -> list[CatalogItem]:
        where, args = [], []
        if regions:
            where.append("region IN (%s)" % ",".join("?" * len(regions)))
            args += regions
        if levels:
            where.append("admin_level IN (%s)" % ",".join("?" * len(levels)))
            args += levels
        if status:
            where.append("status=?")
            args.append(status)
        if keyword:
            where.append("(title LIKE ? OR summary LIKE ? OR doc_no LIKE ? OR issuer LIKE ?)")
            args += [f"%{keyword}%"] * 4
        if topics:
            # topics 存的是 JSON 数组文本，用 LIKE 粗筛即可（量级几千条）
            where.append("(" + " OR ".join("topics LIKE ?" for _ in topics) + ")")
            args += [f'%"{t}"%' for t in topics]
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        # 两遍稳定排序：先按发布日期倒序，再按行政层级升序。
        # 比拼一个混合 key 直白，也不会踩「字符串倒序」那种技巧。
        rows.sort(key=lambda r: (r["published"] or "", r["last_seen"] or 0), reverse=True)
        rows.sort(key=lambda r: _rank(r["admin_level"]))
        return [CatalogItem.from_row(r) for r in rows[:limit]]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def stats(self) -> dict:
        by_level = {r[0]: r[1] for r in self.conn.execute(
            "SELECT admin_level, COUNT(*) FROM items GROUP BY admin_level")}
        by_status = {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM items GROUP BY status")}
        by_region = {r[0]: r[1] for r in self.conn.execute(
            "SELECT region, COUNT(*) FROM items GROUP BY region ORDER BY 2 DESC")}
        by_source = {r[0]: r[1] for r in self.conn.execute(
            "SELECT sources, COUNT(*) FROM items GROUP BY sources ORDER BY 2 DESC")}
        return {"total": self.count(), "by_level": by_level,
                "by_status": by_status, "by_region": by_region,
                "by_source": by_source}

    # ── 源状态 ──────────────────────────────────────────────────────────
    def record_source(self, name: str, status: str, count: int,
                      reason: str = "", ms: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO source_state (source,status,count,reason,checked_at,ms) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
            "status=excluded.status, count=excluded.count, reason=excluded.reason, "
            "checked_at=excluded.checked_at, ms=excluded.ms",
            (name, status, count, reason, time.time(), ms))
        self.conn.commit()

    def source_states(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM source_state ORDER BY source")]

    def record_run(self, region: str, topic: str, keyword: str,
                   n_items: int, coverage: dict) -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at,region,topic,keyword,n_items,coverage) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), region, topic, keyword, n_items,
             json.dumps(coverage, ensure_ascii=False)))
        self.conn.commit()

    def last_run(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["coverage"] = json.loads(d["coverage"] or "{}")
        except (TypeError, ValueError):
            d["coverage"] = {}
        return d

    # ── 台账对齐 ────────────────────────────────────────────────────────
    def align_ledger(self) -> int:
        """把整库的效力状态跟 `data/stale_ledger.json` 对一遍。"""
        items = [CatalogItem.from_row(dict(r))
                 for r in self.conn.execute("SELECT * FROM items")]
        n = apply_ledger(items)
        for it in items:
            self.conn.execute(
                "UPDATE items SET status=?, status_src=?, effective=?, abolished=?, "
                "doc_type=?, doc_type_src=?, note=? WHERE key=?",
                (it.status, it.status_src, it.effective, it.abolished,
                 it.doc_type, it.doc_type_src, it.note, it.key))
        self.conn.commit()
        return n

    def close(self) -> None:
        self.conn.close()


__all__ = ["Store", "DB_PATH"]
