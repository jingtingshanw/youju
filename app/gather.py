#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二步：按指令取证。

## 这里改了什么

旧版的检索是 `search(question, top_k=8)`——**无论问什么都只给 8 条**。
这个设定在「问一个条文」时是对的，在「摸清一个项目的政策环境」时是错的：
南京这一件事，本地的法规、规划、政策，省里的条例和行动方案，国家的条例和
部委文件，加上外地同类街区的做法，二十多份是常态。给 8 条，用户永远不知道
剩下的是什么。

所以这里不设数量，改设**证据强度**：

    strong   标题命中该指令的核心词（queries[0]）。「找的就是它」
    related  标题命中该指令的其它词。「顺带沾边，但确实同主题」
    —— 只命中泛词（办法/通知/规定/规划）而不含任何主题词的，直接丢

数量不设上限，但每条指令留一个天花板（默认 40），超出的不删，标成
「还有 N 条」，可展开。**看不见的结果比没有结果更坏。**

## 关键发现：国家层级必须走全文库

`items` 表 65,xxx 条里，`admin_level='国家'` 的只有 330 条——因为
国务院政策文件库当时只把一部分清单写进了 items。而**18,608 份正文全在
`kb.db`，有 1.7 万份是国家级的**。

所以两条通道分工，不是设计选择，是数据现状：

    本地 / 省级   → items（有元信息，多数没正文）
    国家          → kb.db fulltext（有正文，能逐条引）

拿「国家」去 items 查会得到「只找到 1 条」的假象；拿「南京市」去 kb.db 查
会什么也查不到。**搞错通道，结果是空的，但界面看起来像「库里没有」。**

## 关键词为什么要切

模型给的关键词经常是完整文件名：「历史文化名城名镇名村条例」「城市更新促进
条例」。但标题里不会这么写——真实标题是「历史文化名城名镇名村保护条例」
（多两个字）或「江苏省历史文化街区」（少了条例）。LIKE 是子串匹配，
差一个字就是 0 条。

所以对每个关键词做**领域词表的最长匹配切分**：
「历史文化名城名镇名村条例」→ 历史文化名城 + 名镇名村
「城市更新促进条例」→ 城市更新
切出来的词与原词一起进检索。这一步把「省级法规 0 条」变成了有结果。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAT_DB = ROOT / "data" / "catalog.db"
KB_DB = ROOT / "data" / "kb.db"

# 几乎每份文件标题都有的前缀，算覆盖率时要剥掉，否则「关于…的通知」占满分母
_STOP_TITLE = re.compile(
    r"^(关于|印发|转发|批转|公布|市政府|省政府|国务院|办公厅|人民政府|办公室)+")

# 这些不是文件本身，是对文件的转述。混进清单里会让人以为「找到了依据」，
# 点开才发现是张图或一场发布会。用户要的是能引用、能报批的那份。
_NOT_DOC = re.compile(r"一图读懂|政策解读|解读|新闻发布会|图解|问答|访谈|"
                      r"评论|社论|答记者问|在线访谈|意见征集结果")
# 这些词几乎任何一份文件标题里都有，命中它说明不了「这份文件跟本题有关」。
# 「试点」「示范」「方案」也在这里：南京市碳达峰试点方案命中「试点」，
# 但它跟历史文化街区一点关系没有——这类假阳性要靠词表挡，不靠分数阈值。
_GENERIC = {"办法", "规定", "通知", "意见", "决定", "批复", "细则", "规划",
            "实施", "若干", "有关", "工作", "管理", "文件", "政策", "方案",
            "条例", "公告", "通告", "解读", "试点", "示范", "建设", "发展",
            "印发", "公布", "政府", "部门"}

# 领域词表：城乡规划 / 城建 / 文保里「会出现在文件名里」的规范表述。
# 按长度降序做最长匹配，所以「历史文化名城」优先于「名城」。
DOMAIN = [
    "历史文化名城名镇名村", "历史文化名城", "历史文化名镇", "历史文化名村",
    "历史文化街区", "历史建筑", "历史地段", "历史风貌区", "风貌区", "名镇名村",
    "传统村落", "古村落", "文物保护单位", "文物保护", "非物质文化遗产", "文保单位",
    "城市更新专项规划", "城市更新", "有机更新", "微更新", "旧城改造", "城中村",
    "棚户区", "老旧小区", "危旧房", "危房改造", "存量用地", "低效用地",
    "土地储备", "土地供应", "划拨用地", "征收补偿", "房屋征收", "区片综合地价",
    "国土空间规划", "控制性详细规划", "详细规划", "总体规划", "专项规划",
    "村庄规划", "城市设计", "保护规划", "名城保护", "城市体检",
    "防止大拆大建", "保护传承", "留改拆", "拆除重建", "规划许可",
    "产业用地", "用地保障", "容积率", "建筑高度", "建筑风貌",
    "市政基础设施", "专项资金", "财政支持", "投融资", "试点", "示范",
]
_DOMAIN_SORTED = sorted(set(DOMAIN), key=len, reverse=True)
# 长词里含着的短词：整体撞不上标题时，用短词兜底
_SUB_CACHE = {w: tuple(d for d in _DOMAIN_SORTED
                       if d != w and len(d) >= 4 and d in w)
              for w in _DOMAIN_SORTED}


def segment(q: str) -> list[str]:
    """最长匹配切分。切不出来就返回空，调用方保留原词。

    匹配到长词之后还要把长词里含着的短词一起吐出来：「历史文化名城名镇名村」
    整体在标题里几乎不出现，但「历史文化名城」出现 21 次。只留最长匹配，
    会让一整个方向落成 0 条——这是实测踩到的，省级法规方向就是 zeros。
    """
    out, i = [], 0
    while i < len(q):
        for w in _DOMAIN_SORTED:
            if q.startswith(w, i):
                if w not in out:
                    out.append(w)
                for sub in _SUB_CACHE.get(w, ()):
                    if sub not in out:
                        out.append(sub)
                i += len(w)
                break
        else:
            i += 1
    return out


def expand_queries(qs: list[str], cap: int = 12) -> list[str]:
    out: list[str] = []
    for q in qs:
        q = (q or "").strip()
        if len(q) >= 2 and q not in out:
            out.append(q)
    for q in list(out):
        for w in segment(q):
            if w not in out:
                out.append(w)
    return out[:cap]


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def norm_title(t: str) -> str:
    t = re.sub(r"[《》〈〉\"'“”‘’\s　]", "", t or "")
    t = re.sub(r"[（(][^）)]{0,40}[）)]", "", t)
    return t


_FORWARD = re.compile(r"转发|印发|批转|发布|公布")
_BOOK = re.compile(r"《([^》]{4,80})》")


