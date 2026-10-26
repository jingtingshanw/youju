#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第一步：把一句人话变成一组「取证指令」。

## 为什么需要这一步

用户问「南京老门西历史文化街区的城市更新，想知道相关的文件」。这句话里
没有任何一个字能直接拿去检索：标题里不会写「老门西更新政策」，也不会写
「有哪些规定」。它描述的是**一个任务**，不是一个查询。

规划师拿到这句话会怎么做：先想这个街区受哪几层规定管——
本市的名城保护条例和街区保护规划、市里的城市更新办法、省里的条例和行动方案、
国家的条例和防止大拆大建的部委文件、别的城市同类街区的做法。
**这五个方向不是检索出来的，是职业经验里长出来的。**

所以这一步不检索，只拆解。拆出来的东西叫「取证指令」：每一条说清
「去哪个范围、找哪类文件、为什么必须找、用哪些词去撞标题」。

## 为什么关键词必须是「会出现在文件名里的词」

因为目录层只有标题。65,xxx 条记录里有文号的只有 23%，有正文的更少。
拿「老门西怎么更新」去 LIKE 标题，命中 0 条；拿「城市更新」去撞，命中
《南京市城市更新办法》。**这一步的输入质量直接决定了后面能不能找到东西。**
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

# ── 指令类型：不是分类学，是「证据效力」的排序 ──────────────────────
KINDS: dict[str, dict] = {
    "reg_local":     {"label": "本地法规", "rank": 1,
                      "desc": "本市人大或市政府制定的条例、办法、规定。区内建设活动的直接依据，效力最高。"},
    "plan_local":    {"label": "本地规划", "rank": 2,
                      "desc": "本市已批复的保护规划、更新专项规划。划定保护范围、建设控制地带和管控要求。"},
    "policy_local":  {"label": "本地政策", "rank": 3,
                      "desc": "本市的实施意见、行动计划、试点方案。讲具体怎么办、谁牵头、钱从哪来。"},
    "reg_prov":      {"label": "省级法规", "rank": 4,
                      "desc": "省人大或省政府的条例、办法。市级规定不得突破的上位约束。"},
    "policy_prov":   {"label": "省级政策", "rank": 5,
                      "desc": "省级行动方案、支持措施、试点名单。常决定能不能拿到省级资源。"},
    "reg_nation":    {"label": "国家法规", "rank": 6,
                      "desc": "国务院行政法规。全国通行的底线，任何地方做法都不能低于它。"},
    "policy_nation": {"label": "国家政策", "rank": 7,
                      "desc": "中办国办意见、部委通知。红线、考核口径、禁止性条款多在这里。"},
    "case":          {"label": "同类案例", "rank": 8,
                      "desc": "其他城市同类型街区、地段的已批规划或实施办法。用于比较做法、估难度、找创新点。"},
    "standard":      {"label": "技术标准", "rank": 9,
                      "desc": "保护规划编制审批办法、技术导则、评价标准。管的是成果怎么编、怎么报。"},
}

PLAN_SYS = """你是城乡规划师，正在为一个具体项目做政策依据梳理。
用户会给一个需求，可能很模糊。你的任务不是回答它，而是**列出为了回答它，
必须去查哪几类文件**。

## 你要输出的东西

4—8 条「取证指令」。每条指令 = 一个明确的取证方向。按证据效力从强到弱排：
1. 项目所在地的直接规定（本市法规、规章、规范性文件）
2. 所在地已批复的规划成果（保护规划、更新专项规划、控规调整）
3. 所在地的实施性政策（实施意见、行动计划、试点方案）
4. 上一级的上位规定（省条例、省行动方案；国家条例、部委文件）
5. 其他城市同类型项目的做法（案例）

不是每条都要有。项目在哪就查哪一层，跨省借案例时才用 case。

## 最关键的约束：queries 必须是「会出现在文件名里的词」

文件库里只有标题，没有正文。所以：
- 必须是名词或名词短语，2—10 字，是行业里的规范表述
- 不要问句（「有哪些规定」✗）
- 不要动词短语（「如何更新改造」✗）
- 不要带项目名（「老门西更新政策」✗ —— 标题里不会这么写）
- 每个方向给 2—4 个词，覆盖同义与上下位

正确示范（问南京某历史文化街区的更新）：
- 本地法规方向 → ["历史文化名城保护", "历史文化街区", "历史建筑"]
- 本地规划方向 → ["历史文化街区保护规划", "城市更新专项规划"]
- 本地政策方向 → ["城市更新", "历史风貌区"]
- 省级方向 → ["历史文化名城", "城市更新"]
- 国家方向 → ["历史文化名城名镇名村", "防止大拆大建", "历史文化保护传承"]
- 案例方向 → ["历史文化街区保护", "历史地段", "老城更新"]

错误示范：["南京老门西历史文化街区更新需要哪些文件", "老门西怎么更新"]

## 输出格式

只输出 JSON，不要解释，不要 markdown 代码块：

{"reading": "一句话说出用户真正要办的事（不是复述问题，是说出他背后的任务）",
 "place": {"city": "南京市", "province": "江苏省", "district": "", "site": "老门西"},
 "theme": ["历史文化街区", "城市更新"],
 "tasks": [
   {"kind": "reg_local", "title": "一句话说明这条要找什么",
    "why": "一句话说明为什么必须有它",
    "geo": "南京市", "queries": ["...", "..."], "must": true}
 ]}

kind 只能取：reg_local / plan_local / policy_local / reg_prov / policy_prov /
reg_nation / policy_nation / case / standard
geo 只能取：具体地名（南京市 / 江苏省 / 国家 / 不限）
must：这一条找不到就无法回答的写 true，找不到只是缺参考的写 false"""


