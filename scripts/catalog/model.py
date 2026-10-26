#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目录层数据模型：一条记录 = 一份文件的「存在性事实」，不含全文。

为什么要有这一层
----------------
全文库回答不了「这个方向有哪些文件」——文件本身不知道自己存在。所以目录层
只存**可以穷举的字段**（名称、发文机关、文号、效力级别、施行与失效日、地域、主题），
全文按需再从 `url` 抓。字段能穷举、全文不能，这是目录层能做到接近全、且能每天
刷新的根本原因；也正因如此，「知识库不够大」不再是死结。

两条硬规矩
----------
1. **不假装确定**。凡是推断出来的字段，都配一个 `*_src` 说明依据
   （文号 / 标题 / 源标注 / 推断）。效力状态没有证据就是「未知」，
   绝不默认给「现行有效」——那是最容易把用户带进坑里的一个默认值。
2. **不静默失败**。抓不到由适配器显式报告状态（见 `adapters.SourceReport`），
   不允许返回空列表冒充「查过了，没有」。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_PATH = ROOT / "data" / "stale_ledger.json"

# ══════════════════════════════════════════════════════════════════════════
# 一、主题词表 —— 用来给清单打标签，也用来回答「这个方向该看哪些」
# ══════════════════════════════════════════════════════════════════════════
#
# 这不是完备的法规分类体系，而是一份「规划师做项目时会拿来搜的词」。
# 它的作用是把「城市更新」这种主题自动带出邻接领域——用户说「有可能还要包括
# 历史文物方面的政策」，靠的就是这里：城市更新 × 历史文化保护 两个主题同时命中。
TOPICS: dict[str, list[str]] = {
    "城市更新": ["城市更新", "更新行动", "城市体检", "老旧小区", "城中村", "棚户区",
             "危旧房", "三旧改造", "存量用地", "低效用地", "城市双修", "有机更新"],
    "历史文化保护": ["历史文化", "文物", "历史建筑", "名城", "名镇", "名村", "传统村落",
                "历史街区", "非遗", "非物质文化遗产", "保护规划"],
    "土地管理": ["土地", "用地", "征收", "供地", "出让", "划拨", "耕地", "宅基地",
             "集约节约", "成片开发"],
    "规划管理": ["国土空间规划", "控制性详细规划", "详细规划", "规划许可", "选址",
             "规划编制", "总体规划"],
    "财政资金": ["财政", "专项资金", "专项债", "补助", "预算", "投资", "奖补", "基金"],
    "房屋征收": ["房屋征收", "拆迁", "补偿", "安置", "腾退"],
    "市政设施": ["市政", "道路", "管网", "燃气", "供水", "排水", "照明", "综合管廊"],
    "生态环境": ["生态", "环境", "污染", "扬尘", "噪声", "土壤", "绿化", "垃圾",
             "环卫", "市容", "排污"],
    "消防与安全": ["消防", "安全生产", "应急", "抗震", "防灾", "地质灾害"],
    "建筑管理": ["建筑", "施工", "工程", "监理", "质量", "验收", "拆除", "勘察设计"],
    "招标采购": ["招标", "投标", "政府采购", "采购"],
}

# ══════════════════════════════════════════════════════════════════════════
# 二、归一化
# ══════════════════════════════════════════════════════════════════════════

_PUNCT = re.compile(r"[，。、；：（）()「」『』《》〈〉“”‘’\"'—\-…\s·]")
_DATE_RE = re.compile(r"(20\d{2})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})")
_URL_DATE_RE = re.compile(r"/art/(20\d{2})/(\d{1,2})/(\d{1,2})/")
# 标题末尾的括注：全角/半角圆括号、方括号、书名号里的括注
_TAIL_PAREN = re.compile(r"[（(\[【][^（()）\[\]【】]{0,40}[)）\]】]\s*$")
# 粘在标题尾部的文号：`…的通知国办函〔2023〕115号`。半角方括号与圆括号也接受。
_TAIL_DOCNO = re.compile(r"[〔\[（(]\s*20\d{2}\s*[〕\]）)]\s*\d{1,4}\s*号\s*$")
_DOCNO_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z]{1,10})\s*〔\s*(20\d{2})\s*〕\s*(\d{1,4})\s*号")
_ORDER_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?)人民政府令\s*第\s*(\d{1,4})\s*号")
_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s: str) -> str:
    """去掉 gov.cn 检索结果里返回的 <em> 高亮标签。"""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", s or "")).strip()


