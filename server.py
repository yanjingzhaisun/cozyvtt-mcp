#!/usr/bin/env python3
"""CozyVTT MCP bridge (stdio)：线程安全懒初始化、可恢复失败和资源清理。"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP

from tools import Ctx, register_all

log = logging.getLogger("cozyvtt.server")
_ctx: Ctx | None = None
_ctx_error: str | None = None
_ctx_retry_at = 0.0
_ctx_lock = threading.Lock()
INIT_RETRY_INTERVAL = 180.0


@asynccontextmanager
async def lifespan(server):
    try:
        yield
    finally:
        global _ctx
        with _ctx_lock:
            ctx, _ctx = _ctx, None
        if ctx is not None:
            # 网络线程清理不阻塞 MCP 的事件循环。
            import asyncio
            await asyncio.to_thread(ctx.close)


mcp = FastMCP("cozyvtt", lifespan=lifespan)


def get_ctx() -> Ctx:
    """首次调用时登录；失败冷却后重新初始化，不永久缓存临时故障。"""
    global _ctx, _ctx_error, _ctx_retry_at
    with _ctx_lock:
        if _ctx is not None:
            return _ctx
        remaining = _ctx_retry_at - time.monotonic()
        if remaining > 0:
            raise RuntimeError(f"cozyvtt 初始化冷却 {remaining:.1f}s: {_ctx_error}")
        ctx = None
        try:
            ctx = Ctx.from_env()
            ctx.auth.login()
            try:
                camp = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}").get("campaign", {})
                log.info("自检：战役「%s」status=%s", camp.get("name"), camp.get("status"))
            except Exception as exc:
                log.warning("战役自检失败: %s", exc)
            ctx.auth.start_keepalive()
            # WS 真正按需启动；REST 读操作不依赖 WS 可用性。
            _ctx = ctx
            _ctx_error = None
            _ctx_retry_at = 0.0
            return ctx
        except Exception as exc:
            _ctx_error = str(exc)
            _ctx_retry_at = time.monotonic() + INIT_RETRY_INTERVAL
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    log.warning("初始化失败后的清理异常", exc_info=True)
            raise RuntimeError(f"cozyvtt 上下文初始化失败: {exc}") from exc


register_all(mcp, get_ctx)


def main() -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_dir / "cozyvtt-mcp.log"), logging.StreamHandler(sys.stderr)],
    )
    log.info("cozyvtt-mcp 启动 (stdio)，目标 %s", os.environ.get("COZYVTT_URL", "<env 未设置>"))
    mcp.run(show_banner=False)  # stdio 启动不需要横幅及其联网版本检查


if __name__ == "__main__":
    main()
