"""cozyvtt-mcp — authentication layer.

- On startup, POST /api/auth/login (JSON: email/password/rememberMe:true); store cookies in requests.Session.
- Keepalive: GET /api/auth/ping every 10 minutes in a worker (ordinary sessions expire after 1 hour idle).
- Space re-logins by min_relogin_interval (default 180s; auth limit: 5 requests/15min/IP).
- Login 429: back off 1/2/4s, at most three retries, then raise instead of retrying indefinitely.
"""
from __future__ import annotations

import logging
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

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
                 sleep=time.sleep, clock=time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.keepalive_interval = keepalive_interval
        self.min_relogin_interval = min_relogin_interval
        self.backoff = tuple(backoff)
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_login_ts = None
        self._next_login_ts = 0.0
        self._login_failed = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        self.user: dict | None = None

    # ---- Login ----

    def login(self) -> dict:
        """Log in with exponential backoff for 429; at most len(backoff)+1 attempts."""
        with self._lock:
            return self._login_locked()

    def _login_locked(self) -> dict:
        remaining = self._next_login_ts - self._clock()
        if remaining > 0:
            raise AuthError(f"Login cooldown; retry in {remaining:.1f}s")
        url = f"{self.base_url}/api/auth/login"
        payload = {"email": self.email, "password": self.password, "rememberMe": True}
        last_err = None
        for attempt in range(len(self.backoff) + 1):
            self._login_failed = True
            self._next_login_ts = self._clock() + self.min_relogin_interval
            try:
                resp = self.session.post(url, json=payload, timeout=15)
            except Exception as e:
                raise AuthError(f"Login request network error: {e}") from e
            if resp.status_code == 429:
                last_err = AuthError("Login rate limited (429)")
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(0.0, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            delay = 0.0
                    if delay > 0:
                        self._next_login_ts = max(self._next_login_ts, self._clock() + delay)
                        break  # Do not block the tool thread for a long server cooldown
                if attempt < len(self.backoff):
                    delay = self.backoff[attempt]
                    log.warning("Login 429; backing off %.1fs (retry %d)", delay, attempt + 1)
                    self._sleep(delay)
                    continue
                break
            if resp.status_code >= 400:
                try:
                    error_data = resp.json()
                    msg = error_data.get("message", resp.text[:200]) if isinstance(error_data, dict) else resp.text[:200]
                except ValueError:
                    msg = resp.text[:200]
                raise AuthError(f"Login failed HTTP {resp.status_code}: {msg}")
            try:
                data = resp.json() if resp.content else {}
            except ValueError as exc:
                raise AuthError("Login response is not valid JSON") from exc
            if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                raise AuthError("Login response is missing the user object")
            self._last_login_ts = self._clock()
            self._next_login_ts = self._last_login_ts + self.min_relogin_interval
            self._login_failed = False
            self.user = data.get("user")
            log.info("Login successful: %s", (self.user or {}).get("email", self.email))
            return data
        raise last_err or AuthError("Login failed: rate limited (429)")

    def relogin(self) -> bool:
        """Re-log in after a 401. Skip within min_relogin_interval of the last successful login
        to respect the 5 requests/15min limit. Return whether a login was performed."""
        with self._lock:
            if self._clock() < self._next_login_ts:
                if self._login_failed:
                    raise AuthError(f"Login cooldown; retry in {self._next_login_ts - self._clock():.1f}s")
                return False  # Another concurrent request just refreshed the session; allow the REST retry
            self._login_locked()
            return True

    def request(self, method: str, url: str, **kwargs):
        """Serialize requests and Cookie updates on the shared Session."""
        with self._lock:
            return self.session.request(method, url, **kwargs)

    # ---- Keepalive ----

    def ping(self) -> bool:
        try:
            resp = self.request("GET", f"{self.base_url}/api/auth/ping", timeout=10)
            if resp.status_code == 401:
                self.relogin()
            ok = resp.status_code == 200
            if not ok:
                log.warning("Keepalive ping returned %s", resp.status_code)
            return ok
        except Exception as e:
            log.warning("Keepalive ping failed: %s", e)
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
        if self._keepalive_thread and self._keepalive_thread is not threading.current_thread():
            self._keepalive_thread.join(timeout=16)
        with self._lock:
            self.session.close()

    # ---- WS handshake ----

    def cookie_header(self) -> str:
        """Apply Domain/Path/Secure/Expires rules for the default Socket.IO handshake URL."""
        parts = urlsplit(self.base_url)
        url = urlunsplit((parts.scheme, parts.netloc, "/socket.io/", "", ""))
        with self._lock:
            req = requests.Request("GET", url).prepare()
            return requests.cookies.get_cookie_header(self.session.cookies, req) or ""
