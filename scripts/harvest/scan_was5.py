"""在抓不到的站点上找 TRS WAS5 检索服务，验证它能不能取数。

为什么要单开一轮扫描：WAS5 是国内政务站最常见的 CMS，它的检索服务直接吐
JSON（count / pagenum / docs[]，每条带 fileno 发文字号），比解析列表页可靠。
福建就是靠这个从 0 条变成 6,340 条。

识别方式两条：
  ① 首页有 `<input name="channelid" value="219598">` 这类搜索表单
  ② 页面里出现 `/was5/web/search?channelid=NNN` 的链接
拿到 channelid 之后必须**实测**一次：青海也识别出了 channelid，
但那个 WAS5 服务对匿名请求回的是 TRS 错误页，不是 JSON。识别出来不等于能用。

只读探测，不改库。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harvest.local_catalog as L                                    # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "catalog.db"
OUT = ROOT / "data" / "catalog" / "was5_sites.json"

_FORM = re.compile(r"""name=["']channelid["'][^>]*value=["'](\d{3,10})["']""")
_QUERY = re.compile(r"/was5/web/(?:search|search\.jsp)[^\"'<>]*?channelid=(\d{3,10})")
_HAS = re.compile(r"(/was5/web/search|name=[\"']channelid[\"'])")


def load_failed() -> list[tuple[str, str, str]]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    rows = [(r[0], r[1], r[2]) for r in c.execute(
        "SELECT name,base,level FROM local_site "
        "WHERE status IN ('blocked','error') ORDER BY level,name")]
    c.close()
    return rows


def probe(name: str, base: str) -> dict:
    base = base.rstrip("/")
    try:
        h = L._get(base + "/", base + "/", timeout=12)
    except Exception:                                            # noqa: BLE001
        return {"name": name, "base": base, "ok": False, "why": "首页不可达"}
    if not _HAS.search(h):
        return {"name": name, "base": base, "ok": False, "why": "无 WAS5 痕迹"}

    cid = ""
    m = _FORM.search(h) or _QUERY.search(h)
    if m:
        cid = m.group(1)
    if not cid:
        return {"name": name, "base": base, "ok": False, "why": "有 WAS5 但取不到 channelid"}

    # 实测：能拿到 JSON 且 count>0 才算可用
    api = base + "/was5/web/search"
    q = urllib.parse.urlencode({"channelid": cid, "templet": "docs.jsp",
                                "searchword": "政府", "page": 1, "perpage": 3})
    try:
        d = json.loads(L._get(f"{api}?{q}", base + "/", timeout=20), strict=False)
    except Exception as e:                                       # noqa: BLE001
        return {"name": name, "base": base, "ok": False, "cid": cid,
                "why": f"有 channelid={cid} 但返回不是 JSON（{type(e).__name__}）"}
    cnt = int(d.get("count") or 0)
    if cnt <= 0:
        return {"name": name, "base": base, "ok": False, "cid": cid,
                "why": f"channelid={cid} 可用但 count=0"}
    sample = [x.get("title", "")[:26] for x in (d.get("docs") or [])[:2]]
    return {"name": name, "base": base, "ok": True, "cid": cid,
            "count": cnt, "sample": sample, "api": api}


def main() -> None:
    todo = load_failed()
    print(f"待扫 {len(todo)} 个失败站", flush=True)
    got, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(probe, n, b): (n, lv) for n, b, lv in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            n, lv = futs[fu]
            try:
                r = fu.result()
            except Exception:                                    # noqa: BLE001
                continue
            r["level"] = lv
            if r.get("ok"):
                got.append(r)
                print(f"★ {n:14s} [{lv}] cid={r['cid']:9s} count={r['count']:7d} "
                      f"{' / '.join(r['sample'])[:46]}", flush=True)
            if i % 100 == 0:
                print(f"  …{i}/{len(todo)}  可用 {len(got)}  {time.time() - t0:.0f}s",
                      flush=True)
    got.sort(key=lambda x: -x["count"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nWAS5 可用 {len(got)} 个站 → {OUT}")
    for r in got[:15]:
        print(f"   {r['name']:14s} cid={r['cid']:9s} {r['count']:7d}")


if __name__ == "__main__":
    main()
