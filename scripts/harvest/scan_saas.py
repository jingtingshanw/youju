"""扫描失败站点，看有多少用的是 p.so-gov.cn 这套政务搜索 SaaS。

发现方式：福建站的「政策文件库」页引了 p.so-gov.cn 的 js/api.js，
接口是 POST https://api.so-gov.cn/query/s，靠 siteCode 区分租户。
如果某站的首页/文件库页也引了这套 SaaS，就能用同一套适配器取数。

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
from catalog import net                                              # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "catalog.db"
OUT = ROOT / "data" / "catalog" / "saas_sites.json"

# 首页之外再试几个「政策文件库」常去的位置。很多站首页只有一个跳转，
# 真正的文件库页才引 SaaS 的脚本。
PROBE_PATHS = ("/", "/zck/", "/zcwj/", "/zhengce/", "/zfxxgk/",
               "/zwgk/zcwj/", "/zfxxgk/zcwj/")

_SITECODE = re.compile(r"""siteCode["'\]]*\s*[=:]\s*["']([0-9A-Za-z_\-]{6,40})["']""")
_INPUT = re.compile(r"""id=["']siteCode["'][^>]*value=["']([^"']+)["']""")


def load_failed() -> list[tuple[str, str, str]]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)
    rows = [(r[0], r[1], r[2]) for r in c.execute(
        "SELECT name,base,level FROM local_site "
        "WHERE status IN ('blocked','error') ORDER BY level,name")]
    c.close()
    return rows


def sniff(name: str, base: str) -> dict:
    """抓几页，看有没有 so-gov.cn 与 siteCode。"""
    hit, pages = [], 0
    for p in PROBE_PATHS:
        u = base.rstrip("/") + p
        try:
            h = L._get(u, base + "/", timeout=12)
        except Exception:                                        # noqa: BLE001
            continue
        pages += 1
        if "so-gov.cn" in h:
            hit.append(u)
            m = _INPUT.search(h) or _SITECODE.search(h)
            if m:
                return {"name": name, "base": base, "page": u,
                        "site": m.group(1), "pages": pages}
        if pages >= 3:
            break
    return {"name": name, "base": base, "page": hit[0] if hit else "",
            "site": "", "pages": pages}


def main() -> None:
    todo = load_failed()
    print(f"待扫 {len(todo)} 个失败站")
    found, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(sniff, n, b): (n, lv) for n, b, lv in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            n, lv = futs[fu]
            try:
                r = fu.result()
            except Exception:                                    # noqa: BLE001
                continue
            r["level"] = lv
            if r["page"]:
                found.append(r)
                mark = "★" if r["site"] else "·"
                print(f"{mark} {n:12s} [{lv}] site={r['site'] or '?':14s} {r['page'][:56]}")
            if i % 100 == 0:
                print(f"  …{i}/{len(todo)}  已发现 {len(found)}  "
                      f"{time.time() - t0:.0f}s")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n用到 so-gov.cn 的 {len(found)} 个，其中拿到 siteCode "
          f"{sum(1 for x in found if x['site'])} 个 → {OUT}")


if __name__ == "__main__":
    main()
