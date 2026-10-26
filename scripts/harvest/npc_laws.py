#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""解析《现行有效法律目录（303 件）》PDF，落成可检索的法律清单。

为什么只做目录不做正文：法律本体的权威源 `flk.npc.gov.cn` 整站接口被 WAF
拦着（`/law-search/*` 一律 307 到自身，需要执行 JS 拿 wzws 会话），零依赖
拿不下来。`www.npc.gov.cn` 首页挂的这份 PDF 是次优选择——它只有名称和法律
沿革（哪年通过、哪年修正过几次），没有条文。

但对「效力核验」这件事，这份东西解决的是最要紧的一问：**这部法到底还在不在
现行有效的名单里**。有了它，模型再说「依据《城乡规划法》」时，至少能查证这个
名字在 2026-08-28 的官方现行有效目录上存在，而不是全靠记忆。

用法
----
    python scripts/harvest/npc_laws.py            # 解析 + 导出 CSV + 入 catalog.db
    python scripts/harvest/npc_laws.py --stats    # 只看已入库的
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
PDF = ROOT / "data" / "law" / "npc_law_index.pdf"
CSV_OUT = ROOT / "data" / "law" / "现行有效法律目录.csv"
DB = ROOT / "data" / "catalog.db"

SOURCE = "全国人大·现行有效法律目录（303 件）"
SOURCE_URL = ("http://www.npc.gov.cn/c2597/c5854/c5855/202609/"
              "P020260904396146393500.pdf")
CUTOFF = "2026-08-28"

# 法律部门的小标题，长这样：「宪法相关法（56 件）」
_SECTION = re.compile(r"^(宪法相关法|民法商法|行政法|经济法|社会法|生态环境法|"
                      r"刑法|诉讼与非诉讼程序法|宪法及宪法相关法)\s*[（(]\s*\d+\s*件")
# 条目编号：「1．中华人民共和国…」——注意是全角句点，不是小数点
_NUM = re.compile(r"^(\d{1,3})[．.]\s*(中华人民共和国.*)$")
_YEARS = re.compile(r"(\d{4})\s*年")
# 括号里的沿革：「1979 年通过，1982 年修正，…」
_HIST = re.compile(r"[（(]\s*([^（()）]*?年[^（()）]*)[）)]")


def _lines() -> list[str]:
    from pypdf import PdfReader
    r = PdfReader(str(PDF))
    raw = "\n".join((p.extract_text() or "") for p in r.pages)
    # PDF 每行都在词中间断，直接拼回去；只有出现编号或小标题时才断句
    out, buf = [], ""
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.isdigit():
            continue
        if _NUM.match(s) or _SECTION.match(s) or s.startswith("中华人民共和国"):
            if buf:
                out.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        out.append(buf)
    return out


def parse() -> list[dict]:
    rows: list[dict] = []
    section = ""
    for line in _lines():
        m = _SECTION.match(line)
        if m:
            section = m.group(1)
            # 小标题后面可能还粘着第一个条目
            rest = _SECTION.sub("", line).strip()
            if rest:
                rows.extend(_one(rest, section, len(rows)))
            continue
        if line.startswith("中华人民共和国"):
            rows.extend(_one(line, section or "宪法", len(rows)))
            continue
        m = _NUM.match(line)
        if m:
            rows.extend(_one(m.group(2), section, len(rows)))
    return rows


def _one(text: str, section: str, idx: int) -> list[dict]:
    name = text
    hist = ""
    m = _HIST.search(text)
    if m:
        hist = m.group(1).strip()
        name = (text[:m.start()] + text[m.end():]).strip()
    name = re.sub(r"\s+", "", name)
    if not name.startswith("中华人民共和国") or len(name) < 6:
        return []
    years = [int(y) for y in _YEARS.findall(hist)]
    return [{
        "序号": idx + 1,
        "名称": name,
        "法律部门": section,
        "首次通过": min(years) if years else "",
        "最近修改": max(years) if years else "",
        "沿革": hist,
        "核验基准日": CUTOFF,
        "来源": SOURCE_URL,
    }]


def write_csv(rows: list[dict]) -> None:
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def to_db(rows: list[dict]) -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=rw", uri=True, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    now = time.time()
    new = 0
    for r in rows:
        # key 里必须带序号：《宪法》后面跟着 5 条《宪法修正案》，名字一模一样，
        # 只用名称当 key 会把 5 条压成 1 条（实测 308 条进库只剩 293 条）。
        key = f'law:{r["序号"]}:' + re.sub(r"[（(].*?[）)]", "", r["名称"])
        cur = conn.execute(
            "INSERT OR REPLACE INTO items (key,title,issuer,doc_no,doc_type,"
            "doc_type_src,region,region_src,admin_level,topics,published,"
            "effective,abolished,status,status_src,source,sources,source_type,"
            "source_name,url,summary,note,fetched_at,first_seen,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, r["名称"], "全国人民代表大会及其常务委员会", "", "法律",
             "官方目录", "全国", "官方目录", "国家", r["法律部门"],
             f'{r["最近修改"] or r["首次通过"] or ""}-01-01', "", "",
             "valid", "全国人大现行有效法律目录", SOURCE, SOURCE,
             "official_catalog", SOURCE, SOURCE_URL,
             f'{r["法律部门"]}；{r["沿革"]}', f'核验基准日 {CUTOFF}',
             now, now, now))
        new += cur.rowcount
    conn.commit()
    conn.close()
    return new


def stats() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    n = conn.execute("SELECT COUNT(*) FROM items WHERE source_type='official_catalog'"
                     ).fetchone()[0]
    print(f"库内法律条目 {n} 条")
    for r in conn.execute("SELECT topics,COUNT(*) FROM items "
                          "WHERE source_type='official_catalog' GROUP BY 1 "
                          "ORDER BY 2 DESC"):
        print(f"   {r[0] or '(未分类)':14s} {r[1]:4d}")
    # 这一栏是拿来验收的：查不到就说明目录没进库，或者这部法已被替代。
    print("\n规划口最常引的几部：")
    for kw in ("城乡规划法", "土地管理法", "建筑法", "文物保护法", "生态环境法典"):
        row = conn.execute("SELECT title,topics,summary FROM items WHERE title LIKE ? "
                           "AND source_type='official_catalog' LIMIT 1",
                           (f"%{kw}%",)).fetchone()
        mark = "✔ " + row[0][:30] if row else "✗ 不在现行有效目录（可能已被替代）"
        print(f"   {kw:10s} {mark}")
    print("\n注：查《环境保护法》会落到「不在目录」——它不是漏抓，2026 年"
          "《生态环境法典》通过后，它已不在现行有效名单里。这类判断正是这张表的价值。")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.stats:
        stats()
        return
    if not PDF.exists():
        sys.exit(f"缺 PDF：{PDF}")
    rows = parse()
    print(f"解析出 {len(rows)} 条")
    if len(rows) < 250:
        sys.exit("条目数明显不对（应接近 303），先看 PDF 提取有没有断行")
    write_csv(rows)
    print(f"CSV → {CSV_OUT}")
    print(f"入库 {to_db(rows)} 条")
    stats()


if __name__ == "__main__":
    main()
