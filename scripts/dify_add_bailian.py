#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把百炼（通义）接进本机 Dify，补齐向量模型与重排序模型。

Dify 0.15.3 是插件系统之前的版本，模型供应商硬编码在镜像里。
镜像自带的 `tongyi` 供应商支持 llm / text-embedding / rerank 三类，
正好是本机缺的后两类。

两个细节：
  - 镜像是 2024 年底的，它认识的向量模型只到 `text-embedding-v3`（1024 维，可用），
    重排序只认老的 `gte-rerank`（本账号 403 无权限）。所以：
      · 向量直接用预置的 `text-embedding-v3`
      · 重排序必须**自建模型项**指向 `gte-rerank-v2`
  - tongyi 的 `configurate_methods` 含 `customizable-model`，所以自建模型是官方支持的路径。

前置条件：宿主机转发代理已启动（见 scripts/host_proxy.py），
否则容器连不上 dashscope.aliyuncs.com。

用法：python scripts/dify_add_bailian.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]      # → ai-pm/
sys.path.insert(0, str(ROOT / "scripts"))
import dify  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

PROVIDER = "tongyi"
EMB_MODEL = "text-embedding-v3"      # 预置，1024 维
RERANK_MODEL = "gte-rerank-v2"       # 自建


def load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DASHSCOPE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("[x] .env 里没有 DASHSCOPE_API_KEY")


def main() -> None:
    key = load_key()
    tk = dify.login()
    P = f"{dify.API}/workspaces/current/model-providers/{PROVIDER}"

    print("[1] 写入供应商级凭据")
    st, d = dify.req("POST", P, token=tk, body={"credentials": {"dashscope_api_key": key}})
    print(f"    http={st}  {str(d)[:180]}")

    print("\n[2] 自建重排序模型 gte-rerank-v2")
    st, d = dify.req("POST", P + "/models", token=tk, body={
        "model": RERANK_MODEL,
        "model_type": "rerank",
        "credentials": {"dashscope_api_key": key, "context_size": "4000"},
    })
    print(f"    http={st}  {str(d)[:220]}")

    print("\n[3] 自建向量模型（v4 也试一下，能成就用 v4）")
    st, d = dify.req("POST", P + "/models", token=tk, body={
        "model": "text-embedding-v4",
        "model_type": "text-embedding",
        "credentials": {"dashscope_api_key": key, "context_size": "8192"},
    })
    print(f"    http={st}  {str(d)[:220]}")

    print("\n[4] 当前 tongyi 下已登记的模型")
    st, d = dify.req("GET", P + "/models", token=tk)
    if st == 200:
        for m in d.get("data", []):
            print(f"    {m.get('model_type'):16s} {m.get('model')}  status={m.get('status')}")
    else:
        print(f"    http={st} {str(d)[:200]}")

    print("\n[5] 供应商状态")
    st, d = dify.req("GET", f"{dify.API}/workspaces/current/model-providers", token=tk)
    if st == 200:
        for p in d.get("data", []):
            print(f"    {p.get('provider'):22s} {p.get('status')} "
                  f"custom={p.get('custom_configuration', {}).get('status')}")


if __name__ == "__main__":
    main()
