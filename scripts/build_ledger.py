#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""建全量失效台账 —— 让 18,608 份文件每一份都有确定的时效状态。

## 为什么要有这个脚本

上一版的时效判断只有两个来源：源端给的 `eff_status`（702 份标了 repealed），
和 8 部人工核过的台账。其余 17,906 份的状态是「unknown」——在界面上等于留白。

留白会被读成「没问题」。这是本项目最不能接受的一种误解。

## 三个来源，合并成一张表

1. **源端标记**：`doc_meta.eff_status='repealed'`，702 份。这是别人核过的。
2. **后继文件的废止条款**：这是本轮新增的主路径。
   《XX 条例》被废止，一定有一份后来的文件在正文里写「《XX 条例》同时废止」。
   把 18,608 份的正文扫一遍，把这些句子抓出来，就得到一张**有向图**：
   `废止方 → 被废止方`。图的每个终点，就是一个已失效的文件。
   这比源端标记可靠，因为它来自原文，而不是元数据的某个布尔位。
3. **自述失效**：极少数文件自己写「本规定自某日起废止」，单独标记。

## 输出

`catalog.db` 两张新表：

- `repeal_edge`：废止关系边（谁废止了谁、原文句子、能否在库内匹配到目标）
- `eff_ledger`：每份全文文件一行，`status` 六态

**六种状态，不是三种。** 第一版只有 repealed / superseded / no_signal，只判
「有没有别的文件说它失效」。这在规范性文件上会漏掉一大类：

> 《自然资源部关于进一步做好用地用海要素保障的通知》文末写着
> 「有效期至 2025 年 12 月 31 日」。2026 年 10 月拿它当依据就是错的，
> 但它没有被任何文件点名废止——**它是自己到期的，台账里标 no_signal。**

实测库里 1,746 份正文载有明确的有效期截止日，其中 334 份已经过期，
这 334 份里有 334 份原状态是 no_signal。所以加了 `expired`。

  - `repealed`：有明确依据（源端标记 / 被废止条款点名）
  - `superseded`：同名法规已有更新版本入库，本份是旧版
  - `expired`：原文载有有效期截止日，且已过
  - `pending`：原文载有施行日，且尚未到
  - `in_force`：原文载有有效期截止日，且未过 —— **唯一一类正面证据**
  - `no_signal`：未见废止记录，且原文未载有效期

**`no_signal` 不是「有效」，是「没找到任何信号」**。它能被写进界面，而「unknown」
不能；但也不许被界面显示成「现行有效」——库内 30,687 份里 96% 都是它，
把 no_signal 当有效，等于给整库背书。

用法：
    python scripts/build_ledger.py            # 建表
    python scripts/build_ledger.py --report   # 只看统计
    python scripts/build_ledger.py --sample 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "catalog.db"

# —— 废止边抽取：从「句子语法」judge，不从「最近的两个 token」judge ——
#
# 两个正则在真实文本上会大面积误判，这是实测出来的：
#
#   「根据《中华人民共和国土地管理法实施条例》有关规定，经省人民政府研究，
#     决定废止《江苏省征地补偿安置争议协调裁决办法》」
#
# 旧正则会把《土地管理法实施条例》当被废止对象，因为它到「废止」之间只有 18 字，
# 落在 24 字窗口内。后果是把一部核心上位法标成失效——比没有台账更糟。
#
# 中文的废止句只有四种语序，全部按「动词找宾语」来认：
#   ①「废止《X》」        动词在前，宾语紧跟 → 取动词后第一个书名号
#   ②「《X》同时废止」    主语在前，谓语在后 → 取动词前最近的书名号
#   ③「《X》《Y》一并废止」 并列 → 取动词前连续书名的号串
#   ④「本规定自…起废止」   无宾语 → 不产生边
#
# 判据三条，缺一不可：
#   - 依据词拦截：书名号与动词之间出现「根据/依据/按照…」，说明它是**依据**不是**宾语**
#   - 模态词拦截：「应当废止/需要废止/建议废止」是给下级的要求，不是宣布已废止
#   - 否定词拦截：「未废止/不予废止」
_KILLW = re.compile(r"(?:废止|失效|停止执行)")
_QUOTE = re.compile(r"《([^》]{2,90})》")
_SENT = re.compile(r"[。\n]")
# 依据引导：出现在 gap 里就说明前面那个书名号是被引用的依据
_BASIS = re.compile(r"根据|依据|按照|依照|参照|遵照|符合|违反|执行")
# 模态：给下级的要求，不是本轮宣布
_MODAL = re.compile(r"应当|需要|建议|可以|拟|拟予|应及时|要")
# 代词指代：宾语是「下列规章」而不是前面那个书名号
_PRONOM = re.compile(r"下列|以下|上述|前述|相应|有关|相关")
# 并列连接：两个书名号之间只有这些字，算同一宾语串
_CONJ = re.compile(r"^[、，,和及与]\s*$")

