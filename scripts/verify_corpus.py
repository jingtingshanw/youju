# -*- coding: utf-8 -*-
"""
语料一致性复核：把 editor_sdk 提取的文档结构，与 manifest.csv 的「条款数 / 字符数」对照。
产出：console 表格 + research/01-语料核对报告.md 的原始数据。
"""
import json
import re
import csv
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"C:\Users\17378\WorkBuddy\ai-pm\projects\policy-verify-engine")
STRUCT_DIR = ROOT / "research" / "_struct"
MANIFEST = ROOT / "data" / "corpus" / "manifest.csv"

# editor_sdk 生成时的顺序（先 .doc 后 .docx，各自按文件名排序）
ORDER = [
    "中华人民共和国噪声污染防治法(FBM-CLI.1.5112804)",
    "中华人民共和国土壤污染防治法(FBM-CLI.1.321036)",
    "全国人民代表大会常务委员会关于授权国务院在中国(海南)自由贸易试验区暂时调整适用有关法律规定的决定(FBM-CLI.1.341898)",
    "呼伦贝尔市城市市容和环境卫生管理条例_20260923",
    "怀化市扬尘污染防治条例_20260825",
    "昌吉回族自治州天山天池风景名胜区保护管理条例_20260831",
    "深圳经济特区规划土地监察条例_20260901",
    "葫芦岛市生活垃圾分类管理条例_20260819",
]

# 「第X条」：中文数字或阿拉伯数字
ARTICLE = re.compile(r"^第[一二三四五六七八九十百千零〇\d]{1,8}条")


def load_manifest():
    rows = {}
    with open(MANIFEST, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows[r["文件名"]] = r
    return rows


def main():
    mf = load_manifest()
    print(f"{'文件':40} {'manifest条':>9} {'实测条':>7} {'manifest字':>10} {'实测长':>9} {'段':>4} {'表':>3}")
    print("-" * 100)
    total_mf_art = total_geo_art = 0
    total_mf_chr = total_geo_chr = 0

    for i, name in enumerate(ORDER, start=1):
        p = STRUCT_DIR / f"{i}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        nodes = data.get("nodes", [])
        stats = data.get("stats", {})

        # 实测「第X条」数：节点 preview 以「第X条」开头
        arts = sum(1 for n in nodes if ARTICLE.match((n.get("text_preview") or "").strip()))

        # 长度：所有节点 end_index 的最大值（UTF-16 code unit）
        max_end = max((n.get("end_index", 0) for n in nodes), default=0)

        row = mf.get(name, {})
        mf_art = int(row.get("条款数") or 0)
        mf_chr = int(row.get("字符数") or 0)

        total_mf_art += mf_art
        total_geo_art += arts
        total_mf_chr += mf_chr
        total_geo_chr += max_end

        short = name[:38]
        flag = "" if mf_art == arts else "  <<< 不等"
        print(f"{short:40} {mf_art:>9} {arts:>7} {mf_chr:>10} {max_end:>9} "
              f"{stats.get('total_paragraphs',0):>4} {stats.get('total_tables',0):>3}{flag}")

    print("-" * 100)
    print(f"{'合计':40} {total_mf_art:>9} {total_geo_art:>7} {total_mf_chr:>10} {total_geo_chr:>9}")
    print()
    print(f"manifest 条款数合计 = {total_mf_art}")
    print(f"实测「第X条」合计 = {total_geo_art}")
    print(f"差值 = {total_mf_art - total_geo_art}")
    print()
    print("注：实测长度为 UTF-16 code unit（含结构占位），与 manifest 的纯文本字符数不可直接比。")


if __name__ == "__main__":
    main()
