#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把全文库切成可检索的块，建 FTS5 索引。

为什么用 SQLite FTS5 而不是向量库
--------------------------------
1. 零依赖。项目从第一天就要求「拷走就能跑」，不装 Docker、不装 embedding 服务。
2. **中文 2 字词必须能查**。「征收」「补偿」「用地」这类词是规划场景的日常查询，
   而 FTS5 的 trigram 分词要求查询至少 3 个字符——两字词直接失效。所以这里
   自己做 2-gram 切分，用 unicode61 索引：文本 → 「征收 收补 补偿」，
   查询「补偿」→ 「补偿」，命中。
3. BM25 是 FTS5 内置排序，不用自己实现。

为什么切块而不是整篇索引
------------------------
一份规划文本动辄上万字，整篇命中后没法定位到「第几条」。按「第X条」切，
命中就能直接给到条号——这正是这个产品要交付的东西（可溯源的引用）。

用法
----
    python scripts/harvest/build_index.py                 # 建索引
    python scripts/harvest/build_index.py --limit 500     # 先拿 500 份试
    python scripts/harvest/build_index.py --search 补偿标准
    python scripts/harvest/build_index.py --stats
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CATALOG_DB = ROOT / "data" / "catalog.db"
KB_DB = ROOT / "data" / "kb.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    id      INTEGER PRIMARY KEY,
    doc_key TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    kind    TEXT DEFAULT 'para',      -- art（第X条）/ para（段落）
    label   TEXT DEFAULT '',          -- 第X条 / 章节名
    title   TEXT DEFAULT '',
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS i_b_doc ON blocks(doc_key);
CREATE INDEX IF NOT EXISTS i_b_label ON blocks(label);

CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    bigram,
    tokenize='unicode61 remove_diacritics 0'
);

