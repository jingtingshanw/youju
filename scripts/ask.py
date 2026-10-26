#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核验入口 —— 问一个问题，拿到可直接交付的结论。

这是**人用的那一层**。链路是：

    问题 ──► Dify 应用（检索 + 生成）──► 闸门 2 校验（不调模型）──► 结构化交付

为什么要有它，而不是直接用 Dify 控制台：
  · Dify 控制台的回答是**未校验**的原始输出。谁也不能保证它没引错条号。
  · 这里把闸门 2 接在后面，交付给用户的是**校验后的结论强度**：
    可作答 / 部分作答 / 拒答，以及每一项校验的明细。
  · 每次问答落盘一份完整记录（问题、召回段落、回答、校验结果），可事后追溯。

用法：
    python scripts/ask.py "怀化市扬尘污染防治条例由谁统一监督管理？"
    python scripts/ask.py -i                     # 交互模式，逐条问
    python scripts/ask.py --batch questions.txt  # 批量（每行一个问题）
    python scripts/ask.py -i --json              # 交互 + 打印完整 JSON
    python scripts/ask.py --app app-v6.json "某某问题"

输出分三段：
    ① 交付内容：结论 / 依据 / 时效提示 / 引用清单（校验后的形态）
    ② 校验明细：四项检查与引用逐条核对
    ③ 记录位置：本次问答的完整 JSON 落在哪个文件
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gatekeeper as gk  # noqa: E402
import legalkit as lk  # noqa: E402

RECORDS = lk.ROOT / "eval" / "results" / "ask-records"
BADGE = {"可作答": "✓ 可作答", "部分作答": "! 部分作答", "拒答": "✕ 拒答"}

# 回答里可能出现的区块标题。抠取某一节时，遇到其中任何一个就停 ——
# 不能只靠空行判断：模型经常把「依据」直接写在结论下一行（不带 # 号、不空行），
# 第一版就是这么把整段依据吸进【结论】里的。
KNOWN_HEADS = ("结论", "依据", "依据清单", "引用清单", "边界判定", "历史沿革",
               "时效提示", "说明", "补充说明", "备注", "注")

# 标题的序号前缀。模型常写「### 2. 结论」「三、依据」「（二）引用清单」，
# 直接拿 `startswith("结论")` 去比会全部落空 —— 实测就出现过「结论」那一栏
# 抽成了边界判定的标题行（"1. 边界判定"），因为带序号的标题既不像标题、
# 也不像内容，被当成了正文第一段。
_HEAD_NUM = re.compile(
    r"^(?:(?:\d+|[一二三四五六七八九十]+)\s*[.、)）]\s*"
    r"|[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[)）]\s*)+"
)


def _strip_head_mark(s: str) -> str:
    """抹掉标题的序号前缀：'2. 结论' → '结论'，'（三）依据' → '依据'。"""
    return _HEAD_NUM.sub("", s).strip()


