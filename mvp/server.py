#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""政策核验引擎 · 最小可运行 MVP（单文件 · 零第三方依赖）

它解决一个具体问题：**大模型引用政策条文时会编条号、会拿已废止的法规给依据。**
本服务在模型输出之后加一道确定性校验（不调模型），把结论降级为


    可作答 / 部分作答 / 拒答

并把每一项依据是否真实存在、所引法规是否还有效逐条列出来。

--------------------------------------------------------------------------------
和完整版（Dify 版）的关系
--------------------------------------------------------------------------------
完整版：Dify 0.15.3 自建（10 个容器）负责向量检索 + rerank + 生成，
        校验层 `scripts/gatekeeper.py` 接在后面。

本 MVP：**把 Dify 换成了 200 行的本地检索**，其余全部复用 —— 判定逻辑用的还是
        `scripts/legalkit.py` 与 `scripts/gatekeeper.py`，提示词用的还是
        `prompts/system-v3.md`。所以两版对同一个回答会给出**同样的判定**，
        区别只在检索质量。

这样做是为了让 MVP 能被直接拷走：不装 Docker、不建知识库、不配 Dify，
只要有 Python 和一个模型 key 就能跑起来看效果。

--------------------------------------------------------------------------------
怎么跑
--------------------------------------------------------------------------------
    python mvp/server.py                    # 起服务，浏览器开 http://127.0.0.1:8765
    python mvp/server.py --ask "问题"        # 命令行问一句
    python mvp/server.py --probe            # 自检：用评估集测检索召回
    python mvp/server.py --port 9000 --model deepseek-chat

模型 key 从 `ai-pm/.env` 读（火山方舟 / 百炼 / DeepSeek 任一家，OpenAI 兼容接口）。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ask as askmod  # noqa: E402  复用命令行层的结构化分析（含闸门 2）
import legalkit as lk  # noqa: E402

CORPUS_DIR = ROOT / "data" / "kb_text_v2"
# 地方目录库（省 / 市 / 县）。和 kb.db 的全文块索引是两个库：
# kb.db 存国家级文件的正文切块，catalog.db 存三级地方的「文件存在性」。
CAT_DB = ROOT / "data" / "catalog.db"
PROMPT_FILE = ROOT / "prompts" / "system-v3.md"
ENV_FILE = ROOT.parent.parent / ".env"
PAGE_FILE = HERE / "index.html"

# 语料每行的形状：《法规全称》第X条　正文
# 首行只有《法规全称》没有条号，章标题行形如「第二章　防治措施」，两者都不算条款。
ART_LINE_RE = re.compile(
    r"^《([^》]{2,80})》\s*第\s*([一二三四五六七八九十百千零〇0-9]{1,8})\s*条"
    r"(?:之[一二三四五六七八九十]{1,3})?\s*[　\s]*(.*)$"
)

# 少数文件没有「第X条」这种体例。典型是《全国人大常委会关于授权国务院在…的决定》——
# 它按「授权目录第N项」组织，正文形如：
#   《决定名》〔授权目录第1项〕 1 ｜ 《土地管理法》 第三十五条第一款 ……
# 这条正则兜住它。**第一版没有这个兜底，后果是这份文件在检索层完全不存在**：
# 8 部法规只切出 7 部，L2-08（问海南自贸区的土地征收审批权）必错，
# 而且错得没有提示 —— 模型会拿库里其他法规的内容去凑。
# 所以「一整个文件凭空消失」这类问题，最好由语料条数与 manifest 对账来发现（见 --probe）。
BLOCK_LINE_RE = re.compile(r"^《([^》]{2,80})》\s*〔([^〕]{1,40})〕\s*[　\s]*(.*)$")


