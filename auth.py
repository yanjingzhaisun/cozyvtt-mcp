"""cozyvtt-mcp — 认证层。

- 启动 POST /api/auth/login（json: email/password/rememberMe:true），cookie 存 requests.Session
- 保活：后台线程每 10 min GET /api/auth/ping（普通 session 空闲 1h 过期）
- 重登录间隔不小于 min_relogin_interval（默认 180s，auth 限流 5次/15min/IP）
- 登录遇 429：指数退避 1/2/4s 最多 3 次，仍失败抛错，不循环撞墙
"""
from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger("cozyvtt.auth")


class AuthError(Exception):
    pass


class AuthManager:
    def __init__(self, base_url: str, email: str, password: str,
                 keepalive_interval: float = 600.0,
                 min_relogin_interval: float = 180.0,
                 backoff=(1.0, 2.0, 4.0),
                 session: requests.Session | None = None,
                 sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.keepalive_interval = keepalive_interval
        self.min_relogin_interval = min_relogin_interval
        self.backoff = tuple(backoff)
        self.session = session or requests.Session()
        self._sleep = sleep
        self._last_login_ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self.user: dict | None = None

    # ---- 登录 ----

    def login(self) -> dict:
        """登录。429 指数退避，最多 len(backoff)+1 次尝试。"""
        with self._lock:
            return self._login_locked()

    def _login_locked(self) -> dict:
        url = f"{self.base_url}/api/auth/login"
        payload = {"email": self.email, "password": self.password, "rememberMe": True}
        last_err = None
        for attempt in range(len(self.backoff) + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=15)
            except Exception as e:
                raise AuthError(f"登录请求网络错误: {e}") from e
            if resp.status_code == 429:
                last_err = AuthError("登录被限流 (429)")
                if attempt < len(self.backoff):
                    delay = self.backoff[attempt]
                    log.warning("登录 429，退避 %.1fs（第 %d 次）", delay, attempt + 1)
                    self._sleep(delay)
                    continue
                break
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("message", resp.text[:200])
                except ValueError:
                    msg = resp.text[:200]
                raise AuthError(f"登录失败 HTTP {resp.status_code}: {msg}")
            data = resp.json() if resp.content else {}
            self._last_login_ts = time.time()
            self.user = data.get("user")
            log.info("登录成功: %s", (self.user or {}).get("email", self.email))
            return data
        raise last_err or AuthError("登录失败：429 限流")

    def relogin(self) -> bool:
        """401 触发的重登录。距上次成功登录不足 min_relogin_interval 时跳过
        （避免撞 5次/15min 限流），返回是否真正执行了登录。"""
        with self._lock:
            elapsed = time.time() - self._last_login_ts
            if self._last_login_ts > 0 and elapsed < self.min_relogin_interval:
                log.info("距上次登录 %.0fs < %.0fs，跳过重登录（复用现有 session）",
                         elapsed, self.min_relogin_interval)
                return False
            self._login_locked()
            return True

    # ---- 保活 ----

    def ping(self) -> bool:
        try:
            resp = self.session.get(f"{self.base_url}/api/auth/ping", timeout=10)
            ok = resp.status_code == 200
            if not ok:
                log.warning("keepalive ping 返回 %s", resp.status_code)
            return ok
        except Exception as e:
            log.warning("keepalive ping 异常: %s", e)
            return False

    def start_keepalive(self) -> None:
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="cozyvtt-keepalive", daemon=True)
        self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        while not self._stop.wait(self.keepalive_interval):
            self.ping()

    def stop(self) -> None:
        self._stop.set()

    # ---- WS 握手用 ----

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)
