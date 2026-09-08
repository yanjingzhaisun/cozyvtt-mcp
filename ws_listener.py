"""Socket.IO 监听：单一重连线程、战役认证状态、带缺口提示的事件缓冲。"""
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
]


class WSListener:
    def __init__(self, base_url: str, campaign_id: str, cookie_getter,
                 capacity: int = 500, sio_factory=None, auth_refresh=None,
                 reconnect_backoff=(1.0, 2.0, 4.0, 15.0), auth_timeout=10.0):
        if capacity < 1:
            raise ValueError("capacity 必须大于 0")
        self.base_url = base_url.rstrip("/")
        self.campaign_id = campaign_id
        self._cookie_getter = cookie_getter
        self._auth_refresh = auth_refresh
        self._backoff = tuple(reconnect_backoff) or (1.0,)
        self._auth_timeout = auth_timeout
        self._buf: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        # 状态转换、发送和回调共享 RLock；旧连接回调通过 generation 隔离。
        self._state = threading.Condition(threading.RLock())
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._generation = 0
        self._ever_authenticated = False
        self.authenticated = False
        self.last_error: str | None = None
        self._refresh_needed = False
        self._namespace_ready = self._server_ready = self._auth_sent = False
        self._failed = False
        self._initial_done = False
        self._auth_error: str | None = None
        if sio_factory is None:
            import socketio
            # 只有本类的线程负责重连，避免与工具调用或库内重连任务竞争。
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
            # 不在这里清除认证；connected/authenticated 可由其他收包线程先处理。
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
            self.last_error = self._auth_error = None
            self._refresh_needed = False
            if self._ever_authenticated:
                self._append("system.reconnected", {"generation": generation})
            self._ever_authenticated = True
            self._state.notify_all()
            sio.emit("initiative.request_state", {})

        def on_disconnect(reason=None):
            self.authenticated = False
            self._failed = True
            self._append("system.disconnected", {"reason": str(reason)})
            self._state.notify_all()

        def on_error(data=None):
            self.last_error = str(data)
            self._append("system.error", {"detail": data, "generation": generation})
            if not self.authenticated or "unauthorized" in self.last_error.lower():
                self.authenticated = False
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
            raise ValueError("since 必须 >= 0，limit 必须在 1..500")
        with self._lock:
            oldest = self._buf[0]["seq"] if self._buf else self._seq + 1
            cursor_reset = since > self._seq
            effective_since = 0 if cursor_reset else since
            events = [r for r in self._buf if r["seq"] > effective_since][:limit]
            next_seq = events[-1]["seq"] if events else effective_since
            result = {"events": deepcopy(events), "next_seq": next_seq,
                      # 兼容原有把 latest_seq 当读取游标的调用方。
                      "latest_seq": next_seq, "high_water_seq": self._seq,
                      "oldest_seq": oldest, "gap": effective_since < oldest - 1,
                      "cursor_reset": cursor_reset,
                      "has_more": next_seq < self._seq}
        with self._state:
            result.update(connected=self.connected, authenticated=self.authenticated,
                          last_error=self.last_error)
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
        """启动唯一的后台连接管理线程，首次认证失败返回明确错误。"""
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
                raise RuntimeError(f"WS 尚未认证，后台将重试: {self.last_error or '连接超时'}")

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            sio = None
            try:
                if self._refresh_needed and self._auth_refresh:
                    self._auth_refresh()  # AuthManager 在失败时也执行跨调用冷却
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
                # 每次连接取最新 Cookie，包括 REST 重登录后产生的新值。
                sio.connect(self.base_url, headers=lambda: {"Cookie": self._cookie_getter()},
                            wait_timeout=15)
                deadline = time.monotonic() + self._auth_timeout
                with self._state:
                    self._state.notify_all()
                    while not self.authenticated and not self._failed and not self._stop.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError("WS 战役认证超时")
                        self._state.wait(remaining)
                    if self._failed:
                        raise RuntimeError(self._auth_error or "WS 连接已断开")
                    if self.authenticated:
                        attempt = 0
                    while not self._failed and not self._stop.is_set():
                        self._state.wait()
            except Exception as exc:
                with self._state:
                    self.last_error = str(exc)
                    self._append("system.error", {"detail": str(exc), "phase": "connect"})
                log.warning("WS 连接失败: %s", exc)
            finally:
                with self._state:
                    # 先使旧回调失效，再关闭连接，避免晚到的 authenticated 恢复旧状态。
                    self._generation += 1
                    self.authenticated = False
                    self._initial_done = True
                    self._state.notify_all()
                if sio:
                    try:
                        sio.disconnect()
                    except Exception:
                        log.debug("WS 清理失败", exc_info=True)
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
                raise RuntimeError(f"WS 未完成战役认证: {self._auth_error or self.last_error or '未连接'}")
            with self._lock:
                since = self._seq
            self.sio.emit(event, payload)
            # TODO(#11): 上游 1.2.2 没有 requestId/业务 ACK，不能可靠关联异步广播或
            # error 与某次调用。返回 pending，错误经 events_poll 暴露；绝不自动重放写操作。
            return {"sent": True, "confirmed": False, "status": "pending", "since": since,
                    "note": "仅确认已发送；请轮询业务结果和 system.error，勿盲目重试写操作"}

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