# ══════════════════════════════════════════════════════════════════════════
# 一、检索层 —— 这是 MVP 相对完整版唯一退化的部件
# ══════════════════════════════════════════════════════════════════════════
#
# 完整版这一层是「向量召回 + rerank 精排」，靠的是 embedding 模型理解语义。
# MVP 里没有 embedding，用的是最朴素的做法：**中文 2-gram 倒排 + IDF 打分**。
#
# 为什么 2-gram 在这个场景够用（而不是随便选了个土办法）：
#   · 法律文本的用词高度固定（「责令改正」「统一监督管理」「行政处罚」），
#     用户提问和条文原文的**字面重合度**很高，不需要同义改写能力；
#   · 2-gram 不需要分词词典，中文场景下比「按空格切词」可靠得多；
#   · 语料只有 20 万字符，全量打分毫秒级，不需要近似检索。
#
# 它会在什么地方输给向量检索：用户用完全不同的说法问同一件事
# （问「谁来管」而条文写「实施统一监督管理」这种程度还行，
#  问「环保局管不管这个」而条文写「生态环境主管部门」就悬了）。
# 这个取舍写在 mvp/README.md 里，跑 --probe 可以看到实测召回。

def _grams(text: str, n: int = 2) -> list[str]:
    """中文 2-gram。先抹掉标点和空白，再滑窗。"""
    t = re.sub(r"[^\u4e00-\u9fff0-9A-Za-z]", "", text or "")
    if len(t) < n:
        return [t] if t else []
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def load_chunks() -> list[dict]:
    """把 data/kb_text_v2/ 下的全文切成「一条一款」。

    返回 [{"law": 法规全称, "art": 条号, "no": 条号整数, "text": 正文}, ...]
    没有条号的体例（见 `BLOCK_LINE_RE` 注释）用「〔…〕」里的标记当条号，`no` 记 None。
    """
    chunks: list[dict] = []
    for f in sorted(CORPUS_DIR.glob("*.txt")):
        law = f.stem
        cur: dict | None = None
        for raw in f.read_text(encoding="utf-8").splitlines():
            line = raw.strip().lstrip("\ufeff")
            if not line:
                continue
            m = ART_LINE_RE.match(line)
            if m:
                if cur:
                    chunks.append(cur)
                cur = {"law": law, "art": f"第{m.group(2)}条",
                       "no": lk.cn2int(m.group(2)), "text": m.group(3).strip()}
                continue
            m2 = BLOCK_LINE_RE.match(line)
            if m2:
                if cur:
                    chunks.append(cur)
                cur = {"law": law, "art": m2.group(2), "no": None,
                       "text": m2.group(3).strip()}
                continue
            if cur:
                # 续行：有的文件一条正文分多行；章标题也走这条路（无害）
                cur["text"] += " " + line
        if cur:
            chunks.append(cur)
    return chunks


class Retriever:
    """BM25（中文 2-gram 版）。

    打分公式用标准 BM25 而不是「Σ idf / sqrt(长度)」这种土办法，原因是实测踩过：
    L1-01 问「怀化市对扬尘污染防治实施统一监督管理的是哪个部门？」，
    正确答案是第五条（约 500 字，包含「实施统一监督管理」），
    但最初的 `/sqrt(len)` 把长文档惩罚得过狠，让只有 60 字的第七条排到了第一。
    换成 BM25 的长度归一（b=0.75，相对平均长度而非绝对平方根）之后，
    第五条那几条**词频高**的 gram（「扬尘」「污染防治」在条内反复出现）
    能靠 tf 饱和把分数拉回来 —— 这正是 BM25 相比朴素 idf 求和的意义。

    另外两处加权，也是实测调出来的：

    ① **法规名进文档**。把法规全称拼进每条的 gram 集合，于是「怀化」这两个字
       会为怀化条例的每一条贡献分数 —— 等于做了一次免费的法规路由。
       不做这一步，用户问「怀化市…」时，那些条文里根本没写「怀化」二字的题
       （如问「几日内清扫完毕」）会因为缺少这条线索而排到榜尾。

    ② **问题里点名的法规整体加权**（`LAW_BOOST`）。用户写了书名号或全称，
       就是在指定范围，该法规的条款整体上浮。
    """

    K1 = 1.2
    B = 0.75
    LAW_BOOST = 1.6  # 问题点名某法规时，该法规条款的加权

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.tf: list[Counter] = [
            Counter(_grams(c["law"] + c["text"])) for c in chunks
        ]
        self.dl = [sum(t.values()) or 1 for t in self.tf]
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 1.0
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = max(len(chunks), 1)
        self.idf = {g: math.log(1.0 + (n - d + 0.5) / (d + 0.5))
                    for g, d in df.items()}

    def search(self, query: str, k: int = 8) -> list[dict]:
        qg = set(_grams(query))
        # 问题里点名的库内法规（书名号精确匹配 + 正式全称）
        picked = set(lk.query_laws(query, include_full_names=True))
        scored: list[tuple[float, int]] = []
        for i, t in enumerate(self.tf):
            norm = self.K1 * (1.0 - self.B + self.B * self.dl[i] / self.avgdl)
            s = 0.0
            for g in qg:
                tf = t.get(g)
                if tf:
                    s += self.idf[g] * (tf * (self.K1 + 1.0)) / (tf + norm)
            if s <= 0.0:
                continue
            if self.chunks[i]["law"] in picked:
                s *= self.LAW_BOOST
            scored.append((s, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:k]]


