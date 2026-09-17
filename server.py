#!/usr/bin/env python3
"""CozyVTT MCP bridge (stdio): thread-safe lazy initialization, recovery, and cleanup."""
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
            # Clean up network threads without blocking the MCP event loop.
            import asyncio
            await asyncio.to_thread(ctx.close)


mcp = FastMCP("cozyvtt", lifespan=lifespan)


def get_ctx() -> Ctx:
    """Log in on first use; retry initialization after cooldown rather than caching transient failures forever."""
    global _ctx, _ctx_error, _ctx_retry_at
    with _ctx_lock:
        if _ctx is not None:
            return _ctx
        remaining = _ctx_retry_at - time.monotonic()
        if remaining > 0:
            raise RuntimeError(f"cozyvtt initialization cooldown: {remaining:.1f}s remaining: {_ctx_error}")
        ctx = None
        try:
            ctx = Ctx.from_env()
            ctx.auth.login()
            try:
                camp = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}").get("campaign", {})
                log.info("Self-check: campaign %s status=%s", camp.get("name"), camp.get("status"))
            except Exception as exc:
                log.warning("Campaign self-check failed: %s", exc)
            ctx.auth.start_keepalive()
            # Start WS only on demand; REST reads do not depend on WS availability.
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
                    log.warning("Cleanup failed after initialization error", exc_info=True)
            raise RuntimeError(f"cozyvtt context initialization failed: {exc}") from exc


register_all(mcp, get_ctx)


def main() -> None:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_dir / "cozyvtt-mcp.log"), logging.StreamHandler(sys.stderr)],
    )
    log.info("Starting cozyvtt-mcp (stdio), target %s", os.environ.get("COZYVTT_URL", "<env not set>"))
    mcp.run(show_banner=False)  # stdio startup needs neither a banner nor its online version check


if __name__ == "__main__":
    main()
