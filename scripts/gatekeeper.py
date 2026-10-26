#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闸门 2 · 条文级确定性校验层。

**这个文件是产品的一部分，不是评测脚本。** 它做的是：
拿到「问题 + 候选回答 + 本次召回的段落」，在**不调用任何模型**的前提下，
判定这份回答能不能交付给用户，以及应该以哪种强度交付。

为什么必须有它（这是本项目最重要的一条工程结论）：

    prompts/system-v3.md 里的四条硬约束是**模型自己遵守**的。
    只要换模型、调温度、绕过应用直调 API，约束就没了 —— 没有告警，没有报错。
    评测脚本能事后发现，但发现不了"当下正在发生的错误交付"。

    而闸门 2 要判的三件事，没有一件需要模型：
      · 这个条号超过该法规的条数了吗      → 查一张条数表
      · 这条引用真的在本次召回里吗        → 查召回段落
      · 这部法规还有效吗                  → 查一张时效台账

    所以它可以是确定性的、可写进 SLA 的、可解释到"第几条第几项不通过"的。

闸门 1（rerank 分数）是兜底，本文件不负责 —— 它在上游，由检索配置的
`score_threshold` 承担，实测只覆盖 19/40 对抗题（见 research/07）。

════════════════════════════════════════════════════════════════════════
判定口径（与 prompts/system-v3.md 第 3 节的枚举逐字对齐）
════════════════════════════════════════════════════════════════════════

最终结论边界由**规则**决定，不用模型自报值 —— 自报值只作为一路输入去核对：

    有 block  →  拒答
    有 warn   →  部分作答
    全过      →  可作答

五项检查：

  C1 条号越界      引用的条号 > 该法规实际条数                          block
  C2 引用存在性    引用的 (法规, 条号) 能否在本次召回段落里反查到        block
  C3 时效比对      已失效法规是否被当成了可引用的现行依据                block/warn
  C4 边界一致性    模型自报的「结论边界」与前三行事实是否兼容            block/warn
  C5 所问法规划界  问题点名的库外法规，是否被答成了库内另一部法规        block

C3 之所以分两档：
  · 用户要拿条号去办事（操作性意图）+ 法规已失效 + 回答给了条号 → **block**
    （L4-26 就是这一类：明知已废止，仍说"处罚决定书应引用第七十七条"）
  · 回答只是照实说明"该法已废止、以下仅作历史参考"            → **warn**
    （信息有用且诚实，只是不能作为现行依据 → 判为「部分作答」）

C1–C4 核对的是「模型自报的字段彼此是否自洽」，C5 核对的是「自报字段是否属实」——
后者是换用不同模型之后才暴露出来的一类错误，两层的必要性不同。

用法（命令行）：
    python scripts/gatekeeper.py --app app-v6.json --ask "怀化市扬尘污染防治管理办法是哪个部门负责？"
    python scripts/gatekeeper.py --answer answer.txt --resources resp.json
    python scripts/gatekeeper.py --selftest

用法（作为库）：
    from gatekeeper import verify
    v = verify(question, answer, resources)
    if v.boundary == "拒答":
        refuse_to_user(v.reasons)
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import legalkit as lk  # noqa: E402

BLOCK = "block"
WARN = "warn"
INFO = "info"

BOUNDARY_ORDER = {"可作答": 0, "部分作答": 1, "拒答": 2}

# 否定语境：条号出现在这些词附近，是在说「这条不存在/不是这条」，不是引用
NEG_MARKERS = ["没有", "无", "不存在", "不包含", "未检索到", "不属于", "不是",
               "无法", "未能", "并非", "缺", "未规定", "不涉及"]


# ────────────────────────────── 数据结构 ──────────────────────────────

