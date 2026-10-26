#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端基线跑分（第 4 步主表）。

把 30 道题送进「政策核验·基线」应用，拿回**回答**而不只是召回段落，
然后按可复现的规则打分。检索层测不出的事都发生在这一层：
模型会不会把 A 法规的条文挂到 B 法规名下、会不会在没依据时编造、会不会漏报时效。

主口径 —— 来自应用最后一行强制输出的【引用清单】（见 prompts/system-v1.md）
  引用可追溯率  retr  清单里每条 (法规名, 条号) 都能在本次召回的段落里反查到
  无依据引用率  alien 清单里反查不到的条目 —— **这是幻觉的硬指标，不是猜测**
  拒答正确率    refu  L4 层是否在无依据时拒答（拒答=对，硬答=错）
  清单格式合规  fmt  模型有没有按约定输出清单（missing 说明提示词没被遵守）

辅口径 —— 直接扫回答正文
  判据词命中率  tok   回答有没有答到点上
  时效提示率    stale L3 层是否给出时效状态声明

为什么让应用输出【引用清单】：第一版判分直接正则扫正文里的《法规名》，
结果 4 条**完全正确**的拒答回答里有 3 条被误判成「挂错法规」—— 因为拒答时本来就
要点名那部不存在的法规来说它不存在。正文里「提到某法规」和「把某条内容挂到该法规名下」
在用词上分不开，任何正则都救不回来（试过整句否定判定和 ±28 字窗口，误报 2/4 到 3/4）。
改成让模型显式登记它**实际依赖**的条目，判分就从猜测变成了核对。
这个清单本身也是产品要交付的东西（可溯源引用的载体），不是为测试临时加的。

用法：
    python scripts/eval_run_app.py
    python scripts/eval_run_app.py --layer L4
    python scripts/eval_run_app.py --json eval/results/app-baseline.json
    python scripts/eval_run_app.py --quiet      # 只打汇总表
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "golden-set.csv"
APP = ROOT / "eval" / "app.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import legalkit as lk  # noqa: E402

# 名称归一 / 条号解析 / 三张表读取 / 【边界判定】解析 / 文本比对 —— 全部走 legalkit。
# 这里**刻意不再各写一份**：产品校验层（gatekeeper.py）用的是同一套底层，
# 只有这样才能保证"评测通过 = 上线通过"。此前这些函数在两处各有一份实现，
# 已经造成过口径分歧（`query_laws` 的子串匹配、`parse_boundary` 的枚举取值）。
TOKEN_RE = re.compile(r"『([^』]+)』")

# 只有「明确说没有答案」才算拒答。注意不要把「无法确认时效状态」算进来 ——
# 那是给答案配的时效声明，不是拒答（L1-12、L2-07 就被这个误判过）。
# 主判据其实不靠这些词，而是【引用清单】是否为「无」，见 judge()。
REFUSE_MARKERS = ["无法给出结论", "无法给出可靠答案", "无法回答", "无法提供",
                  "没有能支持", "无相关内容", "没有相关内容", "未检索到",
                  "检索结果中没有", "检索结果中无", "不具备依据", "无依据",
                  "不存在这部", "不包含"]
# 注意：不要放裸的「废止」「失效」——按时效条文本身就含这些字
# （噪声法第九十条「本法自…施行。〈环境噪声污染防治法〉同时废止」），
# 宽松匹配会把「如实复述条文」误记成「主动给出时效提示」。
STALE_MARKERS = ["⚠", "时效提示", "已失效", "已届满", "期限届满", "时效状态未知",
                 "无法确认是否现行有效", "无法确认其现行有效",
                 "时效状态在本次检索结果中未", "时效状态信息"]

