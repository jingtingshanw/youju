#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""建立「城市名 → 政府门户域名」对照表。

为什么要有这一步
----------------
要抓市县级目录，第一件事是知道每个市的网站域名。国务院办公厅 2018 年那份
《关于加强政府网站域名管理的通知》规定：地市政府门户用 `www.□□□.gov.cn`，
□□□ 是地区名的拼音——**规则是真的，但拼音不能猜**。江西「鹰潭」是
`yingtan.gov.cn`，吉林「四平」是 `siping.gov.cn`，但「长治」是 `changzhi`、
「佳木斯」是 `jiamusi`，一路猜下去一定会错一批，而错的那种最难发现：
抓不到会被记成 `blocked`，看起来像「这个站不支持抓取」，其实是域名写错了。

所以域名清单必须来自外部，不能生成。这里用两个来源交叉：

  1. `www.mhdh.cn`（政府门户导航）—— 按省分组，含省/市/县三级，首页给一批、
     每省 `/省拼音` 详情页给全量。
  2. 省级政府门户首页上如果直接挂了「市州政府网站」链接，一并收进来。

结果落 `data/catalog/cities.csv`（地域, 门户, 所属省）。**这份表只是候选**：
真正的可信度靠后面的探测定——探测通了才写进目录库，探测不通就如实记失败。
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

_SELF = Path(__file__).resolve()
ROOT = _SELF.parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import net                                  # noqa: E402

OUT = ROOT / "data" / "catalog" / "cities.csv"
NAV = "https://www.mhdh.cn"

_SECTION = re.compile(r'<section class="cat-row">(.*?)</section>', re.S)
_LABEL = re.compile(r'<div class="cat-label"><a[^>]*>([^<]+)</a>')
_MORE = re.compile(r'<a class="cat-more" href="([^"]+)"')
_ITEM = re.compile(
    r'<a class="cat-item"[^>]*href="[^"]*"[^>]*title="(https?://[^"]+)"[^>]*>([^<]+)</a>')


def _clean_region(s: str) -> str:
    s = s.strip()
    for suf in ("各级人民政府", "人民政府", "人民政府办公室", "政府"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


PROV_NAMES = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆",
}


def _clean_prov(s: str) -> str:
    s = s.strip()
    s = re.sub(r"(各级人民政府|人民政府)$", "", s)
    return s.strip()


def _parse(html: str, prov: str = "") -> list[tuple[str, str, str]]:
    """从一页里提取 (地域, 门户, 省)。prov 为空时从 section 标题取。"""
    out = []
    for sec in _SECTION.findall(html):
        lm = _LABEL.search(sec)
        p = _clean_prov(lm.group(1)) if lm else prov
        if not p:
            continue
        for url, name in _ITEM.findall(sec):
            region = _clean_region(name)
            if not region or not url.startswith("http"):
                continue
            # 只认地方。这个导航站首页同时挂着「国务院部门」和「特别网站」，
            # 不过滤的话「国家民族事务委员会」会被当成一个市塞进市级清单，
            # 后面批量抓市级时会去抓部委的站，白花时间还污染统计。
            if p not in PROV_NAMES:
                continue
            out.append((region, url.rstrip("/"), p))
    return out


def _more_pages(html: str) -> list[str]:
    seen, out = set(), []
    for href in _MORE.findall(html):
        if href.startswith("/") and href not in seen:
            seen.add(href)
            out.append(NAV + href)
    return out


def level_of(region: str) -> str:
    """三级判定。

    第一版把所有非省级都叫「市级」，结果上海的黄浦区、浦东新区全被记成地级市，
    在目录里跟承德市平级——搜索「上海」时会跳出一串区。
    区县的判据很稳：以「区 / 县 / 旗」结尾，或名字里带「自治县」。
    """
    r = region.strip()
    if r.endswith(("省", "自治区")) or r in ("北京市", "天津市", "上海市", "重庆市"):
        return "省级"
    if re.search(r"(区|县|旗)$", r) or "自治县" in r:
        return "县级"
    return "市级"


def build(probe_detail: bool = True) -> int:
    print(f"抓导航首页 {NAV}")
    home = net.get(NAV, timeout=25, interval=0.5, retries=2, insecure=True)
    rows = _parse(home)
    print(f"  首页 {len(rows)} 条")

    if probe_detail:
        for u in _more_pages(home):
            if "/tebie" in u or "/guowuyuanbumen" in u:
                continue          # 特别网站 / 国务院部门，不是地方
            try:
                page = net.get(u, timeout=25, interval=0.5, retries=1,
                               insecure=True, referer=NAV + "/")
            except net.FetchError as e:
                print(f"  {u} 失败：{e}")
                continue
            got = _parse(page)
            print(f"  {u.split('/')[-1]:12s} {len(got):4d} 条")
            rows += got

    # 去重：同一域名只留第一次出现的（首页的省分组比详情页可靠）
    by: dict[str, tuple[str, str, str]] = {}
    for region, url, prov in rows:
        dom = re.sub(r"^https?://", "", url).split("/")[0]
        by.setdefault(dom, (region, url, prov))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["地域", "门户", "所属省", "层级"])
        for dom, (region, url, prov) in sorted(by.items(), key=lambda x: x[1][2]):
            w.writerow([region, url, prov, level_of(region)])
    from collections import Counter
    c = Counter(level_of(r) for _, (r, _, _) in by.items())
    print(f"\n共 {len(by)} 个域名（" +
          "　".join(f"{k} {v}" for k, v in c.most_common()) + f"）→ {OUT}")
    return len(by)


def main() -> None:
    build(probe_detail="--fast" not in sys.argv)


if __name__ == "__main__":
    main()
