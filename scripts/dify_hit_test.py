#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对 Dify 知识库做召回实测（hit-testing）。

用途：证明「建好的库真的能召回正确条文」，同时暴露检索失败模式。
probe 集只做冒烟测试；正式的 L1–L4 分层评估集在 eval/ 下另建。

用法：
    python dify_hit_test.py                # 跑内置 probe 集
    python dify_hit_test.py "自定义问题"     # 单条
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
import dify  # noqa: E402

DATASET_NAME = "政策法规语料库"

RETRIEVAL = {
    "search_method": "hybrid_search",
    "reranking_enable": False,
    # ★ 这一行不能少：Dify 服务端默认取 "reranking_model"，本机没有 rerank 模型时
    #   DataPostProcessor 拿不到 runner，混合检索的两路结果就不做融合/去重/截断，
    #   会原样返回 2×top_k 条（且关键词侧那些记录的 score 是 None → 显示成 0.000）。
    #   详见 research/03-检索配置修复记录.md。
    "reranking_mode": "weighted_score",
    "reranking_model": {"reranking_provider_name": "", "reranking_model_name": ""},
    "weights": {"keyword_setting": {"keyword_weight": 0.3},
                "vector_setting": {"vector_weight": 0.7,
                                   "embedding_provider_name": "ollama",
                                   "embedding_model_name": "bge-m3"}},
    "top_k": 5,
    "score_threshold_enabled": False,
    "score_threshold": None,
}

# (问题, 期望命中的法规, 期望条款关键词, 备注)
PROBES = [
    ("建设工程施工扬尘污染防治由哪个部门负责？", "怀化市扬尘污染防治条例", "第五条", "L1 事实定位"),
    ("生活垃圾分类管理责任人应当履行哪些职责？", "葫芦岛市生活垃圾分类管理条例", "第二十四条", "L1 事实定位"),
    ("深圳经济特区对违法建筑的查处程序是怎么规定的？", "深圳经济特区规划土地监察条例", "", "L1 事实定位"),
    ("中国（海南）自由贸易试验区内的土地征收由谁批准？", "海南决定", "第四十六条", "L2 需要读懂授权表"),
    ("夜间进行产生噪声的建筑施工作业有什么限制？", "噪声污染防治法", "", "L3 边界：该法已废止"),
    ("天山天池风景名胜区内可以开矿吗？", "昌吉回族自治州天山天池风景名胜区保护管理条例", "", "L1 事实定位"),
    ("呼伦贝尔市对店铺招牌和户外广告有什么管理要求？", "呼伦贝尔市城市市容和环境卫生管理条例", "", "L1 事实定位"),
    ("生态环境法典对扬尘污染是怎么规定的？", None, "", "L4 对抗：语料中根本没有这部法典"),
]


def find_dataset(token: str) -> str:
    st, d = dify.req("GET", f"{dify.API}/datasets?page=1&limit=100", token=token)
    for k in d.get("data", []):
        if k.get("name") == DATASET_NAME:
            return k["id"]
    raise SystemExit("[x] 找不到知识库，先跑 dify_build_kb.py")


def hit(token: str, ds_id: str, query: str) -> list[dict]:
    st, d = dify.req("POST", f"{dify.API}/datasets/{ds_id}/hit-testing", token=token,
                     body={"query": query, "retrieval_model": RETRIEVAL})
    if st != 200 or not isinstance(d, dict):
        return [{"error": f"http={st} {str(d)[:200]}"}]
    return d.get("records", [])


def main() -> None:
    token = dify.login()
    ds_id = find_dataset(token)
    print(f"知识库 id={ds_id}\n")

    if len(sys.argv) > 1:
        for r in hit(token, ds_id, sys.argv[1]):
            print(json.dumps(r, ensure_ascii=False, indent=1)[:1500])
        return

    ok = 0
    print("=" * 96)
    for q, expect_doc, expect_art, tag in PROBES:
        rows = hit(token, ds_id, q)
        if rows and "error" in rows[0]:
            print(f"[!] {q}\n    {rows[0]['error']}")
            continue
        docs = [(r.get("segment", {}).get("document", {}) or {}).get("name", "?") for r in rows]
        segs = [r.get("segment", {}).get("content", "") for r in rows]
        scores = [float(r.get("score") or 0.0) for r in rows]
        top1 = docs[0] if docs else "(空)"
        got = expect_doc is None or any((expect_doc or "") in (d or "") for d in docs[:3])
        if got:
            ok += 1
        flag = "OK " if got else "MISS"
        print(f"[{flag}] {tag}  {q}")
        print(f"     期望≒{expect_doc or '（不应命中具体法规）'}"
              + (f" {expect_art}" if expect_art else ""))
        if scores:
            print(f"     top1={top1}  分数={scores[0]:.3f}")
        else:
            print("     （无结果）")
        for i, (d, s, sg) in enumerate(zip(docs, scores, segs)):
            head = (sg or "").replace("\n", " ")[:52]
            print(f"       {i+1}. {s:.3f} {str(d)[:30]:<32} {head}")
        print("-" * 96)
    print(f"\n命中 top3 的比例：{ok}/{len(PROBES)}")


if __name__ == "__main__":
    main()
