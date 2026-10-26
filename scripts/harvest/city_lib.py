#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「已适配的城市政策库」整库抓进本地目录。

## 这个脚本解决的到底是什么问题

运行时联网只能一次抓十几份，用户问「广州城市更新有什么政策」，
拿到的永远是一个切片。真正的解法是把整库抓下来变成库里的东西：
之后所有关于广州的问题都是毫秒级的库内检索，而且能进失效台账、能算覆盖度。

广州实测：`gz_gfxwjk` 接口自报 6,989 份，带文号、发布机关、施行时间，
**还带源端自己的时效标记**（有效 / 失效 / 失效（提前废止））。
这份数据比我们从正文反推的台账硬——台账是「全库没有一份文件提到它被废止」，
源端标记是「发布机关说它失效了」。

## 为什么复用 app/websearch.py 的配置

配置只有一处。`CITY_LIBS` 里加一条，运行时联网和离线整库抓同时生效。
两份配置一定会分叉，分叉之后的表现是「网页上能查到、批量抓抓不到」，
那种 bug 查起来最费劲。

## 用法

    python scripts/harvest/city_lib.py --city 广州市 --stats
    python scripts/harvest/city_lib.py --city 广州市 --run            # 只抓目录
    python scripts/harvest/city_lib.py --city 广州市 --run --bodies   # 目录 + 正文

正文写进 `data/fulltext/<key>.txt`，和 `local_full.py` 的落盘格式一致，
之后跑 `build_index.py` 就能进 kb.db，不需要另写一条索引路径。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "app"))

from concurrent.futures import ThreadPoolExecutor

from catalog import net                                     # noqa: E402
from harvest.extract import extract                         # noqa: E402
from websearch import _doc_no                               # noqa: E402

CATALOG_DB = ROOT / "data" / "catalog.db"
FULL_DIR = ROOT / "data" / "fulltext"


def _cfg(city: str) -> tuple[dict, str]:
    from websearch import CITY_LIBS, _lib_of
    return _lib_of(city) if city else ({}, "")


def _ts(v) -> str:
    import datetime as dt
    if v in (None, "", 0):
        return ""
    if isinstance(v, str):
        m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", v)
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n > 1e11:
        n /= 1000
    if n < 9e8:
        return ""
    return dt.datetime.fromtimestamp(n).strftime("%Y-%m-%d")


def _page(cfg: dict, page: int, tries: int = 3) -> tuple[int, list, int, str]:
    """抓一页，返回 (页码, docs, total, 错误)。失败不抛，交给调用方决定。

    重试放在 worker 里做：并发批次里一页连着失败就整批中断的话，
    并发的好处全被抹掉，还会把「限流」误判成「源站挂了」。
    """
    u = (f"{cfg['api']}?limit={cfg['limit']}&time_sort_field=display_publish_time"
         f"&sort=time&page={page}")
    err = ""
    for attempt in range(1, tries + 1):
        try:
            txt = net.get(u, timeout=30, retries=1, referer=cfg["referer"],
                          insecure=True)
        except Exception as e:                    # net.FetchError 及底层超时
            err = str(e)
            if attempt < tries:
                time.sleep(3 * attempt)
            continue
        i, j = txt.find("("), txt.rfind(")")
        try:
            d = json.loads(txt[i + 1:j]) if i >= 0 else json.loads(txt)
        except json.JSONDecodeError:
            err = "返回不是 JSON"
            continue
        data = (d or {}).get("data") or {}
        return page, data.get("docs") or [], int(data.get("total") or 0), ""
    return page, [], 0, err


