# -*- coding: utf-8 -*-
"""生成 docs/文件路径清单.md —— 全项目文件索引。

为什么要脚本生成：文件一多，手写目录必然过期，而且**过期没有提示**。
`build_file_index.py` 生成的是总说明附录 D 的分组表；这个脚本生成的是完整的
「按目录全量列出」版，包含数据文件体积，用于交付时回答「东西都在哪」。

用法：
    python scripts/build_path_list.py

排除：__pycache__ / output/ / .git / *.pyc / *.log / ~$*；
`data/fulltext/`（18,608 份正文）只汇总体积，不逐个列。
"""
from pathlib import Path
import os, datetime

ROOT = Path(r"C:\Users\17378\WorkBuddy\ai-pm\projects\policy-verify-engine")
OUT = ROOT / "docs" / "文件路径清单.md"

SKIP_DIRS = {".git", "__pycache__", "output", "node_modules"}
# 体量大、按目录汇总即可，不逐个列
SUMMARY_DIRS = {"data/fulltext"}

NOTES = {
    "scripts/build_path_list.py": "本脚本：扫描全项目生成 `docs/文件路径清单.md`",
    "prd/PRD-v4.md": "★ **现行 PRD V4.0**：三层数据 + 两组指标 + FR-01~FR-09 + 竞品结论重算",
    "prd/PRD-v3.md": "上一版 PRD（V3.1，8 部语料时代，含 §0.3 竞品分析全文）",
    "prd/PRD-v2.md": "架构说明版 PRD（两层库 + 四段链路 + 装配流水线规格）",
    "prd/PRD-v1.md": "最早一版（历史），核心是第 4 章「两道闸门」",
    "docs/项目总说明-v3.md": "★ **现行技术总说明**：三层数据、扩容的四个代价、十条判断、路线图",
    "docs/项目总说明-v2.md": "v2 总说明（8 部语料时代的完整叙述，历史）",
    "docs/项目总说明-v1.md": "v1 总说明（历史）",
    "docs/优化方向-v1.md": "★ 四份外部评估的取舍 + 收录率实测（鹰潭 287→212→3，误杀 1）",
    "docs/文件路径清单.md": "本文件（由扫描脚本生成）",
    "scripts/harvest/README.md": "★ **取数层说明**：十一个脚本、四条抓取路径、四类坑、还没解决的",
    "scripts/catalog/README.md": "★ **目录层说明**：状态词表、覆盖度、源配置",
    "scripts/harvest/gov_full.py": "国家级：清单枚举 + 全文抓取（--list / --fetch / --stats）",
    "scripts/harvest/meta.py": "元信息与时效（eff_status: valid/amended/repealed/unknown）",
    "scripts/harvest/city_registry.py": "城市名 → 政府门户域名表",
    "scripts/harvest/local_catalog.py": "★ 省/市/县三级目录：探入口 → 翻栏目 → 落库",
    "scripts/harvest/npc_laws.py": "人大网《现行有效法律目录》PDF → CSV + 入库",
    "scripts/harvest/build_index.py": "★ 全文切块 + FTS5 2-gram 索引（--search 词）",
    "scripts/harvest/extract.py": "正文提取（HTML / docx）",
    "scripts/harvest/export_catalog.py": "导出三级 CSV + 覆盖报告",
    "scripts/harvest/scan_saas.py": "探测失败站里有多少用政务 SaaS（只读，结论：3 站、均不可用）",
    "scripts/harvest/scan_was5.py": "探测失败站有没有可用 TRS WAS5（只读，结论：0 站）",
    "scripts/harvest/diag_blocked.py": "单站受阻原因诊断",
    "scripts/harvest/diag_list.py": "单站列表结构诊断",
    "scripts/harvest/diag_pages.py": "单站翻页机制诊断",
    "scripts/harvest/diag_batch.py": "批量诊断",
    "scripts/gatekeeper.py": "★ **闸门 2**：C1–C5，不调用任何模型；11 组自检用例",
    "scripts/legalkit.py": "★ 共用底层：名称归一 / 条号解析 / 三张表 / 文本比对",
    "scripts/ask.py": "★ 命令行入口 + `analyze()`（两版共用判定入口）",
    "scripts/catalog/crawl.py": "目录层 CLI + 覆盖度审计 + 30 项自检",
    "scripts/catalog/adapters.py": "七种源形态适配器",
    "scripts/catalog/store.py": "SQLite 落库与多源合并",
    "scripts/catalog/model.py": "目录层数据模型与推断规则",
    "scripts/catalog/audit_source.py": "★ 源端收录率审计（把「命中 N 条只收 M 条」摊开）",
    "scripts/catalog/sites.json": "源与地域链配置（加省市只改这里）",
    "mvp/server.py": "★ 单文件服务：BM25 + 模型调用 + 闸门 2 + /api/kb + /api/local + /api/catalog",
    "mvp/index.html": "★ 单文件前端：问答 / 政策清单 / 地方目录 三块面板",
    "mvp/README.md": "★ MVP 说明：四个能力边界、轻包与全包、三层延迟实测",
    "data/catalog.db": "★ 目录库 53 MB：items 65,111 / doc_meta 18,608 / fulltext 18,916 / local_site 2,491",
    "data/kb.db": "★ 检索索引 980 MB：blocks 167,595 + FTS5",
    "data/law/现行有效法律目录.csv": "★ 308 条现行有效法律名录（名称 + 沿革，无条文）",
    "data/law/npc_law_index.pdf": "人大网原版 PDF（2026-08-28）",
    "data/catalog/export/覆盖报告.md": "★ 覆盖率与缺口的当前快照（脚本生成）",
    "data/catalog/export/目录_全部.csv": "★ 65,111 条目录全量导出（16.7 MB）",
    "data/catalog/local_sites.json": "2,491 个站的抓取状态与原因（24 MB）",
    "data/article_counts.json": "★ 条数表（C1 判据）——只覆盖 8 部",
    "data/stale_ledger.json": "★ 时效台账（C3 外部真值）——只覆盖 8 条",
    "data/doc_names.json": "落库代号 ↔ 正式全称",
    "eval/golden-set.csv": "★ 66 题评测集（含「期望边界」答案键）",
    "eval/README.md": "评估集设计与判分口径",
    "prompts/system-v3.md": "★ 现行提示词（四条硬约束 + 边界判定四行字段）",
}

