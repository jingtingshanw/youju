#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""有据 · 产品入口。

    python app/server.py            # http://127.0.0.1:8848

## 六个接口，对应六步

    POST /api/plan     输入问题 → 取证指令（AI 拆解，可人工改）
    POST /api/gather   按指令取证 → 候选文件（数量不限，按证据强度分档）
    POST /api/write    选中的文件 → 结论与建议（AI 撰写，机器核验）
    POST /api/mark     记一处「这里不对」
    GET  /api/trace    看这次留下了哪些痕迹
    GET  /             界面

每一次运行有 run_id，贯穿全程。用户在第 2 步改了哪条指令、在第 3 步丢了
哪份文件，都挂在这个 id 下面——**用户的操作就是标注，不用另外标**。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import check        # noqa: E402
import core         # noqa: E402
import gather       # noqa: E402
import plan as planlib  # noqa: E402
import trace as trace_mod  # noqa: E402
import write as writelib  # noqa: E402

# `.env` 里的开关也要认。环境变量优先，其次 .env —— 顺序反过来的话，
# 写在 .env 里的 PV_TOKEN 会被静默忽略，而界面上一切正常，
# 直到有人刷爆模型额度才发现「设了等于没设」。
_ENV = core.load_env()


def _conf(key: str, default: str = "") -> str:
    return (os.environ.get(key) or _ENV.get(key) or default).strip()


PORT = int(_conf("PV_PORT", "8848"))
# 降级开关：设成 1 之后撰写不调模型，直接排原文条文（见 write.raw_report）。
# 三种用法：模型集体限流时保命、只想看条文不要概括时、演示时控成本。
RAW_ONLY = _conf("QW_RAW_ONLY").lower() not in ("", "0", "false", "off")
# 只监听回环。要挂到公网给魔笔/百炼调，用 nginx/Caddy 反代到 127.0.0.1:8848，
# 不要把这里改成 0.0.0.0——那会绕过反代上的 HTTPS 和限流。
BIND = _conf("PV_BIND", "127.0.0.1")
# 传整个列表，让 call_llm 遇 403 自己往下换。只取第一家的话，
# 一家额度用尽就让整份报告降级成规则兜底。
PV_LIST = core.pick_providers(_ENV)
PV = PV_LIST[0] if PV_LIST else {"name": "none", "model": ""}

# 接口鉴权。本地跑不用管（PV_TOKEN 为空 = 不校验），一旦挂到公网给魔笔/百炼调，
# 就必须在 .env 里设一个 PV_TOKEN，否则任何人都能刷你的模型额度——
# /v1/write 一次要跑 3200 token 的撰写，刷起来很快。
TOKEN = _conf("PV_TOKEN")


# ══════════════════════════════════════════════════════════════════
# 三个阶段的生成器
# ══════════════════════════════════════════════════════════════════

def stage_plan(question: str):
    t0 = time.time()
    run_id = trace_mod.new_run(question)
    yield ("start", {"run_id": run_id})

    t = time.time()
    p = planlib.make_plan(question, PV_LIST, core.call_llm)
    data = p.as_dict()
    trace_mod.set_plan(run_id, data)
    # 埋点 plan_done（PRD §8.1）：n_required 用来判断「必查方向」有没有稳定拆出来
    trace_mod.ev(run_id, "plan_done", round((time.time() - t) * 1000),
                 source=data["source"], n_tasks=len(data["tasks"]),
                 n_required=sum(1 for x in data["tasks"] if x.get("must")),
                 city=(data.get("place") or {}).get("city", ""),
                 q_len=len(question or ""),
                 provider=PV.get("provider", ""), model=PV.get("model", ""))

    # 拆解这一步要埋的坑：模型没给结构化结果、或者缺了必查方向
    if data["source"] != "llm":
        trace_mod.mark(run_id, "plan_fallback", "拆解",
                       data["reading"][-80:] if data["reading"] else "模型未返回结构化结果")
    if not data["place"]["city"] and not data["place"]["site"]:
        trace_mod.mark(run_id, "no_place", "拆解", "问题里没识别出城市或项目名")

    yield ("plan", {"plan": data, "ms": round((time.time() - t) * 1000),
                    "total_ms": round((time.time() - t0) * 1000),
                    "provider": PV["provider"], "model": PV["model"]})


