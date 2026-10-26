#!/usr/bin/env python
"""扫描整个项目，生成「附录 D · 全部文件路径目录」的 Markdown 表格。

为什么要用脚本生成而不是手写：
    语料一变、脚本一加，手写的目录立刻就是错的，而且**错得没有提示**——
    这正是本项目反复记录的那类债（`stale_ledger` 与提示词台账不同源、三张表忘了重建）。
    目录这种东西只有在「一条命令能重生」的前提下才值得放进交付文档。

用法：
    python scripts/build_file_index.py                 # 打印表格
    python scripts/build_file_index.py --write         # 注入 docs/项目总说明-v2.md 的附录 D
    python scripts/build_file_index.py --write --file docs/项目总说明-v3.md

注入位置靠两个标记定位：
    <!--FILE-INDEX-START-->
    <!--FILE-INDEX-END-->
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 排除项：这些不是交付物，列进来只会稀释目录的可读性
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".idea", ".vscode",
             # Word 生成流水线的中间产物（封面/目录/正文 HTML、转换日志），
             # 每次重跑都会变；列进来会让本附录立刻过期
             "output"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
# Office 打开文件时生成的锁文件（~$xxx.docx），不是交付物
SKIP_PREFIX = ("~$",)
# .db 是目录层的 SQLite 缓存，每抓一次都变；列进来会让附录立刻过期
# （与排除 output/ 是同一个理由）
SKIP_EXTS = {".pyc", ".pyo", ".pyd", ".log", ".png", ".tmp", ".db"}

# 有行数统计意义的文本类型（其余只报大小）
TEXT_EXTS = {".py", ".md", ".csv", ".txt", ".json", ".html", ".htm",
             ".yml", ".yaml", ".sh", ".bat", ".ps1", ".sql", ".toml", ".ini", ".cfg"}

# ── 分组的显示顺序与标题 ──────────────────────────────────────────────────
GROUPS: list[tuple[str, str, str]] = [
    ("prompts", "提示词（产品规格的一部分）", ""),
    ("mvp", "最小可运行 MVP（零第三方依赖，不依赖 Dify）", ""),
    ("scripts", "脚本（自动化与工具）", ""),
    ("scripts/catalog", "目录层（政策清单抓取，2026-10 接入）", ""),
    ("prd", "产品需求文档", ""),
    ("docs", "交付文档", ""),
    ("data", "数据资产与语料", ""),
    ("eval", "评估集", ""),
    ("eval/results", "实测结果明细（JSON，可 `--rejudge` 复算）", ""),
    ("demo", "演示前端（静态样本版）", ""),
    ("research", "研究报告（按时间顺序，含自我更正）", ""),
    ("research/_struct", "出题器的结构断言中间产物", ""),
    ("(root)", "项目根目录文件", ""),
]

# ── 关键文件的职责说明（人工维护；未列到的走规则兜底）─────────────────────
NOTES: dict[str, str] = {
    # prompts
    "prompts/system-v1.md": "输出格式：结论 / 依据 / 【引用清单】",
    "prompts/system-v2.md": "＋ 语料时效台账表 + 时效标注要求",
    "prompts/system-v3.md": "＋ 【边界判定】四行 + 时效硬规则 + 【历史沿革】—— **现行版本**",
    # mvp
    "mvp/server.py": "**单文件服务**：本地 BM25 检索 + 直连 OpenAI 兼容接口 + 调闸门 2 + `/api/catalog`",
    "mvp/index.html": "**单文件前端**：真实提问，渲染结论 / 依据 / 时效 / 五项校验；＋ 政策清单面板",
    "mvp/README.md": "MVP 说明：一条命令怎么跑、与完整版的差别、能力边界、政策清单一节",
    # scripts（核心）
    "scripts/legalkit.py": "★ **共用底层**：名称归一 / 条号解析 / 三张表读取 / 边界判定解析 / 文本比对",
    "scripts/gatekeeper.py": "★ **闸门 2**：产品校验层 C1–C5，不调用任何模型；11 组自检用例",
    "scripts/ask.py": "★ 命令行入口 + `analyze()`（命令行与网页共用的判定入口）",
    "scripts/gatekeeper_sweep.py": "66 题全量对照：闸门 2 规则判定 vs 模型自报",
    "scripts/serve_demo.py": "演示页的本地服务（只用标准库）",
    "scripts/build_demo.py": "把真实问答注入演示页作为离线样本（含 JSON 可解析断言）",
    "scripts/build_file_index.py": "本脚本：扫描项目生成「附录 D 全部文件路径目录」",
    "scripts/build_docs_html.py": "把总说明 Markdown 渲染成自包含 HTML（无第三方依赖）",
    "scripts/eval_run.py": "检索层评测：recall@k 与 top1 分数分布",
    "scripts/eval_run_app.py": "答案层评测；`--rejudge` 可只重算判分、不重调模型",
    "scripts/build_eval_set.py": "生成 / 校验评估集（含 6 种结构断言）",
    "scripts/export_article_counts.py": "从语料导出条数表 → `data/article_counts.json`（C1 判据）",
    "scripts/export_stale_ledger.py": "从 manifest 导出时效台账 → `data/stale_ledger.json`（C3 外部真值）",
    "scripts/build_kb_text_v2.py": "生成落库文本 `data/kb_text_v2/` + `data/doc_names.json`",
    "scripts/build_corpus.py": "解析原始语料 → `data/corpus.json`",
    "scripts/verify_corpus.py": "语料与 `manifest.verified.csv` 逐项核对",
    "scripts/dify_build_kb.py": "知识库入库（Dify）",
    "scripts/dify_build_app.py": "用 `prompts/system-v3.md` 建应用（Dify）",
    "scripts/dify_fix_retrieval.py": "修检索配置；**`--rerank` 必须显式跑**",
    "scripts/dify_add_bailian.py": "在 Dify 里注册百炼模型供应商（镜像里没有）",
    "scripts/dify_hit_test.py": "检索连通性冒烟测试",
    # eval
    "eval/golden-set.csv": "★ 66 题评估集（含「期望边界」答案键）",
    "eval/README.md": "评估集设计与判分口径的完整说明",
    "eval/app.json": "现行应用凭据（base / key）",
    # data
    "data/manifest.verified.csv": "★ 8 部法规的人工核对清单——一切时效信息的源头",
    "data/stale_ledger.json": "★ 时效台账（从上一份导出）——**C3 的外部真值**",
    "data/article_counts.json": "★ 条数表（从语料导出）——**C1 的越界判据**",
    "data/doc_names.json": "落库代号 ↔ 正式全称（闸门 2 认全称）",
    "data/corpus.json": "语料正文（含解析出的条款数）",
    # demo / prd / docs
    "demo/index.html": "演示前端单页：离线样本 + 在线实问，浅色 / 深色主题",
    "prd/PRD-v3.md": "★ **现行 PRD V3.1**（含 §0.3 竞品分析）：三套目标 + FR-01~FR-07（带验收标准）+ 评测集重建规格 + 单位经济模型（实测）+ 降级 + 合规",
    "docs/优化方向-v1.md": "★ **优化方向**：四份外部评估的取舍 + 收录率实测 + 翻转的真实代价",
    "scripts/catalog/audit_source.py": "★ **源端收录率审计**：把「命中 N 条只收 M 条」的差额逐条摊开",
    "data/catalog/audit_yingtan.json": "鹰潭站审计明细（287 条命中 / 212 条去重 / 误杀 1 条）",
    "prd/PRD-v2.md": "上一版 PRD：两层库 + 四段链路 + RAG 定位 + 装配流水线规格（作为架构说明仍有效）",
    "prd/PRD-v1.md": "上一版 PRD（历史版本），核心是第 4 章「两道闸门」",
    "docs/项目总说明-v1.md": "上一版总说明（历史版本）",
    "docs/项目总说明-v1.html": "v1 的自包含渲染版（可打印、可离线分发）",
    "docs/项目总说明-v2.md": "**本文档**",
    # research
    "research/00-可行性探针.md": "方案是否可行的第一轮探测",
    "research/01-语料核对报告.md": "8 部法规与 manifest 的逐项核对",
    "research/02-Dify检索实测报告.md": "检索配置实测；**`reranking_mode` 缺失的排查过程**",
    "research/03-检索配置修复记录.md": "检索配置的修复过程与验收",
    "research/04-基线跑分与三轮迭代报告.md": "基线跑分 + 三轮提示词迭代",
    "research/05-百炼接入与rerank判别力实测.md": "进程内复现检索层，量化重排的判别力",
    "research/06-百炼全栈落地与重排实测.md": "全栈落地；检索层开/关重排 A/B",
    "research/07-对抗题扩容与阈值结论翻转.md": "**阈值结论被推翻**（对抗题 4 道 → 40 道）",
    "research/08-边界判定字段与时效硬规则.md": "v3 提示词设计；三轮对照（v5→v6→v7）",
    "research/09-校验层落地与三处假阳性修正.md": "**闸门 2 落地**；三处假阳性修正；全量对照 66/66",
    "research/10-最小MVP与第五项检查.md": "**最小 MVP 与 C5**；换模型暴露的第五类失败；0 回归验证",
    # scripts/catalog（目录层）
    "scripts/catalog/crawl.py": "★ **目录抓取 CLI**：`--region/--topic` 出清单；覆盖度审计；26 项离线自检",
    "scripts/catalog/model.py": "★ 领域模型：`CatalogItem` / 主题词表 / 效力级别与地域推断 / 标题去括注",
    "scripts/catalog/adapters.py": "★ 七种源形态（govcn / govsite / jsearch / manual / flk / note）",
    "scripts/catalog/store.py": "★ SQLite 落库与合并（items / source_state / runs），按依据可信度覆盖",
    "scripts/catalog/net.py": "带限速与重试的 GET / get_json（只用标准库）",
    "scripts/catalog/sites.json": "★ 源与地域展开配置——加一个省市只改这个文件",
    "scripts/catalog/__init__.py": "包入口（导出模型、库、抓取三层）",
    "scripts/catalog/README.md": "目录层说明：状态词表、已实测源、怎么加省市、不做的事",
    # 成本与延迟
    "scripts/measure_cost.py": "★ **成本与延迟实测**：读真实 `usage`，出单次 token/成本/首字/全程（PRD §9 的数据来源）",
    "eval/results/cost_probe.json": "成本实测原始数据（8 次调用，四层各 2 题）",
    # data/catalog
    "data/catalog/manual.csv": "★ 人工补充清单（抓不到的源靠这里补；当前只有表头）",
    "data/catalog/manual.csv.example": "人工补充清单的填写示例",
}

# ── 按目录规则兜底生成说明 ────────────────────────────────────────────────
DIR_RULES: list[tuple[str, str]] = [
    ("data/corpus/", "原始下载件（.doc / .docx，含下载站的文件号）"),
    ("data/corpus_docx/", "统一转成 .docx 的语料（供入库前统一解析）"),
    ("data/kb_text/", "第一版落库文本（旧，已被 kb_text_v2 取代）"),
    ("data/kb_text_v2/", "**现行落库文本**（每行形如《法规全称》第X条　正文）"),
    ("data/text/", "中间产物：从 .doc 抽出的纯文本"),
    ("eval/results/ask-records/", "命令行每次问答的落盘记录（可追溯）"),
    ("eval/results/", "某次实测的落盘明细（可 `--rejudge` 复算）"),
    ("research/_struct/", "出题器的结构断言中间产物"),
    ("data/catalog/", "目录层的人工补充清单（抓不到的源靠这里补）"),
    ("research/", "研究报告"),
]

RESULT_NOTES: dict[str, str] = {
    "app-baseline.json": "答案层基线（v1 提示词，30 题）",
    "app-v2-ledger.json": "v2 提示词（+ 时效台账）",
    "app-v3.json": "v3 提示词首测",
    "app-v4-rerank.json": "接入重排后的答案层",
    "app-v5-40adv.json": "v2 提示词 × 40 道对抗题（**旧判据虚高的那一批**）",
    "app-v6-boundary.json": "v3 首版提示词（13 道标签分歧的那一批）",
    "app-v7-boundary.json": "★ **v3 锐化版提示词——现行，L4 边界处理 40/40**",
    "retrieval-baseline.json": "检索层基线",
    "retrieval-v2.json": "检索层第二轮",
    "retrieval-v3-norerank.json": "检索层 A/B：不开重排",
    "retrieval-v3-rerank.json": "检索层 A/B：开重排",
    "retrieval-v5-40adv.json": "★ **66 题检索层明细——阈值扫描的数据源**",
    "cloud-stack-signal.json": "云端栈的探测信号",
    "emb-v4-cache.json": "embedding 调用缓存（9.3 MB，**非交付物**；删掉只影响重复计费）",
    "gatekeeper-last.json": "闸门 2 最近一次的运行结果",
}

APP_NOTES = {
    "app-v2.json": "v2 提示词的应用凭据",
    "app-v3.json": "v3 提示词的应用凭据",
    "app-v4.json": "接重排后的应用凭据",
    "app-v6.json": "v3 首版的应用凭据",
}


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def count_lines(p: Path) -> str:
    if p.suffix.lower() not in TEXT_EXTS:
        return "—"
    try:
        with p.open("rb") as f:
            return str(sum(1 for _ in f))
    except OSError:
        return "—"


# 这些二级目录单独成组：文件多且同质，混在父目录里会把真正的条目淹没
SUBGROUPS = ("eval/results", "research/_struct", "scripts/catalog")


def group_of(rel: str) -> str:
    for sub in SUBGROUPS:
        if rel.startswith(sub + "/"):
            return sub
    return rel.split("/", 1)[0] if "/" in rel else "(root)"


def note_for(rel: str) -> str:
    if rel in NOTES:
        return NOTES[rel]
    name = rel.rsplit("/", 1)[-1]
    # eval/results 的落盘明细按文件名给专门说明
    if rel.startswith("eval/results/"):
        if rel.startswith("eval/results/ask-records/"):
            return "一次问答的完整记录（问题 / 回答 / 召回的条文 / 判定）"
        return RESULT_NOTES.get(name, "某次实测的落盘明细")
    if rel.startswith("eval/") and name.startswith("app"):
        return APP_NOTES.get(name, "某一版应用凭据")
    for prefix, text in DIR_RULES:
        if rel.startswith(prefix):
            if prefix == "research/":
                return "研究报告"
            return text
    return ""


def collect() -> dict[str, list[tuple[str, int, str]]]:
    """返回 {分组: [(相对路径, 字节数, 行数), ...]}，组内按路径排序。"""
    out: dict[str, list[tuple[str, int, str]]] = {}
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name in SKIP_NAMES or p.suffix.lower() in SKIP_EXTS:
            continue
        if p.name.startswith(SKIP_PREFIX):
            continue
        out.setdefault(group_of(rel), []).append((rel, p.stat().st_size, count_lines(p)))
    return out


def render() -> str:
    groups = collect()
    total_files = sum(len(v) for v in groups.values())
    total_bytes = sum(sz for v in groups.values() for _, sz, _ in v)

    lines: list[str] = []
    lines.append(f"本节由 `scripts/build_file_index.py` 自动扫描生成，"
                 f"共 **{total_files} 个文件**、合计 **{human_size(total_bytes)}**"
                 f"（已排除 `__pycache__` / `.git` / 图片等非交付物，"
                 f"以及 `output/`——Word 生成流水线的中间产物、`*.db`——目录层的 SQLite 缓存，"
                 f"这两类每次重跑都会变）。")
    lines.append("")
    lines.append("重跑方式（语料或脚本变更后应当重跑，否则本节立刻过期）：")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/build_file_index.py --write     # 重新扫描并写回本文档")
    lines.append("python scripts/build_file_index.py             # 只看结果，不改文件")
    lines.append("```")
    lines.append("")

    idx = 0
    for key, title, _ in GROUPS:
        items = groups.get(key)
        if not items:
            continue
        idx += 1
        n = len(items)
        lines.append(f"### D.{idx} `{key}` — {title}")
        lines.append("")
        lines.append(f"共 {n} 个文件。")
        lines.append("")
        lines.append("| 路径 | 大小 | 行数 | 职责 |")
        lines.append("|---|---:|---:|---|")
        for rel, size, nline in items:
            note = note_for(rel)
            lines.append(f"| `{rel}` | {human_size(size)} | {nline} | {note} |")
        lines.append("")

    # 兜底：万一有分组没被 GROUPS 覆盖
    covered = {k for k, _, _ in GROUPS}
    leftover = [k for k in groups if k not in covered]
    if leftover:
        for key in sorted(leftover):
            items = groups[key]
            idx += 1
            lines.append(f"### D.{idx} `{key}`")
            lines.append("")
            lines.append("| 路径 | 大小 | 行数 | 职责 |")
            lines.append("|---|---:|---:|---|")
            for rel, size, nline in items:
                lines.append(f"| `{rel}` | {human_size(size)} | {nline} | {note_for(rel)} |")
            lines.append("")

    lines.append("**读法**：带 ★ 的是「改动会影响结论」的文件——"
                 "`legalkit.py` / `gatekeeper.py` / `ask.py` 是判定层，"
                 "`data/` 下三张表是闸门 2 的全部依据，`eval/golden-set.csv` 是唯一的答案键。"
                 "改这六个文件里的任何一个，都必须重跑 `gatekeeper.py --selftest` 与 `--rejudge` 回归。")
    lines.append("")
    return "\n".join(lines)


START = "<!--FILE-INDEX-START-->"
END = "<!--FILE-INDEX-END-->"


def inject(body: str, doc: Path) -> None:
    if not doc.exists():
        raise SystemExit(f"[x] 找不到 {doc}")
    text = doc.read_text(encoding="utf-8")
    a, b = text.find(START), text.find(END)
    if a < 0 or b < 0:
        raise SystemExit(f"[x] {doc.name} 里找不到 {START} / {END} 标记")
    new = text[:a + len(START)] + "\n\n" + body + "\n" + text[b:]
    doc.write_text(new, encoding="utf-8")
    print(f"[ok] 附录 D 已注入 {doc.relative_to(ROOT)}（{len(body.splitlines())} 行）")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成「全部文件路径目录」并注入总说明文档")
    ap.add_argument("--write", action="store_true", help="写回文档（默认只打印）")
    ap.add_argument("--file", default="docs/项目总说明-v2.md", help="目标文档")
    args = ap.parse_args()

    body = render()
    if args.write:
        inject(body, ROOT / args.file)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
