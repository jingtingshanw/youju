#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估集跑分器 —— 基线（第 4 步）。

分两档，本脚本先做**检索层**基线（不依赖生成模型，纯 Dify 召回）：

  指标 1  recall@1 / @3 / @5   —— 期望来源文档（+ 期望条款特征词）是否落在前 k 条
  指标 2  分层得分             —— L1/L2/L3 分层的 recall@5
  指标 3  L4 对抗分            —— 对抗题的 top1 相似度，与「能正确作答的题」的 top1 对比

为什么单列指标 3：本轮实测已经证明，在没有任何 rerank 的情况下，
对抗题（问一部库里根本没有的法典）的 top1 相似度会落进正确题目的分数区间，
**所以相似度不能当「有没有依据」的判据**。这个指标就是把这件事量化下来。

判分口径（可复现、不靠人肉）：
  - 期望来源文档取自 `eval/golden-set.csv` 的「来源」列；
  - 检索层判据词 = 「判分规则」列里 『』 引号内的词，『』 之外的「」是说明/反例，不参与判分；
  - **只有 L1/L2 用判据词**。L3/L4 的 『』 是答案层判据（如『已废止』『无法回答』），
    它们本来就不在条文正文里，拿去做检索判据会把「检索成功」误判成「未命中」。
  - L1/L2 的判据词是 **任一命中即算过**：检索层只回答「有没有把那条捞上来」，
    「四项有没有答全」属于答案层（需要 LLM 判分器），两件事不混在一起。

为什么 L4 单列一个指标：本轮实测已经证明，在没有任何 rerank 的情况下，
对抗题（问一部库里根本没有的法典）的 top1 相似度会落进正确题目的分数区间，
**所以相似度不能当「有没有依据」的判据**。这个指标就是把这件事量化下来。

用法：
    python scripts/eval_run.py                 # 跑全部 30 题
    python scripts/eval_run.py --layer L4      # 只跑某一层
    python scripts/eval_run.py --json out.json # 另存明细
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parents[1] / "scripts"))  # ai-pm/scripts
import dify  # noqa: E402

GOLD = ROOT / "eval" / "golden-set.csv"
NAMES = ROOT / "data" / "doc_names.json"
DATASET_NAME = "政策法规语料库"


def load_names() -> dict[str, str]:
    """语料内部代号 → 法规正式全称。入库文本换过命名口径后，
    评估集的「来源」列仍写代号，这里做一次翻译，避免判定口径随命名漂移。"""
    if NAMES.exists():
        return json.loads(NAMES.read_text(encoding="utf-8"))
    return {}

# 默认留空 —— 让 hit-testing 回落到**知识库自己存储的 retrieval_model**。
#
# 为什么不留一份写死的配置：之前吃过一次亏。评估脚本里写死的权重和知识库实际存的
# 不是同一份，于是「报告里的数字」和「线上真实跑的东西」悄悄分叉，
# 排查了很久才发现是两处配置不一致。
# 实读 hit_testing_service.py:38 —— 请求里 retrieval_model 为空时，
# 它取 `dataset.retrieval_model`。所以留空才是唯一不会漂移的写法。
RETRIEVAL: dict = {}


def build_retrieval(emb_provider: str | None, emb_model: str | None) -> dict:
    """只有在需要临时覆盖时才构造显式配置（用于 A/B 对照，不用于出报告）。"""
    if not emb_provider:
        return {}
    return {
        "search_method": "hybrid_search",
        "reranking_enable": False,
        "reranking_mode": "weighted_score",
        "reranking_model": {"reranking_provider_name": "", "reranking_model_name": ""},
        "weights": {"keyword_setting": {"keyword_weight": 0.3},
                    "vector_setting": {"vector_weight": 0.7,
                                       "embedding_provider_name": emb_provider,
                                       "embedding_model_name": emb_model}},
        "top_k": 5,
        "score_threshold_enabled": False,
        "score_threshold": None,
    }

TOKEN_RE = re.compile(r"『([^』]+)』")


def load_gold() -> list[dict]:
    with GOLD.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_dataset(token: str) -> str:
    d = dify.req("GET", f"{dify.API}/datasets?page=1&limit=100", token=token)[1]
    for k in d.get("data", []):
        if k.get("name") == DATASET_NAME:
            return k["id"]
    raise SystemExit("[x] 找不到知识库")


def hit(token: str, ds_id: str, q: str) -> list[dict]:
    st, d = dify.req("POST", f"{dify.API}/datasets/{ds_id}/hit-testing", token=token,
                     body={"query": q, "retrieval_model": RETRIEVAL})
    if st != 200 or not isinstance(d, dict):
        return []
    return d.get("records", [])