def stage_gather(run_id: str, plan: dict, scope: str = "kb"):
    t0 = time.time()
    res = gather.gather(plan, scope=scope)
    for task in res["tasks"]:
        if task.get("total", 0) == 0:
            trace_mod.mark(run_id, "task_empty",
                           f'{task.get("label")}·{task.get("title")}',
                           f'词={",".join(task.get("queries_used") or [])}')
    trace_mod.set_gather(run_id, res["n_items"], res["n_body"])
    # 埋点 gather_done：按方向记，用来算「取证空结果率」和「哪一层总空」
    for task in res["tasks"]:
        its = task.get("items") or []
        trace_mod.ev(run_id, "gather_done", task.get("ms", 0),
                     task_id=task.get("id"), kind=task.get("kind"),
                     label=task.get("label"), title=(task.get("title") or "")[:60],
                     n_items=len(its),
                     n_body=sum(1 for i in its if i.get("has_body")),
                     n_usable=task.get("n_usable", 0),
                     where=task.get("scope") or scope,
                     err=str(task.get("note") or "")[:120])
    trace_mod.ev(run_id, "gather_sum", res["ms"], n_items=res["n_items"],
                 n_body=res["n_body"], n_web=res.get("n_web", 0),
                 n_tasks=len(res["tasks"]), scope=scope)
    # 埋点 guard_block：撞到被拦站点（合肥这类整站反爬），用来判断哪些城市总撞墙
    for rep in res.get("web_reports") or []:
        if rep.get("blocked"):
            trace_mod.ev(run_id, "guard_block", rep.get("ms", 0),
                         kind=rep.get("kind"),
                         reason=str(rep.get("error") or "")[:120],
                         channel=rep.get("channel", ""))
    yield ("gather", {"tasks": res["tasks"], "ms": res["ms"],
                      "n_items": res["n_items"], "n_body": res["n_body"],
                      "n_web": res.get("n_web", 0), "scope": scope,
                      "web_reports": res.get("web_reports") or [],
                      "total_ms": round((time.time() - t0) * 1000)})


