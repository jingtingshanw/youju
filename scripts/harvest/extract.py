#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把网页 HTML 变成「能用的正文」。

为什么不用 BeautifulSoup / readability
--------------------------------------
项目从第一天就是零第三方依赖：拷走就能跑，不用装任何东西。正文提取这件事
如果有 200 行能做完 90%，就不该为剩下 10% 引入一棵依赖树。实测下来，政策
文件页面的结构比新闻网站单一得多，200 行够用。

判断「提取成功」的标准不是「有没有拿到文字」，而是**拿到的是不是正文**：
  · 中文字数 ≥ MIN_CHARS
  · 中文占比 ≥ 0.25（挡掉纯英文报错页 / 空白模板）
  · 不含「页面不存在」「403 Forbidden」这类拦截页特征
不满足就返回 ok=False，由调用方记为 failed —— 让失败可见，而不是存一篇
导航栏当正文。
"""
from __future__ import annotations

import html as _html
import re

MIN_CHARS = 100
# 为什么是 100 而不是 150：批复类文件的正文本来就短。但也不能压到 60——
# 实测「只拿到标题+文号」的失败样本正好是 34 个字，60 的线会让它蒙混过关。
MIN_HAN_RATIO = 0.12
# 为什么占比线是 0.12：政策文件里数字、表格、法条编号、附件清单占比很高。
# 「国务院关于同意在深圳市暂时调整实施有关行政法规规定的批复」有 8,000 字正文，
# 中文占比只有 0.21，第一版按 0.25 判它失败——真正的正文被判成垃圾。
# 占比只用来挡纯英文报错页，不需要定得那么高。

# 正文容器的起点。按可靠性排序：先试精确 id/class，再试含糊的。
# 第二项 True = 精确特征（这个 id/class 基本只用于正文），命中即采信；
# False = 含糊特征（class="content" 在导航里也常见），要求后文中文够密。
_CONTAINERS = [
    (r'<div[^>]*\bid=["\']UCAP-CONTENT["\']', True),   # gov.cn
    (r'<div[^>]*\bid=["\']zoom["\']', True),           # 大量省市站（TRS 系）
    (r'<div[^>]*class=["\'][^"\']*\bpages_content\b', True),
    (r'<div[^>]*class=["\'][^"\']*\barticle[-_]?content\b', True),
    (r'<div[^>]*class=["\'][^"\']*\bTRS_Editor\b', True),
    # 南京市政府站（TRS 新版编辑器）用的是 class="view TRS_UEDITOR trs_paper_default"，
    # 与 TRS_Editor 是两个不同的类名。不补这一条，南京 1,510 条政策法规库条目
    # 会全部走整页回退，正文里混进导航和索引号，被判失败。
    (r'<div[^>]*class=["\'][^"\']*\bTRS_UEDITOR\b', True),
    (r'<td[^>]*class=["\'][^"\']*\bb12c\b', True),
    (r'<div[^>]*class=["\'][^"\']*\bcontent[-_]?box\b', False),
    (r'<div[^>]*class=["\'][^"\']*\barticle\b', False),
    (r'<div[^>]*class=["\'][^"\']*\bcontent\b', False),
    (r'<div[^>]*\bid=["\']content["\']', False),
]

# 正文结束的锚点。出现在正文之后，命中就截断——比配对 </div> 可靠得多：
# 政策页面里嵌套 div 常有几十层，数配平容易错位，而「正文后面一定跟着
# 分享/分页/相关阅读」是稳定的结构事实。
_TAIL_ANCHORS = [
    'id="pagination"', 'class="pageInfo', 'class="pages_print',
    'id="share', 'class="share', '扫一扫在手机打开当前页',
    '【我要纠错】', '【字体：', '相关链接', '相关稿件', '相关阅读',
    '责任编辑：', '上一篇', '下一篇', '打印本页', '关闭窗口',
    'class="footer', 'id="footer', '版权所有',
]

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|iframe|object|embed|select|textarea|form)\b[^>]*>.*?</\1>",
    re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|br|tr|li|h[1-6]|section|article|blockquote|pre|table|thead|tbody|td)\b[^>]*>",
    re.I)
# 标签长度不限，但禁止跨标签边界（[^<>]）。
# 2026-10-20 修：原来写 <[^>]{1,400}>，Word 导出的 <td style="...mso-border...">
# 动辄上千字符，剥不掉，几万字的 CSS 样式文本漏进正文——「国务院关于开展
# 营商环境创新试点工作的意见」那 100 多条改革清单表格因此中文占比掉到 0.08，
# 被判失败。402 份失败里绝大多数是这类表格型文件。
_ANY_TAG = re.compile(r"<[a-zA-Z/!][^<>]*>")
# 没有闭合的半个标签。正文截断点落在标签中间时，`_ANY_TAG` 剥不掉它
# （它要求有结尾的 >），于是「…向社会予以公布。\n<div」这种尾巴进了正文。
# 出现在每行末尾的 `<div`、`</p`、`<span style="..."` 一律清掉。
# 只匹配行尾且 `<` 后紧跟字母，`a < b` 这类正文比较不会被误伤。
_HALF_TAG = re.compile(r"</?[a-zA-Z][^<>]*$")
# 先把属性摘掉再去标签：即使标签本身因为含 ">" 漏网，样式文字也不会残留。
_ATTR = re.compile(
    r"\s+(?:style|class|width|height|valign|colspan|rowspan|align|border|face|size|color|lang|id)\s*=\s*"
    r"(?:\"[^\"]*\"|'[^']*')", re.I)
_WS = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")
_HAN = re.compile(r"[\u4e00-\u9fa5]")

# 拦截页 / 异常页特征。命中即判失败。
_BAD_MARKERS = (
    "403 Forbidden", "404 Not Found", "页面不存在", "您访问的页面",
    "该页面已被删除", "Access Denied", "您的访问过于频繁",
    "请开启JavaScript", "请使用浏览器", "网站正在维护",
)


def _strip_blocks(s: str) -> str:
    s = _COMMENT.sub("", s)
    s = _DROP_BLOCKS.sub("", s)
    return s


def html_to_text(raw: str) -> str:
    """HTML → 纯文本，保留段落换行。"""
    s = _strip_blocks(raw)
    s = _ATTR.sub("", s)
    s = _BLOCK_TAG.sub("\n", s)
    s = _ANY_TAG.sub("", s)
    s = _html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for ln in s.split("\n"):
        ln = _WS.sub(" ", ln).strip()
        ln = _HALF_TAG.sub("", ln).strip()
        # 单字符行几乎都是图标/序号残留；「|」开头的行是导航分隔
        if not ln or (len(ln) == 1 and not _HAN.match(ln)):
            continue
        if ln in ("|", "·", "»", "»"):
            continue
        lines.append(ln)
    return _MULTI_NL.sub("\n", "\n".join(lines)).strip()


def _container_start(raw: str) -> tuple[int, str]:
    for pat, exact in _CONTAINERS:
        m = re.search(pat, raw, re.I)
        if not m:
            continue
        cand = m.start()
        if exact:
            return cand, pat
        # 含糊特征才做密度校验：容器后 4000 字符内中文字数够多。
        # 精确特征不做校验——Word 导出的页面把每个字都包在几百字符的
        # <span style> 里，4000 字符只够装标题，密度判据会把真正文判没。
        # 314 份失败里有一半是这么来的。
        tail = raw[cand:cand + 4000]
        if len(_HAN.findall(_ANY_TAG.sub("", tail))) >= 80:
            return cand, pat
    return -1, ""


def _cut_tail(seg: str) -> str:
    """找正文结尾。

    第一版从第 800 个字符往后找锚点，结果年久的短批复（如「国务院关于同意在
    深圳市暂时调整实施有关行政法规规定的批复」，全文不到 800 字符）被截成了
    34 个字——正文整段没进库，而它长得像「提取成功」。
    判据改成「锚点前面已经攒够正文」，而不是「锚点在 800 字符之后」。
    """
    best = -1
    for a in _TAIL_ANCHORS:
        start = 0
        while True:
            i = seg.find(a, start)
            if i < 0:
                break
            # 锚点前面 2000 字符内要有像样的正文，才认它是结尾
            if len(_HAN.findall(seg[max(0, i - 2000):i])) >= 30:
                if best < 0 or i < best:
                    best = i
                break
            start = i + 1
    return seg[:best] if best > 0 else seg


def quality(text: str) -> tuple[bool, str]:
    """返回 (是否可用, 不可用原因)。"""
    if not text:
        return False, "空"
    han = len(_HAN.findall(text))
    if han < MIN_CHARS:
        return False, f"中文仅 {han} 字（<{MIN_CHARS}）"
    ratio = han / max(1, len(text))
    if ratio < MIN_HAN_RATIO:
        return False, f"中文占比 {ratio:.2f}（<{MIN_HAN_RATIO}）"
    for b in _BAD_MARKERS:
        if b in text[:600]:
            return False, f"疑似拦截页：{b}"
    return True, ""


def extract(raw: str) -> tuple[str, str, str]:
    """从整页 HTML 里取正文。

    返回 (正文, 方法, 失败原因)。方法是给运维看的：同一个站连续用回退方法，
    说明它的模板变了，该补容器规则了。
    """
    if not raw:
        return "", "", "空响应"

    head = _html.unescape(_ANY_TAG.sub(" ", raw[:3000]))
    for b in _BAD_MARKERS:
        if b in head:
            return "", "", f"疑似拦截页：{b}"

    start, pat = _container_start(raw)
    if start >= 0:
        # 不设固定窗口。2026-10-20 修：原来切 40 万字符，而 gov.cn 老版页面
        # 整体 44.8 万字符、结尾锚点在第 43.6 万处——锚点落在窗口外，_cut_tail
        # 原样返回整段，40 万字里混入整页导航，中文占比掉到 0.08 被判失败。
        # 402 份失败全部是这个成因（页面越长越容易踩）。
        seg = _cut_tail(raw[start:])
        text = html_to_text(seg)
        # 容器可能只是正文的一部分（有的站正文分多个 div），够长就用
        ok, why = quality(text)
        if ok:
            # 没找到结尾锚点时正文后面可能还挂着整页导航，方法名标出来，
            # 免得「没截断」一直伪装成「提取成功」。
            cut = (len(seg) < len(raw) - start)
            return text, ("container" if cut else "container:nocut"), ""

    # 回退：整页剥标签。会带一点导航，但比抓空强。
    body = raw
    m = re.search(r"<body\b[^>]*>", raw, re.I)
    if m:
        body = raw[m.end():]
    text = html_to_text(_cut_tail(body))
    ok, why = quality(text)
    if ok:
        return text, "whole-page", ""
    return "", "", why


__all__ = ["extract", "html_to_text", "quality", "MIN_CHARS"]
