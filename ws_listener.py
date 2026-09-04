"""cozyvtt-mcp — WebSocket 监听层（socket.io）。

- 握手带 session cookie；连接后 emit 'authenticate' {campaignId} 加入战役房间
- 监听事件写入内存环形缓冲（容量 500）：{seq, ts, event, payload}，seq 单调递增
- 断线自动重连（socket.io 自带），重连成功后记一条 system.reconnected
- 线程安全：缓冲区读写加锁
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("cozyvtt.ws")

# v1 监听的游戏事件
LISTEN_EVENTS = [
    "chat.message",
    "dice.rolled",
    "dice.rolled.secret",
    "map.changed",
    "initiative.state",
    "session.started",
    "session.paused",
    "session.ended",
    "character.hp.updated",
]


class WSListener:
    def __init__(self, base_url: str, campaign_id: str, cookie_getter,
                 capacity: int = 500, sio_factory=None):
        """
        cookie_getter: callable -> Cookie 头字符串（每次连接时取，保证用最新 cookie）
        sio_factory: 可注入 fake（测试用）；默认 python-socketio Client
        """
        self.base_url = base_url.rstrip("/")
        self.campaign_id = campaign_id
        self._cookie_getter = cookie_getter
        self._buf: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._ever_connected = False
        self.authenticated = False
        self.last_error: str | None = None

        if sio_factory is None:
            import socketio
            sio_factory = lambda: socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self.sio = sio_factory()
        self._register_handlers()

    # ---- 事件注册 ----

    def _register_handlers(self) -> None:
        sio = self.sio

        def on_connect():
            if self._ever_connected:
                self._append("system.reconnected", {"ts": time.time()})
            self._ever_connected = True
            self.authenticated = False

        def on_server_connected(data=None):
            # 服务端连接回调顺序：先 emit 'connected'，后注册 'authenticate' 监听。
            # 必须在收到 'connected' 后再发 authenticate，否则包会落在监听注册之前被静默丢弃
            # （2026-09-04 实测：浏览器端就是等 'connected' 再发的）。
            log.info("WS 收到服务端 connected，发送 authenticate")
            sio.emit("authenticate", {"campaignId": self.campaign_id})

        def on_authenticated(data=None):
            self.authenticated = True
            log.info("WS 战役认证成功 role=%s", (data or {}).get("role"))

        def on_disconnect():
            self.authenticated = False
            log.info("WS 断开")

        def on_error(data=None):
            self.last_error = str(data)
            log.warning("WS error 事件: %s", data)

        sio.on("connect", on_connect)
        sio.on("connected", on_server_connected)
        sio.on("authenticated", on_authenticated)
        sio.on("disconnect", on_disconnect)
        sio.on("error", on_error)

        for event in LISTEN_EVENTS:
            # 闭包绑定事件名
            sio.on(event, self._make_handler(event))

    def _make_handler(self, event: str):
        def handler(data=None):
            self._append(event, data)
        return handler

    # ---- 缓冲 ----

    def _append(self, event: str, payload) -> dict:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": time.time(), "event": event, "payload": payload}
            self._buf.append(rec)
            return rec

    def poll(self, since: int = 0, limit: int = 100) -> dict:
        """拉取 seq > since 的事件。返回 {events, latest_seq}。"""
        with self._lock:
            events = [r for r in self._buf if r["seq"] > since]
            if limit and len(events) > limit:
                events = events[-limit:]
            return {"events": events, "latest_seq": self._seq}

    def latest(self, event: str):
        """缓冲中最近一条指定事件，无则 None。"""
        with self._lock:
            for r in reversed(self._buf):
                if r["event"] == event:
                    return r
            return None

    # ---- 生命周期 ----

    def start(self) -> None:
        headers = {"Cookie": self._cookie_getter()}
        log.info("WS 连接 %s ...", self.base_url)
        self.sio.connect(self.base_url, headers=headers, wait_timeout=15)

    def emit(self, event: str, payload: dict, wait_auth: float = 10.0) -> None:
        if not self.connected:
            raise RuntimeError("WS 未连接")
        # 游戏事件要求战役认证完成；连接刚建立时等 authenticate 握手结束再发
        deadline = time.time() + wait_auth
        while not self.authenticated and time.time() < deadline:
            time.sleep(0.1)
        if not self.authenticated:
            raise RuntimeError(f"WS 战役认证超时（{wait_auth}s），拒绝发送 {event}")
        self.sio.emit(event, payload)

    @property
    def connected(self) -> bool:
        try:
            return bool(self.sio.connected)
        except Exception:
            return False

    def stop(self) -> None:
        try:
            self.sio.disconnect()
        except Exception:
            pass