def stage_write(run_id: str, question: str, plan: dict,
                picked: list[dict], default: list[str]):
    t0 = time.time()
    # 每个候选带着自己是哪条指令捞出来的，drop 才能归到「哪类文件最常被丢」
    task_kind = {it["title"]: it.get("task_kind", "") for it in picked}
    # 记录勾选：drop = 系统给了但用户不要，add = 用户自己找的
    sel = trace_mod.picks(run_id, picked, default, task_kind)
    if sel["drop"]:
        trace_mod.mark(run_id, "drop", f'{len(sel["drop"])} 份未入选',
                       "；".join(x[:40] for x in sel["drop"][:6]))
    if not picked:
        yield ("error", {"error": "没有选中任何文件"})
        return

    terms = sorted({w for p in picked for w in gather.segment(p.get("title", ""))})
    t = time.time()
    bodies = gather.fetch_bodies(picked, terms or [question])
    n_body = sum(1 for b in bodies if b["has_body"])
    if n_body == 0:
        trace_mod.mark(run_id, "no_body", "取证", "选中的文件全部只有目录条目，无正文可引")
    yield ("bodies", {"n_body": n_body, "n_picked": len(picked),
                      "ms": round((time.time() - t) * 1000)})

    system, user = writelib.build_prompt(question, plan, picked, bodies)
    t = time.time()
    err, why = "", ""
    if RAW_ONLY:
        why = "QW_RAW_ONLY 已开启"
    else:
        try:
            answer = core.call_llm(PV_LIST, system, user, timeout=300, max_tokens=4200)
        except Exception as e:  # noqa: BLE001
            answer, err = "", f"{type(e).__name__}: {e}"
            why = "模型调用失败"
    if why:
        # 撰写是唯一一步失败就整份报告没了的环节（plan 失败还能手写指令，
        # gather 失败还有目录），所以它是唯一值得配降级的一步。
        # 降级产物是原文清单，不含任何概括，核验照走——反而更容易通过。
        answer = writelib.raw_report(question, plan, picked, bodies, why)
        trace_mod.ev(run_id, "fallback_raw", round((time.time() - t) * 1000),
                     reason=why, err=err[:160], chars=len(answer))
        err = ""
    yield ("write", {"answer": answer, "ms": round((time.time() - t) * 1000),
                     "error": err, "chars": len(user)})
    if err:
        return

    t = time.time()
    vd = check.verify(question, answer, picked, bodies)
    yield ("verify", {**vd, "ms": round((time.time() - t) * 1000)})

    # 自动埋点：核验不过、逐句回查低、缺口多的，全部落库
    if vd["n_fail"]:
        for c in vd["cites"]:
            if c["kind"] == "failed":
                bad = [x for x in c["checks"] if x["state"] == "fail"]
                trace_mod.mark(run_id, "cite_fail", f'{c["claimed"]} 第{c["article"]}条',
                               "；".join(f'{x["name"]}:{x["detail"][:60]}' for x in bad))
    if vd["ratio"] < 0.6:
        trace_mod.mark(run_id, "low_trace", "逐句回查",
                       f'{vd["ratio"]*100:.0f}% 的句子能在原文找到出处')
    for g in check.gaps(answer)[:5]:
        trace_mod.mark(run_id, "gap", g[:120], "")

    # 埋点 report_done：chars 用来看报告长度是否失控（Q-07 那 5,367 字的问题）
    n_sec = sum(1 for s in ("判断", "依据", "路径", "缺口", "下一步")
                if ("## " + s) in answer or ("**" + s) in answer)
    trace_mod.ev(run_id, "report_done", round((time.time() - t0) * 1000),
                 chars=len(answer), n_sections=n_sec,
                 n_cites=len(vd["cites"]), strength=vd["strength"],
                 ratio=round(vd["ratio"], 3),
                 provider=PV.get("provider", ""), model=PV.get("model", ""))

    # 埋点 verify_result：C1—C5 各自的通过/拦截/未核分布。
    # 「未核占比」= skip / 总检查次数，这是诚实层的关键数，破 70% 说明核验形同虚设。
    cstat: dict[str, dict[str, int]] = {}
    for c in vd["cites"]:
        for chk in c.get("checks") or []:
            key = (chk.get("name") or "?")[:2]        # 「C1」「C2」…
            d = cstat.setdefault(key, {"pass": 0, "fail": 0, "skip": 0})
            st = chk.get("state") or "skip"
            d[st if st in d else "skip"] = d.get(st if st in d else "skip", 0) + 1
    n_chk = sum(sum(d.values()) for d in cstat.values())
    n_skip = sum(d.get("skip", 0) for d in cstat.values())
    # 未核占比排除 C5：问题里没点名法规时 C5 必然 skip，这是结构性的，
    # 不是核验没干活。算进去会让这个指标永远偏高，失去预警作用。
    n_chk4 = sum(sum(d.values()) for k, d in cstat.items() if k != "C5")
    n_skip4 = sum(d.get("skip", 0) for k, d in cstat.items() if k != "C5")
    trace_mod.ev(run_id, "verify_result", vd.get("ms", 0),
                 n_cite=len(vd["cites"]), n_check=n_chk,
                 n_unchecked=n_skip,
                 n_unchecked_c1c4=n_skip4,
                 uncheck_ratio=round(n_skip4 / n_chk4, 3) if n_chk4 else 0,
                 n_fail=vd["n_fail"], n_unv=vd["n_unv"],
                 **{f"{k.lower()}": f"{d['pass']}p/{d['fail']}f/{d['skip']}s"
                    for k, d in sorted(cstat.items()) if k.startswith("C")})
    yield ("done", {"total_ms": round((time.time() - t0) * 1000),
                    "run_id": run_id, "marks": len(trace_mod.marks_of(run_id))})


# ══════════════════════════════════════════════════════════════════
# v1：给低代码平台（阿里云百炼 · 魔笔）用的同步 JSON 接口
# ══════════════════════════════════════════════════════════════════
#
# 魔笔的「集成」只吃普通 HTTP + JSON，不吃 SSE。所以这里把上面三个生成器
# 跑完再一次性返回——生成器本身不改，SSE 和 JSON 两条出口共用同一套逻辑，
# 不会出现「网页版能跑、接口版行为不同」这种分叉。
#
# 三段的输入输出刻意做成「可以分开调」：拆解结果可以人工改完再调取证，
# 取证结果可以人工勾选完再调撰写。这对应界面上那三个「确认」动作——
# 中间一定要有人过一道，这是这套系统的核心，不能为了省事合成一个接口。

