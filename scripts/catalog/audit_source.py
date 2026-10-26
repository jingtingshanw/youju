#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""源端收录率审计：把「命中 N 条、只收了 M 条」的差额逐条摊开。

为什么需要这个脚本
------------------
目录层的适配器里有三道过滤：`looks_like_doc`（是不是文件）、`relevant`（是否命中
主题）、`max_per_source`（上限）。三条叠加之后，报告只给一个数字「收录 3 条」，
而源端自报 276 条。**这 273 条差额到底是什么，决定了目录层能不能对外承诺覆盖。**

一份没核过的差额，写法上只能写「未核」；核过之后它要么变成「误杀率 2%，可用」，
要么变成「误杀率 40%，这个源废了」。两种结果都让判断前进，只有「没核」不能。

判据
----
不判断「这条新闻重不重要」，只判断一件事：**它是不是一份可被引用的文件**。
分四类，互斥：

    kept        已经进库的
    not_doc     标题不像文件（新闻、公示、招聘、解读…）—— 应当丢弃
    off_topic   像文件但不命中主题词 —— 应当丢弃
    suspect     **像文件、也沾主题，却被丢掉了** —— 这才是要人看的部分

`suspect` 只是「值得看一眼」，不等于误杀。脚本把它们的分数和理由打出来，
人再过一遍，才能得出误杀率。**脚本不做最后判断，只把人工核对从 276 条压缩到 20 条。**

用法
----
    python scripts/catalog/audit_source.py --source yingtan --topic 城市更新
    python scripts/catalog/audit_source.py --source yingtan --topic 城市更新 --pages 23 --json
    python scripts/catalog/audit_source.py --source yingtan --topic 城市更新 --show 40
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net, store                                    # noqa: E402
from catalog.adapters import (_ART_URL_RE, _region_of, looks_like_doc,  # noqa: E402
                              strip_tags, topic_terms)
from catalog.model import CatalogItem, detect_topics, title_core  # noqa: E402

SITES = HERE / "sites.json"

# 比 `looks_like_doc` 宽一档的后缀：用来发现「本该被认成文件、却没被认出来」的标题。
# 注意这里故意包含 looks_like_doc 没有的『函 / 表 / 台账 / 请示 / 复函 / 通告单』，
# 因为真实公文里这几类恰恰常见于省市站。
WIDE_SUFFIX = ("通知", "办法", "意见", "方案", "规定", "决定", "规划", "计划",
               "细则", "指南", "标准", "条例", "规则", "批复", "通告", "公告",
               "要点", "清单", "目录", "措施", "政策", "函", "表", "台账",
               "请示", "报告", "纲要", "规程", "规范", "导则", "指引")

# 明显的新闻/事务性标题特征。命中就往下压，不直接排除——
# 因为「关于印发…的通知」也可能出现在一条会议新闻的标题里。
NEWSY = ("召开", "会议", "调研", "考察", "座谈", "发布会", "解读", "图解", "访谈",
         "招聘", "中标", "比选", "公示", "深入学习贯彻", "强调", "指出", "出席",
         "主持", "视察", "督导", "慰问", "致贺", "署名文章", "转载", "来源",
         "答复", "回复", "咨询", "热线", "征集意见", "意见征集", "投票", "问卷",
         "名单", "表扬", "表彰", "培训", "讲座", "论坛", "展览", "比赛", "评选")


def _score(title: str) -> tuple[int, list[str]]:
    """给一条被丢弃的标题打分，越高越像「被误杀的文件」。"""
    s, why = 0, []
    core = title_core(title)
    if "《" in title:
        s += 3; why.append("含书名号")
    if any(k in title for k in ("印发", "批转", "转发", "颁布", "发布")):
        s += 2; why.append("含印发/转发")
    if core.endswith(WIDE_SUFFIX):
        s += 3; why.append(f"以「{[k for k in WIDE_SUFFIX if core.endswith(k)][0]}」结尾")
    if re.search(r"关于.{2,40}的(通知|意见|批复|决定|函|方案|报告)", core):
        s += 2; why.append("「关于…的X」公文句式")
    if re.match(r"^.{2,20}(市|省|县|区|局|委员会|政府|办公室)", title):
        s += 1; why.append("以机关名开头")
    hit = [w for w in NEWSY if w in title]
    if hit:
        s -= 3; why.append("新闻词：" + "/".join(hit[:2]))
    if len(core) > 42:
        s -= 2; why.append("标题过长")
    if len(core) < 8:
        s -= 1; why.append("标题过短")
    return s, why


