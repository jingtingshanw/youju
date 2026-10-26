#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""有据 · 核心链路 —— 检索、核验、生成，一个文件装完。

这个文件的每一个函数都对应界面上的一个步骤。顺序是：

    解析问题 → 三路召回 → 融合重排 → 闸门1 → 生成 → 闸门2（五项检查）→ 判定

**为什么检索要三路而不是一路**

单路 BM25 的失败方式很固定：用户问「《历史文化名城名镇名村保护条例》第三十三条
怎么规定的」，它会把「第三十三条」切成 2-gram 去跟全库做匹配，命中的是任何一份
写了「三十三条」的文件——法规名这个最强的信号，反而被稀释掉了。

三路各管一件事：
  A 实体路：问题里点名了哪份文件（书名号 / 法规名尾缀 / 地名+条例）→ 直接锁文件
  B 条款路：问题里点名了第几条 → 直接锁条款
  C 文本路：剩下的语义匹配，交给 2-gram BM25

三路用 RRF 融合。RRF 不比较分数、只比较名次，天然解决了「BM25 分数和规则分数
不是一个量纲」的问题——这也是它能不加调参就上线的唯一原因。

**为什么重排不交给模型**

重排要能解释。用户在界面上看到「这一条排第一」，必须能问「凭什么」并且得到
一个能听懂的回答：「因为问题里的 7 个实词它命中了 6 个，标题里点名了法规名，
而且它是条款层不是段落层。」模型的分数给不出这种解释。

**核验为什么要回原文**

条数表是「没有全文时的替代品」。我们现在有 18,608 份全文、7,291 万字、
101,813 条已切分的条文——判「第 X 条存不存在」，正确的做法是回原文查，
而不是先建一张表再查表。查表会漏：表只覆盖 8 部；回原文不会漏：覆盖每一份有
正文的文件。这是这一版核验层最重要的改动。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_DB = ROOT / "data" / "kb.db"
CAT_DB = ROOT / "data" / "catalog.db"
PROMPT_FILE = ROOT / "prompts" / "answer-v3.md"

# `.env` 找三个位置，取第一个存在的。
#
# 原来是写死 `ROOT.parent.parent / ".env"` —— 那是本地开发时的位置
# （WorkBuddy/ai-pm/.env，项目在 ai-pm/projects/policy-verify-engine）。
# 搬到服务器上就错了：项目在 /srv/policy-verify-engine 时，
# parent.parent 是根目录 `/`，得把 key 放进 `/.env` 才读得到。
# DEPLOY.md 里写「放上一级」也不对，代码当时要的是上两级。
# 三种说法互相打架，而症状是「启动正常、一调模型就说没有可用通道」——
# 这种错在部署现场最难查，所以改成按候选顺序找，谁先存在用谁。
ENV_CANDIDATES = [
    ROOT / ".env",              # 项目根目录（推荐，跟 .env.example 放一起）
    ROOT.parent / ".env",       # 上一级（/srv/.env）
    ROOT.parent.parent / ".env",  # 上两级（本地开发的老位置）
]
ENV_FILE = next((p for p in ENV_CANDIDATES if p.exists()), ENV_CANDIDATES[0])

# ══════════════════════════════════════════════════════════════════
# 一、查询解析
# ══════════════════════════════════════════════════════════════════

_STOP = set("""
怎么 怎样 什么 哪些 哪个 如何 是否 可以 应当 应该 需要 关于 对于 以及 或者 而且
请问 一下 有关 相关 规定 要求 内容 问题 情况 具体 目前 现在 现在 我们 他们 这个
那个 这些 那些 一个 进行 通过 根据 按照 依据 因为 所以 但是 如果 就是 还是 不是
有没 没有 么规 么要 么条 条规 的规 定的 定是 是多 多少 少钱 在什 哪一 一条
""".split())

_CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ART_Q = re.compile(r"第([〇零一二三四五六七八九十百千\d]{1,7})条")
_BOOK = re.compile(r"《([^》]{2,60})》")
_PLACE = re.compile(r"([\u4e00-\u9fa5]{2,8}?(?:省|市|自治区|自治州|自治县|县|区|旗))")


