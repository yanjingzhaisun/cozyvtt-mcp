#!/usr/bin/env python3
"""CozyVTT MCP bridge (stdio): thread-safe lazy initialization, recovery, and cleanup."""
from __future__ import annotations

import argparse
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


def create_server(toolsets: str | None = None) -> FastMCP:
    mcp = FastMCP(
        "cozyvtt",
        lifespan=lifespan,
        instructions=(
            "Operate the configured CozyVTT campaign as the authenticated account; "
            "upstream resource permissions apply to every tool. Discovery is offline; "
            "the first business call lazily logs in. Upstream authentication allows "
            "5 failed attempts per 15 minutes per IP on reviewed v1.4.0; "
            "initialization and re-login failures "
            "have a 180-second cooldown, extended when required by Retry-After. "
            "Do not loop on authentication failures. Tool-body results use {ok,data?,error?}; "
            "HTTP errors retain status/upstream diagnostics, while argument validation is "
            "an MCP error. WS writes report pending, never a business ACK: inspect "
            "events_poll and state before taking further action. Annotations describe "
            "resource effects, not guaranteed delivery; document_read also caches binary "
            "files locally. No tool performs game-rule calculations for the caller."
        ),
    )
    register_all(mcp, get_ctx, toolsets)
    return mcp


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


# CLI selection is resolved before registration; imported servers honor the environment.
mcp = create_server(os.environ.get("COZYVTT_MCP_TOOLSETS")) if __name__ != "__main__" else None


def main() -> None:
    from tools.toolsets import describe_toolsets, select_tools
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolsets", metavar="NAMES",
                        help="Comma-separated presets; overrides COZYVTT_MCP_TOOLSETS (default: all)")
    parser.add_argument("--list-toolsets", action="store_true",
                        help="Print presets, tool counts, and the default, then exit")
    args = parser.parse_args()
    if args.list_toolsets:
        print(describe_toolsets())
        return
    selection = args.toolsets if args.toolsets is not None else os.environ.get("COZYVTT_MCP_TOOLSETS")
    try:
        select_tools(selection)
    except ValueError as exc:
        parser.error(str(exc))
    selected_mcp = create_server(selection)
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_dir / "cozyvtt-mcp.log"), logging.StreamHandler(sys.stderr)],
    )
    log.info("Starting cozyvtt-mcp (stdio), target %s", os.environ.get("COZYVTT_URL", "<env not set>"))
    selected_mcp.run(show_banner=False)  # stdio startup needs neither a banner nor its online version check


if __name__ == "__main__":
    main()
