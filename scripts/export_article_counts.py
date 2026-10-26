#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出「法规 → 条数」对照表，供条号越界校验（闸门 2）独立使用。

为什么单独成文件：条号越界校验是这套产品的主防线（见 `research/07` 第六节）——
它要挡住的是「真法规 + 真主题 + 假条号」这一类，而检索分数对这一类完全无效
（实测三道越界题的重排分数都超过 0.82，比多数正确题还高）。

主防线不该依赖 Python 侧的 `corpus.json` 对象。一张 JSON 表就能让任何语言、
任何服务做这件事，也让这张表可以被人工核对。

用法：
    python scripts/export_article_counts.py
    python scripts/export_article_counts.py --check   # 只校验，不写文件
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "corpus.json"
NAMES = ROOT / "data" / "doc_names.json"
OUT = ROOT / "data" / "article_counts.json"


def cn2int(s: str) -> int | None:
    """中文数字转整数（条号不超过三位数）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    d = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9}
    u = {"十": 10, "百": 100, "千": 1000}
    total, cur = 0, 0
    for ch in s:
        if ch in d:
            cur = d[ch]
        elif ch in u:
            total += (cur or 1) * u[ch]
            cur = 0
        else:
            return None
    return total + cur


def main() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    aliases = json.loads(NAMES.read_text(encoding="utf-8")) if NAMES.exists() else {}

    counts: dict[str, int] = {}
    max_art: dict[str, str] = {}
    for key, val in corpus.items():
        arts = val.get("articles") or []
        counts[key] = len(arts)
        if arts:
            heads = [a["text"].split("\n")[0].strip() for a in arts]
            max_art[key] = heads[-1].split("　")[0].split(" ")[0][:12]

    # 校验：条数与正文实际条号是否自洽（防 corpus.json 本身错）
    errs = 0
    for key, n in counts.items():
        if n == 0:
            continue
        last = max_art.get(key) or ""
        num = cn2int(last.replace("第", "").replace("条", ""))
        if num != n:
            print(f"[!] {key}：articles 长度 {n}，但最后一条是「{last}」（解析为 {num}）")
            errs += 1

    doc = {
        "note": "条号越界校验用的条数表（闸门 2）。由 scripts/export_article_counts.py 从 data/corpus.json 生成。",
        "source": "data/corpus.json",
        "counts": counts,
        "alias": {k: v for k, v in aliases.items()},
    }

    if "--check" in sys.argv:
        print(f"校验完成，条数不自洽 {errs} 处")
        raise SystemExit(1 if errs else 0)

    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 写出 {OUT}")
    for k, v in counts.items():
        tail = f"  末条={max_art.get(k, '—')}" if v else "  （无条款结构）"
        print(f"     {v:3d} 条  {k}{tail}")
    print(f"     条数自洽校验：错误 {errs} 处")


if __name__ == "__main__":
    main()
