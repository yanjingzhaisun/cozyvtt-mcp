"""Socket.IO listener: one reconnect worker, campaign authentication, and an event buffer with gap detection."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from copy import deepcopy

log = logging.getLogger("cozyvtt.ws")

LISTEN_EVENTS = [
    "chat.message", "dice.rolled", "dice.rolled.secret", "map.changed",
    "token.moved", "initiative.state", "session.started", "session.paused",
    "session.ended", "session.resumed", "character.hp.updated",
    "character.updated", "campaign.dm.transferred", "roster.updated", "dice.historyCleared",
]


class WSListener:
    def __init__(self, base_url: str, campaign_id: str, cookie_getter,
                 capacity: int = 500, sio_factory=None, auth_refresh=None,
                 reconnect_backoff=(1.0, 2.0, 4.0, 15.0), auth_timeout=10.0):
        if capacity < 1:
            raise ValueError("capacity must be greater than 0")
        self.base_url = base_url.rstrip("/")
        self.campaign_id = campaign_id
        self._cookie_getter = cookie_getter
        self._auth_refresh = auth_refresh
        self._backoff = tuple(reconnect_backoff) or (1.0,)
        self._auth_timeout = auth_timeout
        self._buf: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        # State transitions, sends, and callbacks share an RLock; generation isolates old callbacks.
        self._state = threading.Condition(threading.RLock())
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._generation = 0
        self._ever_authenticated = False
        self.role = None
        self.user_id = None
        self.on_context_invalidated = None
        self.authenticated = False
        self.last_error: str | None = None
        self._refresh_needed = False
        self._namespace_ready = self._server_ready = self._auth_sent = False
        self._failed = False
        self._initial_done = False
        self._auth_error: str | None = None
        if sio_factory is None:
            import socketio
            # Only this worker reconnects, avoiding races with tool calls and library reconnect tasks.
            sio_factory = lambda: socketio.Client(
                reconnection=False, logger=False, engineio_logger=False, request_timeout=5)
        self._sio_factory = sio_factory
        self.sio = self._sio_factory()
        self._register_handlers(self.sio, self._generation)

    def _register_handlers(self, sio, generation) -> None:
        def guarded(fn):
            def handler(*args):
                with self._state:
                    if generation != self._generation or self._stop.is_set():
                        return
                    return fn(*args)
            return handler

        def authenticate_if_ready():
            if self._namespace_ready and self._server_ready and not self._auth_sent:
                self._auth_sent = True
                sio.emit("authenticate", {"campaignId": self.campaign_id})

        def on_connect():
            # Do not clear authentication here; another receiver may have handled connected/authenticated first.
            self._namespace_ready = True
            authenticate_if_ready()

        def on_server_connected(data=None):
            self._server_ready = True
            authenticate_if_ready()

        def on_authenticated(data=None):
            if (self.authenticated or self._failed or not self._auth_sent or not isinstance(data, dict)
                    or data.get("campaignId") != self.campaign_id):
                return
            self.authenticated = True
            self.role = data.get("role")
            self.user_id = data.get("userId")
            self.last_error = self._auth_error = None
            self._refresh_needed = False
            if self._ever_authenticated:
                self._append("system.reconnected", {"generation": generation})
            self._ever_authenticated = True
            self._state.notify_all()
            sio.emit("initiative.request_state", {})

        def on_disconnect(reason=None):
            self.authenticated = False
            self.role = None
            if self.on_context_invalidated:
                self.on_context_invalidated()
            self._failed = True
            self._append("system.disconnected", {"reason": str(reason)})
            self._state.notify_all()

        def on_error(data=None):
            self.last_error = str(data)
            self._append("system.error", {"detail": data, "generation": generation})
            invalid = any(s in self.last_error.lower() for s in
                          ("unauthorized", "no longer a member", "not a member"))
            if not self.authenticated or invalid:
                self.authenticated = False
                self.role = None
                if self.on_context_invalidated:
                    self.on_context_invalidated()
                self._auth_error = self.last_error
                self._failed = True
                self._refresh_needed = "unauthorized" in self.last_error.lower()
            self._state.notify_all()

        for name, fn in (("connect", on_connect), ("connected", on_server_connected),
                         ("authenticated", on_authenticated), ("disconnect", on_disconnect),
                         ("error", on_error), ("connect_error", on_error)):
            sio.on(name, guarded(fn))
        for event in LISTEN_EVENTS:
            def handler(data=None, event=event):
                self._append(event, data)
                if event == "campaign.dm.transferred":
                    if isinstance(data, dict) and self.user_id:
                        if data.get("newDmId") == self.user_id:
                            self.role = "DM"
                        elif data.get("previousDmId") == self.user_id:
                            self.role = "PLAYER"
                    if self.on_context_invalidated:
                        self.on_context_invalidated()
                self._state.notify_all()
            sio.on(event, guarded(handler))

    def _append(self, event: str, payload) -> dict:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": time.time(), "event": event,
                   "payload": deepcopy(payload), "generation": self._generation}
            self._buf.append(rec)
            return rec

    def poll(self, since: int = 0, limit: int = 100) -> dict:
        if since < 0 or not 1 <= limit <= 500:
            raise ValueError("since must be >= 0 and limit must be in 1..500")
        with self._lock:
            oldest = self._buf[0]["seq"] if self._buf else self._seq + 1
            cursor_reset = since > self._seq
            effective_since = 0 if cursor_reset else since
            events = [r for r in self._buf if r["seq"] > effective_since][:limit]
            next_seq = events[-1]["seq"] if events else effective_since
            result = {"events": deepcopy(events), "next_seq": next_seq,
                      # Keep compatibility with callers that use latest_seq as the read cursor.
                      "latest_seq": next_seq, "high_water_seq": self._seq,
                      "oldest_seq": oldest, "gap": effective_since < oldest - 1,
                      "cursor_reset": cursor_reset,
                      "has_more": next_seq < self._seq}
        with self._state:
            result.update(connected=self.connected, authenticated=self.authenticated,
                          last_error=self.last_error, role=self.role,
                          stale=not self.authenticated)
        return result

    def latest(self, event: str):
        with self._lock:
            for rec in reversed(self._buf):
                if rec["event"] == event:
                    if event == "initiative.state" and rec["generation"] != self._generation:
                        return None
                    return deepcopy(rec)
        return None

    def start(self, wait_timeout: float = 15.0) -> None:
        """Start the single connection worker; report initial authentication failures explicitly."""
        with self._lifecycle_lock:
            if not self._thread or not self._thread.is_alive():
                self._stop.clear()
                with self._state:
                    self._initial_done = False
                self._thread = threading.Thread(target=self._run, name="cozyvtt-ws", daemon=True)
                self._thread.start()
        deadline = time.monotonic() + wait_timeout
        with self._state:
            while not (self.authenticated and self.connected) and not self._initial_done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(remaining)
            if not (self.authenticated and self.connected):
                raise RuntimeError(f"WS is not authenticated; the background worker will retry: {self.last_error or 'connection timed out'}")

    def invalidate_auth(self, reason: str) -> None:
        with self._state:
            self.authenticated = False
            self.role = None
            self._failed = True
            self.last_error = self._auth_error = reason
            self._refresh_needed = True
            self._append("system.error", {"detail": reason, "phase": "rest-auth"})
            self._state.notify_all()

    def wait_for_event(self, event: str, since: int, timeout: float = 2.0):
        """Wait for a newer event on the current connection; for reads only, never a write ACK."""
        deadline = time.monotonic() + timeout
        with self._state:
            while self.authenticated and self.connected:
                rec = self.latest(event)
                if rec and rec["seq"] > since:
                    return rec
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(remaining)
        return None

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            sio = None
            try:
                if self._refresh_needed and self._auth_refresh:
                    self._auth_refresh()  # AuthManager enforces cooldown across calls, including failed attempts
                if self._stop.is_set():
                    break
                with self._state:
                    self._generation += 1
                    self.authenticated = False
                    self._namespace_ready = self._server_ready = self._auth_sent = False
                    self._failed = False
                    self._auth_error = None
                    sio = self._sio_factory()
                    self.sio = sio
                    self._register_handlers(sio, self._generation)
                # Use the latest Cookie for every connection, including updates from REST re-login.
                sio.connect(self.base_url, headers=lambda: {"Cookie": self._cookie_getter()},
                            wait_timeout=15)
                deadline = time.monotonic() + self._auth_timeout
                with self._state:
                    self._state.notify_all()
                    while not self.authenticated and not self._failed and not self._stop.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError("WS campaign authentication timed out")
                        self._state.wait(remaining)
                    if self._failed:
                        raise RuntimeError(self._auth_error or "WS connection closed")
                    if self.authenticated:
                        attempt = 0
                    while not self._failed and not self._stop.is_set():
                        self._state.wait()
            except Exception as exc:
                with self._state:
                    self.last_error = str(exc)
                    self._append("system.error", {"detail": str(exc), "phase": "connect"})
                log.warning("WS connection failed: %s", exc)
            finally:
                with self._state:
                    # Invalidate old callbacks before disconnecting so a late authenticated event cannot restore stale state.
                    self._generation += 1
                    self.authenticated = False
                    self._initial_done = True
                    self._state.notify_all()
                if sio:
                    try:
                        sio.disconnect()
                    except Exception:
                        log.debug("WS cleanup failed", exc_info=True)
            if self._stop.wait(self._backoff[min(attempt, len(self._backoff) - 1)]):
                break
            attempt += 1

    def emit(self, event: str, payload: dict, wait_auth: float = 10.0) -> dict:
        deadline = time.monotonic() + wait_auth
        with self._state:
            while not self.authenticated and not self._failed and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state.wait(remaining)
            if not self.connected or not self.authenticated:
                raise RuntimeError(f"WS campaign authentication is incomplete: {self._auth_error or self.last_error or 'not connected'}")
            with self._lock:
                since = self._seq
            self.sio.emit(event, payload)
            # TODO(#11): Upstream 1.2.2 has no requestId/business ACK, so asynchronous broadcasts or
            # errors cannot be reliably matched to a call. Return pending; expose errors via events_poll; never replay writes.
            return {"sent": True, "confirmed": False, "status": "pending", "since": since,
                    "note": "Dispatch only; poll for business results and system.error. Do not blindly retry writes."}

    @property
    def connected(self) -> bool:
        return bool(self.sio.connected)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            with self._state:
                self.authenticated = False
                self._state.notify_all()
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=20)
