"""初始化并发与真实 stdio 生命周期；所有上游交互均离线。"""
import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

import server


@pytest.fixture
def fresh_server(monkeypatch):
    monkeypatch.setattr(server, "_ctx", None)
    monkeypatch.setattr(server, "_ctx_error", None)
    monkeypatch.setattr(server, "_ctx_retry_at", 0.0)
    return server


def fake_context():
    ctx = Mock()
    ctx.campaign_id = "offline"
    ctx.client.get.return_value = {"campaign": {"id": "offline"}}
    return ctx


def test_concurrent_first_calls_initialize_once(fresh_server, monkeypatch):
    ctx = fake_context()
    entered = threading.Event()
    release = threading.Event()
    def login():
        entered.set()
        assert release.wait(2)
    ctx.auth.login.side_effect = login
    factory = Mock(return_value=ctx)
    monkeypatch.setattr(server.Ctx, "from_env", factory)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(server.get_ctx)
        assert entered.wait(2)
        second = pool.submit(server.get_ctx)
        release.set()
        assert first.result(2) is second.result(2) is ctx
    assert factory.call_count == 1
    ctx.auth.login.assert_called_once()
    ctx.auth.start_keepalive.assert_called_once()
    ctx.ensure_ws.assert_not_called()  # REST 首次调用不依赖 WS


def test_initialization_failure_cleans_up_and_recovers(fresh_server, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    failed = fake_context()
    failed.auth.login.side_effect = RuntimeError("temporary outage")
    ready = fake_context()
    factory = Mock(side_effect=[failed, ready])
    monkeypatch.setattr(server.Ctx, "from_env", factory)
    with pytest.raises(RuntimeError, match="temporary outage"):
        server.get_ctx()
    failed.close.assert_called_once()
    with pytest.raises(RuntimeError, match="冷却"):
        server.get_ctx()
    assert factory.call_count == 1
    clock[0] += server.INIT_RETRY_INTERVAL + 1
    assert server.get_ctx() is ready
    assert server._ctx_error is None


def test_lifespan_closes_context(fresh_server, monkeypatch):
    ctx = fake_context()
    monkeypatch.setattr(server, "_ctx", ctx)
    async def run():
        async with server.lifespan(server.mcp):
            pass
    asyncio.run(run())
    ctx.close.assert_called_once()
    assert server._ctx is None


def test_stdio_list_and_validation_without_live_campaign(tmp_path):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    root = Path(__file__).resolve().parent.parent
    # 子进程也禁用网络；工具列表和参数校验不应触发上游连接。
    bootstrap = (
        "import socket,runpy; "
        "socket.socket.connect=socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network disabled')); "
        f"runpy.run_path({str(root / 'server.py')!r},run_name='__main__')"
    )
    async def run():
        transport = StdioTransport(
            command=sys.executable, args=["-c", bootstrap], cwd=str(root), keep_alive=False,
            env={"COZYVTT_EMAIL": "", "COZYVTT_PASSWORD": "", "COZYVTT_CAMPAIGN_ID": ""},
            log_file=tmp_path / "stdio.stderr",
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            assert len(tools) == 20
            invalid = await client.call_tool("token_move", {}, raise_on_error=False)
            assert invalid.is_error
            missing_env = await client.call_tool("chat_read", {})
            assert missing_env.data["ok"] is False
            assert "缺少" in missing_env.data["error"]
    asyncio.run(asyncio.wait_for(run(), timeout=15))
