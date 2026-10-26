#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""固定知识库的检索配置，消除「混合检索加权异常」（同一段落成对出现、其中一条 0.000 分）。

背景：`research/02-Dify检索实测报告.md` 发现二 —— hit-testing 时部分命中记录
score=null（被格式化成 0.000），且同一段落重复出现。怀疑建库时带的
`retrieval_model` 没有按预期落库，实测跑的其实是工作区默认配置。

做法：GET 出当前 retrieval_model → PATCH 写死一份明确的配置 → 再 GET 回读校验。
幂等，可反复跑。

用法：
    python scripts/dify_fix_retrieval.py            # 固定配置
    python scripts/dify_fix_retrieval.py --show     # 只看不改
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))  # ai-pm/scripts
import dify  # noqa: E402

DATASET_ID = "d3775966-df77-4d29-96fa-f5dca7c94863"

# 明确写死一份配置。要点（按 Dify 0.15.3 源码实读）：
#  - hybrid_search：语义 + 关键词各取所长，条款号这类精确串靠关键词侧兜住
#  - **reranking_mode 必须显式写 weighted_score**。这是本轮查出的真凶：
#    Hybrid 检索会把 embedding_search 与 full_text_index_search 的结果直接 concat 到一个
#    list（retrieval_service.py:112），融合全靠 DataPostProcessor。而
#    `reranking_mode` 在服务端默认取 `retrieval_model.get("reranking_mode") or "reranking_model"`
#    —— 一旦落到 "reranking_model" 且本机没有 rerank 模型，DataPostProcessor 拿到的
#    runner 就是 None（data_post_processor.py:_get_rerank_runner），于是
#    **不融合、不去重、不按分排序、也不做 top_n 截断**，两路结果原样拼在一起返回。
#    本机没有 rerank 模型，所以只能走 weighted_score。
#  - weights：0.15.3 的 schema 里没有 weight_type 字段（只存两个数值权重），带上会被忽略
#  - score_threshold_enabled=false：本步先不做阈值过滤，阈值策略留到评估集里单独验证
TARGET = {
    "search_method": "hybrid_search",
    "reranking_enable": False,          # 无 rerank 模型时显式关闭
    "reranking_mode": "weighted_score",  # ★ 关键：不加这行，混合检索等于没融合
    "reranking_model": {"reranking_provider_name": "", "reranking_model_name": ""},
    "top_k": 5,
    "score_threshold_enabled": False,
    "score_threshold": None,
    "weights": {
        "keyword_setting": {"keyword_weight": 0.3},
        "vector_setting": {
            "vector_weight": 0.7,
            "embedding_provider_name": "ollama",
            "embedding_model_name": "bge-m3",
        },
    },
}


def build_target(emb_provider: str, emb_model: str,
                 rerank: bool, rerank_provider: str, rerank_model: str,
                 top_k: int) -> dict:
    """按参数生成 retrieval_model。

    两组 mode 是互斥的，写错会静默失效（不报错，但融合方式不对）：
      reranking_enable=False → reranking_mode 必须是 "weighted_score"，
                               走 weight_rerank 用两个权重做加权融合；
      reranking_enable=True  → reranking_mode 必须是 "reranking_model"，
                               走真正的 cross-encoder 重排序。
    """
    t = json.loads(json.dumps(TARGET))       # 深拷贝
    t["weights"]["vector_setting"]["embedding_provider_name"] = emb_provider
    t["weights"]["vector_setting"]["embedding_model_name"] = emb_model
    t["top_k"] = top_k
    if rerank:
        t["reranking_enable"] = True
        t["reranking_mode"] = "reranking_model"
        t["reranking_model"] = {"reranking_provider_name": rerank_provider,
                                "reranking_model_name": rerank_model}
    return t


def main() -> None:
    def opt(flag, default=None):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

    only_show = "--show" in sys.argv
    token = dify.login()
    ds_id = DATASET_ID
    if opt("--dataset-name"):
        want = opt("--dataset-name")
        rows = dify.req("GET", f"{dify.API}/datasets?page=1&limit=100", token=token)[1].get("data", [])
        hit = [k for k in rows if k.get("name") == want]
        if not hit:
            raise SystemExit(f"[x] 找不到知识库「{want}」")
        ds_id = hit[0]["id"]
        print(f"[i] 知识库「{want}」 id={ds_id}")

    target = build_target(
        emb_provider=opt("--emb-provider", "ollama"),
        emb_model=opt("--emb-model", "bge-m3"),
        rerank=("--rerank" in sys.argv),
        rerank_provider=opt("--rerank-provider", "tongyi"),
        rerank_model=opt("--rerank-model", "gte-rerank-v2"),
        top_k=int(opt("--top-k", "5")),
    )

    d = dify.req("GET", f"{dify.API}/datasets/{ds_id}", token=token)[1]
    cur = d.get("retrieval_model_dict") or d.get("retrieval_model") or {}
    print("=== 当前 retrieval_model ===")
    print(json.dumps(cur, ensure_ascii=False, indent=2))

    if only_show:
        return

    st, r = dify.req("PATCH", f"{dify.API}/datasets/{ds_id}", token=token, body={
        "retrieval_model": target,
    })
    print(f"\n[PATCH] http={st}")
    if st != 200:
        print(str(r)[:800])
        raise SystemExit(1)

    d2 = dify.req("GET", f"{dify.API}/datasets/{ds_id}", token=token)[1]
    got = d2.get("retrieval_model_dict") or d2.get("retrieval_model") or {}
    print("=== 回读 retrieval_model ===")
    print(json.dumps(got, ensure_ascii=False, indent=2))

    vw = float(((got.get("weights") or {}).get("vector_setting") or {}).get("vector_weight", 0))
    ok = (
        got.get("search_method") == target["search_method"]
        and vw == 0.7
        and int(got.get("top_k") or 0) == target["top_k"]
        and got.get("reranking_mode") == target["reranking_mode"]
        and bool(got.get("reranking_enable")) == target["reranking_enable"]
    )
    print("\n[校验]", "通过 ✓" if ok else "未完全落库 ✗")


if __name__ == "__main__":
    main()