def _subject_title(title: str) -> str:
    """转发/印发类通知，实质文件在书名号里。

    「南京市政府办公厅关于转发市规划局《南京市控制性详细规划执行与调整规定》的通知」
    实质是那份《规定》，通知只是送达形式。按外层标题判形态，它会被判成「通知」
    或被「规划」二字抢走，然后从法规方向被形态过滤掉——而它恰恰是该方向唯一
    真正管用的文件。只影响形态判定，不改显示标题。
    """
    t = title or ""
    m = _BOOK.search(t)
    if m and _FORWARD.search(t):
        return m.group(1)
    return t


def shape_of(title: str) -> str:
    """文件形态。用户判断「这条是硬依据还是软要求」主要看这个。"""
    t = _subject_title(title)
    # 「法」必须放最前面且只在结尾匹配：它是最硬的一档，但「办法」「做法」
    # 里都有这个字，用 `"法" in t` 会把所有办法判成法律。
    # 漏了这一档的直接后果：形态过滤把《中华人民共和国文物保护法》
    # 《中华人民共和国城乡规划法》判成「其他」，从国家法规方向全删掉，
    # 该方向于是只剩《历史文化名城名镇名村保护条例》一条。
    # 后顾断言挡掉「办法/做法/方法」：它们的末字也是「法」，但只要忽略这一层，
    # 《南京市城市更新办法》就会被判成「法律」——它其实是一份市政府规章。
    if re.search(r"(法典|(?<![办做方想用])法)$", t) or "文物保护法" in t:
        return "法律"
    if "条例" in t:
        return "条例"
    if re.search(r"(政府令|令第|规章)", t):
        return "规章"
    if "办法" in t:
        return "办法"
    # 规定/规则/细则必须排在「规划」之前。
    # 《南京市控制性详细规划执行与调整规定》两个词都带，先判规划的话它会被
    # 归成规划成果，从 reg_local 的形态白名单里被删掉——于是「控规」这个方向
    # 返回的全是《南京历史文化名城保护条例》《南朝陵墓石刻保护条例》
    # 《夫子庙秦淮风光带条例》，一份控规文件都没有。规定是规章的正式名称之一
    # （规章可用规定/办法/规则/实施细则），归「规定」而不是「规划」。
    if re.search(r"(规定|规则|细则)", t):
        return "规定"
    if "名录" in t:
        return "名录"
    if "保护规划" in t or "规划" in t:
        return "规划"
    if "批复" in t or "同意" in t:
        return "批复"
    if "意见" in t:
        return "意见"
    if "方案" in t:
        return "方案"
    if "通知" in t:
        return "通知"
    return "其他"


class BodyIndex:
    """标题 → 全文 key。判「这条能不能直接引原文」。"""

    def __init__(self) -> None:
        self.by_title: dict[str, str] = {}
        self.by_key: dict[str, dict] = {}
        # 台账状态一并带进来：检出这一步就要知道这份文件是不是已经失效。
        # 只等核验层（C3）判是不够的——那时报告已经写完，模型已经把它当依据了。
        # 提前给模型一句话，比事后告诉他「你引错了」便宜得多。
        self.eff: dict[str, dict] = {}
        try:
            c = _ro(CAT_DB)
            for r in c.execute("SELECT key,title,url,doc_no,published,chars "
                               "FROM fulltext WHERE status='ok'"):
                self.by_title.setdefault(norm_title(r["title"]), r["key"])
                self.by_key[r["key"]] = dict(r)
            for r in c.execute("SELECT key,status,valid_until,effect_on,repealed_on "
                               "FROM eff_ledger"):
                self.eff[r["key"]] = dict(r)
            c.close()
        except sqlite3.Error:
            pass

    def key_of(self, title: str) -> str:
        n = norm_title(title)
        k = self.by_title.get(n)
        if k:
            return k
        m = re.search(r"《([^》]{4,60})》", title or "")
        if m:
            k = self.by_title.get(norm_title(m.group(1)))
            if k:
                return k
        # 模糊兜底必须有门槛。kb.db 里有一份标题就叫「通知」的脏记录，
        # 不加长度限制时，任何以「…的通知」结尾的目录标题都会命中它，
        # 于是「南京市城市更新办法」被判成「有正文」，核验时又报
        # 「实际是《通知》」——一个脏标题能让整份报告的核验结论失真。
        if len(n) >= 10:
            for t, key in self.by_title.items():
                if len(t) < 10:
                    continue
                if n in t or t in n:
                    short, long_ = (n, t) if len(n) <= len(t) else (t, n)
                    if len(short) / len(long_) < 0.55:
                        continue
                    # 覆盖率够还不够。《中华人民共和国文物保护法》是
                    # 《中华人民共和国文物保护法实施条例》的子串，覆盖率
                    # 12/20 = 0.6，能过线——但那是一部法律和它的下位法，
                    # 条文完全不同。拿实施条例的正文去核文物保护法的引注，
                    # 会把完全正确的引用判成「条号不存在」。
                    # 多出来的部分只要含形态词，就不是同一份文件。
                    rest = long_.replace(short, "", 1)
                    if re.search(r"(条例|办法|规定|规划|法典|细则)", rest):
                        continue
                    return key
        return ""


# 指令类型 → 走哪条通道。这不是可选项，是数据分布决定的。
_NATIONAL = {"reg_nation", "policy_nation"}


def _geo_clause(kind: str, geo: str) -> tuple[str, list]:
    """地域过滤，按指令类型严格化。

    旧版把 `OR admin_level='国家'` 加在所有地域条件后面，理由是「国家级文件
    也适用于地方」。这个理由在问「有没有依据」时成立，在问「南京本地有什么
    规定」时是错的——结果里混进了国务院文件，用户会以为那是南京的规定。
    """
    if kind in _NATIONAL:
        return "", []                       # 走全文库，通道本身就是国家级
    if kind in ("case", "standard") or geo in ("", "不限", "全国"):
        return "", []
    if geo == "国家":
        return "", []
    core = re.sub(r"(省|市|自治区|自治州|地区|盟|县|区|旗)$", "", geo)
    if not core:
        return "", []
    return ("region LIKE ?", [f"%{core}%"])


