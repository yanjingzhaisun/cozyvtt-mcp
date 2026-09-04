"""cozyvtt-mcp — 工具包。Ctx 共享上下文 + register_all。"""
from __future__ import annotations

import logging
import os
import threading
import time

from auth import AuthManager
from client import CozyClient
from ws_listener import WSListener

log = logging.getLogger("cozyvtt.tools")

DICE_MIN_INTERVAL = 2.1  # 骰子 WS 限流 30/min → 客户端最小间隔 2.1s


class Ctx:
    """工具共享上下文：REST client + auth + WS listener + 战役 id。"""

    def __init__(self, client: CozyClient, auth: AuthManager, ws: WSListener, campaign_id: str):
        self.client = client
        self.auth = auth
        self.ws = ws
        self.campaign_id = campaign_id
        self._ws_started = False
        self._ws_lock = threading.Lock()
        self._dice_lock = threading.Lock()
        self._last_dice_ts = 0.0

    @classmethod
    def from_env(cls) -> "Ctx":
        base_url = os.environ.get("COZYVTT_URL", "http://localhost:8899")
        email = os.environ.get("COZYVTT_EMAIL", "")
        password = os.environ.get("COZYVTT_PASSWORD", "")
        campaign_id = os.environ.get("COZYVTT_CAMPAIGN_ID", "")
        if not email or not password:
            raise RuntimeError("缺少 COZYVTT_EMAIL / COZYVTT_PASSWORD 环境变量")
        if not campaign_id:
            raise RuntimeError("缺少 COZYVTT_CAMPAIGN_ID 环境变量")
        auth = AuthManager(base_url, email, password)
        client = CozyClient(base_url, auth)
        ws = WSListener(base_url, campaign_id, auth.cookie_header)
        return cls(client, auth, ws, campaign_id)

    # ---- WS 懒启动 ----

    def ensure_ws(self) -> None:
        with self._ws_lock:
            if self._ws_started:
                return
            self.ws.start()
            self._ws_started = True

    # ---- 骰子节流（排队，不报错）----

    def dice_throttle(self) -> None:
        with self._dice_lock:
            wait = DICE_MIN_INTERVAL - (time.time() - self._last_dice_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_dice_ts = time.time()


def _ok(data=None):
    return {"ok": True, "data": data}


def _err(msg: str):
    return {"ok": False, "error": msg}


def wrap(fn):
    """统一异常 → {ok,error}，绝不抛出 MCP 层。
    functools.wraps 保留原签名（FastMCP 需要解析参数 schema）。"""
    import functools
    from client import ApiError

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return _ok(fn(*args, **kwargs))
        except ApiError as e:
            return _err(f"HTTP {e.status}: {e.message}" if e.status else e.message)
        except Exception as e:
            log.exception("工具调用异常: %s", fn.__name__)
            return _err(f"{type(e).__name__}: {e}")
    return inner


def register_all(mcp, get_ctx) -> None:
    from tools import read_tools, write_tools
    read_tools.register(mcp, get_ctx)
    write_tools.register(mcp, get_ctx)