def fold(s: str) -> str:
    """去重用的折叠形式：抹掉标点空白、去掉书名号、统一全半角。"""
    t = (s or "").strip()
    t = t.replace("（", "(").replace("）", ")")
    return _PUNCT.sub("", t).lower()


def parse_date(s: str) -> str:
    """把 2026.05.28 / 2026-5-28 / 2026年5月28日 统一成 2026-05-28。取不到返回空串。"""
    if not s:
        return ""
    m = _DATE_RE.search(s) or _URL_DATE_RE.search(s)
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_doc_no(text: str) -> str:
    """从标题或页面里抽出文号。两种体例：`国发〔2026〕12号`、`江西省人民政府令第252号`。"""
    m = _DOCNO_RE.search(text or "")
    if m:
        return f"{m.group(1)}〔{m.group(2)}〕{int(m.group(3))}号"
    m = _ORDER_RE.search(text or "")
    if m:
        return f"{m.group(1)}人民政府令第{int(m.group(2))}号"
    return ""


# ══════════════════════════════════════════════════════════════════════════
# 三、推断：地域 / 效力级别 / 主题 / 效力状态
# ══════════════════════════════════════════════════════════════════════════
#
# 全部是「有依据的推断」，不是权威元数据。国内没有一个统一接口会告诉你
# 「这份文件的效力级别是地方政府规章」，所以只能从发文机关、文号和标题推。
# 每个推断结果都带 *_src，UI 上显示依据，用的人才知道该信几分。

PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏",
    "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西",
    "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]

# 国家层面发文机关的常见写法
NATIONAL_ISSUERS = ("国务院", "全国人民代表大会", "全国人大常委会", "全国人大",
                    "中共中央", "国家发展改革委", "住房城乡建设部", "住房和城乡建设部",
                    "财政部", "自然资源部", "生态环境部", "应急管理部", "国家文物局",
                    "国家数据局", "商务部", "教育部", "民政部", "交通运输部",
                    "水利部", "农业农村部", "文化和旅游部", "国家市场监管总局")

# 文号前缀 → 效力级别。前缀不全，命中则信，不命中退回标题推断。
DOCNO_LEVEL: dict[str, str] = {
    "国令": "行政法规",
    "国发": "国务院文件",
    "国办发": "国务院文件",
    "国函": "国务院文件",
    "国办函": "国务院文件",
    "建办": "部门规范性文件",
    "建科": "部门规范性文件",
    "建规": "部门规范性文件",
    "财办建": "部门规范性文件",
    "财建": "部门规范性文件",
    "商流通函": "部门规范性文件",
    "自然资发": "部门规范性文件",
    "自然资规": "部门规范性文件",
    "文物保发": "部门规范性文件",
}

# 标题后缀 → 效力级别（只在没有文号可依时使用，可信度标「推断」）
TITLE_SUFFIX_LEVEL = [
    ("条例", "地方性法规"),
    ("实施细则", "规范性文件"),
    ("实施办法", "规范性文件"),
    ("办法", "规章或规范性文件"),
    ("规定", "规章或规范性文件"),
    ("决定", "规范性文件"),
    ("通知", "规范性文件"),
    ("意见", "规范性文件"),
    ("方案", "规范性文件"),
    ("规划", "规范性文件"),
    ("计划", "规范性文件"),
    ("指南", "技术文件"),
    ("标准", "技术文件"),
]

STATUS_STALE = ("已废止", "已失效")


def infer_region(issuer: str, title: str = "") -> tuple[str, str]:
    """返回 (地域, 依据)。地域形如「国家」「江西省」「鹰潭市」。"""
    blob = f"{issuer} {title}"
    if any(k in blob for k in ("鹰潭",)):
        return "鹰潭市", "标题或机关"
    for p in PROVINCES:
        if p in blob:
            return f"{p}省" if p not in ("北京", "天津", "上海", "重庆", "内蒙古",
                                         "广西", "西藏", "宁夏", "新疆") else p, "标题或机关"
    if any(k in blob for k in NATIONAL_ISSUERS):
        return "国家", "发文机关"
    if any(k in blob for k in ("国务院", "中共中央", "全国人")):
        return "国家", "发文机关"
    return "", "无法判定"


