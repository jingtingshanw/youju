#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""埋点：把「这次哪里不对」记下来。

## 记什么

不是记鼠标轨迹，是记**能用来改东西的四类信号**：

    drop    系统推荐了，用户没勾     → 召回不准（关键词太宽）
    add     系统没勾，用户勾了       → 召回不全（关键词太窄，或分档判错）
    miss    某条指令一条都没找到     → 覆盖缺口（数据没有，或词不对）
    fail    生成的内容核验不通过     → 模型或资料的问题

前两类最有价值，因为它们不需要人工标注：**用户的勾选行为本身就是标注**。
一个方向 20 条候选，用户只勾 3 条，那 17 条为什么被丢，比任何离线评测都真实。

## 为什么不写日志文件

日志文件没人看，也不会被用来改进。写成 SQLite 表，`--report` 一条命令就能
按「哪条指令最常空」「哪类文件最常被丢」聚合——这才叫能用的 badcase 库。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id TEXT PRIMARY KEY, question TEXT, reading TEXT, city TEXT, site TEXT,
    plan_json TEXT, plan_source TEXT, n_tasks INTEGER, n_found INTEGER,
    n_body INTEGER, n_picked INTEGER, created_at REAL, ms INTEGER
);
CREATE TABLE IF NOT EXISTS event (
    run_id TEXT, at REAL, step TEXT, ms INTEGER, data TEXT
);
CREATE TABLE IF NOT EXISTS pick (
    run_id TEXT, at REAL, action TEXT, task_kind TEXT, title TEXT, doc_no TEXT,
    has_body INTEGER, bucket TEXT
);
CREATE TABLE IF NOT EXISTS mark (
    run_id TEXT, at REAL, kind TEXT, target TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS i_event_run ON event(run_id);
CREATE INDEX IF NOT EXISTS i_mark_run ON mark(run_id);
"""


def _conn() -> sqlite3.Connection:
    # 埋点是并发写的：plan / gather（10 个方向）/ write 可能同时往 event 表插。
    # 三个请求挤在一起时，默认 5 秒的等锁时间不够——一次写虽然只有毫秒级，
    # 但排队的人多了就会超时，超时的报错是 "database is locked"，
    # 而埋点失败不该影响主流程。所以给足 30 秒，并且失败也不抛。
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def new_run(question: str) -> str:
    rid = time.strftime("%y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    c = _conn()
    try:
        c.execute("INSERT INTO run (id,question,created_at) VALUES (?,?,?)",
                  (rid, question, time.time()))
        c.commit()
    finally:
        c.close()
    return rid


def set_plan(run_id: str, plan: dict) -> None:
    c = _conn()
    try:
        place = plan.get("place") or {}
        c.execute("UPDATE run SET reading=?,city=?,site=?,plan_json=?,plan_source=?,"
                  "n_tasks=? WHERE id=?",
                  (plan.get("reading", ""), place.get("city", ""), place.get("site", ""),
                   json.dumps(plan, ensure_ascii=False), plan.get("source", ""),
                   len(plan.get("tasks") or []), run_id))
        c.commit()
    finally:
        c.close()


def ev(run_id: str, step: str, ms: int = 0, **data) -> None:
    c = _conn()
    try:
        c.execute("INSERT INTO event (run_id,at,step,ms,data) VALUES (?,?,?,?,?)",
                  (run_id, time.time(), step, ms,
                   json.dumps(data, ensure_ascii=False)[:4000]))
        c.commit()
    finally:
        c.close()


def set_gather(run_id: str, n_found: int, n_body: int) -> None:
    c = _conn()
    try:
        c.execute("UPDATE run SET n_found=?,n_body=? WHERE id=?", (n_found, n_body, run_id))
        c.commit()
    finally:
        c.close()


def mark(run_id: str, kind: str, target: str, note: str = "") -> None:
    c = _conn()
    try:
        c.execute("INSERT INTO mark (run_id,at,kind,target,note) VALUES (?,?,?,?,?)",
                  (run_id, time.time(), kind, str(target)[:300], str(note)[:600]))
        c.commit()
    finally:
        c.close()


def picks(run_id: str, picked: list[dict], default: list[str],
          task_kind: dict[str, str]) -> dict:
    """记下勾选结果，并算出 drop / add 两类信号。

    `default` 是系统默认勾选的标题集合。用户把它取消了 = drop；
    勾了不在 default 里的 = add。
    """
    c = _conn()
    dset = set(default)
    got = {it["title"] for it in picked}
    drop = [t for t in dset if t not in got]
    add = [t for t in got if t not in dset]
    try:
        for it in picked:
            c.execute("INSERT INTO pick (run_id,at,action,task_kind,title,doc_no,"
                      "has_body,bucket) VALUES (?,?,?,?,?,?,?,?)",
                      (run_id, time.time(), "pick",
                       task_kind.get(it["title"], ""), it["title"][:200],
                       it.get("doc_no", "")[:40], int(bool(it.get("has_body"))),
                       it.get("bucket", "")))
        for t in drop:
            c.execute("INSERT INTO pick (run_id,at,action,task_kind,title,doc_no,"
                      "has_body,bucket) VALUES (?,?,?,?,?,?,?,?)",
                      (run_id, time.time(), "drop", task_kind.get(t, ""), t[:200],
                       0, 0, ""))
        c.execute("UPDATE run SET n_picked=? WHERE id=?", (len(picked), run_id))
        c.commit()
    finally:
        c.close()
    return {"n_pick": len(picked), "drop": drop, "add": add}


def marks_of(run_id: str) -> list[dict]:
    c = _conn()
    try:
        return [dict(r) for r in
                c.execute("SELECT kind,target,note,at FROM mark WHERE run_id=? "
                          "ORDER BY at", (run_id,))]
    finally:
        c.close()


def summary(run_id: str) -> dict:
    c = _conn()
    try:
        r = c.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        if not r:
            return {}
        return {"run": dict(r), "marks": marks_of(run_id),
                "n_drop": c.execute("SELECT COUNT(*) FROM pick WHERE run_id=? "
                                    "AND action='drop'", (run_id,)).fetchone()[0],
                "n_pick": c.execute("SELECT COUNT(*) FROM pick WHERE run_id=? "
                                    "AND action='pick'", (run_id,)).fetchone()[0]}
    finally:
        c.close()


def report(limit: int = 20) -> str:
    """按「哪条指令最常空」「哪类文件最常被丢」聚合。这是 badcase 的用处。"""
    c = _conn()
    L = ["# 埋点报告", ""]
    try:
        n = c.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        L += [f"累计 {n} 次运行。", ""]
        L += ["## 最近的问题", ""]
        for r in c.execute("SELECT id,question,n_found,n_picked,plan_source,created_at "
                           "FROM run ORDER BY created_at DESC LIMIT ?", (limit,)):
            L.append(f"- `{r['id']}` 找到 {r['n_found']} 条 / 勾选 {r['n_picked']} 条 "
                     f"（拆解来源 {r['plan_source'] or '—'}）｜{r['question'][:50]}")
        L += ["", "## 最常被丢掉的候选（推荐不准）", ""]
        for r in c.execute("SELECT task_kind,title,COUNT(*) n FROM pick "
                           "WHERE action='drop' GROUP BY title ORDER BY n DESC LIMIT 15"):
            L.append(f"- {r['n']} 次 · {r['task_kind'] or '—'} · {r['title'][:60]}")
        L += ["", "## 标记最多的问题类型", ""]
        for r in c.execute("SELECT kind,COUNT(*) n FROM mark GROUP BY kind ORDER BY n DESC"):
            L.append(f"- {r['kind']}：{r['n']} 次")
        L += ["", "## 用户手动标记", ""]
        for r in c.execute("SELECT kind,target,note FROM mark ORDER BY at DESC LIMIT 20"):
            L.append(f"- [{r['kind']}] {r['target'][:60]}"
                     + (f" —— {r['note'][:60]}" if r["note"] else ""))
    finally:
        c.close()
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--run", default="")
    a = ap.parse_args()
    if a.run:
        print(json.dumps(summary(a.run), ensure_ascii=False, indent=2))
    else:
        print(report())
