#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上海市统一政策发布平台：整库抓取（目录 + 正文）。

## 为什么单独写一个脚本

广州那套 `city_lib.py` 是 GET + JSONP，字段藏在 `json_ext` 里还要按
`_gz_zcwjk_` 前缀取。上海是 **POST + 扁平 JSON**，参数名、分页名、时效字段
全不一样，硬塞进同一个 `_page()` 只会让两边都变形。

所以这里只做「取数」，落盘和入库复用同一套实现：
`city_lib.save_catalog` 写目录，`local_full._save / ft_path / key_of`
写正文并按 `data/fulltext/<key前2位>/<key>.txt` 落盘。

**落盘路径和 fulltext 表登记必须和 local_full / build_index 完全一致。**
对不上的时候不会报错，`build_index` 只会说「待索引 0 份」。

## 接口

    POST /gwk/policy/page    {pageNum, pageSize}      列表
    POST /gwk/policy/detail  {businessId, siteId}     详情（带正文）

详情必须同时给 `businessId` 和 `siteId`。只给 businessId 时接口返回
`{"msg":"success","code":0,"data":{}}`——**成功但空**，很容易被当成
「这份文件没有正文」。这个坑在这里记一笔。

## 数据质量

比广州库好：除了文号、施行/失效日期，还带
- `effectiveFlag` 源端时效（有效 / 失效）
- `evolutionList` 沿革（这份文件取代了哪一份）
- `relates` 关联的政策解读

列表里混着 `test_` 开头的测试记录，必须过滤。

## 用法

    python scripts/harvest/sh_lib.py --run                  # 目录 + 正文
    python scripts/harvest/sh_lib.py --run --catalog-only   # 只抓目录
    python scripts/harvest/sh_lib.py --bodies-only --limit 50
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "app"))

from harvest.extract import extract                         # noqa: E402
from harvest.local_full import _db, _save, ft_path, key_of  # noqa: E402

BASE = "https://www.shanghai.gov.cn"
API_LIST = BASE + "/gwk/policy/page"
API_DETAIL = BASE + "/gwk/policy/detail"
CITY = "上海市"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_CTX.options |= 0x00020000                     # 允许旧版重协商


