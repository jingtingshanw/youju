#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 data/corpus_docx/*.docx 抽成可入库的纯文本与条款切片。

纯标准库实现（zipfile + ElementTree），不依赖 python-docx。
输出：
  data/text/<name>.txt     —— 按文档顺序还原的正文（段落一行，表格用制表符分行）
  data/corpus.json         —— 结构化结果：blocks（段落/表格）+ articles（按「第X条」切分）
并打印与 manifest.csv 的条款数对照，用于发现口径偏差。
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ROOT = Path(__file__).resolve().parent.parent          # projects/policy-verify-engine
DOCX_DIR = ROOT / "data" / "corpus_docx"
TEXT_DIR = ROOT / "data" / "text"
OUT_JSON = ROOT / "data" / "corpus.json"
MANIFEST = ROOT / "data" / "corpus" / "manifest.csv"

# 条款起始标记：第X条 / 第X条之一。允许中文数字与阿拉伯数字
ART_HEAD = re.compile(r"^\s*第\s*[一二三四五六七八九十百千零〇0-9]{1,8}\s*条(?:之[一二三四五六七八九十]{1,3})?")


def para_text(p: ET.Element) -> str:
    """按 XML 顺序拼接段落内所有文本节点（含超链接内的 run）。"""
    buf = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{W}t":
            buf.append(node.text or "")
        elif tag == f"{W}tab":
            buf.append("\t")
        elif tag in (f"{W}br", f"{W}cr"):
            buf.append("\n")
    return "".join(buf).strip()


def table_rows(tbl: ET.Element) -> list[list[str]]:
    rows = []
    for tr in tbl.findall(f"{W}tr"):
        cells = []
        for tc in tr.findall(f"{W}tc"):
            cell = "\n".join(para_text(p) for p in tc.findall(f"{W}p"))
            cells.append(cell.strip())
        rows.append(cells)
    return rows


