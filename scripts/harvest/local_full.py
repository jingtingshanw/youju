#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""地方文件正文抓取。

为什么需要这个
--------------
国家级已经有 18,608 份正文（gov_full.py），地方 66,692 条只有目录——标题、文号、
日期、链接，没有内容。

这不是一个小缺陷。用户问的是「南京老门西城市更新要哪些文件」，工具能告诉他
「《南京历史文化名城保护条例》存在」,却引不出第五十四条讲什么。核验只能标
`unverifiable`（确认存在、不能确认内容），「依据」段全是空引用。

南京 1,581 条里 0 份正文，是提问最集中的一块，先补这里。

与 gov_full.py 的分工
---------------------
· gov_full.py 从 gov.cn 的两个清单接口枚举，key 由清单给。
· 这个脚本从 catalog.db 的 items 表取 url，key 自己算（"lf" + md5(url)[:14]），
  前缀区分开，同一份文件被两边抓到也不会撞键。
· 都写 data/fulltext/<key前2位>/<key>.txt 和同一张 fulltext 表，所以
  build_index.py 不用改就能把新抓的正文编进 kb.db。

失败要分类
----------
· ok      —— 正文提取合格
· empty   —— 页面抓到了，但抽不出正文（通常是图件页、JS 渲染空壳）
· gone    —— 404/410，源站已经删了，重试一万次也没用
· failed  —— 网络/解析出错，值得重试

混在一起统计会把「源站没内容」算成「我们抓错了」，从而一直重试、一直报错。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                  # noqa: E402
from harvest import extract                              # noqa: E402

CATALOG_DB = ROOT / "data" / "catalog.db"
FT_DIR = ROOT / "data" / "fulltext"

WORKERS = 4
FETCH_INTERVAL = 0.4      # 地方政府站比 gov.cn 脆，间隔留宽一点

# 与 app/gather.py 的 _NOT_DOC 同一条规则：解读稿不是文件。
_NOT_DOC = re.compile(
    r"一图读懂|政策解读|解读|新闻发布会|图解|问答|访谈|评论|社论|"
    r"答记者问|在线访谈|意见征集结果")

FT_SCHEMA = """
CREATE TABLE IF NOT EXISTS fulltext (
    key         TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    doc_no      TEXT DEFAULT '',
    issuer      TEXT DEFAULT '',
    published   TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    path        TEXT DEFAULT '',
    chars       INTEGER DEFAULT 0,
    method      TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',
    reason      TEXT DEFAULT '',
    fetched_at  REAL
);
"""


def key_of(url: str) -> str:
    return "lf" + hashlib.md5(url.encode("utf-8")).hexdigest()[:14]


def ft_path(key: str) -> Path:
    return FT_DIR / key[:2] / f"{key}.txt"


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(CATALOG_DB, timeout=60)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
    except sqlite3.OperationalError:
        pass
    c.executescript(FT_SCHEMA)
    c.commit()
    return c


# ══════════════════════════════════════════════════════════════════════════
# 取待抓清单
# ══════════════════════════════════════════════════════════════════════════

def load_todo(region: str = "", source: str = "", retry_failed: bool = False,
              limit: int = 0) -> tuple[list[dict], dict]:
    conn = _db()
    where, args = ["coalesce(url,'') <> ''", f"title NOT LIKE '%一图读懂%'"], []
    if region:
        where.append("region LIKE ?")
        args.append(f"%{region}%")
    if source:
        where.append("source_name LIKE ?")
        args.append(f"%{source}%")
    rows = list(conn.execute(
        "SELECT title,url,doc_no,published,region,source_name FROM items "
        "WHERE " + " AND ".join(where) + " ORDER BY coalesce(published,'') DESC",
        args))
    # 解读稿不该抓。江苏那一批 53 条 empty 全是「一图读懂」「新闻发布会」——
    # 它们不是文件，是媒体解读，抓下来也引不到条文。加这一层是为了省请求，
    # 不是为了改变结果（它们本来就抓不到正文）。用 gather 的同一个正则，
    # 免得两处规则不一致。
    rows = [r for r in rows if not _NOT_DOC.search(r["title"] or "")]

    done: dict[str, tuple] = {}
    for r in conn.execute("SELECT key,status,chars,path FROM fulltext"):
        done[r["key"]] = (r["status"], r["chars"], r["path"])
    conn.close()

    todo, skip = [], 0
    seen: set[str] = set()
    for r in rows:
        k = key_of(r["url"])
        if k in seen:
            skip += 1
            continue
        seen.add(k)
        st = done.get(k)
        if st is None or (retry_failed and st[0] in ("failed", "empty")):
            todo.append({**dict(r), "key": k})
        else:
            skip += 1
    if limit:
        todo = todo[:limit]
    return todo, done


# ══════════════════════════════════════════════════════════════════════════
# 抓一份
# ══════════════════════════════════════════════════════════════════════════

_throttle_lock = threading.Lock()
_last_hit = [0.0]


