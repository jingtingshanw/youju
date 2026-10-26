#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 catalog.db 里的条目导出成可交付的目录（CSV + 一份覆盖报告）。

为什么要导出 CSV 而不只是留在库里：目录的价值在于「能被人翻」。
库只服务检索，CSV 服务「这个省到底有哪些文件」这种翻检，两者不能互相替代。

输出
----
data/catalog/export/
    目录_省级.csv
    目录_市级.csv
    目录_县级.csv
    目录_全部.csv
    覆盖报告.md

用法
----
    python scripts/harvest/export_catalog.py
    python scripts/harvest/export_catalog.py --level 市
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import time
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
DB = ROOT / "data" / "catalog.db"
OUT = ROOT / "data" / "catalog" / "export"

LEVELS = [("省级", "省"), ("市级", "市"), ("县级", "县")]
COLS = ["标题", "发文机关", "文号", "发布日期", "地域", "层级", "来源站", "链接"]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def export(level: str, path: Path) -> int:
    c = _conn()
    rows = c.execute(
        "SELECT title,issuer,doc_no,published,region,admin_level,source_name,url "
        "FROM items WHERE admin_level=? AND title<>'' "
        "ORDER BY region, published DESC, title", (level,)).fetchall()
    c.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([r["title"], r["issuer"], r["doc_no"], r["published"],
                        r["region"], r["admin_level"], r["source_name"], r["url"]])
    return len(rows)


