#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核验：把「撰写」这一步的产物再过一遍机器。

## 跟上一版的区别

上一版只核「有正文」的文件，对只有目录的条目一律判失败——这会让
《南京历史文化名城保护条例》这种**确实存在但没抓到正文**的文件被标成
「引用不通过」，等于把数据缺口说成模型的错。

现在分三类，界面用不同颜色：

    verified      有正文，条号/引文/名称都对得上 → 可以照抄
    unverifiable  只有目录条目，无正文 → 能确认「这份文件存在」，不能确认内容
    failed        有正文，但条号不存在 / 引文查不到 / 引错文件 → 必须改

结论强度按最差的一类定，**但 unverifiable 不参与降级**——它是数据的边界，
不是内容的错误。这一点必须和 failed 分开，否则用户会把「没抓到正文」
误读成「这份报告有问题」。
"""
from __future__ import annotations

import re

import core


def _evidence_part(answer: str) -> str:
    """抠出「依据」段。抠不到就退回全文。"""
    m = re.search(r"##\s*依据\s*\n(.*?)(?=\n##\s|\Z)", answer, re.S)
    return m.group(1) if m else answer


def build_hits(picked: list[dict], bodies: list[dict]) -> list[dict]:
    """把勾选的文件摊成核验器能吃的块列表。"""
    by_key = {b["doc_key"]: b for b in bodies if b.get("has_body")}
    # 联网抓来的正文不在 kb.db 里，没有 body_key，只有 url。
    # 不按 url 兜这一层，两万字正文会被当成「库里没有正文」，
    # 核验时整份判 unverifiable——引用明明能对上，却告诉用户无法核对。
    by_url = {b.get("url"): b for b in bodies
              if b.get("has_body") and b.get("url")}
    hits = []
    for it in picked:
        b = by_key.get(it.get("body_key") or "") or by_url.get(it.get("url") or "")
        if b and b.get("blocks"):
            for blk in b["blocks"]:
                hits.append({"doc_key": b["doc_key"], "title": it["title"],
                             "label": blk.get("label", ""), "text": blk["text"],
                             "doc_no": it.get("doc_no", ""),
                             "published": it.get("published", ""),
                             "url": it.get("url", "")})
        else:
            # 没有正文也要进 hits，否则 parse_cites 匹配不到，会被误判成编造
            hits.append({"doc_key": "", "title": it["title"], "label": "",
                         "text": "", "doc_no": it.get("doc_no", ""),
                         "published": it.get("published", ""),
                         "url": it.get("url", "")})
    return hits


def _has_body(doc_key: str) -> bool:
    return bool(doc_key)


SHORT_BODY = 800


def _body_chars(bodies: list[dict]) -> dict[str, int]:
    """doc_key → 正文实际字数。

    为什么需要：南京的规划类文件（《南京市城市更新专项规划》《总统府历史文化
    街区保护规划》）源站只发了一两百字的「批后公布」公告，规划本体是图件。
    这类文件「有正文」，但正文是个壳。

    拿壳去核验内容引用，必然判「引文查不到」——而模型并没有引错，是我们给了
    它一份没有内容的文件。把它记成 failed，等于把数据边界算成内容错误，
    用户会以为模型在编。这是本项目第三次踩同一个坑（前两次是脏标题、
    逐句回查范围）。"""
    out: dict[str, int] = {}
    for b in bodies:
        if not b.get("has_body"):
            continue
        # 优先用全文总字数（fulltext.chars），拿不到才退回装配片段的字数。
        # 用片段字数判过一次，结果是《南京历史文化名城保护条例》1 万字、
        # 装配只给了 3 段 600 字，被判成「仅有公告」，整篇引注全标 unverifiable。
        c = b.get("chars")
        if c is None:
            c = sum(len(x.get("text") or "") for x in b.get("blocks") or [])
        out[b["doc_key"]] = c
    return out


def verify(question: str, answer: str, picked: list[dict], bodies: list[dict]) -> dict:
    hits = build_hits(picked, bodies)
    q = core.Query(question)
    v = core.Verifier()
    short = _body_chars(bodies)
    try:
        cites = core.parse_cites(answer, hits)
        docnos = core.check_docnos(core.parse_docnos(answer), core.docno_index())
        # 逐句回查只对「依据」段有意义。结论和下一步是判断与动作，
        # 本来就该用自己的话写；拿它们算「有出处比例」，会稳定得到 0.3，
        # 然后天天报警，最后没人看这个指标。
        trace = core.trace_answer(_evidence_part(answer),
                                  [h for h in hits if h["text"]])
        per = []
        for c in cites:
            dk = c.get("doc_key") or ""
            checks = []
            if not dk:
                state = "unverifiable"
                checks.append({"name": "核验范围", "state": "skip",
                               "detail": "该文件只有目录条目，未抓到正文；"
                                         "能确认文件存在，不能确认条文内容"})
            elif short.get(dk, 10 ** 6) < SHORT_BODY:
                state = "unverifiable"
                checks.append({"name": "核验范围", "state": "skip",
                               "detail": f"该文件正文只有 {short[dk]} 字，是源站的公布公告；"
                                         f"文件本体是图件或附件，未入库。"
                                         f"能确认它已公布，不能核对其内容"})
            else:
                c1 = v.c1(dk, c["article"])
                c2 = v.c2(dk, c.get("quote") or "")
                c3 = v.c3(dk)
                c4 = v.c4(c["claimed"], dk)
                c5 = v.c5(q, dk)
                checks = [c1, c2, c3, c4, c5]
                bad = [x for x in checks if x["state"] == "fail"]
                state = "failed" if bad else "verified"
            per.append({**c, "checks": checks, "kind": state})
        # 同一份文件常被引用多次。逐条列出会让核验区变成一堵墙，
        # 而且「南京历史文化名城保护条例 ×3」比「三行同名」信息量更大：
        # 它说明这份文件是整篇报告的主依据。
        agg: dict[tuple, dict] = {}
        for c in per:
            k = (c["claimed"], c["article"])
            if k in agg:
                agg[k]["count"] += 1
                # 同一份文件同一处引用出现多次时，聚合保留的是第一次的 checks。
                # 如果后面某一次判失败，必须把 checks 换成失败那份——否则界面上
                # 会出现「标红的条目，点开五行全绿」，比不显示还糟。
                if c["kind"] == "failed":
                    agg[k]["kind"] = "failed"
                    agg[k]["checks"] = c["checks"]
            else:
                agg[k] = {**c, "count": 1}
        per = list(agg.values())
        per.sort(key=lambda c: (c["kind"] != "failed",
                                c["kind"] != "unverifiable", -c["count"]))
        # 强度按「错了几条」定，不按「错了多少次」。同一句话被引三次、三次都错，
        # 要改的是一句话，不是三处——用出现次数当分母会把「改一句」说成「整篇不能用」。
        n_kind = len(per)
        n_fail = sum(1 for c in per if c["kind"] == "failed")
        n_fail_occ = sum(c["count"] for c in per if c["kind"] == "failed")
        n_unv = sum(1 for c in per if c["kind"] == "unverifiable")
        n_unv_occ = sum(c["count"] for c in per if c["kind"] == "unverifiable")
        n_all = sum(c["count"] for c in per)
        bad_docno = [d for d in docnos if not d["matched"]]

        rate = n_fail / max(1, n_kind)
        if n_fail and (n_fail >= 3 and rate > 0.15):
            strength = "需修正"
            why = (f"{n_kind} 条不同引注里有 {n_fail} 条对不上原文"
                   f"（共出现 {n_fail_occ} 处），见下方标红的条目")
        elif n_fail:
            strength = "基本可用"
            why = (f"{n_kind} 条不同引注里 {n_fail} 条与原文不完全一致"
                   f"（共 {n_fail_occ} 处），其余已核对；改掉这几条即可")
        elif not per and bad_docno:
            strength = "需修正"
            why = f"{len(bad_docno)} 个文号在库里查不到"
        elif not per and not docnos:
            strength = "无引注"
            why = "报告里没有任何可核对的出处，无法判断依据从哪来"
        elif n_unv:
            strength = "部分可核"
            why = (f"回原文核对 {n_all - n_unv_occ} 处，{n_unv_occ} 处只有目录条目——"
                   f"这些文件源站发过，但正文没入库，只能确认存在、不能确认内容")
        else:
            strength = "可交付"
            why = f"{n_all} 处引注全部回到原文核对通过"
        return {"cites": per, "docnos": docnos, "trace": trace,
                "strength": strength, "reason": why,
                "n_fail": n_fail, "n_fail_occ": n_fail_occ,
                "n_unv": n_unv, "n_unv_occ": n_unv_occ, "n_all": n_all,
                "ratio": trace.get("ratio", 0)}
    finally:
        v.close()


def gaps(answer: str) -> list[str]:
    """从「缺口」段里抠出条目，用于埋点与界面提示。"""
    m = re.search(r"##\s*缺口\s*\n(.*?)(?=\n##\s|\Z)", answer, re.S)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        s = re.sub(r"^[-*\d.、\s]+", "", ln).strip()
        if len(s) >= 8:
            out.append(s[:200])
    return out[:8]
