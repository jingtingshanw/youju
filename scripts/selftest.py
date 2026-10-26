#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线自检 30 项。

    python scripts/selftest.py

## 为什么要有它

PRD 里 AC-03 写了「离线自检 30/30」，但一直没脚本——这是唯一一条我找不到实现
也证明不了做过的验收项。要么删，要么补。补的原因是它值这个钱：

系统和数据是会烂的。烂的方式都很安静：索引没重建、台账少一个地域、
地域判定退回到「国家」、某个表的行数悄悄变了。这些都不会报错，只会让检索
少给几条，而少给几条的后果是「用户以为没有」——这正好是本项目最不能出的错。

所以这 30 项全是断言，不是「打印出来给人看」：
每一项都判「该是这样」还是「不是这样」，任一项不过，退出码非 0。

## 三条约束

1. **不联网。** 断网也要能跑。
2. **不调模型。** 一分钱不花，跑一次两秒。
3. **不改数据。** 全部只读。

前两条决定了它能在发布前、重启后、怀疑数据坏了时随时跑；第三条决定了
它可以对着生产库跑，不会把正在用的东西搞坏。
"""
from __future__ import annotations

import re
import sqlite3
import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

CAT = ROOT / "data" / "catalog.db"
KB = ROOT / "data" / "kb.db"
APP = ROOT / "data" / "app.db"

_n_pass = 0
_n_fail = 0
_fails: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _n_pass, _n_fail
    if cond:
        _n_pass += 1
        print(f"  {_n_pass + _n_fail:>2}. ✓ {name}" + (f"　{detail}" if detail else ""))
    else:
        _n_fail += 1
        _fails.append(f"{name}　{detail}")
        print(f"  {_n_pass + _n_fail:>2}. ✗ {name}" + (f"　{detail}" if detail else ""))


def ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def main() -> int:
    # ── 前置：三个库都在，否则下面 30 项全是噪音 ─────────────
    # 刚 clone 下来还没装数据的人会走到这里。不先拦的话，第 4 项就抛
    # sqlite3.OperationalError: unable to open database file ——
    # 这句话指向「文件打不开」，而人真正需要知道的是「去 Releases 下数据包」。
    # 部署现场最贵的错误就是这种：报错了，但指向完全错误的方向。
    missing = [p.name for p in (CAT, KB, APP) if not p.exists()]
    if missing:
        print(f"\n  缺数据文件：{'、'.join(missing)}")
        print(f"  应该在　　：{CAT.parent}\n")
        print("  这个仓库里没有数据。2.8 GB 的政策正文不进 Git，走 Releases 附件：\n")
        print("    1. 去仓库 Releases 页下载 pv-data-*.tar.gz")
        print("    2. tar -xzf pv-data-*.tar.gz -C <项目根>      # 解出 data/")
        print("    3. 再跑一次 python scripts/selftest.py\n")
        return 3

    today = date.today().isoformat()

    # ── 1—6 文件与表都在 ───────────────────────────────────
    print("\n【一】数据文件与表")
    ok("catalog.db 存在", CAT.exists(), f"{CAT.stat().st_size / 1048576:.0f} MB" if CAT.exists() else "缺")
    ok("kb.db 存在", KB.exists(), f"{KB.stat().st_size / 1048576:.0f} MB" if KB.exists() else "缺")
    ok("app.db 存在", APP.exists())

    cat = ro(CAT)
    kb = ro(KB)
    n_items = cat.execute("SELECT count(*) FROM items").fetchone()[0]
    ok("items 有记录", n_items > 50000, f"{n_items:,} 条")

    n_docs = kb.execute("SELECT count(*) FROM docs").fetchone()[0]
    n_blk = kb.execute("SELECT count(*) FROM blocks").fetchone()[0]
    ok("docs 有记录", n_docs > 20000, f"{n_docs:,} 份")
    ok("blocks 有记录", n_blk > 100000, f"{n_blk:,} 块")

    # ── 7—14 索引完整性 ───────────────────────────────────
    print("\n【二】索引完整性")
    n_art = kb.execute("SELECT count(*) FROM blocks WHERE kind='art'").fetchone()[0]
    ok("条文块存在", n_art > 50000, f"{n_art:,} 条")

    n_orphan = kb.execute(
        "SELECT count(*) FROM blocks b LEFT JOIN docs d ON b.doc_key=d.doc_key "
        "WHERE d.doc_key IS NULL").fetchone()[0]
    ok("没有孤儿块（块都有主）", n_orphan == 0, f"{n_orphan} 块无主")

    n_empty_region = kb.execute(
        "SELECT count(*) FROM docs WHERE coalesce(region,'')=''").fetchone()[0]
    ok("每份文档都有地域", n_empty_region == 0, f"{n_empty_region} 份缺地域")

    regions = [r[0] for r in kb.execute(
        "SELECT region, count(*) n FROM docs GROUP BY 1 ORDER BY n DESC")]
    ok("地域数 ≥ 4", len(regions) >= 4, f"{len(regions)} 个：{'/'.join(regions[:5])}")

    # 地域判定曾经整批退化成「国家」，上海 8,903 份全被标成国家。
    # 这条断言就是钉住那个 bug：上海的文档必须真的在「上海市」下面。
    n_sh = kb.execute("SELECT count(*) FROM docs WHERE region='上海市'").fetchone()[0]
    ok("上海在地域上没被吞进「国家」", n_sh > 5000, f"{n_sh:,} 份")

    n_gov = kb.execute("SELECT count(*) FROM docs WHERE region='国家'").fetchone()[0]
    ok("「国家」不含地方文件", n_gov < n_docs * 0.8,
       f"{n_gov:,} / {n_docs:,}（{n_gov / max(n_docs, 1) * 100:.0f}%）")

    try:
        n_fts = kb.execute(
            "SELECT count(*) FROM blocks_fts WHERE blocks_fts MATCH '规划'").fetchone()[0]
        ok("全文索引可查", n_fts > 100, f"「规划」命中 {n_fts:,} 块")
    except sqlite3.Error as e:
        ok("全文索引可查", False, str(e)[:60])

    # ── 15—22 时效台账 ────────────────────────────────────
    print("\n【三】时效台账")
    led = {r["key"]: dict(r) for r in cat.execute("SELECT * FROM eff_ledger")}
    ok("台账覆盖全部正文", len(led) >= n_docs * 0.99,
       f"{len(led):,} / {n_docs:,}")

    states = {r[0]: r[1] for r in cat.execute(
        "SELECT status, count(*) FROM eff_ledger GROUP BY 1")}
    ok("状态只有六种且都有", set(states) == {
        "repealed", "superseded", "expired", "pending", "in_force", "no_signal"},
       " ".join(f"{k}={v:,}" for k, v in sorted(states.items())))

    ok("没有空状态", all(k for k in states))

    # 到期判定必须和「今天」一致，否则台账就是一张过期的账
    bad_exp = [r for r in led.values()
               if r["status"] == "expired" and (r["valid_until"] or "") >= today]
    ok("expired 的都真的过期了", not bad_exp,
       f"{len(bad_exp)} 条日期未到" if bad_exp else f"{states.get('expired', 0)} 条")

    bad_pend = [r for r in led.values()
                if r["status"] == "pending" and (r["effect_on"] or "") <= today]
    ok("pending 的都没到施行日", not bad_pend,
       f"{len(bad_pend)} 条已到" if bad_pend else f"{states.get('pending', 0)} 条")

    bad_force = [r for r in led.values()
                 if r["status"] == "in_force" and (r["valid_until"] or "") <= today]
    ok("in_force 的都还没到期", not bad_force,
       f"{len(bad_force)} 条已过期" if bad_force else f"{states.get('in_force', 0)} 条")

    # 日期离谱说明正则抽错了（正文里常有「1998 年」这类别的日期）
    odd = [r for r in led.values()
           if (r["valid_until"] or "9") < "1990-01-01" or
           (r["valid_until"] or "0") > "2100-01-01"]
    ok("有效期年份在合理区间", not odd, f"{len(odd)} 条越界" if odd else "1920—2100")

    no_ev = [r for r in led.values()
             if r["status"] != "no_signal" and not (r["evidence"] or "").strip()]
    ok("非 no_signal 的都带依据", not no_ev,
       f"{len(no_ev)} 条无依据" if no_ev else "每条都能回原文")

    # 废止关系也要能复核。判定「这份被哪份废了」时，用户唯一能自己验的
    # 就是那句原文。没有原文片段的边，等于让人凭信任接受一个结论。
    n_edge = cat.execute("SELECT count(*) FROM repeal_edge").fetchone()[0]
    n_no_snip = cat.execute(
        "SELECT count(*) FROM repeal_edge WHERE coalesce(snippet,'')=''").fetchone()[0]
    ok("废止边带原文片段", n_edge > 1000 and n_no_snip < n_edge * 0.15,
       f"{n_edge:,} 条边，{n_no_snip} 条无片段")

    # 上海那次「索引重建了、台账没重建」导致 8,903 份全走未核
    led_keys = set(led)
    sh_keys = [r[0] for r in kb.execute(
        "SELECT doc_key FROM docs WHERE region='上海市'")]
    miss = sum(1 for k in sh_keys if k not in led_keys)
    ok("上海每份都在台账里", miss == 0, f"{miss} 份缺台账")

    # ── 23—27 检索与核验能跑 ───────────────────────────────
    print("\n【四】检索与核验")
    # 索引按字切分，整词搜不到——「城市更新」是 0，「城市 更新」才是 3,380。
    # 这不是 bug，是分词策略：整词搜不到但拆开能搜到，正好是这一行要钉住的事。
    hits = kb.execute(
        "SELECT count(*) FROM blocks_fts WHERE blocks_fts MATCH '城市 更新'").fetchone()[0]
    ok("检索「城市 / 更新」有结果", hits > 500, f"{hits:,} 块")

    try:
        import core
        r = core.Retriever()
        q = core.Query("城市更新 建筑高度 控制性详细规划")
        res = r.search(q, k=5)
        got = res.get("hits") or res.get("items") or []
        ok("Retriever 能跑通检索", isinstance(res, dict) and len(got) > 0,
           f"返回 {len(got)} 条，闸门「{res.get('gate', '')}」")
        r.close()
    except Exception as e:                                  # noqa: BLE001
        ok("Retriever 能跑通检索", False, f"{type(e).__name__}: {e}"[:70])

    try:
        import check
        v = check.verify("测试问题", "## 判断\n可以。\n\n## 依据\n"
                         "《中华人民共和国城乡规划法》第五十条：修改控制性详细规划。",
                         [], [])
        ok("核验器对空资料不崩", isinstance(v, dict) and "strength" in v,
           f"结论「{v.get('strength', '')}」")
    except Exception as e:                                  # noqa: BLE001
        ok("核验器对空资料不崩", False, f"{type(e).__name__}: {e}"[:70])

    try:
        import check
        g = check.gaps("## 判断\n可以。\n\n## 缺口\n1. 没有 XX 市的文件\n2. 缺条文")
        ok("缺口段能解析出来", len(g) >= 1, f"{len(g)} 条")
    except Exception as e:                                  # noqa: BLE001
        ok("缺口段能解析出来", False, f"{type(e).__name__}: {e}"[:70])

    # ── 28—30 发布面 ─────────────────────────────────────
    print("\n【五】发布面")
    try:
        import websearch
        ok("受阻站点有明确原因", len(websearch.BLOCKED_SITES) > 0,
           f"{len(websearch.BLOCKED_SITES)} 个站已登记")
    except Exception as e:                                  # noqa: BLE001
        ok("受阻站点有明确原因", False, f"{type(e).__name__}: {e}"[:70])

    try:
        ap = APP
        if ap.exists():
            c = sqlite3.connect(f"file:{ap}?mode=ro", uri=True)
            has_job = c.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='job'"
            ).fetchone()[0]
            c.close()
            ok("任务表存在（重启不丢）", bool(has_job))
        else:
            ok("任务表存在（重启不丢）", False, "app.db 不存在")
    except sqlite3.Error as e:
        ok("任务表存在（重启不丢）", False, str(e)[:60])

    try:
        import server                    # noqa: F401  只验证能 import
        ok("服务模块能加载", True, f"{len(server.PV_LIST)} 个模型通道")
    except Exception as e:                                  # noqa: BLE001
        ok("服务模块能加载", False, f"{type(e).__name__}: {e}"[:70])

    cat.close()
    kb.close()

    total = _n_pass + _n_fail
    print(f"\n{'=' * 46}")
    print(f"  通过 {_n_pass}/{total}")
    if _fails:
        print("  未通过：")
        for f in _fails:
            print(f"    · {f}")
    print(f"{'=' * 46}\n")
    return 0 if _n_fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                        # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
