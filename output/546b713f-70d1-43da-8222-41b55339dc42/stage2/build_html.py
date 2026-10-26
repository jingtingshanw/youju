#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_html.py —— 把 docs/项目总说明-v2.md 转成交付给 S3 的单个完整 HTML。

为什么用脚本而不是手写 HTML：
  1. 源文档 1693 行、101 个标题、484 行表格、23 个代码块——手写无法保证与源逐字一致；
  2. 源文档会继续更新（附录 D 由 build_file_index.py 自动重写），脚本可重跑；
  3. S2 的硬约束（宋体小四、text-indent、h1~h3、table 替代 grid、禁裸值）是确定性规则，
     脚本化才能逐条自检。

产出结构（依据 agents/doc-formatter.md §2.5）：
  <section role="cover">          封面（标题禁用 h1~h6，用 p + 大字号）
  <section role="cover">          nav.doc-toc 独占一节
  <section role="body" data-page-restart="1">   正文（executive-summary + main）

用法：
  python build_html.py --md <源 md> --out <目标 html> [--exec-summary <json>]
  python build_html.py --md <源 md> --check        # 只做结构自检，不写文件
"""

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# 0. 常量
# --------------------------------------------------------------------------- #

# md 顶部那段「## 目录」要被 nav.doc-toc 取代，整段丢弃
DROP_SECTION_TITLES = {"目录"}

# 跨章引用的内联锚点：md 用 GitHub slug，HTML 侧改用 id="section-N"
SLUG_KEEP = re.compile(r"[^\w\- ]", re.UNICODE)


def gh_slug(text: str) -> str:
    """GitHub 风格锚点：小写 → 去标点 → 空格转连字符。（与 md 现有内联链接同源）"""
    t = text.strip().lower()
    t = SLUG_KEEP.sub("", t)
    return t.replace(" ", "-")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# 1. 行内标记
# --------------------------------------------------------------------------- #
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def inline(text: str, slug2id: dict) -> str:
    """把一行 md 行内标记转成 HTML。先抽出 code span 占位，避免内部被二次处理。"""
    holds: list[str] = []

    def hold(html: str) -> str:
        holds.append(html)
        return "\x00%d\x00" % (len(holds) - 1)

    s = esc(text)

    # code span：内容已转义，直接包 <code>
    s = _CODE_SPAN.sub(lambda m: hold("<code>%s</code>" % m.group(1)), s)

    def link(m):
        label, url = m.group(1), m.group(2).strip()
        if url.startswith("#"):
            slug = url[1:]
            target = slug2id.get(slug)
            if target:
                return hold('<a href="#%s">%s</a>' % (target, label))
            # 锚点对不上（源文档改了标题）→ 降级为纯文本，不留死链
            return hold("<strong>%s</strong>" % label)
        return hold('<a href="%s">%s</a>' % (esc(url), label))

    s = _LINK.sub(link, s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)

    for i, h in enumerate(holds):
        s = s.replace("\x00%d\x00" % i, h)
    return s


# --------------------------------------------------------------------------- #
# 2. 块级解析
# --------------------------------------------------------------------------- #
class Block:
    __slots__ = ("kind", "level", "text", "rows", "lang", "items")

    def __init__(self, kind, **kw):
        self.kind = kind
        self.level = kw.get("level", 0)
        self.text = kw.get("text", "")
        self.rows = kw.get("rows", [])
        self.lang = kw.get("lang", "")
        self.items = kw.get("items", [])


_LIST_ITEM = re.compile(r"^( *)([-*+]|\d{1,2}\.)\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:\-|]+\|$")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


def parse_blocks(md: str) -> list[Block]:
    lines = md.split("\n")
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        # HTML 注释（附录 D 的注入标记）整行丢弃
        if line.strip().startswith("<!--"):
            i += 1
            continue

        # 代码块
        if line.lstrip().startswith("```"):
            lang = line.strip()[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(Block("code", text="\n".join(buf), lang=lang))
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            blocks.append(Block("head", level=len(m.group(1)), text=m.group(2).strip()))
            i += 1
            continue

        # 分隔线：丢弃（章节之间靠标题间距分隔，避免转换后出现满版横线）
        if _HR.match(line):
            i += 1
            continue

        # 表格
        if line.startswith("|"):
            rows = []
            while i < n and lines[i].rstrip().startswith("|"):
                r = lines[i].strip()
                if not _TABLE_SEP.match(r):
                    cells = [c.strip() for c in r.strip("|").split("|")]
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(Block("table", rows=rows))
            continue

        # 引用块 → callout
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            blocks.append(Block("quote", text="\n".join(buf)))
            continue

        # 列表
        if _LIST_ITEM.match(raw):
            items, i = parse_list(lines, i)
            blocks.append(Block("list", items=items))
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 段落：直到空行 / 块级起始
        buf = [line]
        i += 1
        while i < n:
            nxt = lines[i].rstrip()
            if (not nxt.strip() or nxt.startswith("|") or nxt.lstrip().startswith("```")
                    or re.match(r"^#{1,6}\s", nxt) or nxt.startswith(">")
                    or _LIST_ITEM.match(lines[i]) or _HR.match(nxt)
                    or nxt.strip().startswith("<!--")):
                break
            buf.append(nxt)
            i += 1
        blocks.append(Block("para", text="\n".join(buf)))
    return blocks


def parse_list(lines, i):
    """解析一个列表块，支持 2 空格递进的嵌套与多行条目。"""
    n = len(lines)
    items = []  # (indent, marker, text_lines)
    pending_blank = False
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        m = _LIST_ITEM.match(raw)
        if m:
            indent = len(m.group(1))
            ordered = m.group(2).endswith(".")
            items.append([indent, ordered, [m.group(3)]])
            pending_blank = False
            i += 1
            continue
        if not line.strip():
            # 空行：允许（松散列表），但若下一非空行不是列表项则结束
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and (_LIST_ITEM.match(lines[j])
                          or (lines[j].startswith(" ") and items)):
                pending_blank = True
                i = j
                continue
            break
        # 续行（缩进大于当前条目）
        if items and (raw.startswith("  ") or raw.startswith("\t")):
            items[-1][2].append(line.strip())
            i += 1
            continue
        break
    return items, i


# --------------------------------------------------------------------------- #
# 3. 渲染
# --------------------------------------------------------------------------- #
def render_table(rows, slug2id, caption=None) -> str:
    head = rows[0]
    body = rows[1:]
    out = ['<table>']
    if caption:
        out.append("<caption>%s</caption>" % inline(caption, slug2id))
    out.append("<thead><tr>%s</tr></thead>"
               % "".join("<th>%s</th>" % inline(c, slug2id) for c in head))
    out.append("<tbody>")
    for r in body:
        cells = r + [""] * (len(head) - len(r))
        out.append("<tr>%s</tr>"
                   % "".join("<td>%s</td>" % inline(c, slug2id) for c in cells[:len(head)]))
    out.append("</tbody></table>")
    return "".join(out)


def render_list(items, slug2id) -> str:
    """按缩进递进生成嵌套 ul/ol。"""
    out = []
    stack = []  # (indent, tag)
    for indent, ordered, tlines in items:
        tag = "ol" if ordered else "ul"
        text = " ".join(x.strip() for x in tlines if x.strip())
        while stack and indent < stack[-1][0]:
            out.append("</li></%s>" % stack.pop()[1])
        if not stack or indent > stack[-1][0]:
            out.append("<%s>" % tag)
            stack.append((indent, tag))
            out.append("<li>%s" % inline(text, slug2id))
        else:
            if stack[-1][1] != tag:
                out.append("</%s>" % stack[-1][1])
                stack.pop()
                out.append("<%s>" % tag)
                stack.append((indent, tag))
                out.append("<li>%s" % inline(text, slug2id))
            else:
                out.append("</li><li>%s" % inline(text, slug2id))
    while stack:
        out.append("</li></%s>" % stack.pop()[1])
    return "".join(out)


def split_long_para(text: str, limit: int = 460) -> list[str]:
    """单段超长（>500 字上限）时按句号切分，避免 docx 里出现巨型段落。"""
    plain = re.sub(r"[*`\[\]()]", "", text)
    if len(re.sub(r"\s", "", plain)) <= limit:
        return [text]
    parts = re.split(r"(?<=[。！？；])", text)
    chunks, cur = [], ""
    for p in parts:
        if cur and len(re.sub(r"\s", "", cur + p)) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        chunks.append(cur)
    return chunks


def strip_index_section(blocks: list[Block]) -> list[Block]:
    """丢掉 md 自带的「## 目录」整节（由 nav.doc-toc 取代）。"""
    out, skip_level = [], None
    for b in blocks:
        if b.kind == "head":
            if b.level >= 2 and b.text.strip() in DROP_SECTION_TITLES:
                skip_level = b.level
                continue
            if skip_level is not None and b.level <= skip_level:
                skip_level = None
            elif skip_level is not None:
                continue
        if skip_level is not None:
            continue
        out.append(b)
    return out


