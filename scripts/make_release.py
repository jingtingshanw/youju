#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打一个能发出去的版本。

    python scripts/make_release.py               # 代码包 + 数据包 + 说明
    python scripts/make_release.py --code-only   # 只要代码包（几百 KB，改代码用）

## 为什么拆两个包

数据 2.4 GB，代码 400 KB。混在一起每次改一行字都要重传 700 MB，没人受得了。
所以拆成「代码包」和「数据包」：改代码只传前者，数据只在补库之后传一次。

## 产物

    dist/pv-code-<日期>.tar.gz     代码 + 文档，几百 KB
    dist/pv-data-<日期>.tar.xz     三个数据文件，约 600—800 MB
    dist/RELEASE-<日期>.md         这个版本是什么、怎么装、自检结果

数据包会先做 `wal_checkpoint(TRUNCATE)`。不做的话 `-wal` 里最近的写入
不会进主库文件，传上去的库会缺一块——而缺的那块不报错，只是查不到。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
PORT = int(os.environ.get("PV_PORT", "8848"))

# 代码包要带的东西。白名单而不是黑名单——黑名单漏一条就是泄漏一个 key
# 或者多传 2 GB，这两种错都很难在打包完立刻发现。
CODE_INCLUDE = [
    "app", "scripts", "prompts", "docs", "prd",
    "README.md", "DEPLOY.md", "requirements.txt", ".env.example", "run.sh",
    "启动有据.bat",
]
# 白名单里面还有这些不要
CODE_SKIP = {
    "__pycache__", ".pytest_cache", ".ipynb_checkpoints",
    "results", "reports", "node_modules", ".shots",
}
CODE_SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".db-wal", ".db-shm"}

DATA_FILES = ["data/kb.db", "data/catalog.db", "data/fulltext"]


