#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""国家级政策全文库：把 gov.cn 政策文件库里**所有**文件抓成可检索的正文。

为什么要单独一个脚本
--------------------
目录层（`scripts/catalog/`）只存「这份文件存在」这件事，不存正文——因为目录
可以穷举（19,028 条），全文不能随便抓（体积、失败率、条款切分都要单独处理）。
两件事的生命周期不同，混在一个流程里会互相拖累，所以切开。

三段
----
    --list     枚举清单。把 gw（国务院文件）/ bm（部门文件）/ bmgz（部门规章）
               三类翻完，落 `data/catalog/govcn_list.*.jsonl`。可断点续跑。
    --fetch    按清单抓全文，正文落 `data/fulltext/<xx>/<key>.txt`，
               索引落 `data/catalog.db` 的 fulltext 表。可断点续跑。
    --stats    看抓了多少、失败多少、为什么失败。

两条硬规矩（和目录层一致）
--------------------------
1. **不静默失败**：抓不到的记 `failed` + 原因，不写空文件冒充成功。
2. **不覆盖已有**：二次运行只补缺的，不会把已经抓好的正文重抓一遍。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                  # noqa: E402
from catalog.model import CatalogItem, strip_tags         # noqa: E402
from harvest import extract                               # noqa: E402

LIST_DIR = ROOT / "data" / "catalog"
DB_PATH = ROOT / "data" / "catalog.db"
FT_DIR = ROOT / "data" / "fulltext"
GOVCN_API = "https://sousuo.www.gov.cn/search-gov/data"
REFERER = "https://sousuo.www.gov.cn/"

# t 参数 / type 参数 / 人读名。两个库合起来是「国家级能搜到的全部」：
#   gw 6,222 条（国务院文件：国发/国办发/国函/国务院令）
#   bm 12,806 条（国务院部门文件：部委行文、部委令、部门规章）
#
# 2026-10-20 实测：这个接口只认 type，`t` 传什么都一样——传
# `zhengcelibrary_bmgz` 仍然返回部门库的 12,806 条。所以「部门规章」不是
# 第三个独立库，它的内容已经在 bm 里。别再花时间找那个不存在的入口。
KINDS = {
    "gw": ("zhengcelibrary_gw", "gwyzcwjk", "国务院政策文件库"),
    "bm": ("zhengcelibrary_bm", "bwyzcwjk", "国务院部门文件库"),
}