def load_app(path: str | None) -> dict:
    if path:
        p = Path(path)
    else:
        p = lk.ROOT / "eval" / "app-v6.json"
        if not p.exists():
            p = lk.ROOT / "eval" / "app.json"
    if not p.exists():
        raise SystemExit(f"[x] 找不到应用凭据文件 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def call_app(info: dict, query: str) -> dict:
    import urllib.error
    import urllib.request
    body = {"inputs": {}, "query": query, "response_mode": "blocking", "user": "ask-cli"}
    r = urllib.request.Request(info["base"] + "/chat-messages",
                               data=json.dumps(body).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer " + info["api_key"])
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": f"http={e.code} {e.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def _section(answer: str, *heads: str) -> str:
    """从回答里抠出一节内容（按标题行切，遇到下一个标题就停）。抠不到返回空串。

    先整体摘掉【边界判定】块：它的字段行里「结论边界：可作答」也以「结论」开头，
    不摘掉的话会把区块起点定位到边界判定块内部，结论永远是空的。
    （真实调用踩到过：模型写「### 结论」做标题时，抽出来的结论是一串【引用清单】。）
    """
    body = lk.BOUNDARY_BLOCK_RE.sub("", answer or "")
    out: list[str] = []
    on = False
    for ln in body.split("\n"):
        s = ln.strip().lstrip("#").strip().strip("*").strip()
        h = _strip_head_mark(s)  # 标题判断要看抹掉序号后的形态
        if not on:
            if any(h.startswith(x) for x in heads):
                on = True
            continue
        if h.startswith("【") or any(h.startswith(x) for x in KNOWN_HEADS):
            break
        out.append(s)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


def _first_paragraph(answer: str) -> str:
    """抠不到「结论」标题时的退化方案：拿到边界判定块之后的第一个实质段落。

    模型不总是严格按格式写标题（有时用加粗、有时干脆不写），与其让用户看到
    「（见下方原始回答）」，不如给一个最接近的段落。

    **这里是逐行扫的状态机，不是正则截断**，原因是踩过一次：
    第一版写成「先用 `BOUNDARY_BLOCK_RE.sub` 删掉边界块，再按空行取第一段」，
    而 `BOUNDARY_BLOCK_RE` 的结束标记要求下一行是【…】或强标题词 —— 模型不写
    「### 结论」标题时，结束标记一直找不到，于是**把边界块后面的结论段一起吃掉**，
    最后返回的是「依据：」之后那段，用户看到的结论成了一串条号：
        实际输出 → 【结论】【引用清单】怀化市扬尘污染防治条例|第五条
    逐行扫不会出现这种错位：谁在边界块里由「结论边界：」这一行明确划界，
    与「下一个标题长什么样」无关。
    """
    lines = (answer or "").split("\n")
    out: list[str] = []
    in_boundary = False
    for ln in lines:
        s = ln.strip().lstrip("#").strip().strip("*").strip().strip("`").strip()
        h = _strip_head_mark(s)
        if "【边界判定】" in s or h.startswith("边界判定"):
            in_boundary = True
            continue
        if in_boundary:
            if s.startswith("结论边界"):
                in_boundary = False
            continue
        if not s:
            if out:  # 段落结束
                break
            continue
        if h.startswith("【"):
            break
        if any(h.startswith(x) and len(h) <= 12 for x in KNOWN_HEADS):
            break
        out.append(s)
    text = " ".join(out).strip()
    for pfx in ("结论：", "结论:", "答案：", "答案:"):
        if text.startswith(pfx):
            return text[len(pfx):].strip()
    return text


def analyze(query: str, resp: dict) -> dict:
    """把一次问答整理成结构化记录 —— 命令行与演示前端共用这一份。

    **不打印任何东西**。凡是"给用户看的形态"都从这里派生，
    这样 CLI 与网页不会出现两套判定文案（`render()` 负责文字形态）。
    """
    answer = resp.get("answer") or ""
    res = ((resp.get("metadata") or {}).get("retriever_resources")) or []
    v = gk.verify(query, answer, res)
    manifest, mstat = lk.parse_citation(answer)
    bfields, bbad = lk.parse_boundary(answer)

    if v.boundary == "拒答":
        concl = "本次检索结果中没有能支持该问题的条文，无法给出结论。"
    else:
        concl = _section(answer, "结论", "答案") or _first_paragraph(answer)

    cited_names = [x for x in (lk.match_law(l) for l, _ in manifest) if x]
    touched = list(dict.fromkeys(lk.query_laws(query, include_full_names=True) + cited_names))
    staleness = []
    for name in touched:
        info = lk.stale_info(name)
        if info:
            staleness.append({"name": name, "status": info.get("时效性"),
                              "abolished": info.get("废止日期"),
                              "reason": info.get("废止依据"),
                              "in_ledger": True})
        else:
            staleness.append({"name": name, "status": "未知",
                              "abolished": None, "reason": None, "in_ledger": False})

    return {
        "question": query,
        "answer": answer,
        "resources": res,
        "n_res": len(res),
        "boundary": v.boundary,
        "decided_by": v.decided_by,
        "model_boundary": v.model_boundary,
        "boundary_fields": bfields,
        "boundary_missing": bbad,
        "badge": BADGE[v.boundary],
        "stale_laws": [{"name": n, "info": lk.stale_info(n) or {}} for n in v.stale_laws],
        "conclusion": concl,
        "staleness": staleness,
        "manifest": [[l, a] for l, a in manifest],
        "manifest_status": mstat,
        "cites": [{"raw_law": c.raw_law, "law": c.law, "art": c.art,
                   "status": c.status, "ok": c.ok, "note": c.note,
                   "source": c.source} for c in v.cites],
        "checks": [{"code": c.code, "name": c.name, "level": c.level,
                    "passed": c.passed, "detail": c.detail} for c in v.checks],
        "reasons": v.reasons,
        "verdict_text": v.render(),
    }


def render(rec: dict) -> None:
    """把结构化记录打印成三段式报告（给人看）。"""
    line = "─" * 78
    print(line)
    print(f"问：{rec['question']}")
    print(line)
    print(f"判定：{rec['badge']}　（由 {rec['decided_by']} 判定"
          + (f"，模型自报 {rec['model_boundary']}" if rec["model_boundary"] else "") + "）")
    for s in rec["stale_laws"]:
        info = s["info"]
        print(f"      ⚠ 时效：《{s['name']}》{info.get('时效性')}"
              f"（{info.get('废止日期') or '—'}）")
    print()

    if rec["boundary"] == "拒答":
        print(f"【结论】{rec['conclusion']}")
    else:
        print(f"【结论】{rec['conclusion'] or '（见下方原始回答）'}")

    print()
    print("【依据】")
    if rec["cites"]:
        for c in rec["cites"]:
            mark = "✓" if c["ok"] else "✗"
            tgt = c["law"] or f"{c['raw_law']}（库内无此法规）"
            print(f"  {mark} 《{tgt}》{c['art']}　—— {c['note']}")
    elif rec["manifest"]:
        print("  （清单解析异常）")
    else:
        print("  （无可用依据）")

    print()
    print("【相关文件时效】")
    if rec["staleness"]:
        for s in rec["staleness"]:
            if s["in_ledger"]:
                flag = "⚠" if s["status"] in lk.STALE_STATES else "✓"
                extra = f"，失效日 {s['abolished']}" if s["abolished"] else ""
                print(f"  {flag} 《{s['name']}》{s['status']}{extra}")
            else:
                print(f"  ? 《{s['name']}》不在时效台账中（时效状态未知，不做有利假设）")
    else:
        print("  —（问题未指向库内法规，回答也未引用库内法规）")

    print()
    print("【引用清单】")
    if rec["manifest"]:
        for law, art in rec["manifest"]:
            print(f"  {law}|{art}")
    else:
        print(f"  无（清单状态：{rec['manifest_status']}）")

    print()
    print(line)
    print(rec["verdict_text"])


def save(records: list[dict]) -> Path:
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = RECORDS / f"ask-{stamp}.json"
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def main() -> None:
    def opt(flag):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None

    info = load_app(opt("--app"))
    as_json = "--json" in sys.argv
    questions: list[str] = []

    if "--batch" in sys.argv:
        questions = [l.strip() for l in
                     Path(opt("--batch")).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
    elif "-i" in sys.argv or "--interactive" in sys.argv:
        print("交互模式：输入问题回车提问，空行或 Ctrl-C 退出。")
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            questions.append(q)
            # 交互模式边问边答，避免攒到最后
            resp = call_app(info, q)
            if resp.get("_error"):
                print(f"[x] {resp['_error']}")
                questions.pop()
                continue
            rec = analyze(q, resp)
            render(rec)
            if as_json:
                print(json.dumps(rec, ensure_ascii=False, indent=2))
        records = []  # 交互模式不重复落盘
        return
    else:
        rest = [a for a in sys.argv[1:] if not a.startswith("-")]
        # 排除掉作为参数值的那些（--app 后面那个）
        for flag in ("--app", "--batch"):
            val = opt(flag)
            if val in rest:
                rest.remove(val)
        questions = rest

    if not questions:
        print(__doc__)
        return

    records = []
    for q in questions:
        t0 = time.perf_counter()
        resp = call_app(info, q)
        if resp.get("_error"):
            print(f"[x] {q[:40]} → {resp['_error']}")
            continue
        rec = analyze(q, resp)
        render(rec)
        rec["seconds"] = round(time.perf_counter() - t0, 1)
        records.append(rec)

    p = save(records)
    print(f"\n本次 {len(records)} 条问答已记录到 {p}")
    counts: dict[str, int] = {}
    for r in records:
        counts[r["boundary"]] = counts.get(r["boundary"], 0) + 1
    print("结论分布：" + "　".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