@dataclass
class Task:
    """一条取证指令。"""
    kind: str
    title: str
    why: str = ""
    geo: str = "不限"
    queries: list[str] = field(default_factory=list)
    must: bool = False
    id: str = ""
    auto: bool = False        # 模型漏了、由系统补上的方向

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = "policy_local"
        if not self.id:
            self.id = f"t{abs(hash((self.kind, self.title))) % 10000:04d}"

    @property
    def label(self) -> str:
        return KINDS[self.kind]["label"]

    @property
    def rank(self) -> int:
        return KINDS[self.kind]["rank"]


@dataclass
class Plan:
    reading: str = ""
    city: str = ""
    province: str = ""
    district: str = ""
    site: str = ""
    theme: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    source: str = "llm"          # llm | rule（规则兜底时记下来，界面要标）

    def as_dict(self) -> dict:
        return {"reading": self.reading,
                "place": {"city": self.city, "province": self.province,
                          "district": self.district, "site": self.site},
                "theme": self.theme,
                "tasks": [asdict(t) | {"label": t.label, "rank": t.rank}
                          for t in sorted(self.tasks, key=lambda x: x.rank)],
                "source": self.source}


# ── 地名与主题：规则兜底用 ──────────────────────────────────────────
_CITY = re.compile(r"([\u4e00-\u9fa5]{2,8}?(?:市|自治州|地区|盟))")
_PROV = re.compile(r"([\u4e00-\u9fa5]{2,8}?(?:省|自治区))")
_DIST = re.compile(r"([\u4e00-\u9fa5]{2,8}?(?:区|县|旗))")

# 「城市」「都市」「市场」的末字也是「市」，正则切地名时会当成城市名。
# 实测「南京老门西历史文化街区的城市更新」被切成「历史文化街区的城市」，
# 这个错误一路传下去：geo 成了这个假地名 → region LIKE 匹配不到任何条目 →
# 本地法规/规划/政策三层全空 → 报告写「未检索到本市规定」。
# 一次抽取失败被包装成了「数据缺失」，比直接报错更难查。
_BAD_GEO = {"城市", "都市", "市场", "区域", "地区", "社区", "园区", "景区",
            "街区", "小区", "市区", "新区", "老区", "山区", "库区"}


def _find_city(q: str) -> str:
    """认城市名：先按已知城市表，再用正则，过滤掉「城市」这类假地名。"""
    for name in _PROV_OF_CITY:
        if name in q:
            return name + "市"
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,6}?)(市|自治州|地区|盟)", q):
        cand = m.group(1)
        if "的" in cand or cand in _BAD_GEO:
            continue
        return m.group(0)
    return ""

# 主题词表：键是「人话里可能出现的词」，值是「文件标题里会用的词」
THEMES: list[tuple[re.Pattern, list[str], list[str]]] = [
    (re.compile(r"历史文化街区|历史街区|老门西|老门东|历史地段"),
     ["历史文化街区", "历史文化名城保护", "历史建筑", "历史地段"],
     ["历史文化名城名镇名村保护条例", "历史文化保护传承", "防止大拆大建"]),
    (re.compile(r"城市更新|更新改造|旧城改造|微更新"),
     ["城市更新", "城市更新专项规划", "城市更新办法"],
     ["城市更新行动", "防止大拆大建", "老旧小区改造"]),
    (re.compile(r"老旧小区|危房|棚户"),
     ["老旧小区改造", "城镇老旧小区", "危房改造"],
     ["老旧小区改造", "配套设施建设"]),
    (re.compile(r"征收|拆迁|腾退"),
     ["征收补偿", "房屋征收", "区片综合地价"],
     ["国有土地上房屋征收与补偿"]),
    (re.compile(r"国土空间规划|总规|控规|详细规划"),
     ["国土空间规划", "控制性详细规划", "详细规划"],
     ["国土空间规划", "规划编制审批"]),
    (re.compile(r"土地|供地|用地"),
     ["土地利用", "土地供应", "划拨用地"],
     ["土地管理法", "节约集约用地"]),
    (re.compile(r"产业|园区|招商"),
     ["产业用地", "产业园区", "用地保障"],
     ["产业用地", "高质量发展"]),
]


