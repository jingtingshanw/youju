#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""目录层：抓取「这个方向有哪些文件」，不含全文。

解决的问题
----------
规划师接到一个新项目（比如鹰潭的城市更新专项），第一步是**发现**：这个方向
上有哪些文件——市里的、省里的、国家的，可能还要跨到历史文化保护。
这一步的判据是「不能漏」，所以它必须有覆盖度；而它真正需要的东西是**清单**，
不是每份文件的全文。

用法
----
    python scripts/catalog/crawl.py --region 鹰潭 --topic 城市更新
    python scripts/catalog/crawl.py --region 鹰潭 --topic 城市更新 --refresh
    python scripts/catalog/crawl.py --region 鹰潭 --topic 城市更新 --json
    python scripts/catalog/crawl.py --stats
    python scripts/catalog/crawl.py --sources
    python scripts/catalog/crawl.py --import data/catalog/江西省清单.csv
    python scripts/catalog/crawl.py --selftest

输出的每一行都带**效力状态**和**来源**；末尾一定有一段「覆盖度」，
写明哪一级抓到了、哪一级没抓到、为什么。抓不到就写抓不到，不留空白行。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_SELF = Path(__file__).resolve()
if __package__ in (None, ""):
    sys.path.insert(0, str(_SELF.parent.parent))
    __package__ = "catalog"

from catalog import adapters, model                       # noqa: E402
from catalog.model import CatalogItem, TOPICS             # noqa: E402
from catalog.store import Store                            # noqa: E402

ROOT = _SELF.parent.parent.parent
DEFAULT_MAX_AGE_H = 24.0

LEVEL_ORDER = {"国家": 0, "省级": 1, "市级": 2}


# ══════════════════════════════════════════════════════════════════════════
# 一、一次抓取
# ══════════════════════════════════════════════════════════════════════════

def _coverage(chain: list[str], reports: list[dict]) -> dict:
    """把来源报告聚合成「每一级覆盖到什么程度」。

    这是整个模块的产品核心：`missing` / `partial` 必须被说出来。
    """
    by: dict[str, dict] = {}
    for lv in ("国家", "省级", "市级"):
        rs = [r for r in reports if r["level"] == lv]
        if not rs:
            continue
        ok = [r for r in rs if r["status"] in ("ok", "empty", "skipped")]
        bad = [r for r in rs if r["status"] in ("blocked", "unavailable", "error")]
        st = "ok" if not bad else ("partial" if ok else "missing")
        by[lv] = {
            "status": st,
            "n_sources": len(rs),
            "n_ok": len(ok),
            "n_items": sum(r["count"] for r in rs),
        }
    gaps = [{"level": r["level"], "label": r["label"],
             "status": r["status"], "reason": r["reason"]}
            for r in reports if r["status"] in ("blocked", "unavailable", "error")]
    return {"by_level": by, "gaps": gaps,
            "levels_checked": [lv for lv in ("国家", "省级", "市级") if lv in by]}


def run(region: str = "", topic: str = "", keyword: str = "",
        *, refresh: bool = False, only: list[str] | None = None,
        pages: int = 3, pages_override: int = 0, max_per_source: int = 200,
        loose: bool = False, docs_only: bool = True,
        cfg_path: Path | str | None = None,
        store: Store | None = None) -> dict:
    """抓一次，落库，返回清单 + 来源报告 + 覆盖度。"""
    cfg = adapters.load_config(cfg_path or adapters.CONFIG_PATH)
    defaults = cfg.get("defaults", {})
    # 翻页数优先看每个源自己的配置（各站能承受的翻页深度不一样），
    # 没配才用这里的默认值。命令行 --pages 会强行覆盖所有源。
    max_per_source = int(defaults.get("max_per_source", max_per_source))

    chain, canon = adapters.region_chain(cfg, region)
    terms, kws = adapters.topic_terms(topic, keyword)
    q = adapters.Query(region=canon, topic=topic, keyword=keyword,
                       terms=terms, topic_kws=kws,
                       max_per_source=max_per_source, pages=pages,
                       loose=loose, docs_only=docs_only,
                       pages_override=pages_override)

    st = store or Store()
    reports: list[dict] = []
    collected: list[CatalogItem] = []

    for src in adapters.build_sources(cfg, only=only):
        if src.region and src.region not in chain:
            continue
        items, rep = src.fetch(q)
        collected.extend(items)
        reports.append(rep.to_dict())
        st.record_source(rep.name, rep.status, rep.count, rep.reason, rep.ms)

    # 与既有失效台账对齐：清单里最值钱的一列是「这条还有效吗」
    n_ledger = model.apply_ledger(collected)

    stats = st.upsert(collected)
    rows = st.query(regions=chain, topics=[topic] if topic else None,
                    keyword=keyword, limit=max_per_source * 2)

    cov = _coverage(chain, reports)
    # 覆盖度里的条数要按**去重后的实际清单**算。直接用各源条数相加会虚高：
    # 同一份国务院文件同时出现在政策库和部门库里，加出来是 4，清单只有 3 行，
    # 用户一眼就看出对不上。
    real: dict[str, int] = {}
    for it in rows:
        real[it.admin_level] = real.get(it.admin_level, 0) + 1
    for lv, v in cov["by_level"].items():
        v["n_items"] = real.get(lv, 0)

    st.record_run(canon, topic, keyword, len(rows), cov)

    return {
        "query": {"region": region, "region_canon": canon, "chain": chain,
                  "topic": topic, "keyword": keyword, "terms": terms},
        "items": [it.to_dict() for it in rows],
        "sources": sorted(reports, key=lambda r: (
            LEVEL_ORDER.get(r["level"], 9), r["name"])),
        "coverage": cov,
        "stats": {"fetched": len(collected), "new": stats["new"],
                  "merged": stats["merged"], "returned": len(rows),
                  "ledger_matched": n_ledger},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cached": False,
    }


