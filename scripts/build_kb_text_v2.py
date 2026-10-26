#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""产出 v2 版入库文本：**每个切分段自带法规全称 + 条号**，且文档名用正式全称。

为什么改这个（两处缺陷是端到端基线实测出来的，不是猜的）：

  缺陷 1 · 切分段无身份
    原入库文本是「标题一行 + 每个第X条一行」，Dify 分段标识符设为换行后，
    每个条文的段里**只剩条号，没有法规名**。模型拿到「第四十三条 在噪声敏感
    建筑物集中区域，禁止夜间…」时，只能靠 Dify 附的 document_name 判归属。
    实测里它多次说「该段文字未标明所属法规名称」「检索结果中的条文均出自一部
    名称未显示的地方性法规」——然后开始靠猜。

  缺陷 2 · 文档名与法规全称不一致
    KB 里这份文件叫「海南决定」，而模型的引用清单自然写全称，于是
    「引用可追溯」这一层全线误报（v2 实测 4 条真编造里有 2 条是这个原因）。
    同一类问题还有「噪声污染防治法.txt」vs「中华人民共和国噪声污染防治法」。

改法：
  - 文件名 = 法规正式全称
  - 每段正文前缀 = 《法规正式全称》第X条
  - 表格型文件（海南决定）没有条号，前缀退化为《全称》+ 行序号

副作用必须实测：每个段都带上法规名以后，同一部法规内部的段落共享更多词，
可能削弱「哪一条」的区分度。所以这一步必须跑评估集对照，不能凭感觉。

用法：
    python scripts/build_kb_text_v2.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "corpus.json"
OUT_DIR = ROOT / "data" / "kb_text_v2"
NAMES = ROOT / "data" / "doc_names.json"

# 语料内部代号 → 法规正式全称。同时是入库文件名和切分段前缀。
# 「正式全称」口径与 docs 台账、引用清单保持一致，是引用可追溯的前提。
FORMAL = {
    "噪声污染防治法": "中华人民共和国噪声污染防治法",
    "土壤污染防治法": "中华人民共和国土壤污染防治法",
    "海南决定": "全国人大常委会关于授权国务院在中国（海南）自由贸易试验区暂时调整适用有关法律规定的决定",
    "葫芦岛市生活垃圾分类管理条例_20260819": "葫芦岛市生活垃圾分类管理条例",
    "怀化市扬尘污染防治条例_20260825": "怀化市扬尘污染防治条例",
    "深圳经济特区规划土地监察条例_20260901": "深圳经济特区规划土地监察条例",
    "昌吉回族自治州天山天池风景名胜区保护管理条例_20260831": "昌吉回族自治州天山天池风景名胜区保护管理条例",
    "呼伦贝尔市城市市容和环境卫生管理条例_20260923": "呼伦贝尔市城市市容和环境卫生管理条例",
}


def main() -> None:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    missing = [k for k in data if k not in FORMAL]
    if missing:
        raise SystemExit("[x] 以下文档没有登记正式全称，先补 FORMAL：" + str(missing))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name_map = {}
    print(f"{'入库文本(v2)':<40} {'段数':>5} {'字符':>7}")
    print("-" * 56)

    for stem, info in data.items():
        full = FORMAL[stem]
        name_map[stem] = full
        lines: list[str] = [f"《{full}》"]

        arts = info.get("articles") or []
        for a in arts:
            body = a["text"].replace("\n", " ").strip()
            # a["no"] 形如「第五条　」；取条号部分与正文合并，前缀法规名
            head = body.split("　")[0].split(" ")[0].strip()
            rest = body[len(head):].strip("　 ")
            lines.append(f"《{full}》{head}　{rest}" if rest else f"《{full}》{head}")

        if not arts:
            # 表格型（海南决定）：无条号，用行序号定位
            idx = 0
            for b in info.get("blocks") or []:
                if b.get("kind") != "tbl":
                    continue
                for r in b.get("rows", [])[1:]:
                    idx += 1
                    lines.append(f"《{full}》〔授权目录第{idx}项〕 "
                                 + " ｜ ".join(c.replace("\n", " ").strip() for c in r))

        body = "\n".join(lines) + "\n"
        (OUT_DIR / f"{full}.txt").write_text(body, encoding="utf-8")
        print(f"{full[:38]:<40} {len(lines):>5} {len(body):>7}")

    NAMES.write_text(json.dumps(name_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ok] 已写出 {len(name_map)} 份到 {OUT_DIR}")
    print(f"[ok] 名称映射写入 {NAMES}")


if __name__ == "__main__":
    main()
