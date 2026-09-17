"""cozyvtt-mcp — REST 封装。

- 401：任一请求收到 401 → 重新登录一次并重试（只重试一次）
- 429：指数退避 1s/2s/4s，最多 3 次，仍失败抛 ApiError
- 所有错误以 ApiError(status, message) 抛出，message 含上游 message 字段
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("cozyvtt.client")


class ApiError(Exception):
    """上游 HTTP 错误。status=0 表示网络层错误。"""

    def __init__(self, status: int, message: str, details=None):
        self.status = status
        self.message = message
        self.details = details
        super().__init__(f"HTTP {status}: {message}" if status else message)


class CozyClient:
    """薄 REST 封装。auth 需提供线程安全的 .request() 与 .relogin()。"""

    def __init__(self, base_url: str, auth, timeout: float = 15.0,
                 backoff=(1.0, 2.0, 4.0), sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.backoff = tuple(backoff)
        self._sleep = sleep
        self.on_unauthorized = None

    # ---- 内部 ----

    def _extract_message(self, resp) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict):
                return str(data.get("message") or data.get("error") or resp.text[:200])
        except (ValueError, json.JSONDecodeError):
            pass
        return (resp.text or "")[:200] or f"status {resp.status_code}"

    def request(self, method: str, path: str, retry_401: bool = True,
                raw: bool = False, **kwargs):
        """处理 401/429；raw 按 Content-Type 返回文本或 bytes，保留 ETag/304。"""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        attempt_429 = 0
        retried_401 = False
        # requests 编码 multipart 会读完文件；每次重试必须从原位置重新读。
        streams = []
        files = kwargs.get("files") or {}
        for part in (files.values() if isinstance(files, dict) else (v for _, v in files)):
            stream = part[1] if isinstance(part, tuple) else part
            if hasattr(stream, "read"):
                streams.append((stream, stream.tell()))
        while True:
            try:
                for stream, position in streams:
                    stream.seek(position)
                resp = self.auth.request(method, url, **kwargs)
            except Exception as e:  # 网络层错误
                raise ApiError(0, f"{method} {path} 网络错误: {e}") from e

            if resp.status_code == 401 and self.on_unauthorized:
                self.on_unauthorized()
            if resp.status_code == 401 and retry_401 and not retried_401:
                retried_401 = True
                log.info("收到 401，尝试重新登录后重试 %s %s", method, path)
                try:
                    self.auth.relogin()
                except Exception as e:
                    raise ApiError(401, f"重新登录失败: {e}") from e
                continue

            if resp.status_code == 429 and attempt_429 < len(self.backoff):
                delay = self.backoff[attempt_429]
                attempt_429 += 1
                log.warning("收到 429，第 %d 次退避 %.1fs %s %s", attempt_429, delay, method, path)
                self._sleep(delay)
                continue

            if resp.status_code >= 400:
                try:
                    details = resp.json()
                except ValueError:
                    details = None
                raise ApiError(resp.status_code, self._extract_message(resp), details)

            if raw:
                if resp.status_code == 304:
                    return {"not_modified": True}
                mime = resp.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
                content = resp.content
                if mime.startswith("text/"):
                    content = content.decode(requests_encoding(resp), errors="strict")
                return {"mime_type": mime, "etag": resp.headers.get("ETag"), "content": content}

            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"_raw": resp.text}

    # ---- 便捷方法 ----

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, payload=None, **kw):
        return self.request("POST", path, json=payload or {}, **kw)

    def put(self, path, payload=None, **kw):
        return self.request("PUT", path, json=payload or {}, **kw)

    def patch(self, path, payload=None, **kw):
        return self.request("PATCH", path, json=payload or {}, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def post_multipart(self, path, fields, files, **kw):
        """交给 requests 生成 boundary；不要手工设置 Content-Type 或传 json。"""
        return self.request("POST", path, data=fields, files=files, **kw)

    def get_raw(self, path, **kw):
        return self.request("GET", path, raw=True, **kw)


def requests_encoding(resp):
    # requests 对无 charset 的 text/* 猜 ISO-8859-1；上游文档是 UTF-8。
    if "charset=" in resp.headers.get("Content-Type", "").lower():
        return resp.encoding or "utf-8"
    return "utf-8"
