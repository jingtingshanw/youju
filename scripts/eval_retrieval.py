"""检索对比：基线（块级 BM25 top-8）vs 三路召回 + 融合 + 重排。

10 条真实规划业务问题，人工判定「期望命中的文件」是否出现在 top-3 / top-8。
跑法：
    python scripts/eval_retrieval.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import core  # noqa: E402

# (问题, 期望命中的文件名关键词)
CASES = [
    ("《历史文化名城名镇名村保护条例》第三十三条对历史建筑修缮是怎么规定的", "历史文化名城名镇名村保护条例"),
    ("老旧小区改造的资金从哪来", "老旧小区改造配套设施建设"),
    ("征收补偿标准怎么确定", "国有土地上房屋征收与补偿条例"),
    ("国土空间规划编制审批流程", "全面开展国土空间规划工作"),
    ("城市更新中历史建筑可以改变用途吗", "历史文化名城名镇名村保护条例"),
    ("城市更新防止大拆大建有什么要求", "防止大拆大建"),
    ("历史文化街区怎么划定保护范围", "历史文化名城名镇名村保护条例"),
    ("建设工程消防设计审查由谁负责", "消防设计审查"),
    ("耕地占补平衡怎么落实", "占补平衡"),
    ("村庄规划编制有什么要求", "村庄规划"),
]

r = core.Retriever()
pv = core.pick_provider(core.load_env())
rows = []
for qs, want in CASES:
    q = core.Query(qs)
    b = r.search_baseline(q, k=8)["hits"]
    m = r.search(q, k=8)["hits"]
    def rank(hits):
        for i, h in enumerate(hits, 1):
            if want in h["title"] or h["title"] in want:
                return i
        # 放宽：标题关键词的前 6 字
        for i, h in enumerate(hits, 1):
            if want[:6] in h["title"]:
                return i
        return 0
    rb, rm = rank(b), rank(m)
    rows.append((qs, want, rb, rm))
    print(f"{'✓' if rm and (not rb or rm <= rb) else ('=' if rm == rb else '×')} "
          f"基线@{rb or '—':<3} 改进@{rm or '—':<3} {qs[:34]}")

def topn(xs, n): return sum(1 for x in xs if x and x <= n)
rb = [x[2] for x in rows]; rm = [x[3] for x in rows]
print(f"\n{'':14s} 基线   改进")
print(f"top-3 命中    {topn(rb,3):>2}/{len(rows)}   {topn(rm,3):>2}/{len(rows)}")
print(f"top-8 命中    {topn(rb,8):>2}/{len(rows)}   {topn(rm,8):>2}/{len(rows)}")
print(f"未命中        {rb.count(0):>2}/{len(rows)}   {rm.count(0):>2}/{len(rows)}")
print(f"平均名次(命中) "
      f"{sum(x for x in rb if x)/max(1,topn(rb,8)):.2f}   "
      f"{sum(x for x in rm if x)/max(1,topn(rm,8)):.2f}")
r.close()