def _hr(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def checkpoint() -> None:
    """把 WAL 里的写入并回主库，然后删掉 wal/shm。

    这一步不能省。SQLite 在 WAL 模式下，最近的写入先落在 `-wal` 文件里，
    主库文件可能落后几百 MB。只拷主库 = 拷一个旧的库，而且不报错。

    但反过来的风险更大：**服务还开着的时候删 `-shm`，那个进程会当场崩**。
    所以先探端口，开着就拒绝往下走；checkpoint 返回 busy 也不删文件。
    """
    c = None
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.6):
            raise SystemExit(
                f"[x] 127.0.0.1:{PORT} 还在监听，服务没停。\n"
                "    打包要 checkpoint 并删 -wal/-shm，服务开着做这件事会让它崩。\n"
                f"    先停掉服务再跑（找 PID：netstat -ano | grep :{PORT}）。")
    except (socket.timeout, ConnectionRefusedError, OSError):
        pass

    for db in (ROOT / "data" / "kb.db", ROOT / "data" / "catalog.db"):
        if not db.exists():
            continue
        try:
            c = sqlite3.connect(db, timeout=30)
            busy, _log, _ck = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            c.close()
            c = None
            print(f"  checkpoint {db.name}　busy={busy}")
            if busy:
                # 还有别的连接在读，TRUNCATE 没做干净，删 wal 会丢数据
                print(f"  ⚠ {db.name} 未能独占，跳过删 wal（别的进程还开着这个库）")
                continue
            for suf in ("-wal", "-shm"):
                p = db.with_name(db.name + suf)
                if p.exists():
                    p.unlink()
                    print(f"  清理 {p.name}")
        except sqlite3.Error as e:
            print(f"  ⚠ checkpoint {db.name} 失败：{e}")
        finally:
            if c is not None:
                c.close()

    # app.db 是埋点库，不发货，顺手清掉 wal 就行
    for suf in ("-wal", "-shm"):
        p = ROOT / "data" / ("app.db" + suf)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def code_tar(out: Path) -> int:
    def keep(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        p = Path(ti.name)
        if any(part in CODE_SKIP for part in p.parts):
            return None
        if p.suffix in CODE_SKIP_SUFFIX:
            return None
        return ti

    with tarfile.open(out, "w:gz") as tf:
        for name in CODE_INCLUDE:
            p = ROOT / name
            if not p.exists():
                print(f"  ⚠ 缺 {name}")
                continue
            tf.add(p, arcname=f"policy-verify-engine/{name}", filter=keep)
    return out.stat().st_size


def data_tar(out: Path, xz: bool = False) -> int:
    """打数据包。

    默认 gzip，不用 xz。理由不是压缩率，是时间成本：2.4 GB 走 xz -6
    单核要二三十分钟，gzip -6 三四分钟，体积差 30% 左右。
    补一次库就要重打一次包，把每次几十分钟的等待换成一次多传 300 MB，
    这个交换在「随时要能发版」的场景里是划算的。要极限体积加 `--xz`。
    """
    missing = [f for f in DATA_FILES if not (ROOT / f).exists()]
    if missing:
        print(f"  ⚠ 缺数据：{missing}")
    opener = (lambda: tarfile.open(out, "w:xz", preset=6)) if xz else \
             (lambda: tarfile.open(out, "w:gz", compresslevel=6))
    with opener() as tf:
        for name in DATA_FILES:
            p = ROOT / name
            if p.exists():
                print(f"  打包 {name} …", flush=True)
                tf.add(p, arcname=name)
    return out.stat().st_size


def stats() -> dict:
    """读规模数字写进 RELEASE。

    用 `mode=ro` 打开 —— 打包期间不该有任何写入。路径要转成 `as_posix()`：
    Windows 的反斜杠在 `file:` URI 里不是分隔符，拼出来会变成「打不开数据库」，
    而这个报错指向的又是「文件不存在」，很容易查错方向。
    """
    out: dict = {}
    cat = kb = None
    try:
        cat = sqlite3.connect(
            f"file:{(ROOT / 'data' / 'catalog.db').as_posix()}?mode=ro", uri=True)
        kb = sqlite3.connect(
            f"file:{(ROOT / 'data' / 'kb.db').as_posix()}?mode=ro", uri=True)
        out["items"] = cat.execute("SELECT count(*) FROM items").fetchone()[0]
        out["regions"] = cat.execute(
            "SELECT count(DISTINCT region) FROM items WHERE coalesce(region,'')<>''"
        ).fetchone()[0]
        out["docs"] = kb.execute("SELECT count(*) FROM docs").fetchone()[0]
        out["chars"] = kb.execute(
            "SELECT coalesce(sum(chars),0) FROM docs").fetchone()[0]
        out["blocks"] = kb.execute("SELECT count(*) FROM blocks").fetchone()[0]
        out["articles"] = kb.execute(
            "SELECT count(*) FROM blocks WHERE kind='art'").fetchone()[0]
        out["by_region"] = kb.execute(
            "SELECT region, count(*) n FROM docs GROUP BY 1 ORDER BY n DESC"
        ).fetchall()
        out["ledger"] = dict(cat.execute(
            "SELECT status, count(*) FROM eff_ledger GROUP BY 1").fetchall())
    except sqlite3.Error as e:
        out["error"] = str(e)
    finally:
        for c in (cat, kb):
            if c is not None:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
    return out


def selftest() -> tuple[int, int, str]:
    """跑一遍自检，把结果写进 RELEASE。发布前必须过。"""
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "selftest.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    txt = (p.stdout or "") + (p.stderr or "")
    n_pass = n_total = 0
    for line in txt.splitlines():
        if "通过" in line and "/" in line:
            import re
            m = re.search(r"通过 (\d+)/(\d+)", line)
            if m:
                n_pass, n_total = int(m.group(1)), int(m.group(2))
    return n_pass, n_total, txt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code-only", action="store_true")
    ap.add_argument("--xz", action="store_true", help="数据包用 xz（更小，慢十倍）")
    ap.add_argument("--reuse-data", action="store_true",
                    help="数据包已经打好了就别重打（只改了代码时用，省两分钟）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    day = dt.date.today().strftime("%Y%m%d")
    DIST.mkdir(exist_ok=True)
    d_ext = "tar.xz" if a.xz else "tar.gz"

    print("① 自检")
    n_pass, n_total, log = selftest()
    ok = n_pass == n_total and n_total >= 30
    print(f"  {n_pass}/{n_total}  {'通过' if ok else '未通过'}")
    if not ok:
        # 先自检再打包，而不是打完再检查：自检不过的版本不该产出文件。
        # 产出了就一定会被传上去——「反正已经打好了」是最常见的一次事故。
        print("\n[x] 自检没过，不打这个包。看 scripts/selftest.py 的输出：")
        for line in log.splitlines():
            if "✗" in line or "失败" in line or "not ok" in line.lower():
                print("   " + line.strip())
        return 1

    print("\n② 收缩数据库")
    checkpoint()

    print("\n③ 代码包")
    code = DIST / f"pv-code-{day}.tar.gz"
    n_code = code_tar(code)
    print(f"  {code.name}  {_hr(n_code)}")

    n_data = 0
    data = DIST / f"pv-data-{day}.{d_ext}"
    if not a.code_only:
        if a.reuse_data and data.exists():
            n_data = data.stat().st_size
            print(f"\n④ 数据包 沿用 {data.name}  {_hr(n_data)}（--reuse-data）")
        else:
            print(f"\n④ 数据包（2.4 GB，{'xz 要二三十分钟' if a.xz else 'gzip 约四分钟'}）")
            n_data = data_tar(data, xz=a.xz)
            print(f"  {data.name}  {_hr(n_data)}")

    st = stats()
    by_region = " · ".join(f"{r or '未标'} {n:,}" for r, n in (st.get("by_region") or [])[:8])
    n_region = len(st.get("by_region") or [])
    led = st.get("ledger") or {}
    st_warn = f"\n> ⚠ 读数据失败：{st['error']}（下面的数字不可信）\n" if st.get("error") else ""

    md = f"""# 发布包 {day}

打于 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}。

## 里面有什么

| 文件 | 体积 | 用途 |
|---|---|---|
| `pv-code-{day}.tar.gz` | {_hr(n_code)} | 代码与文档。改代码只传这个 |
| `pv-data-{day}.{d_ext}` | {_hr(n_data) if n_data else '（本次未打）'} | 三个数据文件。补库之后才需要重传 |
| `RELEASE-{day}.md` | — | 就是这个文件 |

## 这一版的数据

{st_warn}
| 项 | 值 |
|---|---|
| 目录条目 | {st.get('items', 0):,} 条，{st.get('regions', 0):,} 个地域 |
| 有正文 | {st.get('docs', 0):,} 份 / {(st.get('chars') or 0) / 1e8:.2f} 亿字 |
| 可检索块 | {st.get('blocks', 0):,} 块 |
| 条文块 | {st.get('articles', 0):,} 条 |
| 地域分布 | {by_region} |

时效台账：{led.get('no_signal', 0):,} 条未见信号 · {led.get('in_force', 0):,} 条期限内
· {led.get('repealed', 0):,} 条已废止 · **{led.get('expired', 0):,} 条有效期届满**
· {led.get('superseded', 0):,} 条旧版 · {led.get('pending', 0):,} 条尚未生效

## 自检

```
{n_pass}/{n_total} {'全部通过' if ok else '未全部通过 —— 不要发布'}
```

## 装

```bash
tar -xzf pv-code-{day}.tar.gz && cd policy-verify-engine
tar -x{'J' if a.xz else 'z'}f ../pv-data-{day}.{d_ext}  # 解到项目根，保持 data/ 结构
cp .env.example .env                              # 填一家模型的 key，设 PV_TOKEN
vim .env
python scripts/selftest.py                        # 过 30/30
python app/server.py                              # 打开 http://127.0.0.1:8848
```

完整部署（公网、systemd、Caddy、回滚）看 `DEPLOY.md`。

## 发出去之前的三件事

1. **设 `PV_TOKEN`。** 不设等于公网裸奔，几个小时就会被扫到。写在 `.env` 里就行，
   现在服务端也会读 `.env`（2026-10-26 之前只认系统环境变量，写 `.env` 等于没设）。
2. **`.env` 放项目根目录。** 代码按「项目根 → 上一级 → 上两级」找，第一个存在的生效。
   找不到的症状是**启动一切正常、一调模型就报「没有可用的模型通道」**。
3. **先自检再发版。** 自检不过的版本不要发——它不会报错，只是检索会少给几条，而少给几条会被读成「没有这份文件」。

## 已知边界

- 库里有正文的是 {n_region} 个地域口径：{by_region}。其余 1,400 多个地域只有目录，正文按需联网取。
- 广州明确不做（接口适配成本高于收益），库里 849 条目录保留，检索时会显示「仅目录」。
- 法律条文原文（人大网）没入库，规划最常引的那几部法只能给名录。
- 全稿自查（F-05）没做。
- 不走内网部署，不做涉密文件共享。
"""
    rp = DIST / f"RELEASE-{day}.md"
    rp.write_text(md, encoding="utf-8")
    print(f"\n  {rp.name}")

    print(f"\n产出在 {DIST}")
    for f in sorted(DIST.glob(f"*{day}*")):
        print(f"  {f.name:<28} {_hr(f.stat().st_size)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
