#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把评估集里的真实问答做成演示页的内置样本，注入 demo/index.html。

**为什么要内置样本**：`demo/index.html` 直接双击打开时是连不上本地服务的
（跨进程、也没有 API key 能暴露给浏览器）。没有内置样本的话，页面就是一片空白，
"给人看一眼"这个用途就废了。内置之后，即使不启动任何服务，也能看到真实的
判定结果；启动了服务则自动切到实问模式。

样本来自 `eval/results/app-v7-boundary.json`（v3 锐化版提示词下的 66 题实测），
判定由 `gatekeeper.verify()` **离线**重算 —— 不调用任何模型，也不花钱。

用法：
    python scripts/build_demo.py                     # 用默认的 6 道
    python scripts/build_demo.py --list              # 看当前结果里可以挑哪些题
    python scripts/build_demo.py --pick L1-01,L4-26  # 自己指定
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask as askmod  # noqa: E402
import legalkit as lk  # noqa: E402

SOURCE = lk.ROOT / "eval" / "results" / "app-v7-boundary.json"
PAGE = lk.ROOT / "demo" / "index.html"
START = "<!--SAMPLES-START-->"
END = "<!--SAMPLES-END-->"
# 注入时连 <script> 标签一起写 —— 标记必须留在脚本标签**外面**。
# 第一版把标记放在标签里面，注入后 JSON 里混进了 `/*SAMPLES-START*/` 注释，
# `JSON.parse` 直接抛错，页面一片空白。这种错只有真打开页面才发现，
# 所以下面加了一条"注入后必须能被 json.loads 解析"的断言，把它挡在构建阶段。
SCRIPT_RE = re.compile(r'<script id="samples" type="application/json">(.*?)</script>', re.S)

# 默认挑的 6 道：覆盖三种结论边界，且包含最有代表性的两类对抗形态。
# 选它们不是为了"好看"，是为了让第一次看的人能在两分钟内看见产品在防什么。
DEFAULT_PICKS: list[tuple[str, str]] = [
    ("L1-01", "① 基本题 · 单点条文定位"),
    ("L2-01", "② 推理题 · 要拼两处信息"),
    ("L3-01", "③ 时效题 · 这部法还有效吗"),
    ("L4-12", "④ 对抗 · 条号越界（要第80条，实际只有66条）"),
    ("L4-37", "⑤ 对抗 · 虚构制度（绿色账户积分兑换）"),
    ("L4-26", "⑥ 对抗 · 已废止法规还索要处罚条号"),
    ("L4-05", "⑦ 对抗 · 法规名真实，但库里没有全文"),
]


def load_source() -> dict[str, dict]:
    if not SOURCE.exists():
        raise SystemExit(f"[x] 找不到 {SOURCE}，先跑一次 eval_run_app.py --json 生成它")
    return {r["id"]: r for r in json.loads(SOURCE.read_text(encoding="utf-8"))}


def build_one(row: dict, tag: str) -> dict:
    """用 gatekeeper 离线重算判定，得到与命令行完全一致的记录。"""
    resp = {"answer": row["answer"],
            "metadata": {"retriever_resources": [
                {"document_name": x["name"], "content": x["content"]}
                for x in row.get("resources", [])]}}
    rec = askmod.analyze(row["query"], resp)
    rec["id"] = row["id"]
    rec["layer"] = row.get("layer")
    rec["tag"] = tag
    rec["seconds"] = row.get("seconds")
    return rec


def inject(html: str, samples: list[dict]) -> str:
    """把样本写进 `<script id="samples">`，并断言页面里那份 JSON 真的能解析。"""
    a, b = html.find(START), html.find(END)
    if a < 0 or b < 0:
        raise SystemExit(f"[x] {PAGE} 里找不到 {START} / {END} 标记")
    payload = json.dumps(samples, ensure_ascii=False, indent=1)
    block = f'<script id="samples" type="application/json">{payload}</script>'
    out = html[:a + len(START)] + "\n" + block + "\n" + html[b:]

    # 断言：注入后的页面里，样本块必须是一个能被 json.loads 吃下去的纯 JSON。
    m = SCRIPT_RE.search(out)
    if not m:
        raise SystemExit("[x] 注入后页面里找不到 samples 脚本块")
    try:
        got = json.loads(m.group(1))
    except ValueError as e:
        raise SystemExit(f"[x] 注入后的样本 JSON 解析失败（页面会白屏）：{e}")
    if len(got) != len(samples):
        raise SystemExit(f"[x] 注入条数不符：期望 {len(samples)} 实得 {len(got)}")
    if "</script" in payload.lower():
        raise SystemExit("[x] 样本里含 </script，会截断脚本标签")
    return out


def main() -> None:
    src = load_source()

    if "--list" in sys.argv:
        print(f"{SOURCE.name}：{len(src)} 条\n")
        for k, r in src.items():
            b = (r.get("boundary") or {}).get("结论边界") or "—"
            print(f"  {k:7s} {r.get('layer','?'):8s} {b:6s} {r['query'][:56]}")
        return

    picks = DEFAULT_PICKS
    if "--pick" in sys.argv:
        ids = sys.argv[sys.argv.index("--pick") + 1].split(",")
        picks = [(i.strip(), i.strip()) for i in ids if i.strip()]

    samples = []
    for qid, tag in picks:
        row = src.get(qid)
        if not row:
            print(f"[!] 结果里没有 {qid}，跳过")
            continue
        rec = build_one(row, tag)
        samples.append(rec)
        print(f"  {qid:7s} {rec['boundary']:6s} 依据 {len(rec['cites'])} 条  {tag}")

    html = PAGE.read_text(encoding="utf-8")
    PAGE.write_text(inject(html, samples), encoding="utf-8")
    print(f"\n{len(samples)} 个样本已注入 {PAGE.relative_to(lk.ROOT)}（JSON 已校验可解析）")


if __name__ == "__main__":
    main()