@dataclass
class Cite:
    """一条引用的判定结果。"""
    raw_law: str                 # 回答里写的法规名
    law: str | None              # 对到库内正式全称（None = 不在库内）
    art: str                     # 回答里写的条号
    art_int: int | None
    status: str                  # ok | out_of_range | not_in_recall | not_in_library
    note: str
    source: str                  # 清单 | 正文

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Check:
    code: str
    name: str
    level: str                   # block | warn | info
    passed: bool
    detail: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    boundary: str                # 可作答 | 部分作答 | 拒答  ← 闸门 2 的最终输出
    decided_by: str              # rule | model
    reasons: list[str]
    checks: list[Check] = field(default_factory=list)
    cites: list[Cite] = field(default_factory=list)
    model_boundary: str | None = None
    stale_laws: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blocked"] = any(c.level == BLOCK and not c.passed for c in self.checks)
        return d

    def render(self) -> str:
        """给人和日志看的多行文本。"""
        icon = {"可作答": "✓", "部分作答": "!", "拒答": "✕"}[self.boundary]
        out = [f"{icon} 结论边界：{self.boundary}（由 {self.decided_by} 判定）"]
        if self.model_boundary:
            same = "一致" if self.model_boundary == self.boundary else "不一致"
            out.append(f"  模型自报：{self.model_boundary}（{same}）")
        if self.stale_laws:
            out.append(f"  命中非现行有效的文件：{'、'.join(self.stale_laws)}")
        out.append("")
        out.append("校验明细：")
        for c in self.checks:
            mark = "通过" if c.passed else ("拦截" if c.level == BLOCK else "提示")
            out.append(f"  [{mark}] {c.code} {c.name}")
            for d in c.detail:
                out.append(f"        · {d}")
        if self.cites:
            out.append("")
            out.append("引用逐条核对：")
            for c in self.cites:
                mark = "ok" if c.ok else c.status
                tgt = c.law or f"{c.raw_law}（库内无此法规）"
                out.append(f"  [{mark}] {tgt} {c.art}　{c.note}")
        out.append("")
        out.append("判定理由：")
        for r in self.reasons:
            out.append(f"  · {r}")
        return "\n".join(out)


# ────────────────────────────── 校验 ──────────────────────────────

def _norm_resources(resources) -> list[tuple[str, str]]:
    """统一召回段落的形状。接受 [{name, content}] 或 Dify 的 retriever_resources。"""
    out = []
    for x in resources or []:
        if isinstance(x, dict):
            name = x.get("document_name") or x.get("name") or ""
            out.append((lk.norm_doc(name), x.get("content") or ""))
        else:
            out.append((lk.norm_doc(str(x[0])), str(x[1])))
    return out


def _check_cites(cites: list[tuple[str, str]], source: str,
                 chunks: list[tuple[str, str]]) -> list[Cite]:
    """逐条核对引用的 (法规, 条号)。"""
    out: list[Cite] = []
    for raw_law, art in cites:
        law = lk.match_law(raw_law)
        if law is None:
            # 法规不在库内。分两种：只在被承载文件里被提及，还是纯编造。
            carried = any(raw_law.replace(" ", "") in lk.squeeze(c) for _, c in chunks)
            out.append(Cite(raw_law, None, art, lk.cn2int(art),
                            "not_in_library",
                            "库内没有这部法规的全文" + ("（它只在其他文件的制定依据里被提及）"
                                                        if carried else ""),
                            source))
            continue
        n = lk.article_count(law)
        a = lk.cn2int(re.sub(r"[^\d一二三四五六七八九十百千零〇]", "", art))
        art_clean = art if art.startswith("第") else f"第{art}条"
        if n is not None and a is not None and a > n:
            out.append(Cite(raw_law, law, art_clean, a, "out_of_range",
                            f"越界：该法规共 {n} 条，不存在 {art_clean}", source))
            continue
        if n == 0:
            out.append(Cite(raw_law, law, art_clean, a, "out_of_range",
                            "该文件没有条文结构（是决定/表格形式），引不出条号", source))
            continue
        if not any(lk.law_in_docname(law, dn) and art_clean in c for dn, c in chunks):
            out.append(Cite(raw_law, law, art_clean, a, "not_in_recall",
                            "本次召回的该法规段落里找不到这个条号", source))
            continue
        out.append(Cite(raw_law, law, art_clean, a, "ok", "在本次召回中可反查", source))
    return out


def _ledger_corroborated(self_state: str | None, touched: list[str]) -> bool:
    """回答的落脚点是不是**时效台账**这个产品权威数据源。

    判据完全是客观的，没有一处需要解释：
      · 问题（或回答）指向的库内法规都在台账里；
      · 模型自报的「时效状态」与台账的取值**逐字相同**。

    为什么需要这一条：合成规则里有一句「一条可交付依据都没有 → 拒答」。
    这句话对"模型什么都没给出来"是对的，但会把另一类**完全正确的回答**误杀 ——
    最典型的是「《噪声污染防治法》现在还有效吗？」：这个问题的依据本来就来自
    时效台账，法规正文里根本没有"还有效吗"这句话，所以它**永远不会有条号可引用**。
    「没有条号」被读成了「没有依据」，正确答案（已废止）就被降成了拒答。

    实测证据（v7 的 66 题离线重算）：L3-01 / L3-02 / L4-12 三道都是这种情形 ——
    模型自报「部分作答」、时效自报与台账逐字一致、无任何硬性问题，
    却被规则层判成「拒答」。这与 PRD 第 4.2 节给闸门 1 定的取向是同一条：
    **宁可漏挡，不可误杀** —— 误杀会让用户以为库里没有，直接放弃使用。

    仍然不放弃否决权：这一条只在**没有 block**、且模型自报比"拒答"更宽松时生效，
    并且要求台账印证。自报「拒答」的回答永远不会被它升格。
    """
    if not self_state or not touched:
        return False
    truth = {lk.stale_info(t).get("时效性") for t in touched if lk.stale_info(t)}
    truth.discard(None)
    return len(truth) == 1 and self_state == truth.pop()


