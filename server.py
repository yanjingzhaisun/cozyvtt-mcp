#!/usr/bin/env python3
"""cozyvtt-mcp — MCP bridge for CozyVTT (FastMCP, stdio).

Lets an AI agent drive a CozyVTT campaign as DM/KP.
- REST 封装：client.py（401 重登重试 / 429 指数退避）
- 认证层：auth.py（rememberMe 登录 + 10min keepalive + 重登录限距）
- WS 层：ws_listener.py（环形缓冲 500，断线重连）
- 工具：tools/（统一 {ok,data,error}，异常不抛出 MCP 层）

启动自检：login → campaign_status 打日志；失败不阻塞 MCP server 启动，
工具调用时返回明确错误。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 保证 tools/ 等可导入

from fastmcp import FastMCP

from tools import Ctx, register_all

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "cozyvtt-mcp.log"), logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("cozyvtt.server")

mcp = FastMCP("cozyvtt")

_ctx: Ctx | None = None
_ctx_error: str | None = None


def get_ctx() -> Ctx:
    """懒初始化上下文；启动自检失败时首次工具调用返回明确错误。"""
    global _ctx, _ctx_error
    if _ctx is not None:
        return _ctx
    if _ctx_error is not None:
        raise RuntimeError(f"cozyvtt 上下文初始化失败: {_ctx_error}")
    try:
        ctx = Ctx.from_env()
        ctx.auth.login()
        ctx.auth.start_keepalive()
        # 启动自检（只打日志，不阻塞）
        try:
            camp = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}").get("campaign", {})
            log.info("自检通过：战役「%s」status=%s", camp.get("name"), camp.get("status"))
        except Exception as e:
            log.warning("campaign_status 自检失败（server 继续运行）: %s", e)
        # WS 连接失败不阻塞（工具调用时会 ensure_ws 重试）
        try:
            ctx.ensure_ws()
        except Exception as e:
            ctx._ws_started = False
            log.warning("WS 初始连接失败（server 继续运行）: %s", e)
        _ctx = ctx
        return ctx
    except Exception as e:
        _ctx_error = str(e)
        log.error("上下文初始化失败: %s", e)
        raise RuntimeError(f"cozyvtt 上下文初始化失败: {_ctx_error}") from e


register_all(mcp, get_ctx)


def main() -> None:
    log.info("cozyvtt-mcp 启动 (stdio)，目标 %s", os.environ.get("COZYVTT_URL", "<env 未设置>"))
    mcp.run()


if __name__ == "__main__":
    main()
