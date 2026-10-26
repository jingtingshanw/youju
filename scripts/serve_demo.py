#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""演示页的本地服务 —— 一个文件、只用标准库，没有框架。

它在演示链路里的位置：

    浏览器（demo/index.html）
        │  POST /api/ask  {"question": "..."}
        ▼
    本服务
        ├─► Dify 应用（检索 + 生成）        ← 这是唯一需要模型的地方
        └─► 闸门 2 gatekeeper.verify()       ← 纯规则，不调模型
        │
        ▼  返回结构化 JSON（结论 / 依据 / 时效 / 清单 / 四项校验）
    页面渲染

**为什么不把 API key 放进页面**：key 一旦进了浏览器就等于公开。所以页面只是展示层，
凡是要拿 key 的事都放在本服务里做。

**为什么不用 Web 框架**：这一层要能在一个没装任何依赖的机器上跑起来。
`http.server` 足够，代码量也不大。

用法：
    python scripts/serve_demo.py                  # 默认 http://127.0.0.1:8765
    python scripts/serve_demo.py --port 9000
    python scripts/serve_demo.py --app app-v6.json
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask as askmod  # noqa: E402
import legalkit as lk  # noqa: E402

PAGE = lk.ROOT / "demo" / "index.html"
APP = None          # main() 里填；None 表示"每次调用自己找"
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "PolicyVerify/1.0"

    # ── 工具 ────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ── 路由 ────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not PAGE.exists():
                self._send(500, "demo/index.html 不存在".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json(200, {"ok": True, "page": PAGE.name, "laws": len(lk.law_pairs())})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/api/ask":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            self._json(400, {"error": "请求体为空或过大"})
            return
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"error": f"请求体不是合法 JSON：{e}"})
            return

        question = (payload.get("question") or "").strip()
        if not question:
            self._json(400, {"error": "question 不能为空"})
            return

        try:
            info = APP or askmod.load_app(None)
        except SystemExit as e:
            self._json(500, {"error": str(e)})
            return

        resp = askmod.call_app(info, question)
        if resp.get("_error"):
            print(f"[x] {question[:40]} → {resp['_error']}")
            self._json(502, {"error": resp["_error"]})
            return

        rec = askmod.analyze(question, resp)
        # 页面不需要原始召回段落全文，去掉能显著减小响应体
        rec.pop("resources", None)
        print(f"[ok] {question[:44]}  →  {rec['boundary']}（{rec['decided_by']}）")
        self._json(200, rec)

    def log_message(self, fmt, *args):  # 默认日志噪音太大，只留我们自己打的
        if "api/ask" not in (self.path or ""):
            return


def main() -> None:
    global APP
    if "--app" in sys.argv:
        APP = askmod.load_app(sys.argv[sys.argv.index("--app") + 1])
        print(f"使用应用凭据：{sys.argv[sys.argv.index('--app') + 1]}")

    port = 8765
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print("=" * 72)
    print(f"演示页已启动：{url}")
    print(f"  页面    {PAGE}")
    print(f"  接口    POST {url}api/ask")
    print("  停止    Ctrl-C")
    print("=" * 72)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
