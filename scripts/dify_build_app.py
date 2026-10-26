#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""建「政策核验·基线」应用（chat 模式），把语料库挂上去，并写死引用约束提示词。

为什么要有这个应用：
  第 4 步的基线跑分如果只测检索层，测不出产品真正要解决的问题 ——
  「模型会不会把检索到的东西讲成别的法规」「会不会在没依据时编造」这两件事
  发生在**生成环节**，不在召回环节。所以必须有一个能端到端问答的应用。

提示词在这里是产品的一部分，不是装饰。它编码了四条硬约束：
  1. 引用存在性检查 —— 回答里每个法规名/条号都必须能在本次检索结果里反查到
  2. 时效标注     —— 命中文件若已废止/失效，必须先声明再回答
  3. 条号越界检查 —— 引用的条号不得超过该法规实际条数
  4. 时效硬规则   —— 为现在或将来的行为取条号依据、而所依据法规已废止/失效时，必须拒答条号
                      （v3 新增。旧版只说「标注历史参考」，结果仍然把条号交了出去，见 research/08）

v3 起另外要求模型输出【边界判定】四行字段（引用法规在库内 / 所问条号核验 /
时效状态 / 结论边界）。前三行是可核对事实，第四行由前三行唯一推出 ——
判分器据此做**机械核对**，不再靠扫正文猜模型想干什么。

幂等：应用名已存在则复用，模型配置每次覆盖。

用法：
    python scripts/dify_build_app.py
    python scripts/dify_build_app.py --prompt-file prompts/system-v3.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parents[1] / "scripts"))  # ai-pm/scripts
import dify  # noqa: E402

APP_NAME = "政策核验·基线"
OUT_JSON = None
DATASET_NAME = "政策法规语料库"

# 应用级检索配置（与知识库侧那份是两回事，都要写）
APP_EMB_PROVIDER = "ollama"
APP_EMB_MODEL = "bge-m3"
APP_RERANK = False
APP_RERANK_PROVIDER = "tongyi"
APP_RERANK_MODEL = "gte-rerank-v2"
APP_TOP_K = 5
DEFAULT_PROMPT = ROOT / "prompts" / "system-v3.md"

PROMPT_FALLBACK = """你是城乡规划领域的法规核验助手。你只依据【检索到的条文】作答，绝不使用检索结果之外的记忆。

## 三条硬约束（违反其一即为不合格回答）

1. **引用存在性**：回答中出现的每一部法规名称、每一个条号，都必须能在本次检索结果中找到对应文本。
   找不到就在对应位置写「本次检索结果中无相关内容」，不要把检索到的其他法规内容改挂到别的法规名下。
   特别注意：某部法规出现在检索结果的**元数据**里（例如作为废止依据、制定依据被提及），
   不等于库里存在这部法规的全文。这种情形一律按「无依据」处理。

2. **时效状态**：如果命中的条文所属文件处于废止/失效状态，必须在引用前先用一行声明，格式：
   「⚠ 时效提示：《名称》已于 YYYY-MM-DD 失效（原因），以下内容仅作历史参考」
   若无法确定时效状态，写「时效状态未知」。

3. **条号越界**：引用的条号必须在所引法规的条数范围内。不确定某条是否存在时，不要写条号，改写成
   「该法规相关条款」并说明无法定位。

## 输出格式

先给结论（一句话），再给依据。依据逐条列出，每条格式：
- 【《法规全名》第X条】原文关键句……（必要时注明「该条文与本问题的关系」）

若依据全部不存在，直接回答：「本次检索结果中没有能支持该问题的条文，无法给出结论。」并说明你检索到了什么、它们为什么不能作为依据。
不要为了显得有用而给出推测性的法律意见。"""


def find_dataset(token: str) -> str:
    d = dify.req("GET", f"{dify.API}/datasets?page=1&limit=100", token=token)[1]
    for k in d.get("data", []):
        if k.get("name") == DATASET_NAME:
            return k["id"]
    raise SystemExit(f"[x] 找不到知识库「{DATASET_NAME}」，先跑 dify_build_kb.py")


def find_app(token: str, name: str) -> str | None:
    d = dify.req("GET", f"{dify.API}/apps?page=1&limit=100", token=token)[1]
    for a in d.get("data", []):
        if a.get("name") == name:
            return a["id"]
    return None


def ensure_app(token: str) -> str:
    app_id = find_app(token, APP_NAME)
    if app_id:
        print(f"[=] 复用已有应用 {APP_NAME}  id={app_id}")
        return app_id
    st, d = dify.req("POST", f"{dify.API}/apps", token=token, body={
        "name": APP_NAME, "mode": "chat",
        "icon_type": "emoji", "icon": "📘", "icon_background": "#E4FBCC",
        "description": "政策效力核验与可溯源引用 —— 基线应用（本地 Dify）",
    })
    if st not in (200, 201):
        raise SystemExit(f"[x] 建应用失败 http={st} {str(d)[:400]}")
    app_id = d["id"]
    print(f"[+] 新建应用 {APP_NAME}  id={app_id}")
    return app_id