def load_cached(region: str = "", topic: str = "", keyword: str = "",
                *, max_age_h: float = DEFAULT_MAX_AGE_H,
                limit: int = 400, store: Store | None = None,
                cfg_path: Path | str | None = None) -> dict | None:
    """读缓存。没有缓存 / 过期 / 查询条件变了就返回 None，由调用方决定要不要抓。"""
    cfg = adapters.load_config(cfg_path or adapters.CONFIG_PATH)
    chain, canon = adapters.region_chain(cfg, region)
    st = store or Store()
    last = st.last_run()
    if not last:
        return None
    fresh = (time.time() - (last.get("started_at") or 0)) < max_age_h * 3600
    same = (last.get("region") == canon and last.get("topic") == topic
            and last.get("keyword") == keyword)
    if not (fresh and same):
        return None
    rows = st.query(regions=chain, topics=[topic] if topic else None,
                    keyword=keyword, limit=limit)
    if not rows:
        return None
    states = st.source_states()
    reports = [{"name": s["source"], "label": s["source"],
                "level": _level_of_source(cfg, s["source"]),
                "region": "", "status": s["status"], "count": s["count"],
                "scanned": 0, "available": 0, "reason": s["reason"],
                "ms": s["ms"]} for s in states]
    return {
        "query": {"region": region, "region_canon": canon, "chain": chain,
                  "topic": topic, "keyword": keyword},
        "items": [it.to_dict() for it in rows],
        "sources": sorted(reports, key=lambda r: (
            LEVEL_ORDER.get(r["level"], 9), r["name"])),
        "coverage": last.get("coverage") or _coverage(chain, reports),
        "stats": {"returned": len(rows), "fetched": 0, "new": 0, "merged": 0},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                      time.localtime(last["started_at"] or 0)),
        "cached": True,
    }


def _level_of_source(cfg: dict, name: str) -> str:
    for sc in cfg.get("sources", []):
        if sc.get("name") == name:
            r = sc.get("region", "")
            if r.endswith(("省", "自治区")):
                return "省级"
            if r.endswith(("市", "州", "盟")):
                return "市级"
            return "国家"
    return "其他"


# ══════════════════════════════════════════════════════════════════════════
# 二、打印
# ══════════════════════════════════════════════════════════════════════════

LEVEL_LABEL = {"国家": "国家级", "省级": "省级", "市级": "市级"}
STATUS_LABEL = {
    "ok": "已覆盖", "empty": "已查无", "blocked": "未覆盖（解析不到）",
    "unavailable": "未覆盖（无接口）", "error": "失败", "skipped": "跳过",
    "partial": "部分覆盖", "missing": "缺失",
}
MARK = {"ok": "✔", "empty": "·", "blocked": "✗", "unavailable": "✗",
        "error": "!", "skipped": "·", "partial": "△", "missing": "✗"}