def _case_bonus(region: str, level: str, city: str, province: str) -> tuple[float, str]:
    """同类案例的地域可比性。

    规划师找案例是为了「照着做」，不是「看看别人也有」——同城的保护规划可以直接
    对照深度要求、审批流程、图件比例；外省一个县的管理办法对南京几乎是零参考。

    纯文本相关度排不出这个差别。「芷江侗族自治县历史文化街区、历史地段保护管理
    办法」标题里同时命中「历史文化街区」和「历史地段」两个领域词，得分 0.85，
    排第一；《南京总统府历史文化街区保护规划》只命中一个，0.63，排第五。
    对使用者来说第一条是噪音，后一条才是他要的。

    县级降权而不是删除：平遥、丽江都是县级，好案例确实可能在县级里，只是平均
    可比性最低，该排在后面等用户自己翻到。
    """
    r = region or ""
    core_city = re.sub(r"(省|市|自治区|自治州|地区|盟|县|区|旗)$", "", city or "")
    core_prov = re.sub(r"(省|市|自治区|自治州|地区|盟|县|区|旗)$", "", province or "")
    b, why = 0.0, ""
    if core_city and core_city in r:
        b, why = 0.14, "同城"
    elif core_prov and core_prov in r:
        b, why = 0.06, "同省"
    else:
        # 外地市级案例要能和同城竞争。用户问「指标能不能突破」，真正想看到的是
        # 「哪个城市突破过、走的什么程序」。同城有先例当然最好，但不能因此把
        # 外地已经落地的做法全压下去。
        b, why = 0.08, "外地"
    if level == "县级":
        # -0.20 而不是 -0.12：芷江那份「办法」同时命中两个领域词，基础分 0.85，
        # 只扣 0.12 仍然排在《南京总统府历史文化街区保护规划》前面。
        b -= 0.20
        why = f"{why}·县级" if why else "县级"
    return b, why


# 指令类型 → 允许的文件形态。规划师的分类是硬的：条例有法律约束力，
# 规划是法定成果，政策是行政安排，三者不能混。
#
# 不加这层会怎样：实测「本地法规」方向返回了《南京总统府历史文化街区保护规划
# 批后公布》——因为它命中了「历史文化街区保护」这个关键词。用户打开一看，
# 「法规」里躺着规划，会怀疑整个分类是不是都在瞎标。
#
# 只约束 reg_* 和 plan_local 两类：这两类的边界最清楚。policy_* 不设限，
# 因为「意见/方案/通知/函」的命名在实际文件里非常随意，硬卡会漏掉真东西。
_KIND_SHAPE = {
    "reg_local": {"法律", "条例", "规章", "办法", "规定"},
    "reg_prov": {"法律", "条例", "规章", "办法", "规定"},
    "reg_nation": {"法律", "条例", "规章", "办法", "规定"},
    # 批复给规划用：保护规划的批后公布常以「关于同意…的批复」形式发文。
    "plan_local": {"规划", "批复"},
}


# 「本市其它法规」兜底用的主题词。它不参与打分，只用来从本市全部条例/办法里
# 挑出可能相关的。用词必须**指向历史文化领域本身**，不能用「保护」「文化」
# 「建筑」这种谁都能带的词——实测「保护」放进《南京市水资源保护条例》、
# 「文化」放进《江苏省文化市场综合行政执法管理办法》、「建筑」放进
# 《南京市建筑垃圾资源化利用管理办法》。兜底一旦松，就从补召回变成制造噪音。
_THEME_CATCH = ["历史", "遗产", "文物", "名城", "街区", "历史建筑", "风貌",
                "古都", "传统", "古镇", "村落", "修缮", "活化", "更新",
                "陵墓", "石刻", "遗址", "城墙", "秦淮", "民国", "风景名胜"]

# 国家级兜底用的词表，比地方宽一档。国家层面的相关法律不带「历史」「名城」
# 这些字——《中华人民共和国城乡规划法》《消防法》《土地管理法》都在这条链上，
# 但没有一个词能概括它们。国家级法/条例总共才 269 条，宽一点不会失控。
_THEME_NATION = ["文物", "遗产", "规划", "建筑", "消防", "土地", "名城",
                 "街区", "历史", "风景名胜", "不动产权", "征收"]

# 判定「这个方向是不是历史文化领域」。_THEME_CATCH 里剔掉了修缮/活化/更新——
# 这三个词太泛，「城市更新专项规划」这种方向一沾上就会被判成历史文化领域，
# 于是又拿到一屏名城条例。
_DOMAIN_GATE = [w for w in _THEME_CATCH
                if w not in ("修缮", "活化", "更新")]

# 从方向标题里剥离的尾部泛词。分两类，不能混：
# 形态词（条例/办法/规定）随便剥，剥完还是同义；动词词（管理/调整/保护）
# 只在剩余长度够时才剥，否则「控制性详细规划」会被剥成「控制性详细」。
_TAIL_FORM = re.compile(
    r"(?:的)?(?:条例|办法|规定|规则|细则|通知|意见|方案|政策|文件|措施|"
    r"法规|规章|成果|汇编|清单|目录|台账)+$")
_LEAD_NOISE = re.compile(r"^(?:该|本|这|那|其|上述|所在|相关|已批复的|已批准的|已有的|"
                         r"所在地|项目|片区)+")
_TAIL_VERB = re.compile(
    r"(?:的)?(?:管理|监督|实施|执行|调整|修改|审批|编制|制定|保护|利用|"
    r"工作|要求|内容)+$")
_DOMAIN_SPLIT = re.compile(r"[、，,／/；;]|(?:以及|和|与|及|或)")


def _shrink(s: str) -> list[str]:
    """一个词块 → 由长到短的核心词候选。"""
    out = []
    s = _TAIL_FORM.sub("", s)
    s = _LEAD_NOISE.sub("", s) or s
    while len(s) >= 4:
        if _WEAK_Q.match(s):
            break
        out.append(s)
        nxt = _TAIL_VERB.sub("", s)
        # 剥到 5 字以下就停：「控制性详细规划」再剥是「控制性详细」，
        # 是个不存在的词，匹配不上任何标题，只会占着位置。
        if nxt == s or len(nxt) < 5:
            break
        s = nxt
    return out


def _task_words(task: dict) -> list[str]:
    """从方向自身抽核心词——兜底必须跟着问题走。

    上一版兜底用的是一张固定的历史文化词表（历史/名城/街区/陵墓/秦淮…）。
    「南京市控制性详细规划管理与调整的规定」这个方向于是返回前三名
    《南京历史文化名城保护条例》《南京市南朝陵墓石刻保护条例》
    《南京市夫子庙秦淮风光带条例》——方向问控规，兜底答文物保护。
    这比返回 0 条更坏：0 条用户知道这里缺，答偏了他会当成有依据。

    「南京市控制性详细规划管理与调整的规定」剥成 → 控制性详细规划
    「江苏省历史文化名城保护与规划条例」→ 历史文化名城保护
    """
    title = task.get("title") or ""
    geo = task.get("geo") or ""
    if geo and title.startswith(geo):
        title = title[len(geo):]
    chunks = list(task.get("queries") or []) + [title]
    out, seen = [], set()
    for c in chunks:
        for part in _DOMAIN_SPLIT.split(c or ""):
            for cand in _shrink((part or "").strip()):
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
    # 长的排前面：长词更专指，命中它比命中短词更能说明是同一样东西
    out.sort(key=len, reverse=True)
    return out[:8]


def _in_domain(task: dict, gate: list[str]) -> bool:
    text = " ".join([task.get("title") or ""] + list(task.get("queries") or []))
    return any(w in text for w in gate)



