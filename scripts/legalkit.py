#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""legalkit —— 法规文本处理的共用底层。

**为什么要有这个文件**：同一个「把法规名对上号」「把中文条号变成数字」的需求，
在评测脚本（`eval_run_app.py`）和产品校验层（`gatekeeper.py`）里各写一遍，
就会出现两份实现、两套边界情况、两个地方改 bug。这两处必须给出**完全一致**的结论，
否则"评测通过了、上线出问题"就成了必然。

所以这里只放三件事，其余一律不放：
  1. **名称归一**：把用户/模型/落库文件名里的各种写法对到库内正式全称
  2. **条号解析**：中文条号 ⇄ 整数，以及从文本里提取「法规+条号」
  3. **三张表读取**：条数表、时效台账、名称映射

三张表的分工（都从语料派生，语料变更后必须一起重建）：

| 表 | 文件 | 谁生成 | 回答什么问题 |
|---|---|---|---|
| 条数表 | `data/article_counts.json` | `export_article_counts.py` | 这部法规一共多少条 |
| 时效台账 | `data/stale_ledger.json` | `export_stale_ledger.py` | 这部法规还有没有效 |
| 名称映射 | `data/doc_names.json` | `build_kb_text_v2.py` | 落库代号对应哪个正式全称 |

用法：
    from legalkit import law_pairs, article_count, stale_info, query_laws, parse_citation
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

NAMES_PATH = DATA / "doc_names.json"
COUNTS_PATH = DATA / "article_counts.json"
LEDGER_PATH = DATA / "stale_ledger.json"

# 「第X条」/「第X条之一」。覆盖中文数字与阿拉伯数字两种写法。
ART_RE = re.compile(r"第\s*([一二三四五六七八九十百千零〇0-9]{1,8})\s*条(?:之[一二三四五六七八九十]{1,3})?")
# 【引用清单】法规全名|第X条
CITE_RE = re.compile(r"【引用清单】\s*(.+?)\s*$", re.M)
# 区块结束的判定：下一行是「行首的【…】标题」或「行首的区块标题（允许 markdown 井号）」。
#
# **这里踩了两个坑，都不轻，所以注释写详细。**
#
# 坑 1 · 只写 `\n\s*【` 是不够的。模型常把标题写成「### 【引用清单】」，
#   那种写法下 `\n\s*【` 匹配不上（"### " 不是空白），区块会一路吃到文末。
#   实测后果：`_first_paragraph` 把【引用清单】的内容当成了结论（用户看到的结论
#   是一串条号）；【历史沿革】区块吞掉它后面的整个结论区 → 时效硬规则的操作性
#   指引检查漏检（该拦的没拦住，且不报错）。
#
# 坑 2 · 把「结论」列进标题词之后，「**结论边界**：可作答」也满足
#   「行首是结论」，于是【边界判定】块在第二行就被截断，四个字段全丢。
#   所以「结论」必须带否定前瞻 `(?!边界)`。
#
# 坑 3 · 标题的**序号前缀**。模型写「### 2. 结论」时，`#{1,6}\s*` 匹配掉「### 」
#   之后要求立刻是「结论」，而实际还有「2. 」—— 于是整条 lookahead 失配，
#   区块一路吃到下一个能匹配上的地方（实测吃到【引用清单】之前），
#   把「结论」和「依据」两节一起吞了。后果是 `ask.py` 抽出的结论为空，
#   用户看到「（见下方原始回答）」。与坑 1 属于同一类：
#   **模型换了个写法，正则没跟上**。
#   所以这里把「数字. 」「（三）」这类前缀也纳入标题格式。
#
# 只列**强**标题词：松散的「说明」「注」「备注」也会出现在正文行首，
# 列进来会误截断区块（宁可漏截，不可误截）。
_HEAD_NUM = (r"(?:(?:\d+|[一二三四五六七八九十]+)\s*[.、)）]\s*"
             r"|[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[)）]\s*)*")
_SEC_END = (r"(?=\n\s*(?:#{1,6}\s*)?" + _HEAD_NUM +
            r"(?:【|结论(?!边界)|依据清单|依据|引用清单|边界判定|历史沿革|时效提示)|\Z)")

# 【边界判定】四行块
BOUNDARY_BLOCK_RE = re.compile(r"【边界判定】[^\n]*\n(.*?)" + _SEC_END, re.S)
# 【历史沿革】一节（允许出现失效法规的条号，检查时要排除）
HISTORY_BLOCK_RE = re.compile(r"【历史沿革】.*?" + _SEC_END, re.S)
# 书名号里的法规名
TITLE_MARK_RE = re.compile(r"《([^》]{2,60})》")

