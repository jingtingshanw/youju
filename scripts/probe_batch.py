#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""五道题跑完整链路，出一份能贴进 PRD 的实测记录。

    python scripts/probe_batch.py                 # 跑内置的五道题
    python scripts/probe_batch.py --only 1        # 只跑第一道（调提示词时用）
    python scripts/probe_batch.py --out x.md

## 为什么要五道而不是一道

南京那道题是调优时一直盯着的题，改到后来它已经是「背过的题」——
提示词为它改过三遍，它通过不说明任何事。换一道没见过的题型就现原形。
所以这五道题各占一个位置：

  1 南京老门西      强覆盖地域 + 专业判断（改控规能不能突破）
  2 昭通危旧房      新补的库，验证「刚抓的正文真的进了检索」
  3 上海产业用地    一线城市，台账里 expired 最多的地方
  4 福建历史建筑    第三个新补库，且属于「补助标准」这类要给数字的题
  5 攀枝花闲置土地  库里基本没有，专门验「没有的时候怎么回答」——
                   这是产品最容易被骂的一种情况：找不到还硬编一段。

第 5 题不追求高分。它低分是对的。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8848"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

QUESTIONS = [
    ("南京 · 强覆盖 + 专业判断",
     "南京老门西历史文化街区更新，建筑高度和密度能不能突破现行控规"),
    ("昭通 · 新补库",
     "昭通市城市更新中，危旧房拆除重建的补偿标准怎么确定"),
    ("上海 · 一线城市",
     "上海市产业用地全生命周期管理，土地出让后几年内要达到约定产出"),
    ("福建 · 新补库 + 要给数字",
     "福建省历史建筑修缮有没有财政补助，补助比例是多少"),
    ("攀枝花 · 库里没有",
     "攀枝花市工业园区闲置土地处置，闲置满两年能不能无偿收回"),
]


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with OPENER.open(req, timeout=400) as r:
        return json.loads(r.read())


def _poll(job_id: str, limit: int = 400) -> dict:
    for _ in range(limit):
        req = urllib.request.Request(BASE + "/v1/jobs/" + job_id)
        with OPENER.open(req, timeout=20) as r:
            d = json.loads(r.read())
        if d.get("state") != "running":
            return d
        time.sleep(2)
    return {"state": "timeout"}


def _job(op: str, body: dict) -> dict:
    d = _post("/v1/jobs", {"op": op, **body})
    if not d.get("ok"):
        raise SystemExit(f"提交失败（{op}）：{d}")
    r = _poll(d["job_id"])
    if r.get("state") != "done":
        raise SystemExit(f"任务没跑完（{op}）：{r.get('state')} {str(r.get('error'))[:200]}")
    return r["result"]


def run_one(q: str, scope: str = "kb") -> dict:
    """走 plan → gather → write，返回这一题的全部可量化指标。"""
    t0 = time.time()
    pl = _job("plan", {"q": q})
    plan = pl["plan"]
    g = _job("gather", {"plan": plan, "run_id": pl.get("run_id", ""),
                        "scope": scope})
    items = [it for t in g["tasks"] for it in t.get("items", [])]
    # 选材规则跟界面上的 autoPick 一致：同标题去重、跳过失效、
    # 最多 14 份。不一致的话，测出来的数不代表用户看到的数。
    seen, picked, dead = set(), [], 0
    for it in items:
        if it["title"] in seen or not it.get("body_key"):
            continue
        if it.get("eff") in ("expired", "repealed", "superseded"):
            dead += 1
            continue
        seen.add(it["title"])
        picked.append(it)
        if len(picked) >= 14:
            break

    w = _job("write", {"q": q, "plan": plan, "picked": picked,
                       "run_id": pl.get("run_id", ""), "default": []})
    vd = w.get("verify") or {}
    cites = vd.get("cites") or []
    answer = w.get("answer") or ""
    # 降级不体现在返回字段里，只体现在正文口气上：raw_report 会明说
    # 「这一份是原文条文清单，不是分析结论」。有这句话就是降级过。
    fb = "走了原文清单降级" if "原文条文" in answer else ""

    stat: dict[str, dict[str, int]] = {}
    for c in cites:
        for chk in c.get("checks") or []:
            k = (chk.get("name") or "?")[:2]
            d = stat.setdefault(k, {})
            d[chk.get("state", "?")] = d.get(chk.get("state", "?"), 0) + 1

    return {
        "q": q,
        "tasks": len(plan["tasks"]),
        "cand": len(items),
        "picked": len(picked),
        "dead": dead,
        "chars": len(answer),
        "sec": round(time.time() - t0, 1),
        "strength": vd.get("strength"),
        "reason": vd.get("reason", ""),
        "cites": len(cites),
        "with_quote": sum(1 for c in cites if c.get("quote")),
        "ratio": vd.get("ratio", 0),
        "stat": stat,
        "answer": answer,
        "fallback": fb,
    }


def _count(d: dict, st: str) -> int:
    return d.get(st, 0)