def build_slug_map(blocks: list[Block]) -> dict[str, str]:
    """md 标题 → id="section-N"（N 按文档顺序递增）。"""
    m = {}
    k = 0
    for b in blocks:
        if b.kind != "head":
            continue
        k += 1
        m.setdefault(gh_slug(b.text), "section-%d" % k)
    return m


def render_body(blocks, slug2id, first_heading_is_title: bool):
    """正文渲染。返回 (html, toc_entries, stats)。"""
    out, toc, stats = [], [], {"h1": 0, "h2": 0, "h3": 0, "tables": 0, "code": 0, "p": 0}
    k = 0
    open_chapter = False  # 是否已进入某个 h1 章
    for idx, b in enumerate(blocks):
        if b.kind == "head":
            k += 1
            hid = "section-%d" % k
            lv = b.level
            if first_heading_is_title and idx == 0:
                # 文档主标题：已在封面表达，正文不再重复
                continue
            if lv == 1:
                out.append("</div>" if open_chapter else "")
                out.append('<div class="chapter">')
                open_chapter = True
            tag = "h%d" % min(lv, 3) if lv <= 3 else "h3"
            out.append('<%s id="%s">%s</%s>' % (tag, hid, inline(b.text, slug2id), tag))
            stats[tag] = stats.get(tag, 0) + 1
            if lv == 1:
                toc.append(("h1", b.text, hid, []))
            elif lv == 2 and toc:
                toc[-1][3].append((b.text, hid))
            elif lv == 2:
                toc.append(("h1", b.text, hid, []))
            continue

        if b.kind == "para":
            for chunk in split_long_para(b.text):
                out.append("<p>%s</p>" % inline(chunk, slug2id))
                stats["p"] += 1
            continue

        if b.kind == "table":
            out.append(render_table(b.rows, slug2id))
            stats["tables"] += 1
            continue

        if b.kind == "code":
            out.append('<pre><code>%s</code></pre>' % esc(b.text))
            stats["code"] += 1
            continue

        if b.kind == "quote":
            paras = [x for x in b.text.split("\n") if x.strip()]
            inner = "".join("<p>%s</p>" % inline(x, slug2id) for x in paras)
            out.append('<div data-component="callout" data-variant="info">'
                       '<div class="callout-content">%s</div></div>' % inner)
            continue

        if b.kind == "list":
            out.append(render_list(b.items, slug2id))
            continue

    if open_chapter:
        out.append("</div>")
    return "".join(out), toc, stats


