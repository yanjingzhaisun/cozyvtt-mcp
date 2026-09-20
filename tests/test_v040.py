"""Offline preset and completeness contracts, pinned to CozyVTT v1.4.0.

API_DOCUMENTATION.yaml:2932,3740,3876,4071; source routes/maps.ts:69,714,1217
and routes/characters.ts:738. No real TCP or live campaign access.
"""
import asyncio
import json
import sys
from unittest.mock import Mock

import pytest
import responses
from fastmcp import Client, FastMCP

import server
from tools import register_all
from test_cozyvtt import BASE, CID, FakeMCP, build_tools
from test_contracts import http_ctx

EXPECTED = {
    "play": set("campaign_get chat_read chat_send dice_roll events_poll map_list map_switch "
                "map_create map_delete token_add token_move token_hp_update token_place_creature "
                "token_delete initiative_read initiative_manage session_list session_manage "
                "session_notes_update creature_search".split()),
    "docs": set("document_upload document_create document_list document_read document_update "
                "document_share document_unshare document_delete campaign_document_list".split()),
    "roster": set("character_list character_get character_create character_update character_validate "
                  "character_hitdice_spend character_delete".split()),
    "macros": set("saved_roll_list saved_roll_create saved_roll_update saved_roll_delete".split()),
    "admin": {"campaign_transfer_dm"},
}
EXPECTED["all"] = set().union(*EXPECTED.values())


@pytest.mark.parametrize("preset", EXPECTED)
def test_exact_preset_registration(preset):
    ctx = Mock(side_effect=AssertionError("Discovery must not initialize a context"))
    mcp = FastMCP("presets")
    register_all(mcp, ctx, preset)
    async def run():
        async with Client(mcp) as client:
            assert {t.name for t in await client.list_tools()} == EXPECTED[preset]
            if preset == "docs":
                missing = await client.call_tool("token_delete", {"map_id": "m", "token_id": "t"}, raise_on_error=False)
                assert missing.is_error
    asyncio.run(run())
    ctx.assert_not_called()


@pytest.mark.parametrize("selection", [None, "all", "all,play", "docs,all,docs"])
def test_all_and_default(selection):
    mcp = FakeMCP()
    register_all(mcp, Mock(), selection)
    assert set(mcp.tools) == EXPECTED["all"]


def test_union_duplicates_and_whitespace():
    mcp = FakeMCP()
    register_all(mcp, Mock(), " play,docs, play ")
    assert set(mcp.tools) == EXPECTED["play"] | EXPECTED["docs"]


@pytest.mark.parametrize("selection", ["", " ", "bogus", "play,", ",docs", "all,bogus"])
def test_invalid_selection_has_counts_and_does_not_register(selection):
    mcp = Mock()
    with pytest.raises(ValueError) as exc:
        register_all(mcp, Mock(), selection)
    for name, members in EXPECTED.items():
        assert f"{name} ({len(members)} tools)" in str(exc.value)
    mcp.tool.assert_not_called()


@pytest.mark.parametrize("env,flag,expected", [
    (None, None, None), ("docs", None, "docs"), ("docs", "play", "play"),
    ("invalid", "all", "all"), ("", "play", "play"),
])
def test_cli_flag_overrides_environment(monkeypatch, env, flag, expected):
    monkeypatch.delenv("COZYVTT_MCP_TOOLSETS", raising=False)
    if env is not None:
        monkeypatch.setenv("COZYVTT_MCP_TOOLSETS", env)
    monkeypatch.setattr(sys, "argv", ["server.py"] + ([] if flag is None else ["--toolsets", flag]))
    factory = Mock()
    monkeypatch.setattr(server, "create_server", factory)
    monkeypatch.setattr(server.logging, "basicConfig", Mock())
    monkeypatch.setattr(server.logging, "FileHandler", Mock())
    server.main()
    factory.assert_called_once_with(expected)
    factory.return_value.run.assert_called_once_with(show_banner=False)


@pytest.mark.parametrize("argv,env", [(["--toolsets", "oops"], None), ([], ""), ([], "oops")])
def test_cli_invalid_exit(monkeypatch, capsys, argv, env):
    monkeypatch.delenv("COZYVTT_MCP_TOOLSETS", raising=False)
    if env is not None:
        monkeypatch.setenv("COZYVTT_MCP_TOOLSETS", env)
    monkeypatch.setattr(sys, "argv", ["server.py", *argv])
    factory = Mock()
    monkeypatch.setattr(server, "create_server", factory)
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2
    # Read once: capsys drains its buffer.
    error = capsys.readouterr().err
    assert all(f"{name} ({len(members)} tools)" in error for name, members in EXPECTED.items())
    factory.assert_not_called()


def test_list_toolsets_exits_without_registration(monkeypatch, capsys):
    monkeypatch.setenv("COZYVTT_MCP_TOOLSETS", "invalid")
    monkeypatch.setattr(sys, "argv", ["server.py", "--list-toolsets"])
    factory = Mock()
    monkeypatch.setattr(server, "create_server", factory)
    server.main()
    output = capsys.readouterr().out
    assert "all (41 tools) [default]" in output
    for name in EXPECTED["all"]:
        assert name in output
    factory.assert_not_called()


