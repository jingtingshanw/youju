#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 manifest.verified.csv 导出语料时效台账，供判分器做「外部真值」使用。

为什么需要它：
  prompts/system-v3.md 要求模型在【边界判定】里自报「时效状态」。
  自报值的正确性必须由**外部真值**来核对 —— 否则模型只要自报「现行有效」
  就能绕过时效硬规则（自报自验等于没验）。
  这份台账就是那个真值来源，与提示词里那张表同源（同一份 manifest），
  但**物理分开**：提示词是给模型看的，这份 JSON 是给判分器看的。

与 export_article_counts.py 的分工：
  那份导出「每部法规多少条」→ 用于条号越界校验；
  这份导出「每部法规还有没有效」→ 用于时效校验。
  两张表都来自 manifest.verified.csv，所以核对结论只依赖一个数据源。

用法：
    python scripts/export_stale_ledger.py
    python scripts/export_stale_ledger.py --check   # 只校验不写
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "manifest.verified.csv"
OUT = ROOT / "data" / "stale_ledger.json"
NAMES = ROOT / "data" / "doc_names.json"

# manifest 的「文件名」列带了 FBM 编号、日期后缀这些落库痕迹，还原成正式全称
STRIP_TAIL = re.compile(r"[_\-]?20\d{6}$")
STRIP_FBM = re.compile(r"\(FBM-[^)]*\)")
# 全角括号 vs 半角括号：manifest 与落库文件名在这处不一致，
# 归一后才能对上（否则同一部海南决定会被登记成两条，见本脚本首版输出）
PAREN = str.maketrans("（）", "()")

# 台账里只登记两种「非现行有效」状态；其余一律「现行有效」。
# manifest 的原始取值是「废止或失效」这种合并写法，这里拆成细粒度，
# 因为提示词里的表已经把「已废止」（被新法取代）和「已失效」（期限届满）
# 分开了 —— 两者的失效原因和用户应对方式不同。
STALE_KW = "废止或失效"


def clean(name: str) -> str:
    n = (name or "").strip()
    n = STRIP_FBM.sub("", n)
    n = re.sub(r"\.(txt|docx|doc|md)$", "", n)
    n = STRIP_TAIL.sub("", n)
    return n.strip()


def norm_key(name: str) -> str:
    """归一化用于匹配。

    三处不一致都要抹平，否则同一部文件在 manifest 与落库文件名之间对不上：
      ① 全角括号 vs 半角括号
      ② 空格
      ③ 「人民代表大会常务委员会」vs「人大常委会」—— manifest 用简化写法，
         落库文件名用全称（海南决定就卡在这里，第三版才修掉）
    """
    n = clean(name).translate(PAREN).replace(" ", "")
    return n.replace("人民代表大会常务委员会", "人大常委会")


def main() -> None:
    with MANIFEST.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not NAMES.exists():
        raise SystemExit(f"[x] 找不到 {NAMES}，先跑 build_kb_text_v2.py")
    names_map: dict[str, str] = json.loads(NAMES.read_text(encoding="utf-8"))

    # manifest 行做索引：清洗后的文件名 → 该行。同一部文件可能有多行写法，
    # 后者不覆盖前者（保留先出现的，manifest 第一条才是权威条目）。
    man: dict[str, dict] = {}
    for r in rows:
        raw = (r.get("文件名") or "").strip()
        if not raw:
            continue
        man.setdefault(norm_key(raw), r)

    # ★ 以**落库的 8 部**为主循环，而不是以 manifest 行为主。
    # 理由：manifest 里同一部文件会有多种写法（海南决定在 manifest 中出现两次，
    # 一次是全称带半角括号、一次是落库短名），按行遍历会把同一部法规登记两遍。
    # 落库文件才是产品真实面对的东西，台账必须与它一一对应。
    ledger: dict[str, dict] = {}
    for doc_key, full in names_map.items():
        # 先用**正式全称**匹配 manifest —— manifest 的权威条目是以全称登记的。
        # 顺序反了会出事：短名「海南决定」在 manifest 里另有一行无时效信息的
        # 占位行（`海南决定.docx`，各列全空），先按短名查会命中那一行，
        # 把一份已失效的法规登记成「现行有效」。这个 bug 在本脚本第二版出现过。
        r = man.get(norm_key(full)) or man.get(norm_key(doc_key))
        if r is None:
            print(f"[!] {full} 在 manifest 中找不到对应行，时效状态记为未知")
            ledger[full] = {"时效性": "未知", "效力位阶": None, "施行日期": None,
                            "废止日期": None, "废止依据": None, "替代文件": None}
            continue
        stale = STALE_KW in (r.get("时效性") or "")
        # 已废止 vs 已失效：
        #   有「替代文件」且不是「无直接替代」→ 被新法整体取代 → 已废止
        #   否则（授权期限届满这类）→ 已失效
        repl = (r.get("替代文件") or "").strip()
        if stale:
            state = "已失效" if (not repl or "无直接替代" in repl) else "已废止"
        else:
            state = "现行有效"
        ledger[full] = {
            "时效性": state,
            "效力位阶": (r.get("效力位阶") or "").strip(),
            "施行日期": (r.get("施行日期") or "").strip() or None,
            "废止日期": (r.get("废止日期") or "").strip() or None,
            "废止依据": (r.get("废止依据") or "").strip() or None,
            "替代文件": repl or None,
        }

    stale_names = [k for k, v in ledger.items() if v["时效性"] in ("已废止", "已失效")]

    # 自洽校验：今天之前判分器的 STALE_MARKERS / README 只认这三份文件失效，
    # 如果台账算出来的不是三份，说明 manifest 被改过而下游没跟上。
    if len(ledger) < 8:
        print(f"[!] 台账只解析出 {len(ledger)} 部（预期 ≥8），检查 manifest 列名是否变了")
    if len(stale_names) != 3:
        print(f"[!] 非现行有效的应为 3 份，实为 {len(stale_names)}：{stale_names}")

    print(f"台账 {len(ledger)} 部法规")
    for k, v in ledger.items():
        flag = "  ← 非现行有效" if v["时效性"] in ("已废止", "已失效") else ""
        print(f"  {v['时效性']:5s} {v['废止日期'] or '—':12s} {k}{flag}")

    if "--check" in sys.argv:
        print("[check] 只校验未写入")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}")


if __name__ == "__main__":
    main()