def render(res: dict) -> None:
    q = res["query"]
    cov = res["coverage"]
    print(f"\n清单 · {q['region'] or '不限地域'} × {q['topic'] or q.get('keyword') or '不限主题'}"
          f"　（{res['generated_at']}，{'缓存' if res['cached'] else '本次抓取'}）")
    print(f"地域向上展开：{' → '.join(q['chain'])}")
    print("─" * 78)

    if not res["items"]:
        print("（清单为空）")
    cur = None
    for it in res["items"]:
        lv = it["admin_level"]
        if lv != cur:
            cur = lv
            print(f"\n【{LEVEL_LABEL.get(lv, lv)}】")
        stale = it["status"] in model.STATUS_STALE
        flag = "废止" if stale else ("有效" if it["status"] == "现行有效" else "未核")
        flag = f"[{flag}]"
        doc = it["doc_no"] or "—"
        pub = it["published"] or "—"
        print(f"  {pub}  {flag:6s} {doc:18s} {it['title']}")
        if it["doc_type"]:
            print(f"{'':10s}{'':8s}{'':19s}└ {it['doc_type']}"
                  f"（依据：{it['doc_type_src'] or '—'}）"
                  + (f"　{it['note']}" if it["note"] else ""))

    print("\n" + "─" * 78)
    print("覆盖度")
    for lv in ("国家", "省级", "市级"):
        v = cov["by_level"].get(lv)
        if not v:
            print(f"  {LEVEL_LABEL[lv]:6s} —— 本次没有配置任何源")
            continue
        print(f"  {LEVEL_LABEL[lv]:6s} {MARK.get(v['status'], '?')} "
              f"{STATUS_LABEL.get(v['status'], v['status'])}"
              f"　{v['n_items']} 条　（{v['n_ok']}/{v['n_sources']} 个源可用）")

    src_rows = res["sources"]
    if src_rows:
        print("\n各源明细")
        for r in src_rows:
            avail = f"，源端命中 {r['available']}" if r.get("available") else ""
            print(f"  {MARK.get(r['status'], '?')} {r['label']}："
                  f"{STATUS_LABEL.get(r['status'], r['status'])}"
                  f"　收录 {r['count']} 条，扫描 {r['scanned']} 条{avail}"
                  f"　{r['ms']}ms")
            if r["reason"]:
                print(f"      {r['reason']}")

    if cov["gaps"]:
        print("\n未覆盖的部分（这些不是「没有文件」，是「这次没查到」）")
        for g in cov["gaps"]:
            print(f"  · {LEVEL_LABEL.get(g['level'], g['level'])}：{g['label']}"
                  f" —— {g['reason'] or STATUS_LABEL.get(g['status'])}")
        print("  补法：整理成 CSV 后 `--import 文件` 或放进 data/catalog/manual.csv"
              "（表头见 data/catalog/manual.csv.example）")

    s = res["stats"]
    print(f"\n本次：抓取 {s['fetched']} 条，新增 {s['new']}，合并 {s['merged']}，"
          f"台账对上 {s.get('ledger_matched', 0)}，返回 {s['returned']} 条")
    print("注：效力状态为「未核」表示没有证据支持有效或废止，不做有利假设。")


# ══════════════════════════════════════════════════════════════════════════
# 三、人工导入
# ══════════════════════════════════════════════════════════════════════════

def import_file(path: Path | str, store: Store | None = None) -> dict:
    """把一份 CSV / JSONL 清单导进库。用于省市级这类抓不到的源。"""
    p = Path(path).resolve()
    if not p.exists():
        return {"file": str(p), "status": "error", "reason": "文件不存在",
                "read": 0, "items": 0, "new": 0, "merged": 0}
    src = adapters.ManualSource({"name": "manual_import", "label": p.name,
                                 "path": str(p)})
    items, rep = src.fetch(adapters.Query(topic_kws=[], docs_only=False))
    st = store or Store()
    n = st.upsert(items)
    return {"file": str(p), "status": rep.status, "reason": rep.reason,
            "read": rep.scanned, "items": len(items), **n}


# ══════════════════════════════════════════════════════════════════════════
# 四、自检（不联网）
# ══════════════════════════════════════════════════════════════════════════