def doc_blocks(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    body = ET.fromstring(xml).find(f"{W}body")
    if body is None:
        raise ValueError(f"{path.name}: 没有 w:body")
    out: list[dict] = []
    for child in body:
        if child.tag == f"{W}p":
            t = para_text(child)
            if t:
                out.append({"kind": "p", "text": t})
        elif child.tag == f"{W}tbl":
            rows = table_rows(child)
            if rows:
                out.append({"kind": "tbl", "rows": rows})
    return out


def split_articles(blocks: list[dict]) -> tuple[list[str], list[dict]]:
    """按「第X条」把段落流切成条款。

    返回 (preamble, articles)：
      preamble —— 首个「第X条」之前的内容（标题、公布信息、目录等），以及无条款文档的整段内容
      articles —— 仅真正的条款块，no 为条款起始文本
    表格并入其所在的条款；若表格出现在任何条款之前，则归入 preamble。
    """
    preamble: list[str] = []
    arts: list[dict] = []
    cur = None
    for b in blocks:
        if b["kind"] == "p":
            if ART_HEAD.match(b["text"]):
                cur = {"no": b["text"][:24], "text": b["text"]}
                arts.append(cur)
            elif cur is not None:
                cur["text"] += "\n" + b["text"]
            else:
                preamble.append(b["text"])
        else:  # 表格
            flat = "\n".join("\t".join(r) for r in b["rows"])
            if cur is not None:
                cur["text"] += "\n[表格]\n" + flat
            else:
                preamble.append("[表格]\n" + flat)
    return preamble, arts


def load_manifest() -> dict[str, int]:
    """返回 {文件名(不含扩展名): 条款数}。"""
    if not MANIFEST.exists():
        return {}
    import csv
    m = {}
    with MANIFEST.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            fn = (row.get("文件名") or "").strip()
            if not fn:
                continue
            try:
                m[fn] = int((row.get("条款数") or "0").strip() or 0)
            except ValueError:
                m[fn] = -1
    return m


def main() -> None:
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    man = load_manifest()
    result: dict[str, dict] = {}
    print(f"{'文件':<48} {'段落':>5} {'表格':>4} {'条款':>5} {'字符':>7}")
    print("-" * 82)
    for path in sorted(DOCX_DIR.glob("*.docx")):
        blocks = doc_blocks(path)
        preamble, arts = split_articles(blocks)
        np_ = sum(1 for b in blocks if b["kind"] == "p")
        nt = sum(1 for b in blocks if b["kind"] == "tbl")
        full = "\n".join(
            b["text"] if b["kind"] == "p" else "\n".join("\t".join(r) for r in b["rows"])
            for b in blocks
        )
        nchar = len(re.sub(r"\s", "", full))
        (TEXT_DIR / f"{path.stem}.txt").write_text(full, encoding="utf-8")
        result[path.stem] = {
            "blocks": blocks,
            "preamble": preamble,
            "articles": arts,
            "stats": {"paragraphs": np_, "tables": nt, "articles": len(arts),
                      "preamble_blocks": len(preamble), "chars": nchar},
        }
        print(f"{path.stem[:46]:<48} {np_:>5} {nt:>4} {len(arts):>5} {nchar:>7}")

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("-" * 82)
    print(f"写出 {OUT_JSON.relative_to(ROOT)}  与  {TEXT_DIR.relative_to(ROOT)}/ (共 {len(result)} 个 txt)")

    # 与 manifest 对照条款数
    if man:
        def find_manifest(stem: str):
            """manifest 的 文件名 与 docx 的 stem 做包含匹配（manifest 常带法条编号后缀）。"""
            for k, v in man.items():
                ks = Path(k).stem
                if ks == stem or ks.startswith(stem) or stem.startswith(ks) or stem in ks:
                    return v
            return None

        print("\n=== 条款数对照（manifest ←→ 实测）===")
        bad = 0
        for stem, info in result.items():
            hit = find_manifest(stem)
            got = info["stats"]["articles"]
            if hit is None:
                print(f"  {stem[:42]:<44} manifest 未收录（实测 {got} 条）")
            elif hit != got:
                bad += 1
                print(f"  {stem[:42]:<44} manifest={hit:<4} 实测={got:<4} ← 不一致")
            else:
                print(f"  {stem[:42]:<44} manifest={hit:<4} 实测={got:<4} ✓")
        print(f"  不一致 {bad} / {len(result)}")

    write_verified_manifest(result, man)
    write_kb_text(result)


def write_kb_text(result: dict) -> None:
    """产出 data/kb_text/<name>.txt —— 供 Dify 入库用。

    约定「一条一段」：标题单独一行，其后每个「第X条」占一行。
    这样在 Dify 里把分段标识符设成换行，就能正好切到条文粒度，不需要正则。
    无条款号的文档（如表格型决定）退化为「一行一个表格数据行」。
    """
    kb_dir = ROOT / "data" / "kb_text"
    kb_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    print(f"\n{'入库文本':<44} {'行数':>5} {'字符':>7}")
    print("-" * 62)
    for stem, info in result.items():
        lines: list[str] = [info["preamble"][0]] if info["preamble"] else [stem]
        for a in info["articles"]:
            lines.append(a["text"].replace("\n", " ").strip())
        if not info["articles"]:
            # 表格型：每个数据行拼成一行，带上来源表头，便于溯源
            for b in info["blocks"]:
                if b["kind"] == "tbl":
                    for r in b["rows"][1:]:
                        lines.append(" ｜ ".join(c.replace("\n", " ").strip() for c in r))
        body = "\n".join(lines) + "\n"
        (kb_dir / f"{stem}.txt").write_text(body, encoding="utf-8")
        total += len(lines)
        print(f"{stem[:42]:<44} {len(lines):>5} {len(body):>7}")
    print("-" * 62)
    print(f"共 {len(result)} 个文件 / {total} 行，写出 {kb_dir.relative_to(ROOT)}/")


def write_verified_manifest(result: dict, man: dict) -> None:
    """产出 data/manifest.verified.csv：保留 manifest 原字段，并列实测值 + 核对结论。

    manifest.csv 是原始证据，不修改；本文件是派生产物，供项目实际使用。
    """
    if not MANIFEST.exists():
        return
    import csv

    def stem_of(name: str) -> str:
        return Path(name).stem

    def match(stem: str):
        for k in man:
            ks = stem_of(k)
            if ks == stem or ks.startswith(stem) or stem.startswith(ks) or stem in ks:
                return k
        return None

    rows_out = []
    used: set[str] = set()
    with MANIFEST.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        fields = list(rd.fieldnames or [])
        for row in rd:
            name = (row.get("文件名") or "").strip()
            hit = None
            for stem, info in result.items():
                ks = stem_of(name)
                if ks == stem or ks.startswith(stem) or stem.startswith(ks) or stem in ks:
                    hit = (stem, info)
                    break
            if hit:
                stem, info = hit
                used.add(stem)
                got_a = info["stats"]["articles"]
                got_c = info["stats"]["chars"]
                try:
                    man_a = int((row.get("条款数") or "0").strip() or 0)
                except ValueError:
                    man_a = -1
                if got_a == man_a:
                    concl = "一致"
                elif got_a == 0:
                    concl = "原文无「第X条」条款号（表格型决定），按 0 计"
                else:
                    concl = f"manifest 有误：条款数应为 {got_a}"
                row["条款数_实测"] = got_a
                row["字符数_实测"] = got_c
                row["核对结论"] = concl
            else:
                row["条款数_实测"] = ""
                row["字符数_实测"] = ""
                row["核对结论"] = "未找到对应原文"
            rows_out.append(row)

    for stem, info in result.items():
        if stem in used:
            continue
        rows_out.append({
            "文件名": stem + ".docx",
            "条款数_实测": info["stats"]["articles"],
            "字符数_实测": info["stats"]["chars"],
            "核对结论": "manifest 未收录",
        })

    out = ROOT / "data" / "manifest.verified.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields + ["条款数_实测", "字符数_实测", "核对结论"])
        w.writeheader()
        for r in rows_out:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
    print(f"\n写出 {out.relative_to(ROOT)}（原始 manifest.csv 未改动）")


if __name__ == "__main__":
    sys.exit(main())