def eval_one(row: dict, rows: list[dict], alias: dict[str, str] | None = None) -> dict:
    """row 为 golden-set 的一行，rows 为检索结果。判定命中位置。

    参数别名叫 `alias` 而不是 `names`：本函数内部已经用 `names` 存
    「召回命中的文档名列表」，同名会把别名映射覆盖掉，导致别名形同虚设。
    第一版就踩了这个坑，表现为怀化的题全部 MISS —— 而召回结果其实全对。
    """
    layer = row["分层"]
    src = (row.get("来源") or "").strip()
    doc_expect, art_expect = (src.split() + [""])[:2] if src else ("", "")

    # 只有 L1/L2 把『』当检索判据；L3/L4 的『』是答案层判据
    tokens = TOKEN_RE.findall(row.get("判分规则") or "") if layer in ("L1基础", "L2推理") else []
    tokens = [t for t in tokens if len(t) >= 2]

    ranks, scores, names = [], [], []
    for i, r in enumerate(rows):
        seg = (r.get("segment") or {})
        content = seg.get("content") or ""
        name = ((seg.get("document") or {}).get("name")) or ""
        names.append(name)
        scores.append(float(r.get("score") or 0.0))

        # 期望文档既可以按语料代号写，也可以按正式全称写；两种都认
        cands = [doc_expect]
        if alias and doc_expect in alias:
            cands.append(alias[doc_expect])
        doc_ok = (not doc_expect) or any(c[:12] in name for c in cands if c)
        tok_ok = (not tokens) or any(t in content for t in tokens)
        if doc_ok and tok_ok:
            ranks.append(i + 1)

    return {
        "id": row["id"],
        "layer": layer,
        "query": row["输入"],
        "expect_doc": doc_expect,
        "tokens": tokens,
        "ranks": ranks,
        "top1_score": scores[0] if scores else None,
        "max_score": max(scores) if scores else None,
        "top_names": names[:5],
    }


def main() -> None:
    global RETRIEVAL

    def opt(flag, default=None):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

    only = opt("--layer")
    if opt("--dataset"):
        globals()["DATASET_NAME"] = opt("--dataset")
    # 默认不覆盖，用知识库自身存的配置；给 --emb-provider 才临时覆盖（A/B 用）
    RETRIEVAL = build_retrieval(opt("--emb-provider"), opt("--emb-model"))
    names = load_names()

    gold = load_gold()
    if only:
        gold = [g for g in gold if g["分层"].startswith(only)]

    tk = dify.login()
    ds = find_dataset(tk)
    print(f"[i] 知识库「{DATASET_NAME}」 id={ds}")
    eff = dify.req("GET", f"{dify.API}/datasets/{ds}", token=tk)[1]
    eff = eff.get("retrieval_model_dict") or eff.get("retrieval_model") or {}
    print("[i] 生效检索配置（来自知识库）: "
          f"method={eff.get('search_method')} top_k={eff.get('top_k')} "
          f"rerank_enable={eff.get('reranking_enable')} "
          f"rerank_mode={eff.get('reranking_mode')} "
          f"rerank_model={eff.get('reranking_model')}")
    if RETRIEVAL:
        print("[i] ⚠ 本次请求临时覆盖了检索配置（A/B 对照用，勿用于出报告）")

    results = []
    for g in gold:
        r = eval_one(g, hit(tk, ds, g["输入"]), names)
        results.append(r)
        sc = f"{r['top1_score']:.3f}" if r["top1_score"] is not None else "  -  "
        if r["layer"] == "L4对抗":
            print(f"[{r['id']:6s}] {r['layer']:6s} top1={sc} 对抗题·只看相似度（不计召回）")
        else:
            r1 = r["ranks"][0] if r["ranks"] else None
            tag = f"@{r1}" if r1 and r1 <= 5 else "MISS"
            print(f"[{r['id']:6s}] {r['layer']:6s} top1={sc} 命中={tag:6s} "
                  f"期望≒{r['expect_doc'][:22] or '（无）'}")
            if r["tokens"]:
                print(f"         判据词={r['tokens'][:4]}")

    print("\n" + "=" * 78)
    by_layer = defaultdict(list)
    for r in results:
        by_layer[r["layer"]].append(r)

    def rate(rs, k):
        if not rs:
            return 0.0
        return sum(1 for r in rs if r["ranks"] and r["ranks"][0] <= k) / len(rs)

    print("分层 recall（只对 L1–L3 有意义；L4 的『有依据』本身就该是空）")
    print(f"{'层':8s} {'n':>3s} {'@1':>7s} {'@3':>7s} {'@5':>7s}")
    for layer in ("L1基础", "L2推理", "L3边界"):
        rs = by_layer.get(layer, [])
        if not rs:
            continue
        print(f"{layer:8s} {len(rs):3d} {rate(rs,1):7.0%} {rate(rs,3):7.0%} {rate(rs,5):7.0%}")
    l123 = [r for r in results if r["layer"] != "L4对抗"]
    print(f"{'合计':8s} {len(l123):3d} {rate(l123,1):7.0%} {rate(l123,3):7.0%} {rate(l123,5):7.0%}")

    l4 = by_layer.get("L4对抗", [])
    if l4:
        pos = sorted(r["top1_score"] for r in l123 if r["top1_score"] is not None)
        print("\n指标 3 · 对抗题（L4）能不能被相似度挡住？")
        if pos:
            print(f"  L1–L3 的 top1 相似度分布：min={min(pos):.3f}  中位={statistics.median(pos):.3f}  max={max(pos):.3f}")
        worst_pos = min(pos) if pos else 0.0
        for r in l4:
            if r["top1_score"] is None:
                continue
            s = r["top1_score"]
            pct = (sum(1 for p in pos if p < s) / len(pos)) if pos else 0.0
            verdict = "★ 落到正确题的分数区间内" if s >= worst_pos else "低于所有正确题"
            print(f"  {r['id']} top1={s:.3f}  比 {pct:.0%} 的正确题目还高  {verdict}")
        if pos:
            overlap = sum(1 for r in l4 if r["top1_score"] and r["top1_score"] >= worst_pos)
            print(f"  → {overlap}/{len(l4)} 条对抗题的相似度 ≥ 正确题的最低分。"
                  f"结论：**相似度阈值不能用来判断『有没有依据』**，必须做显式引用存在性检查。")

    if "--json" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--json") + 1])
        p.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {p}")


if __name__ == "__main__":
    main()