def admin_level_of(region: str) -> str:
    """地域 → 行政层级。用于「市级→省级→国家」向上展开。"""
    if not region or region == "国家":
        return "国家"
    if region.endswith(("省", "市", "自治区")) and region in (
            "北京市", "天津市", "上海市", "重庆市"):
        return "省级"
    if region.endswith(("省", "自治区")):
        return "省级"
    if region.endswith(("市", "州", "盟", "地区")):
        return "市级"
    return "未知"


def title_core(title: str) -> str:
    """剥掉标题末尾的括注与文号：「…专项规划（2026—2035年）」→「…专项规划」。

    这一步不做，`endswith("规划")` 就落空，一份规划被判成「无法判定」。
    实测踩到的原样：鹰潭那份《中心城区城市更新专项规划（2026-2035年）》
    因为末尾是「）」而被当成非文件，直接从清单里掉了。

    文号也要剥，因为有些站的转载标题把文号直接粘在标题尾部：
    `…政府和社会资本合作新机制的通知国办函〔2023〕115号`——不剥，末尾是「号」，
    `endswith("通知")` 落空，一份国务院文件被静默丢掉（2026-10-19 审计实测）。
    """
    t = (title or "").strip()
    prev = None
    while prev != t:
        prev = t
        t = _TAIL_PAREN.sub("", t).strip()
        t = _TAIL_DOCNO.sub("", t).strip()
    return t


def infer_doc_type(issuer: str, title: str, doc_no: str = "") -> tuple[str, str]:
    """返回 (效力级别, 依据)。依据取值：文号 / 标题 / 推断。"""
    if doc_no:
        m = re.match(r"([\u4e00-\u9fa5A-Za-z]+)〔", doc_no)
        prefix = m.group(1) if m else ""
        for k, v in DOCNO_LEVEL.items():
            if prefix == k:
                return v, "文号"
        if "人民政府令" in doc_no:
            return ("地方政府规章" if "省" not in doc_no and "国务院" not in doc_no
                    else "地方政府规章"), "文号"
        if re.match(r"^[\u4e00-\u9fa5]{1,4}(政发|政办发|政办|府发|府办发)〔", doc_no):
            return "地方政府规范性文件", "文号"
    # 只看标题的「核心部分」——括注里出现「条例（草案）」这类字样不算数。
    # 不剥括注会实测把一条国务院常务会议新闻判成「地方性法规」。
    core = title_core(title)
    for suf, lv in TITLE_SUFFIX_LEVEL:
        if core.endswith(suf):
            return lv, "标题"
    return "", "无法判定"


def detect_topics(text: str) -> list[str]:
    """按主题词表打标签。命中即打，一份文件可以有多个主题（这正是跨领域展开的来源）。"""
    t = text or ""
    hits = [name for name, kws in TOPICS.items() if any(k in t for k in kws)]
    return hits


def infer_status(title: str) -> tuple[str, str]:
    """从标题里能看出来的效力状态。看不出来就返回「未知」——不给有利假设。"""
    t = title or ""
    if any(k in t for k in ("（已废止）", "(已废止)", "（失效）", "(失效)")):
        return "已废止", "标题标注"
    return "未知", "无依据"


