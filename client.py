"""cozyvtt-mcp — REST wrapper.

- 401: re-log in and retry the request once.
- 429: exponential backoff of 1s/2s/4s, at most three retries, then raise ApiError.
- Raise ApiError(status, message) for all errors, preserving the upstream message.
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("cozyvtt.client")


class ApiError(Exception):
    """Upstream HTTP error. status=0 indicates a network error."""

    def __init__(self, status: int, message: str, details=None):
        self.status = status
        self.message = message
        self.details = details
        super().__init__(f"HTTP {status}: {message}" if status else message)


class CozyClient:
    """Thin REST wrapper. auth must provide thread-safe .request() and .relogin()."""

    def __init__(self, base_url: str, auth, timeout: float = 15.0,
                 backoff=(1.0, 2.0, 4.0), sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.backoff = tuple(backoff)
        self._sleep = sleep
        self.on_unauthorized = None

    # ---- Internal helpers ----

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
        """Handle 401/429; raw returns text or bytes by Content-Type and preserves ETag/304."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)

        attempt_429 = 0
        retried_401 = False
        # requests consumes files when encoding multipart; rewind to the initial position on each retry.
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
            except Exception as e:  # Network error
                raise ApiError(0, f"{method} {path} network error: {e}") from e

            if resp.status_code == 401 and self.on_unauthorized:
                self.on_unauthorized()
            if resp.status_code == 401 and retry_401 and not retried_401:
                retried_401 = True
                log.info("Received 401; attempting re-login before retrying %s %s", method, path)
                try:
                    self.auth.relogin()
                except Exception as e:
                    raise ApiError(401, f"Re-login failed: {e}") from e
                continue

            if resp.status_code == 429 and attempt_429 < len(self.backoff):
                delay = self.backoff[attempt_429]
                attempt_429 += 1
                log.warning("Received 429; retry %d, backing off %.1fs %s %s", attempt_429, delay, method, path)
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

    # ---- Convenience methods ----

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
        """Let requests generate the boundary; do not set Content-Type manually or pass json."""
        return self.request("POST", path, data=fields, files=files, **kw)

    def get_raw(self, path, **kw):
        return self.request("GET", path, raw=True, **kw)


def requests_encoding(resp):
    # requests guesses ISO-8859-1 for text/* without charset; upstream documents use UTF-8.
    if "charset=" in resp.headers.get("Content-Type", "").lower():
        return resp.encoding or "utf-8"
    return "utf-8"