# 「正确识别了边界」的判据（**只用于 L4**，且不进 hard_fab）。
#
# 为什么不能只用「清单是否为空」判断拒答：那是第二次踩同一个坑了。
#   第一次：用「扫正文正则」判断挂错法规 → 4 条正确拒答里 3 条误报 → 改成【引用清单】；
#   第二次：用「清单是否为空」判断是否拒答 → 6 条里 5 条误报。
# 根因相同——拿一个**不区分的代理指标**代替语义判断。
# 「清单为空」只对 B/E/H（话题整个不存在）成立。对 A/C/D/F/G，正确答案本来就是
# 「指出问题 + 可能给出部分可用依据」，清单不该为空。实测这 5 道被误报的回答，
# 每一道都明确写了「没有该法规全文 / 无法确认该条号存在 / 无法回答」。
#
# 判据 = 回答里是否出现负面边界声明。命中任一即算「正确识别了边界」。
# 保守起见它比 REFUSE_MARKERS 宽，宁可漏判失守、不要误判正确。
BOUNDARY_MARKERS = REFUSE_MARKERS + [
    "已失效", "已废止", "期限届满", "不宜再作为", "不能引用", "无法确认",
    "不涉及", "未规定", "没有规定", "无全文", "没有全文", "未能找到",
    "恢复了原", "恢复施行原",
]

# 否定语境标记：条号出现在这些词附近，是在说「这条不存在/不是这条」，不是引用
NEG_MARKERS = ["没有", "无", "不存在", "不包含", "未检索到", "不属于", "不是",
               "无法", "未能", "并非", "缺"]

# ============================================================================
# 【边界判定】四行字段（prompts/system-v3.md 第 3 节）
# ============================================================================
# 字段定义、解析、一致性推导全部在 legalkit（BOUNDARY_FIELDS / parse_boundary /
# boundary_consistency），因为产品校验层 gatekeeper.py 要用同一套。
#
# 设计动机：判分器此前**两次**用「不区分的代理指标」代替语义判断而误报
#   （第一次：扫正文正则判挂错法规；第二次：用「引用清单是否为空」判是否拒答）。
# 第三次的解法不同 —— 不再让判分器去猜模型想干什么，而是让模型把**客观事实**
# 显式登记出来，由程序机械核对。
#
# 四行里前三行都是可核对的事实（库里有没有全文 / 那个条号存不存在 / 法规还有没有效），
# 由它们可以**唯一推出**第四行「结论边界」应当是什么。第四行是模型的判断。
# 两者不一致 = 自相矛盾 = 不合格，而这个检查不需要任何语义猜测。
#
# L4-26 就是被这一层抓住的：它前三行的事实全对（法规已废止，模型自己都写了），
# 但结论区说「处罚决定书应引用第七十七条」—— 事实与结论不兼容。


def stale_violation_check(ans: str, b: dict[str, str | None],
                          query: str) -> tuple[list[str], list[str]]:
    """时效硬规则（提示词第 4 条）核查。返回 (违规项, 模型自报与真值不符项)。

    违规的两种形态：
      ① 自报「可作答」，而命中的法规已废止或已失效 —— 字段层的自相矛盾（最省事的一种）
      ② 正文里出现「应当引用第X条」这类**操作性指引**，且该法规已废止/已失效
         —— L4-26 的真身。它在字段上可能填「部分作答」（所以 ① 不一定触发），
         但结论区把条号交出去了，用户会照着写文书。

    「自报与真值不符」是另一个指标：模型说时效状态是 A，台账说是 B。
    它和违规分开，因为「说错了时效」和「给错了指引」是两类问题。
    """
    flat = ans.replace(" ", "")
    hist = lk.HISTORY_BLOCK_RE.search(flat)
    body = flat.replace(hist.group(0), "") if hist else flat

    violations: list[str] = []
    if b.get("结论边界") == "可作答" and b.get("时效状态") in lk.STALE_STATES:
        violations.append(f"自报可作答×时效={b['时效状态']}")

    # 问题指向了哪几部库内法规（书名号精确匹配，见 legalkit.query_laws 的注释）
    ledger = lk.ledger()
    touched = lk.query_laws(query)
    stale_touched = [t for t in touched
                     if (ledger.get(t) or {}).get("时效性") in lk.STALE_STATES]

    # 正文检查的开关：模型自报「拒答」时不再看正文（拒答的回答不会再给操作性指引，
    # 里面出现的条号都是复述提问或历史陈述）。**字段缺失时照做**（None != "拒答"）——
    # 正是靠这一条，v2 提示词下的 L4-26 也能被自动抓出来，
    # 而它此前被 marker 扫描判成了「已拒答」（因为正文含「已失效」二字）。
    if b.get("结论边界") != "拒答":
        for m in lk.OPERATIONAL_CITE_RE.finditer(body):
            if stale_touched:
                violations.append(f"操作性指引×已失效:{m.group(0)}")
                break

    # 自报时效 vs 台账真值
    mismatched: list[str] = []
    self_state = b.get("时效状态")
    if self_state and touched:
        truth = {ledger.get(t, {}).get("时效性") for t in touched}
        truth.discard(None)
        if len(truth) == 1:
            t = truth.pop()
            if self_state != t:
                mismatched.append(f"自报「{self_state}」≠台账「{t}」")
    return violations, mismatched