def verify(question: str, answer: str, resources, *,
           model_boundary: dict | None = None) -> Verdict:
    """闸门 2 主入口。**不调用任何模型。**

    参数：
        question        用户的问题
        answer          模型的候选回答
        resources       本次召回的段落 [{name, content}]
        model_boundary  可选：模型自报的【边界判定】四行（不传则自己解析）

    返回 Verdict，其中 `boundary` 是可以直接交付给用户的结论强度。
    """
    chunks = _norm_resources(resources)
    chunks_text = "\n".join(lk.squeeze(c) for _, c in chunks)
    checks: list[Check] = []
    reasons: list[str] = []

    # ── 解析回答里的引用：清单优先，清单缺失时用正文兜底 ────────────────
    manifest, mstat = lk.parse_citation(answer)
    if manifest:
        cites_raw, cite_source = manifest, "清单"
    elif mstat == "missing":
        # 模型没按格式输出清单 —— 无从知道它依赖了哪些条目，
        # 用正文兜底从严核对（它连契约都没遵守，不值得给宽松口径）。
        cites_raw, cite_source = lk.extract_cites(answer), "正文"
    else:
        # 清单**明确写「无」** = 模型声明本次没有任何交付依据。
        # 正文里出现的条号按**说明性引用**处理，不登记为交付依据。
        #
        # 为什么这一条是必须的：把说明性引用当引用去核对会制造假阳性 ——
        # 「该条例共 66 条，不存在第八十条」「该决定暂调《海商法》第四条」
        # 这两类句子里的条号都会被当成"越界条号 / 库外法规"而拦下来。
        # 实测（app-v7-boundary 离线重算）L4-12、L3-02 两道**完全正确**的
        # 回答就是这样被判成「拒答」的。
        #
        # 编造仍然照抓：C2 末尾的正文扫描会检查"这个条号在本次召回里根本不存在"，
        # 那一条不受这里影响；C3 的时效硬规则走 OPERATIONAL_CITE_RE，也不受影响。
        cites_raw, cite_source = [], "清单(无)"
    cites = _check_cites(cites_raw, cite_source, chunks)

    # **清单的语义**：它是模型对"我实际依赖了哪些条目"的声明
    # （提示词里写的是"只登记正文真正引用过的条文"）。
    # 当它明确写「无」时，产品应当尊重这个声明。
    #
    # 这一条的来源：真实调用时 v3 提示词下的 L4-26 声明清单无、正文给出第四十三条
    # 作为历史说明，第一版实现把它算成"依赖了失效法规的条文"而判成「部分作答」，
    # 与模型自己的结论（拒答）矛盾。
    cites_for_decision = [] if mstat == "none" else cites

    # ── 模型自报的边界字段 ────────────────────────────────────────────
    b, b_bad = (model_boundary, []) if model_boundary else lk.parse_boundary(answer)

    # ── C1 条号越界 ──────────────────────────────────────────────────
    oor = [c for c in cites if c.status == "out_of_range"]
    checks.append(Check("C1", "条号越界", BLOCK, not oor,
                        [f"{c.raw_law} {c.art} —— {c.note}" for c in oor]))

    # ── C2 引用存在性 ────────────────────────────────────────────────
    # 包含两部分：清单/正文里登记的引用反查不到，以及正文里散写但无据的条号。
    # 后者的意义：模型可能把编造的条号写在正文里但不登记进清单 —— 清单只管得住清单。
    c2_detail: list[str] = []
    reported: set[str] = set()
    for c in cites:
        if c.status in ("not_in_recall", "not_in_library"):
            c2_detail.append(f"{c.raw_law} {c.art} —— {c.note}")
            reported.add(c.art)
    q_flat = lk.squeeze(question)
    a_flat = lk.squeeze(answer)
    for m in lk.ART_RE.finditer(a_flat):
        a = m.group(0)
        if a in reported or a in chunks_text or a in q_flat:
            continue
        if any(x in a_flat[max(0, m.start() - 12): m.end() + 12] for x in NEG_MARKERS):
            continue
        c2_detail.append(f"正文无据条号 {a} —— 本次召回的任何段落里都没有它")
        reported.add(a)
    checks.append(Check("C2", "引用存在性", BLOCK, not c2_detail, c2_detail))

    # ── C3 时效比对 ──────────────────────────────────────────────────
    # 用 include_full_names：产品层面对的是真人提问，用户常常不打书名号
    # （「……授权决定，现在还有效吗？」）。只认书名号会让这类问题全部漏掉时效比对。
    q_laws = lk.query_laws(question, include_full_names=True)
    cited_laws = [c.law for c in cites if c.law]
    touched = list(dict.fromkeys(q_laws + cited_laws))
    stale_laws = [l for l in touched if lk.is_stale(l)]

    # 【历史沿革】一节允许出现失效法规的条号，检查时要排除它
    body = answer or ""
    h = lk.HISTORY_BLOCK_RE.search(body)
    body_wo_hist = body.replace(h.group(0), "") if h else body
    op_hits = [m.group(0) for m in lk.OPERATIONAL_CITE_RE.finditer(lk.squeeze(body_wo_hist))]

    c3_warn: list[str] = []
    if stale_laws:
        # 关键区分：**回答是否实际依据了失效法规的条文**。
        #   · 只是在说明"这部法已废止"这个事实、清单为空 → 不降级（它已经拒答了）
        #   · 真的引用了失效法规的条文作为依据（历史参考） → 降到「部分作答」
        # 这一条是被真实调用纠正出来的：v3 提示词下的 L4-26 明确拒答且清单为空，
        # 第一版实现却因为它"提到了已废止的法规"而把结论降成「部分作答」——
        # 和当初旧判据把 L4-26 判成"已拒答"是同一类错误的镜像（方向相反）。
        relied = [c for c in cites_for_decision if c.law in stale_laws]
        block_detail = None
        for name in stale_laws:
            info = lk.stale_info(name) or {}
            state = (info.get("时效性") or "")[1:]        # 已废止 → 废止
            if op_hits:
                block_detail = [
                    f"{name} 已{state}（{info.get('废止日期') or '—'}），"
                    f"回答仍给出了可引用的条号指引：{op_hits[0]}"]
                break
            if relied:
                c3_warn.append(
                    f"{name} 已{state}（{info.get('废止日期') or '—'}），"
                    f"回答引用了它的条文（{relied[0].art}）作为依据，只能算历史参考")
        if block_detail:
            checks.append(Check("C3", "时效比对", BLOCK, False, block_detail))
        elif c3_warn:
            checks.append(Check("C3", "时效比对", WARN, False, c3_warn))
        else:
            names = "、".join(stale_laws)
            extra = ""
            if mstat == "none" and any(c.law in stale_laws for c in cites):
                extra = ("；正文里出现了它的条号，但未登记进引用清单，按说明性引用处理")
            checks.append(Check("C3", "时效比对", INFO, True,
                                [f"{names} 已失效，回答没有依据其条文提供可引用依据，"
                                 f"仅说明了效力状态{extra}"]))
    else:
        checks.append(Check("C3", "时效比对", INFO, True,
                            ["本次回答未依赖非现行有效的文件"] if touched
                            else ["问题未指向库内法规，本轮不做时效比对"]))

    # ── C4 边界判定字段一致性 ────────────────────────────────────────
    if b_bad or not b:
        # 缺字段只说明"没法与登记事实交叉核对"，不等于回答有问题 ——
        # 前三项规则本身就是完整的判据，所以这一档是提示，不是降级。
        checks.append(Check("C4", "边界判定一致性", INFO, True,
                            ["回答里没有可解析的【边界判定】四行 —— 无法与登记事实交叉核对，"
                             "本次结论完全由前三项规则判定"]))
    else:
        # 判断本身在 legalkit.boundary_consistency —— 评测判分器走的是同一段代码，
        # 两处各写一遍就会出现"评测放过、上线拦下"这种最难查的分歧。
        consistent, why = lk.boundary_consistency(b)
        if not consistent:
            checks.append(Check("C4", "边界判定一致性", BLOCK, False,
                                [f"自报的结论边界「{b.get('结论边界')}」与它自己登记的事实不兼容：{why}"]))
        else:
            checks.append(Check("C4", "边界判定一致性", INFO, True,
                                [f"自报边界「{b.get('结论边界')}」与前三行事实一致"]))

    # ── C5 所问法规划界 ──────────────────────────────────────────────
    # 判据：问题里用书名号点名的法规**不在库内**，而模型自报「引用法规在库内：有全文」。
    #
    # 为什么需要它：C1–C4 检查的都是「模型自报的四行字段彼此是否自洽」，
    # 查不出**字段本身填错了**。真实调用里出现过一次 —— 用户问库外的
    # 《城市市容和环境卫生管理条例》，检索却召回了库内的
    # 《呼伦贝尔市城市市容和环境卫生管理条例》（两部都涉及户外广告、牌匾标识），
    # 模型于是自报「有全文 / 可作答」，四项检查全部通过，交付给用户的是一个
    # 张冠李戴的「可作答」——用户会以为拿到的是自己问的那部法规的条文。
    #
    # 这一条把「用户点名的是哪部法规」这个**问题本身就写着的事实**，
    # 与「模型自报哪部法规在库内」对齐。两个都是客观的，不需要语义猜测，
    # 与最初用【引用清单】替掉正文正则、用【边界判定】替掉拒答 marker 是同一个解法。
    #
    # 触发面（在 v7 的 66 题上离线核验）：**0 题**。完整版的模型从没犯过这个错，
    # 所以加它对既有指标没有任何影响。它的价值恰恰在换模型之后才显现出来：
    # 直连 MVP 换成 qwen-plus 时就踩到了（见 mvp/README.md 的实测记录）。
    # ——这正是不该把硬约束只写在提示词里的理由：约束的可靠性不该跟模型绑死。
    named_out = [m.group(1) for m in lk.TITLE_MARK_RE.finditer(question or "")]
    outside = [x for x in named_out if lk.match_law(x) is None]
    self_inlib = (b or {}).get("引用法规在库内")
    if outside and self_inlib == "有全文":
        checks.append(Check("C5", "所问法规划界", BLOCK, False, [
            f"问题点名的是《{outside[0]}》，库内没有这部法规的全文；"
            f"但自报「引用法规在库内：有全文」—— 这两件事对不上",
            "常见成因：检索召回了库内另一部主题相近的法规，模型据此作答。"
            "用户会以为拿到的是自己问的那部法规的条文。",
        ]))
    elif outside:
        checks.append(Check("C5", "所问法规划界", INFO, True,
                            [f"问题点名的《{outside[0]}》不在库内，"
                             f"模型自报「{self_inlib or '未填'}」，与事实一致"]))
    else:
        checks.append(Check("C5", "所问法规划界", INFO, True,
                            ["问题没有点名库外法规，本轮不做划界核对"]))

    # ── 合成最终结论 ────────────────────────────────────────────────
    # 规则优先于模型自报：模型说「可作答」，但条号越界了，那就是拒答。
    #
    # 这一段的顺序是刻意的，前三步都不看模型自报值：
    #   ① 有拦截项            → 拒答（给了不该给的东西）
    #   ② 一条可交付依据都没有 → 拒答（什么都给不出来，就只能说给不出来）
    #   ③ 只依赖了失效法规的条文 → 部分作答（有用，但只能算历史参考）
    # 之后才轮到模型自报值，且只用来**往更保守的方向**调整。
    blocked = [c for c in checks if c.level == BLOCK and not c.passed]
    warned = [c for c in checks if c.level == WARN and not c.passed]
    usable = [c for c in cites_for_decision if c.ok and not lk.is_stale(c.law)]
    stale_relied = [c for c in cites_for_decision if c.law in stale_laws]
    mb = (b or {}).get("结论边界")

    if blocked:
        boundary, decided_by = "拒答", "rule"
        for c in blocked:
            reasons.append(f"{c.code} {c.name}：{c.detail[0] if c.detail else '未通过'}")
        reasons.append("存在上述硬性问题，不能给出可引用的条号依据")
    elif not usable and not stale_relied:
        # 「一条可交付依据都没有」有两种完全不同的成因，不能合并成拒答：
        #   ① 模型确实什么都没给出来            → 拒答
        #   ② 回答落在时效台账上（问"还有效吗"）→ 结论可用，只是没有条号可引用
        # 第 ② 类靠 _ledger_corroborated 客观识别，见该函数的注释。
        if mb in ("部分作答", "可作答") and _ledger_corroborated(b.get("时效状态"), touched):
            boundary, decided_by = "部分作答", "rule"
            reasons.append(
                "回答没有可引用的条号 —— 它的依据落在时效台账上（产品的权威数据源），"
                f"而模型自报的时效与台账逐字一致，结论可用，但不构成可引用的条文依据")
            if mb == "可作答":
                reasons.append("模型自报「可作答」，但本次没有任何可反查的条文依据，"
                               "按更保守的「部分作答」交付")
        else:
            boundary, decided_by = "拒答", "rule"
            reasons.append("回答没有给出任何能在本次召回中反查到、且现行有效的条文依据")
            if stale_laws:
                reasons.append(f"问题涉及 {'、'.join(stale_laws)}，该文件已失效，"
                               f"回答也未依据其条文 → 不存在可引用的现行依据")
    elif stale_relied and not usable:
        boundary, decided_by = "部分作答", "rule"
        reasons.append(f"回答只依据了已失效法规的条文（{stale_relied[0].art}），"
                       f"只能算历史参考")
    elif stale_relied or warned:
        boundary, decided_by = "部分作答", "rule"
        for c in warned:
            reasons.append(f"{c.code} {c.name}：{c.detail[0] if c.detail else '需注意'}")
        reasons.append("依据可用但需用户另行核验，按「部分作答」交付")
    elif mb == "拒答":
        boundary, decided_by = "拒答", "model"
        reasons.append("规则校验全部通过，模型仍自评为「拒答」，采信更保守的一方")
    elif mb == "部分作答":
        boundary, decided_by = "部分作答", "model"
        reasons.append("规则校验全部通过，模型自报「部分作答」（依据可用但需另行核验）")
    else:
        boundary, decided_by = "可作答", ("model" if mb else "rule")
        reasons.append("五项校验全部通过，引用逐条可反查，依据可交付")

    return Verdict(boundary=boundary, decided_by=decided_by, reasons=reasons,
                   checks=checks, cites=cites,
                   model_boundary=(b or {}).get("结论边界"),
                   stale_laws=stale_laws)