# 纯形态词：「保护条例」「管理办法」「实施方案」这种，去掉泛动词后只剩文件形态。
# 模型经常把它塞进 queries（问法规时它爱补一条「保护条例」），而标题以「保护条例」
# 结尾的文件有上百份——《南京市湿地保护条例》《南京市水资源保护条例》全都能命中，
# 且得分不低（0.36），足以排在真正相关的文件之前。
# 一个 query 若本身不携带任何主题，就不能单独构成命中。
_WEAK_Q = re.compile(
    r"^(?:保护|管理|实施|试行|暂行|若干|有关|相关|进一步|加强|推进|行动|工作|"
    r"建设|发展|调整|优化|促进|提升|完善|规范|详细|专项|系列|综合)*"
    r"(?:条例|办法|规定|规则|细则|法|规划|通知|意见|决定|方案|政策|文件|措施)$")


def _score(title: str, queries: list[str], published: str) -> tuple[float, list[str], bool]:
    """`first` 表示「直接命中」——标题里有这件事的名字，不是顺带沾边。

    两条路都算：

    1. **命中模型排在前两位的词**。模型把带项目名的词放第一位很常见
       （「老门西保护规划」），它在标题里永远找不到，所以认前两个；只认第一个会
       让「本地规划」整层落成灰的。
    2. **命中 5 字以上的专业词**。切分出来的短词不占前两位——「历史文化名城名镇
       名村保护条例」拆成 历史文化名城 / 名镇名村 之后排在索引 3、4，而
       「省政府关于同意将宿迁市列为江苏省历史文化名城的批复」正是靠它认出来的。
       只按位置判，省级法规和同类案例两层会全部落到 related，界面上 22 条候选
       一片灰，用户以为库里没东西。

    5 字这条线分的是「专业词」和「大词」：历史文化街区(6)、历史文化名城(6)、
    防止大拆大建(6)、控制性详细规划(7) 够长，撞上就是冲着这件事来的；
    城市更新(4)、历史地段(4)、老旧小区(4) 太宽，撞上只能算沾边，
    除非它正好排在模型的前两位。
    """
    hit, first = [], False
    for i, q in enumerate(queries):
        if q and q in title:
            hit.append(q)
            # 弱词只加覆盖度，不当「直接命中」——「保护条例」撞上《水资源保护条例》
            # 不是冲着历史文化街区来的
            if not _WEAK_Q.match(q) and (i < 2 or len(q) >= 5):
                first = True
    # 命中的全是弱形态词 → 等于没命中。否则「本地法规」一屏全是《XX保护条例》
    if not hit or all(_WEAK_Q.match(h) for h in hit):
        return 0.0, [], False
    cover = sum(len(h) for h in hit) / max(4, len(_STOP_TITLE.sub("", title)))
    yr = 0.0
    m = re.match(r"(\d{4})", published or "")
    if m:
        yr = max(0.0, min(1.0, (int(m.group(1)) - 2010) / 16.0))
    s = (0.42 * min(1.0, len(hit) / 2) + 0.33 * (1.0 if first else 0.0)
         + 0.18 * min(1.0, cover) + 0.07 * yr)
    return s, hit, first


_NOISE_HEAD = re.compile(r"^(?:标题|题名|文件标题|名\s*称)[：:]\s*")
_NOISE_TAIL = re.compile(r"\s*(?:点击数|浏览数|访问量|阅读数|点击量)[：:]\s*\d+\s*$")


def clean_title(t: str) -> str:
    """去掉采集时连字段名一起抓进来的噪音。

    个别区县政府站的列表页把「标题：」「点击数：1」也放在同一个节点里，
    入库后标题就成了「标题：庆政发〔2024〕42号…的通知 点击数：1」。
    它不影响检索，但会原样显示给用户。
    """
    t = _NOISE_HEAD.sub("", (t or "").strip())
    t = _NOISE_TAIL.sub("", t)
    return t.strip()


def _pack(it: dict, bidx: BodyIndex) -> dict:
    # 先用原始标题查正文：全文库的 key 是按入库时的标题算的，
    # 清洗后标题变了就查不到了。清洗只影响显示。
    bk = it.get("body_key") or bidx.key_of(it["title"])
    it["has_body"] = bool(bk)
    it["body_key"] = bk
    ct = clean_title(it["title"])
    if ct and ct != it["title"]:
        it["title_raw"] = it["title"]
        it["title"] = ct
    it["shape"] = shape_of(it["title"])
    # 目录通道不知道正文有多长，界面要靠这个字数区分「有正文」和「仅有公布公告」。
    # 无条件给值（没有正文就是 0），不能只在有正文时才设：字段时有时无的话，
    # 下游 `b.get("chars")` 拿到 None 会退回去数装配片段，又误判一遍。
    if not it.get("chars"):
        it["chars"] = ((bidx.by_key.get(bk) or {}).get("chars") or 0) if bk else 0
    # 台账状态按「非正常」才带出去：no_signal 占全库 91%，带上去只会让每个
    # 条目多一个字段、界面多一片灰字，用户还得自己判断灰字是什么意思。
    e = bidx.eff.get(bk) if bk else None
    if e and e.get("status") and e["status"] != "no_signal":
        it["eff"] = e["status"]
        it["eff_date"] = _eff_date(e)
    return it


def _eff_date(e: dict) -> str:
    """界面上跟在这个状态词后面的那个日期，必须跟状态对得上。

    原来一律取 valid_until or effect_on，于是 repealed 的文件显示的是它的
    **施行日**：《市政府关于印发南京市国有土地上房屋征收与补偿办法的通知》
    2012-06-01 施行、2017-10-30 废止，界面上印「已废止 2012-06-01」——
    差的这五年正好是最需要判断的那五年。日期和状态词对不上，比不给日期更糟：
    读者会以为这份文件 2012 年就废了。
    """
    st = e.get("status") or ""
    if st == "repealed":
        return e.get("repealed_on") or ""
    if st == "pending":
        return e.get("effect_on") or ""
    if st in ("expired", "in_force"):
        return e.get("valid_until") or ""
    # superseded 没有统一日期的来源，有到期日就用到期日，否则留空。
    return e.get("valid_until") or ""