def build_config(ds_id: str, prompt: str) -> dict:
    """chat 应用的完整模型配置。结构对齐 GET /apps/{id}/export 导出的 DSL 的 model_config。

    注意：应用自己有 `dataset_configs` 这一份检索配置，**不**等于知识库里存的那份。
    两份都得写对，否则会出现「库里开了重排、应用没开」这种静默不一致。
    """
    return {
        "pre_prompt": prompt,
        "prompt_type": "simple",
        "chat_prompt_config": {},
        "completion_prompt_config": {},
        "dataset_query_variable": None,
        "agent_mode": {"enabled": False, "prompt": None, "strategy": None, "tools": []},
        "annotation_reply": {"enabled": False},
        "external_data_tools": [],
        "more_like_this": {"enabled": False},
        "opening_statement": None,
        "suggested_questions": [],
        "suggested_questions_after_answer": {"enabled": False},
        # 打开它，响应里才会带回 retriever_resources，评估脚本才能核对「究竟引了哪几段」
        "retriever_resource": {"enabled": True},
        "sensitive_word_avoidance": {"enabled": False, "type": "", "configs": []},
        "speech_to_text": {"enabled": False},
        "text_to_speech": {"enabled": False},
        "user_input_form": [],
        "file_upload": {"image": {"enabled": False, "number_limits": 3,
                                  "detail": "high", "transfer_methods": ["remote_url", "local_file"]}},
        "model": {
            "provider": "deepseek", "name": "deepseek-chat", "mode": "chat",
            "completion_params": {"temperature": 0.2, "top_p": 0.9, "max_tokens": 1024},
        },
        "dataset_configs": {
            "retrieval_model": "multiple",
            "top_k": APP_TOP_K,
            "score_threshold": None,
            "score_threshold_enabled": False,
            "reranking_enabled": APP_RERANK,
            # ★ 与知识库侧一致：不加 weighted_score，混合检索不会融合
            "reranking_mode": "reranking_model" if APP_RERANK else "weighted_score",
            "reranking_model": ({"reranking_provider_name": APP_RERANK_PROVIDER,
                                 "reranking_model_name": APP_RERANK_MODEL}
                                if APP_RERANK else
                                {"reranking_provider_name": "", "reranking_model_name": ""}),
            "weights": {
                "keyword_setting": {"keyword_weight": 0.3},
                "vector_setting": {"vector_weight": 0.7,
                                   "embedding_provider_name": APP_EMB_PROVIDER,
                                   "embedding_model_name": APP_EMB_MODEL},
            },
            "datasets": {
                "strategy": "router",
                "datasets": [{"dataset": {"enabled": True, "id": ds_id}}],
            },
        },
    }


def ensure_api_key(token: str, app_id: str) -> str:
    st, d = dify.req("GET", f"{dify.API}/apps/{app_id}/api-keys?page=1&limit=100", token=token)
    for k in (d or {}).get("data", []):
        if k.get("type") == "app":
            print("[=] 复用已有 API Key")
            return k["token"]
    st, d = dify.req("POST", f"{dify.API}/apps/{app_id}/api-keys", token=token, body={})
    if st not in (200, 201):
        raise SystemExit(f"[x] 建 API Key 失败 http={st} {str(d)[:400]}")
    print("[+] 已创建 API Key")
    return d["token"]


def main() -> None:
    global APP_NAME, DATASET_NAME, APP_EMB_PROVIDER, APP_EMB_MODEL
    global APP_RERANK, APP_RERANK_PROVIDER, APP_RERANK_MODEL, APP_TOP_K

    def opt(flag, default=None):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

    if opt("--dataset"):
        DATASET_NAME = opt("--dataset")
    APP_EMB_PROVIDER = opt("--emb-provider", APP_EMB_PROVIDER)
    APP_EMB_MODEL = opt("--emb-model", APP_EMB_MODEL)
    APP_RERANK = "--rerank" in sys.argv
    APP_RERANK_PROVIDER = opt("--rerank-provider", APP_RERANK_PROVIDER)
    APP_RERANK_MODEL = opt("--rerank-model", APP_RERANK_MODEL)
    APP_TOP_K = int(opt("--top-k", str(APP_TOP_K)))
    pf = DEFAULT_PROMPT
    if opt("--prompt-file"):
        pf = Path(opt("--prompt-file"))
    if opt("--name"):
        APP_NAME = opt("--name")
    prompt = pf.read_text(encoding="utf-8") if pf.exists() else PROMPT_FALLBACK
    if not pf.exists():
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(PROMPT_FALLBACK, encoding="utf-8")
        print(f"[i] 提示词文件不存在，已写入默认版 {pf}")

    token = dify.login()
    ds_id = find_dataset(token)
    app_id = ensure_app(token)

    st, d = dify.req("POST", f"{dify.API}/apps/{app_id}/model-config", token=token,
                     body=build_config(ds_id, prompt))
    print(f"[model-config] http={st}")
    if st != 200:
        print(str(d)[:800])
        raise SystemExit(1)

    key = ensure_api_key(token, app_id)

    # 回读校验：确认 dataset_configs 真的落库了
    st, d = dify.req("GET", f"{dify.API}/apps/{app_id}", token=token)
    mc = (d or {}).get("model_config") or {}
    dc = mc.get("dataset_configs") or {}
    ids = [x["dataset"]["id"] for x in (dc.get("datasets") or {}).get("datasets", [])]
    print(f"[回读] 绑定知识库={ids}  top_k={dc.get('top_k')}  "
          f"rerank_enabled={dc.get('reranking_enabled')}  rerank_mode={dc.get('reranking_mode')}  "
          f"rerank_model={dc.get('reranking_model')}")
    print(f"[回读] 向量={((dc.get('weights') or {}).get('vector_setting'))}")
    print(f"[回读] 提示词长度={len(mc.get('pre_prompt') or '')} 字符")

    info = {"app_id": app_id, "api_key": key, "dataset_id": ds_id,
            "base": dify.BASE + "/v1", "name": APP_NAME}
    out = ROOT / "eval" / (sys.argv[sys.argv.index("--out") + 1]
                           if "--out" in sys.argv else "app.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ok] 应用就绪，凭据写入 {out}")


if __name__ == "__main__":
    main()