def build_context(hits: list[dict]) -> str:
    """把召回条款拼成给模型的上下文。编号方便模型在回答里回指。"""
    lines = []
    for n, c in enumerate(hits, 1):
        lines.append(f"[{n}] 《{c['law']}》{c['art']}　{c['text']}")
    return "\n".join(lines)


# 把输出格式在这里再说一遍，理由是实测：system 里写了四节格式，但模型仍可能
# 只用一段无标题文字写结论（第一轮就发生了）——判定是对的，可产品要展示的
# 「结论」那一栏就抽不出来，用户看到的是一串条号。
# 在提问的最后再钉一次格式，成本是几十个 token，换回来的是展示层的稳定。
# 注意这里**不重复**判据本身（哪些算拒答、台账怎么查），那些仍在 system 里，
# 避免两处规则各说各话。
OUTPUT_SPEC = """请严格按下列四节顺序输出。标题照抄，**不要用 ``` 代码块包裹**：

### 1. 边界判定
【边界判定】
引用法规在库内：（有全文 / 仅被提及 / 不在库内，选一个）
所问条号核验：（存在 / 不存在 / 未指定，选一个）
时效状态：（现行有效 / 已废止 / 已失效 / 未知，选一个）
结论边界：（可作答 / 部分作答 / 拒答，选一个）

### 2. 结论
一句话。拒答时写「本次检索结果中没有能支持该问题的条文，无法给出结论。」

### 3. 依据
- 【《法规全名》第X条】原文关键句……

### 4. 引用清单
每行一条：【引用清单】法规全名|第X条
无依据时只写一行：【引用清单】无"""


# ══════════════════════════════════════════════════════════════════════════
# 二、模型调用 —— 直连 OpenAI 兼容接口，不走 Dify
# ══════════════════════════════════════════════════════════════════════════