# 边界判定四行的取值域。**必须与 prompts/system-v3.md 第 3 节逐字一致** ——
# 提示词改了这里不改，字段就会被判成"缺失"，而真正的原因是枚举对不上。
BOUNDARY_FIELDS: dict[str, list[str]] = {
    "引用法规在库内": ["有全文", "仅被提及", "不在库内"],
    "所问条号核验": ["存在", "不存在", "未指定"],
    "时效状态": ["现行有效", "已废止", "已失效", "未知"],
    "结论边界": ["可作答", "部分作答", "拒答"],
}

STALE_STATES = ("已废止", "已失效")

# 「拿条号去办事」的表述 —— 时效硬规则要堵的对象。
# 不用「(应当|应…)引用第X条」这种窄写法：L4-26 的真实表述是「处罚依据是该法第七十七条」，
# 窄写法会漏掉它（第一版就漏了）。宁可宽一点，由调用方限定使用场景。
OPERATIONAL_CITE_RE = re.compile(
    r"(?:应当|应以|应予|应|须|需|可以|建议|依法|直接|按|依据|适用|援引|引用|依据是|适用的是)"
    r"[^。；！？\n]{0,10}第[一二三四五六七八九十百千零〇0-9]{1,8}条"
)

# 归一化用：去扩展名、去尾部日期、去 FBM 编号
_STRIP_TAIL = re.compile(r"[_\-]?20\d{6}$")
_STRIP_FBM = re.compile(r"[（(]FBM-[^）)]*[）)]")
_STRIP_EXT = re.compile(r"\.(txt|docx|doc|md)$")
_PUNCT = re.compile(r"[，。、；：（）()「」『』《》\"'’“”—\-…\s]")


# ────────────────────────────── 名称归一 ──────────────────────────────

def norm_doc(name: str) -> str:
    """把落库文件名还原成可读名称：去扩展名、去尾部 _YYYYMMDD、去 FBM 编号。"""
    n = (name or "").strip()
    n = _STRIP_FBM.sub("", n)
    n = _STRIP_EXT.sub("", n)
    return _STRIP_TAIL.sub("", n).strip()


def _fold(name: str) -> str:
    """匹配专用归一：抹平括号全半角、空格、以及「常务委员」的两种写法。

    这三处不一致在真实数据里都出现过，每处都造成过一次匹配失败：
      · 全角（）vs 半角 ()  —— manifest 与落库文件名不同
      · 「人民代表大会常务委员」vs「人大常委」—— 同一部海南决定的两种写法
    不抹平就会把同一部法规登记成两条，或把一部法规漏掉。
    """
    n = norm_doc(name).translate(str.maketrans("（）", "()")).replace(" ", "")
    return n.replace("人民代表大会常务委员会", "人大常委会")