def to_md(rows: list[dict], day: str) -> str:
    L = [f"# 五题实测 {day}", "",
         "跑法：`python scripts/probe_batch.py`。每题走完整的 plan → gather → write，",
         "选材规则与界面一致（同标题去重、跳过失效件、最多 14 份）。", ""]

    L += ["## 一、总表", "",
          "| # | 题 | 方向 | 候选 | 选中 | 跳过失效 | 字数 | 耗时 | 结论 | 逐句回查 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        name = r["q"][:14] + ("…" if len(r["q"]) > 14 else "")
        L.append(
            f"| {i} | {name} | {r['tasks']} | {r['cand']} | {r['picked']} | "
            f"{r['dead']} | {r['chars']} | {r['sec']}s | "
            f"{r['strength'] or '—'} | {r['ratio'] * 100:.0f}% |")

    L += ["", "## 二、核验分布", "",
          "C1 引注可追溯 / C2 引文逐字核 / C3 时效 / C4 地域 / C5 立场。",
          "「未核」多说明那一层没在干活，不是因为干净。", "",
          "| # | C1 过/拦/改/未 | C2 | C3 | C4 | C5 | 带引文 |",
          "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        cells = []
        for k in ("C1", "C2", "C3", "C4", "C5"):
            d = r["stat"].get(k)
            if not d:
                cells.append("—")
            else:
                cells.append("/".join(str(_count(d, s))
                                      for s in ("pass", "fail", "warn", "skip")))
        L.append(f"| {i} | " + " | ".join(cells) +
                 f" | {r['with_quote']}/{r['cites']} |")

    L += ["", "## 三、逐题", ""]
    for i, r in enumerate(rows, 1):
        L += [f"### {i}. {r['q']}", ""]
        L.append(f"- 结论强度 **{r['strength'] or '—'}**：{r['reason'][:120]}")
        if r["fallback"]:
            L.append(f"- ⚠ 降级：{r['fallback']}")
        ev = ""
        for seg in r["answer"].split("## "):
            if seg.startswith("依据"):
                ev = seg
                break
        head = [x for x in ev.split("\n") if x.strip()][1:3]
        for h in head:
            L.append("> " + h.strip()[:220])
        L.append("")

    L += ["## 四、读这张表要注意什么", ""]
    L += ["**1. 回查率最低的那道是拒答题，不是质量问题。** 福建那道题的报告原文写着"
          "「本次 A 类 14 份文件里，没有一条规范历史建筑修缮财政补助的条文，"
          "故本段无可用引文」，后面那些句子讲的是「为什么别的条文不能拿来当依据」——"
          "这些话当然在原文里找不到，逐句回查自然低。"
          "**回查率对「如实说没有」的报告不适用**：看它有没有硬编一个补助比例出来，"
          "比看这个百分数重要。", "",
          "**2. 同一道题跑两次，回查率能差 30 个点。** 昭通那道题第一次 54%、第二次 85%，"
          "中间没改任何代码。所以这个数是抽样指标，不是稳定分数："
          "**别拿单次结果下结论，也别为了让它好看去调提示词。**", "",
          "**3. C5 全是「未核」是结构性的，不是坏了。** C5 判「引的法规是不是你问的那部」，"
          "这五道题都没点名法规，判据不存在，按规矩就得标未核而不能默认通过。"
          "服务端算「未核率」时已经排除了 C5。", "",
          "**4. 库里没有的那道题，表现比有资料的更好看。** 攀枝花抽到 14 份但只有部分有正文，"
          "模型引了《闲置土地处置办法》并自己标注「该办法正文不在本次资料库内，"
          "此条为方向性提示，不得直接引用」，C2 随后拦了 1 条。"
          "**这才是这套系统存在的理由**：知道自己没有，就把那条降成提示而不是依据。", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, default=0, help="只跑第 N 题（1 起）")
    ap.add_argument("--scope", default="kb", choices=["kb", "both", "web"])
    ap.add_argument("--out", default="")
    ap.add_argument("--rerender", default="",
                    help="读上次的结果 json 重出 markdown，不调模型")
    a = ap.parse_args()

    day = dt.date.today().strftime("%Y%m%d")

    if a.rerender:
        src = Path(a.rerender)
        rows = json.loads(src.read_text(encoding="utf-8"))
        out = Path(a.out) if a.out else src.with_suffix(".md")
        out.write_text(to_md(rows, day), encoding="utf-8")
        print(f"重出 {out}（{len(rows)} 题，未调模型）")
        return 0

    todo = QUESTIONS[a.only - 1:a.only] if a.only else QUESTIONS
    rows = []
    for i, (tag, q) in enumerate(todo, 1):
        idx = a.only if a.only else i
        print(f"[{idx}/{len(QUESTIONS)}] {tag}", flush=True)
        print(f"      {q}", flush=True)
        r = run_one(q, a.scope)
        rows.append(r)
        print(f"      方向 {r['tasks']}　候选 {r['cand']}　选中 {r['picked']}"
              f"　{r['chars']} 字　{r['sec']}s　{r['strength']}"
              f"　回查 {r['ratio'] * 100:.0f}%　引文 {r['with_quote']}/{r['cites']}",
              flush=True)
        if r["fallback"]:
            print(f"      ⚠ 降级：{r['fallback']}", flush=True)

    out = Path(a.out) if a.out else ROOT / "output" / f"五题实测-{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_md(rows, day), encoding="utf-8")
    # 同时留一份原始结果。markdown 是给人看的，json 是给「改了措辞重出报告」
    # 和后面做统计用的——不然每改一句话都要重跑一遍模型，白花钱。
    js = out.with_suffix(".json")
    js.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写出 {out}")
    print(f"     {js.name}（原始结果，改措辞时不用重跑）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