# ══════════════════════════════════════════════════════════════════════════
# 四、记录
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class CatalogItem:
    title: str
    issuer: str = ""
    doc_no: str = ""
    doc_type: str = ""          # 效力级别
    doc_type_src: str = ""
    region: str = ""
    region_src: str = ""
    topics: list[str] = field(default_factory=list)
    published: str = ""
    effective: str = ""
    abolished: str = ""
    status: str = "未知"
    status_src: str = "无依据"
    source: str = ""            # 适配器名
    source_type: str = ""       # official_api / official_html / manual
    source_name: str = ""       # 人读的来源名，如「国务院政策文件库」
    url: str = ""
    summary: str = ""
    fetched_at: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.title = strip_tags(self.title)
        self.summary = strip_tags(self.summary)

    # ---- 派生字段 ----
    def normalize(self) -> "CatalogItem":
        """把能自动填的字段填上，已经是显式值的保留（不强覆盖）。"""
        if not self.doc_no:
            self.doc_no = parse_doc_no(self.title)
        if not self.region:
            self.region, self.region_src = infer_region(self.issuer, self.title)
        if not self.doc_type:
            self.doc_type, self.doc_type_src = infer_doc_type(
                self.issuer, self.title, self.doc_no)
        if not self.topics:
            self.topics = detect_topics(f"{self.title} {self.summary}")
        if not self.status or self.status == "未知":
            st, src = infer_status(self.title)
            if st != "未知":
                self.status, self.status_src = st, src
        self.published = parse_date(self.published)
        self.effective = parse_date(self.effective)
        self.abolished = parse_date(self.abolished)
        return self

    @property
    def key(self) -> str:
        """去重键。有文号用文号，没有就用折叠后的标题。

        跨源同一份文件常常一个有文号一个没有，所以 store 里同时索引
        `key` 和 `doc_no`，两种方式都能撞上。
        """
        basis = fold(self.doc_no) if self.doc_no else fold(self.title)
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def admin_level(self) -> str:
        return admin_level_of(self.region)

    @property
    def is_stale(self) -> bool:
        return self.status in STATUS_STALE

    def to_row(self) -> dict:
        """给 SQLite 用的行：list 字段要转成 JSON 文本。"""
        d = asdict(self)
        d["key"] = self.key
        d["admin_level"] = self.admin_level
        d["topics"] = json.dumps(self.topics, ensure_ascii=False)
        return d

    def to_dict(self) -> dict:
        """给接口用的字典：topics 保持数组，前端不用再解一次。"""
        d = asdict(self)
        d["key"] = self.key
        d["admin_level"] = self.admin_level
        return d

    @classmethod
    def from_row(cls, row: dict) -> "CatalogItem":
        d = dict(row)
        d.pop("key", None)
        d.pop("admin_level", None)
        t = d.pop("topics", "")
        try:
            topics = json.loads(t) if t else []
        except (TypeError, ValueError):
            topics = [t] if t else []
        return cls(topics=topics, **{k: v for k, v in d.items()
                                     if k in cls.__dataclass_fields__})


# ══════════════════════════════════════════════════════════════════════════
# 五、与既有失效台账对齐
# ══════════════════════════════════════════════════════════════════════════
#
# 项目里已经有一份 `data/stale_ledger.json`（逐部法规核过时效，带废止依据和替代文件）。
# 目录层不去重复这件事：清单项如果撞上台账，就用台账的结论覆盖推断值，
# 并且把「废止依据 / 替代文件」一起带出来——这是清单最值钱的一列。

def load_ledger() -> dict[str, dict]:
    if not LEDGER_PATH.exists():
        return {}
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply_ledger(items: list[CatalogItem], ledger: dict[str, dict] | None = None) -> int:
    """用台账覆盖清单项的效力状态。返回被覆盖的条数。"""
    ledger = ledger if ledger is not None else load_ledger()
    if not ledger:
        return 0
    idx = {fold(k): v for k, v in ledger.items()}
    n = 0
    for it in items:
        info = idx.get(fold(it.title))
        if not info:
            continue
        if info.get("时效性"):
            it.status = info["时效性"]
            it.status_src = "时效台账"
        if info.get("施行日期") and not it.effective:
            it.effective = parse_date(info["施行日期"])
        if info.get("废止日期") and not it.abolished:
            it.abolished = parse_date(info["废止日期"])
        if info.get("效力位阶") and not it.doc_type:
            it.doc_type, it.doc_type_src = info["效力位阶"], "时效台账"
        extra = []
        if info.get("废止依据"):
            extra.append(f"废止依据：{info['废止依据']}")
        if info.get("替代文件"):
            extra.append(f"替代：{info['替代文件']}")
        if extra:
            it.note = "；".join(filter(None, [it.note, *extra]))
        n += 1
    return n


__all__ = [
    "TOPICS", "CatalogItem", "fold", "strip_tags", "title_core", "parse_date",
    "parse_doc_no", "infer_region", "infer_doc_type", "detect_topics",
    "infer_status", "admin_level_of", "load_ledger", "apply_ledger", "STATUS_STALE",
]