def _drain(gen) -> dict:
    """把 stage_* 生成器跑完，按 step 归拢成一个 dict。"""
    out: dict = {}
    for step, data in gen:
        out.setdefault(step, []).append(data)
    return out


def v1_plan(question: str) -> dict:
    r = _drain(stage_plan(question))
    p = (r.get("plan") or [{}])[0]
    s = (r.get("start") or [{}])[0]
    return {"ok": "plan" in r, "run_id": s.get("run_id", ""),
            "plan": p.get("plan"), "ms": p.get("ms"),
            "provider": p.get("provider"), "model": p.get("model")}


def v1_gather(plan: dict, run_id: str, scope: str = "kb") -> dict:
    r = _drain(stage_gather(run_id or trace_mod.new_run("(接口调用)"), plan, scope))
    g = (r.get("gather") or [{}])[0]
    return {"ok": "gather" in r, "tasks": g.get("tasks") or [],
            "n_items": g.get("n_items", 0), "n_body": g.get("n_body", 0),
            "n_web": g.get("n_web", 0), "scope": scope,
            "web_reports": g.get("web_reports") or [], "ms": g.get("ms")}


def v1_write(question: str, plan: dict, picked: list[dict],
             run_id: str = "", default: list[str] | None = None) -> dict:
    r = _drain(stage_write(run_id or trace_mod.new_run(question), question,
                           plan, picked, default or []))
    w = (r.get("write") or [{}])[0]
    v = (r.get("verify") or [{}])[0]
    b = (r.get("bodies") or [{}])[0]
    d = (r.get("done") or [{}])[0]
    return {"ok": bool(w.get("answer")), "answer": w.get("answer", ""),
            "error": w.get("error", ""), "bodies": b, "verify": v,
            "run_id": d.get("run_id", run_id), "total_ms": d.get("total_ms")}


def v1_coverage() -> dict:
    """库与联网的真实能力边界。魔笔首页那几个数字从这里取，不写死。"""
    import websearch
    kb = sqlite3.connect(f"file:{core.KB_DB}?mode=ro", uri=True)
    cat = sqlite3.connect(f"file:{core.CAT_DB}?mode=ro", uri=True)
    try:
        n_doc = kb.execute("SELECT count(*) FROM docs").fetchone()[0]
        n_blk = kb.execute("SELECT count(*) FROM blocks").fetchone()[0]
        n_art = kb.execute("SELECT count(*) FROM blocks WHERE kind='art'").fetchone()[0]
        n_chars = kb.execute("SELECT coalesce(sum(chars),0) FROM docs").fetchone()[0]
        by_region = dict(kb.execute(
            "SELECT region, count(*) FROM docs GROUP BY region").fetchall())
        n_cat = cat.execute("SELECT count(*) FROM items").fetchone()[0]
        n_region = cat.execute(
            "SELECT count(DISTINCT region) FROM items WHERE coalesce(region,'')<>''"
        ).fetchone()[0]
        eff = dict(cat.execute(
            "SELECT status, count(*) FROM eff_ledger GROUP BY status").fetchall())
    finally:
        kb.close()
        cat.close()
    return {"fulltext": {"docs": n_doc, "blocks": n_blk, "articles": n_art,
                         "chars": n_chars, "by_region": by_region},
            "catalog": {"items": n_cat, "regions": n_region},
            "efficacy": eff,
            "web": websearch.coverage()}