def list_all(city: str, pages: int = 0, interval: float = 1.0,
             start: int = 1, workers: int = 4) -> list[dict]:
    """翻完全部页，返回条目清单。pages=0 表示翻到没有为止。

    `workers` 是并发抓页数。单页实测稳定 11 秒，但那是 TLS 握手与
    等响应的开销，不是源站扛不住——4 页并发 3.25 秒就跑完，串行要 44 秒。
    6,989 份的全量抓取因此从 1 小时压到几分钟。
    """
    cfg, key = _cfg(city)
    if not cfg:
        raise SystemExit(f"「{city}」没有适配的城市政策库。"
                         f"先在 app/websearch.py 的 CITY_LIBS 里加一条。")
    f, ef = cfg["f"], cfg["ext"]

    def _pack_one(it: dict) -> dict | None:
        url = it.get(f["url"]) or ""
        title = (it.get(f["title"]) or "").strip()
        if not url or not title:
            return None
        # json_ext 不一定是对象：实测第 116 页有一条「政策解读」是 []，
        # 直接 ext.get() 抛 AttributeError，把整库抓取进程打死在 1515/6989。
        # 一份脏数据不该毁掉整轮任务，非 dict 一律当空元数据。
        ext = it.get(f["ext"]) or {}
        if isinstance(ext, str):
            try:
                ext = json.loads(ext)
            except json.JSONDecodeError:
                ext = {}
        if not isinstance(ext, dict):
            ext = {}
        st = str(ext.get(ef["status"]) or "")
        return {"title": title, "url": url,
                "doc_no": _doc_no(ext, cfg),
                "issuer": str(ext.get(ef["issuer"]) or ""),
                "published": _ts(it.get(f["ts"])),
                "eff_state": st,
                "eff_hint": ("repealed" if any(w in st for w in cfg["dead"])
                             else "ok") if st else "",
                "eff_end": _ts(ext.get(ef["end"]))}

    out: dict[str, dict] = {}
    again: list[int] = []          # 本批失败、末尾统一补抓的页码
    total, stop_at = 0, None
    page = max(1, start)
    n_pages = 0
    while True:
        window = list(range(page, page + max(1, workers)))
        batch = window
        # 按「实际翻了几页」算，不按页码算：续抓时 page 从 116 起，
        # 用 page >= pages 判，--pages 2 会在第 116 页就成立，等于没生效。
        if pages:
            batch = batch[:max(0, pages - n_pages)]
        if stop_at is not None:
            batch = [p for p in batch if p < stop_at]
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            res = sorted(ex.map(lambda p: _page(cfg, p), batch))
        dead = 0
        for pg, docs, tot, err in res:
            if tot and not total:
                total = tot
                print(f"  源端自报 {total} 份")
            if err:
                # 单页失败不能只是 continue：那一页的 20 条就永久消失了，
                # 而整库抓取是后台任务，没人会发现库里少了 20 条。
                print(f"  第 {pg} 页失败：{err}")
                dead += 1
                again.append(pg)
                continue
            if not docs:
                # 空页说明翻到底了。同一批里页码更高的也不必再抓。
                if stop_at is None or pg < stop_at:
                    stop_at = pg
                continue
            for it in docs:
                row = _pack_one(it)
                if row:
                    out[row["url"]] = row
            n_pages += 1
            print(f"  第 {pg} 页 +{len(docs)}，累计 {len(out)}/{total or '?'}")
        # 推进到窗口末尾，不按 batch 长度推进：batch 被 stop_at 过滤过，
        # 用它算会往回退，同一页被反复抓。
        page = window[-1] + 1
        if dead == len(batch):
            print(f"  连续 {dead} 页失败，已抓 {len(out)} 条。")
            print(f"  续抓：--run --start {page}（已入目录的部分不会重复）")
            break
        if total and len(out) >= total:
            break
        time.sleep(interval)

    # 失败页补抓。两轮，间隔拉长——失败基本都是限流，硬扛只会更糟。
    for rnd in range(2):
        if not again:
            break
        print(f"  补抓 {len(again)} 个失败页（第 {rnd + 1} 轮）…")
        time.sleep(5 * (rnd + 1))
        left: list[int] = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(again)))) as ex:
            res = sorted(ex.map(lambda p: _page(cfg, p, tries=4), again))
        for pg, docs, tot, err in res:
            if err:
                print(f"  第 {pg} 页仍失败：{err}")
                left.append(pg)
                continue
            for it in docs:
                row = _pack_one(it)
                if row:
                    out[row["url"]] = row
            n_pages += 1
            print(f"  第 {pg} 页(补) +{len(docs)}，累计 {len(out)}/{total or '?'}")
        again = left
    if again:
        print(f"  仍缺 {len(again)} 页：{again[:12]}"
              f"{' …' if len(again) > 12 else ''}")
        print(f"  单页补：--run --start {again[0]} --pages 1")
    return list(out.values())


def _level_of(name: str) -> str:
    if name.endswith(("省", "自治区")) or name in ("北京市", "天津市", "上海市", "重庆市"):
        return "省级"
    if re.search(r"(区|县|旗)$", name) or "自治县" in name:
        return "县级"
    return "市级"