PAGE_SIZE = 20
LIST_INTERVAL = 0.35     # 列表接口比正文页脆，留足间隔
FETCH_INTERVAL = 0.15    # 正文页是静态 CDN，可以快一些
WORKERS = 4

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
CREATE INDEX IF NOT EXISTS i_ft_status ON fulltext(status);
"""


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=60)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=60000")
    except sqlite3.OperationalError:
        pass
    c.executescript(FT_SCHEMA)
    c.commit()
    return c


# ══════════════════════════════════════════════════════════════════════════
# 一、枚举清单
# ══════════════════════════════════════════════════════════════════════════

def _progress_path() -> Path:
    return LIST_DIR / "govcn_progress.json"


def _load_progress() -> dict:
    p = _progress_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _save_progress(d: dict) -> None:
    _progress_path().write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                encoding="utf-8")


def _row_to_record(r: dict, kind: str) -> dict:
    title = strip_tags(r.get("title") or "")
    it = CatalogItem(
        title=title,
        issuer=(r.get("puborg") or "").strip(),
        doc_no=(r.get("pcode") or r.get("wenhao") or "").strip(),
        published=(r.get("pubtimeStr") or "").strip(),
        summary=strip_tags(r.get("summary") or ""),
        url=(r.get("url") or "").strip(),
        source=f"govcn_{kind}",
        source_type="official_api",
        source_name=KINDS[kind][2],
        note=(r.get("childtype") or "").strip(),
    ).normalize()
    if not it.region:
        it.region, it.region_src = "国家", "来源库"
    d = it.to_dict()
    d["_kind"] = kind
    d["_index"] = (r.get("index") or "").strip()   # 政府信息公开索引号
    return d


def cmd_list(kinds: list[str], pages: int = 0) -> None:
    """翻完全部页。`pages>0` 时只翻这么多页（用于试跑）。"""
    LIST_DIR.mkdir(parents=True, exist_ok=True)
    prog = _load_progress()
    for kind in kinds:
        t, ty, label = KINDS[kind]
        out_path = LIST_DIR / f"govcn_list.{kind}.jsonl"
        st = prog.setdefault(kind, {"page": 0, "total": 0, "done": False})
        if st["done"] and not pages:
            print(f"[{kind}] 已完成（{st['total']} 条），跳过")
            continue

        # 先问总量，才知道要翻多少页
        try:
            d = net.get_json(GOVCN_API, {
                "t": t, "q": "", "p": 1, "n": PAGE_SIZE, "type": ty,
                "sort": "pubtime", "sortType": 1,
            }, referer=REFERER, interval=LIST_INTERVAL)
        except net.FetchError as e:
            print(f"[{kind}] 首页请求失败：{e}")
            continue
        total = int((d.get("searchVO") or {}).get("totalCount") or 0)
        st["total"] = total
        n_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if pages:
            n_pages = min(n_pages, pages)
        print(f"[{kind}] {label}：源端自报 {total} 条 / {n_pages} 页"
              f"（从第 {st['page'] + 1} 页继续）")

        start_page = st["page"] + 1
        seen_keys: set[str] = set()
        if out_path.exists():
            with out_path.open(encoding="utf-8") as f:
                for ln in f:
                    try:
                        seen_keys.add(json.loads(ln)["key"])
                    except (ValueError, KeyError):
                        pass

        t0 = time.time()
        with out_path.open("a", encoding="utf-8") as f:
            for page in range(start_page, n_pages + 1):
                try:
                    d = net.get_json(GOVCN_API, {
                        "t": t, "q": "", "p": page, "n": PAGE_SIZE, "type": ty,
                        "sort": "pubtime", "sortType": 1,
                    }, referer=REFERER, interval=LIST_INTERVAL)
                except net.FetchError as e:
                    print(f"  p{page} 失败：{e}　（已写 {len(seen_keys)} 条，"
                          f"下次从第 {page} 页继续）")
                    break
                rows = (d.get("searchVO") or {}).get("listVO") or []
                if not rows:
                    break
                new = 0
                for r in rows:
                    rec = _row_to_record(r, kind)
                    if rec["key"] in seen_keys:
                        continue
                    seen_keys.add(rec["key"])
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    new += 1
                f.flush()
                st["page"] = page
                if page % 20 == 0 or page == n_pages:
                    _save_progress(prog)
                    rate = (page - start_page + 1) / max(0.001, time.time() - t0)
                    left = (n_pages - page) / max(0.01, rate)
                    print(f"  p{page}/{n_pages}　累计 {len(seen_keys)} 条"
                          f"　剩余约 {left / 60:.1f} 分钟")
                if len(rows) < PAGE_SIZE:
                    st["done"] = True
                    break
            if st["page"] >= n_pages:
                st["done"] = True
        _save_progress(prog)
        print(f"[{kind}] 完成：{len(seen_keys)} 条 → {out_path.name}")


# ══════════════════════════════════════════════════════════════════════════
# 二、抓全文
# ══════════════════════════════════════════════════════════════════════════

_throttle_lock = threading.Lock()
_last_hit = [0.0]


def _wait_turn(gap: float = FETCH_INTERVAL) -> None:
    """多线程共用一个节拍器：保证对 gov.cn 的总请求速率不随线程数放大。"""
    with _throttle_lock:
        wait = gap - (time.time() - _last_hit[0])
        if wait > 0:
            time.sleep(wait)
        _last_hit[0] = time.time()


def _ft_path(key: str) -> Path:
    return FT_DIR / key[:2] / f"{key}.txt"


def _load_list(kinds: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for kind in kinds:
        p = LIST_DIR / f"govcn_list.{kind}.jsonl"
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if not d.get("url"):
                    continue
                out.setdefault(d["key"], d)
    return out


def _fetch_one(rec: dict) -> dict:
    key, url = rec["key"], rec["url"]
    _wait_turn()
    t0 = time.perf_counter()
    try:
        raw = net.get(url, timeout=25, retries=1, referer="https://www.gov.cn/",
                      interval=0)
    except net.FetchError as e:
        msg = str(e)
        # 404/410 = 这一页在 gov.cn 上已经没了（改版/迁移），不是我们抓错。
        # 必须和「抓取失败」分开记：前者重试一万次也没用，后者是我的代码问题。
        # 混在一起统计，会把上游的下线算成自己的失败率。
        gone = "HTTP 404" in msg or "HTTP 410" in msg
        return {"key": key, "status": "gone" if gone else "failed",
                "reason": ("源端已下线：" if gone else "网络：") + msg[:120],
                "chars": 0}
    text, method, why = extract.extract(raw)
    if not text:
        return {"key": key, "status": "failed", "reason": why or "提取为空",
                "chars": 0, "method": method}
    p = _ft_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {"key": key, "status": "ok", "reason": "", "chars": len(text),
            "method": method,
            "ms": int((time.perf_counter() - t0) * 1000)}


def cmd_fetch(kinds: list[str], limit: int = 0, retry_failed: bool = False,
              workers: int = WORKERS) -> None:
    conn = _db()
    done: dict[str, tuple] = {}
    for r in conn.execute("SELECT key,status,chars,path FROM fulltext"):
        done[r[0]] = (r[1], r[2], r[3])

    recs = _load_list(kinds)
    todo = []
    for k, rec in recs.items():
        st = done.get(k)
        if st is None:
            todo.append(rec)
        elif retry_failed and st[0] == "failed":
            todo.append(rec)
    # 已有文件但表里没记录（上次异常退出）→ 补登记，不重抓
    orphan = [r for r in todo if _ft_path(r["key"]).exists()]
    for r in orphan:
        p = _ft_path(r["key"])
        conn.execute(
            "INSERT OR REPLACE INTO fulltext (key,title,doc_no,issuer,published,url,"
            "path,chars,method,status,reason,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["key"], r["title"], r["doc_no"], r["issuer"], r["published"], r["url"],
             str(p), p.stat().st_size, "existing", "ok", "", time.time()))
    if orphan:
        conn.commit()
        todo = [r for r in todo if not _ft_path(r["key"]).exists()]

    if limit:
        todo = todo[:limit]
    print(f"待抓 {len(todo)} 条（已在库 {sum(1 for v in done.values() if v[0] == 'ok')} 条）")
    if not todo:
        conn.close()
        return

    ok = fail = gone = 0
    t0 = time.time()
    conn.execute("BEGIN")
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_one, r): r for r in todo}
            for i, fu in enumerate(as_completed(futs), 1):
                r = futs[fu]
                try:
                    res = fu.result()
                except Exception as e:                       # noqa: BLE001
                    res = {"key": r["key"], "status": "failed",
                           "reason": f"{type(e).__name__}: {e}", "chars": 0}
                st = res["status"]
                ok += st == "ok"
                gone += st == "gone"
                fail += st not in ("ok", "gone")
                conn.execute(
                    "INSERT OR REPLACE INTO fulltext (key,title,doc_no,issuer,"
                    "published,url,path,chars,method,status,reason,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["key"], r["title"], r["doc_no"], r["issuer"], r["published"],
                     r["url"], str(_ft_path(r["key"])) if st == "ok" else "",
                     res.get("chars", 0), res.get("method", ""), st,
                     res.get("reason", ""), time.time()))
                if i % 100 == 0:
                    conn.commit()
                    el = time.time() - t0
                    rate = i / el
                    print(f"  {i}/{len(todo)}　成功 {ok} 源下线 {gone} 失败 {fail}"
                          f"　{rate:.2f} 条/秒　剩余约 {(len(todo) - i) / rate / 60:.1f} 分钟")
    finally:
        conn.commit()
        conn.close()
    print(f"\n完成：成功 {ok}，源端已下线 {gone}，失败 {fail}，"
          f"用时 {(time.time() - t0) / 60:.1f} 分钟")


# ══════════════════════════════════════════════════════════════════════════
# 三、统计
# ══════════════════════════════════════════════════════════════════════════

def cmd_stats(kinds: list[str]) -> None:
    conn = _db()
    print("全文索引")
    for r in conn.execute("SELECT status,COUNT(*),SUM(chars) FROM fulltext "
                          "GROUP BY status ORDER BY 2 DESC"):
        print(f"  {r[0]:10s} {r[1]:6d} 条　{r[2] or 0:,} 字")
    n_ok = conn.execute("SELECT COUNT(*) FROM fulltext WHERE status='ok'").fetchone()[0]
    if n_ok:
        avg = conn.execute("SELECT AVG(chars) FROM fulltext WHERE status='ok'").fetchone()[0]
        print(f"  平均 {avg:.0f} 字/份")

    print("\n清单（源端自报）")
    prog = _load_progress()
    for kind in kinds:
        p = LIST_DIR / f"govcn_list.{kind}.jsonl"
        n = sum(1 for _ in p.open(encoding="utf-8")) if p.exists() else 0
        st = prog.get(kind, {})
        print(f"  {KINDS[kind][2]:16s} 已枚举 {n:6d} 条 / 源端 {st.get('total', '?')}"
              f"　{'完成' if st.get('done') else '未完成'}")

    print("\n失败原因 Top")
    for r in conn.execute("SELECT reason,COUNT(*) c FROM fulltext WHERE status='failed' "
                          "GROUP BY substr(reason,1,40) ORDER BY c DESC LIMIT 8"):
        print(f"  {r[1]:5d}  {r[0]}")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="国家级政策全文库")
    ap.add_argument("--list", action="store_true", help="枚举清单")
    ap.add_argument("--fetch", action="store_true", help="抓全文")
    ap.add_argument("--stats", action="store_true", help="统计")
    ap.add_argument("--kinds", default="gw,bm")
    ap.add_argument("--pages", type=int, default=0, help="只翻前 N 页（试跑）")
    ap.add_argument("--limit", type=int, default=0, help="只抓 N 条（试跑）")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--retry-failed", action="store_true")
    a = ap.parse_args()
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]

    if a.list:
        cmd_list(kinds, pages=a.pages)
    if a.fetch:
        cmd_fetch(kinds, limit=a.limit, retry_failed=a.retry_failed,
                  workers=a.workers)
    if a.stats or not (a.list or a.fetch):
        cmd_stats(kinds)


if __name__ == "__main__":
    main()