def _post(url: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Referer": BASE + "/zhengce/"})
    raw = urllib.request.urlopen(req, timeout=timeout, context=_CTX).read()
    return json.loads(raw.decode("utf-8", "replace"))


def _is_test(rec: dict) -> bool:
    return str(rec.get("businessId") or "").startswith("test")


def _docno(rec: dict) -> str:
    """沪交行规 + 2026 + 13 → 沪交行规〔2026〕13号。三段缺一不可。"""
    t = str(rec.get("docType") or "").strip()
    y = str(rec.get("docYear") or "").strip()
    n = str(rec.get("docNo") or "").strip()
    if t and y and n:
        return f"{t}〔{y}〕{n}号"
    return ""


# 发布机关在标题开头：「上海市交通委员会关于印发…的通知」
_ISSUER = re.compile(r"^(上海市[^关的发]{2,20}?(?:委员会|局|办公室|人民政府|厅|"
                     r"管理委员会|中心|协会|集团))(?=关于|印发|发布)")
_DEAD = ("失效", "废止", "无效", "已修改")


def _row(rec: dict) -> dict | None:
    title = (rec.get("title") or "").strip()
    biz = str(rec.get("businessId") or "").strip()
    if not title or not biz:
        return None
    eff = str(rec.get("effectiveFlag") or "").strip()
    m = _ISSUER.search(title)
    return {
        "title": title,
        # url 用目录层原来的形状 zhengce/list#<id>，这样同一份文件在两个来源
        # 里算同一个 key，不会在界面上出现两次。
        "url": f"{BASE}/zhengce/list#{biz}",
        "doc_no": _docno(rec),
        "issuer": m.group(1) if m else "",
        "published": (rec.get("publishDate") or "")[:10],
        "eff_state": eff,
        "eff_hint": ("repealed" if any(w in eff for w in _DEAD)
                     else "ok") if eff else "",
        "eff_end": (rec.get("invalidDate") or "")[:10],
        "biz": biz,
        "site": str(rec.get("siteId") or ""),
    }


def _page(n: int, size: int = 100) -> tuple[list[dict], int]:
    """取第 n 页。

    分页参数名是 `pageNo`，**不是 `pageNum`**。写错的时候接口不报错，
    静默忽略、永远返回第 1 页——表现是「翻了 106 页只拿到 95 条」。
    这个坑差点让整库抓取变成抓同一页 106 次，靠 url 去重才没入库一万条
    重复数据。响应体里的字段名（pageNo）就是线索。
    """
    d = _post(API_LIST, {"pageNo": n, "pageSize": size}).get("data") or {}
    return d.get("records") or [], int(d.get("totalCount") or 0)


def list_all(workers: int = 4, interval: float = 0.3) -> list[dict]:
    first, total = _page(1)
    if not first:
        raise SystemExit("列表接口没有返回记录")
    n_page = (total + 99) // 100
    print(f"  源端自报 {total} 份，{n_page} 页")

    out: dict[str, dict] = {}
    for rec in first:
        if not _is_test(rec):
            r = _row(rec)
            if r:
                out[r["url"]] = r

    pages = list(range(2, n_page + 1))
    done = 0
    dup_streak = 0
    while pages:
        batch, pages = pages[:workers], pages[workers:]
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            futs = {ex.submit(_page, p): p for p in batch}
            for f in as_completed(futs):
                p = futs[f]
                try:
                    recs, _t = f.result()
                except Exception as e:                     # noqa: BLE001
                    print(f"  第 {p} 页失败：{str(e)[:60]}")
                    continue
                before = len(out)
                for rec in recs:
                    if _is_test(rec):
                        continue
                    r = _row(rec)
                    if r:
                        out[r["url"]] = r
                # 分页参数写错时接口不报错，只是把第 1 页重复给你。
                # 没有这道闸，整库抓取会「成功」跑完，而库里只有 95 条。
                dup_streak = dup_streak + 1 if len(out) == before else 0
                if dup_streak >= 3:
                    print(f"  ! 连续 {dup_streak} 页没有新增（已抓 {len(out)} 条 / "
                          f"源端 {total}），分页参数可能失效，中止")
                    return list(out.values())
                done += 1
        if done % 20 < workers:
            print(f"  已翻 {done}/{n_page - 1} 页，累计 {len(out)} 条")
        time.sleep(interval)
    return list(out.values())


def pull_bodies(rows: list[dict], workers: int = 8,
                limit: int = 0) -> dict:
    """逐条调详情接口取正文。

    正文在 `txt` 字段里，是一段 HTML 片段（不是整页），`extract()` 能直接吃，
    段落和「第一条（目的和依据）」这类结构都保留得住。
    """
    todo = rows[:limit] if limit else rows
    stat = {"ok": 0, "cached": 0, "no_text": 0, "fetch_fail": 0, "short": 0}

    def one(r: dict) -> tuple[dict, str, int, str]:
        k = key_of(r["url"])
        p = ft_path(k)
        if p.exists() and p.stat().st_size > 300:
            try:
                return r, "cached", len(p.read_text(encoding="utf-8",
                                                    errors="replace")), ""
            except OSError:
                return r, "cached", 0, ""
        try:
            d = _post(API_DETAIL, {"businessId": r["biz"], "siteId": r["site"]})
        except Exception:                                  # noqa: BLE001
            return r, "fetch_fail", 0, ""
        raw = str((d.get("data") or {}).get("txt") or "")
        if not raw.strip():
            # code=0 + data={} 多半是 siteId 对不上，不是「这份文件没正文」。
            return r, "no_text", 0, ""
        text, method, _why = extract(raw)
        text = (text or "").strip()
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
                _save(conn, {
                    "key": k, "title": r.get("title", ""),
                    "doc_no": r.get("doc_no", ""),
                    "published": r.get("published", ""),
                    "source_name": r.get("issuer", ""), "url": r["url"],
                    "path": str(ft_path(k)) if why in ("ok", "cached") else "",
                    "chars": n, "method": method,
                    "status": "ok" if why in ("ok", "cached") else
                              ("fail" if why == "fetch_fail" else "empty"),
                    "reason": "" if why in ("ok", "cached") else why,
                })
                if i % 500 == 0:
                    conn.commit()
                    print(f"  正文 {i}/{len(todo)}：成功 {stat['ok']}，"
                          f"已有 {stat['cached']}，无正文 {stat['no_text']}，"
                          f"抓不到 {stat['fetch_fail']}，过短 {stat['short']}")
        conn.commit()
    finally:
        conn.close()
    return stat


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--catalog-only", action="store_true")
    ap.add_argument("--bodies-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if not (args.run or args.bodies_only):
        print("用法：--run 抓目录和正文；--bodies-only 只补正文")
        return

    from city_lib import city_rows, save_catalog

    t0 = time.time()
    if args.bodies_only:
        rows = city_rows(CITY)
        print(f"库内 {len(rows)} 条")
    else:
        print("抓目录…")
        rows = list_all()
        n_new, n_upd = save_catalog(CITY, rows)
        print(f"目录 {len(rows)} 条，用时 {time.time() - t0:.0f}s；"
              f"入库新增 {n_new}，更新 {n_upd}")
        n_dead = sum(1 for r in rows if r["eff_hint"] == "repealed")
        print(f"源端标记失效 {n_dead} 份（{n_dead / len(rows) * 100:.1f}%）")
        if args.catalog_only:
            return

    t1 = time.time()
    print(f"抓正文 {len(rows)} 份…")
    st = pull_bodies(rows, args.workers, args.limit)
    print(f"正文：新增 {st['ok']}，已有 {st['cached']}，"
          f"接口无正文 {st['no_text']}，抓不到 {st['fetch_fail']}，"
          f"过短 {st['short']}；用时 {time.time() - t1:.0f}s")
    print("下一步：python scripts/harvest/build_index.py --build")


if __name__ == "__main__":
    main()