def _fill_tasks(city: str, province: str, site: str,
                theme_local: list[str], theme_nation: list[str],
                has_site: bool) -> list[Task]:
    """规则兜底：不靠模型，按在职规划师的固定动作生成指令。"""
    t: list[Task] = []
    site_q = (theme_local + ([site] if site else []))[:5]
    if city:
        t.append(Task("reg_local", f"{city}对本类项目的直接规定",
                      "区内每一处建设行为首先受这一层约束，效力最高", city,
                      theme_local[:4], True))
        t.append(Task("plan_local", f"{city}已批复的相关规划",
                      "划定保护范围、建设控制地带，决定哪些能动、能改多少", city,
                      [f"{x}规划" for x in theme_local[:2]] + theme_local[2:4], True))
        t.append(Task("policy_local", f"{city}的实施性政策",
                      "讲具体怎么办、谁牵头、资金与审批路径", city,
                      theme_local[:3] + site_q[:1], True))
    if province:
        t.append(Task("reg_prov", f"{province}的上位规定",
                      "市级规定不得突破省条例，报批时按省口径审", province,
                      theme_local[:3], False))
        t.append(Task("policy_prov", f"{province}的省级政策与试点",
                      "决定能不能拿到省级资金、试点名额", province,
                      theme_local[:3], False))
    t.append(Task("reg_nation", "国务院行政法规",
                  "全国通行底线，地方做法不得低于它", "国家",
                  theme_nation[:2] or theme_local[:2], True))
    t.append(Task("policy_nation", "中办国办与部委文件",
                  "红线、考核口径、禁止性条款多在这里，出事时常被引", "国家",
                  theme_nation[1:] or theme_local[:3], True))
    if has_site:
        t.append(Task("case", "其他城市同类街区/地段的做法",
                      "同类型项目怎么批的、改了什么、踩过什么坑", "不限",
                      theme_local[:2] + ["历史地段"], False))
    return t


def _parse(raw: str) -> dict | None:
    """模型输出可能是纯 JSON，也可能裹了代码块或前后废话。"""
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.S)
    if m:
        s = m.group(1)
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return None
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _clean_query(q: str) -> str:
    q = re.sub(r"[《》\"'“”‘’]", "", str(q)).strip()
    q = re.sub(r"(有哪些|需要|如何|怎么|哪些|什么|规定|文件|政策)$", "", q).strip()
    return q[:14]


def _themes(question: str) -> tuple[list[str], list[str]]:
    """从问题里认主题，返回（本地标题用词，国家标题用词）。"""
    local, nation = [], []
    for pat, lw, nw in THEMES:
        if pat.search(question):
            local += lw
            nation += nw
    local = list(dict.fromkeys(local)) or ["城市更新"]
    nation = list(dict.fromkeys(nation)) or local[:2]
    return local, nation


def _ensure_coverage(plan: Plan, question: str) -> Plan:
    """保证两个方向不缺：国家法规、国家政策。

    模型经常把「国家条例」并进「国家政策」里，结果《历史文化名城名镇名村
    保护条例》这种最硬的依据反而不出现。它在库里、有正文、能逐条引，
    是这份报告里唯一能说「这是国务院行政法规」的东西。

    缺哪一条，就从规则兜底里取同一条补上——补进来的会在界面上标「自动补齐」。
    """
    have = {t.kind for t in plan.tasks}
    need = [k for k in ("reg_nation", "policy_nation") if k not in have]
    # 省级还要单独补一条政策方向。模型的固定误区是只给「省级法规」，而实测
    # 江苏根本没有公开的省级历史文化名城保护条例——真正能引的省级依据在
    # 《省政府办公厅印发关于支持城市更新行动若干政策措施的通知》里。
    # 只开一扇「法规」的门，省级这一层会永远空着，用户以为无上位依据。
    if plan.province and "policy_prov" not in have:
        need.append("policy_prov")
    if not need:
        return plan
    local, nation = _themes(question)
    fallback = _fill_tasks(plan.city, plan.province, plan.site, local, nation,
                           bool(plan.site))
    for t in fallback:
        if t.kind in need:
            t.auto = True
            plan.tasks.append(t)
            need.remove(t.kind)
        if not need:
            break
    return plan