def _load_source(name: str) -> dict:
    cfg = json.loads(SITES.read_text(encoding="utf-8"))
    for s in cfg["sources"]:
        if s.get("name") == name:
            return s
    raise SystemExit(f"配置里没有名为 {name} 的源；可用：" +
                     ", ".join(s["name"] for s in cfg["sources"]))


def audit(name: str, topic: str, pages: int, max_items: int) -> dict:
    cfg = _load_source(name)
    if cfg.get("type") != "jsearch":
        raise SystemExit(f"{name} 的类型是 {cfg.get('type')}，本脚本目前只审 jsearch 形态")

    base = cfg["base"].rstrip("/")
    endpoint = cfg.get("endpoint", "/jsearchfront/interfaces/cateSearch.do")
    interval = cfg.get("interval")
    _, topic_kws = topic_terms(topic)
    topic_kws = [k for k in topic_kws if k]

    st = store.Store()
    in_db = set()
    try:
        rows = st.query(regions=[cfg["region"]] if cfg.get("region") else None,
                        topics=[topic], limit=2000)
        for it in rows:
            in_db.add(re.sub(r"\s+", "", it.title))
    finally:
        st.close()

    total = 0
    scanned = 0
    kept, not_doc, off_topic, off_other, suspect, missed = [], [], [], [], [], []
    t0 = time.perf_counter()

    for page in range(1, pages + 1):
        try:
            d = net.get_json(base + endpoint,
                             {"websiteid": str(cfg["websiteid"]), "q": topic, "p": page},
                             referer=base + "/", encoding="utf-8", interval=interval)
        except net.FetchError as e:
            print(f"  ! p{page} 失败：{e}", file=sys.stderr)
            break
        frags = d.get("result") or []
        if not frags:
            break
        try:
            total = max(total, int(d.get("total") or 0))
        except (TypeError, ValueError):
            pass

        for frag in frags:
            scanned += 1
            m = re.search(r'class="jcse-news-title"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>'
                          r'(.*?)</a>', frag, re.S)
            if not m:
                off_other.append({"title": "(解析失败)", "why": "标题正则没匹配"})
                continue
            title = strip_tags(m.group(2))
            if len(title) < 6:
                off_other.append({"title": title, "why": "标题过短"})
                continue

            real = m.group(1)
            if "url=" in real:
                for blob in (real, real.split("?", 1)[-1]):
                    u = re.search(r"[?&]url=([^&]+)", blob)
                    if u:
                        import urllib.parse
                        cand = urllib.parse.unquote(u.group(1))
                        if cand.startswith("http"):
                            real = cand
                            break
            dm = _ART_URL_RE.search(real) if real else None
            published = (f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-"
                         f"{int(dm.group(3)):02d}") if dm else ""

            it = CatalogItem(title=title, published=published, url=real,
                             source=name, source_name=cfg.get("label", name)).normalize()
            it.region, it.region_src = _region_of(it, cfg.get("region") or "")
            topics = detect_topics(it.title)

            rec = {"title": title, "core": title_core(title), "date": published,
                   "url": real, "topics": topics}

            if re.sub(r"\s+", "", title) in in_db:
                kept.append(rec)
                continue
            if not looks_like_doc(title):
                sc, why = _score(title)
                rec.update(score=sc, why=why)
                if sc >= 3:
                    suspect.append(rec)
                else:
                    not_doc.append(rec)
                continue
            # 关键：判「是不是本主题的文件」，用本次主题的词表；
            # 用「命中任意主题」会把「土地管理 / 规划管理」的邻接文件全算成误杀。
            on_topic = any(k in title for k in topic_kws)
            if not on_topic:
                off_topic.append(rec)
                continue
            # 像文件、也命中主题，却没进库 → 只能是 max_per_source 或去重丢了
            sc, why = _score(title)
            rec.update(score=sc, why=why + ["像文件且命中本主题，但未入库"])
            missed.append(rec)
            if len(missed) + len(suspect) >= max_items * 3:
                break

    ms = int((time.perf_counter() - t0) * 1000)
    # 源端检索结果本身有重复（同一份文件挂在多个栏目/被多次转载）。
    # 不去重就会把「276 条命中」当成「276 份文件」，进而把收录率算低一个量级。
    every = kept + not_doc + off_topic + off_other + suspect + missed
    uniq = {re.sub(r"[\s\u3000（）()《》〈〉“”‘’\"'·、，,。.：:；;—\-－_]+", "", x["core"])
            for x in every}
    return {
        "source": name, "label": cfg.get("label"), "region": cfg.get("region"),
        "topic": topic, "pages": pages, "ms": ms,
        "reported_total": total, "scanned": scanned, "unique": len(uniq),
        "dup": scanned - len(uniq),
        "kept": len(kept), "not_doc": len(not_doc), "off_topic": len(off_topic),
        "off_other": len(off_other), "suspect": len(suspect), "missed": len(missed),
        "suspect_rate_of_scanned": round(len(suspect) / scanned * 100, 1) if scanned else 0,
        "_kept": kept, "_suspect": sorted(suspect, key=lambda r: -r["score"]),
        "_missed": sorted(missed, key=lambda r: -r["score"]),
        "_not_doc": not_doc, "_off_topic": off_topic, "_off_other": off_other,
    }


