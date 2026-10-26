#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑一轮完整流程，打印核验分布。

    python scripts/probe_roundtrip.py ["问题"] [kb|web|both]

这是改完提示词、改完核验逻辑之后唯一的验证手段。看四个数：

    带引文的引注比例   低于 80% 说明提示词没约束住，C2 会大面积跳过
    C2 的通过/拦截     全通过说明核验没在干活（可能是比对太松）
    逐句回查比例       低于 60% 说明依据段有句子在原文里找不到出处
    总耗时             超过 60 秒要考虑砍方向数

需要先起服务（另一个终端 `python app/server.py`）。
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8848"
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with NO_PROXY.open(req, timeout=400) as r:
        return json.loads(r.read())


def poll(job_id: str, limit: int = 400) -> dict:
    for _ in range(limit):
        req = urllib.request.Request(BASE + "/v1/jobs/" + job_id)
        with NO_PROXY.open(req, timeout=20) as r:
            d = json.loads(r.read())
        if d.get("state") != "running":
            return d
        time.sleep(2)
    return {"state": "timeout"}


def job(op: str, body: dict) -> dict:
    d = post("/v1/jobs", {"op": op, **body})
    if not d.get("ok"):
        print("提交失败", d)
        sys.exit(1)
    r = poll(d["job_id"])
    if r.get("state") != "done":
        print("任务没完成", r.get("state"), str(r.get("error"))[:200])
        sys.exit(1)
    return r["result"]


q = sys.argv[1] if len(sys.argv) > 1 else \
    "南京老门西历史文化街区更新，建筑高度和密度能不能突破现行控规"
scope = sys.argv[2] if len(sys.argv) > 2 else "kb"

t0 = time.time()
print("① plan …", flush=True)
pl = job("plan", {"q": q})
plan = pl["plan"]
print(f"   {pl.get('ms', 0)} ms，{len(plan['tasks'])} 个方向")

print("② gather …", flush=True)
g = job("gather", {"plan": plan, "run_id": pl.get("run_id", ""), "scope": scope})
items = [it for t in g["tasks"] for it in t.get("items", [])]
seen, picked = set(), []
for it in items:
    if it["title"] in seen or not it.get("body_key"):
        continue
    if it.get("eff") in ("expired", "repealed", "superseded"):
        continue
    seen.add(it["title"])
    picked.append(it)
    if len(picked) >= 14:
        break
print(f"   {g.get('ms', 0)} ms，候选 {len(items)}，选中 {len(picked)}")

print("③ write …", flush=True)
w = job("write", {"q": q, "plan": plan, "picked": picked,
                  "run_id": pl.get("run_id", ""), "default": []})
vd = w.get("verify") or {}
print(f"   {w.get('total_ms', 0)} ms，{len(w.get('answer') or '')} 字")
print(f"   结论 {vd.get('strength')}　{vd.get('reason', '')[:60]}")

print("\n④ 核验分布")
stat: dict[str, dict[str, int]] = {}
for c in vd.get("cites") or []:
    for chk in c.get("checks") or []:
        k = (chk.get("name") or "?")[:2]
        d = stat.setdefault(k, {})
        d[chk.get("state", "?")] = d.get(chk.get("state", "?"), 0) + 1
for k in sorted(stat):
    d = stat[k]
    print(f"   {k}  通过 {d.get('pass', 0):>2}  拦截 {d.get('fail', 0):>2}"
          f"  改写 {d.get('warn', 0):>2}  未核 {d.get('skip', 0):>2}")

print(f"\n   引注 {len(vd.get('cites') or [])} 处，逐句回查 {vd.get('ratio', 0) * 100:.0f}%")
n_q = sum(1 for c in vd.get("cites") or [] if c.get("quote"))
print(f"   带引文的引注 {n_q}/{len(vd.get('cites') or [])}")

print("\n⑤ 依据段前 3 条")
ev = ""
for seg in (w.get("answer") or "").split("## "):
    if seg.startswith("依据"):
        ev = seg
        break
for ln in [x for x in ev.split("\n") if x.strip()][1:4]:
    print("   " + ln[:150])

print(f"\n总耗时 {(time.time() - t0) / 1000:.1f} 秒")