def report() -> str:
    c = _conn()
    lines = ["# 地方政策文件目录 · 覆盖报告", "",
             f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]

    lines += ["## 一、目录条目", "", "| 层级 | 条目数 | 覆盖地域数 |", "|---|---:|---:|"]
    for lv, _ in LEVELS:
        n = c.execute("SELECT COUNT(*) FROM items WHERE admin_level=?",
                      (lv,)).fetchone()[0]
        reg = c.execute("SELECT COUNT(DISTINCT region) FROM items WHERE admin_level=?",
                        (lv,)).fetchone()[0]
        lines.append(f"| {lv} | {n:,} | {reg} |")

    lines += ["", "## 二、站点抓取状态", "",
              "`可抓` = 拿到了条目；`受阻` = 入口在但列表是脚本渲染 / 反爬；"
              "`不可达` = 站点打不开。三种状态必须分开看，"
              "「抓不到」不等于「这个地方没有文件」。", "",
              "| 层级 | 可抓 | 受阻 | 不可达 | 合计 | 可抓率 |", "|---|---:|---:|---:|---:|---:|"]
    for lv, _ in LEVELS:
        q = dict(c.execute(
            "SELECT status,COUNT(*) FROM local_site WHERE level=? GROUP BY status",
            (lv,)).fetchall())
        ok, bl, er = q.get("ok", 0), q.get("blocked", 0), q.get("error", 0)
        tot = ok + bl + er
        rate = f"{ok / tot * 100:.0f}%" if tot else "—"
        lines.append(f"| {lv} | {ok} | {bl} | {er} | {tot} | {rate} |")

    lines += ["", "## 三、条目最多的 15 个地域", "",
              "| 地域 | 条目数 |", "|---|---:|"]
    for r in c.execute("SELECT region,COUNT(*) n FROM items WHERE region<>'' "
                       "GROUP BY region ORDER BY n DESC LIMIT 15"):
        lines.append(f"| {r[0]} | {r[1]:,} |")

    lines += ["", "## 四、走专用接口的站", "",
              "通用列表解析对付不了前端渲染的站，这些站是逐个挖出数据接口接进来的。",
              "它们一条能抵几十个普通站——上海一个接口就是 10,608 条。", "",
              "| 站 | 接口 | 条目 |", "|---|---|---:|"]
    for name, kind in (("上海市", "POST /gwk/policy/page"),
                       ("福建省", "TRS WAS5 检索（按文号前缀分片）")):
        n = c.execute("SELECT COUNT(*) FROM items WHERE region=?", (name,)).fetchone()[0]
        lines.append(f"| {name} | {kind} | {n:,} |")

    n_law = c.execute("SELECT COUNT(*) FROM items WHERE source_type='official_catalog'"
                      ).fetchone()[0]
    lines += ["", "## 五、法律层", "",
              f"《现行有效法律目录》共 **{n_law} 条**（全国人大网 2026-08-28 版），"
              "只有名称和法律沿革，没有条文。它对「效力核验」解决的是最要紧的一问："
              "**这部法还在不在现行有效名单里**。", "",
              "一个实测例子：查《环境保护法》会落到「不在目录」——不是漏抓，"
              "2026 年《生态环境法典》通过后它已被替代。目录里同时能看到多地正在"
              "发「关于修改涉及生态环境法典规范性文件的决定」，两件事互相印证。", ""]

    n_nodate = c.execute("SELECT COUNT(*) FROM items WHERE COALESCE(published,'')=''"
                         ).fetchone()[0]
    tot = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # ── 字段完整度：库大了不等于库好用了，这几个数必须一起报 ──────────────
    def scalar(sql: str) -> int:
        try:
            return c.execute(sql).fetchone()[0] or 0
        except Exception:  # noqa: BLE001  表可能还没建
            return 0

    n_nodoc = scalar("SELECT COUNT(*) FROM items WHERE COALESCE(doc_no,'')=''")
    n_future = scalar("SELECT COUNT(*) FROM items WHERE published > date('now')")
    n_ft = scalar("SELECT COUNT(*) FROM fulltext WHERE status='ok'")
    n_ft_chars = scalar("SELECT COALESCE(SUM(chars),0) FROM fulltext WHERE status='ok'")
    n_ft_fail = scalar("SELECT COUNT(*) FROM fulltext WHERE status<>'ok'")
    n_meta = scalar("SELECT COUNT(*) FROM doc_meta")
    n_repealed = scalar("SELECT COUNT(*) FROM doc_meta WHERE eff_status='repealed'")
    # 国家级清单的真实条数 = 抓到正文的 + 抓正文失败的（失败的那批没有 doc_meta 行）
    n_national = scalar("SELECT COUNT(*) FROM fulltext")
    n_catalog_total = tot + n_national

    def pct(a: int, b: int) -> str:
        return f"{a / b * 100:.1f}%" if b else "—"

    lines += ["", "## 六、字段完整度与可引用性", "",
              "库的规模不等于可用性。下面这几个数决定「搜到的这一条能不能用」，"
              "必须和条目数一起看。", "",
              "| 指标 | 数量 | 占比 | 说明 |", "|---|---:|---:|---|",
              f"| 有文号 | {tot - n_nodoc:,} | {pct(tot - n_nodoc, tot)} | "
              "文号不在列表页上，在正文红头里；缺失时效力级别只能按标题后缀推断 |",
              f"| 有发布日期 | {tot - n_nodate:,} | {pct(tot - n_nodate, tot)} | "
              "无日期的条目无法排序新旧 |",
              f"| 日期为未来（脏数据） | {n_future:,} | {pct(n_future, tot)} | "
              "列表页日期解析错误，最大出现 2099 年 |",
              f"| 国家级有正文 | {n_ft:,} | {pct(n_ft, n_catalog_total)} | "
              f"{n_ft_chars / 10000:,.0f} 万字；相对全部记录（目录 + 国家级清单） |",
              f"| 正文抓取失败 | {n_ft_fail:,} | — | 抽样 60 份全是 1980—1990 年代扫描件 |",
              f"| 时效有标记 | {n_repealed:,} | {pct(n_repealed, n_meta)} | "
              "源端文末标注「已宣布失效」；其余为「未知」，**未知不等于有效** |",
              "",
              "**「能引」和「有」是两件事。** 目录层回答「有哪些文件」，全文层回答"
              "「这一条怎么写的」。界面上两者必须分开显示，否则用户会把"
              "「搜到一条目录」当成「读到一份文件」。", ""]

    lines += ["## 七、已知缺口", "",
              f"- 共 {tot:,} 条目录中，{n_nodate:,} 条没有发布日期"
              f"（{n_nodate / tot * 100:.0f}%），来自列表页不给日期的站点。",
              "- 目录只有标题与链接，没有正文。全文目前只覆盖国家级"
              "（18,608 份，见 kb.db）。",
              "- 人事任免、表彰、政协提案答复函这类有文号但不构成政策依据的条目"
              "已统一剔除（约 3,500 条），剔除规则是标题词表，见 `_NOISE_TITLE`。",
              "- 受阻站点的清单见 `data/catalog/local_sites.json`，"
              "每条都写了具体原因，可以按原因分类逐个补。", ""]
    c.close()
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", default="全部")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    for lv, short in LEVELS:
        if a.level not in ("全部", short, lv):
            continue
        p = OUT / f"目录_{lv}.csv"
        n = export(lv, p)
        total += n
        print(f"  {lv}  {n:6,d} 条  → {p.relative_to(ROOT)}")

    p = OUT / "目录_全部.csv"
    # 全部层级合并导出（地域排序，方便按地方翻）
    c = _conn()
    rows = c.execute("SELECT title,issuer,doc_no,published,region,admin_level,"
                     "source_name,url FROM items WHERE title<>'' "
                     "ORDER BY CASE admin_level WHEN '省级' THEN 1 "
                     "WHEN '市级' THEN 2 WHEN '县级' THEN 3 ELSE 0 END, "
                     "region, published DESC").fetchall()
    c.close()
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([r["title"], r["issuer"], r["doc_no"], r["published"],
                        r["region"], r["admin_level"], r["source_name"], r["url"]])
    print(f"  全部  {len(rows):6,d} 条  → {p.relative_to(ROOT)}")

    # 法律单独出一份。它和信息源不是一回事：这份是官方「现行有效」名录，
    # 能直接回答「这部法还在不在」；地方目录只能回答「哪个站发过它」。
    c = _conn()
    laws = c.execute("SELECT title,topics,published,summary,url FROM items "
                     "WHERE source_type='official_catalog' ORDER BY rowid").fetchall()
    c.close()
    if laws:
        lp = OUT / "法律_现行有效目录.csv"
        with lp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["法律名称", "法律部门", "最近修改年份", "沿革", "来源"])
            for r in laws:
                w.writerow([r["title"], r["topics"], (r["published"] or "")[:4],
                            r["summary"], r["url"]])
        print(f"  法律  {len(laws):6,d} 条  → {lp.relative_to(ROOT)}")

    rp = OUT / "覆盖报告.md"
    rp.write_text(report(), encoding="utf-8")
    print(f"\n报告 → {rp.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
