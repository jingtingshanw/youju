#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 Markdown 说明文档渲染成一份自包含的 HTML（不依赖任何第三方包）。

**为什么自己写转换器**：这个项目有一条"代码零第三方依赖"的约束 —— 校验层要能在
任何一台机器上跑起来。文档渲染属于同一类需求（别人拿到仓库就能重新生成），
用一个只认本仓库用到的语法子集的转换器，比引入一整条 Markdown 工具链更合适。

支持的语法（正好是本项目文档用到的那些）：
    标题 # ## ### ####    表格 | a | b |    代码块 ```    引用 >     分隔线 ---
    有序/无序列表           **加粗**          `行内代码`      [文字](#锚点)

标题锚点按 GitHub 的规则生成（小写、去标点、空格转短横线），因此文档里的目录链接
在 HTML 里也能直接跳转。

用法：
    python scripts/build_docs_html.py                     # 渲染 docs/*.md
    python scripts/build_docs_html.py docs/xxx.md         # 指定文件
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{
  --bg:#ffffff; --ink:#1c2024; --ink2:#525a63; --ink3:#868e97; --line:#e4e8ec;
  --panel:#f7f9fa; --accent:#2f6fd0; --code-bg:#f4f6f8; --quote:#f0f4fa;
}}
html[data-theme=dark]{{
  --bg:#16191d; --ink:#e7eaee; --ink2:#a9b2bb; --ink3:#79828c; --line:#2e343b;
  --panel:#1d2126; --accent:#6fa4e8; --code-bg:#1b1f24; --quote:#1b2531;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:15.5px;line-height:1.78;-webkit-font-smoothing:antialiased}}
.bar{{position:sticky;top:0;z-index:9;background:var(--bg);border-bottom:1px solid var(--line);
  padding:9px 20px;display:flex;gap:14px;align-items:center;font-size:13px}}
.bar .t{{font-weight:600;color:var(--ink2)}}
.bar a{{color:var(--accent);text-decoration:none}}
.bar button{{margin-left:auto;font:inherit;font-size:13px;cursor:pointer;padding:5px 12px;
  border:1px solid var(--line);border-radius:7px;background:var(--panel);color:var(--ink)}}
main{{max-width:900px;margin:0 auto;padding:26px 22px 90px}}
h1{{font-size:26px;margin:34px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--line)}}
h2{{font-size:21px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}}
h3{{font-size:17.5px;margin:26px 0 9px}}
h4{{font-size:15.5px;margin:20px 0 7px;color:var(--ink2)}}
p{{margin:10px 0}}
a{{color:var(--accent)}}
strong{{font-weight:650}}
code{{background:var(--code-bg);border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}}
pre{{background:var(--code-bg);border:1px solid var(--line);border-radius:9px;
  padding:14px 16px;overflow:auto}}
pre code{{background:none;border:none;padding:0;font-size:13px;line-height:1.65}}
blockquote{{margin:14px 0;padding:10px 16px;background:var(--quote);
  border-left:3px solid var(--accent);border-radius:0 8px 8px 0;color:var(--ink2)}}
blockquote p{{margin:5px 0}}
hr{{border:none;border-top:1px solid var(--line);margin:32px 0}}
table{{width:100%;border-collapse:collapse;margin:14px 0;font-size:14px;
  display:block;overflow-x:auto}}
th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}}
th{{background:var(--panel);font-weight:650;white-space:nowrap}}
ul,ol{{padding-left:24px;margin:10px 0}}
li{{margin:4px 0}}
.toc{{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px 20px;margin:18px 0}}
.toc ul{{list-style:none;padding-left:0;margin:0}}
@media print{{
  .bar{{display:none}} body{{font-size:11.5pt}}
  a{{color:inherit;text-decoration:none}} h2{{page-break-after:avoid}}
  table,pre,blockquote{{page-break-inside:avoid}}
}}
</style>
</head>
<body>
<div class="bar">
  <span class="t">{title}</span>
  <a href="#top">回到开头</a>
  <button id="theme">切换主题</button>
</div>
<main id="top">
{body}
</main>
<script>
(function(){{
  var k='pv-doc-theme';
  var saved=localStorage.getItem(k);
  document.documentElement.dataset.theme = saved ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.getElementById('theme').onclick=function(){{
    var n = document.documentElement.dataset.theme==='dark' ? 'light':'dark';
    document.documentElement.dataset.theme=n; localStorage.setItem(k,n);
  }};
}})();
</script>
</body>
</html>
"""


def slug(text: str) -> str:
    """GitHub 风格的锚点：小写、去标点、空格转短横线。"""
    s = re.sub(r"[*`]", "", text.strip()).lower()
    s = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", s)
    return s.replace(" ", "-")


def inline(text: str) -> str:
    """行内格式：先把行内代码切出来单独转义，其余部分转义后再还原加粗与链接。"""
    out = []
    for part in re.split(r"(`[^`]*`)", text):
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append("<code>" + html.escape(part[1:-1]) + "</code>")
            continue
        s = html.escape(part)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\[([^\]]+)\]\(#([^)]+)\)", r'<a href="#\2">\1</a>', s)
        out.append(s)
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    list_tag: str | None = None
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + inline(" ".join(para).strip()) + "</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    while i < n:
        ln = lines[i]
        s = ln.strip()

        # 代码块
        if s.startswith("```"):
            flush_para(); close_list()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue

        # 表格
        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            flush_para(); close_list()
            head = _cells(s)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i])); i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            tr = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>")
            continue

        # 分隔线
        if re.match(r"^-{3,}$", s):
            flush_para(); close_list(); out.append("<hr>"); i += 1; continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush_para(); close_list()
            lv = len(m.group(1)); txt = m.group(2).strip()
            out.append(f'<h{lv} id="{slug(txt)}">{inline(txt)}</h{lv}>')
            i += 1
            continue

        # 引用
        if s.startswith(">"):
            flush_para(); close_list()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip()); i += 1
            out.append("<blockquote>" + "".join(f"<p>{inline(b)}</p>" for b in buf if b) + "</blockquote>")
            continue

        # 列表
        m = re.match(r"^([-*])\s+(.*)$", s)
        m2 = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m or m2:
            flush_para()
            want = "ul" if m else "ol"
            if list_tag != want:
                close_list(); out.append(f"<{want}>"); list_tag = want
            out.append("<li>" + inline((m or m2).group(2)) + "</li>")
            i += 1
            continue

        # 空行
        if not s:
            flush_para(); close_list(); i += 1; continue

        para.append(s)
        i += 1

    flush_para(); close_list()
    return "\n".join(out)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    files = [Path(a) for a in args] if args else sorted(DOCS.glob("*.md"))
    if not files:
        raise SystemExit("[x] docs/ 下没有 .md 文件")
    for src in files:
        if not src.is_absolute():
            src = ROOT / src
        md = src.read_text(encoding="utf-8")
        title = next((l.lstrip("# ").strip() for l in md.split("\n") if l.startswith("# ")), src.stem)
        dst = src.with_suffix(".html")
        dst.write_text(TEMPLATE.format(title=html.escape(title), body=render(md)), encoding="utf-8")
        print(f"{src.name}  →  {dst.relative_to(ROOT)}   （{len(md)} 字符 → {dst.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
