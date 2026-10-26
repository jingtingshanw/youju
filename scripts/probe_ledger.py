"""探测：能否在 18,608 份全文上自动生成「条数表」与「失效台账」。

这不是交付脚本，是一次性口径测量。目的有两个：
1. 条数表能不能不靠人工、从正文结构直接抽出来，正确率多少；
2. 失效关系能不能从「废止条款」反向推出来，能覆盖多少文件。

跑法：
    python scripts/probe_ledger.py
"""
from __future__ import annotations

import collections
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "catalog.db"

# 「第十二条」→ 12；同时吃「第十二条之一」
_CN = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
       "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}
_ART = re.compile(r"第([〇零一二三四五六七八九十百千\d]{1,7})条(?:之([一二三四五六七八九十]))?")
# 只认「行首条号」：本文的条文总是另起一行/段首；
# 而「依据民法典第一千零五十二条」这类是**引用别人的条**，不能算本文条数。
_ART_LINE = re.compile(
    r"(?:^|(?<=[\n。；]))[ \t　]*第([〇零一二三四五六七八九十百千\d]{1,7})条"
    r"(?:之([一二三四五六七八九十]))?")
# 引用前缀：这些词后面的「第X条」是在说别人
_REF_TAIL = re.compile(r"(?:典|法|例|规|则|》）|》)\s*$")


def cn2int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if "百" in s or "千" in s:
        # 只处理「一百二十」这类
        total, cur = 0, 0
        for ch in s:
            if ch in ("百", "千"):
                total += (cur or 1) * _CN[ch]
                cur = 0
            else:
                cur = _CN.get(ch, 0)
        return total + cur
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CN.get(a, 0) if a else 1) * 10 + (_CN.get(b, 0) if b else 0)
    return _CN.get(s, 0)


def article_shape(text: str) -> dict:
    """判断一份文件的条结构形态：只认行首/句首条号，排除对他法的引用。

    判据是「递增链」而不是「最大号」——本文条文按顺序出现，页内回引不会超过
    已出现的最大条号；一旦出现远超链尾的号（如正文引《民法典》第一千零五十二条），
    那一定不是本文的条，直接丢弃。
    """
    chain = 0          # 当前链尾
    count = 0          # 链上有效条数
    for m in _ART_LINE.finditer(text):
        before = text[max(0, m.start() - 12):m.start()]
        if _REF_TAIL.search(before):
            continue          # 「民法典第一千零五十二条」这类引用，跳过
        n = cn2int(m.group(1))
        if n <= 0 or n > 500:
            continue
        if n > chain:
            if n - chain > 8 and chain > 0:
                continue      # 断层太大，判为引用而非本文条
            chain = n
            count += 1
    if chain < 2:
        return {"kind": "no_article", "max": 0, "n": count, "gap": 0, "ratio": 0.0}
    ratio = count / chain
    if ratio >= 0.9:
        kind = "dense"
    elif ratio >= 0.5:
        kind = "sparse"
    else:
        kind = "thin"
    return {"kind": kind, "max": chain, "head_max": chain,
            "n": count, "gap": chain - count, "ratio": ratio}


# ── 废止关系 ────────────────────────────────────────────────
_REPEAL_CTX = re.compile(
    r"(?:同时|予以|即行|一并)?(?:废止|停止执行|失效)", re.S)
_REPEAL_TITLE = re.compile(
    r"(《[^》]{4,60}》)(?:[^。；\n]{0,20})?(?:同时)?(?:废止|停止执行)")
_REPEAL_DOCNO = re.compile(
    r"([\u4e00-\u9fa5]{1,10}[〔\[]\d{4}[〕\]]\s*\d{1,4}号)(?:[^。；\n]{0,25})?(?:同时)?(?:废止|停止执行)")
_SELF_REPEAL = re.compile(r"本(?:规定|办法|细则|意见|通知|决定|条例)[^。；\n]{0,12}(?:废止|失效)")


def main() -> int:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT key,title,doc_no,path,chars FROM fulltext WHERE status='ok'").fetchall()
    print(f"全文 {len(rows)} 份")

    kinds = collections.Counter()
    maxes: list[tuple[str, str, int]] = []
    reap_title = 0
    reap_docno = 0
    self_reap = 0
    reap_edges: list[tuple[str, str]] = []
    missing_path = 0
    by_kind_chars = collections.Counter()

    for r in rows:
        p = Path(r["path"]) if r["path"] else None
        if not p or not p.exists():
            missing_path += 1
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            missing_path += 1
            continue
        s = article_shape(text)
        kinds[s["kind"]] += 1
        by_kind_chars[s["kind"]] += r["chars"] or 0
        if s["kind"] in ("dense", "sparse"):
            maxes.append((r["title"], r["doc_no"] or "", s["max"]))

        # 废止：只取最后 4000 字，废止条款几乎都在尾部
        tail = text[-4000:]
        if _SELF_REPEAL.search(tail):
            self_reap += 1
        for m in _REPEAL_TITLE.finditer(tail):
            reap_title += 1
            reap_edges.append((r["key"], m.group(1)))
            break
        if _REPEAL_DOCNO.search(tail):
            reap_docno += 1

    tot = sum(kinds.values())
    print("\n=== 条结构形态 ===")
    for k, v in kinds.most_common():
        print(f"  {k:12s} {v:6d}  {v/tot*100:5.1f}%   合计 {by_kind_chars[k]:,} 字")
    print(f"  缺文件 {missing_path}")

    usable = kinds["dense"] + kinds["sparse"]
    print(f"\n有条结构（可建表）: {usable} / {tot} = {usable/tot*100:.1f}%")
    print(f"其中 dense（可直接建表）: {kinds['dense']} = {kinds['dense']/tot*100:.1f}%")

    print("\n=== 废止条款 ===")
    print(f"  正文出现《X》废止 : {reap_title}")
    print(f"  正文出现文号废止  : {reap_docno}")
    print(f"  自述废止/失效     : {self_reap}")

    print("\n=== 条数最多的 15 份（检查抽取是否合理）===")
    for t, d, m in sorted(maxes, key=lambda x: -x[2])[:15]:
        print(f"  {m:4d} 条  {d:20s} {t[:44]}")
    print("\n=== 条数为 1–2 的样本（可能是误抽）===")
    for t, d, m in [x for x in maxes if x[2] <= 2][:10]:
        print(f"  {m:4d} 条  {t[:56]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