def save_catalog(city: str, rows: list[dict]) -> tuple[int, int]:
    """写进 catalog.db 的 items 表。

    用 `CatalogItem` 生成 key，不自己另造一个——目录层的去重键是
    「有文号用文号、没文号用折叠标题」，跟 `local_catalog.py` 抓的
    走的是同一套。另造一套的话，同一个文件会被两个来源各插一条，
    检索时在界面上出现两次（标题一模一样、链接不同），然后有人要花
    半天去查「为什么重复」。
    """
    from catalog.model import CatalogItem

    # 60 秒锁等待 + 分批提交。这两个都不是「顺手加的参数」：
    # 广州整库抓完一次写 6,080 条，默认 5 秒锁等待，撞上另一个写入进程
    # （当时上海正在写 fulltext）就整批失败——14 分钟抓取白跑，且这是
    # 后台任务，没人会盯着最后一行报错。
    # 分批提交同时把持锁时间切碎，让并发写入方能插进来。
    con = sqlite3.connect(CATALOG_DB, timeout=60)
    n_new = n_upd = 0
    try:
        con.execute("PRAGMA busy_timeout=60000")
        have = {r[0] for r in con.execute("SELECT key FROM items")}
        for i, r in enumerate(rows, 1):
            it = CatalogItem(
                title=r["title"], issuer=r.get("issuer") or "",
                doc_no=r.get("doc_no") or "", published=r.get("published") or "",
                url=r["url"], region=city,
                source=f"city_lib:{city}", source_type="official_json",
                source_name=f"{city}·政策文件库",
            ).normalize()
            k = it.key
            if k in have:
                con.execute(
                    "UPDATE items SET title=?, doc_no=?, published=?, url=?, "
                    "status=?, status_src=?, last_seen=? WHERE key=?",
                    (it.title, it.doc_no, it.published, it.url,
                     "已废止" if r.get("eff_hint") == "repealed"
                     else (r.get("eff_state") or "未知"),
                     "源端标记" if r.get("eff_state") else "",
                     time.strftime("%Y-%m-%d"), k))
                n_upd += 1
            else:
                have.add(k)
                con.execute(
                    "INSERT INTO items (key, title, issuer, doc_no, doc_type, region, "
                    "region_src, admin_level, topics, published, status, status_src, "
                    "source, source_type, source_name, url, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (k, it.title, it.issuer, it.doc_no, it.doc_type, it.region,
                     it.region_src, _level_of(city),
                     json.dumps(it.topics, ensure_ascii=False),
                     it.published,
                     "已废止" if r.get("eff_hint") == "repealed"
                     else (r.get("eff_state") or "未知"),
                     "源端标记" if r.get("eff_state") else "",
                     it.source, it.source_type, it.source_name, it.url,
                     time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d")))
                n_new += 1
            if i % 500 == 0:
                con.commit()
        con.commit()
    finally:
        con.close()
    return n_new, n_upd


def save_bodies(city: str, rows: list[dict], workers: int = 8,
                limit: int = 0) -> dict:
    """抓正文落盘并登记进 fulltext 表。

    ## 落盘路径必须和 local_full.py 完全一致

    `build_index.py` 读的是 `data/fulltext/<key前2位>/<key>.txt`，清单来自
    `fulltext` 表。这两条任何一条对不上，抓下来的正文就永远进不了索引——
    而且不会有任何报错：`build_index` 只是「待索引 0 份」，看起来像没事。

    最初这版就是直接写 `data/fulltext/<key>.txt`、不写表，差点让广州
    六千里抓下来的正文整批作废。复用 `local_full.key_of / ft_path / _save`，
    不另写一套。

    实测正文页 0.4—0.8 秒（API 分页要 11 秒，因为服务端要做全文检索排序），
    所以并发开到 8，6,000 份约十分钟。

    返回分类统计。三种失败必须分开：抓不到、抓到了但抽不出正文、正文太短。
    混成一个数字，后面判「这个库覆盖率 30%」就不知道是源站没内容还是我们没抓到。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from harvest.local_full import _db, _save, ft_path, key_of

    todo = rows[:limit] if limit else rows
    stat = {"ok": 0, "cached": 0, "fetch_fail": 0, "no_text": 0, "short": 0}

    def one(r: dict) -> tuple[dict, str, int, str]:
        k = key_of(r["url"])
        p = ft_path(k)
        if p.exists() and p.stat().st_size > 500:
            # 已落盘也要登记，而且 chars 必须是真实字数：
            # 文件字节数不能当字数用（中文 UTF-8 三字节一字），
            # 这个字段会直接进 docs.chars，界面按它排「哪份文件更厚」。
            try:
                n = len(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                n = 0
            return r, "cached", n, ""
        try:
            html = net.get(r["url"], timeout=25, retries=2, insecure=True)
        except Exception:                                  # noqa: BLE001
            return r, "fetch_fail", 0, ""
        text, method, _why = extract(html)
        text = (text or "").strip()
        if not text:
            return r, "no_text", 0, ""
        if len(text) < 300:
            return r, "short", len(text), ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return r, "ok", len(text), method or ""

    conn = _db()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, r) for r in todo]
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    r, why, n, method = f.result()
                except Exception:                          # noqa: BLE001
                    continue
                stat[why] = stat.get(why, 0) + 1
                k = key_of(r["url"])
                # 失败也要登记。不登记的话下次跑还是「没有记录」，无法区分
                # 「没试过」和「试过、源站没有正文」。
                _save(conn, {
                    "key": k, "title": r.get("title", ""),
                    "doc_no": r.get("doc_no", ""), "published": r.get("published", ""),
                    "source_name": r.get("issuer", ""), "url": r["url"],
                    "path": str(ft_path(k)) if why in ("ok", "cached") else "",
                    "chars": n, "method": method,
                    "status": "ok" if why in ("ok", "cached") else
                              ("fail" if why == "fetch_fail" else "empty"),
                    "reason": "" if why in ("ok", "cached") else why,
                })
                if i % 200 == 0:
                    conn.commit()
                    print(f"  正文 {i}/{len(todo)}：成功 {stat['ok']}，"
                          f"已有 {stat['cached']}，抓不到 {stat['fetch_fail']}，"
                          f"抽不出 {stat['no_text']}，过短 {stat['short']}")
        conn.commit()
    finally:
        conn.close()
    return stat


def city_rows(city: str) -> list[dict]:
    """从目录库读该城市的全部条目，供抓正文用。

    带文号/机关/日期，因为 `fulltext` 表要登记这些字段——索引时
    `docs` 表的 doc_no、issuer 就是从这里来的，缺了核验时引不出完整出处。
    """
    con = sqlite3.connect(CATALOG_DB)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            "SELECT title, url, doc_no, issuer, published FROM items "
            "WHERE region=? AND url LIKE 'http%'", (city,))
        return [{"title": r["title"], "url": r["url"],
                 "doc_no": r["doc_no"] or "", "issuer": r["issuer"] or "",
                 "published": r["published"] or ""} for r in cur]
    finally:
        con.close()


def stats(city: str) -> None:
    con = sqlite3.connect(CATALOG_DB)
    con.row_factory = sqlite3.Row
    try:
        n = con.execute("SELECT count(*) FROM items WHERE region=?",
                        (city,)).fetchone()[0]
        print(f"{city} 目录 {n} 条")
        for r in con.execute("SELECT source_name, count(*) c FROM items "
                             "WHERE region=? GROUP BY source_name ORDER BY c DESC",
                             (city,)):
            print(f"  {r['c']:5d}  {r['source_name']}")
        kb = sqlite3.connect(ROOT / "data" / "kb.db")
        try:
            m = kb.execute("SELECT count(*) FROM docs WHERE region=?",
                           (city,)).fetchone()[0]
            print(f"{city} 全文 {m} 份")
        finally:
            kb.close()
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="广州市")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--bodies", action="store_true", help="同时抓正文")
    ap.add_argument("--bodies-only", action="store_true",
                    help="跳过目录抓取，直接给库里该城市的条目补正文")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 条（调试用）")
    ap.add_argument("--pages", type=int, default=0, help="只翻 N 页（0 = 全部）")
    ap.add_argument("--start", type=int, default=1,
                    help="从第 N 页开始（限流中断后用它续抓）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--interval", type=float, default=1.0,
                    help="翻页间隔秒。目标站被限流时调大，别硬扛")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.stats:
        stats(args.city)
        return
    if not (args.run or args.bodies_only):
        cfg, key = _cfg(args.city)
        print(f"{args.city} → {cfg.get('name') or '(未适配)'}")
        if cfg:
            print(f"  接口 {cfg['api']}")
            print(f"  时效字段 {cfg['ext']['status']}，判定失效的关键词 {cfg['dead']}")
        return

    t0 = time.time()
    if args.bodies_only:
        rows = city_rows(args.city)
        print(f"{args.city} 库内 {len(rows)} 条，开始抓正文")
    else:
        rows = list_all(args.city, pages=args.pages, interval=args.interval,
                        start=args.start, workers=args.workers)
        print(f"\n目录 {len(rows)} 条，用时 {time.time() - t0:.0f}s")
        if not rows:
            return
        n_new, n_upd = save_catalog(args.city, rows)
        print(f"入库：新增 {n_new}，更新 {n_upd}")
        n_dead = sum(1 for r in rows if r["eff_hint"] == "repealed")
        print(f"源端标记失效 {n_dead} 份（{n_dead / len(rows) * 100:.1f}%）")

    if args.bodies or args.bodies_only:
        # 抓正文用「库里该城市的全部条目」，不用本次抓的这批。
        # 分页续抓时 rows 只有一段，只抓这一段的话，前面几批永远没正文，
        # 而没人会记得回去补——最后就是「目录有 6,000 条、正文只有 1,500 份」。
        todo = city_rows(args.city) if not args.bodies_only else rows
        print(f"抓正文 {len(todo)} 份…")
        st = save_bodies(args.city, todo, args.workers, args.limit)
        print(f"正文：新增 {st['ok']}，已有 {st['cached']}，"
              f"抓不到 {st['fetch_fail']}，抽不出正文 {st['no_text']}，"
              f"过短 {st['short']}")
        print("下一步：python scripts/harvest/build_index.py 把它并进 kb.db")

    stats(args.city)


if __name__ == "__main__":
    main()
