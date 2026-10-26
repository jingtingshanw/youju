#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫全部正文，摸清「有效期」和「施行日期」在原文里到底有多少可提取。

只统计不写库。目的是决定台账要不要加这两个字段，以及加不加得起。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAT = ROOT / "data" / "catalog.db"

# 有效期：写法很多，「有效期至2028年12月31日」「有效期到2028年底」
# 「本通知有效期5年」「有效期自2023年1月1日至2028年12月31日」
P_VALID = re.compile(
    r"有效期(?:限)?(?:为|是)?\s*[至到]\s*(\d{4})\s*年\s*(\d{1,2})\s*月"
    r"(?:\s*(\d{1,2})\s*日)?")
P_VALID2 = re.compile(r"有效期(?:限)?(?:为|是)?\s*(\d{1,2})\s*年")
P_VALID3 = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起(?:施行|实施|执行)"
    r"[^。；]{0,40}?有效期(?:至|到)\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
P_EFFECT = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起(?:施行|实施|执行)")
P_REPEAL_DATE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*废止")


def main() -> int:
    c = sqlite3.connect(f"file:{CAT}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT key, title, path FROM fulltext "
        "WHERE status='ok' AND chars>=300 AND coalesce(path,'')<>''").fetchall()
    print(f"待扫 {len(rows):,} 份")

    n_valid = n_valid2 = n_eff = n_rep = n_miss_file = 0
    samples = {"valid": [], "eff": [], "rep": []}
    for i, (key, title, path) in enumerate(rows):
        p = ROOT / path if not Path(path).is_absolute() else Path(path)
        if not p.exists():
            p = ROOT / "data" / "fulltext" / path
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            n_miss_file += 1
            continue
        # 只扫尾部 800 字：有效期和施行日期几乎都在文末
        tail = txt[-900:]
        m = P_VALID3.search(tail) or P_VALID.search(tail)
        if m:
            n_valid += 1
            if len(samples["valid"]) < 6:
                samples["valid"].append((title[:34], m.group(0)[:46]))
        elif P_VALID2.search(tail):
            n_valid2 += 1
        m = P_EFFECT.search(tail)
        if m:
            n_eff += 1
            if len(samples["eff"]) < 5:
                samples["eff"].append((title[:34], m.group(0)[:34]))
        m = P_REPEAL_DATE.search(tail)
        if m:
            n_rep += 1
            if len(samples["rep"]) < 5:
                samples["rep"].append((title[:34], m.group(0)[:34]))
        if (i + 1) % 5000 == 0:
            print(f"  {i + 1:,} …", flush=True)

    out = {"scanned": len(rows), "miss_file": n_miss_file,
           "valid_abs_date": n_valid, "valid_duration": n_valid2,
           "effect_date": n_eff, "repeal_date": n_rep, "samples": samples}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    (ROOT / "data" / "ledger_probe.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