# ══════════════════════════════════════════════════════════════════
# 异步任务层——为了能挂到百炼 / 魔笔上
#
# 百炼自定义插件和魔笔数据源读一个 HTTP 接口，超时大多在 10—30 秒。
# 而 /v1/write 实测 17—60 秒（3200 token 撰写 + 正文装配 + 逐条核验），
# /v1/gather 开联网也要 20—40 秒。同步调它们的结果是：服务端跑完了，
# 客户端已经断开——**额度花了，结果丢了**，而且日志里看不出来。
#
# 所以三个重接口各配一个异步版本：
#     POST /v1/jobs  {"op":"plan|gather|write", ...}  → 立即返回 job_id
#     GET  /v1/jobs/<id>                              → 轮询，done 时带 result
#
# 任务落 app.db 的 job 表，不放进程内存。原因有三个，都在真实使用中撞过：
#
#   1. 进程一重启，用户手上那个 job_id 就查不到了，返回 404 还是 running
#      全看运气。轮询方（魔笔）只会显示"处理中"，永远不结束。
#   2. 请求打到 B 实例、任务跑在 A 实例时，轮询永远拿不到结果。
#   3. 结果没人留：一个问题跑完 30 秒、烧掉几千 token，用户刷新一下就没了。
#
# 落库后这三个都不成立。代价是每次读要开一次 SQLite 连接——一轮轮询几次，
# 比起跑任务本身的开销可以忽略。
#
# 僵尸任务：进程被 kill（断电、重启、CI 里跑完就退）时，job 行会永远停在
# running。所以启动时把 t0 早于本进程启动时刻的 running 行改判 interrupted，
# 让轮询方拿到一个明确的失败，而不是无限等待。
# ══════════════════════════════════════════════════════════════════

_JOB_KEEP = 86400         # 结果留一天，够查「刚才那份报告去哪了」
_BOOT_TS = time.time()
_JOB_READY = False
_JOB_INIT_LOCK = threading.Lock()