# ────────────────────────────── 命令行 ──────────────────────────────

def _load_app() -> dict:
    for name in ("app-v6.json", "app.json"):
        p = lk.ROOT / "eval" / name
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit("[x] 找不到 eval/app-v6.json 或 eval/app.json")


def _ask_app(info: dict, query: str) -> dict:
    import urllib.error
    import urllib.request
    body = {"inputs": {}, "query": query, "response_mode": "blocking",
            "user": "gatekeeper"}
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


# 自检用例：不依赖模型，纯规则，用来验证校验层本身的行为是否符合预期。
SELFTEST_CASES = [
    {
        "name": "越界条号必须被拦截（深圳条例 66 条，问第八十条）",
        "question": "《深圳经济特区规划土地监察条例》第八十条关于强制拆除违法建筑的程序是怎么规定的？",
        "answer": ("结论：该程序规定在第八十条。\n"
                   "【引用清单】深圳经济特区规划土地监察条例|第八十条"),
        "resources": [{"name": "深圳经济特区规划土地监察条例_20260901",
                       "content": "第一条 为了加强规划土地监察工作……\n第二十二条 强制拆除违法建筑，应当……"}],
        "expect": "拒答",
        "expect_codes": ["C1"],
    },
    {
        "name": "库外法规必须被拦截（上海条例不在库内）",
        "question": "《上海市生活垃圾管理条例》对湿垃圾是怎么规定的？",
        "answer": ("结论：该条例第四条规定了湿垃圾的定义。\n"
                   "【引用清单】上海市生活垃圾管理条例|第四条"),
        "resources": [{"name": "葫芦岛市生活垃圾分类管理条例_20260819",
                       "content": "第四条 生活垃圾分为可回收物、厨余垃圾、有害垃圾、其他垃圾。"}],
        "expect": "拒答",
        "expect_codes": ["C2"],
    },
    {
        "name": "时效硬规则必须被拦截（已废止法规仍给条号指引）",
        "question": "我要依据《中华人民共和国噪声污染防治法》对一家夜间施工企业作出行政处罚，处罚决定书上应当引用第几条？",
        "answer": ("⚠ 时效提示：该法已于 2026-08-15 失效，以下仅作历史参考。\n"
                   "结论：处罚依据是该法第七十七条。\n"
                   "【引用清单】中华人民共和国噪声污染防治法|第七十七条"),
        "resources": [{"name": "噪声污染防治法",
                       "content": "第七十七条 违反本法规定，建设单位、施工单位有下列行为之一……"}],
        "expect": "拒答",
        "expect_codes": ["C3"],
    },
    {
        "name": "已废止法规＋拒答且未给条号 → 拒答（不得降级）",
        "question": "我要依据《中华人民共和国噪声污染防治法》对一家夜间施工企业作出行政处罚，处罚决定书上应当引用第几条？",
        "answer": ("⚠ 时效提示：《中华人民共和国噪声污染防治法》已于 2026-08-15 失效。\n"
                   "结论：该法已废止，不存在可引用的现行依据，"
                   "我无法告诉你处罚决定书上应当引用第几条。\n"
                   "【引用清单】无"),
        "resources": [{"name": "噪声污染防治法",
                       "content": "第七十七条 违反本法规定，建设单位、施工单位有下列行为之一……"}],
        "expect": "拒答",
        "expect_codes": [],
    },
    {
        "name": "清单声明无依据、正文只作历史说明 → 拒答",
        "question": "我要依据《中华人民共和国噪声污染防治法》对一家夜间施工企业作出行政处罚，处罚决定书上应当引用第几条？",
        "answer": ("⚠ 时效提示：该法已于 2026-08-15 失效，以下内容仅作历史参考。\n"
                   "结论：该法已废止，不存在可引用的现行依据，我无法告知处罚决定书应当引用第几条。\n"
                   "依据：\n"
                   "- 【《中华人民共和国噪声污染防治法》第四十三条】原文关键句："
                   "「在噪声敏感建筑物集中区域，禁止夜间进行产生噪声的建筑施工作业……」"
                   "（该条是禁止性规定，随该法失效，不可作为现行依据引用）\n"
                   "【引用清单】无"),
        "resources": [{"name": "噪声污染防治法",
                       "content": "第四十三条 在噪声敏感建筑物集中区域，禁止夜间进行产生噪声的建筑施工作业……"}],
        "expect": "拒答",
        "expect_codes": [],
    },
    {
        "name": "自相矛盾必须被拦截（登记已失效却自报可作答）",
        "question": "《中华人民共和国噪声污染防治法》第三十九条对建筑施工噪声的定义是什么？",
        "answer": ("【边界判定】\n引用法规在库内：有全文\n所问条号核验：存在\n"
                   "时效状态：已废止\n结论边界：可作答\n\n"
                   "结论：建筑施工噪声是指在建筑施工过程中产生的干扰周围生活环境的声音。\n"
                   "【引用清单】中华人民共和国噪声污染防治法|第三十九条"),
        "resources": [{"name": "噪声污染防治法",
                       "content": "第三十九条 本法所称建筑施工噪声，是指在建筑施工过程中产生的干扰周围生活环境的声音。"}],
        "expect": "拒答",
        "expect_codes": ["C4"],
    },
    {
        "name": "正常回答必须放行（现行有效法规，引用可反查）",
        "question": "怀化市对扬尘污染防治实施统一监督管理的是哪个部门？",
        "answer": ("【边界判定】\n引用法规在库内：有全文\n所问条号核验：未指定\n"
                   "时效状态：现行有效\n结论边界：可作答\n\n"
                   "结论：生态环境主管部门。\n"
                   "【引用清单】怀化市扬尘污染防治条例|第五条"),
        "resources": [{"name": "怀化市扬尘污染防治条例_20260825",
                       "content": "第五条 生态环境主管部门对本辖区内的扬尘污染防治实施统一监督管理……"}],
        "expect": "可作答",
        "expect_codes": [],
    },
    {
        "name": "失效法规只做历史说明 → 部分作答（不该被拦截）",
        "question": "《中华人民共和国噪声污染防治法》第七十七条规定的罚则内容是什么？",
        "answer": ("⚠ 时效提示：该法已于 2026-08-15 失效，以下内容仅作历史参考。\n"
                   "结论：该条规定的罚则内容见下。\n【引用清单】中华人民共和国噪声污染防治法|第七十七条"),
        "resources": [{"name": "噪声污染防治法",
                       "content": "第七十七条 违反本法规定，建设单位、施工单位有下列行为之一，由工程所在地人民政府指定的部门责令改正……"}],
        "expect": "部分作答",
        "expect_codes": [],
    },
    {
        "name": "只问效力状态、没有条号 → 部分作答（依据在时效台账里，不是拒答）",
        "question": "《中华人民共和国噪声污染防治法》现在还有效吗？",
        "answer": ("【边界判定】\n引用法规在库内：有全文\n所问条号核验：未指定\n"
                   "时效状态：已废止\n结论边界：部分作答\n\n"
                   "结论：该法已于 2026-08-15 废止，现行有效的是《中华人民共和国生态环境法典》。\n"
                   "【引用清单】无"),
        "resources": [{"name": "噪声污染防治法", "content": "第一条 为了防治噪声污染……"}],
        "expect": "部分作答",
        "expect_codes": [],
    },
    {
        "name": "自报时效与台账不符 → 不得放行（拒答）",
        "question": "《中华人民共和国噪声污染防治法》现在还有效吗？",
        "answer": ("【边界判定】\n引用法规在库内：有全文\n所问条号核验：未指定\n"
                   "时效状态：现行有效\n结论边界：部分作答\n\n"
                   "结论：该法现行有效。\n【引用清单】无"),
        "resources": [{"name": "噪声污染防治法", "content": "第一条 为了防治噪声污染……"}],
        "expect": "拒答",
        "expect_codes": [],
    },
    {
        "name": "问库外法规、答成库内另一部 → 拦截（C5）",
        # 这一条来自 MVP（直连 qwen-plus）的真实调用：
        # 用户问的是库外的《城市市容和环境卫生管理条例》，检索召回了库内的
        # 《呼伦贝尔市城市市容和环境卫生管理条例》（两部都管户外广告、牌匾标识），
        # 模型据后者作答并自报「有全文 / 可作答」—— C1–C4 全过。
        "question": "《城市市容和环境卫生管理条例》对户外广告和牌匾标识的设置是怎么规定的？",
        "answer": ("【边界判定】\n引用法规在库内：有全文\n所问条号核验：未指定\n"
                   "时效状态：现行有效\n结论边界：可作答\n\n"
                   "结论：该条例规定户外广告、牌匾标识应当按照规划要求设置。\n"
                   "【引用清单】呼伦贝尔市城市市容和环境卫生管理条例|第十七条"),
        "resources": [{"name": "呼伦贝尔市城市市容和环境卫生管理条例",
                       "content": "第十七条 户外广告、牌匾标识、霓虹灯、电子显示屏（牌）、"
                                  "灯箱等的设置应当符合城市容貌标准……"}],
        "expect": "拒答",
        "expect_codes": ["C5"],
    },
]