@lru_cache(maxsize=1)
def load_names() -> dict[str, str]:
    """落库代号 → 正式全称。"""
    if not NAMES_PATH.exists():
        return {}
    return json.loads(NAMES_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def law_pairs() -> tuple[tuple[str, str], ...]:
    """((正式全称, 落库代号去日期), ...)。落库代号短、可以做文本匹配的锚点。"""
    return tuple((full, norm_doc(k)) for k, full in load_names().items())


@lru_cache(maxsize=1)
def _fold_index() -> dict[str, str]:
    """折叠形式 → 正式全称。三种写法都进索引：全称、落库代号、折叠后的全称。"""
    idx: dict[str, str] = {}
    for key, full in load_names().items():
        idx[_fold(full)] = full
        idx[_fold(key)] = full
    return idx


def match_law(name: str) -> str | None:
    """把任意写法的法规名对到库内正式全称；不在库内返回 None。

    **这是精确匹配，不是子串匹配。** 子串匹配在这个领域会造成实质错误：
    「环境噪声污染防治法」（旧法）包含「噪声污染防治法」（新法），
    子串匹配会把问旧法的问题按新法回答，而两者的效力状态完全相反
    （一个不存在于库内，一个已废止）。这个坑在本项目里出现过两次，
    所以这里刻意不做 `in` 判断，只做折叠后的相等比较。
    """
    return _fold_index().get(_fold(name))


@lru_cache(maxsize=1)
def _counts() -> dict:
    if not COUNTS_PATH.exists():
        return {"counts": {}, "alias": {}}
    return json.loads(COUNTS_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _ledger() -> dict:
    if not LEDGER_PATH.exists():
        return {}
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def article_count(law: str) -> int | None:
    """该法规的条款数；查不到返回 None。

    注意 0 是有意义的取值：海南决定是本机语料里唯一「无条款结构」的文件
    （它是一份授权决定，正文是表格而非条文），0 表示"引不出任何条号"，
    与「查不到」不是一回事。
    """
    by_alias = _counts().get("counts") or {}
    if law in by_alias:
        return by_alias[law]
    for k, v in by_alias.items():
        if match_law(k) == law or match_law(law) == match_law(k):
            return v
    return None


def stale_info(law: str) -> dict | None:
    """该法规的时效信息；不在台账中返回 None（口径：未知，不做有利假设）。"""
    if law in _ledger():
        return _ledger()[law]
    for k, v in _ledger().items():
        if match_law(k) == law:
            return v
    return None


def is_stale(law: str) -> bool:
    info = stale_info(law)
    return bool(info) and info.get("时效性") in STALE_STATES


def ledger() -> dict[str, dict]:
    """整张时效台账（法规全称 → 时效信息）。需要批量比对时用，单部法规用 stale_info。"""
    return _ledger()


def query_laws(question: str, *, include_full_names: bool = False) -> list[str]:
    """问题指向了哪几部**库内**法规 —— 只认书名号内容，且必须精确对上。

    为什么不扫全句：题面里出现「噪声污染防治法」不等于问的是库内那部新法
    （可能是「环境噪声污染防治法」这个旧法名的一部分）。只认书名号内容
    + 精确匹配，宁可漏判也不误判 —— 漏判只是少一个提示，误判会给出相反的效力结论。

    `include_full_names=True` 时，另外接受「问题里**逐字出现了库内法规的正式全称**」，
    不要求带书名号。产品层需要它：用户问「……授权决定，现在还有效吗？」时常常
    不打书名号，只认书名号会把这类问题全判成"没指向任何库内法规"。

    **只放开正式全称，不放开落库代号/简称** —— 简称会撞上「长名包含短名」那个坑
    （「环境噪声污染防治法」包含「噪声污染防治法」，旧法会被读成新法）。
    正式全称里带「环境」二字，逐字比对天然区分得开。
    """
    q = question or ""
    out: list[str] = []
    for m in TITLE_MARK_RE.finditer(q):
        full = match_law(m.group(1))
        if full and full not in out:
            out.append(full)
    if include_full_names:
        flat = _fold(q)
        for key, full in load_names().items():
            if full in out:
                continue
            f = _fold(full)
            if f and f in flat:
                out.append(full)
    return out


# ────────────────────────────── 条号与引用 ──────────────────────────────

def cn2int(s: str) -> int | None:
    """中文数字转整数（条号不超过三位数，够用即可）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    d = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9}
    u = {"十": 10, "百": 100, "千": 1000}
    total, cur = 0, 0
    for ch in s:
        if ch in d:
            cur = d[ch]
        elif ch in u:
            total += (cur or 1) * u[ch]
            cur = 0
        else:
            return None
    return total + cur


def parse_citation(answer: str) -> tuple[list[tuple[str, str]], str]:
    """解析【引用清单】。返回 ([(法规名, 条号)], 状态)。

    状态：
      ok       有清单内容
      none     明确写了「无」
      missing  没输出清单（模型没遵守格式，或回答被截断）
    """
    hits = [h.strip() for h in CITE_RE.findall(answer or "") if h.strip()]
    if not hits:
        return [], "missing"
    if all(x.strip("【】 ") in ("无", "無") for x in hits):
        return [], "none"
    out: list[tuple[str, str]] = []
    for ln in hits:
        # 容错：模型有时写成「【引用清单】法规|条号」，整行被捕获进来
        ln = ln.replace("【引用清单】", "").strip()
        if "|" not in ln:
            continue
        law, _, art = ln.partition("|")
        law, art = law.strip().strip("《》"), art.strip()
        if law:
            out.append((law, art))
    return out, ("ok" if out else "none")


def _pick_option(value: str, options: list[str]) -> str | None:
    """在候选里选一个：取**最长匹配**。

    不能用 `opt in value` 直接判：候选「存在」是「不存在」的子串，
    value 为「不存在」时会同时命中两者；若先命中「存在」，
    一条正确的越界题会被读成"模型声称该条号存在"，判断整个反过来。
    """
    v = (value or "").strip().strip("。，、；;：: ").replace(" ", "")
    for opt in sorted(options, key=len, reverse=True):
        if opt in v:
            return opt
    return None


def parse_boundary(answer: str) -> tuple[dict[str, str | None], list[str]]:
    """解析【边界判定】四行。返回 ({字段: 取值}, [缺失或无法解析的字段])。"""
    got: dict[str, str | None] = {}
    bad: list[str] = []
    m = BOUNDARY_BLOCK_RE.search(answer or "")
    if not m:
        return {}, list(BOUNDARY_FIELDS)
    for key, options in BOUNDARY_FIELDS.items():
        val = None
        for line in m.group(1).split("\n"):
            if key not in line:
                continue
            if "：" in line:
                raw = line.split("：", 1)[1]
            elif ":" in line:
                raw = line.split(":", 1)[1]
            else:
                raw = line.replace(key, "", 1)
            val = _pick_option(raw, options)
            break
        if val is None:
            bad.append(key)
        got[key] = val
    return got, bad


def boundary_consistency(b: dict[str, str | None]) -> tuple[bool, str | None]:
    """由【边界判定】前三行的**客观事实**推出「应然结论边界」，与第四行比对。

    返回 (是否一致, 不一致说明)。字段缺失时不判矛盾（另有「字段合规率」单独统计）。

    这是产品校验层（C4）与评测判分器共用的一段判断：两侧各写一遍就会各有一套
    边界情况，而它判的正是「模型自相矛盾」，最不能有两套口径。
    """
    if any(b.get(k) is None for k in BOUNDARY_FIELDS):
        return True, None
    allow = {"可作答", "部分作答", "拒答"}
    why: list[str] = []
    if b["引用法规在库内"] in ("仅被提及", "不在库内"):
        allow.discard("可作答")
        why.append(f"库内={b['引用法规在库内']}")
    if b["所问条号核验"] == "不存在":
        allow.discard("可作答")
        why.append("条号不存在")
    if b["时效状态"] in STALE_STATES:
        allow.discard("可作答")
        why.append(f"时效={b['时效状态']}")
    if b["结论边界"] not in allow:
        return False, (f"{'、'.join(why) or '无约束'} ⇒ 结论边界应为 "
                       f"{'/'.join(sorted(allow))}，实填「{b['结论边界']}」")
    return True, None


def extract_cites(text: str, drop_history: bool = True) -> list[tuple[str, str]]:
    """从回答正文里提取 (法规名, 条号) 引用。

    两种来源都收：
      · 【《法规全名》第X条】…… 这种依据行（结构化引用）
      · 任意位置出现的「《法规全名》第X条」（散写引用）

    用途是**兜底**：模型可能把编造的条号写在正文里但不登记进清单。
    `drop_history=True` 时排除【历史沿革】一节 —— 那一节按规则可以有失效法规的条号。
    """
    t = text or ""
    if drop_history:
        h = HISTORY_BLOCK_RE.search(t)
        if h:
            t = t.replace(h.group(0), "")
    out: list[tuple[str, str]] = []
    # 结构化依据行 / 散写：找书名号法规名，再看其后 20 字内有没有条号
    for m in TITLE_MARK_RE.finditer(t):
        law = m.group(1).strip()
        tail = t[m.end(): m.end() + 24]
        for a in ART_RE.finditer(tail):
            out.append((law, a.group(0)))
            break
    # 正文里单跑的条号（无书名号）交给调用方决定怎么处理 —— 这里不猜
    return out


def loose_articles(text: str, drop_history: bool = True) -> list[str]:
    """正文里出现的所有条号（不要求带法规名）。用于「无据条号」检查。"""
    t = text or ""
    if drop_history:
        h = HISTORY_BLOCK_RE.search(t)
        if h:
            t = t.replace(h.group(0), "")
    return [m.group(0) for m in ART_RE.finditer(t)]


# ────────────────────────────── 文本比对 ──────────────────────────────

def squeeze(text: str) -> str:
    """去掉空白与标点，用于引文比对（模型引述时常做轻微改写，标点差异要先抹掉）。"""
    return _PUNCT.sub("", text or "")


def window_hit(needle: str, pool: list[str], w: int = 12) -> bool:
    """needle 中是否存在长度 w 的连续片段出现在 pool 任一段落里。

    用滑动窗口而不是「取头 N 字」：模型引述时会改连接词、删冗余修饰
    （「对于市规划土地监察机构依照…作出的」→「对市规划土地监察机构作出的」），
    取头 N 字会因为开头两个字不同就整条判失配 —— 实测这一条制造了 2/3 的噪声。
    """
    if not needle:
        return True
    if len(needle) <= w:
        return any(needle in s for s in pool)
    return any(needle[k:k + w] in s for s in pool for k in range(len(needle) - w + 1))


def law_in_docname(law: str, doc: str) -> bool:
    """判断召回段落所属文件是不是这部法规。用折叠后的包含关系，容忍落库代号。"""
    a, b = _fold(law), _fold(doc)
    if not a or not b:
        return False
    return a in b or b in a


if __name__ == "__main__":
    # 自检：把三张表读一遍，确认口径一致
    pairs = law_pairs()
    print(f"法规 {len(pairs)} 部")
    for full, short in pairs:
        n = article_count(full)
        info = stale_info(full) or {}
        print(f"  {full}")
        print(f"      条数={n}  时效={info.get('时效性', '（不在台账）')}"
              f"  废止日={info.get('废止日期') or '—'}")
    stale = [f for f, _ in pairs if is_stale(f)]
    print(f"\n非现行有效 {len(stale)} 部：{stale}")
    print(f"条号自检：第二十二条 = {cn2int('二十二')}；"
          f"八十八 = {cn2int('八十八')}；一百零三 = {cn2int('一百零三')}")