def _query_catalog(cat: sqlite3.Connection, bidx: BodyIndex, task: dict,
                   queries: list[str]) -> list[dict]:
    where = "(" + " OR ".join(["title LIKE ?"] * len(queries)) + ")"
    args = [f"%{q}%" for q in queries]
    gclause, gargs = _geo_clause(task["kind"], task.get("geo", ""))
    sql = ("SELECT title,region,admin_level,doc_no,published,url,source_name,doc_type "
           "FROM items WHERE " + where + (f" AND {gclause}" if gclause else "") +
           " LIMIT 4000")
    out = []
    ok_shape = _KIND_SHAPE.get(task["kind"])
    for r in cat.execute(sql, args + gargs):
        title = r["title"] or ""
        s, hit, first = _score(title, queries, r["published"] or "")
        if s <= 0 or all(h in _GENERIC for h in hit) or _NOT_DOC.search(title):
            continue
        if ok_shape and shape_of(title) not in ok_shape:
            continue
        gwhy = ""
        if task["kind"] == "case":
            b, gwhy = _case_bonus(r["region"] or "", r["admin_level"] or "",
                                  task.get("_city", ""), task.get("_province", ""))
            s = max(0.001, s + b)
        it = _pack({
            "title": title, "region": r["region"] or "",
            "level": r["admin_level"] or "", "doc_no": r["doc_no"] or "",
            "published": (r["published"] or "")[:10], "url": r["url"] or "",
            "source": r["source_name"] or "", "doc_type": r["doc_type"] or "",
            "hits": hit, "bucket": "strong" if first else "related",
            "score": round(s, 3), "where": "catalog", "geo_why": gwhy,
            "task_kind": task["kind"],
        }, bidx)
        # 案例方向看的是「别人怎么做的」。没有正文，等于只知道有个文件叫这个名；
        # 正文只有 195 字的批后公告，等于知道「有个规划」但拿不到任何做法。
        # 这两种在案例场景下是一回事，都要沉下去。
        if task["kind"] == "case" and (it["chars"] or 0) < 800:
            it["score"] = round(max(0.001, it["score"] - 0.35), 3)
            tag = "无正文" if not it["has_body"] else "仅公告"
            it["geo_why"] = f"{gwhy}·{tag}" if gwhy else tag
        # 规划层：批后公布页只有一两百字公告，本体是图件。它证明「这个规划存在」，
        # 但提供不了任何可引的条文。实测首屏 10 条全是这种，而《关于进一步加强
        # 老城风貌管控严格控制老城建筑高度规划管理规定的通知》——真正写着限高
        # 要求的这一份——排在末尾。规划层的排序必须按「能不能引」来，不能按标题像不像。
        if task["kind"] == "plan_local" and (it["chars"] or 0) < 800:
            it["score"] = round(max(0.001, it["score"] - 0.45), 3)
        out.append(it)
    return out


def _query_fulltext(cat: sqlite3.Connection, bidx: BodyIndex, task: dict,
                    queries: list[str]) -> list[dict]:
    """国家层级：直接扫 fulltext 的标题（国家级 18,608 条，几十毫秒）。

    这张表最初只装 gov.cn 政策文件库的正文，所以「扫全表 = 扫国家级」成立。
    2026-10 补抓了地方正文（local_full.py，key 前缀 `lf`）之后就不成立了：
    不排除的话，「国家法规」方向会返回《南京历史文化名城保护条例》，而用户问的
    是国务院行政法规。地方文件冒充上位法，比缺一条严重。

    靠 key 前缀区分是隐式约定，但它比按 url 域名判可靠——地方政府站同样是
    gov.cn 子域，按域名分不开。
    """
    where = "(" + " OR ".join(["title LIKE ?"] * len(queries)) + ")"
    args = [f"%{q}%" for q in queries]
    out = []
    ok_shape = _KIND_SHAPE.get(task["kind"])
    for r in cat.execute(
            "SELECT key,title,url,doc_no,published,issuer,chars FROM fulltext "
            "WHERE status='ok' AND key NOT LIKE 'lf%' AND " + where +
            " LIMIT 1200", args):
        title = r["title"] or ""
        s, hit, first = _score(title, queries, r["published"] or "")
        if s <= 0 or all(h in _GENERIC for h in hit) or _NOT_DOC.search(title):
            continue
        if ok_shape and shape_of(title) not in ok_shape:
            continue
        out.append(_pack({
            "title": title, "region": "国家", "level": "国家",
            "doc_no": r["doc_no"] or "", "published": (r["published"] or "")[:10],
            "url": r["url"] or "", "source": r["issuer"] or "国务院政策文件库",
            "doc_type": "", "hits": hit,
            "bucket": "strong" if first else "related",
            "score": round(s, 3), "where": "fulltext", "geo_why": "",
            "body_key": r["key"], "chars": r["chars"],
            "task_kind": task["kind"],
        }, bidx))
    return out


# 同一模板批量发出的文件：「国务院关于同意将 X 列为国家历史文化名城的批复」一份一份
# 攒到了 19 条，把国家法规方向的前半屏占满。折叠成 1 条 + 「另有 N 条同模板」，
# 信息一点没丢，列表能看。
_TPL = [
    (re.compile(r"同意将.+?列为"), "同意将*列为"),
    (re.compile(r"公布第.+?批"), "公布第*批"),
    (re.compile(r"[（(]\d{4}\s*[—\-－]\s*\d{4}\s*年?[)）]"), "(*)"),
]
_FOLD_MIN = 3


def _tpl(title: str) -> str:
    t = norm_title(title)
    for p, r in _TPL:
        t = p.sub(r, t)
    return t


def _fold(items: list[dict]) -> list[dict]:
    """同模板 ≥3 条时只留分最高的那条，其余计数挂在 `more` 上。"""
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(_tpl(it["title"]), []).append(it)
    out = []
    for it in items:
        g = groups[_tpl(it["title"])]
        if len(g) < _FOLD_MIN:
            out.append(it)
        elif it is g[0]:
            it = dict(it)
            it["more"] = len(g) - 1
            it["more_titles"] = [x["title"] for x in g[1:4]]
            out.append(it)
    return out


