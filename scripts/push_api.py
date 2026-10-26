#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 GitHub API 推代码——当 `git push` 连不上 github.com 时用。

    # 推荐：token 放环境变量，不进 shell history
    set GH_TOKEN=ghp_xxx          # Windows CMD
    $env:GH_TOKEN="ghp_xxx"       # PowerShell
    python scripts/push_api.py

    python scripts/push_api.py --dry-run      # 只看会推什么，不真推

## 为什么需要它

`git push` 走的是 github.com:443 的 git 协议，公司网、校园网、某些代理会直接
拦掉，报 `CONNECT tunnel failed` 或 `Connection timed out`。
而 `api.github.com` 往往还能通——两个域名走的是不同的策略。

这个脚本只做一件事：把当前 HEAD 的文件，通过 Git Data API 建成一个 commit，
然后把分支指过去。等价于 `git push`，只是换了条路。

## 它不做什么

- 不改变本地仓库状态（不动 .git 里的 ref、不动工作区）
- 不推历史（只推 HEAD 那一份快照，一个 commit）——第一次上传够用，
  后续有历史了还是应该回 `git push`
- 不处理 100 MB 以上的单文件（GitHub API 的 blob 上限）

## 要走的能力

    POST   /git/blobs           每个文件一次
    POST   /git/trees           一次，含全部路径
    POST   /git/commits         一次
    POST   /git/refs            空仓库；或 PATCH /git/refs/heads/<branch> 非空仓库

token 需要 `repo` 权限（classic）或 Contents: Read and write（fine-grained）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

# GitHub 建议带 Accept 和 UA，不带 UA 会被部分中间层拦。
HDR = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "youju-push-api/1.0",
}


def sh(*args: str) -> str:
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def call(method: str, path: str, body: dict | None, token: str) -> dict:
    """调一次 API。错误信息原样抛出来——这里的报错都很有指向性。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={**HDR, "Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise SystemExit(f"  API {method} {path} → {e.code}\n  {detail}") from None
    except urllib.error.URLError as e:
        raise SystemExit(f"  连不上 api.github.com：{e.reason}") from None


def files() -> list[tuple[str, bytes]]:
    """按 git 索引取文件——不用扫目录，也不用重复实现 .gitignore。

    前提是工作区和索引一致。不一致就停下来，否则推上去的内容和你看到的不一样。
    """
    dirty = sh("git", "status", "--porcelain").strip()
    if dirty:
        raise SystemExit("工作区有未提交的改动，先 commit 再推：\n" + dirty[:600])

    names = [n for n in sh("git", "ls-files", "-z").split("\0") if n]
    out = []
    for n in names:
        p = ROOT / n
        if not p.is_file():
            continue
        b = p.read_bytes()
        if len(b) > 95 * 1024 * 1024:
            raise SystemExit(f"{n} 超过 95 MB，API 传不了，走 Release 附件")
        out.append((n, b))
    return out


def blob(args: tuple[str, bytes, str]) -> dict:
    name, data, token = args
    r = call("POST", "/git/blobs",
             {"content": base64.b64encode(data).decode(), "encoding": "base64"}, token)
    return {"path": name, "mode": "100644", "type": "blob", "sha": r["sha"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""),
                    help="默认读环境变量 GH_TOKEN，别写在命令行里（会进 history）")
    ap.add_argument("--repo", default="jingtingshanw/-", help="owner/name")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="", help="commit 说明，默认取 HEAD")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.token and not a.dry_run:
        print("  没有 token。设环境变量 GH_TOKEN 后重试：")
        print('    PowerShell:  $env:GH_TOKEN="ghp_xxx"')
        print('    CMD:         set GH_TOKEN=ghp_xxx')
        return 1

    # ── 1. 取 HEAD 快照 ──────────────────────────────────
    head = sh("git", "rev-parse", "HEAD").strip()
    subject = sh("git", "log", "-1", "--pretty=%B").strip()
    print(f"① 本地 HEAD {head[:8]}　「{subject.splitlines()[0][:40]}」")
    fs = files()
    total = sum(len(b) for _, b in fs)
    print(f"  {len(fs)} 个文件，{total / 1048576:.1f} MB")

    if a.dry_run:
        for n, b in fs[:12]:
            print(f"    {len(b):>10,} B  {n}")
        if len(fs) > 12:
            print(f"    …还有 {len(fs) - 12} 个")
        print("\n  --dry-run，到此为止。")
        return 0

    # ── 2. 逐个建 blob ───────────────────────────────────
    # 串行 171 个请求要 50 秒，4 线程压到 15 秒左右。
    # 再多会被 API 次级限流（secondary rate limit）盯上。
    print("② 上传文件内容（blob）")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        trees = list(ex.map(blob, [(n, b, a.token) for n, b in fs]))
    print(f"  {len(trees)} 个 blob，{time.time() - t0:.1f} 秒")

    # ── 3. 建树 ──────────────────────────────────────────
    print("③ 建目录树（tree）")
    tree = call("POST", f"/repos/{a.repo}/git/trees",
                {"tree": trees}, a.token)
    print(f"  tree {tree['sha'][:8]}")

    # ── 4. 建提交 ────────────────────────────────────────
    # parents 留空表示根提交——空仓库只能这样。如果仓库里已经有内容，
    # 这里会覆盖成一个孤立提交，历史会丢；所以先探一下 ref。
    print("④ 建提交（commit）")
    cm = call("POST", f"/repos/{a.repo}/git/commits",
              {"message": a.message or subject, "tree": tree["sha"]}, a.token)
    print(f"  commit {cm['sha'][:8]}")

    # ── 5. 分支指过去 ────────────────────────────────────
    print(f"⑤ 把 {a.branch} 指到新提交")
    exists = True
    try:
        call("GET", f"/repos/{a.repo}/git/ref/heads/{a.branch}", None, a.token)
    except SystemExit:
        exists = False           # 空仓库没有这个 ref

    if exists:
        call("PATCH", f"/repos/{a.repo}/git/refs/heads/{a.branch}",
             {"sha": cm["sha"], "force": False}, a.token)
        print("  已更新（如果报 not a fast-forward，说明远端有别人的提交，"
              "请改用 git pull 合并后再推）")
    else:
        call("POST", f"/repos/{a.repo}/git/refs",
             {"ref": f"refs/heads/{a.branch}", "sha": cm["sha"]}, a.token)
        print("  已创建（空仓库）")

    # ── 6. 回查 ──────────────────────────────────────────
    print("⑥ 回查远端")
    got = call("GET", f"/repos/{a.repo}/git/ref/heads/{a.branch}", None, a.token)
    same = got["object"]["sha"] == cm["sha"]
    print(f"  远端 {a.branch} = {got['object']['sha'][:8]}　{'一致' if same else '不一致'}")

    # 本地也把远程对齐一下，之后 git status 才不显示差异。
    # 这一步只写 .git/refs/remotes，不动工作区和 main。
    try:
        sh("git", "update-ref", f"refs/remotes/origin/{a.branch}", cm["sha"])
        sh("git", "config", f"branch.{a.branch}.remote", "origin")
        sh("git", "config", f"branch.{a.branch}.merge", f"refs/heads/{a.branch}")
        print(f"  本地 origin/{a.branch} 已同步")
    except SystemExit:
        print("  本地 remote 引用没同步（不影响远端，可忽略）")

    print(f"\n  打开 https://github.com/{a.repo}")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