def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"

def walk():
    dirs: dict[str, list[Path]] = {}
    ft_n, ft_size = 0, 0
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        rel_dir = Path(dp).relative_to(ROOT).as_posix()
        if rel_dir == ".":
            rel_dir = "(root)"
        for fn in fns:
            if fn.endswith((".pyc", ".log")) or fn.startswith("~$"):
                continue
            p = Path(dp) / fn
            if rel_dir.startswith("data/fulltext"):
                ft_n += 1
                ft_size += p.stat().st_size
                continue
            dirs.setdefault(rel_dir, []).append(p)
    dirs["data/fulltext"] = []
    dirs["__FT__"] = [(ft_n, ft_size)]  # 占位，main 里单独处理
    return dirs, ft_n, ft_size

def main():
    dirs, ft_n, ft_size = walk()
    dirs.pop("__FT__", None)
    total_files = sum(len(v) for v in dirs.values()) + ft_n
    total_size = sum(p.stat().st_size for v in dirs.values() for p in v) + ft_size
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    L = []
    L.append("# 有据 · 项目文件路径清单")
    L.append("")
    L.append("> 根目录：`C:\\Users\\17378\\WorkBuddy\\ai-pm\\projects\\policy-verify-engine`  ")
    L.append(f"> 生成时间：{now} · 共 **{total_files} 个文件 / {len(dirs)} 个目录**，{human(total_size)}"
             "（已排除 `__pycache__`、`output/`、`.git`；`data/fulltext/` 只汇总不逐个列）")
    L.append(">")
    L.append("> 本文件由扫描生成，不要手改——文件一多，手写的目录必然过期，而且**过期没有提示**。")
    L.append("")
    L.append("## 一、现行文档（先看这几个）")
    L.append("")
    L.append("| 文档 | 路径 | 大小 | 读它干什么 |")
    L.append("|---|---|---:|---|")
    order = [
        ("PRD v4（现行）", "prd/PRD-v4.md"),
        ("技术总说明 v3（现行）", "docs/项目总说明-v3.md"),
        ("取数层说明", "scripts/harvest/README.md"),
        ("目录层说明", "scripts/catalog/README.md"),
        ("MVP 说明", "mvp/README.md"),
        ("覆盖率快照", "data/catalog/export/覆盖报告.md"),
        ("优化方向", "docs/优化方向-v1.md"),
    ]
    for label, rel in order:
        p = ROOT / rel
        if p.exists():
            L.append(f"| {label} | `{rel}` | {human(p.stat().st_size)} | {NOTES.get(rel,'').replace('★ ','')} |")
    L.append("")
    L.append("## 二、按目录全量列出")
    L.append("")

    def sort_key(d):
        if d == "(root)":
            return (0, d)
        return (1, d)

    for d in sorted(dirs, key=sort_key):
        files = sorted(dirs[d], key=lambda p: p.name)
        size = sum(p.stat().st_size for p in files)
        if d == "data/fulltext":
            L.append(f"### `{d}/` —— **{ft_n} 个文件，{human(ft_size)}**（按 256 个子目录分片，不逐个列）")
            L.append("")
            L.append("正文全文，文件名是 `doc_key.txt`，可逐份追回 `catalog.db.doc_meta` 的 URL。")
            L.append("")
            continue
        L.append(f"### `{d}/` —— {len(files)} 个文件，{human(size)}")
        L.append("")
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            note = NOTES.get(rel, "")
            line = f"- `{rel}`"
            if note:
                line += f"  ← {note}"
            L.append(line)
        L.append("")

    L.append("## 三、数据资产一览")
    L.append("")
    L.append("| 资产 | 文件 | 规模 | 能不能引用 |")
    L.append("|---|---|---:|---|")
    rows = [
        ("L0 法律名录", "`data/law/现行有效法律目录.csv`", "308 条", "❌ 只有名称与沿革"),
        ("L1 国家级清单", "`data/catalog/govcn_list.*.jsonl`", "18,916 条", "❌"),
        ("L1 地方目录", "`data/catalog.db` · `items`", "65,111 条", "❌ 只有元信息"),
        ("L1 站点状态", "`data/catalog/local_sites.json`", "2,491 站", "—"),
        ("L2 国家级正文", "`data/fulltext/`", "18,608 份 / 7,291 万字", "✅"),
        ("L2 检索索引", "`data/kb.db`", "167,595 块 / 980 MB", "✅"),
        ("核验：条数表", "`data/article_counts.json`", "8 部 / 419 单元", "✅"),
        ("核验：时效台账", "`data/stale_ledger.json`", "8 条", "✅"),
        ("核验：元信息时效", "`data/catalog.db` · `doc_meta`", "18,608 条（702 条已失效）", "🟡 仅标记"),
        ("评测：题目", "`eval/golden-set.csv`", "66 题", "—"),
    ]
    for a, b, c, e in rows:
        L.append(f"| {a} | {b} | {c} | {e} |")
    L.append("")
    L.append("**三层数据的可信度不一样，别混着用**：目录层「知道这份文件存在」，全文层「知道它怎么写的」，"
             "核验层「能判断引的那一条对不对」。前两层只能搜，第三层才能判。")
    L.append("")

    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"[ok] {OUT} · {len(L)} 行 · {total_files} 文件")

if __name__ == "__main__":
    main()
