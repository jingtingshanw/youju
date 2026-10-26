#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闸门 2 全量对照 —— 把**产品校验层**（gatekeeper）跑在已保存的评估结果上。

**它回答一个评测脚本回答不了的问题**：模型自己填的【结论边界】，和产品实际交付给
用户的结论强度，是不是同一件事？

不是。评测脚本读的是**模型自报**（那是"模型觉得自己答到了什么程度"）；
交付给用户的是**闸门 2 的规则判定**（那是"这份回答实际能交付到什么强度"）。
两者应当高度一致 —— 不一致的地方就是产品行为与评测口径的差，
必须逐条看明白，因为它们决定用户到底收到了什么。

**不调用任何模型**：回答与召回段落都从 `eval/results/*.json` 读。

用法：
    python scripts/gatekeeper_sweep.py                                  # 默认 v7 结果
    python scripts/gatekeeper_sweep.py eval/results/app-v6-boundary.json
    python scripts/gatekeeper_sweep.py --diff                           # 只列分歧
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatekeeper as gk  # noqa: E402
import legalkit as lk  # noqa: E402

DEFAULT = lk.ROOT / "eval" / "results" / "app-v7-boundary.json"


def run(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        resp = {"answer": r["answer"],
                "metadata": {"retriever_resources": [
                    {"document_name": x["name"], "content": x["content"]}
                    for x in r.get("resources", [])]}}
        v = gk.verify(r["query"], r["answer"], resp["metadata"]["retriever_resources"])
        model = (r.get("boundary") or {}).get("结论边界")
        out.append({
            "id": r["id"], "layer": r.get("layer"), "question": r["query"],
            "model": model, "rule": v.boundary, "by": v.decided_by,
            "agree": model == v.boundary,
            "blocked": [c.code for c in v.checks if c.level == gk.BLOCK and not c.passed],
            "warned": [c.code for c in v.checks if c.level == gk.WARN and not c.passed],
            "cites": [(c.law or c.raw_law, c.art, c.status) for c in v.cites],
            "reasons": v.reasons,
        })
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = Path(args[0]) if args else DEFAULT
    if not path.exists():
        raise SystemExit(f"[x] 找不到 {path}")
    if not path.is_absolute():
        path = lk.ROOT / path

    res = run(path)
    print("=" * 96)
    print(f"闸门 2 全量对照 · 来源 {path.name} · {len(res)} 题 · 不调用任何模型")
    print("=" * 96)

    both = Counter((r["model"], r["rule"]) for r in res)
    agree = sum(1 for r in res if r["agree"])
    print(f"\n一致：{agree}/{len(res)}（{agree/len(res):.0%}）")
    print("分布（模型自报 → 规则判定）：")
    for (m, u), n in sorted(both.items(), key=lambda kv: -kv[1]):
        mark = "✓" if m == u else "★"
        print(f"  {mark} {str(m):6s} → {u:6s}  {n} 题")

    print(f"\n按层：")
    for layer in ("L1基础", "L2推理", "L3边界", "L4对抗"):
        rs = [r for r in res if r["layer"] == layer]
        if not rs:
            continue
        a = sum(1 for r in rs if r["agree"])
        u = Counter(r["rule"] for r in rs)
        print(f"  {layer:8s} {len(rs):3d} 题   一致 {a:3d}   "
              f"规则判定：可作答 {u['可作答']} / 部分作答 {u['部分作答']} / 拒答 {u['拒答']}")

    dis = [r for r in res if not r["agree"]]
    if dis:
        print(f"\n{'='*96}\n分歧逐条（{len(dis)} 条 —— 产品实际交付的强度与模型自报不同）\n{'='*96}")
        for r in dis:
            print(f"\n[{r['id']}] {r['layer']}　模型自报 {r['model']} → 规则判定 {r['rule']}"
                  f"（由 {r['by']}）")
            print(f"      Q: {r['question'][:78]}")
            if r["blocked"]:
                print(f"      拦截项：{r['blocked']}　提示项：{r['warned']}")
            for x in r["reasons"]:
                print(f"      · {x}")

    if "--json" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--json") + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {p}")


if __name__ == "__main__":
    main()
