#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量一次问答的真实成本与延迟。

为什么单独做一个脚本：`mvp/server.py` 的 `call_llm()` 只取了 `choices[0].message.content`，
把响应里的 `usage` 丢掉了。所以整个项目跑到今天，**一次问答花多少钱、首字多久，
从来没有被记下来过**。面试问「单次成本多少」时，靠估是不行的。

本脚本不改产品代码，只用同一套 system prompt、同一个检索器、同一份上下文拼装方式，
把请求重新发一遍，并把 `usage` 与流式首字时间读出来。

    python scripts/measure_cost.py                 # 四层各取一题，共 4 次调用
    python scripts/measure_cost.py --n 3            # 每层 3 题
    python scripts/measure_cost.py --question "…"   # 只测指定问题
    python scripts/measure_cost.py --price-in 0.0008 --price-out 0.002

价格默认值取自阿里云百炼 qwen-plus 的公开价目（元 / 千 token），**用前请核对当日价目**——
本脚本只负责把 token 量准，不负责价格永远正确。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "mvp"))

import server as srv  # noqa: E402

GOLDEN = ROOT / "eval" / "golden-set.csv"


def post(pv: dict, body: dict, timeout: int = 180) -> urllib.request.Request:
    req = urllib.request.Request(
        pv["base"] + "/chat/completions",
        data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + pv["api_key"])
    return req


def ask_once(engine: srv.Engine, question: str) -> dict:
    """发两次请求：一次非流式拿 usage，一次流式拿首字延迟。"""
    hits = engine.retriever.search(question, k=engine.topk)
    context = srv.build_context(hits)
    user = (f"【检索到的条文】\n{context}\n\n"
            f"【用户问题】\n{question}\n\n"
            f"{srv.OUTPUT_SPEC}")
    base = {"model": engine.pv["model"],
            "messages": [{"role": "system", "content": engine.system},
                         {"role": "user", "content": user}],
            "temperature": 0.1, "max_tokens": 1800}

    # ① 非流式：要 usage
    t0 = time.perf_counter()
    with urllib.request.urlopen(post(engine.pv, base), timeout=180) as r:
        d = json.loads(r.read().decode("utf-8"))
    total_ms = (time.perf_counter() - t0) * 1000
    u = d.get("usage") or {}

    # ② 流式：要首字延迟
    t1 = time.perf_counter()
    first_ms = None
    with urllib.request.urlopen(post(engine.pv, dict(base, stream=True)), timeout=180) as r:
        for line in r:
            if line.startswith(b"data: ") and b"[DONE]" not in line:
                first_ms = (time.perf_counter() - t1) * 1000
                break

    return {"question": question,
            "in_tokens": u.get("prompt_tokens"),
            "out_tokens": u.get("completion_tokens"),
            "total_ms": round(total_ms),
            "first_ms": round(first_ms) if first_ms else None,
            "context_chars": len(context),
            "system_chars": len(engine.system),
            "hits": len(hits)}


def pick_questions(n: int) -> list[dict]:
    rows = list(csv.DictReader(open(GOLDEN, encoding="utf-8-sig")))
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["分层"], []).append(r)
    picked = []
    for layer in ("L1基础", "L2推理", "L3边界", "L4对抗"):
        picked.extend(by.get(layer, [])[:n])
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1, help="每层取几题")
    ap.add_argument("--question", default="", help="只测这一句")
    ap.add_argument("--price-in", type=float, default=0.0008, help="元/千输入 token")
    ap.add_argument("--price-out", type=float, default=0.002, help="元/千输出 token")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    engine = srv.Engine()
    print(f"模型  {engine.pv['provider']}/{engine.pv['model']}")
    print(f"语料  {len(engine.chunks)} 条款 / {len({c['law'] for c in engine.chunks})} 部")
    print(f"价目  输入 ¥{a.price_in}/千 token，输出 ¥{a.price_out}/千 token"
          f"（核对当日价目）\n")

    if a.question:
        cases = [{"输入": a.question, "分层": "手工"}]
    else:
        cases = pick_questions(a.n)

    rows = []
    for c in cases:
        try:
            m = ask_once(engine, c["输入"])
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {c['分层']}　{type(e).__name__}: {e}")
            continue
        cost = (m["in_tokens"] or 0) / 1000 * a.price_in + \
               (m["out_tokens"] or 0) / 1000 * a.price_out
        m["cost"] = cost
        m["layer"] = c["分层"]
        rows.append(m)
        print(f"  {c['分层']:<6} 入 {m['in_tokens']:>6} / 出 {m['out_tokens']:>5} token"
              f"　首字 {m['first_ms']:>5} ms　全程 {m['total_ms']:>5} ms"
              f"　¥{cost:.5f}")
        print(f"         上下文 {m['context_chars']} 字 · 系统提示 {m['system_chars']} 字"
              f" · 召回 {m['hits']} 条")

    if not rows:
        print("\n全部失败，未产生数据。")
        return

    n = len(rows)
    avg_in = sum(r["in_tokens"] or 0 for r in rows) / n
    avg_out = sum(r["out_tokens"] or 0 for r in rows) / n
    avg_cost = sum(r["cost"] for r in rows) / n
    tm = [r["total_ms"] for r in rows]
    fm = [r["first_ms"] for r in rows if r["first_ms"]]

    print(f"\n── 汇总（{n} 次调用）" + "─" * 40)
    print(f"平均每次　入 {avg_in:.0f} + 出 {avg_out:.0f} token")
    print(f"单次成本　¥{avg_cost:.5f}")
    for q in (100, 1000, 5000, 10000):
        print(f"  {q:>6} 次/月 → ¥{avg_cost * q:,.2f}/月")
    print(f"总耗时　  中位 {sorted(tm)[n // 2]} ms　最大 {max(tm)} ms")
    if fm:
        print(f"首字延迟　中位 {sorted(fm)[len(fm) // 2]} ms　最大 {max(fm)} ms")

    out = ROOT / "eval" / "results" / "cost_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": f'{engine.pv["provider"]}/{engine.pv["model"]}',
                               "price": {"in": a.price_in, "out": a.price_out},
                               "measured_at": time.strftime("%Y-%m-%d %H:%M"),
                               "rows": rows,
                               "summary": {"avg_in": round(avg_in, 1),
                                           "avg_out": round(avg_out, 1),
                                           "avg_cost": round(avg_cost, 6)}},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