def selftest() -> int:
    import tempfile
    ok = fail = 0

    def chk(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}　{extra}")

    cfg = adapters.load_config()

    # 1. 地域展开
    chain, canon = adapters.region_chain(cfg, "鹰潭")
    chk("地域别名 → 规范名", canon == "鹰潭市", canon)
    chk("地域向上展开到省和国家", chain == ["鹰潭市", "江西省", "国家"], str(chain))

    # 2. 检索词与相关性词表
    terms, kws = adapters.topic_terms("城市更新")
    chk("主题 → 源端检索词", terms == ["城市更新"], str(terms))
    chk("主题 → 相关性词表含邻接领域", "老旧小区" in kws and "城中村" in kws)

    # 3. 文号与级别
    chk("文号归一（国发）", model.parse_doc_no("国务院关于印发X的通知") == "")
    chk("文号归一（正文里）",
        model.parse_doc_no("文号：国发〔2026〕12号") == "国发〔2026〕12号")
    lv, src = model.infer_doc_type("国务院", "关于印发X的通知", "国发〔2026〕12号")
    chk("文号 → 效力级别", lv == "国务院文件" and src == "文号", f"{lv}/{src}")
    lv2, _ = model.infer_doc_type("鹰潭市人民政府", "鹰潭市城市更新管理办法", "")
    chk("标题 → 效力级别（办法类存疑，标「规章或规范性文件」）",
        lv2 == "规章或规范性文件", lv2)
    lv3, _ = model.infer_doc_type("鹰潭市人民政府", "鹰潭市城市更新实施办法", "")
    chk("标题 → 效力级别（实施办法不算规章）", lv3 == "规范性文件", lv3)

    # 4. 地域推断
    chk("地域推断（市）", model.infer_region("鹰潭市人民政府")[0] == "鹰潭市")
    chk("地域推断（省）", model.infer_region("江西省人民政府")[0] == "江西省")
    chk("地域推断（国家）", model.infer_region("住房和城乡建设部")[0] == "国家")

    # 5. 主题打标（跨领域）
    t = model.detect_topics("关于加强城市更新中历史文化保护的通知")
    chk("主题多标签（城市更新 × 历史文化）",
        "城市更新" in t and "历史文化保护" in t, str(t))

    # 6. 文件 vs 新闻
    chk("文件类标题识别", adapters.looks_like_doc("2026年鹰潭市城市更新年度计划"))
    chk("文件类标题识别（末尾带年份括注）",
        adapters.looks_like_doc("鹰潭市中心城区城市更新专项规划（2026-2035年）"))
    chk("新闻类标题剔除", not adapters.looks_like_doc("第二批城市更新典型案例发布"))
    chk("议事新闻不判为地方性法规",
        model.infer_doc_type("", "李强主持召开国务院常务会议 审议通过《古树名木保护条例（草案）》")[0] == "")
    # 2026-10-19 审计发现的两类静默丢失，各补一条反向用例
    chk("标题尾部粘文号仍认作文件",
        adapters.looks_like_doc("国务院办公厅转发国家发展改革委、财政部"
                                "《关于规范实施政府和社会资本合作新机制的指导意见》"
                                "的通知国办函〔2023〕115号"))
    chk("转载形态「《X》印发」仍认作文件",
        adapters.looks_like_doc("《支持城市更新的规划与土地政策指引（2023版）》印发"))
    chk("单看「发布」不把新闻收进来",
        not adapters.looks_like_doc("第二批城市更新典型案例发布"))

    # 7. 日期
    chk("日期归一（点分）", model.parse_date("2026.05.28") == "2026-05-28")
    chk("日期归一（URL）",
        model.parse_date("/art/2026/6/5/art_19337_1602805.html") == "2026-06-05")

    # 8. 去重键：同标题不同标点应撞上
    a = CatalogItem(title="鹰潭市城市更新实施办法", doc_no="").normalize()
    b = CatalogItem(title="《鹰潭市城市更新实施办法》", doc_no="").normalize()
    chk("去重键抗标点差异", a.key == b.key, f"{a.key} vs {b.key}")

    # 9. 落库与查询
    with tempfile.TemporaryDirectory() as d:
        st = Store(Path(d) / "t.db")
        it = CatalogItem(title="鹰潭市城市更新实施办法", region="鹰潭市",
                         published="2026-03-01", source="t",
                         topics=["城市更新"]).normalize()
        st.upsert([it])
        got = st.query(regions=["鹰潭市", "江西省", "国家"], topics=["城市更新"])
        chk("落库后可按地域×主题查到", len(got) == 1, str(len(got)))
        # 同标题二次入库应合并而不是重复
        st.upsert([CatalogItem(title="鹰潭市城市更新实施办法", issuer="鹰潭市人民政府",
                               source="t2", topics=["城市更新"]).normalize()])
        got2 = st.query(regions=["鹰潭市"], topics=["城市更新"])
        chk("同文件二次入库合并", len(got2) == 1 and got2[0].issuer == "鹰潭市人民政府",
            f"{len(got2)} / {got2[0].issuer if got2 else ''}")
        st.close()

    # 9.2 跨源去重：同一份文件转载标题不同、一个有文号一个没有
    with tempfile.TemporaryDirectory() as d:
        s3 = Store(Path(d) / "b.db")
        try:
            s3.upsert([CatalogItem(
                title="自然资源部办公厅关于印发《支持城市更新的规划与土地政策指引"
                      "（2023版）》的通知", doc_no="自然资办发〔2023〕47号",
                region="国家", source="govcn", topics=["城市更新"]).normalize()])
            s3.upsert([CatalogItem(
                title="《支持城市更新的规划与土地政策指引（2023版）》印发",
                source="yingtan", topics=["城市更新"]).normalize()])
            got = s3.query(topics=["城市更新"], limit=10)
            chk("跨源同一文件按书名号合并（不重复、不串级）",
                len(got) == 1 and got[0].region == "国家",
                f"{len(got)} 条 / {got[0].region if got else ''}")
        finally:
            s3.close()

    # 9.5 人工导入（临时 CSV，验证省市级补录这条路能走通）
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.csv"
        p.write_text("名称,发文机关,地域,主题,效力状态,链接\n"
                     "江西省城市更新实施办法,江西省人民政府,江西省,城市更新,现行有效,http://example\n",
                     encoding="utf-8")
        s2 = Store(Path(d) / "i.db")
        try:
            r = import_file(p, s2)
        finally:
            s2.close()          # Windows 上不关掉，临时目录删不掉
        chk("人工清单可导入并按列映射", r["read"] == 1 and r["new"] == 1, str(r))

    # 10. 台账对齐
    led = model.load_ledger()
    if led:
        name, info = next(iter(led.items()))
        it = CatalogItem(title=name).normalize()
        model.apply_ledger([it], led)
        chk("台账覆盖效力状态", it.status == info["时效性"], it.status)

    # 11. 配置里的源都能造出来
    srcs = adapters.build_sources(cfg)
    chk("配置中的源可实例化", len(srcs) >= 4, str(len(srcs)))
    chk("每个源都有 name/label/region",
        all(getattr(s, "name", "") and getattr(s, "label", "") for s in srcs))

    print(f"\n目录层自检：{ok}/{ok + fail} 通过"
          + ("" if fail == 0 else f"，{fail} 项失败"))
    return 0 if fail == 0 else 1