# 按顺序探测，用第一个有 key 的。
PROVIDERS = [
    ("DASHSCOPE", "qwen-plus"),
    ("DEEPSEEK", "deepseek-chat"),
    ("ARK", "doubao-seed-1-6-250615"),
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            env[k.strip()] = v.strip()
    return env


def pick_provider(env: dict[str, str], override_model: str | None = None) -> dict:
    for pfx, default_model in PROVIDERS:
        key, base = env.get(pfx + "_API_KEY"), env.get(pfx + "_BASE_URL")
        if key and base:
            return {"provider": pfx.lower(), "api_key": key, "model": default_model,
                    "base": base.rstrip("/")}
    raise SystemExit(
        f"[x] {ENV_FILE} 里没有可用的模型 key。\n"
        f"    需要至少填一组 XXX_API_KEY / XXX_BASE_URL（DASHSCOPE / DEEPSEEK / ARK）。"
    )


def call_llm(pv: dict, system: str, user: str, timeout: int = 180) -> str:
    body = json.dumps({
        "model": pv["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": 1800,
    }).encode("utf-8")
    req = urllib.request.Request(pv["base"] + "/chat/completions", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + pv["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"模型接口 HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:300]}")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"模型接口异常：{type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 三、一次完整问答
# ══════════════════════════════════════════════════════════════════════════

class Engine:
    def __init__(self, model: str | None = None, topk: int = 8):
        self.chunks = load_chunks()
        self.retriever = Retriever(self.chunks)
        self.system = PROMPT_FILE.read_text(encoding="utf-8")
        env = load_env()
        self.pv = pick_provider(env)
        if model:
            self.pv["model"] = model
        self.topk = topk

    def answer(self, question: str) -> dict:
        """问题 → 检索 → 生成 → 校验 → 结构化结果。

        返回的字典直接就是前端要的形状（由 `ask.analyze` 生产，
        与命令行版逐字段相同）。
        """
        t0 = time.perf_counter()
        hits = self.retriever.search(question, k=self.topk)
        context = build_context(hits)
        user = (f"【检索到的条文】\n{context}\n\n"
                f"【用户问题】\n{question}\n\n"
                f"{OUTPUT_SPEC}")
        raw = call_llm(self.pv, self.system, user)
        elapsed = time.perf_counter() - t0

        # ★ 关键：把输出包装成 Dify 的形状，直接复用既有分析层。
        #   判定的每一行代码都是完整版在跑 66 题评估时验证过的那一份。
        #
        # content 必须带上「第X条」这几个字，而且要和给模型看的上下文逐字一样。
        # 最初只传了条文正文，结果闸门 2 的 C2 检查（`art_clean in c`）找不到
        # 「第五条」这个串，把一道**模型答得完全正确**的题降成了拒答。
        # 完整版不会踩这个坑，是因为 Dify 的分段本来就保留了条号。
        # 教训是：召回段落喂给模型和喂给校验层，必须是同一份文本 ——
        # 否则「模型看到的依据」和「校验层认定的依据」就不是一回事了。
        resp = {"answer": raw,
                "metadata": {"retriever_resources": [
                    {"document_name": c["law"],
                     "content": f"《{c['law']}》{c['art']}　{c['text']}"}
                    for c in hits]}}
        rec = askmod.analyze(question, resp)
        rec["retrieved"] = [{"law": c["law"], "art": c["art"]} for c in hits]
        rec["context"] = context
        rec["model"] = f'{self.pv["provider"]}/{self.pv["model"]}'
        rec["seconds"] = round(elapsed, 1)
        return rec


# ══════════════════════════════════════════════════════════════════════════
# 四、HTTP 服务
# ══════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    engine: Engine = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 静音默认访问日志，只留错误
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("[http] %s\n" % (fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ── 全文库：跨全库检索 ──────────────────────────────────────────────
    def _kb(self, path: str) -> None:
        """GET /api/kb?q=城市更新 补偿&top=10

        走 `data/kb.db`（19,000 份国家级全文的 FTS5 索引），和上面的「政策清单」
        是两件事：
          · 清单回答「这个方向有哪些文件」（目录层，字段级，可穷举）
          · 这里回答「哪份文件的哪一条说了这件事」（全文层，条款级，可溯源）
        """
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(400, {"error": "缺少 q"})
        try:
            top = max(1, min(50, int((qs.get("top") or ["10"])[0])))
        except ValueError:
            top = 10
        try:
            from harvest import build_index as bi
            hits = bi.query(q, top)
        except Exception as e:  # noqa: BLE001
            return self._json(502, {"error": f"{type(e).__name__}: {e}"})
        return self._json(200, {"query": q, "n": len(hits), "hits": hits})

    # ── 目录层：政策清单 ────────────────────────────────────────────────
    def _catalog(self, path: str) -> None:
        """GET /api/catalog?region=鹰潭&topic=城市更新[&keyword=][&refresh=1]
           GET /api/catalog/sources

        冷启动（缓存失效或带 refresh=1）会真的去抓，最坏十几秒；其余时候读缓存。
        返回里一定带 `coverage`——哪一级抓到了、哪一级没抓到、为什么没抓到。
        """
        from catalog import adapters
        from catalog import crawl as cat

        qs = parse_qs(urlparse(self.path).query)
        one = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731

        if path.rstrip("/") == "/api/catalog/sources":
            cfg = adapters.load_config()
            st = cat.Store()
            try:
                states = {s["source"]: s for s in st.source_states()}
                total = st.count()
            finally:
                st.close()
            out = []
            for sc in cfg.get("sources", []):
                s = states.get(sc.get("name"), {})
                out.append({"name": sc.get("name"), "type": sc.get("type"),
                            "label": sc.get("label"), "region": sc.get("region"),
                            "status": s.get("status") or "未运行",
                            "count": s.get("count") or 0,
                            "reason": s.get("reason") or ""})
            return self._json(200, {
                "sources": out, "total_items": total,
                "regions": list((cfg.get("regions") or {}).keys())})

        region, topic, keyword = one("region"), one("topic"), one("keyword")
        if not (region or topic or keyword):
            return self._json(400, {"error": "至少给 region / topic / keyword 之一"})
        refresh = one("refresh") in ("1", "true", "yes")
        try:
            res = None if refresh else cat.load_cached(region, topic, keyword)
            if res is None:
                res = cat.run(region, topic, keyword, refresh=True)
        except Exception as e:  # noqa: BLE001
            return self._json(502, {"error": f"{type(e).__name__}: {e}"})
        return self._json(200, res)

    # ── 地方目录层：省级 / 市级 / 县级文件清单 ──────────────────────────
    def _local(self, path: str) -> None:
        """GET /api/local?q=城市更新&level=市级&region=鹰潭&limit=30

        查的是 `catalog.db` 的 items 表（省 / 市 / 县三级目录，几万条），
        跟 `/api/kb` 的全文块索引是两套东西：那个回答「原文怎么写的」，
        这个回答「有哪些文件存在」。

        匹配用 2-gram 拆开 OR，不用整串 LIKE——「城市更新 补偿」整串去
        LIKE 一条也匹配不上，拆成「城市/市更/更新/补偿」才有召回。
        """
        # 用 self.path 而不是入参 path：do_GET 传进来的是已经剥掉查询串的
        # 纯路径，拿它 parse_qs 永远是空字典。
        qs = parse_qs(urlparse(self.path).query)
        one = lambda k, d="": (qs.get(k) or [d])[0]      # noqa: E731
        q = one("q").strip()
        level = one("level").strip()
        region = one("region").strip()
        try:
            limit = max(1, min(int(one("limit", "30")), 200))
        except ValueError:
            limit = 30
        if not (q or level or region):
            return self._json(400, {"error": "至少给 q / level / region 之一"})

        where, args = ["title <> ''"], []
        grams: list[str] = []
        if q:
            grams = [g for g in _grams(q) if len(g) >= 2][:12] or [q]
            where.append("(" + " OR ".join("title LIKE ?" for _ in grams) + ")")
            args += [f"%{g}%" for g in grams]
        if level:
            where.append("admin_level = ?")
            args.append(level)
        if region:
            where.append("region LIKE ?")
            args.append(f"%{region}%")
        base_where = " AND ".join(where)

        # 排序要分两层，不能只按日期。
        # 2-gram OR 的召回很宽：查「城市更新」会把「城市园林绿化管理办法」也捞进来
        # （因为「城市」命中），而那跟城市更新毫无关系。所以先用 SQL 把
        # 「完整串命中」的排在前面，取一批候选，再在 Python 里按打分精排。
        order = "COALESCE(published,'') DESC"
        order_args: list = []
        if q:
            order = ("(CASE WHEN title LIKE ? THEN 0 ELSE 1 END), "
                     "COALESCE(published,'') DESC")
            order_args = [f"%{q}%"]

        sql = ("SELECT title,issuer,doc_no,published,region,admin_level,"
               "source_name,url FROM items WHERE " + base_where +
               " ORDER BY " + order + " LIMIT ?")
        try:
            conn = sqlite3.connect(f"file:{CAT_DB}?mode=ro", uri=True, timeout=20)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, (*args, *order_args,
                                      min(400, limit * 8))).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM items WHERE " + base_where,
                                 args).fetchone()[0]
            facets = dict(conn.execute(
                "SELECT admin_level,COUNT(*) FROM items WHERE " + base_where +
                " GROUP BY admin_level", args).fetchall())
            conn.close()
        except sqlite3.Error as e:
            return self._json(503, {"error": f"目录库不可用：{e}"})

        if q and rows:
            # 打分：整串命中 > 各 gram 全命中 > 命中 gram 数 > 命中密度。
            # 密度那项是为了压住「标题很长、只蹭到一个词」的条目。
            def _score(t: str) -> float:
                s = 0.0
                if q in t:
                    s += 100
                hit = sum(1 for g in grams if g in t)
                s += hit * 6
                if grams and hit == len(grams):
                    s += 40
                s -= len(t) / 200.0
                return s

            # 两段排序：先按日期降序，再按分数稳定重排。
            # sort 是稳定的，所以同分的条目仍然保持「新的在前」。
            rows = sorted(rows, key=lambda r: r["published"] or "", reverse=True)
            rows = sorted(rows, key=lambda r: -_score(r["title"]))
            rows = rows[:limit]

        return self._json(200, {
            "query": {"q": q, "level": level, "region": region},
            "total": total,
            "facets": facets,
            "items": [{"title": r["title"], "issuer": r["issuer"],
                       "doc_no": r["doc_no"], "published": r["published"],
                       "region": r["region"], "level": r["admin_level"],
                       "source": r["source_name"], "url": r["url"]}
                      for r in rows],
        })

    def do_GET(self):
        # 必须用 urlparse 取 path 再比对：`self.path` 是含查询串的原始值，
        # 直接拿它和 "/" 比，访问 `/?q=…` 会落到 404（截图验证时踩到的）。
        path = urlparse(self.path).path
        if path.startswith("/api/health"):
            cat_n = 0
            try:
                from catalog.store import Store
                st = Store()
                cat_n = st.count()
                st.close()
            except Exception:  # noqa: BLE001  目录库缺失不影响主服务
                pass
            return self._json(200, {"ok": True, "name": "有据",
                                    "model": f'{self.engine.pv["provider"]}/'
                                             f'{self.engine.pv["model"]}',
                                    "chunks": len(self.engine.chunks),
                                    "laws": len({c["law"] for c in self.engine.chunks}),
                                    "catalog_items": cat_n,
                                    "kb": _kb_stat()})
        if path.startswith("/api/kb"):
            return self._kb(path)
        if path.startswith("/api/local"):
            return self._local(path)
        if path.startswith("/api/catalog"):
            return self._catalog(path)
        if path in ("/", "/index.html"):
            if not PAGE_FILE.exists():
                return self._send(500, b"index.html missing", "text/plain; charset=utf-8")
            return self._send(200, PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not urlparse(self.path).path.startswith("/api/ask"):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": f"请求体不是合法 JSON：{e}"})
        q = (payload.get("question") or "").strip()
        if not q:
            return self._json(400, {"error": "question 不能为空"})
        if len(q) > 500:
            return self._json(400, {"error": "question 过长（上限 500 字）"})
        try:
            rec = self.engine.answer(q)
        except Exception as e:  # noqa: BLE001
            return self._json(502, {"error": str(e)})
        return self._json(200, rec)


# ══════════════════════════════════════════════════════════════════════════
# 五、自检：用评估集量一次检索召回
# ══════════════════════════════════════════════════════════════════════════
#
# 检索层是 MVP 唯一退化的部件，所以它必须有一个可复现的数字，而不是「感觉还行」。
#
# 判据取自 eval/golden-set.csv 的「判分规则」列：那一列里凡是 『』 包起来的，
# 就是答案必须包含的关键词/数值（如 『生态环境主管部门』『二万元』『责令停工整治』）。
#
# 度量的口径是「**含答案的那一条，有没有进 top-k**」，而不是「判据词有没有整串出现」。
# 这个区别是被三条假阴性打出来的教训：判据词写的是『要求改正』『张贴在显著位置』，
# 而语料原文写的是「应当要求**予以**改正」「公告应当张贴在**拟拆除违法建筑物…设施的**
# 显著位置」—— 措辞对不上整串，但内容明明检索到了。用整串匹配会把两条完全正确的
# 检索判成失败，进而误导调参方向（第一次就差点去改 BM25 参数）。
# 改成「半数以上判据词落在同一条里」来定位 gold 条款，再问它有没有进候选，就稳了。
#
# 只统计 L1/L2：这两层的正确答案就在语料里。L3 是时效判断、L4 是对抗题，
# 它们的目标本来就不是「检索到能支持结论的条文」。

CRIT_RE = re.compile(r"『([^』]{1,30})』")
PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


def _kb_stat() -> dict:
    """全文库的规模。health 里报出来，界面上一眼能看到「库里有多少」。"""
    import sqlite3
    p = ROOT / "data" / "kb.db"
    if not p.exists():
        return {"docs": 0, "blocks": 0}
    try:
        c = sqlite3.connect(p)
        docs = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        blk = c.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        c.close()
        return {"docs": docs, "blocks": blk}
    except Exception:  # noqa: BLE001
        return {"docs": 0, "blocks": 0}


def crit_words(rule: str) -> list[str]:
    out = []
    for w in CRIT_RE.findall(rule or ""):
        w = PAREN_RE.sub("", w).strip().strip("、，。 ")
        if len(w) >= 2:
            out.append(w)
    return out


def probe(topk: int = 8) -> None:
    import csv
    rows = list(csv.DictReader((ROOT / "eval" / "golden-set.csv").open(encoding="utf-8-sig")))

    chunks = load_chunks()
    rt = Retriever(chunks)
    idx_of = {id(c): i for i, c in enumerate(chunks)}

    covered = {c["law"] for c in chunks}
    print(f"语料：{len(chunks)} 条款 ｜ {len(covered)} 部法规")
    empty = [full for full, _ in lk.law_pairs() if full not in covered]
    if empty:
        print(f"  ⚠ 落库但没有切出任何条款：{'、'.join(empty)}")
    print(f"评估集：{len(rows)} 题 ｜ 检索 top-{topk}\n")

    stat: dict[str, list[bool]] = {}
    detail: list[str] = []
    for r in rows:
        if r["分层"] not in ("L1基础", "L2推理"):
            continue
        ws = crit_words(r.get("判分规则") or "")
        if not ws:
            continue
        need = max(1, len(ws) // 2)
        gold = {i for i, c in enumerate(chunks)
                if sum(w in c["text"] for w in ws) >= need}
        hits = rt.search(r["输入"], k=topk)
        hit_idx = {idx_of[id(c)] for c in hits if id(c) in idx_of}
        ok = bool(gold & hit_idx)
        stat.setdefault(r["分层"], []).append(ok)
        if not ok:
            got = "、".join(f"《{c['law']}》{c['art']}" for c in hits[:3])
            detail.append(f'  {r["id"]:6s} 含答案的条款（{len(gold)} 条）未进候选'
                          f'　实得前三 {got or "（空）"}')

    print("分层      题数   含答案条款进候选")
    ta = tn = 0
    for layer in ("L1基础", "L2推理"):
        v = stat.get(layer)
        if not v:
            continue
        a = sum(v)
        ta += a
        tn += len(v)
        print(f"{layer:9s} {len(v):4d}   {a}/{len(v)}")
    if tn:
        print(f"{'合计':9s} {tn:4d}   {ta}/{tn} ({ta / tn:.0%})")

    if detail:
        print(f"\n未命中 {len(detail)} 条（这一步失败 = 模型再强也答不对）：")
        print("\n".join(detail))
    else:
        print("\n全部命中：L1/L2 每道题的答案条文都进了候选。")

    # 顺带跑一遍闸门 2 的规则自检，确认校验层没被改坏
    import contextlib
    import io
    import gatekeeper as gk
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gk.selftest()
    tail = [x for x in buf.getvalue().splitlines() if x.strip()]
    print(f"\n闸门 2 自检：{tail[-1] if tail else rc}")
    if rc != 0:
        print(buf.getvalue())


# ══════════════════════════════════════════════════════════════════════════
# 六、命令行入口
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    def opt(flag, default=None):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

    if "--probe" in sys.argv:
        return probe(topk=int(opt("--topk", 8)))

    model = opt("--model")
    topk = int(opt("--topk", 8))

    if "--ask" in sys.argv:
        q = opt("--ask")
        engine = Engine(model=model, topk=topk)
        rec = engine.answer(q)
        askmod.render(rec)
        return

    port = int(opt("--port", 8765))
    engine = Engine(model=model, topk=topk)
    Handler.engine = engine
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"政策核验 MVP 已启动 →  http://127.0.0.1:{port}")
    print(f"  模型  {engine.pv['provider']}/{engine.pv['model']}")
    print(f"  语料  {len(engine.chunks)} 条款 / {len({c['law'] for c in engine.chunks})} 部法规")
    print(f"  校验  闸门 2（scripts/gatekeeper.py，不调模型）")
    print("  Ctrl-C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