# --------------------------------------------------------------------------- #
# 4. 样式（所有颜色/字号/间距均为自定义属性，禁止裸值）
# --------------------------------------------------------------------------- #
CSS = """
@page { @bottom-center { content: counter(page); } }
@page cover { @bottom-center { content: none; } }
section[role="cover"] { page: cover; }

:root {
  /* 字体：正文第一项必须是 SimSun / 宋体（转换层取第一项作 Word eastAsia 字体） */
  --typography-fontFamily-heading: "SimHei", "黑体", "Microsoft YaHei", sans-serif;
  --typography-fontFamily-body: "SimSun", "宋体", "Microsoft YaHei", serif;
  --typography-fontFamily-mono: "Consolas", "Courier New", monospace;
  /* 字号：小四 = 12pt 为正文基准 */
  --typography-fontSize-title: 26pt;
  --typography-fontSize-subtitle: 13pt;
  --typography-fontSize-h1: 16pt;
  --typography-fontSize-h2: 14pt;
  --typography-fontSize-h3: 12pt;
  --typography-fontSize-body: 12pt;
  --typography-fontSize-small: 10.5pt;
  --typography-fontSize-code: 10pt;
  --typography-lineHeight-body: 1.65;
  --typography-lineHeight-heading: 1.4;
  --typography-fontWeight-heading: 700;
  --typography-fontWeight-body: 400;
  --typography-fontWeight-bold: 700;
  /* 颜色 */
  --color-primary: #1565C0;
  --color-secondary: #0288D1;
  --color-text: #212121;
  --color-muted: #5F6368;
  --color-border: #C8CDD3;
  --color-bg: #FFFFFF;
  --color-highlight: #F5F7FA;
  --color-emphasis: #C62828;
  --color-callout-bg: #EEF4FB;
  --color-callout-border: #B7D0EA;
  --color-code-bg: #F4F6F8;
  --color-code-border: #DDE2E7;
  --color-thead-bg: #EDF2F9;
  /* 间距：全部先落自定义属性，正文只引用 var() */
  --space-tiny: 0.25em;
  --space-paragraph: 0.7em;
  --space-block: 0.9em;
  --space-card: 1.1em;
  --space-cell: 0.45em;
  --space-section: 1.8em;
  --hairline: 1px;
  --rule-weight: 2px;
  /* 版面 */
  --layout-contentWidth: 15.6cm;
  --layout-pageMargin: 2.5cm;
  /* 本地别名（DT-05 要求 :root 内出现 --fs-body / --color-text / --ff-body） */
  --fs-title: var(--typography-fontSize-title);
  --fs-subtitle: var(--typography-fontSize-subtitle);
  --fs-h1: var(--typography-fontSize-h1);
  --fs-h2: var(--typography-fontSize-h2);
  --fs-h3: var(--typography-fontSize-h3);
  --fs-body: var(--typography-fontSize-body);
  --fs-small: var(--typography-fontSize-small);
  --fs-code: var(--typography-fontSize-code);
  --ff-heading: var(--typography-fontFamily-heading);
  --ff-body: var(--typography-fontFamily-body);
  --ff-mono: var(--typography-fontFamily-mono);
  --lh-body: var(--typography-lineHeight-body);
  --lh-heading: var(--typography-lineHeight-heading);
  --fw-bold: var(--typography-fontWeight-bold);
  --fw-normal: var(--typography-fontWeight-body);
  --spacing-paragraph: var(--space-paragraph);
  --spacing-block: var(--space-block);
  --spacing-section: var(--space-section);
  --spacing-tiny: var(--space-tiny);
  --margin-page: var(--layout-pageMargin);
  --page-content-width: var(--layout-contentWidth);
}

* { box-sizing: border-box; }
body {
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  color: var(--color-text);
  background: var(--color-bg);
  max-width: var(--page-content-width);
  margin-left: auto;
  margin-right: auto;
}

/* 标题：每级都显式声明关键属性，不依赖继承 */
h1, h2, h3, p, li, td, th {
  font-family: var(--ff-body);
  text-indent: 0;
}
h1 {
  font-family: var(--ff-heading);
  font-size: var(--fs-h1);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  line-height: var(--lh-heading);
  text-indent: 0;
  margin: var(--spacing-section) 0 var(--spacing-block) 0;
  padding-bottom: var(--spacing-tiny);
  border-bottom: var(--rule-weight) solid var(--color-primary);
}
h2 {
  font-family: var(--ff-heading);
  font-size: var(--fs-h2);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  line-height: var(--lh-heading);
  text-indent: 0;
  margin: var(--spacing-section) 0 var(--spacing-block) 0;
}
h3 {
  font-family: var(--ff-heading);
  font-size: var(--fs-h3);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  line-height: var(--lh-heading);
  text-indent: 0;
  margin: var(--spacing-block) 0 var(--spacing-paragraph) 0;
}
p {
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  text-indent: 2em;
  margin: 0 0 var(--spacing-paragraph) 0;
}
.chapter { margin-bottom: var(--spacing-section); }

/* 列表：正文缩进不进列表 */
ul, ol { margin: 0 0 var(--spacing-block) 0; padding-left: var(--spacing-section); text-indent: 0; }
li { font-size: var(--fs-body); text-indent: 0; margin-bottom: var(--space-tiny); }
ul ul, ul ol, ol ul, ol ol { margin-bottom: 0; }

/* 表格 */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-small);
  margin: 0 0 var(--spacing-block) 0;
  text-indent: 0;
}
caption {
  font-family: var(--ff-heading);
  font-size: var(--fs-small);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  text-align: left;
  text-indent: 0;
  margin-bottom: var(--spacing-tiny);
}
th, td {
  border: var(--hairline) solid var(--color-border);
  padding: var(--space-cell);
  font-size: var(--fs-small);
  text-align: left;
  vertical-align: top;
  text-indent: 0;
}
th { background: var(--color-thead-bg); font-weight: var(--fw-bold); }

/* 代码 */
pre {
  font-family: var(--ff-mono);
  font-size: var(--fs-code);
  line-height: var(--lh-heading);
  background: var(--color-code-bg);
  border: var(--hairline) solid var(--color-code-border);
  padding: var(--spacing-block);
  margin: 0 0 var(--spacing-block) 0;
  text-indent: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
code { font-family: var(--ff-mono); font-size: var(--fs-code); }
p code, li code, td code, th code { background: var(--color-code-bg); }

/* 强调数据 */
.data-emphasis { color: var(--color-emphasis); font-weight: var(--fw-bold); }

/* 提示块：用整框而非左边框（左边框在 Word 里文字会紧贴竖线） */
[data-component="callout"] {
  background: var(--color-callout-bg);
  border: var(--hairline) solid var(--color-callout-border);
  padding: var(--spacing-block);
  margin: 0 0 var(--spacing-block) 0;
}
.callout-content p:last-child { margin-bottom: 0; }

/* 封面 */
.report-cover { padding: var(--spacing-section) var(--margin-page); }
.cover-category {
  font-family: var(--ff-heading);
  font-size: var(--fs-h3);
  font-weight: var(--fw-bold);
  color: var(--color-secondary);
  letter-spacing: 0.06em;
  text-indent: 0;
  margin-bottom: var(--spacing-block);
}
.cover-title {
  font-family: var(--ff-heading);
  font-size: var(--fs-title);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  line-height: var(--lh-heading);
  text-indent: 0;
  margin-bottom: var(--spacing-block);
}
.cover-subtitle {
  font-family: var(--ff-body);
  font-size: var(--fs-subtitle);
  color: var(--color-muted);
  text-indent: 0;
  margin-bottom: var(--spacing-section);
}
.cover-meta {
  font-family: var(--ff-body);
  font-size: var(--fs-small);
  color: var(--color-muted);
  text-indent: 0;
}
.cover-note {
  font-family: var(--ff-body);
  font-size: var(--fs-small);
  color: var(--color-text);
  text-indent: 0;
  margin-top: var(--spacing-section);
}

/* 目录 */
.toc-page { padding: var(--spacing-section) var(--margin-page); }
.doc-toc { text-indent: 0; }
.toc-title {
  font-family: var(--ff-heading);
  font-size: var(--fs-h2);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  text-indent: 0;
  margin-bottom: var(--spacing-block);
}
.toc-list, .toc-list ol, .toc-list ul { list-style: none; list-style-type: none; padding-left: var(--spacing-block); }
.toc-list li { font-size: var(--fs-small); text-indent: 0; margin-bottom: var(--space-tiny); }
.toc-list a { color: var(--color-text); text-decoration: none; }
.toc-list > li > a { font-weight: var(--fw-bold); }

/* 执行摘要 */
.executive-summary {
  background: var(--color-callout-bg);
  border: var(--hairline) solid var(--color-callout-border);
  padding: var(--spacing-card);
  margin: 0 0 var(--spacing-section) 0;
}
.summary-heading {
  font-family: var(--ff-heading);
  font-size: var(--fs-h2);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  text-indent: 0;
  margin-bottom: var(--spacing-block);
}
.summary-metrics { margin-bottom: var(--spacing-block); }
.summary-metrics td { font-size: var(--fs-small); text-align: center; }
.summary-metrics .metric-value {
  font-family: var(--ff-heading);
  font-size: var(--fs-h2);
  font-weight: var(--fw-bold);
  color: var(--color-primary);
  text-indent: 0;
}
.summary-metrics .metric-label { color: var(--color-muted); text-indent: 0; }

/* 文档信息表 */
.doc-info { margin-bottom: var(--spacing-section); }
.doc-info th { width: 22%; }
"""