def render(r: dict, show: int) -> None:
    print(f"\n源：{r['label']}（{r['source']}）  主题：{r['topic']}")
    print(f"源端自报 {r['reported_total']} 条 · 实际扫描 {r['scanned']} 条 · "
          f"去重后 {r['unique']} 条（重复 {r['dup']} 条）· {r['ms']} ms")
    print("-" * 72)
    print(f"  已收录        {r['kept']:>4} 条")
    print(f"  不是文件      {r['not_doc']:>4} 条（新闻、公示、解读、招聘…）")
    print(f"  不命中主题    {r['off_topic']:>4} 条（是文件，但与本主题无关）")
    print(f"  解析失败/过短 {r['off_other']:>4} 条")
    print(f"  ★ 疑似误杀    {r['suspect']:>4} 条（像文件，被判成非文件，占已扫描 {r['suspect_rate_of_scanned']}%）")
    print(f"  ★ 漏收        {r['missed']:>4} 条（认成文件、命中主题，却没进库）")
    print("-" * 72)
    if r["_missed"]:
        print(f"\n漏收明细（这是 bug，不是判断题）：")
        for s in r["_missed"][:10]:
            print(f"  · {s['core'][:60]}   主题：{'/'.join(s['topics']) or '—'}")
    if r["_suspect"]:
        print(f"\n疑似误杀前 {min(show, len(r['_suspect']))} 条（按可疑度降序，需人工判断）：")
        for i, s in enumerate(r["_suspect"][:show], 1):
            print(f"  {i:>2}. [{s['score']:>2}] {s['core'][:56]}")
            print(f"      理由：{'；'.join(s['why'])}")
            print(f"      主题：{'/'.join(s['topics']) or '—'}   日期：{s['date'] or '—'}")
    if r["_not_doc"]:
        print(f"\n被判为「不是文件」的样例（抽 5 条，确认判得对不对）：")
        for s in r["_not_doc"][:5]:
            print(f"  · {s['core'][:60]}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="源端收录率审计")
    ap.add_argument("--source", required=True, help="sites.json 里的源名，如 yingtan")
    ap.add_argument("--topic", required=True, help="检索主题词，如 城市更新")
    ap.add_argument("--pages", type=int, default=23, help="翻多少页（默认 23）")
    ap.add_argument("--show", type=int, default=20, help="打印多少条疑似误杀")
    ap.add_argument("--max", type=int, default=200, help="疑似误杀最多收多少条")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON（去掉下划线字段的正文）")
    ap.add_argument("--out", default="", help="把完整明细写到文件")
    a = ap.parse_args()

    r = audit(a.source, a.topic, a.pages, a.max)
    render(r, a.show)

    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细已写入 {p}（{p.stat().st_size} 字节）")
    if a.json:
        slim = {k: v for k, v in r.items() if not k.startswith("_")}
        print(json.dumps(slim, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