def cn2int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if "百" in s or "千" in s:
        unit = 1000 if "千" in s else 100
        head, _, rest = s.partition("千" if "千" in s else "百")
        base = (_CN_NUM.get(head[-1], 1) if head else 1) * unit
        return base + (cn2int(rest) if rest else 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return ((_CN_NUM.get(a, 0) if a else 1) * 10
                + (_CN_NUM.get(b, 0) if b else 0))
    return _CN_NUM.get(s, 0)


def int2cn(n: int) -> str:
    """条号在库里一律是中文数字（「第三十三条」），回查必须先转过来。

    这个转换看着琐碎，但它是上一版 C1 出错的地方：拿阿拉伯数字去 LIKE 中文
    条文，永远 0 命中——于是每条引用都被判成「越界」，而界面上显示的是
    「核验通过」。一个方向错的 bug，表现成一次全员通过。
    """
    if n <= 0:
        return ""
    d = "〇一二三四五六七八九"
    if n < 10:
        return d[n]
    if n < 20:
        return "十" + (d[n % 10] if n % 10 else "")
    if n < 100:
        return d[n // 10] + "十" + (d[n % 10] if n % 10 else "")
    if n < 1000:
        s = d[n // 100] + "百"
        r = n % 100
        if r == 0:
            return s
        if r < 10:
            return s + "〇" + d[r]
        return s + (d[r // 10] + "十" + (d[r % 10] if r % 10 else ""))
    return str(n)


def grams(text: str) -> set[str]:
    """中文 2-gram，去停用词。检索和重排共用同一套切法。"""
    out: set[str] = set()
    for tok in re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z0-9]+", text):
        if tok[0] < "\u4e00":
            out.add(tok.lower())
            continue
        if len(tok) == 1:
            out.add(tok)
        else:
            for i in range(len(tok) - 1):
                g = tok[i:i + 2]
                if g not in _STOP:
                    out.add(g)
    return out


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度。用于标题命中判断（比词覆盖率锐利得多）。"""
    if not a or not b:
        return 0
    a, b = a[:60], b[:60]
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


class Query:
    """把一句人话拆成检索能用的四个信号。

    **实体识别不靠正则猜，靠库里的真实名称反查。**
    上一版用「以条例/办法/规定结尾」去切，把「第三十三条对历史建筑修缮是怎么规定」
    也切成了法规名——因为它也以「规定」结尾。这类错误没法靠加规则补完。
    改成：把全库 18,608 个标题 + 1,489 个地域名装进内存，问题进来就用
    最长子串去撞。撞上的就是真实存在的文件，撞不上的就是库里没有——后者本身
    也是有用的信号（闸门 1 要用）。
    """

    __slots__ = ("raw", "laws", "law_keys", "article", "places", "terms", "kw")

    _NAMES: dict[str, list[str]] | None = None
    _PLACES: dict[str, int] | None = None

    @classmethod
    def _load(cls) -> None:
        if cls._NAMES is not None:
            return
        names: dict[str, list[str]] = {}
        places: dict[str, int] = {}
        try:
            c = _ro(CAT_DB)
            for r in c.execute("SELECT key,title FROM fulltext WHERE status='ok'"):
                n = _core_name(r["title"])
                if len(n) >= 4:
                    names.setdefault(n, []).append(r["key"])
            for r in c.execute("SELECT region, COUNT(*) n FROM items "
                               "WHERE region IS NOT NULL AND region<>'' GROUP BY region"):
                reg = r["region"].strip()
                if 2 <= len(reg) <= 12:
                    places[reg] = r["n"]
            c.close()
        except sqlite3.Error:
            pass
        cls._NAMES, cls._PLACES = names, places

    def __init__(self, raw: str):
        self._load()
        self.raw = raw
        self.laws: list[str] = []
        self.law_keys: list[str] = []

        # ① 书名号里的，一律当法规名（哪怕库里没有——那也要能说出来「没有」）
        for m in _BOOK.finditer(raw):
            t = m.group(1).strip()
            if t and t not in self.laws:
                self.laws.append(t)
        # ② 用库内真实标题做最长子串匹配，补上不带书名号的点名
        flat = _core_name(raw)
        for i in range(len(flat)):
            for j in range(min(len(flat), i + 26), i + 3, -1):
                seg = flat[i:j]
                if seg in self._NAMES:
                    if not any(seg in x or x in seg for x in self.laws):
                        self.laws.append(seg)
                    self.law_keys.extend(self._NAMES[seg])
                    break
        # ③ 地域名，同样用库内 region 反查
        self.places = []
        for i in range(len(flat)):
            for j in range(min(len(flat), i + 12), i + 1, -1):
                seg = flat[i:j]
                if seg in self._PLACES and seg not in self.places:
                    self.places.append(seg)
                    break

        m = _ART_Q.search(raw)
        self.article = cn2int(m.group(1)) if m else 0

        # ④ 查询词：扣掉法规名和地域名后的 2-gram
        g = grams(raw)
        for x in self.laws + self.places:
            g -= grams(x)
        self.terms = {t for t in g if len(t) == 2}
        self.kw = [t for t in re.split(r"[\s，。？?！!、；;：:（）()]+", raw) if len(t) >= 2]

    def as_dict(self) -> dict:
        return {"raw": self.raw, "laws": self.laws[:4], "article": self.article,
                "places": self.places[:3], "terms": sorted(self.terms)[:24],
                "kw": self.kw[:8]}


def _core_name(t: str) -> str:
    """标题归一：去书名号/空白/括号注释/发文机关前缀，得到能用来撞名的核心串。"""
    t = re.sub(r"[《》\s　]", "", t or "")
    t = re.sub(r"[（(][^）)]{0,40}[）)]", "", t)
    # 「国务院关于…的通知」这类，核心是后半段；但「XX省XX条例」不能砍前缀
    m = re.match(r"^.*?(?:关于|印发|批转|转发)(.+?)(?:的)?(?:通知|意见|决定|批复|函)$", t)
    if m and len(m.group(1)) >= 6:
        t = m.group(1).lstrip("《")
    return t


# ══════════════════════════════════════════════════════════════════
# 二、三路召回 + RRF 融合 + 重排
# ══════════════════════════════════════════════════════════════════

def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


class Retriever:
    def __init__(self) -> None:
        self.kb = _ro(KB_DB)
        self.cat = _ro(CAT_DB)
        # 失效分三档：明确废止 / 有同名新版取代 / 有效期届满。三者都不该被优先装配，
        # 但旧版的条文往往和新版字面几乎一样，会和新版抢同一个位置——
        # 不降权就会出现「检索到旧版、引注写旧版、C3 判失败」的死循环。
        # 到期失效的降权幅度略小于废止：它的失效依据是原文自载的日期，可复算；
        # 而且「有效期至 2026-10-23」这类刚过期几天的文件，往往正是用户手上那版。
        self._dead: dict[str, float] = {}
        for r in self.cat.execute(
                "SELECT key,status FROM eff_ledger "
                "WHERE status IN ('repealed','superseded','expired')"):
            self._dead[r["key"]] = {"repealed": 0.30, "superseded": 0.22,
                                    "expired": 0.26}[r["status"]]

    def close(self) -> None:
        self.kb.close()
        self.cat.close()

    # ── A 实体路：问题点名了哪份文件 ────────────────────────────
    def route_entity(self, q: Query, per_law: int = 6) -> list[dict]:
        """锁定文件之后，不是把它的头几块拿来充数，而是**在这份文件内部再排一次**。

        上一版直接取 `ORDER BY seq LIMIT 40`，结果问「《XX条例》第三十三条」
        返回的是「第一章 总则 第一条」。文件锁对了，块锁错了，而且错得很隐蔽：
        界面上文件名是对的，用户会以为找对了。
        """
        keys = list(dict.fromkeys(q.law_keys))[:4]
        if not keys and q.laws:
            # 书名号里的名字库里没有（或名字被改过），退一步用标题模糊找
            for law in q.laws:
                n = _core_name(law)
                if len(n) < 4:
                    continue
                for r in self.kb.execute(
                        "SELECT doc_key FROM docs WHERE title LIKE ? LIMIT 20",
                        (f"%{n}%",)):
                    keys.append(r["doc_key"])
        if not keys:
            return []
        out: list[dict] = []
        terms = q.terms
        for key in keys[:6]:
            rows = self.kb.execute(
                "SELECT doc_key,title,label,text,kind,seq FROM blocks WHERE doc_key=?",
                (key,)).fetchall()
            if not rows:
                continue
            if q.article:                      # 点名了条号，块就该是那一条
                want = f"第{int2cn(q.article)}条"
                hit = [r for r in rows if re.search(rf"{want}(?!之)", r["label"] or "")]
                if hit:
                    out.extend({**dict(r), "_route": "entity"} for r in hit[:2])
                    continue
            scored = []
            for r in rows:
                g = grams(f"{r['label'] or ''} {r['text'][:900]}")
                scored.append((len(terms & g), r))
            scored.sort(key=lambda x: (-x[0], x[1]["seq"]))
            out.extend({**dict(r), "_route": "entity"} for _, r in scored[:per_law])
        return out

    # ── B 条款路：问题点名了第几条 ──────────────────────────────
    def route_article(self, q: Query, allow: set[str] | None = None) -> list[dict]:
        """只在「文件已被锁定」时才跑。

        单独凭一个条号去全库召回是没意义的：问「XX 条例第五条」，库里只要有
        四十份文件写了「第五条」，它们全都会被召回，而且因为命中了条号这一路，
        排序还会靠前。上一版就是这么把《先照后证改革意见》排到第一的。
        条号是**文件内部的定位器**，脱离文件它什么都不是。
        """
        if not q.article:
            return []
        want = f"第{int2cn(q.article)}条"       # 库里是中文数字，这里必须转
        rows = self.kb.execute(
            "SELECT doc_key,title,label,text,kind,seq FROM blocks "
            "WHERE kind='art' AND label LIKE ? LIMIT 200", (f"%{want}%",)).fetchall()
        out = []
        for r in rows:
            if not re.search(rf"{want}(?!之)", r["label"] or ""):
                continue
            if allow and r["doc_key"] not in allow:
                continue
            out.append({**dict(r), "_route": "article"})
            if len(out) >= 40:
                break
        return out

    # ── C 文本路：2-gram BM25 ───────────────────────────────────
    def route_text(self, q: Query, limit: int = 60) -> list[dict]:
        return self.route_text_terms(q.terms, limit)

    def route_text_terms(self, terms: set[str], limit: int = 60) -> list[dict]:
        gs = [g for g in terms if len(g) == 2][:20]
        if not gs:
            return []
        # 先严后宽：全部 gram 都要在 → 再退到任一词组命中
        strict = " AND ".join(f'"{g}"' for g in gs)
        loose = " OR ".join(f'"{g}"' for g in gs)
        sql = ("SELECT b.doc_key,b.title,b.label,b.text,b.kind,b.seq, "
               "       bm25(blocks_fts) AS s "
               "FROM blocks_fts JOIN blocks b ON b.id=blocks_fts.rowid "
               "WHERE blocks_fts MATCH ? ORDER BY s LIMIT ?")
        rows = []
        for expr in (strict, loose):
            try:
                rows = self.kb.execute(sql, (expr, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                break
        return [{**dict(r), "_route": "text"} for r in rows]

    # ── 重排打分 ────────────────────────────────────────────────
    def _rescore(self, q: Query, cand: dict, rrf_norm: float) -> tuple[float, dict]:
        """三个信号，各自有解释，不掺「语义相似度」这种说不清的项。

        · rrf_norm  —— 三路名次融合后的位置（谁被更多路认同）
        · struct    —— 是否被「点名」：点了文件 / 点了条号 / 两者都点了
        · art       —— 是不是条款层（条款可以直接引，段落不行）

        这里刻意**没有**「问题词覆盖率」这一项。上一版加过，效果更差：
        覆盖率天然偏向长块和常见词，把《大中型水利水电工程建设征地补偿和移民安置
        条例》顶到了《国有土地上房屋征收与补偿条例》前面——只因为它更长、更爱重复
        「补偿」两个字。BM25 的 IDF 加权已经在干这件事，而且干得更好。
        """
        routes = cand.get("_routes", [])
        if "entity" in routes and "article" in routes:
            struct = 1.0
        elif "article" in routes:
            struct = 0.75
        elif "entity" in routes:
            struct = 0.55
        else:
            struct = 0.0
        art = 1.0 if cand["kind"] == "art" else 0.0
        dead = self._dead.get(cand["doc_key"], 0.0)
        long_pen = 0.05 if len(cand["text"]) > 1600 else 0.0
        # 标题信号：问题里最长的那个连续片段，在标题里出现过吗
        # 用「最长公共子串 / 问题长度」而不是词覆盖率——覆盖率区分不开
        # 「森林公园总体规划审批」和「国土空间规划」，但连续片段能。
        tlcs = _lcs_len(q.raw, cand["title"]) / max(4, min(len(q.raw), 24))
        score = (0.46 * rrf_norm + 0.26 * struct + 0.06 * art
                 + 0.22 * min(1.0, tlcs) - dead - long_pen)
        why = {"routes": routes, "struct": round(struct, 2),
               "art": bool(art), "rrf": round(rrf_norm, 2),
               "title_lcs": round(tlcs, 2), "exp": cand.get("_exp", 0),
               "repealed": bool(dead)}
        return score, why

    def search(self, q: Query, k: int = 8, per_doc: int = 1,
               expand: list[str] | None = None) -> dict:
        """返回 top-k，并附带每一路的原始名次，供界面展示「怎么排出来的」。

        `expand` 是查询扩展词（由模型给的检索词组）。它走的是**独立的一组检索**，
        不是把词塞进原查询。原因：塞进去会让 AND 全废、OR 被稀释，等于把原问题
        改掉了；独立成组再融合，等于「多派几个人分头找，最后看谁被更多人认出」。
        扩展组的权重给 0.6，原问题的判断永远占主导。
        """
        t0 = time.time()
        r_ent = self.route_entity(q)
        # 条款路只在文件已锁定时启用——条号是文件内部的定位器
        ent_keys = {r["doc_key"] for r in r_ent}
        r_art = self.route_article(q, allow=ent_keys) if ent_keys else []
        r_txt = self.route_text(q)

        pool: dict[tuple, dict] = {}
        groups: list[tuple[str, float, list[dict]]] = [
            ("entity", 1.0, r_ent), ("article", 1.0, r_art), ("text", 1.0, r_txt)]
        for i, w in enumerate((expand or [])[:4]):
            groups.append((f"exp{i + 1}", 0.6, self.route_text_terms(grams(w), 30)))

        for tag, weight, rows in groups:
            for i, r in enumerate(rows, 1):
                cid = (r["doc_key"], r["seq"])
                item = pool.setdefault(cid, {**r, "_rrf": 0.0, "_routes": [],
                                             "_rank": {}, "_exp": 0})
                item["_rrf"] += weight / (60 + i)
                if tag.startswith("exp"):
                    item["_exp"] += 1
                elif tag not in item["_routes"]:
                    item["_routes"].append(tag)
                item["_rank"][tag] = i
        if not pool:
            return {"hits": [], "ms": 0, "catalog": self.search_catalog(q),
                    "diag": {"entity": 0, "article": 0, "text": 0, "pool": 0}}

        rrf_max = max(c["_rrf"] for c in pool.values()) or 1.0
        ranked = []
        for c in pool.values():
            s, why = self._rescore(q, c, c["_rrf"] / rrf_max)
            ranked.append((s, c, why))
        ranked.sort(key=lambda x: (-x[0], x[1]["seq"]))

        hits, seen = [], {}
        for s, c, why in ranked:
            n = seen.get(c["doc_key"], 0)
            if n >= per_doc:
                continue
            seen[c["doc_key"]] = n + 1
            meta = self.cat.execute(
                "SELECT doc_no,published,issuer,url,path FROM fulltext WHERE key=?",
                (c["doc_key"],)).fetchone()
            hits.append({
                "doc_key": c["doc_key"], "title": c["title"], "label": c["label"],
                "kind": c["kind"], "seq": c["seq"], "text": c["text"],
                "snippet": c["text"][:240].replace("\n", " "),
                "doc_no": (meta["doc_no"] if meta else "") or "",
                "published": (meta["published"] if meta else "") or "",
                "issuer": (meta["issuer"] if meta else "") or "",
                "url": (meta["url"] if meta else "") or "",
                "score": round(s, 3), "why": why,
                "rank": dict(c["_rank"]),
            })
            if len(hits) >= k:
                break
        return {"hits": hits, "ms": round((time.time() - t0) * 1000),
                "diag": {"entity": len(r_ent), "article": len(r_art),
                         "text": len(r_txt), "pool": len(pool)},
                "catalog": self.search_catalog(q, limit=6)}

    # ── 目录层回退：全文没有，但清单里有 ────────────────────────
    def search_catalog(self, q: Query, limit: int = 6) -> list[dict]:
        """全文库只有国家级 1.86 万份。问地方政策时，正文常常没有，但目录里有。

        这一步的意义不在于「多给几条结果」，而在于**回答「有没有」**：
        库里到底有没有这个方向的文件、在哪一级、什么时候发的。没有这一步，
        用户看到「无结果」会以为全国都没发过这份文件。

        不能拿 OR 串一堆 2-gram 去扫——「消防」「规划」这种词会把全部
        65,111 条都捞出来，首页全是县城中学的采购公告。判据是**命中词数**：
        命中越多越靠前，一个都不命中的直接不要。
        """
        terms = [t for t in q.terms][:8]
        where, args = [], []
        if q.places:
            where.append("(" + " OR ".join(["region LIKE ?"] * len(q.places[:3])) + ")")
            args += [f"%{p}%" for p in q.places[:3]]
        if terms:
            where.append("(" + " OR ".join(["title LIKE ?"] * len(terms)) + ")")
            args += [f"%{t}%" for t in terms]
        if not where:
            return []
        sql = ("SELECT title,region,admin_level,doc_no,published,url,source_name "
               "FROM items WHERE " + " AND ".join(where) +
               " ORDER BY published DESC LIMIT 400")
        scored = []
        for r in self.cat.execute(sql, args):
            t = r["title"] or ""
            n = sum(1 for x in terms if x in t)
            if n == 0:
                continue
            scored.append((n, r["published"] or "", r))
        scored.sort(key=lambda x: (-x[0], x[1]), reverse=False)
        out = []
        for n, _, r in scored[:limit]:
            out.append({"title": r["title"], "region": r["region"],
                        "level": r["admin_level"], "doc_no": r["doc_no"] or "",
                        "published": r["published"] or "", "url": r["url"] or "",
                        "source": r["source_name"] or "", "hit_terms": n,
                        "kind": "catalog_only"})
        return out

    # ── 基线：朴素 BM25（就是「以前的做法」）────────────────────
    def search_baseline(self, q: Query, k: int = 8) -> dict:
        """块级 top-k，**不按文件去重**。

        这才是 2023 年以来绝大多数 RAG 的默认形态：向量库或 BM25 取前 8 个块，
        塞进上下文。它的问题是块级的——同一份文件的不同段落会占掉好几个名额。
        问「历史建筑修缮」时，那部条例的第三十二、三十三、三十四、三十五条
        全在榜上，剩下的四五个名额给谁就全凭运气了。

        这里刻意不做去重，因为去重是个设计决策，不是默认行为；「过去的做法」
        之所以是过去，就因为它没有做过这些决策。
        """
        t0 = time.time()
        rows = self.route_text(q, limit=k * 4)[:k]
        out = []
        for r in rows:
            meta = self.cat.execute(
                "SELECT doc_no,published,issuer,url FROM fulltext WHERE key=?",
                (r["doc_key"],)).fetchone()
            out.append({
                "doc_key": r["doc_key"], "title": r["title"], "label": r["label"],
                "kind": r["kind"], "seq": r["seq"], "text": r["text"],
                "snippet": r["text"][:240].replace("\n", " "),
                "doc_no": (meta["doc_no"] if meta else "") or "",
                "published": (meta["published"] if meta else "") or "",
                "issuer": (meta["issuer"] if meta else "") or "",
                "url": (meta["url"] if meta else "") or "",
                "score": round(-r["s"], 2), "why": {},
            })
        return {"hits": out, "ms": round((time.time() - t0) * 1000)}


# ══════════════════════════════════════════════════════════════════
# 三、闸门 1：话题级
# ══════════════════════════════════════════════════════════════════

def gate1(q: Query, hits: list[dict]) -> dict:
    """判「库里有没有这个话题」。

    两条判据取其一：

    1. **实体落地**：问题点名的法规或地域，在召回结果里能对上。
       这是最硬的，但它只在「用户点名了东西」时才有——而真实问题常常不点名。
    2. **首条覆盖率**：排名第一的那一块，覆盖了问题里多少实词。
       没点名时用这条。阈值 0.35 是实测出来的：库里确实有相关内容时，
       首条覆盖普遍在 0.45 以上；而只撞上一两个常用词（「第五条」「防治」）
       的错召回，覆盖通常在 0.2 以下。

    两条都不成立，说明这个库答不了这个问题。这时候的正确行为是**不生成**，
    而不是让模型硬答——「库内未覆盖」是一个可以交付的答案，「编一个」不是。
    """
    if not hits:
        return {"pass": False, "reason": "检索无结果", "anchors": [], "cov": 0.0}

    # 硬判据：问题点了地域，召回里就必须有该地域的文件。
    # 「怀化市扬尘污染防治条例」在库内没有，但 BM25 会拿「污染防治」+「第五条」
    # 撞出《畜禽规模养殖污染防治条例》——词面像，地域完全不对。这一条能拦住。
    if q.places:
        got = [p for p in q.places
               if any(p in h["title"] for h in hits[:8])]
        if not got:
            return {"pass": False, "anchors": [], "cov": 0.0,
                    "reason": f"指定地域「{'、'.join(q.places[:2])}」在全库无匹配文件"}

    anchors = []
    for h in hits[:5]:
        if q.laws and any(_core_name(l) and _core_name(l) in _core_name(h["title"])
                          for l in q.laws):
            anchors.append({"type": "law", "value": h["title"], "hit": h["label"]})
        if q.places and any(p in h["title"] for p in q.places):
            anchors.append({"type": "place", "value": h["title"], "hit": h["label"]})

    top = hits[0]
    cov = len(q.terms & grams(f"{top['label']} {top['text'][:1200]}")) / max(1, len(q.terms))
    # 软判据：标题里有没有「问题里那个 4 字以上的说法」。
    # 只用词覆盖率会误杀——「改变用途」在原文里写作「改变使用性质」，
    # 词面一个都不重合，但标题里的「城市更新」「历史建筑」说明方向是对的。
    lcs_hit = [h for h in hits[:8] if _lcs_len(q.raw, h["title"]) >= 4]
    ok = bool(anchors) or bool(lcs_hit) or cov >= 0.5
    if anchors:
        reason = f"{len(anchors)} 个实体落地"
    elif lcs_hit:
        reason = f"标题吻合「{_lcs_str(q.raw, lcs_hit[0]['title'])}」等 {len(lcs_hit)} 份"
    elif cov >= 0.5:
        reason = f"首条覆盖问题实词 {cov*100:.0f}%"
    else:
        reason = f"无实体落地、标题无吻合，首条只覆盖 {cov*100:.0f}% 实词"
    return {"pass": ok, "anchors": anchors[:3], "cov": round(cov, 2), "reason": reason}


def _lcs_str(a: str, b: str) -> str:
    """最长公共子串本体（不只是长度），用于把「凭什么放行」写成人话。"""
    a, b = a[:60], b[:60]
    best, end, prev = 0, 0, [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], i
        prev = cur
    return a[end - best:end]


# ══════════════════════════════════════════════════════════════════
# 四、闸门 2：条文级五项检查（全部回原文核）
# ══════════════════════════════════════════════════════════════════

_SENT = re.compile(r"[^。；;！!？?\n]+")
_PUNCT = re.compile(r"[\s，。、；：？！“”‘’（）《》,.;:?!\"'()\[\]【】—…·*#\->]")


def trace_answer(answer: str, hits: list[dict], min_len: int = 10) -> dict:
    """逐句回查：答案里的每一句，能不能在召回原文里找到出处。

    **这一步不要求模型配合。** 上一版是按模型自己写的引注去核，模型不给引注就
    什么也核不了——而模型给不给引注，是它心情问题，不是我们能控制的。
    这里反过来做：把答案切成句子，每句拿 10 字的窗口去原文里搜。搜到了，
    这句就是有出处的；搜不到，这句就是模型自己长的。

    10 字是权衡后的值。14 字更保守，但中文 14 字连续相同已经接近「原文照抄」，
    而模型的正常写法是概括——实测 14 字下连「所有权人应当负责维护和修缮」这种
    几乎照抄的句子都会漏。10 字在中文里撞车概率极低，同时能容忍正常的概括。

    这个方向的额外好处：它能给一个**可比较的数字**——依据密度。
    过去那种做法经常通篇没有出处，但它也不会告诉你；现在这个数字直接摆在界面上。
    """
    blocks = [(h, _PUNCT.sub("", h["text"])) for h in hits]
    sents, supported, unsupported = [], [], []
    for m in _SENT.finditer(answer or ""):
        raw = m.group(0).strip(" *>-\t　")
        s = _PUNCT.sub("", raw)
        # 太短的句子（「依据如下：」）不参与判定
        if len(s) < min_len:
            continue
        sents.append(raw)
        found = None
        for h, body in blocks:
            for i in range(0, max(1, len(s) - min_len + 1), 2):
                probe = s[i:i + min_len]
                j = body.find(probe)
                if j < 0:
                    continue
                # 命中后向两边扩，拿到完整的那段原话
                a, b = i, i + min_len
                while a > 0 and body.find(s[a - 1:b]) >= 0:
                    a -= 1
                while b < len(s) and body.find(s[a:b + 1]) >= 0:
                    b += 1
                found = {"hit": h, "span": s[a:b], "len": b - a}
                break
            if found:
                break
        if found:
            supported.append({"sentence": raw, "title": found["hit"]["title"],
                              "label": found["hit"]["label"],
                              "doc_key": found["hit"]["doc_key"],
                              "span": found["span"], "len": found["len"]})
        else:
            unsupported.append({"sentence": raw})
    n = len(sents)
    return {"sentences": n, "supported": len(supported), "unsupported": unsupported,
            "ratio": round(len(supported) / n, 3) if n else 0.0,
            "detail": supported}


class Verifier:
    """C1–C5。每一项的判据都落在「原文」或「台账」上，不落在模型上。"""

    def __init__(self) -> None:
        self.cat = _ro(CAT_DB)
        self.kb = _ro(KB_DB)
        self.ledger = {r["key"]: r for r in
                       self.cat.execute("SELECT key,title,status,basis,evidence,by_doc,"
                                        "repealed_on,effect_on,valid_until,valid_raw "
                                        "FROM eff_ledger")}
        self.n_docs = self.kb.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def close(self) -> None:
        self.cat.close()
        self.kb.close()

    # C1 条号越界 —— 回原文搜，不再查条数表
    def c1(self, doc_key: str, article: int) -> dict:
        if not article:
            return {"name": "C1 条号越界", "state": "skip", "detail": "答案未引用条号"}
        cn = int2cn(article)
        row = self.kb.execute(
            "SELECT COUNT(*) FROM blocks WHERE doc_key=? AND kind='art' AND label LIKE ?",
            (doc_key, f"%第{cn}条%")).fetchone()
        if row[0]:
            return {"name": "C1 条号越界", "state": "pass",
                    "detail": f"原文含第{cn}条（条级索引命中 {row[0]} 处）"}
        # 条级索引没切到，再回全文逐字搜一次（防切块漏条）
        txt = self._text(doc_key)
        if re.search(rf"第{cn}条(?!之)", txt):
            return {"name": "C1 条号越界", "state": "pass",
                    "detail": f"条级切分未覆盖，但全文逐字搜到第{cn}条"}
        has_art = self.kb.execute(
            "SELECT COUNT(*) FROM blocks WHERE doc_key=? AND kind='art'",
            (doc_key,)).fetchone()[0]
        if has_art == 0:
            return {"name": "C1 条号越界", "state": "fail",
                    "detail": f"该文件全篇不分条（{len(txt):,} 字），不存在第{cn}条——判为编造"}
        return {"name": "C1 条号越界", "state": "fail",
                "detail": f"全文搜不到第{cn}条（该文件有 {has_art} 条条文，无此条）"}

    # C2 引文查不到
    def c2(self, doc_key: str, quote: str) -> dict:
        if not quote:
            return {"name": "C2 引文查不到", "state": "skip", "detail": "未给引文"}
        txt = re.sub(r"\s+", "", self._text(doc_key))
        q = re.sub(r"\s+", "", quote)
        if len(q) < 8:
            return {"name": "C2 引文查不到", "state": "skip",
                    "detail": "引文过短，不做比对（少于 8 字无法判定）"}
        if q in txt:
            return {"name": "C2 引文查不到", "state": "pass", "detail": "引文逐字命中原文"}
        # 允许标点差异：去标点后比对
        strip = lambda s: re.sub(r"[，。、；：？！“”‘’（）《》,.;:?!\"'()\[\]—…·\s]", "", s)
        if strip(q) in strip(txt):
            return {"name": "C2 引文查不到", "state": "pass",
                    "detail": "引文命中（仅标点差异）"}
        # 局部相似：找最长公共子串占比，给出「引文被改写」的证据
        best = _lcs_ratio(q, strip(txt))
        if best >= 0.7 or best * len(q) >= 30:
            return {"name": "C2 引文查不到", "state": "warn",
                    "detail": f"引文有改写：原文能对上 {best*100:.0f}%，不是逐字照抄"}
        return {"name": "C2 引文查不到", "state": "fail",
                "detail": f"原文中找不到这段引文（最长连续吻合 {best*100:.0f}%）"}

    # C3 时效
    def c3(self, doc_key: str) -> dict:
        r = self.ledger.get(doc_key)
        if not r:
            return {"name": "C3 时效违规", "state": "unknown",
                    "detail": "该文件不在全文库（目录层条目），时效需回源确认"}
        st = r["status"]
        if st == "repealed":
            by = f"，由《{r['by_doc']}》宣布废止" if r["by_doc"] else ""
            return {"name": "C3 时效违规", "state": "fail",
                    "detail": f"已失效（依据：{r['basis']}{by}）"}
        if st == "superseded":
            # 同名多版本：新版进了库，正文里未必写「旧版废止」（废止说明常在发布令里），
            # 但同名法规不可能同时有效。这一条判 fail，且必须把新版名字给出来——
            # 只说「你引错了」而不说「该引哪份」，等于把人卡在原地。
            # 新旧同名，不给日期就分不清说的是哪一份。
            by = f"《{r['by_doc']}》" if r["by_doc"] else "同名新版"
            on = f"（{r['repealed_on']}）" if r["repealed_on"] else ""
            return {"name": "C3 时效违规", "state": "fail",
                    "detail": f"这是旧版本，已被{by}{on}取代，引注应改用新版"}
        if st == "expired":
            # 到期失效不需要任何别的文件宣布。这一类比废止更隐蔽：用户搜「废止」
            # 永远搜不到，只能从原文末尾那句「有效期至 X」算出来。
            return {"name": "C3 时效违规", "state": "fail",
                    "detail": f"有效期已届满（原文载明有效期至 {r['valid_until']}）。"
                              f"届满失效无需另行发文，引注应改用接续文件"}
        if st == "pending":
            # 还没生效的文件本身是真的，引它不算编造，但当成现行依据就是错的。
            # 判 fail 会误伤「问的就是新规」的场景，所以判 warn 并要求标出施行日。
            return {"name": "C3 时效违规", "state": "warn",
                    "detail": f"尚未生效（原文载明自 {r['effect_on']} 起施行）。"
                              f"如作为现行依据引用，须注明施行日"}
        if st == "in_force":
            return {"name": "C3 时效违规", "state": "pass",
                    "detail": f"原文载明有效期至 {r['valid_until']}，未届满"}
        return {"name": "C3 时效违规", "state": "pass",
                "detail": f"未见废止记录，原文未载有效期（全库 {self.n_docs:,} 份正文比对）"}

    # C4 名称错配
    def c4(self, title_claimed: str, doc_key: str) -> dict:
        real = self.kb.execute("SELECT title FROM docs WHERE doc_key=?",
                               (doc_key,)).fetchone()[0]
        # 书名号有四种写法：《》、〈〉、[]、【】。模型在引一个「公告的标题里又套了
        # 一份文件名」时，内层会用 〈〉（「发布〈XX指南〉等2项行业标准的公告」），
        # 只剥《》会让这种完全正确的引注被判「名称对不上」。
        a = re.sub(r"[《》（）()〈〉\[\]【】\s]", "", title_claimed or "")
        b = re.sub(r"[《》（）()〈〉\[\]【】\s]", "", real or "")
        if not a:
            return {"name": "C4 名称错配", "state": "skip", "detail": "未给文件名"}
        if a == b or a in b or b in a:
            return {"name": "C4 名称错配", "state": "pass", "detail": f"名称一致：{real}"}
        # 规划领域最多的一种错：库内同名的地方版被当成中央版（或反过来）
        core = re.sub(r"^.*?省|^.*?市|^中华人民共和国", "", a)
        if core and (core in b or b in core):
            return {"name": "C4 名称错配", "state": "warn",
                    "detail": f"名称近似但主体不同：你写《{title_claimed}》，库里是《{real}》"}
        return {"name": "C4 名称错配", "state": "fail",
                "detail": f"名称对不上：引作《{title_claimed}》，实际《{real}》"}

    # C5 所问法规划界
    def c5(self, q: Query, doc_key: str) -> dict:
        if not q.laws:
            return {"name": "C5 所问法规范围", "state": "skip", "detail": "问题未点名法规"}
        title = self.kb.execute("SELECT title FROM docs WHERE doc_key=?",
                                (doc_key,)).fetchone()[0] or ""
        for law in q.laws:
            n = law.replace("《", "").replace("》", "")
            if n in title or title in n:
                return {"name": "C5 所问法规范围", "state": "pass", "detail": f"引用的就是所问的《{title}》"}
        return {"name": "C5 所问法规范围", "state": "fail",
                "detail": f"所问是《{q.laws[0]}》，引用的是《{title}》——不是同一部"}

    def _text(self, doc_key: str) -> str:
        r = self.cat.execute("SELECT path FROM fulltext WHERE key=?",
                             (doc_key,)).fetchone()
        if not r or not r["path"]:
            return ""
        p = Path(r["path"])
        return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


def _lcs_ratio(a: str, b: str) -> float:
    """最长公共子串占 a 的比例。用来区分「改写」和「完全编造」。"""
    import difflib
    if not a:
        return 0.0
    a = a[:150]
    b = b[:60000]
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b))
    return m.size / len(a)


# ══════════════════════════════════════════════════════════════════
# 五、生成
# ══════════════════════════════════════════════════════════════════

PROVIDERS = [("DASHSCOPE", "qwen-plus"), ("DEEPSEEK", "deepseek-chat"),
             ("ARK", "doubao-seed-1-6-250615")]


def load_env() -> dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                env[k.strip()] = v.strip()
    for k in ("DASHSCOPE", "DEEPSEEK", "ARK"):
        if os.environ.get(k + "_API_KEY"):
            env[k + "_API_KEY"] = os.environ[k + "_API_KEY"]
        if os.environ.get(k + "_BASE_URL"):
            env[k + "_BASE_URL"] = os.environ[k + "_BASE_URL"]
    return env


def pick_providers(env: dict) -> list[dict]:
    """所有可用的模型，按优先级排。

    为什么返回列表：`.env` 里配了五家 key，但旧版 `pick_provider` 只取第一家。
    2026-10-24 实测第一次撞上「HTTP 403：Free quota」——免费额度用完，
    整条链路直接降级成规则兜底，拆解从 8 条指令退化成 6 条泛化指令，
    地名抽取也出错，本地三层全空。配了五家却只用一家，等于只配了一家。
    """
    out = []
    for pfx, model in PROVIDERS:
        if env.get(pfx + "_API_KEY") and env.get(pfx + "_BASE_URL"):
            out.append({"provider": pfx.lower(), "api_key": env[pfx + "_API_KEY"],
                        "base": env[pfx + "_BASE_URL"].rstrip("/"), "model": model})
    if not out:
        raise SystemExit("没有可用的模型 key：需要 DASHSCOPE/DEEPSEEK/ARK 之一")
    return out


def pick_provider(env: dict) -> dict:
    return pick_providers(env)[0]


SYSTEM = """你是城乡规划与政策研究领域的助手。只能用提供的资料回答。

【引注格式 · 硬性要求，没有例外】
每一句结论的末尾，必须跟一个方括号引注，长这样：

    ……负责历史建筑的维护和修缮[《历史文化名城名镇名村保护条例》第三十三条]。

规则：
1. 引注里写文件全名和条号，格式固定：[《文件名》第X条]
   文件不分条的，写 [《文件名》]
   不要写 [1]、[文件2]、[资料三]——读者看不到编号对应哪份文件。
2. 条号必须真的存在于那份文件里。不确定是哪一条，就只写文件名、不写条号。
3. 引用的原话要加引号，且必须逐字照抄，不得改写。
4. 资料里没有的内容，直接说「库内资料未覆盖」，不要用常识补。
5. 资料彼此矛盾或有时效疑问，先指出，再作答。
6. 只答所问，不扩展到没问的领域。

【反例 · 这样写是错的】
    历史建筑可以由所有权人改变用途，但需报批。（没有引注）
    历史建筑可以由所有权人改变用途，但需报批[1]。（用了编号）
    历史建筑可以由所有权人改变用途，但需报批[《城乡规划法》第三十六条]。（引了不在资料里的法规）"""


def _call_one(pv: dict, system: str, user: str, timeout: int,
              max_tokens: int) -> str:
    body = json.dumps({"model": pv["model"],
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}],
                       "temperature": 0.1, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(pv["base"] + "/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + pv["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"模型接口 HTTP {e.code}：{e.read().decode('utf-8','replace')[:200]}")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"模型接口异常：{type(e).__name__}: {e}")


def call_llm(pv, system: str, user: str, timeout: int = 150,
             max_tokens: int = 1600) -> str:
    """`pv` 可以是单个 provider，也可以是 pick_providers 的列表。

    传列表时逐个试。第一个 403 就换第二个，而不是把它抛给上层——
    上层拿到的异常会被当成「模型不可用」，直接降级成规则兜底，
    一次额度耗尽会让整份报告的拆解质量跌一个档。
    """
    cands = pv if isinstance(pv, list) else [pv]
    errs = []
    for p in cands:
        try:
            return _call_one(p, system, user, timeout, max_tokens)
        except RuntimeError as e:
            errs.append(f"{p['provider']}={str(e)[:100]}")
    raise RuntimeError("所有模型都不可用：" + " ｜ ".join(errs))


EXPAND_SYS = """你把用户的政策问题改写成检索用的关键词组。只输出 JSON，不要解释。

规则：
- 输出 3 组关键词，每组 2—8 个字，必须是城乡规划/土地/住建领域的规范表述
- 第 1 组：用户原话里最核心的那个名词（不要加动词）
- 第 2、3 组：同义表述、上位词、或人们实际写文件时用的说法
  （例：老旧小区改造 → 城镇老旧小区改造 / 配套设施建设；
   征地补偿标准 → 征收补偿 / 区片综合地价）
- 不要输出整句，不要输出「怎么」「有哪些」这类问法

格式：{"keywords": ["...", "...", "..."]}"""


def expand_query(q: Query, pv: dict) -> dict:
    """查询扩展。一次约 0.6 秒，换来「同一个意思的三种写法」都能召回。

    为什么要这一步：用户写的是「老旧小区改造资金」，文件里写的是「城镇老旧小区
    改造配套设施建设」——2-gram 能撞上「老旧」「小区」「改造」，撞不上「配套」。
    而「配套」恰恰是资金从哪来的关键。词表补不了这个，只有改写能。
    """
    t0 = time.time()
    try:
        raw = call_llm(pv, EXPAND_SYS, q.raw, timeout=40, max_tokens=200)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        kws = [str(x).strip() for x in data.get("keywords", []) if str(x).strip()][:3]
        return {"keywords": kws, "ms": round((time.time() - t0) * 1000), "ok": bool(kws)}
    except Exception as e:  # noqa: BLE001
        return {"keywords": [], "ms": round((time.time() - t0) * 1000),
                "ok": False, "error": f"{type(e).__name__}"}


def build_context(hits: list[dict], budget: int = 9000) -> str:
    """喂给模型的资料块。

    头部**不用 [1] [2] 这种编号**。用编号会立刻教坏模型：它会在答案里写
    「[文件2 第4条]」，因为那是最省力的写法。而「文件2」在交付物里没有意义——
    读者手里只有这份答案，没有我们当时的上下文。所以头部直接就是引注该有的样子：
    《文件名》 第X条，模型照抄就行。
    """
    parts, used = [], 0
    for h in hits:
        label = h["label"] or "（本文件不分条）"
        head = (f"《{h['title']}》 {label}"
                f"（{h['doc_no'] or '无文号'}｜{h['published'] or '无日期'}）")
        blk = head + "\n" + h["text"][:1500]
        if used + len(blk) > budget:
            break
        parts.append(blk)
        used += len(blk)
    return "\n\n— — —\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════
# 六、从答案里抽引注
# ══════════════════════════════════════════════════════════════════

# 引注允许三种写法，都是模型实际会写的：
#   【《条例》第二十二条】  【《条例》】  【《市政府关于印发X办法的通知》（宁政规字〔2023〕5号）】
# 第三种最容易漏——书名号后面跟了文号。旧正则漏掉它，8 条引注只解析出 4 条，
# 界面显示「核验 4 处」实际有 8 处，少核的那一半正好是最需要核的软依据。
_CITE = re.compile(
    r"[\[【]\s*《?([^\]】》第。，；：、\n]{4,60}?)》?"
    r"\s*(?:[（(][^）)]{0,40}[）)])?\s*"
    r"(?:第([〇零一二三四五六七八九十百千\d]{1,6})条)?\s*[\]】]")
_DOCNO = re.compile(
    r"([\u4e00-\u9fa5]{1,12}[〔\[［(]\s*\d{4}\s*[〕\]］)]\s*\d{1,5}\s*号)")
_QUOTE = re.compile(r"[“\"「]([^”\"」]{8,200})[”\"」]")


def _norm_docno(s: str) -> str:
    return re.sub(r"[\[\]〔〕［］()（）\s]", "", s or "")


def parse_docnos(answer: str) -> list[dict]:
    """答案里提到的文件文号，逐个回库查。

    为什么要有这一项：很多回答不给条号，只给「（建科〔2021〕63号）」。
    只按条号核，这类答案会被判成「不可溯源」；实际上文号是更强的身份标识——
    它唯一对应一份文件。查得到，这条依据就成立；查不到，就是编的。
    """
    seen, out = set(), []
    for m in _DOCNO.finditer(answer):
        raw = m.group(1)
        k = _norm_docno(raw)
        if k in seen:
            continue
        seen.add(k)
        out.append({"raw": raw, "norm": k})
    return out


def check_docnos(items: list[dict], index: dict[str, dict]) -> list[dict]:
    for it in items:
        hit = index.get(it["norm"])
        it["matched"] = bool(hit)
        it["title"] = hit["title"] if hit else ""
        it["url"] = hit["url"] if hit else ""
    return items


_DOCNO_IDX: dict[str, dict] | None = None


def docno_index() -> dict[str, dict]:
    """文号 → 文件。全库 1.86 万份建一次，之后进程内复用。"""
    global _DOCNO_IDX
    if _DOCNO_IDX is None:
        idx: dict[str, dict] = {}
        c = _ro(CAT_DB)
        try:
            for r in c.execute(
                    "SELECT doc_no,title,url FROM fulltext "
                    "WHERE doc_no IS NOT NULL AND doc_no<>''"):
                idx.setdefault(_norm_docno(r["doc_no"]),
                               {"title": r["title"], "url": r["url"]})
        finally:
            c.close()
        _DOCNO_IDX = idx
    return _DOCNO_IDX


_TITLE_JUNK = re.compile(r"[《》〈〉（）()\[\]【】\s　]")
# 这些「引注」是模型在说「库里没有」，不是引用，不能拿去核对
_NOT_A_CITE = ("未覆盖", "无相关", "未提及", "无资料", "不存在", "无此", "未找到")


def parse_cites(answer: str, hits: list[dict]) -> list[dict]:
    """把答案里的引注解析成可核验的结构，并绑定到召回块的文件。

    **引文必须按句配对，不能按出现顺序配对。** 上一版是第 i 个引注配第 i 个
    引号内容，模型一句话里有多个引号时就整体错位——于是明明逐字照抄的句子被
    判成「原文中找不到」，而这恰恰是最不该误判的一档。
    """
    by_title = {}
    for h in hits:
        by_title.setdefault(_TITLE_JUNK.sub("", h["title"]), h)
    out = []
    for m in _CITE.finditer(answer):
        claimed = m.group(1).strip()
        if any(w in claimed for w in _NOT_A_CITE):
            continue
        art = cn2int(m.group(2)) if m.group(2) else 0
        key = _TITLE_JUNK.sub("", claimed)
        hit = by_title.get(key)
        if not hit:
            # 模型常把副标题、发文机关顺序写得不完全一样，放宽一档再找
            for t, h in by_title.items():
                if key and (key in t or t in key) and abs(len(key) - len(t)) <= 30:
                    hit = h
                    break
        out.append({"claimed": claimed, "article": art,
                    "doc_key": hit["doc_key"] if hit else "",
                    "title": hit["title"] if hit else claimed,
                    "label": hit["label"] if hit else "",
                    "matched": bool(hit), "pos": m.start()})

    # 每句里的引号内容，归给这句里的引注（不跨句）
    #
    # ⚠️ 这里踩过一个坑，值得写清楚：**不能按「句号/分号/换行」切句再找引号。**
    #
    # 引文本身几乎一定含句号——「经批准的控制性详细规划是老城高度管理的依据，
    # 应当严格执行。控制性详细规划确定的建筑高度等未经法定程序不得修改。」
    # 按句号切，切点落在引文中间，于是「引号开头」落在前一句、「引号结尾」落在
    # 后一句，两边都凑不成对，quote 一律为空。
    #
    # 后果很隐蔽：模型明明逐字摘了原文，C2 却全部标成「未给引文」跳过。
    # 实测 14 条引注只配上 2 条，另外 12 条被当成模型没摘引文。
    #
    # 改成：先把全文所有成对引号的位置一次性找出来，再按「引注」给它们分区间——
    # 每条引注只认「上一条引注之后、本条引注之前」的引号，取最近的一个。
    # 这样既不依赖分句，也不会把上一条的引文配给下一条。
    spans = [(m.start(), m.end(), m.group(1)) for m in _QUOTE.finditer(answer)]
    prev = -1
    for c in out:
        cand = [s for s in spans
                if s[0] >= prev and s[1] <= c["pos"] and c["pos"] - s[1] <= 400]
        c["quote"] = cand[-1][2] if cand else ""
        prev = c["pos"]
    return out


def verify_answer(q: Query, answer: str, hits: list[dict]) -> dict:
    v = Verifier()
    try:
        cites = parse_cites(answer, hits)
        docnos = check_docnos(parse_docnos(answer), docno_index())
        trace = trace_answer(answer, hits)
        checks: list[dict] = []
        per_cite: list[dict] = []
        for c in cites:
            if not c["doc_key"]:
                per_cite.append({**c, "verdict": "fail",
                                 "detail": "引注指向的文件不在本次召回里，无法核对"})
                continue
            r = [v.c1(c["doc_key"], c["article"]),
                 v.c2(c["doc_key"], c.get("quote", "")),
                 v.c3(c["doc_key"]),
                 v.c4(c["claimed"], c["doc_key"]),
                 v.c5(q, c["doc_key"])]
            checks.extend(r)
            bad = [x for x in r if x["state"] == "fail"]
            warn = [x for x in r if x["state"] == "warn"]
            per_cite.append({**c, "verdict": "fail" if bad else ("warn" if warn else "pass"),
                             "detail": (bad + warn)[0]["detail"] if (bad or warn) else "五项通过",
                             "checks": r})
        n_fail = sum(1 for c in per_cite if c["verdict"] == "fail")
        n_warn = sum(1 for c in per_cite if c["verdict"] == "warn")
        bad_docno = [d for d in docnos if not d["matched"]]
        density = trace["ratio"]
        if not per_cite:
            # 「没有引注」和「引错了」是两件事，但对使用者来说后果一样：
            # 都无法判断这句话对不对。所以它必须是拒绝交付的一档，
            # 不能因为「看起来没出错」就放过去。
            #
            # 但它也不能一刀切：模型没写引注，不代表每句都没出处。
            # 逐句回查给出的「依据密度」是这里唯一能用的客观量。
            if bad_docno:
                strength = "拒答"
                why = f"{len(bad_docno)}/{len(docnos)} 个文号在库里查不到——{bad_docno[0]['raw']}"
            elif density >= 0.6:
                strength = "部分作答"
                why = (f"没有条号引注，但逐句回查原文 {trace['supported']}/{trace['sentences']} "
                       f"句有出处（{density*100:.0f}%）")
            elif docnos:
                strength = "部分作答"
                why = (f"{len(docnos)} 个文号核对通过，但正文只有 "
                       f"{density*100:.0f}% 的句子能在原文里找到出处")
            else:
                strength = "不可溯源"
                why = (f"没有引注，逐句回查也只有 {trace['supported']}/{trace['sentences']} "
                       f"句能找到出处（{density*100:.0f}%）")
        elif n_fail:
            # 引注没过，但逐句回查说大部分句子在原文里有出处——说明错的是
            # 引文写法（改写、配错条号），不是内容本身编造。这两件事的处置
            # 方式不一样：前者要人改引注，后者要人重写答案。不能一律拒答。
            if trace["ratio"] >= 0.6:
                strength = "部分作答"
                why = (f"{n_fail}/{len(per_cite)} 条引注不通过，"
                       f"但逐句回查 {trace['supported']}/{trace['sentences']} 句有出处"
                       f"（{trace['ratio']*100:.0f}%）——引注要改，内容大致可采")
            else:
                strength = "拒答"
                why = (f"{n_fail}/{len(per_cite)} 条依据不通过，且只有 "
                       f"{trace['ratio']*100:.0f}% 的句子能在原文里找到出处")
        elif n_warn:
            strength, why = "部分作答", f"{n_warn}/{len(per_cite)} 条依据需人工确认"
        else:
            strength, why = "可作答", f"{len(per_cite)} 条依据全部通过五项检查"
        return {"cites": per_cite, "checks": checks, "docnos": docnos,
                "trace": trace, "strength": strength, "reason": why,
                "n_fail": n_fail, "n_warn": n_warn}
    finally:
        v.close()