# --------------------------------------------------------------------------- #
# 5. 封面 / 摘要 / 目录
# --------------------------------------------------------------------------- #
COVER = """<section role="cover" class="report-cover">
<p class="cover-category" style="text-align: left; text-indent: 0;">项目总说明 · 完整版 v2</p>
<p class="cover-title" style="text-align: left; text-indent: 0;">政策效力核验与<br>可溯源引用引擎</p>
<p class="cover-subtitle" style="text-align: left; text-indent: 0;">把「有依据的回答」与「看起来有依据的回答」分开</p>
<p class="cover-meta" style="text-align: left; text-indent: 0;">8 部法规 · 419 个可检索单元 · 66 道评估题 · 两道确定性闸门</p>
<p class="cover-note" style="text-align: left; text-indent: 0;">文档版本 v2（截至 2026-10-15）<br>本地可运行 · 有实测数据 · 有产品校验层</p>
</section>
"""

SUMMARY = """<div class="executive-summary">
<p class="summary-heading">执行摘要</p>
<table class="summary-metrics">
<thead><tr><th>语料规模</th><th>评估题数</th><th>闸门 2 自检</th><th>最小 MVP 依赖</th></tr></thead>
<tbody><tr>
<td><span class="metric-value">419</span><br><span class="metric-label">个可检索单元</span></td>
<td><span class="metric-value">66</span><br><span class="metric-label">道题全部有实测</span></td>
<td><span class="metric-value">11 / 11</span><br><span class="metric-label">组自检全通过</span></td>
<td><span class="metric-value">0</span><br><span class="metric-label">第三方依赖</span></td>
</tr></tbody>
</table>
<p>这个系统回答政策法规类问题，并且<b>不允许回答超出所问法规范围的内容</b>。它的核心不是检索得更准，而是在答案出口处设两道确定性闸门——话题级与条文级——逐条核验模型自报的法规名、条号、时效与边界，核验不过就拦截。</p>
<p>全篇守住一条规矩：<span class="data-emphasis">凡是写出来的能力，都必须能指到一次实测或一个数据文件</span>；指不到的地方明确写成「未验证」，未验证项不得当作已证明的能力使用。截至本文档，<span class="data-emphasis">最大的两处未验证</span>是：语料是按出题需要拼的、还没有真实用户跑过。</p>
</div>
"""