# 「关于修改《X》的决定」——只动一部文件，它废的是**旧版**，库里同名的是新版。
# 必须和「关于修改和废止部分…的决定」区分开：后者是打包清单，逐条「废止《X》」都是真废止。
# 判据：修改/修正后 10 字内是否直接跟书名号。跟了 → 单文件修改；没跟 → 打包决定。
_SINGLE_AMEND = re.compile(r"(?:修改|修正)[^《》]{0,10}《")

# 法规类标题：同名多版本不可能同时有效，可据「有更新的同名版本入库」直接判旧版失效。
# 「通知/意见/批复/决定」这类是文件容器名，不同年份往往讲不同事，不能这样判。
_REG_LIKE = re.compile(r"(条例|办法|规定|细则|规则|准则|标准|办法|法)$")

# 修改类：不算废止，标 modified
_AMEND = re.compile(r"《([^》]{4,80})》[^。；\n]{0,10}?(?:修订|修改)")


def kill_targets(tail: str) -> list[tuple[str, str]]:
    """从正文尾部抽废止宾语，返回 [(被废止标题, 依据片段)]。

    语序优先：动词后的书名号是宾语（①），动词前的是主语（②③）。
    """
    out: list[tuple[str, str]] = []
    for sent in _SENT.split(tail):
        if not _KILLW.search(sent):
            continue
        qs = [(m.start(), m.end(), m.group(1)) for m in _QUOTE.finditer(sent)]
        if not qs:
            continue
        for km in _KILLW.finditer(sent):
            kpos, kend = km.start(), km.end()
            # 否定：未废止 / 不予废止
            if re.search(r"[不未]$", sent[max(0, kpos - 2):kpos]):
                continue

            # ① 动词在前：废止《X》《Y》
            after = [q for q in qs if q[0] >= kend]
            if after:
                gap = sent[kend:after[0][0]]
                if len(gap) <= 10 and not _QUOTE.search(gap) and not _BASIS.search(gap):
                    picked = [after[0]]
                    for q in after[1:]:
                        between = sent[picked[-1][1]:q[0]]
                        if _CONJ.match(between):
                            picked.append(q)
                        else:
                            break
                    for q in picked:
                        s = max(0, kpos - 24)
                        out.append((q[2], sent[s:kend + 30].strip()))
                    continue

            # ②③ 动词在后：取动词前最近的书名号，并列串一起收
            before = [q for q in qs if q[1] <= kpos]
            if not before:
                continue
            q = before[-1]
            gap = sent[q[1]:kpos]
            if len(gap) > 30 or _QUOTE.search(gap):
                continue
            if _BASIS.search(gap) or _MODAL.search(gap) or _PRONOM.search(gap):
                continue
            picked = [q]
            for pq in reversed(before[:-1]):
                between = sent[pq[1]:picked[0][0]]
                if _CONJ.match(between):
                    picked.insert(0, pq)
                else:
                    break
            s = max(0, q[0] - 20)
            for pq in picked:
                out.append((pq[2], sent[s:min(len(sent), kend + 8)].strip()))
    return out

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repeal_edge (
    src_key      TEXT NOT NULL,     -- 宣布废止的那份文件
    src_title    TEXT,
    target_title TEXT,              -- 被废止的文件（正文里写的名字）
    target_key   TEXT,              -- 在库内匹配到的 key，可为空
    snippet      TEXT,              -- 依据原文
    at           INTEGER,
    PRIMARY KEY (src_key, target_title)
);
CREATE TABLE IF NOT EXISTS eff_ledger (
    key        TEXT PRIMARY KEY,
    title      TEXT,
    status     TEXT,                -- 见上：六态
    basis      TEXT,                -- source_tag / repeal_clause / newer_version
                                    -- / expiry / effect_date / none
    evidence   TEXT,
    by_doc     TEXT,                -- 由哪份文件废止 / 替代（title）
    repealed_on TEXT,               -- 废止方文件的发布日
    effect_on  TEXT,                -- 原文载明的施行日 YYYY-MM-DD
    valid_until TEXT,               -- 原文载明的有效期截止日 YYYY-MM-DD
    valid_raw  TEXT,                -- 原文那句话，用于复核
    checked_at REAL
);
CREATE INDEX IF NOT EXISTS ix_ledger_status ON eff_ledger(status);
"""

# —— 有效期与施行日：只从文末 900 字抽 ——
#
# 这两种表述几乎总在最后一段（「本通知自 2023 年 1 月 1 日起施行，有效期至
# 2028 年 12 月 31 日」），扫全文没必要，也会撞上正文里引用的别人的日期。
#
# 抽样实测：29,416 份正文中，1,746 份能抽到绝对有效期，339 份只写「有效期 5 年」
# （无基准日，除非同时有施行日，否则算不出截止日，不用），4,568 份能抽到施行日。
_VALID = re.compile(
    r"有效期(?:限)?(?:为|是)?\s*[至到]\s*(\d{4})\s*年\s*(\d{1,2})\s*月"
    r"(?:\s*(\d{1,2})\s*日)?")
# 「自 2023 年 1 月 1 日起施行，有效期至 2028 年 12 月 31 日」这种，两个都要，
# 先抓整句再拆——分开抓容易把施行日抓成上一个句子的日期。
_EFFECT = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起(?:施行|实施|执行|生效)")
# 「本规定自 2023 年 1 月 1 日起废止」——自述失效，直接给废止日
_SELF_KILL = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起\s*(?:废止|失效)")


def _date(y, m, d=None) -> str:
    try:
        return dt.date(int(y), int(m), int(d or 1)).isoformat()
    except (ValueError, TypeError):
        return ""


def norm_title(t: str) -> str:
    """标题归一：去书名号、括号年份、空白、全角空格。"""
    t = t.replace("《", "").replace("》", "").replace(" ", "").replace("　", "")
    t = re.sub(r"[（(]\d{4}年?[^）)]*[）)]", "", t)
    t = re.sub(r"（[^）]*修正[^）]*）|\([^)]*修正[^)]*\)", "", t)
    return t.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="只打印统计，不重建")
    ap.add_argument("--sample", type=int, default=0,
                    help="随机抽 N 条 repealed 打出来人工校验准确率")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)

    if args.sample:
        n = args.sample
        for r in con.execute(
                "SELECT e.key,e.title,e.by_doc,e.evidence FROM eff_ledger e "
                "WHERE e.status='repealed' AND e.basis='repeal_clause' "
                "ORDER BY RANDOM() LIMIT ?", (n,)):
            print(f"\n【失效】{r['title']}")
            print(f"  废止方：{r['by_doc'] or '—'}")
            print(f"  原文：{r['evidence']}")
        return 0

    if args.report:
        for sel in ("SELECT status, COUNT(*) n FROM eff_ledger GROUP BY status ORDER BY n DESC",
                    "SELECT basis, COUNT(*) n FROM eff_ledger GROUP BY basis ORDER BY n DESC",
                    "SELECT COUNT(*) n, SUM(target_key IS NOT NULL) matched FROM repeal_edge"):
            for r in con.execute(sel):
                print(dict(r))
            print("---")
        return 0

    t0 = time.time()
    docs = list(con.execute(
        "SELECT key,title,doc_no,published,path FROM fulltext WHERE status='ok'"))
    print(f"全文 {len(docs)} 份")

    # 标题索引：归一化标题 → [(发布日, key)]，同标题多版本全部保留。
    # 只留最新一份会让「旧版被废止」这条边落到新版头上——《军人抚恤优待条例》
    # 就是这么被自己的修改决定判成失效的。
    by_title: dict[str, list[tuple[str, str]]] = {}
    for d in docs:
        by_title.setdefault(norm_title(d["title"]), []).append(
            (d["published"] or "", d["key"]))
    for v in by_title.values():
        v.sort()                      # 早→晚

    con.execute("DELETE FROM repeal_edge")
    con.execute("DROP TABLE IF EXISTS eff_ledger")     # 列变了，重建
    con.executescript(_SCHEMA)

    edges: list[tuple] = []
    repealed: dict[str, tuple] = {}          # key -> (basis, evidence, by_doc)
    now = int(time.time())
    n_with_clause = 0
    n_amend_skip = 0
    dates: dict[str, tuple[str, str, str]] = {}   # key -> (effect_on, valid_until, raw)

    for i, d in enumerate(docs, 1):
        p = Path(d["path"]) if d["path"] else None
        if not p or not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        # 有效期 / 施行日只在尾部
        tail900 = text[-900:]
        eff, vu, vraw = "", "", ""
        mv = _VALID.search(tail900)
        if mv:
            vu = _date(mv.group(1), mv.group(2), mv.group(3))
            vraw = mv.group(0).strip()
        me = _EFFECT.search(tail900)
        if me:
            eff = _date(*me.groups())
            if not vraw:
                vraw = me.group(0).strip()
        ms = _SELF_KILL.search(tail900)
        if ms:
            # 自述失效：日期即废止日，直接算 repealed，不用等别人点名
            self_date = _date(*ms.groups())
            if self_date:
                repealed.setdefault(d["key"], (
                    "self", f"原文自述：{ms.group(0).strip()}", "",
                    self_date))
        if eff or vu:
            dates[d["key"]] = (eff, vu, vraw)
        # 废止/修订条款在条例里位于末尾，但在「关于废止和修改部分…的决定」这类
        # 打包清单里位于正文中段，能列到一百多条。只扫尾 5,000 字会漏掉前面全部条目。
        # 两头各取 2 万字，既覆盖清单又控制成本。
        tail = text if len(text) <= 40000 else (text[:20000] + "\n" + text[-20000:])
        # 单文件修改决定（「关于修改《X》的决定」）废止的是被修改文件的旧版本，
        # 而库里同名的通常是新版本身。判错代价 > 漏判收益，一律只记边不判状态。
        # 打包决定（「关于修改和废止部分…的决定」）不在此列，它的废止条目是真的。
        is_amend_src = bool(_SINGLE_AMEND.search(d["title"] or ""))
        found = 0
        for tgt_raw, snippet in kill_targets(tail):
            tgt = norm_title(tgt_raw)
            if not tgt or tgt == norm_title(d["title"]):
                continue
            found += 1
            cands = by_title.get(tgt) or []
            if is_amend_src:
                n_amend_skip += 1
                edges.append((d["key"], d["title"], tgt_raw,
                              cands[-1][1] if cands else None,
                              "（修改决定，不判时效）" + snippet, now))
                continue
            # 同名多版本：废止对象只能是**早于废止方发布**的那些，
            # 且取其中最晚的一份——「同时废止」废的是上一版，不是最初版。
            # 若废止方日期明确但库内没有更早版本，说明被废的那版没进库：
            # 不判状态（留 no_signal），比标错一份现行文件强。
            pub = d["published"] or ""
            cands2 = [c for c in cands if c[1] != d["key"]]
            older = [c for c in cands2 if c[0] and pub and c[0] < pub]
            if older:
                pick = older[-1][1]
            elif not pub:
                pick = cands2[0][1] if cands2 else None
            else:
                pick = None
            edges.append((d["key"], d["title"], tgt_raw, pick, snippet, now))
            if pick and pick != d["key"] and pick not in repealed:
                repealed[pick] = ("repeal_clause", snippet, d["title"],
                                  d["published"] or "")
        # 修改类边单独记，不改变有效性，但能回答「这份文件被谁改过」
        for m in _AMEND.finditer(tail):
            tgt = norm_title(m.group(1))
            if tgt and tgt != norm_title(d["title"]):
                cands = by_title.get(tgt) or []
                if cands:
                    edges.append((d["key"], d["title"], m.group(1) + "（修订）",
                                  cands[-1][1], "", now))
        if found:
            n_with_clause += 1
        if i % 2000 == 0:
            print(f"  {i}/{len(docs)}  {(time.time()-t0):.0f}s  边 {len(edges)}")

    # 源端标记优先于推导（人工核过的口径更硬）
    src_tag = {r["key"]: r for r in con.execute(
        "SELECT key, eff_status FROM doc_meta WHERE eff_status='repealed'")}

    # 同名法规多版本：最新一份是现行版，其余是旧版。
    # 新版的正文里不一定写「旧版废止」（废止说明常在国务院令里，不在这份文件里），
    # 但同名法规不可能同时有效，这层推断不需要原文依据。
    superseded: dict[str, str] = {}          # 旧版 key -> 新版 key
    latest_title: dict[str, str] = {}
    for t, v in by_title.items():
        if not _REG_LIKE.search(t):
            continue
        dated = [c for c in v if c[0]]
        if len(dated) < 2:
            continue
        newest = dated[-1][1]
        latest_title[newest] = t
        for _, k in dated[:-1]:
            superseded[k] = newest

    pub_of = {d["key"]: (d["published"] or "") for d in docs}
    ttl_of = {d["key"]: d["title"] for d in docs}
    today = dt.date.today().isoformat()

    rows = []
    for d in docs:
        k, title = d["key"], d["title"]
        eff, vu, vraw = dates.get(k, ("", "", ""))
        if k in src_tag:
            rows.append((k, title, "repealed", "source_tag",
                         "源端元数据标记为已失效（由发布方自己打标）",
                         "", "", eff, vu, vraw, now))
        elif k in repealed:
            basis, ev, byd, ron = repealed[k]
            rows.append((k, title, "repealed", basis, ev, byd, ron,
                         eff, vu, vraw, now))
        elif k in superseded:
            nk = superseded[k]
            rows.append((k, title, "superseded", "newer_version",
                         f"同名的更新版本已入库（{pub_of.get(nk) or '日期不详'}）："
                         f"{ttl_of.get(nk, '')}",
                         ttl_of.get(nk, ""), pub_of.get(nk, ""),
                         eff, vu, vraw, now))
        elif vu and vu < today:
            # 到期的文件不靠任何别的文件宣布，它自己写着到期日。
            # 这一类比 repealed 更容易漏，因为搜遍全库都搜不到「废止」二字。
            rows.append((k, title, "expired", "expiry",
                         f"原文载明有效期至 {vu}，已届满：{vraw}",
                         "", vu, eff, vu, vraw, now))
        elif eff and eff > today:
            rows.append((k, title, "pending", "effect_date",
                         f"原文载明自 {eff} 起施行，尚未到施行日：{vraw}",
                         "", "", eff, vu, vraw, now))
        elif vu:
            rows.append((k, title, "in_force", "expiry",
                         f"原文载明有效期至 {vu}，未届满：{vraw}",
                         "", "", eff, vu, vraw, now))
        else:
            ev = f"全库 {len(docs):,} 份正文中未见点名废止的文件"
            ev += (f"；原文载明自 {eff} 起施行，未载有效期" if eff
                   else "；原文未载施行日与有效期")
            rows.append((k, title, "no_signal", "none", ev, "", "",
                         eff, vu, vraw, now))

    con.executemany("INSERT OR REPLACE INTO repeal_edge "
                    "(src_key,src_title,target_title,target_key,snippet,at) "
                    "VALUES (?,?,?,?,?,?)", edges)
    con.executemany("INSERT OR REPLACE INTO eff_ledger "
                    "(key,title,status,basis,evidence,by_doc,repealed_on,"
                    "effect_on,valid_until,valid_raw,checked_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()

    print(f"\n完成 {(time.time()-t0):.0f}s")
    print(f"  废止关系边：{len(edges)}（其中 {sum(1 for e in edges if e[3])} 条在库内匹配到目标）")
    print(f"  含废止/修订条款的文件：{n_with_clause}")
    for r in con.execute("SELECT status, COUNT(*) n FROM eff_ledger GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:12s} {r['n']:,}")
    print("\n被点名废止的常见目标：")
    for r in con.execute(
            "SELECT target_title, COUNT(*) n FROM repeal_edge WHERE target_title LIKE '%条例%' "
            "GROUP BY target_title ORDER BY n DESC LIMIT 8"):
        print(f"  {r['n']:3d} 次  {r['target_title']}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