def _jobdb() -> sqlite3.Connection:
    """每一次任务操作都开一个短连接。

    两个坑踩过：

    1. **`PRAGMA journal_mode=WAL` 每次连接都执行会报
       `attempt to write a readonly database`。** 切日志模式要拿排他锁，
       两个请求同时进来，一个在切、另一个在等，等不到就报这个错——
       错误信息完全指向错误方向（说是只读库，其实是锁竞争）。
       所以只在首次建表时切一次，之后不再碰。

    2. 并发写必须给 `busy_timeout`。SQLite 默认等 5 秒就放弃。一次写
       只有一个 INSERT/UPDATE、毫秒级，5 秒够；但网络抖动时任务线程可能
       正好在写，默认值太紧。给 30 秒。
    """
    global _JOB_READY
    c = sqlite3.connect(str(trace_mod.DB), timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    if not _JOB_READY:
        with _JOB_INIT_LOCK:
            if not _JOB_READY:
                try:
                    c.execute("PRAGMA journal_mode=WAL")
                except sqlite3.Error:
                    pass                        # 已经有别的连接切成 WAL 了
                c.execute("""CREATE TABLE IF NOT EXISTS job (
                    id TEXT PRIMARY KEY, op TEXT, state TEXT, result TEXT,
                    error TEXT, t0 REAL, ms INTEGER)""")
                c.execute("CREATE INDEX IF NOT EXISTS i_job_t0 ON job(t0)")
                c.commit()
                _JOB_READY = True
    return c


def _jobs_recover() -> int:
    """把上次进程留下的 running 改判中断。返回改了几行。"""
    c = _jobdb()
    try:
        cur = c.execute(
            "UPDATE job SET state='interrupted', "
            "error='服务重启，任务已中断，请重新提交' "
            "WHERE state='running' AND t0 < ?", (_BOOT_TS,))
        c.commit()
        return cur.rowcount or 0
    finally:
        c.close()


def _jobs_prune() -> None:
    c = _jobdb()
    try:
        c.execute("DELETE FROM job WHERE t0 < ?", (time.time() - _JOB_KEEP,))
        c.commit()
    finally:
        c.close()


def _job_start(fn, op: str = "") -> str:
    jid = "j" + uuid.uuid4().hex[:16]
    t0 = time.time()
    c = _jobdb()
    try:
        c.execute("INSERT INTO job (id, op, state, result, error, t0, ms) "
                  "VALUES (?,?,'running','','',?,0)", (jid, op, t0))
        c.commit()
    finally:
        c.close()

    def _run() -> None:
        state, res, err = "done", "", ""
        try:
            res = json.dumps(fn(), ensure_ascii=False, default=str)
        except Exception as e:                       # noqa: BLE001
            state = "error"
            err = f"{type(e).__name__}: {str(e)[:300]}"
            traceback.print_exc()
        c2 = _jobdb()
        try:
            c2.execute("UPDATE job SET state=?, result=?, error=?, ms=? "
                       "WHERE id=?",
                       (state, res, err, int((time.time() - t0) * 1000), jid))
            c2.commit()
        finally:
            c2.close()

    threading.Thread(target=_run, daemon=True).start()
    return jid


def _job_get(jid: str) -> dict | None:
    c = _jobdb()
    try:
        row = c.execute("SELECT * FROM job WHERE id=?", (jid,)).fetchone()
    finally:
        c.close()
    if not row:
        return None
    jid_, op, state, result, error, t0, ms = row
    return {"job_id": jid_, "op": op or "", "state": state, "error": error or "",
            "ms": ms or int((time.time() - t0) * 1000),
            "result": json.loads(result) if result else None}


def _v1_start_job(body: dict) -> dict:
    """POST /v1/jobs 的分发。参数与同步接口完全一致，只是不等着。"""
    _jobs_prune()
    op = (body.get("op") or "").strip()
    if op == "plan":
        q = (body.get("q") or "").strip()
        if not q:
            return {"ok": False, "error": "空问题"}
        return {"ok": True, "job_id": _job_start(lambda: v1_plan(q), "plan")}
    if op == "gather":
        plan = body.get("plan") or {}
        if not plan.get("tasks"):
            return {"ok": False, "error": "没有取证指令"}
        scope = (body.get("scope") or "kb").strip()
        if scope not in ("kb", "web", "both"):
            scope = "kb"
        return {"ok": True, "job_id": _job_start(
            lambda: v1_gather(plan, body.get("run_id") or "", scope), "gather")}
    if op == "write":
        picked = body.get("picked") or []
        if not picked:
            return {"ok": False, "error": "没有选中任何文件"}
        return {"ok": True, "job_id": _job_start(lambda: v1_write(
            (body.get("q") or "").strip(), body.get("plan") or {}, picked,
            body.get("run_id") or "", body.get("default")), "write")}
    return {"ok": False, "error": f"未知 op：{op or '(空)'}，只支持 plan/gather/write"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _denied(self) -> bool:
        """PV_TOKEN 设了才校验。/v1/health 不校验，给探活用。"""
        if not TOKEN:
            return False
        if self.headers.get("X-PV-Token") == TOKEN:
            return False
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return False
        self._json({"ok": False, "error": "未授权：缺 X-PV-Token 或 Authorization: Bearer"}, 401)
        return True

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    def _v1(self, path: str, body: dict) -> None:
        """同步 JSON 出口。异常一律转成 {"ok":false,"error":...}，
        不让低代码平台拿到 HTML 错误页——那种 500 页面在魔笔里只会显示
        「请求失败」，排查时无从下手。"""
        try:
            if path == "/v1/plan":
                q = (body.get("q") or "").strip()
                if not q:
                    self._json({"ok": False, "error": "空问题"}, 400)
                    return
                self._json(v1_plan(q))
                return
            if path == "/v1/gather":
                plan = body.get("plan") or {}
                if not plan.get("tasks"):
                    self._json({"ok": False, "error": "没有取证指令"}, 400)
                    return
                scope = (body.get("scope") or "kb").strip()
                if scope not in ("kb", "web", "both"):
                    scope = "kb"
                self._json(v1_gather(plan, body.get("run_id") or "", scope))
                return
            if path == "/v1/jobs":
                r = _v1_start_job(body)
                self._json(r, 200 if r.get("ok") else 400)
                return
            if path == "/v1/write":
                picked = body.get("picked") or []
                if not picked:
                    self._json({"ok": False, "error": "没有选中任何文件"}, 400)
                    return
                self._json(v1_write(
                    (body.get("q") or "").strip(), body.get("plan") or {},
                    picked, body.get("run_id") or "", body.get("default")))
                return
            self._json({"ok": False, "error": f"未知接口 {path}"}, 404)
        except Exception as e:                           # noqa: BLE001
            self._json({"ok": False,
                        "error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)

    def _sse_run(self, gen) -> None:
        self._sse_start()
        try:
            for step, data in gen:
                msg = "data: " + json.dumps({"step": step, **data},
                                            ensure_ascii=False) + "\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc(limit=4)
            try:
                self.wfile.write(("data: " + json.dumps(
                    {"step": "error", "error": f"{type(e).__name__}: {e}",
                     "trace": tb}, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
        self.close_connection = True

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/v1/") and p != "/v1/health" and self._denied():
            return
        if p in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/v1/jobs/"):
            j = _job_get(p[len("/v1/jobs/"):])
            if not j:
                self._json({"ok": False, "error": "任务不存在或已过期（保留 1 小时）"}, 404)
                return
            self._json({"ok": True, **j})
            return
        if p == "/v1/coverage":
            try:
                self._json(v1_coverage())
            except Exception as e:                       # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        if p == "/v1/health":
            self._json({"ok": True, "t": int(time.time()),
                        "providers": [x["provider"] for x in PV_LIST]})
            return
        if p == "/v1/openapi.json":
            f = HERE / "openapi.json"
            self._json(json.loads(f.read_text(encoding="utf-8"))
                       if f.exists() else {"error": "openapi.json 未生成"}, 200)
            return
        if p == "/api/trace":
            qs = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        qs[k] = v
            rid = qs.get("run_id", "")
            self._json(trace_mod.summary(rid) if rid else {})
            return
        self.send_error(404, "not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?")[0]

        if path.startswith("/v1/"):
            if self._denied():
                return
            self._v1(path, body)
            return

        if path == "/api/plan":
            q = (body.get("q") or "").strip()
            if not q:
                self._json({"error": "空问题"}, 400)
                return
            self._sse_run(stage_plan(q))
            return

        if path == "/api/gather":
            plan = body.get("plan") or {}
            rid = body.get("run_id") or ""
            scope = (body.get("scope") or "kb").strip()
            if scope not in ("kb", "web", "both"):
                scope = "kb"
            if not plan.get("tasks"):
                self._json({"error": "没有取证指令"}, 400)
                return
            self._sse_run(stage_gather(rid, plan, scope))
            return

        if path == "/api/write":
            rid = body.get("run_id") or ""
            q = body.get("q") or ""
            plan = body.get("plan") or {}
            picked = body.get("picked") or []
            default = body.get("default") or []
            if not rid:
                self._json({"error": "缺 run_id"}, 400)
                return
            self._sse_run(stage_write(rid, q, plan, picked, default))
            return

        if path == "/api/ev":
            # 前端行为埋点。没有 run_id 也收（比如页面一打开就想记一条），
            # 但那样归不到任何一次问答上，所以只允许在 q_start 之前用。
            name = (body.get("event") or "").strip()
            if not name:
                self._json({"ok": False, "error": "缺 event"}, 400)
                return
            extra = body.get("data") or {}
            if not isinstance(extra, dict):
                extra = {"raw": str(extra)[:200]}
            rid = body.get("run_id") or ""
            trace_mod.ev(rid, name, int(body.get("ms") or 0), **extra)
            self._json({"ok": True})
            return

        if path == "/api/mark":
            rid = body.get("run_id") or ""
            if not rid:
                self._json({"error": "缺 run_id"}, 400)
                return
            trace_mod.mark(rid, body.get("kind") or "user", 
                           body.get("target") or "", body.get("note") or "")
            self._json({"ok": True, "n": len(trace_mod.marks_of(rid))})
            return

        self.send_error(404, "not found")


def main() -> int:
    print("有据 · 政策依据梳理")
    print("  模型：" + " → ".join(f"{x['provider']}/{x['model']}" for x in PV_LIST))
    try:
        cov = v1_coverage()
        ft, cat = cov["fulltext"], cov["catalog"]
        print(f"  数据：全文 {ft['docs']:,} 份 / {ft['blocks']:,} 块 · "
              f"目录 {cat['items']:,} 条 · {cat['regions']:,} 个地域")
        # 打印全部地域，不截断。原来只印前 5 个，补了昭通之后印不出来——
        # 而「昭通在不在库里」正是启动横幅最该回答的问题。
        rows = sorted(ft["by_region"].items(), key=lambda kv: -kv[1])
        print("  分布：" + " · ".join(f"{k} {v:,}" for k, v in rows))
        blocked = (cov.get("web") or {}).get("blocked") or []
        print(f"  联网：{len(blocked)} 个站标记为不可达（直接返回原因，不硬等超时）")
    except Exception as e:                    # noqa: BLE001
        print(f"  数据：读取失败（{type(e).__name__}），服务仍然启动")
    n = _jobs_recover()
    if n:
        print(f"  任务：{n} 个上次未完成的任务已标为中断")
    print(f"  打开：http://127.0.0.1:{PORT}")
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