def _query_catch(cat: sqlite3.Connection, bidx: BodyIndex, task: dict,
                 cap: int = 8) -> list[dict]:
    """本市其它法规兜底——按目录翻，不按关键词搜。

    关键词检索有个结构性盲区：法规标题不一定含问题里的词。
    《南京市夫子庙秦淮风光带风景名胜区条例》管的就是老门西所在那一带，
    但标题里没有「历史文化街区」四个字，纯关键词永远召回不了；而
    《南京历史文化名城保护条例》能被召回，只是因为标题恰好含了这几个字。
    于是「南京在这方面有什么规定」会被答成 1 条——比实际少一个量级。

    规划师不这么找。他翻本市地方性法规目录，扫一遍名称，凭常识挑出相关的。

    触发条件收得很紧：**只在关键词命中少于 3 条时**才补，且最多 8 条。
    不这样限制，南京 454 条条例/办法会淹没真正命中的那几条——兜底是为了
    补召回，不是为了把列表塞满。
    """
    gclause, gargs = _geo_clause(task["kind"], task.get("geo", ""))
    tw = _task_words(task)
    if task["kind"] == "reg_nation":
        # 国家级走全文库，_geo_clause 返回空。但法律本体（《文物保护法》
        # 《城乡规划法》）根本不在 gov.cn 政策文件库里，只在目录层。
        # 不单独兜一次，国家法规方向会只剩《历史文化名城名镇名村保护条例》一条，
        # 而规划师做这件事第一顺位要看的就是那两部法。
        gclause, gargs = "admin_level='国家'", []
        # 国家层面保持宽词表：法律不带「历史」「名城」这些字，收窄会漏掉
        # 《城乡规划法》《消防法》。总数才 269 条，宽一点不会失控。
        words = tw + [w for w in _THEME_NATION if w not in tw]
    else:
        if not gclause:
            return []
        # 领域兜底只在方向本身落在该领域时启用。
        # 「历史文化街区」的方向可以补《内秦淮河历史风貌区保护规划》——
        # 它标题里没有「历史文化街区」四个字，纯关键词永远召回不了，
        # 但它就是这片区的规划。而「控规管理与调整」的方向不启用：
        # 它跟名城保护不是一个领域，补进来全是串味。
        words = tw
        if _in_domain(task, _DOMAIN_GATE):
            words = tw + [w for w in _THEME_CATCH if w not in tw]
    if not words:
        return []
    sql = ("SELECT title,region,admin_level,doc_no,published,url,source_name,doc_type "
           "FROM items WHERE " + gclause +
           " AND (title LIKE '%条例%' OR title LIKE '%办法%' OR title LIKE '%规定%' "
           "      OR title LIKE '%法') "
           "ORDER BY coalesce(published,'') DESC LIMIT 400")
    ok_shape = _KIND_SHAPE.get(task["kind"]) or {"条例", "规章", "办法"}
    out = []
    for r in cat.execute(sql, gargs):
        title = r["title"] or ""
        if _NOT_DOC.search(title) or "废止" in title or "修改" in title:
            continue
        # 目录层没有正文，已经能从全文库拿到的不重复补
        if shape_of(title) not in ok_shape:
            continue
        hit = [w for w in words if w in title]
        if not hit:
            continue
        # 命中方向自身的词，比命中领域词更说明是同一样东西。
        # 不加这 0.08，「控规」方向和「名城」方向捞回来的条目分数一样，
        # 只按分数排会把专门的规定排到泛泛的保护条例后面。
        on_task = [w for w in tw if w in title]
        it = _pack({
            "title": title, "region": r["region"] or "",
            "level": r["admin_level"] or "", "doc_no": r["doc_no"] or "",
            "published": (r["published"] or "")[:10], "url": r["url"] or "",
            "source": r["source_name"] or "", "doc_type": r["doc_type"] or "",
            "hits": hit, "bucket": "strong" if on_task else "related",
            # 兜底触发时说明关键词检索已经失效，这时候兜底条目就是主力候选，
            # 不能再按「补位」给 0.04 这种分数——那会让它们永远排在
            # 「标题像但引不出条文」的文件后面。
            "score": round(0.30 + 0.05 * len(hit) + (0.08 if on_task else 0), 3),
            "where": "catalog", "geo_why": "", "catch": True,
            "task_kind": task["kind"],
        }, bidx)
        if it["has_body"]:
            it["score"] = round(it["score"] + 0.12, 3)
        out.append(it)
    # 兜底按分数取前 cap 条，不能按目录顺序取前 cap 条：
    # 目录是发布时间倒序，先撞上的往往是无关的近期文件。
    out.sort(key=lambda x: -x["score"])
    return out[:cap]


_SRC_SCOPE_PROV = ("本库的省级来源是省政府政策文件库，收的是省政府规章和省政府办公厅发文，"
                   "**不含省人大通过的地方性法规**。省级是否有专项条例，需另查省人大法规库。")


def _empty_note(task: dict) -> str:
    """空方向不能只留白。

    「省级 0 条」被读成「省级没规定」，是本项目最危险的一种误读——
    它会让人直接跳过这一层。空的原因分三种，必须分别说清：
    本库没这个来源、这个来源不收这类文件、确实没有。
    """
    k = task["kind"]
    geo = task.get("geo") or ""
    if k in ("reg_prov", "policy_prov"):
        return _SRC_SCOPE_PROV
    if k in ("reg_local", "plan_local", "policy_local"):
        return f"本库未收录{geo or '本市'}这方面可用的文件。这不等于当地没有规定，只说明本次资料里没有。"
    if k == "case":
        return "本库没有检索到其他城市在这件事上的做法。"
    return "本库未检索到相关文件。"


def gather_task(cat: sqlite3.Connection, bidx: BodyIndex, task: dict,
                cap: int = 40) -> dict:
    t0 = time.time()
    base = [q for q in (task.get("queries") or []) if len(q) >= 2]
    if not base:
        return {"id": task["id"], "items": [], "overflow": 0, "total": 0,
                "queries_used": [], "ms": 0}
    queries = expand_queries(base)
    rows = (_query_fulltext(cat, bidx, task, queries)
            if task["kind"] in _NATIONAL
            else _query_catalog(cat, bidx, task, queries))

    # 法规方向关键词命中太少时，按本市法规目录再翻一遍。
    # 阈值 3 是试出来的：命中 1–2 条通常意味着问题用的是「街区更新」这类
    # 只在「本地法规」和「国家法规」兜底。省级法规不兜：省级法规目录里本来
    # 就没有专门的历史文化名城条例（江苏实测如此），按主题词捞只会捞到
    # 《江苏省水域保护办法》这类不相干的省政府令，把「省级无专门规定」
    # 这个真实结论冲淡。
    #
    # 规划方向也要兜，且比法规更需要：保护区规划的标题常用老地名
    # （《内秦淮河历史风貌区保护规划修编》），而老门西就在内秦淮河风貌区内。
    # 只按「历史文化街区」搜，这份规划永远浮不上来——但它是本片区最直接的那一份。
    #
    # 判据不能用命中条数：规划层 14 条命中全是批后公布，正文 200 字（本体是图件），
    # 一条条文都引不出。「有 14 条」和「有 14 条能引」是两回事。
    k = task["kind"]
    if k == "plan_local":
        usable = sum(1 for x in rows if (x.get("chars") or 0) >= 800)
        if usable < 3:
            rows = list(rows) + _query_catch(cat, bidx, task)
    elif k in ("reg_local", "reg_nation") and \
            sum(1 for x in rows if not x.get("catch")) < 3:
        rows = list(rows) + _query_catch(cat, bidx, task)

    seen: dict[str, dict] = {}
    for it in rows:
        nk = norm_title(it["title"])
        old = seen.get(nk)
        if not old or it["score"] > old["score"]:
            seen[nk] = it

    # strong 是「标题里有这件事的名字」，但它不该压过分数。
    # 规划层尤其明显：批后公布靠「历史文化街区保护规划」这个长标题全被判 strong
    # （195 字，图件公告），而按目录兜底捞到的《老城建筑高度规划管理规定》（1,828 字，
    # 真能引）只能算 related。分档优先会让后者永远沉底，一分都用不上。
    if k == "plan_local":
        items = sorted(seen.values(), key=lambda x: -x["score"])
    elif k == "case":
        # 案例方向的目标是「别的城市怎么做、走的什么程序」。但本库其他城市的
        # 文件基本只有目录（实测 19 条里 16 条 0 字），能引的一条没有；
        # 反而是本市的历史街区保护规划有 195 字正文、还吃 0.14 同城加成，
        # 于是首屏两条是《南京总统府…批后公布》——标题写着「其他城市」，
        # 底下躺着本市，用户会直接判定这个方向标错了。
        # 三层：能引正文的在前；同城沉底（同城内容已由本地规划方向覆盖）；
        # 有正题命中的在同类里靠前。
        items = sorted(seen.values(), key=lambda x: (
            (x.get("chars") or 0) < 800,
            (x.get("geo_why") or "").startswith("同城"),
            x["bucket"] != "strong",
            -x["score"]))
    else:
        items = sorted(seen.values(), key=lambda x: (x["bucket"] != "strong", -x["score"]))
    items = _fold(items)
    total = len(items)
    n_usable = sum(1 for x in items if (x.get("chars") or 0) >= 800)
    note = _empty_note(task) if not items else ""
    if items and n_usable == 0:
        # 「有 19 条」和「有 19 条能引」是两回事。列表填满但一条条文都引不出，
        # 用户会以为这一层查到了，实际什么依据都没有。这一句必须在列表上方说清，
        # 不能等撰写阶段含糊过去。
        if k == "case":
            note = ("其他城市的相关文件本库只收了目录，没有正文。这些条目只能当线索"
                    "（去哪找、文件叫什么），引不出条文，也不能当依据。")
        elif k == "plan_local":
            note = ("本库这几份规划只有批后公布或图则公告，正文是图件，抽不出条文。"
                    "它们能证明「这份规划存在、已批复」，不能引条款。")
    return {"id": task["id"], "items": items[:cap],
            "overflow": max(0, total - cap), "total": total,
            "n_usable": n_usable, "note": note,
            "n_strong": sum(1 for x in items if x["bucket"] == "strong"),
            "queries_used": queries, "ms": round((time.time() - t0) * 1000)}


