#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 data/kb_text/*.txt 灌进本机 Dify，建成可检索的知识库，并做一次召回实测。

幂等：同名知识库已存在则复用；同名文档已存在则跳过。
依赖 dify.py（同目录）提供的登录与请求封装。

用法：
    python dify_build_kb.py            # 全流程：配供应商 -> 建库 -> 上传 -> 等待索引
    python dify_build_kb.py --status   # 只看索引状态
"""
from __future__ import annotations

import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# dify.py 是本机通用工具，放在 ai-pm/scripts/；项目脚本目录也放一份以便独立运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "scripts"))
import dify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "data" / "kb_text"

PROVIDER = "ollama"
EMB_MODEL = "bge-m3"
OLLAMA_BASE = "http://ollama:11434"          # Dify 容器内可达
EMB_CONTEXT = "8192"
DATASET_NAME = "政策法规语料库"
# 一条一段：入库文本已是「标题一行 + 每条第 1 行」，故用换行切分即可命中条文粒度
SEPARATOR = "\n"
MAX_TOKENS = 1024


# ---------------------------------------------------------------- 底层请求

def req_raw(method: str, path: str, token: str, body: bytes, ctype: str, timeout: int = 300):
    r = urllib.request.Request(dify.BASE + path, data=body, method=method)
    r.add_header("Content-Type", ctype)
    r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def upload_file(token: str, path: Path) -> str:
    boundary = "----wbboundary" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "text/plain"
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    st, d = req_raw("POST", "/console/api/files/upload", token, body,
                    f"multipart/form-data; boundary={boundary}")
    if st not in (200, 201) or not isinstance(d, dict) or "id" not in d:
        raise SystemExit(f"[x] 上传失败 {path.name} http={st}: {str(d)[:300]}")
    return d["id"]


# ---------------------------------------------------------------- 步骤

def _dashscope_key() -> str:
    for line in (Path(__file__).resolve().parents[3] / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DASHSCOPE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("[x] ai-pm/.env 里没有 DASHSCOPE_API_KEY")


def _creds() -> dict:
    """按供应商给出正确的凭据结构。

    这两家的凭据位置完全不同，混用会 400：
      ollama  —— customizable-model，凭据挂在模型上，字段是 base_url
      tongyi  —— 有 provider 级凭据，字段是 dashscope_api_key
    """
    if PROVIDER == "tongyi":
        return {"dashscope_api_key": _dashscope_key()}
    return {"base_url": OLLAMA_BASE}


def ensure_provider(token: str) -> None:
    """ollama 属于 customizable-model 供应商：没有 provider 级凭据，凭据挂在模型上。

    Dify 对该类供应商的 provider 级写入会返回 400，属预期行为，忽略即可。
    tongyi 则有 provider 级凭据，正常写入。
    """
    st, d = dify.req("POST", f"{dify.API}/workspaces/current/model-providers/{PROVIDER}",
                     token=token, body={"credentials": _creds()})
    if st in (200, 201):
        print(f"  供应商 {PROVIDER}: 凭据已写入")
    else:
        print(f"  供应商 {PROVIDER}: 跳过 provider 级凭据（customizable-model 供应商不需要），"
              f"http={st}")


def ensure_default_model(token: str) -> None:
    """把工作区默认向量模型指向 ollama/bge-m3。

    建库时 Dify 取的是「工作区默认向量模型」（出厂默认是 openai 的 text-embedding-ada-002）。
    不改这里，POST /datasets 会直接报 credentials is not initialized。
    """
    st, d = dify.req("GET", f"{dify.API}/workspaces/current/default-model?model_type=text-embedding",
                     token=token)
    cur = (d or {}).get("data") or {}
    print(f"  当前默认向量模型: {cur.get('provider')}/{cur.get('model')}")
    if cur.get("provider") == PROVIDER and cur.get("model") == EMB_MODEL:
        return
    st, d = dify.req("POST", f"{dify.API}/workspaces/current/default-model", token=token,
                     body={"model_settings": [{"model_type": "text-embedding",
                                               "provider": PROVIDER, "model": EMB_MODEL}]})
    print(f"  设为 {PROVIDER}/{EMB_MODEL}: http={st} {str(d)[:160]}")


def ensure_model(token: str) -> None:
    st, d = dify.req("GET", f"{dify.API}/workspaces/current/model-providers/{PROVIDER}/models",
                     token=token)
    have = {m.get("model") for m in (d.get("data") or [])} if st == 200 else set()
    if EMB_MODEL in have:
        print(f"  模型 {EMB_MODEL}: 已存在")
        return
    st, d = dify.req("POST", f"{dify.API}/workspaces/current/model-providers/{PROVIDER}/models",
                     token=token,
                     body={"model": EMB_MODEL, "model_type": "text-embedding",
                           "credentials": {**_creds(), "context_size": EMB_CONTEXT}})
    print(f"  模型 {EMB_MODEL}: http={st} {str(d)[:160]}")


def find_dataset(token: str):
    st, d = dify.req("GET", f"{dify.API}/datasets?page=1&limit=100", token=token)
    if st != 200:
        return None
    for k in d.get("data", []):
        if k.get("name") == DATASET_NAME:
            return k
    return None


def create_dataset(token: str) -> str:
    st, d = dify.req("POST", f"{dify.API}/datasets", token=token,
                     body={"name": DATASET_NAME, "description": "8 份政策法规，按条文切分",
                           "indexing_technique": "high_quality"})
    if st != 201:
        raise SystemExit(f"[x] 建库失败 http={st}: {str(d)[:300]}")
    print(f"  新建知识库 id={d['id']}")
    return d["id"]


def list_docs(token: str, ds_id: str) -> set[str]:
    st, d = dify.req("GET", f"{dify.API}/datasets/{ds_id}/documents?page=1&limit=100", token=token)
    if st != 200:
        return set()
    return {x.get("name") for x in d.get("data", [])}


RETRIEVAL = {
    "search_method": "hybrid_search",
    "reranking_enable": False,
    "reranking_mode": None,
    "reranking_model": {"provider": "", "model": ""},
    "weights": {"weight_type": "customized",
                "keyword_setting": {"keyword_weight": 0.3},
                "vector_setting": {"vector_weight": 0.7, "embedding_provider_name": PROVIDER,
                                   "embedding_model_name": EMB_MODEL}},
    "top_k": 4,
    "score_threshold_enabled": False,
    "score_threshold": 0.5,
}


def repair_error_docs(token: str, ds_id: str) -> None:
    """把处于 error 状态的文档删掉重传。

    Dify 的 `processing/resume` 只对 paused 生效，对 error 会返回
    `Document is not in paused status`，所以没有官方「重试」按钮可用，
    只能走删除 + 重传这条路。入库文本是可再生的（build_kb_text_v2.py 产出的），
    所以删除没有信息损失。
    """
    st, d = dify.req("GET", f"{dify.API}/datasets/{ds_id}/documents?page=1&limit=100", token=token)
    bad = [x for x in d.get("data", []) if x.get("indexing_status") == "error"]
    if not bad:
        print("  没有处于 error 的文档")
        return
    names = [x.get("name") for x in bad]
    print(f"  error 文档 {len(bad)} 个：{names}")
    for x in bad:
        st, r = dify.req("DELETE", f"{dify.API}/datasets/{ds_id}/documents/{x['id']}", token=token)
        print(f"    删除 {x.get('name')[:30]:32s} http={st}")
    wanted = {Path(n).stem for n in names}
    files = [f for f in sorted(KB_DIR.glob("*.txt")) if f.stem in wanted]
    print(f"  重传 {len(files)} 个文件")
    add_documents(token, ds_id, files)


def add_documents(token: str, ds_id: str, files: list[Path]) -> None:
    fids = []
    for p in files:
        fid = upload_file(token, p)
        fids.append(fid)
        print(f"    上传 {p.name} -> {fid}")
    body = {
        "indexing_technique": "high_quality",
        "data_source": {
            "type": "upload_file",
            "info_list": {"data_source_type": "upload_file",
                          "file_info_list": {"file_ids": fids}},
        },
        "process_rule": {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [
                    {"id": "remove_extra_spaces", "enabled": True},
                    {"id": "remove_urls_emails", "enabled": True},
                ],
                "segmentation": {"separator": SEPARATOR, "max_tokens": MAX_TOKENS},
            },
        },
        "doc_form": "text_model",
        "doc_language": "Chinese",
        "embedding_model": EMB_MODEL,
        "embedding_model_provider": PROVIDER,
        "retrieval_model": RETRIEVAL,
    }
    st, d = dify.req("POST", f"{dify.API}/datasets/{ds_id}/documents", token=token, body=body)
    print(f"  建文档: http={st} {json.dumps(d, ensure_ascii=False)[:400] if isinstance(d, dict) else str(d)[:400]}")


def indexing_status(token: str, ds_id: str):
    st, d = dify.req("GET", f"{dify.API}/datasets/{ds_id}/indexing-status", token=token)
    return st, d


def wait_indexed(token: str, ds_id: str, timeout: int = 900) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, d = indexing_status(token, ds_id)
        if st != 200 or not isinstance(d, dict):
            print(f"  [warn] indexing-status http={st}")
            time.sleep(5)
            continue
        rows = d.get("data", [])
        done = sum(1 for r in rows if r.get("indexing_status") == "completed")
        err = [r for r in rows if r.get("indexing_status") in ("error", "paused")]
        total = len(rows)
        seg = sum(r.get("completed_segments", 0) or 0 for r in rows)
        print(f"  [{int(time.time()-t0):>3}s] 完成 {done}/{total}，累计段落 {seg}"
              + (f"，异常 {len(err)}" if err else ""))
        if total and done == total:
            return
        time.sleep(8)
    print("  [warn] 等待索引超时")


def main() -> None:
    global KB_DIR, DATASET_NAME, PROVIDER, EMB_MODEL, OLLAMA_BASE, EMB_CONTEXT

    def opt(flag: str) -> str | None:
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None

    # 支持多套入库文本并列建库（例如 kb_text 与 kb_text_v2），便于跑 A/B 对照
    if opt("--kb-dir"):
        KB_DIR = ROOT / "data" / opt("--kb-dir")
    if opt("--dataset-name"):
        DATASET_NAME = opt("--dataset-name")
    # 向量模型可换供应商：本机原本只有 ollama/bge-m3，接入百炼后多了 tongyi/text-embedding-v*
    if opt("--provider"):
        PROVIDER = opt("--provider")
    if opt("--emb-model"):
        EMB_MODEL = opt("--emb-model")
    if opt("--emb-base"):
        OLLAMA_BASE = opt("--emb-base")
    if opt("--emb-context"):
        EMB_CONTEXT = opt("--emb-context")

    token = dify.login()
    print(f"[0] 入库目录 {KB_DIR.name}  知识库名「{DATASET_NAME}」")
    print("[1] 配置向量模型")
    ensure_provider(token)
    ensure_model(token)
    ensure_default_model(token)

    print("[2] 知识库")
    k = find_dataset(token)
    ds_id = k["id"] if k else create_dataset(token)
    if k:
        print(f"  复用已有知识库 id={ds_id}（docs={k.get('document_count')}）")

    files = sorted(KB_DIR.glob("*.txt"))
    if not files:
        raise SystemExit("[x] data/kb_text 为空，先跑 build_corpus.py")

    if "--repair" in sys.argv:
        print("[3] 修复 error 文档")
        repair_error_docs(token, ds_id)
        print("[4] 等待索引完成")
        wait_indexed(token, ds_id)
        return

    if "--status" in sys.argv:
        print("[3] 索引状态")
        wait_indexed(token, ds_id, timeout=60)
        return

    have = list_docs(token, ds_id)
    todo = [p for p in files if p.name not in have]
    if todo:
        print(f"[3] 入库 {len(todo)} 个文件（已存在 {len(have)} 个，跳过）")
        add_documents(token, ds_id, todo)
    else:
        print("[3] 全部文档已在库中，跳过上传")

    print("[4] 等待索引完成")
    wait_indexed(token, ds_id)

    st, d = dify.req("GET", f"{dify.API}/datasets/{ds_id}?page=1&limit=100", token=token)
    docs = d.get("documents") if isinstance(d, dict) and st == 200 else None
    st2, d2 = dify.req("GET", f"{dify.API}/datasets/{ds_id}/documents?page=1&limit=100", token=token)
    print("\n=== 结果 ===")
    if st2 == 200:
        print(f"  文档数 {d2.get('total')}")
    st3, d3 = indexing_status(token, ds_id)
    if st3 == 200:
        for r in d3.get("data", []):
            print(f"  {str(r.get('name'))[:44]:<46} {r.get('indexing_status'):<10} "
                  f"段落 {r.get('completed_segments')}/{r.get('total_segments')}")


if __name__ == "__main__":
    main()