def _wait() -> None:
    with _throttle_lock:
        gap = time.time() - _last_hit[0]
        if gap < FETCH_INTERVAL:
            time.sleep(FETCH_INTERVAL - gap)
        _last_hit[0] = time.time()


def fetch_one(rec: dict) -> dict:
    _wait()
    t0 = time.perf_counter()
    host = (rec.get("url") or "").split("/")[2] if "//" in (rec.get("url") or "") else ""
    try:
        raw = net.get(rec["url"], timeout=25, retries=1,
                      referer=f"https://{host}/" if host else "",
                      interval=0, insecure=True)
    except net.FetchError as e:
        msg = str(e)
        gone = "HTTP 404" in msg or "HTTP 410" in msg
        return {**rec, "status": "gone" if gone else "failed",
                "reason": ("源端已下线：" if gone else "网络：") + msg[:120],
                "chars": 0}

    text, method, why = extract.extract(raw)
    if not text:
        # 图件页（保护规划批后公布常是几张 JPG）和 JS 空壳都落在这里。
        # 记 empty 而不是 failed：重抓也还是这样。
        return {**rec, "status": "empty", "reason": why or "提取为空",
                "chars": 0, "method": method}

    p = ft_path(rec["key"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {**rec, "status": "ok", "reason": "", "chars": len(text),
            "method": method, "path": str(p),
            "ms": int((time.perf_counter() - t0) * 1000)}


def _save(conn: sqlite3.Connection, r: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fulltext (key,title,doc_no,issuer,published,url,"
        "path,chars,method,status,reason,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["key"], r.get("title", ""), r.get("doc_no", ""),
         r.get("source_name", ""), r.get("published", ""), r.get("url", ""),
         r.get("path", ""), r.get("chars", 0), r.get("method", ""),
         r["status"], r.get("reason", ""), time.time()))


# ══════════════════════════════════════════════════════════════════════════
# 命令
# ══════════════════════════════════════════════════════════════════════════

def cmd_fetch(region: str, source: str, limit: int, retry_failed: bool,
              workers: int) -> None:
    todo, done = load_todo(region, source, retry_failed, limit)
    n_ok = sum(1 for v in done.values() if v[0] == "ok")
    print(f"待抓 {len(todo)} 条（库里已有正文 {n_ok} 份）")
    if not todo:
        return
    conn = _db()
    ok = empty = gone = fail = 0
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_one, r): r for r in todo}
            for i, f in enumerate(as_completed(futs), 1):
                r = f.result()
                _save(conn, r)
                if r["status"] == "ok":
                    ok += 1
                elif r["status"] == "empty":
                    empty += 1
                elif r["status"] == "gone":
                    gone += 1
                else:
                    fail += 1
                if i % 20 == 0:
                    conn.commit()
                    el = time.time() - t0
                    print(f"  {i}/{len(todo)}　成功 {ok}　空页 {empty}　下线 {gone}　"
                          f"失败 {fail}　{el / i:.2f}s/条　"
                          f"剩余约 {(len(todo) - i) * el / i / 60:.1f} 分钟")
        conn.commit()
    finally:
        conn.close()
    el = time.time() - t0
    print(f"\n完成 {len(todo)} 条：成功 {ok}　空页 {empty}　下线 {gone}　失败 {fail}"
          f"　用时 {el / 60:.1f} 分钟")
    if empty:
        print("  空页多半是图件类规划（批后公布的正文是 JPG），这类引不到条文是数据本身的边界")


def cmd_stats(region: str, source: str) -> None:
    conn = _db()
    where, args = ["coalesce(url,'') <> ''"], []
    if region:
        where.append("region LIKE ?")
        args.append(f"%{region}%")
    if source:
        where.append("source_name LIKE ?")
        args.append(f"%{source}%")
    rows = list(conn.execute(
        "SELECT title,url FROM items WHERE " + " AND ".join(where), args))
    keys = {key_of(r["url"]) for r in rows}
    stat: dict[str, int] = {}
    chars = 0
    for k in keys:
        r = conn.execute("SELECT status,chars FROM fulltext WHERE key=?",
                         (k,)).fetchone()
        st = r["status"] if r else "pending"
        stat[st] = stat.get(st, 0) + 1
        if r and r["status"] == "ok":
            chars += r["chars"] or 0
    conn.close()
    total = len(keys) or 1
    print(f"条目 {len(keys)} 条")
    for st, n in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {st:<9} {n:>5}  {n / total:.1%}")
    print(f"  正文合计 {chars:,} 字，平均 {chars / max(1, stat.get('ok', 1)):,.0f} 字/份")


def main() -> None:
    ap = argparse.ArgumentParser(description="地方文件正文抓取")
    ap.add_argument("--region", default="", help="地域过滤，如 南京市 / 江苏省")
    ap.add_argument("--source", default="", help="来源过滤，如 政策法规库")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.stats:
        cmd_stats(a.region, a.source)
    else:
        cmd_fetch(a.region, a.source, a.limit, a.retry_failed, a.workers)


if __name__ == "__main__":
    main()