def gather(plan: dict, cap: int = 40, on_task=None, scope: str = "kb") -> dict:
    """按方向取证。

    `scope` 是检索范围，三档：

      kb    只查库（快、稳、可核，受库覆盖限制）
      web   只查联网（现抓目标政府站，慢、可能失败，但覆盖无上限）
      both  库优先，某个方向库内不足 3 条时才联网补

    `both` 不做「全方向都联网」：8 个方向各抓一轮要一分多钟，而
    库内已经够用的方向再抓一遍纯属浪费——**联网只补缺口**，这是它
    在这套系统里唯一站得住的位置。
    """
    cat = _ro(CAT_DB)
    bidx = BodyIndex()
    t0 = time.time()
    out = []
    place = plan.get("place") or {}
    city, prov = place.get("city", ""), place.get("province", "")
    net_rep: list[dict] = []
    try:
        # 第一阶段：库内，串行。
        # 查库是几十毫秒级，串行不心疼；sqlite 连接不能跨线程，也不该跨。
        staged = []
        for task in plan["tasks"]:
            # 案例方向要知道「这件事发生在哪」，才能判同城/同省。
            # 其它方向靠 geo 硬过滤，不需要这两个值。
            task = {**task, "_city": city, "_province": prov}
            if scope == "web":
                res = {"id": task["id"], "items": [], "total": 0, "overflow": 0,
                       "n_strong": 0, "queries_used": task.get("queries") or [],
                       "note": "", "ms": 0}
            else:
                res = gather_task(cat, bidx, task, cap)
            staged.append((task, res))

        # 第二阶段：联网，并发。
        #
        # 串行跑 9 个方向实测 140 秒以上——单个方向 15—25 秒（现抓政府站要等
        # 网络），乘起来没法用。并发 4 路压到 40 秒左右，这是能让用户坐得住的量级。
        # 联网只补缺口：both 模式下库内已有 3 份以上有正文的方向不联网。
        todo = [(task, res) for task, res in staged
                if scope == "web" or (scope == "both" and sum(
                    1 for x in (res.get("items") or []) if x.get("has_body")) < 3)]
        web_hits: dict[str, tuple[list, dict]] = {}
        if todo:
            import websearch
            # 按「目标站」分组，组内串行、组间并发。
            #
            # 不能直接对所有方向开 4 路并发：广州 4 个本地方向打的是同一个
            # gz.gov.cn，并发之后四个请求排队过 net 的 0.8 秒限速闸，
            # 反而互相拖死——实测第一个方向直接 HTTP 超时，返回 0 条。
            # 对同一个站就该一个一个来，这才是它承受得住的方式。
            groups: dict[str, list] = {}
            for task, res in todo:
                k = task.get("kind") or ""
                if k in ("reg_nation", "policy_nation", "standard"):
                    host = "_nation"
                elif k in ("reg_prov", "policy_prov"):
                    host = websearch.domain_of(prov) or "_unknown"
                else:
                    host = websearch.domain_of(city) or "_unknown"
                groups.setdefault(host, []).append((task, res))

            def _run_group(pairs):
                got = {}
                for task, _ in pairs:
                    try:
                        got[task["id"]] = websearch.search(
                            task, city=city, province=prov,
                            topk=12 if scope == "web" else 8)
                    except Exception as e:                   # noqa: BLE001
                        got[task["id"]] = ([], {
                            "error": f"{type(e).__name__}: {str(e)[:80]}",
                            "n_body": 0, "ms": 0, "channel": ""})
                return got

            with ThreadPoolExecutor(max_workers=min(4, len(groups))) as ex:
                futs = [ex.submit(_run_group, pairs) for pairs in groups.values()]
                for f in as_completed(futs):
                    try:
                        web_hits.update(f.result())
                    except Exception:                        # noqa: BLE001
                        continue

        for task, res in staged:
            if task["id"] in web_hits:
                w_items, w_rep = web_hits[task["id"]]
                w_rep["id"] = task["id"]
                net_rep.append(w_rep)
                if w_items:
                    # 已入库的同名文件不重复列；联网的排在库内之后，
                    # 让「库内已有的」始终是首选。
                    have = {norm_title(x["title"]) for x in res.get("items") or []}
                    add = [x for x in w_items if norm_title(x["title"]) not in have]
                    res["items"] = list(res.get("items") or []) + add
                    res["total"] = res.get("total", 0) + len(add)
                    res["n_web"] = len(add)
                if w_rep.get("error"):
                    res["web_error"] = w_rep["error"]
                    # 「站点挡住了」和「确实没有」是两件事，不能共用一句说明。
                    # 不覆盖的话，界面会显示「本方向库内没有文件」，用户读成
                    # 「合肥确实没有这方面政策」——而我们根本没看到那个站。
                    if w_rep.get("blocked") and not res.get("items"):
                        res["note"] = w_rep["error"]
                        res["blocked"] = True

            merged = {k: v for k, v in task.items() if not k.startswith("_")}
            merged.update({k: v for k, v in res.items() if k != "id"})
            merged["scope"] = scope
            out.append(merged)
            if on_task:
                on_task(merged)
    finally:
        cat.close()
    return {"tasks": out, "ms": round((time.time() - t0) * 1000),
            "scope": scope,
            "n_items": sum(len(t["items"]) for t in out),
            "n_body": sum(1 for t in out for i in t["items"] if i.get("has_body")),
            "n_web": sum(1 for t in out for i in t["items"] if i.get("where") == "web"),
            "web_reports": net_rep}