def selftest() -> int:
    print("=" * 78)
    print(f"闸门 2 自检 · {len(SELFTEST_CASES)} 组规则用例（不调用任何模型）")
    print("=" * 78)
    bad = 0
    for case in SELFTEST_CASES:
        v = verify(case["question"], case["answer"], case["resources"])
        blocks = [c.code for c in v.checks if c.level == BLOCK and not c.passed]
        warns = [c.code for c in v.checks if c.level == WARN and not c.passed]
        ok = v.boundary == case["expect"] and all(x in blocks for x in case["expect_codes"])
        mark = "✓" if ok else "✗"
        print(f"\n{mark} {case['name']}")
        print(f"    期望 {case['expect']}／拦截 {case['expect_codes'] or '无'}"
              f"    实得 {v.boundary}／拦截 {blocks or '无'}／提示 {warns or '无'}")
        if not ok:
            bad += 1
            for c in v.checks:
                if not c.passed:
                    print(f"      [{c.code}/{c.level}] {c.detail}")
    print("\n" + "=" * 78)
    print(f"自检结果：{'全部通过' if not bad else f'{bad} 条不符'}")
    return bad


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(1 if selftest() else 0)

    if "--ask" in sys.argv:
        q = sys.argv[sys.argv.index("--ask") + 1]
        info = _load_app()
        resp = _ask_app(info, q)
        if resp.get("_error"):
            raise SystemExit(f"[x] 调用失败：{resp['_error']}")
        ans = resp.get("answer") or ""
        res = ((resp.get("metadata") or {}).get("retriever_resources")) or []
        print("=" * 78)
        print(f"问：{q}")
        print("=" * 78)
        print(ans)
        print("=" * 78)
        v = verify(q, ans, res)
        print(v.render())
        out = lk.ROOT / "eval" / "results" / "gatekeeper-last.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"question": q, "answer": ans,
                                   "resources": res, "verdict": v.to_dict()},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整记录写入 {out}")
        return

    if "--answer" in sys.argv:
        ap = Path(sys.argv[sys.argv.index("--answer") + 1])
        answer = ap.read_text(encoding="utf-8")
        q = sys.argv[sys.argv.index("--question") + 1] if "--question" in sys.argv else ""
        res = []
        if "--resources" in sys.argv:
            res = json.loads(Path(sys.argv[sys.argv.index("--resources") + 1])
                             .read_text(encoding="utf-8"))
        v = verify(q, answer, res)
        print(v.render())
        return

    print(__doc__)


if __name__ == "__main__":
    main()
