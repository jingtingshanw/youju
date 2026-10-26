#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取用的 HTTP 小工具：超时、重试、限速、gzip。

只用标准库。走系统代理（`HTTPS_PROXY` 环境变量会被 urllib 自动识别），
所以宿主机需要代理时不用额外配置——这一点和项目里 `scripts/host_proxy.py`
是同一套前提。

限速是必要的：`sousuo.www.gov.cn` 对密集请求会返回空结果，看起来像「查无此项」，
其实是把自己封了。这类失败最难排查，所以在这里硬限速，不留给调用方随手决定。
"""
from __future__ import annotations

import gzip
import io
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 每个 host 的最小请求间隔（秒）。可以在源配置里用 `interval` 覆盖：
# 翻 20 多页时 0.8 秒会拖到十几秒，小站扛得住更密的请求。
MIN_INTERVAL = 0.8
_last: dict[str, float] = {}


class FetchError(RuntimeError):
    """网络层失败。调用方要把它变成 SourceReport 的 error 状态，不能吞掉。"""


def _throttle(host: str, gap: float = MIN_INTERVAL) -> None:
    wait = gap - (time.time() - _last.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def _ssl_ctx(insecure: bool):
    """返回 ssl 上下文。insecure=True 时放过证书问题与旧版重协商。"""
    import ssl
    try:
        ctx = ssl.create_default_context()
    except Exception:                                   # noqa: BLE001
        return None
    if not insecure:
        return ctx
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # OP_LEGACY_SERVER_CONNECT：允许不安全的旧版重协商（多数地方政务站需要）
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx


def _decode(raw: bytes, enc: str | None) -> str:
    for cand in (enc, "utf-8", "gb18030"):
        if not cand:
            continue
        try:
            return raw.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def get(url: str, params: dict | None = None, *,
        timeout: int = 20, retries: int = 2,
        referer: str = "", encoding: str | None = None,
        interval: float | None = None,
        headers: dict | None = None,
        insecure: bool = False) -> str:
    """GET 一个 URL，返回文本。失败抛 FetchError（带最后一次的原因）。

    `insecure=True` 时放过证书错误并允许旧版重协商。地方政府站这个问题很普遍：
    实测江西报 UNSAFE_LEGACY_RENEGOTIATION、安徽/海南/云南报 CERTIFICATE_
    VERIFY_FAILED、湖南报 BAD_ECPOINT——都是站点的证书配置老旧，不是我们抓错。
    这些页面全是公开的只读文件，不涉及任何凭据，所以对这些域放宽是合理的；
    对带账号的请求永远不要打开这个开关。
    """
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    host = urllib.parse.urlparse(url).netloc
    gap = MIN_INTERVAL if interval is None else max(0.0, interval)
    last_err = ""
    ctx = _ssl_ctx(insecure)
    for attempt in range(retries + 1):
        _throttle(host, gap)
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "text/html,application/json,*/*")
        req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
        req.add_header("Accept-Encoding", "gzip, deflate")
        if referer:
            req.add_header("Referer", referer)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read()
                enc_hdr = r.headers.get("Content-Encoding", "")
                if enc_hdr == "gzip":
                    raw = gzip.decompress(raw)
                elif enc_hdr == "deflate":
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return _decode(raw, r.headers.get_content_charset() or encoding)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                b = e.read()[:2000]
                # 错误响应也可能是 gzip。不先解压就直接 decode，日志里会出现
                # `HTTP 404 \x1f\x8b\x08...` 这种乱码，没法判断是真 404 还是
                # 被 CDN 拦了——2026-10-20 排查 308 份失败全被这个坑住。
                enc = (e.headers.get("Content-Encoding", "") if e.headers else "")
                if enc == "gzip" and b[:2] == b"\x1f\x8b":
                    b = gzip.decompress(b)
                elif enc == "deflate":
                    b = zlib.decompress(b, -zlib.MAX_WBITS)
                body = b[:200].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            last_err = f"HTTP {e.code} {body}".strip()
            if e.code in (403, 404, 410):   # 不重试，重试也没用
                break
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    raise FetchError(last_err or "unknown")


def get_json(url: str, params: dict | None = None, **kw):
    import json
    txt = get(url, params, **kw)
    try:
        return json.loads(txt)
    except ValueError as e:
        raise FetchError(f"返回不是 JSON（前 120 字：{txt[:120]!r}）：{e}") from e


__all__ = ["get", "get_json", "FetchError", "UA"]