CREATE TABLE IF NOT EXISTS docs (
    doc_key  TEXT PRIMARY KEY,
    title    TEXT, doc_no TEXT, issuer TEXT, published TEXT,
    region   TEXT, url TEXT, chars INTEGER, n_blocks INTEGER,
    indexed_at REAL
);
"""

# 条文行：「第十二条」「第十二条之一」
_ART = re.compile(r"^\s*第\s*([一二三四五六七八九十百千零〇0-9]{1,8})\s*条"
                  r"(?:之[一二三四五六七八九十]{1,3})?\s*[　 ]*(.*)$")
# 章节行：「第三章」「第二节」「一、」「（一）」
_CHAP = re.compile(r"^\s*(第[一二三四五六七八九十]{1,4}[章节]|"
                   r"[一二三四五六七八九十]{1,3}、|（[一二三四五六七八九十]{1,3}）)\s*(.{0,40})$")

MAX_BLOCK = 900        # 单块上限（字）。超过就按段落再切
MIN_BLOCK = 40         # 太短的块（页眉、文号行）单独成块没有检索价值


def bigrams(text: str) -> str:
    """中文切 2-gram，英文数字按词。用于建索引与查询，两边必须一致。"""
    out = []
    for tok in re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z0-9]+", text):
        if tok[0] >= "\u4e00":                     # 中文：滑窗 2 字
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok.lower())
    return " ".join(out)


def _resplit_long_lines(text: str) -> str:
    """把「一行到底」的正文重新断成条款行。

    有的站正文容器里只有文本节点和 `<br>`，没有 `<p>` / `<div>`，
    `html_to_text` 抽出来就是一行两万字。按行切块的逻辑（`split_blocks`）
    这时会把它整份当成一个块——检索命中时给出两万字，定位不到任何一条，
    等同于一整份文件没法引用。实测《广州市城市更新办法配套文件》
    （19,149 字、含五个办法）就是这样：切出 1 块、n_art=0。

    断行只在「条号前面紧跟句末标点或空白」时做。正文中间那个
    「按照第三条规定办理」的「第三条」前面是「照」，不是标点，不会被切断——
    这是这条规则唯一的难点，判错会把一句话劈成两半。
    """
    out = []
    for ln in text.split("\n"):
        if len(ln) <= 200:
            out.append(ln)
            continue
        # 第一种挤行：「第一章 总则第一条 为了…」——条号前面是「则」不是标点，
        # 光靠标点后断行会把第一条漏掉（整行开头是「第一章」，_ART 也匹配不上，
        # 最终掉进 preamble 被丢掉）。先把章标题和条号拆开。
        ln = re.sub(
            r"(第[一二三四五六七八九十]{1,4}[章节][^。；\n]{0,30}?)"
            r"(第[一二三四五六七八九十百千零〇0-9]{1,8}条)",
            r"\1\n\2", ln)
        parts = re.split(
            r"(?<=[。；：！？」』\s])"
            r"(?=(?:第[一二三四五六七八九十百千零〇0-9]{1,8}条"
            r"|第[一二三四五六七八九十]{1,4}[章节]))", ln)
        for p in parts:
            for q in p.split("\n"):
                if q.strip():
                    out.append(q.strip())
    return "\n".join(out)


def split_blocks(text: str) -> list[tuple[str, str, str]]:
    """正文 → [(kind, label, 块文本)]。

    优先按「第X条」切；没有条文体例的（规划、方案、通知）按段落聚合成
    不超过 MAX_BLOCK 的块，块开头带上最近的章节标题，这样命中时能看出位置。
    """
    lines = [ln.strip() for ln in _resplit_long_lines(text).split("\n") if ln.strip()]
    arts: list[tuple[str, str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    preamble: list[str] = []
    chap = ""

    for ln in lines:
        m = _ART.match(ln)
        if m:
            if cur:
                arts.append((cur[0], chap, cur[1]))
            label = f"第{m.group(1)}条"
            cur = (label, [m.group(2)] if m.group(2) else [])
            continue
        mc = _CHAP.match(ln)
        if mc and len(ln) <= 40:
            chap = ln
            continue
        if cur:
            cur[1].append(ln)
        else:
            preamble.append(ln)
    if cur:
        arts.append((cur[0], chap, cur[1]))

    if arts:
        out = []
        for label, chapter, body in arts:
            t = "\n".join(body).strip()
            if not t:
                continue
            out.append(("art", f"{chapter} {label}".strip(), t))
        return out

    # 无条文体例：按段落聚合
    out, buf = [], []
    for ln in lines:
        buf.append(ln)
        if sum(len(x) for x in buf) >= MAX_BLOCK:
            out.append(("para", "", "\n".join(buf)))
            buf = []
    if buf:
        out.append(("para", "", "\n".join(buf)))
    return out


def build(limit: int = 0, rebuild: bool = False) -> None:
    src = sqlite3.connect(CATALOG_DB)
    src.row_factory = sqlite3.Row
    # region 原来是写死「国家」——因为最初 fulltext 表里只有 gov.cn 政策文件库的
    # 18,608 份，每份都是国家级。2026-10 补抓了地方正文（local_full.py），
    # 写死就不成立了：南京的条例会被标成「国家」，检索时冒充上位法。
    # fulltext 表本身没有 region 列，所以按 url 回 items 查一次；查不到才用
    # 「国家」（地方条目一定在 items 里，国家对不上就是国家对）。
    url_region: dict[str, str] = {}
    host_region: dict[str, str] = {}
    for r in src.execute(
            "SELECT url, region FROM items WHERE coalesce(url,'') <> ''"):
        u = r["url"] or ""
        url_region.setdefault(u, r["region"] or "")
        # 同一份正文可能来自两次不同的采集，url 规则不一样，按 url 精确匹配
        # 一定有一部分对不上：上海正文走 /gwk/policy 接口拿到的是
        # `zhengce/list#<businessId>`，而目录里是 `zhengce/list#<另一批 hash>`，
        # 一个都对不上。对不上时退回「国家」，2026-10-25 实测把 8,903 份
        # 上海文件全标成了国家——地域检索查上海返回 0 条，而国家方向的
        # 召回里混进上海文件。所以再加一层：按域名投票。
        host = urlparse(u).netloc.lower()
        if host and r["region"]:
            host_region.setdefault(host, r["region"])

    rows = list(src.execute(
        "SELECT f.key, f.title, f.doc_no, f.issuer, f.published, f.url, f.chars "
        "FROM fulltext f WHERE f.status='ok' ORDER BY f.chars DESC"))
    def _region_of(r: dict) -> str:
        u = r["url"] or ""
        reg = url_region.get(u) or ""
        if reg:
            return reg
        host = urlparse(u).netloc.lower()
        reg = host_region.get(host) or ""
        if reg:
            return reg
        # 国家级正文不进 items，只在 fulltext 里，所以只剩这一档兜底。
        # 判据是域名本身：www.gov.cn 才是中央文件库。
        # 这里的 default 不能写成「国家」——那会让任何 url 对不上的地方文件
        # 都冒充上位法，是这个项目最不能犯的错（见 PRD N11 同源问题）。
        return "国家" if host in ("www.gov.cn", "gov.cn") else ""

    rows = [dict(r, region=_region_of(r)) for r in rows]
    if limit:
        rows = rows[:limit]
    src.close()

    KB_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(KB_DB)
    db.executescript(SCHEMA)
    if rebuild:
        db.execute("DELETE FROM blocks")
        db.execute("DELETE FROM blocks_fts")
        db.execute("DELETE FROM docs")
        db.commit()

    done = {r[0] for r in db.execute("SELECT doc_key FROM docs")}
    todo = [r for r in rows if r["key"] not in done]
    print(f"待索引 {len(todo)} 份（已索引 {len(done)} 份）")
    if not todo:
        _stats(db)
        db.close()
        return

    t0 = time.time()
    n_blocks = 0
    for i, r in enumerate(todo, 1):
        p = ROOT / "data" / "fulltext" / r["key"][:2] / f"{r['key']}.txt"
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        blocks = split_blocks(text)
        batch_rows, batch_fts = [], []
        for seq, (kind, label, body) in enumerate(blocks):
            if len(body) < MIN_BLOCK and kind == "para":
                continue
            batch_rows.append((r["key"], seq, kind, label, r["title"], body))
            batch_fts.append(bigrams(f"{r['title']} {label} {body}"))
        cur = db.execute(
            "INSERT INTO blocks (doc_key,seq,kind,label,title,text) VALUES (?,?,?,?,?,?)",
            batch_rows[0]) if batch_rows else None
        if batch_rows:
            first_id = cur.lastrowid
            ids = [first_id]
            for row in batch_rows[1:]:
                ids.append(db.execute(
                    "INSERT INTO blocks (doc_key,seq,kind,label,title,text) "
                    "VALUES (?,?,?,?,?,?)", row).lastrowid)
            db.executemany("INSERT INTO blocks_fts (rowid,bigram) VALUES (?,?)",
                           list(zip(ids, batch_fts)))
        db.execute(
            "INSERT OR REPLACE INTO docs (doc_key,title,doc_no,issuer,published,"
            "region,url,chars,n_blocks,indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["key"], r["title"], r["doc_no"], r["issuer"], r["published"],
             r["region"], r["url"], r["chars"], len(batch_rows), time.time()))
        n_blocks += len(batch_rows)
        if i % 200 == 0:
            db.commit()
            el = time.time() - t0
            print(f"  {i}/{len(todo)}　{len(batch_rows)} 块/份　"
                  f"{n_blocks} 块　{el / i:.2f}s/份　剩余约 {(len(todo) - i) * el / i / 60:.1f} 分钟")
    db.commit()
    print(f"完成：{len(todo)} 份，{n_blocks} 块，{(time.time() - t0) / 60:.1f} 分钟")
    _stats(db)
    db.close()


def _stats(db: sqlite3.Connection) -> None:
    n_doc = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    n_blk = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    if not n_doc:
        print("索引为空")
        return
    avg = db.execute("SELECT AVG(n_blocks) FROM docs").fetchone()[0]
    chars = db.execute("SELECT SUM(chars) FROM docs").fetchone()[0]
    print(f"\n索引：{n_doc:,} 份文件，{n_blk:,} 块，平均 {avg:.1f} 块/份，"
          f"正文合计 {chars / 1e8:.2f} 亿字")
    print(f"库大小：{KB_DB.stat().st_size / 1e6:.0f} MB")


# 查询侧的停用 2-gram。不过滤的话「历史建筑 保护 修缮」这种多词查询会被
# 「的」「和」「在」这类高频 gram 拖走，BM25 排出来的全是套话段落。
_STOP_GRAMS = {
    "的", "了", "在", "和", "与", "及", "或", "对", "为", "是", "等", "中",
    "有关", "进行", "根据", "按照", "应当", "可以", "通过", "以及", "对于",
    "及其", "并且", "或者", "如果", "但是", "我们", "他们", "这个", "其中",
}


def _exprs(q: str) -> list[str]:
    """把查询变成两种 FTS 表达式：先严后宽。

    一条查询里用户敲的是几个词（空格分开），词与词之间通常是「同时满足」，
    词内部则是同一件事的不同 2-gram（必须都对上才算命中这个词）。
    所以先试「词间 AND」，查不到再退到「词间 OR」。

    为什么不能只用 OR：实测「城市更新 专项规划」在 OR 下第一条是
    《新能源汽车产业发展规划》——只因为沾了「规划」两个字。
    也不能只用 AND：「历史建筑 保护 修缮」这种三词组合在库里常常凑不齐。
    """
    groups = []
    for term in q.split():
        grams = [g for g in bigrams(term).split() if g not in _STOP_GRAMS]
        if grams:
            groups.append("(" + " AND ".join(f'"{g}"' for g in grams) + ")")
    if not groups:
        return []
    strict = " AND ".join(groups)
    loose = " OR ".join(groups)
    return [strict] if strict == loose else [strict, loose]


def query(q: str, k: int = 10) -> list[dict]:
    """检索，返回结构化结果。CLI 和 MVP 共用这一个入口。

    结果按**文件**去重，不按块。理由：搜「历史建筑 保护」时，第 32 条和第 33 条
    是同一份《历史文化名城名镇名村保护条例》的两个相邻块，两块都排进前二，
    十条结果里有一半位置被同一份文件占掉——用户看到的不是「十份文件」而是
    「三份文件重复三遍」。同一文件只留最相关的一块，另外记下它总共有几块命中，
    这个数比堆重复块有用：命中块多说明整份文件都在讲这件事。
    """
    exprs = _exprs(q)
    if not exprs:
        return []
    db = sqlite3.connect(KB_DB)
    sql = ("SELECT b.doc_key, b.title, b.label, b.text, d.doc_no, d.published, "
           "       d.region, d.url, d.issuer, bm25(blocks_fts) AS score "
           "FROM blocks_fts JOIN blocks b ON b.id = blocks_fts.rowid "
           "JOIN docs d ON d.doc_key = b.doc_key "
           "WHERE blocks_fts MATCH ? ORDER BY score LIMIT ?")
    rows = []
    try:
        for expr in exprs:
            try:
                # 多抓候选：去重之后还要凑够 k 份文件。倍数给到 20 是因为
                # 命中密集的文件很能吃名额——《历史文化名城名镇名村保护条例》
                # 一份就占了 10 块，按 8 倍抓 24 块只剩两三份文件。
                rows = db.execute(sql, (expr, k * 20)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                break
    finally:
        db.close()
    out: list[dict] = []
    seen: dict[str, int] = {}
    for (doc_key, title, label, text, doc_no, pub, region, url, issuer,
         score) in rows:
        if doc_key in seen:
            out[seen[doc_key]]["hit_blocks"] += 1
            continue
        if len(out) >= k:
            continue
        seen[doc_key] = len(out)
        out.append({
            "doc_key": doc_key, "title": title, "label": label,
            "snippet": text[:220], "doc_no": doc_no, "published": pub,
            "region": region, "url": url, "issuer": issuer,
            "score": round(-score, 2), "hit_blocks": 1,
        })
    return out


def search(q: str, k: int = 10, meta: bool = True) -> None:
    hits = query(q, k)
    print(f"\n「{q}」→ {len(hits)} 份")
    for h in hits:
        more = f" · 命中 {h['hit_blocks']} 块" if h["hit_blocks"] > 1 else ""
        print(f"  [{h['score']}] {h['region'] or '—'} · {h['published'] or '—'}"
              f" · {h['doc_no'] or '—'}{more}")
        print(f"      {h['title'][:60]}")
        if h["label"]:
            print(f"      {h['label']}")
        print(f"      {h['snippet'][:110].replace(chr(10), ' ')}…")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--search", default="")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.search:
        search(a.search, a.top)
    elif a.stats:
        db = sqlite3.connect(KB_DB)
        db.executescript(SCHEMA)
        _stats(db)
        db.close()
    else:
        build(a.limit, a.rebuild)


if __name__ == "__main__":
    main()