def build_toc(toc) -> str:
    parts = ['<nav class="doc-toc" aria-label="文档目录">',
             '<p class="toc-title">目录</p>', '<ol class="toc-list">']
    for _lv, title, hid, subs in toc:
        parts.append('<li><a href="#%s">%s</a>' % (hid, esc(title)))
        if subs:
            parts.append("<ol>")
            for st, sid in subs:
                parts.append('<li><a href="#%s">%s</a></li>' % (sid, esc(st)))
            parts.append("</ol>")
        parts.append("</li>")
    parts.append("</ol></nav>")
    return "".join(parts)


def build_doc_info(rows) -> str:
    body = "".join("<tr><th>%s</th><td>%s</td></tr>" % (esc(a), inline(b, {})) for a, b in rows)
    return ('<div class="doc-info"><table class="doc-info-table">'
            '<thead><tr><th>项</th><th>内容</th></tr></thead>'
            '<tbody>%s</tbody></table></div>' % body)


# --------------------------------------------------------------------------- #
# 6. 主流程
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    md = Path(args.md).read_text(encoding="utf-8")

    # 顶部信息表（md 第 3~10 行）单独抽出，正文不再重复
    info_rows = []
    m = re.search(r"^\| 项 \| 内容 \|\n\|[-|]+\|\n((?:\|.*\|\n)+)", md, re.M)
    if m:
        for line in m.group(1).strip().split("\n"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                info_rows.append(cells[:2])
        md = md.replace(m.group(0), "")

    blocks = parse_blocks(md)
    blocks = strip_index_section(blocks)
    slug2id = build_slug_map(blocks)

    body_html, toc, stats = render_body(blocks, slug2id, first_heading_is_title=True)

    html = "\n".join([
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '<meta name="docx-page-size" content="A4">',
        "<title>政策效力核验与可溯源引用引擎 · 项目总说明 v2</title>",
        "<style>%s</style>" % CSS,
        "</head>",
        "<body>",
        COVER,
        '<section role="cover" class="toc-page">%s</section>' % build_toc(toc),
        '<section role="body" class="report-body" data-page-restart="1">',
        SUMMARY,
        build_doc_info(info_rows),
        "<main>%s</main>" % body_html,
        "</section>",
        "</body>",
        "</html>",
    ])

    unresolved = [s for s in re.findall(r"\]\(#([^)]+)\)", Path(args.md).read_text(encoding="utf-8"))
                  if s not in slug2id]
    report = {
        "toc_chapters": len(toc),
        "toc_entries": sum(1 + len(s) for _l, _t, _h, s in toc),
        "stats": stats,
        "info_rows": len(info_rows),
        "unresolved_anchors": sorted(set(unresolved)),
        "bytes": len(html.encode("utf-8")),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.check:
        return 0
    if not args.out:
        sys.stderr.write("需要 --out 或 --check\n")
        return 2
    Path(args.out).write_text(html, encoding="utf-8")
    print("written:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