def call_app(info: dict, query: str, user: str) -> dict:
    body = {"inputs": {}, "query": query, "response_mode": "blocking", "user": user}
    r = urllib.request.Request(info["base"] + "/chat-messages",
                               data=json.dumps(body).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer " + info["api_key"])
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": f"http={e.code} {e.read().decode('utf-8', 'replace')[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def _law_hits(text: str, laws: list[tuple[str, str]]) -> list[str]:
    """一行话里出现了哪几部库内法规（按语料代号返回）。

    注意用长名优先：行里写「环境噪声污染防治法」时，短名「噪声污染防治法」
    也会被 substring 命中 —— 所以调用方必须用 len(hits) == 1 才认，
    命中两部以上一律视为「主体不确定」而跳过，宁漏不误。
    """
    t = text.replace(" ", "")
    return [short for _, short in laws if short in t]


def prose_cross_doc(ans: str, chunks: list[tuple[str, str]],
                    laws: list[tuple[str, str]]) -> list[str]:
    """盲区一：跨法规主体归错。

    eval/README.md 里记下的第二个盲区 —— L2-02 的正文写了「本次检索结果中另有
    第四十七条第（四）项」，而怀化条例只有 34 条。原判据（「条号是否出现在任意
    召回段落里」）抓不到它，因为「第四十七条」恰好出现在同批召回的**呼伦贝尔**
    段落里 —— 是主体归错，不是条号编造。

    做法：逐行扫描，维护「当前讨论主体」= 最近一个只含唯一法规名的行；
    某行出现条号、该条号不在当前主体的召回段落里、却存在于别的法规段落里 → 记一条。
    最后这个条件（必须能在**别的**法规里找到）是刻意的：它把「主体归错」和
    「条号根本不存在」分开，后者归 prose_unbacked 管，两件事不重叠。
    """
    by_law: dict[str, str] = {}
    for dn, txt in chunks:
        flat = txt.replace(" ", "")
        for _, short in laws:
            if short in dn or dn in short or (len(short) >= 4 and short[-4:] in dn):
                by_law[short] = by_law.get(short, "") + flat

    cur: str | None = None
    out: list[str] = []
    for line in ans.split("\n"):
        flat = line.replace(" ", "")
        hits = _law_hits(flat, laws)
        if len(hits) == 1:
            cur = hits[0]
        if not cur:
            continue
        for m in lk.ART_RE.finditer(flat):
            a = m.group(0)
            if a in by_law.get(cur, ""):
                continue
            if any(a in t for k, t in by_law.items() if k != cur):
                tag = f"{cur}×{a}"
                if tag not in out:
                    out.append(tag)
    return out


# 「【《法规》第X条】原文关键句」这种依据行 —— 分别捕获条号与引述文字
QUOTE_RE = re.compile(
    r"【\s*《?[^》】]{2,40}》?\s*(第[一二三四五六七八九十百千零〇0-9]{1,8}条(?:之[一二三四五六七八九十]{1,3})?)[^】]*】\s*([^\n]{12,})"
)
QUOTE_PUNCT = re.compile(r"[，。、；：（）()「」『』《》\"'’“”—\-…\s]")
# 格式里的模板词：提示词要求写「【《X》第Y条】原文关键句……」，
# 这五个字是模板不是原文。第一版没剥掉它，导致取出来的前 12 个字是
# 「原文关键句工程施工单位…」，永远匹配不上 —— 误报率一度到 53%。
QUOTE_TEMPLATE = re.compile(r"^(原文关键句|原文摘录|原文为|原文|关键句|摘录|节录)")


def quote_unbacked_check(ans: str, chunks: list[tuple[str, str]]) -> list[str]:
    """盲区二：引文级字符比对（**报告性指标，不计入硬指标**）。

    清单级校验只保证了「法规 + 条号存在」，不能保证「引述的文字与原文一致」。
    eval/README.md 记的例：v1 的 L3-06 登记的是真实存在的第四十三条，
    但它引述的文字超出了该条原文（引的是附则里「夜间」的定义）—— 自动判分没抓到。

    做法分两步：
      1. **按条号定位**：从召回段落里解析出「第X条 → 段落」，引述只在**该条号对应的
         段落**里找。这一步才是抓到 L3-06 那类「条号挂错」的关键 ——
         只搜全部段落的话，别的条文的内容也会被当作「有依据」。
      2. 召回里找不到该条号段落时，才退回全部段落。

    配对用 12 字滑动窗口（见 `legalkit.window_hit`），容忍引述时的轻微改写。

    **为什么不进 hard_fab**：概括、省略（「…」）、跨条合并都会触发它，
    误报会导向去「修」一个不存在的问题。所以只报告，作为人工复核的线索。
    """
    flats = [QUOTE_PUNCT.sub("", c) for _, c in chunks]
    by_art: dict[str, list[str]] = {}
    for f in flats:
        for m in lk.ART_RE.finditer(f):
            by_art.setdefault(m.group(0), []).append(f)

    out: list[str] = []
    for m in QUOTE_RE.finditer(ans):
        art = m.group(1)
        q = QUOTE_TEMPLATE.sub("", QUOTE_PUNCT.sub("", m.group(2)))
        if len(q) < 10:
            continue
        pool = by_art.get(art) or flats
        if not lk.window_hit(q, pool):
            out.append(q[:24])
    return out


def judge(row: dict, resp: dict, art_counts: dict[str, int] | None = None) -> dict:
    ans = (resp.get("answer") or "").strip()
    res = ((resp.get("metadata") or {}).get("retriever_resources")) or []
    src_docs = {lk.norm_doc(x.get("document_name")) for x in res}
    chunks = [(lk.norm_doc(x.get("document_name")), (x.get("content") or "")) for x in res]

    tokens = [t for t in TOKEN_RE.findall(row.get("判分规则") or "") if 2 <= len(t) <= 22]
    tokens = [t for t in tokens if not any(m in t for m in REFUSE_MARKERS)]

    manifest, mstat = lk.parse_citation(ans)

    def law_ok(law: str) -> bool:
        law = law.strip()
        return any(law in d or d in law or (len(law) >= 4 and law[-4:] in d) for d in src_docs)

    def art_ok(law: str, art: str) -> bool:
        # 清单里写「无具体条号（仅依据语料时效台账判定效力）」是**正确**的诚实做法，
        # 不能当成"条号反查不到"。第一版漏了这一类，把一条正确的时效回答判成了编造。
        a = (art or "").strip()
        if not a or a in ("-", "—", "无"):
            return True
        if a.startswith("无") or "无具体条号" in a or "未引用具体条文" in a:
            return True
        for dn, txt in chunks:
            if law in dn or dn in txt or (len(law) >= 4 and law[-4:] in dn):
                if a in txt:
                    return True
        return False

    # 正文里出现的条号，只要在任何召回段落里都找不到，就是无据条号 —— 清单管不到的洞。
    # 两个排除项，否则误报会淹掉真信号：
    #   ① 问题本身提到的条号（模型复述提问不算编造）
    #   ② 处在否定语境里的条号（「该条不属于第四十条」是在纠错，不是引用）
    all_chunk_text = "\n".join(c for _, c in chunks).replace(" ", "")
    q_text = (row.get("输入") or "").replace(" ", "")
    prose_unbacked = []
    for m in lk.ART_RE.finditer(ans.replace(" ", "")):
        a = m.group(0)
        if a in all_chunk_text or a in q_text or a in prose_unbacked:
            continue
        win = ans.replace(" ", "")[max(0, m.start() - 12): m.end() + 12]
        if any(x in win for x in NEG_MARKERS):
            continue
        prose_unbacked.append(a)
    prose_unbacked.sort(key=lambda x: lk.cn2int(x[1:-1]) or 0)

    laws = list(lk.law_pairs())
    cross_misuse = prose_cross_doc(ans, chunks, laws)
    quote_unbacked = quote_unbacked_check(ans, chunks)

    # ---- 边界判定字段 ----
    b, boundary_bad = lk.parse_boundary(ans)
    consistent, conflict = lk.boundary_consistency(b)
    stale_violation, stale_self_wrong = stale_violation_check(ans, b, row.get("输入") or "")

    bad_law = [f"{l}|{a}" for l, a in manifest if not law_ok(l)]
    bad_art = [f"{l}|{a}" for l, a in manifest if law_ok(l) and not art_ok(l, a)]

    # 把「反查不到」分类。三类性质完全不同，混在一个数字里会掩盖真问题：
    #   art_missing  条号在该文档的召回段落里找不到 —— 真·条号级编造，最严重
    #   alias        文档名口径不一致（模型写全称、库内存简称）—— 产品设计问题，非编造
    #   carried      引的是「承载文件里被引用的上位法」，粒度应为承载文件 —— 概念混淆
    #   fabricated   以上都不是 —— 真·法名级编造
    kind = {}
    for l, a in manifest:
        if law_ok(l):
            if not art_ok(l, a):
                kind[f"{l}|{a}"] = "art_missing"
            continue
        if any(len(l) >= 6 and l[-6:] in d for d in src_docs):
            kind[f"{l}|{a}"] = "alias"
            continue
        if any(l in txt for _, txt in chunks):
            kind[f"{l}|{a}"] = "carried"
            continue
        kind[f"{l}|{a}"] = "fabricated"

    # ---- 拒答 / 边界识别 ----
    # 【边界判定】字段在（v3 及以后）→ 用模型自报的「结论边界」说话；
    # 字段不在（v2 及更早的回答，--rejudge 旧结果时走这条路）→ 退回 marker 扫描。
    #
    # 两个指标分得很清，因为它们回答不同问题：
    #   refused     字面拒答（结论边界=拒答）—— 用于看模型是否明确说「没有依据」
    #   boundary_ok 识别了边界（拒答 **或** 部分作答）—— L4 的正确答案
    # 「部分作答」在 L4 上同样正确：A/C/D/F/G 类题的正解本来就是
    # 「指出问题 + 给出部分可用依据」，不是一律拒答。上一轮把这类判成失守，
    # 就是拿「拒绝」这一个动作去套所有边界形态。
    if b.get("结论边界"):
        refused = b["结论边界"] == "拒答"
        boundary_ok = b["结论边界"] in ("拒答", "部分作答")
    else:
        refused = (mstat == "none") or any(m in ans for m in BOUNDARY_MARKERS)
        boundary_ok = refused

    # L4 通过 = 识别了边界 + 字段不自相矛盾 + 没触时效硬规则 + 命中答案键的期望边界。
    # 四项都要，缺一项就说明「嘴上认了、手上没认」。
    # 期望边界来自 golden-set.csv 的「期望边界」列（见 build_eval_set.py 的
    # BOUNDARY_BY_FORM）—— 判分器读答案键，不去猜题目意图。
    expect = [x for x in (row.get("期望边界") or "").split("|") if x]
    # expect_ok 三态：True 命中 / False 不符 / None 无法核对（旧回答没有边界字段）。
    # 区分 None 很重要：v2 及更早的回答本来就无从核对，把它们算成「不通过」
    # 会让新旧对照失真（旧配置会显示 0/40）。None 时该维度不参与判定。
    expect_ok = None if not b.get("结论边界") else \
        ((not expect) or (b.get("结论边界") in expect))
    l4_pass = boundary_ok and consistent and not stale_violation and (expect_ok is not False)

    return {
        "id": row["id"], "layer": row["分层"], "query": row["输入"],
        "answer": ans, "n_res": len(res), "src_docs": sorted(src_docs),
        # 存下原始召回段落，--rejudge 时不用再调模型就能重算判分
        "resources": [{"name": n, "content": c} for n, c in chunks],
        "tokens": tokens,
        "tok_hit": (any(t in ans for t in tokens) if tokens else None),
        "manifest": manifest, "manifest_status": mstat,
        "bad_law": bad_law, "bad_art": bad_art, "alien": bad_law + bad_art,
        "alien_kind": kind,
        "prose_unbacked": prose_unbacked,
        # 只把「真编造」计入硬指标：清单里反查不到 + 正文里无据的条号 + 跨法规挪用
        "hard_fab": ([k for k, v in kind.items() if v in ("art_missing", "fabricated")]
                     + [f"正文无据条号:{a}" for a in prose_unbacked]
                     + [f"跨法规挪用:{c}" for c in cross_misuse]),
        "cross_misuse": cross_misuse,
        "quote_unbacked": quote_unbacked,
        # 边界判定（v3 起）
        "boundary": b,
        "boundary_bad": boundary_bad,
        "boundary_conflict": conflict,
        "stale_violation": stale_violation,
        "stale_self_wrong": stale_self_wrong,
        "refused": refused,
        "boundary_ok": boundary_ok,
        "expect_boundary": expect,
        "expect_ok": expect_ok,
        "l4_pass": l4_pass,
        "stale": any(m in ans for m in STALE_MARKERS),
        "error": resp.get("_error"),
    }


def main() -> None:
    only = sys.argv[sys.argv.index("--layer") + 1] if "--layer" in sys.argv else None
    quiet = "--quiet" in sys.argv
    rejudge = "--rejudge" in sys.argv

    with GOLD.open(encoding="utf-8-sig", newline="") as f:
        gold = {g["id"]: g for g in csv.DictReader(f)}

    if rejudge:
        # 复用已保存的回答与召回段落，只重算判分 —— 调整判据时不必再烧一遍模型调用
        src = Path(sys.argv[sys.argv.index("--rejudge") + 1]) if len(sys.argv) > sys.argv.index("--rejudge") + 1 \
            and not sys.argv[sys.argv.index("--rejudge") + 1].startswith("--") \
            else ROOT / "eval" / "results" / "app-baseline.json"
        old = json.loads(Path(src).read_text(encoding="utf-8"))
        rows = []
        for r in old:
            resp = {"answer": r["answer"],
                    "metadata": {"retriever_resources": [
                        {"document_name": x["name"], "content": x["content"]}
                        for x in r.get("resources", [])]}}
            j = judge(gold[r["id"]], resp)
            j["seconds"] = r.get("seconds")
            rows.append(j)
    else:
        info = json.loads((ROOT / "eval" /
                           (sys.argv[sys.argv.index("--app") + 1]
                            if "--app" in sys.argv else "app.json")).read_text(encoding="utf-8"))
        gl = list(gold.values())
        if only:
            gl = [g for g in gl if g["分层"].startswith(only)]
        rows = []
        for g in gl:
            t0 = time.perf_counter()
            j = judge(g, call_app(info, g["输入"], f"baseline-{g['id']}"))
            j["seconds"] = round(time.perf_counter() - t0, 1)
            rows.append(j)

    for j in rows:
        if j["error"]:
            print(f"[{j['id']:6s}] ERROR {j['error'][:160]}")
            continue
        if quiet:
            continue
        flags = []
        if j["hard_fab"]:
            flags.append("★真编造:" + "/".join(j["hard_fab"][:2]))
        soft = [k for k, v in j["alien_kind"].items() if v in ("alias", "carried")]
        if soft:
            flags.append("口径问题:" + "/".join(soft[:2]))
        if j["manifest_status"] == "missing":
            flags.append("清单缺失")
        if j.get("boundary_bad"):
            flags.append("边界字段缺:" + "/".join(j["boundary_bad"][:2]))
        if j.get("boundary_conflict"):
            flags.append("★自相矛盾:" + j["boundary_conflict"][:40])
        if j.get("stale_violation"):
            flags.append("★时效违规:" + j["stale_violation"][0][:32])
        if j.get("stale_self_wrong"):
            flags.append("时效自报错:" + j["stale_self_wrong"][0][:24])
        if j["refused"]:
            flags.append("拒答")
        if j["stale"]:
            flags.append("时效提示")
        if j["tok_hit"] is False:
            flags.append("判据词未命中")
        sec = f"{j['seconds']:5.1f}s" if j.get("seconds") else "     "
        print(f"[{j['id']:6s}] {j['layer']:6s} {sec} "
              f"清单={len(j['manifest'])}条 来源={j['n_res']}  {' '.join(flags)}")

    print("\n" + "=" * 96)
    by = defaultdict(list)
    for r in rows:
        if not r["error"]:
            by[r["layer"]].append(r)

    def ratio(rs, pred):
        rs = [r for r in rs if pred(r) is not None]
        return (sum(1 for r in rs if pred(r)) / len(rs)) if rs else float("nan")

    def tok_cell(rs) -> str:
        """判据词命中率只对 L1/L2 有意义。

        在 L3/L4 上这个指标会系统性地骗人：
          · L4 的正确回答**本来就该是拒答**，不去命中任何判据词；
          · L3 问的是「还有效吗」这类问题，正确回答的措辞和出题时写的答案键
            几乎不可能逐字对上（实测两条完全正确的回答因为「没有…全文」vs
            预设的『库中无法典全文』而判未命中）。
        把一个不适用的指标填成 0%/50% 比不填更糟 —— 会被当成缺陷去"修"。
        """
        if not rs:
            return "     —"
        if rs[0]["layer"] not in ("L1基础", "L2推理"):
            return "     —"
        return f"{ratio(rs, lambda r: r['tok_hit']):9.0%}"

    def refu_cell(rs) -> str:
        """拒答率只对 L4 有意义。

        L1–L3 的正确答案本来就不是拒答——L3 问的是「这条法规还有效吗」，
        正确回答是给出时效结论，那自然不属于拒答。把它们的拒答率填成 17%
        只会被误读成一个缺陷（这个坑在「判据词命中率」上已经踩过一次）。
        """
        if not rs or rs[0]["layer"] != "L4对抗":
            return "     —"
        return f"{ratio(rs, lambda r: r['refused']):6.0%}"

    def bound_cell(rs) -> str:
        """边界判定字段合规率：四行都按枚举填出来（v2 及更早的回答自然是 0%）。"""
        if not rs:
            return "     —"
        return f"{ratio(rs, lambda r: not r['boundary_bad']):7.0%}"

    print(f"{'层':8s} {'n':>3s} {'判据词命中':>9s} {'引用可追溯':>10s} "
          f"{'真编造':>8s} {'口径问题':>9s} {'清单合规':>9s} {'边界字段':>8s} "
          f"{'拒答':>6s} {'时效提示':>8s} {'引文存疑':>8s}")
    for layer in ("L1基础", "L2推理", "L3边界", "L4对抗"):
        rs = by.get(layer, [])
        if not rs:
            continue
        print(f"{layer:8s} {len(rs):3d} {tok_cell(rs):>9s} "
              f"{ratio(rs, lambda r: not r['alien']):10.0%} "
              f"{ratio(rs, lambda r: bool(r['hard_fab'])):8.0%} "
              f"{ratio(rs, lambda r: any(v in ('alias', 'carried') for v in r['alien_kind'].values())):9.0%} "
              f"{ratio(rs, lambda r: r['manifest_status'] != 'missing'):9.0%} "
              f"{bound_cell(rs)} "
              f"{refu_cell(rs):>6s} "
              f"{ratio(rs, lambda r: r['stale']):8.0%} "
              f"{ratio(rs, lambda r: bool(r['quote_unbacked'])):8.0%}")

    allr = [r for r in rows if not r["error"]]
    factual = [r for r in allr if r["layer"] in ("L1基础", "L2推理")]
    print(f"{'合计':8s} {len(allr):3d} {tok_cell(factual):>9s} "
          f"{ratio(allr, lambda r: not r['alien']):10.0%} "
          f"{ratio(allr, lambda r: bool(r['hard_fab'])):8.0%} "
          f"{ratio(allr, lambda r: any(v in ('alias', 'carried') for v in r['alien_kind'].values())):9.0%} "
          f"{ratio(allr, lambda r: r['manifest_status'] != 'missing'):9.0%} "
          f"{bound_cell(allr)} "
          f"{'':>6s} {'':>8s} "
          f"{ratio(allr, lambda r: bool(r['quote_unbacked'])):8.0%}")
    print("  ↑ 「判据词命中」只在 L1+L2 上统计（L3/L4 不适用，理由见脚本注释）")
    print("  ↑ 「边界字段」= 模型是否按 v3 提示词的枚举填出【边界判定】四行")
    print("  ↑ 「引文存疑」是报告性指标（引述文字在召回段落里找不到），不参与判罚，仅供人工复核")

    l4 = by.get("L4对抗", [])
    if l4:
        ok = sum(1 for r in l4 if r["refused"])
        bpass = sum(1 for r in l4 if r["l4_pass"])
        scored = [r for r in l4 if r.get("expect_ok") is not None]
        tail = "" if len(scored) == len(l4) else \
            f"；其中 {len(scored)}/{len(l4)} 条可核对答案键（其余没有边界字段，该维度不参与）"
        print(f"\n指标 · L4 边界处理正确率：{bpass}/{len(l4)}"
              f"（识别边界 且 无自相矛盾 且 无时效违规 且 命中答案键{tail}）")
        print(f"      其中字面拒答（结论边界=拒答）：{ok}/{len(l4)}")
        bad = [r for r in l4 if not r["l4_pass"]]
        if bad:
            print(f"      未通过：{', '.join(r['id'] for r in bad)}")

    conflict = [r for r in allr if r.get("boundary_conflict")]
    if conflict:
        print(f"\n★边界判定自相矛盾（{len(conflict)} 条 · 前三行事实与第四行结论不兼容）：")
        for r in conflict:
            print(f"  {r['id']}  {r['boundary_conflict']}")

    sv = [r for r in allr if r.get("stale_violation")]
    if sv:
        print(f"\n★时效硬规则违规（{len(sv)} 条 · 已失效法规仍给出可引用依据）：")
        for r in sv:
            print(f"  {r['id']}  {r['stale_violation']}")

    sw = [r for r in allr if r.get("stale_self_wrong")]
    if sw:
        print(f"\n时效自报与台账不符（{len(sw)} 条 · 可能是模型错，也可能是题目未指向库内法规）：")
        for r in sw:
            print(f"  {r['id']}  {r['stale_self_wrong']}")

    fab = [r for r in allr if r["hard_fab"]]
    if fab:
        print(f"\n真编造/主体归错明细（{len(fab)} 条 · 清单反查不到 + 正文无据条号 + 跨法规挪用）：")
        for r in fab:
            for k in r["hard_fab"]:
                if k.startswith("跨法规挪用:"):
                    kk = "cross_doc"
                elif k.startswith("正文无据条号:"):
                    kk = "prose"
                else:
                    kk = r["alien_kind"].get(k, "prose")
                print(f"  {r['id']} [{kk}] {k}   来源={r['src_docs']}")

    qu = [r for r in allr if r.get("quote_unbacked")]
    if qu:
        print(f"\n引文存疑（{len(qu)} 条 · 报告性指标，不计入判罚，供人工复核）：")
        for r in qu:
            print(f"  {r['id']}  {r['quote_unbacked'][:2]}")

    soft = [(r, k, v) for r in allr for k, v in r["alien_kind"].items() if v in ("alias", "carried")]
    if soft:
        print(f"\n口径问题（{len(soft)} 项 · 非编造，但产品要处理）：")
        for r, k, v in soft:
            print(f"  {r['id']} [{v}] {k}   来源={r['src_docs']}")

    miss = [r for r in allr if r["manifest_status"] == "missing"]
    if miss:
        print(f"\n未按格式输出引用清单：{len(miss)} 条 → {[r['id'] for r in miss]}")

    if "--json" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--json") + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细已写入 {p}")


if __name__ == "__main__":
    main()