C = f"{BASE}/api/campaigns/{CID}"
DELETES = [
    ("token_delete", {"map_id": "m", "token_id": "t"}, C + "/maps/m/tokens/t", "Token removed successfully"),
    ("map_delete", {"map_id": "m"}, C + "/maps/m", "Map deleted successfully"),
    ("character_delete", {"character_id": "c"}, BASE + "/api/characters/c", "Character deleted successfully"),
]


@responses.activate
@pytest.mark.parametrize("name,params,url,message", DELETES)
def test_delete_request_and_response(name, params, url, message):
    responses.delete(url, json={"message": message})
    ctx = http_ctx()
    ctx.ensure_ws = Mock(side_effect=AssertionError("REST only"))
    result = build_tools(ctx)[name](**params)
    assert result == {"ok": True, "data": {"message": message}}
    assert len(responses.calls) == 1
    assert responses.calls[0].request.body is None
    ctx.ensure_ws.assert_not_called()


@responses.activate
@pytest.mark.parametrize("name,params,url,message", DELETES)
@pytest.mark.parametrize("status", [403, 404])
def test_delete_error_diagnostics(name, params, url, message, status):
    body = {"message": "Resource unavailable", "code": "DENIED"}
    responses.delete(url, json=body, status=status)
    result = build_tools(http_ctx())[name](**params)
    assert result["ok"] is False
    assert str(status) in result["error"]
    assert result["data"] == {"status": status, "upstream": body}
    assert len(responses.calls) == 1


@responses.activate
def test_current_map_delete_rejected_without_switching():
    body = {"message": "Cannot delete the current map. Set a different map as current first."}
    responses.delete(C + "/maps/m", json=body, status=400)
    result = build_tools(http_ctx())["map_delete"](map_id="m")
    assert result["ok"] is False
    assert result["data"] == {"status": 400, "upstream": body}
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("optional", [{}, {"grid_size": 70, "spirit_layer_url": "spirit-asset"}])
def test_map_create_wire_contract(optional):
    payload = {"name": "Dungeon", "image_url": "map-asset", "width": 20, "height": 15, **optional}
    upstream = {"map": {"id": "new-map", "tokens": [], "annotations": []}}
    responses.post(C + "/maps", json=upstream, status=201)
    ctx = http_ctx()
    ctx.ensure_ws = Mock(side_effect=AssertionError("REST only"))
    assert build_tools(ctx)["map_create"](**payload) == {"ok": True, "data": upstream}
    expected = {"name": "Dungeon", "imageUrl": "map-asset", "width": 20, "height": 15,
                "gridSize": optional.get("grid_size", 50)}
    if optional:
        expected["spiritLayerUrl"] = "spirit-asset"
    assert json.loads(responses.calls[0].request.body) == expected
    assert len(responses.calls) == 1
    ctx.ensure_ws.assert_not_called()


@responses.activate
@pytest.mark.parametrize("status", [400, 403])
def test_map_create_upstream_failure(status):
    body = {"message": "Invalid image", "validationErrors": {"imageUrl": "unavailable"}}
    responses.post(C + "/maps", json=body, status=status)
    result = build_tools(http_ctx())["map_create"]("Dungeon", "asset", 20, 15)
    assert result["ok"] is False
    assert result["data"] == {"status": status, "upstream": body}
    assert len(responses.calls) == 1


@pytest.mark.parametrize("patch", [
    {"name": " "}, {"image_url": ""}, {"width": 0}, {"height": -1},
    {"grid_size": 0}, {"width": 1.5}, {"height": True},
])
def test_map_create_invalid_no_transport(patch):
    ctx = http_ctx()
    ctx.client = Mock()
    payload = {"name": "Dungeon", "image_url": "asset", "width": 20, "height": 15, **patch}
    assert build_tools(ctx)["map_create"](**payload)["ok"] is False
    ctx.client.post.assert_not_called()


@pytest.mark.parametrize("flag,env,expected", [
    (None, None, "all"), ("play", None, "play"), ("docs", None, "docs"),
    (None, "roster", "roster"), ("docs", "play", "docs"),
])
def test_stdio_selection(flag, env, expected, tmp_path):
    from pathlib import Path
    from fastmcp.client.transports import StdioTransport
    root = Path(__file__).resolve().parent.parent
    bootstrap = (
        "import socket,runpy; "
        "socket.socket.connect=socket.create_connection=lambda *a,**k: "
        "(_ for _ in ()).throw(RuntimeError('network disabled')); "
        f"runpy.run_path({str(root / 'server.py')!r},run_name='__main__')"
    )
    async def run():
        transport = StdioTransport(
            command=sys.executable,
            args=["-c", bootstrap] + ([] if flag is None else ["--toolsets", flag]),
            cwd=str(root), keep_alive=False,
            env={"COZYVTT_MCP_TOOLSETS": env or "all"},
            log_file=tmp_path / "stdio.stderr",
        )
        async with Client(transport) as client:
            assert {tool.name for tool in await client.list_tools()} == EXPECTED[expected]
    asyncio.run(asyncio.wait_for(run(), timeout=20))