def make_plan(question: str, pv: dict, call_llm) -> Plan:
    """拆解。模型不通就用规则兜底——**兜底不隐藏**，Plan.source 记着。"""
    plan: Plan | None = None
    err = ""
    try:
        raw = call_llm(pv, PLAN_SYS, f"需求：{question}", timeout=100, max_tokens=1400)
        d = _parse(raw)
        if d and d.get("tasks"):
            place = d.get("place") or {}
            tasks = []
            for x in d["tasks"][:10]:
                qs = [_clean_query(q) for q in (x.get("queries") or [])]
                qs = [q for q in dict.fromkeys(qs) if len(q) >= 2]
                if not qs:
                    continue
                tasks.append(Task(str(x.get("kind") or "policy_local"),
                                  str(x.get("title") or "")[:60],
                                  str(x.get("why") or "")[:80],
                                  str(x.get("geo") or "不限")[:12],
                                  qs[:4], bool(x.get("must"))))
            if tasks:
                plan = Plan(str(d.get("reading") or "")[:160],
                            str(place.get("city") or ""), str(place.get("province") or ""),
                            str(place.get("district") or ""), str(place.get("site") or ""),
                            [str(t)[:12] for t in (d.get("theme") or [])][:4], tasks, "llm")
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    if plan:
        # 模型给了城市但没给省，按常识补一层（只补 31 个省会/直辖市的常见情形）
        if plan.city and not plan.province:
            plan.province = _guess_province(plan.city)
        return _ensure_coverage(plan, question)

    # ── 规则兜底 ──────────────────────────────────────────────────
    city_s = _find_city(question)
    prov = _PROV.search(question)
    dist = _DIST.search(question)
    prov_s = prov.group(1) if prov else ""
    if not prov_s and city_s:
        prov_s = _guess_province(city_s)

    local, nation = _themes(question)

    site = ""
    m = re.search(r"([\u4e00-\u9fa5]{2,6}(?:街区|片区|地段|风貌区|古镇|古村))", question)
    if m:
        site = m.group(1)
    dist_s = dist.group(1) if dist else ""
    if dist_s in _BAD_GEO or "的" in dist_s:
        dist_s = ""
    p = Plan(reading=f"按「{city_s or '本地'}·{site or (local[0] if local else '')}」的取证路径排",
             city=city_s, province=prov_s,
             district=dist_s, site=site,
             theme=local[:3],
             tasks=_fill_tasks(city_s, prov_s, site, local, nation, bool(site)),
             source="rule")
    p.reading += f"（模型未返回结构化结果：{err[:60]}）" if err else ""
    return p


_PROV_OF_CITY = {
    "南京": "江苏省", "苏州": "江苏省", "无锡": "江苏省", "常州": "江苏省",
    "扬州": "江苏省", "南通": "江苏省", "徐州": "江苏省", "镇江": "江苏省",
    "泰州": "江苏省", "盐城": "江苏省", "淮安": "江苏省", "连云港": "江苏省",
    "宿迁": "江苏省",
    "杭州": "浙江省", "宁波": "浙江省", "温州": "浙江省", "绍兴": "浙江省",
    "广州": "广东省", "深圳": "广东省", "佛山": "广东省", "珠海": "广东省",
    "成都": "四川省", "西安": "陕西省", "武汉": "湖北省", "长沙": "湖南省",
    "郑州": "河南省", "济南": "山东省", "青岛": "山东省", "合肥": "安徽省",
    "福州": "福建省", "厦门": "福建省", "南昌": "江西省", "太原": "山西省",
    "石家庄": "河北省", "沈阳": "辽宁省", "长春": "吉林省", "哈尔滨": "黑龙江省",
    "昆明": "云南省", "贵阳": "贵州省", "南宁": "广西壮族自治区",
    "兰州": "甘肃省", "西宁": "青海省", "银川": "宁夏回族自治区",
    "乌鲁木齐": "新疆维吾尔自治区", "呼和浩特": "内蒙古自治区",
    "拉萨": "西藏自治区", "海口": "海南省",
    "北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市",
}


def _guess_province(city: str) -> str:
    core = re.sub(r"(市|自治州|地区|盟)$", "", city or "")
    for k, v in _PROV_OF_CITY.items():
        if k in core or core in k:
            return v
    return ""