def fetch_bodies(picked: list[dict], terms: list[str],
                 per_doc: int = 3, budget: int = 28000) -> list[dict]:
    """对勾选的每份文件，回 kb.db 取最相关的几段。

    只取整份文件的前 3 段是错的——问「修缮责任」，条例的前三段是总则，
    讲的是「为了加强保护…制定本条例」。要在文件内部按问题词再排一次。

    总预算按份数均分，不按顺序先到先得。原来是全局 `used` 累加到 16,000
    就停，勾 18 份时预算在第 3 份就烧完，后面 15 份一块正文都没取到，
    `has_body` 全是 False —— 核验把它们全判成「只有目录条目」，
    一份本来 5,920 字的通知被判「未入库」。排序靠后的文件被系统性歧视，
    而排序靠后恰恰意味着它不是这个方向最核心的那份。
    """
    kb = _ro(KB_DB)
    out, used = [], 0
    n_doc = max(1, sum(1 for x in picked if x.get("body_key")))
    share = max(1200, budget // n_doc)
    try:
        for it in picked:
            # 联网抓来的条目自带正文块，直接装配，不去 kb.db 找。
            # 这类条目的 body_key 是空的（没入库），不先接住就会被判「只有目录」，
            # 刚抓下来的两万字白扔。
            wb = it.get("_blocks") or []
            if wb:
                # 联网条目也要有自己的 doc_key。
                #
                # 没有 key 的正文等于没有正文：`check.build_hits` 按 key 建索引，
                # 拿不到 key 就落进「只有目录」那一支，核验时整份判 unverifiable ——
                # 明明两万字就在手里，却告诉用户「库里没有正文，无法核对」。
                # key 用 url 的哈希，同一次运行内稳定，也不会和库内的 key 撞。
                wkey = "w" + hashlib.md5(
                    (it.get("url") or it["title"]).encode("utf-8")).hexdigest()[:14]
                # 按「命中词数 + 条款块加权」排，再按字数预算装满
                order = sorted(range(len(wb)),
                               key=lambda i: -(sum(1 for t in terms if t and t in (wb[i].get("text") or ""))
                                               + (0.5 if wb[i].get("kind") == "art" else 0.0)))
                blocks, got = [], 0
                for i in order:
                    if len(blocks) >= 14:
                        break
                    body = (wb[i].get("text") or "")[:1700]
                    if not body:
                        continue
                    room = min(share - got, budget - used)
                    if len(body) > room:
                        if room < 400:
                            break
                        body = body[:room]
                    got += len(body)
                    used += len(body)
                    blocks.append({"label": wb[i].get("label") or "", "text": body,
                                   "seq": i})
                blocks.sort(key=lambda b: b["seq"])
                for b in blocks:
                    b.pop("seq", None)
                out.append({"doc_key": wkey, "title": it["title"], "blocks": blocks,
                            "has_body": bool(blocks),
                            "chars": it.get("chars") or 0,
                            "n_blocks": len(wb), "n_art": it.get("n_art") or 0,
                            "where": "web", "url": it.get("url") or ""})
                continue
            key = it.get("body_key") or ""
            if not key:
                out.append({"doc_key": "", "title": it["title"], "blocks": [],
                            "has_body": False})
                continue
            rows = kb.execute("SELECT label,text,kind,seq FROM blocks "
                              "WHERE doc_key=? ORDER BY seq LIMIT 80", (key,)).fetchall()
            if not rows:
                out.append({"doc_key": key, "title": it["title"], "blocks": [],
                            "has_body": False})
                continue
            scored = []
            for r in rows:
                txt = r["text"] or ""
                n = sum(1 for t in terms if t and t in txt)
                # 条款块优先：能直接引条号
                scored.append((n + (0.5 if r["kind"] == "art" else 0.0), -r["seq"], r))
            scored.sort(key=lambda x: (-x[0], x[1]))
            # 按字数预算装满，不按固定块数。
            #
            # 固定 3 块是个隐蔽的浪费：块是条款级的，一份 12 块 1,044 字的规定
            # （《南京市控制性详细规划执行与调整规定》）只装 172 字，而它的第三、
            # 四、五条——「已批准的控详是…依据」「哪些情况可局部修改」——正是
            # 回答问题要的东西。模型读不到，就只能写「从文件名称看…」。
            # 一份 10,607 字的条例装 284 字，等于没装。
            blocks, got = [], 0
            for _, _, r in scored:
                if len(blocks) >= 14:          # 块太多模型会抓不住主线
                    break
                body = (r["text"] or "")[:1700]
                if not body:
                    continue
                room = min(share - got, budget - used)
                if len(body) > room:
                    # 「通知」体文件常被切成 900–1,400 字的大块，剩 800 字预算装不下
                    # 整块就跳过的话，这份文件只能装 1 块，预算白白剩着。
                    # 剩够 400 字就截断装进去——半块也带着条号，照样能引。
                    if room < 400:
                        break
                    body = body[:room]
                got += len(body)
                used += len(body)
                blocks.append({"label": r["label"] or "", "text": body,
                               "seq": r["seq"]})
                if got >= share:
                    break
            blocks.sort(key=lambda b: b["seq"])
            for b in blocks:
                b.pop("seq", None)
            # chars 是全文总字数，n_blocks 是切出来的块数——两个都要带出去。
            # 装配给模型的只是最相关的 3 块（可能只有 600 字），拿它判断
            # 「这份文件有没有实质内容」会错判：一份 1 万字的条例会被当成
            # 只有公告，核验时整篇标成 unverifiable。
            out.append({"doc_key": key, "title": it["title"], "blocks": blocks,
                        "has_body": bool(blocks),
                        "chars": it.get("chars") or 0,
                        "n_blocks": len(rows),
                        "n_art": sum(1 for r in rows if r["kind"] == "art")})
    finally:
        kb.close()
    return out