# ══════════════════════════════════════════════════════════════════════════
# 五、命令行
# ══════════════════════════════════════════════════════════════════════════

def _opt(flag: str, default=None):
    a = sys.argv
    return a[a.index(flag) + 1] if flag in a else default


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    st = Store()

    if "--stats" in sys.argv:
        s = st.stats()
        print(f"目录库：{s['total']} 条")
        for k, title in (("by_level", "按行政层级"), ("by_status", "按效力状态"),
                         ("by_region", "按地域")):
            print(f"\n{title}")
            for kk, vv in (s[k] or {}).items():
                print(f"  {kk or '（未判定）':10s} {vv}")
        last = st.last_run()
        if last:
            print(f"\n最近一次抓取：{time.strftime('%Y-%m-%d %H:%M', time.localtime(last['started_at']))}"
                  f"　{last['region'] or '不限'} × {last['topic'] or '-'}"
                  f"　{last['n_items']} 条")
        return

    if "--sources" in sys.argv:
        cfg = adapters.load_config()
        states = {s["source"]: s for s in st.source_states()}
        for src in adapters.build_sources(cfg):
            s = states.get(src.name, {})
            print(f"{MARK.get(s.get('status'), '·')} {src.name:14s} "
                  f"{src.level:4s} {src.label}")
            if s:
                print(f"    {STATUS_LABEL.get(s['status'], s['status'])}"
                      f"　{s['count']} 条　{s['reason'] or ''}")
        return

    if "--align-ledger" in sys.argv:
        print(f"台账对齐：{st.align_ledger()} 条更新")
        return

    if "--import" in sys.argv:
        r = import_file(_opt("--import"), st)
        print(f"导入 {r['file']}：读到 {r['read']} 条，入库 {r['items']} 条"
              f"（新增 {r['new']}，合并 {r['merged']}）")
        if r["reason"]:
            print(f"  {r['reason']}")
        return

    region = _opt("--region", "") or ""
    topic = _opt("--topic", "") or ""
    keyword = _opt("--keyword", "") or ""
    if not (region or topic or keyword):
        print(__doc__)
        return

    res = None
    if "--refresh" not in sys.argv:
        res = load_cached(region, topic, keyword, store=st,
                          max_age_h=float(_opt("--max-age", DEFAULT_MAX_AGE_H)))
    if res is None:
        res = run(region, topic, keyword, refresh=True,
                  pages=int(_opt("--pages", 3)),
                  pages_override=int(_opt("--pages", 0) or 0),
                  max_per_source=int(_opt("--max", 200)),
                  loose="--loose" in sys.argv,
                  docs_only="--all" not in sys.argv,
                  only=_opt("--only", "").split(",") if _opt("--only") else None,
                  store=st)

    if "--json" in sys.argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        render(res)


if __name__ == "__main__":
    main()
