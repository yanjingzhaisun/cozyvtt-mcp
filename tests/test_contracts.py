"""固定 CozyVTT 1.2.2 契约的离线回归；不访问真实实例。"""
import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import responses

from auth import AuthError, AuthManager
from client import ApiError, CozyClient
from tools import Ctx
from ws_listener import WSListener
from test_cozyvtt import BASE, CID, FakeSIO, StubClient, build_tools, make_ctx, make_ws


def http_ctx():
    auth = AuthManager(BASE, "dm@invalid", "dummy", backoff=())
    return make_ctx(client=CozyClient(BASE, auth))


@responses.activate
def test_character_patch_preserves_nested_fields_and_replaces_arrays():
    original = {"hp": {"current": 12, "maximum": 20}, "skills": {"arcana": 4},
                "inventory": ["a", "b"], "note": "old"}
    responses.get(f"{BASE}/api/characters/c1", json={"character": {"data": original}})
    responses.put(f"{BASE}/api/characters/c1", json={"character": {"id": "c1"}})
    patch = {"data": {"hp": {"current": 5}, "inventory": [], "note": None}}
    saved_patch = copy.deepcopy(patch)
    out = build_tools(http_ctx())["character_update"]("c1", patch)
    assert out["ok"]
    body = json.loads(responses.calls[-1].request.body)
    assert body["data"] == {**original, "hp": {"current": 5, "maximum": 20},
                            "inventory": [], "note": None}
    assert patch == saved_patch


@responses.activate
@pytest.mark.parametrize("response", [{}, {"character": {}}, {"character": {"data": None}}])
def test_character_patch_fails_closed_on_bad_read(response):
    responses.get(f"{BASE}/api/characters/c1", json=response)
    out = build_tools(http_ctx())["character_update"]("c1", {"data": {"hp": 5}})
    assert not out["ok"]
    assert len(responses.calls) == 1


@pytest.mark.parametrize("patch", [{"hp": 5}, {"data": None}, {}, {"gameSystem": "DND_5E"}])
def test_character_patch_rejects_ambiguous_or_destructive_body(patch):
    c = StubClient()
    assert not build_tools(make_ctx(client=c))["character_update"]("c1", patch)["ok"]
    assert not c.calls


def test_character_updates_are_serialized():
    class Cards:
        def __init__(self):
            self.data = {"hp": {"current": 1, "maximum": 10}, "san": 50}
            self.reading = 0
        def get(self, path):
            self.reading += 1
            assert self.reading == 1
            return {"character": {"data": copy.deepcopy(self.data)}}
        def put(self, path, payload):
            self.data = payload["data"]
            self.reading -= 1
            return {"character": {"data": self.data}}
    c = Cards()
    tool = build_tools(make_ctx(client=c))["character_update"]
    barrier = threading.Barrier(2)
    def update(patch):
        barrier.wait(timeout=2)
        return tool("c1", {"data": patch})
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(update, [{"hp": {"current": 3}}, {"san": 40}]))
    assert all(r["ok"] for r in result)
    assert c.data == {"hp": {"current": 3, "maximum": 10}, "san": 40}


@responses.activate
@pytest.mark.parametrize("action,method,suffix", [
    ("start", "POST", "/sessions"), ("pause", "PUT", "/sessions/s1/pause"),
    ("end", "PUT", "/sessions/s1/end"),
])
def test_session_rest_contract(action, method, suffix):
    if action != "start":
        responses.get(f"{BASE}/api/campaigns/{CID}", json={"campaign": {"activeSession": {"id": "s1"}}})
    responses.add(method, f"{BASE}/api/campaigns/{CID}{suffix}", json={"message": "done"})
    ctx = http_ctx()
    assert build_tools(ctx)["session_manage"](action)["ok"]
    assert not ctx.ws.emitted
    assert responses.calls[-1].request.method == method


@responses.activate
def test_session_no_active_session_never_writes():
    responses.get(f"{BASE}/api/campaigns/{CID}", json={"campaign": {"activeSession": None}})
    assert not build_tools(http_ctx())["session_manage"]("pause")["ok"]
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("assign_status", [200, 403, 500])
def test_create_assign_and_partial_failure(assign_status):
    responses.post(f"{BASE}/api/characters", json={"character": {"id": "new-card"}}, status=201)
    responses.post(f"{BASE}/api/characters/new-card/assign", json={"message": "assignment"}, status=assign_status)
    out = build_tools(http_ctx())["character_create"]("New")
    assert out["ok"] is (assign_status == 200)
    assert json.loads(responses.calls[1].request.body) == {"campaignId": CID}
    assert len(responses.calls) == 2
    if assign_status != 200:
        assert out["data"]["created"] and not out["data"]["assigned"]
        assert out["data"]["character"]["id"] == "new-card"
        assert "勿重复创建" in out["error"]


def test_secret_roll_uses_upstream_field_and_pending_receipt():
    ctx = make_ctx()
    result = build_tools(ctx)["dice_roll"]("1d20", is_secret=True)
    event, payload = ctx.ws.emitted[-1]
    assert event == "dice.roll" and payload["secret"] is True
    assert "isSecret" not in payload
    assert result["data"]["confirmed"] is False
    assert result["data"]["status"] == "pending" and "rolled" not in result["data"]


@pytest.mark.parametrize("content,kind", [("x", "NARRATION"), ("", "DM"), ("x" * 2001, "DM")])
def test_chat_validation_never_sends(content, kind):
    ctx = make_ctx()
    assert not build_tools(ctx)["chat_send"](content, kind)["ok"]
    assert not ctx.ws.emitted


@pytest.mark.parametrize("system", [None, "CALL_OF_CTHULHU_7E", "SHADOWRUN_6E", "PATHFINDER_2E"])
def test_default_creature_search_excludes_srd(system):
    c = StubClient(system=system)
    assert build_tools(make_ctx(client=c))["creature_search"]()["ok"]
    assert c.calls[-1][2]["params"]["source"] == "custom"


def test_creature_source_normalized_and_invalid_rejected():
    c = StubClient(system="DND_5E")
    tool = build_tools(make_ctx(client=c))["creature_search"]
    assert tool(source=" SRD ")["ok"]
    assert c.calls[-1][2]["params"]["source"] == "srd"
    before = len(c.calls)
    assert not tool(source="all")["ok"]
    assert len(c.calls) == before


@responses.activate
@pytest.mark.parametrize("status", [401, 429, 500])
def test_failed_login_cooldown_across_calls(status):
    clock = [10.0]
    auth = AuthManager(BASE, "dummy", "dummy", clock=lambda: clock[0], backoff=())
    responses.post(f"{BASE}/api/auth/login", json={"message": "failed"}, status=status)
    with pytest.raises(AuthError):
        auth.relogin()
    with pytest.raises(AuthError, match="冷却"):
        auth.relogin()
    with pytest.raises(AuthError, match="冷却"):
        auth.login()
    assert len(responses.calls) == 1
    clock[0] += 181
    with pytest.raises(AuthError):
        auth.relogin()
    assert len(responses.calls) == 2


@responses.activate
def test_login_retry_after_extends_cooldown():
    clock = [1.0]
    auth = AuthManager(BASE, "dummy", "dummy", clock=lambda: clock[0])
    responses.post(f"{BASE}/api/auth/login", status=429, headers={"Retry-After": "900"})
    with pytest.raises(AuthError):
        auth.login()
    clock[0] += 500
    with pytest.raises(AuthError, match="冷却"):
        auth.relogin()
    assert len(responses.calls) == 1


def test_cookie_scope_secure_expiry_and_path():
    auth = AuthManager(BASE, "dummy", "dummy")
    jar = auth.session.cookies
    jar.set("valid", "yes", domain="test.local", path="/")
    jar.set("foreign", "no", domain="other.local", path="/")
    jar.set("private", "no", domain="test.local", path="/api")
    jar.set("secure", "yes", domain="test.local", path="/", secure=True)
    jar.set("expired", "no", domain="test.local", path="/", expires=1)
    assert auth.cookie_header() == "valid=yes"
    auth.base_url = "https://test.local/subpath"
    assert set(auth.cookie_header().split("; ")) == {"valid=yes", "secure=yes"}


def test_poll_paginates_without_loss_and_reports_overflow():
    ws = WSListener(BASE, CID, lambda: "", capacity=5, sio_factory=FakeSIO)
    for i in range(8):
        ws._append("chat.message", {"n": i})
    first = ws.poll(limit=2)
    assert first["gap"] and first["oldest_seq"] == 4
    assert [e["seq"] for e in first["events"]] == [4, 5]
    assert first["latest_seq"] == first["next_seq"] == 5
    assert first["high_water_seq"] == 8 and first["has_more"]
    second = ws.poll(since=first["next_seq"], limit=2)
    third = ws.poll(since=second["next_seq"], limit=2)
    assert [e["seq"] for e in second["events"] + third["events"]] == [6, 7, 8]
    assert not second["gap"] and not third["has_more"]
    assert ws.poll(since=100)["cursor_reset"]


@pytest.mark.parametrize("since,limit", [(-1, 1), (0, 0), (0, -1), (0, 501)])
def test_poll_rejects_bad_bounds(since, limit):
    with pytest.raises(ValueError):
        make_ws().poll(since=since, limit=limit)


def test_handshake_reordered_callbacks_and_wrong_campaign():
    ws = WSListener(BASE, CID, lambda: "", sio_factory=FakeSIO)
    sio = ws.sio
    sio.connected = True
    sio.push("connected", {})  # 应用 connected 先于 namespace connect
    assert not sio.emitted
    sio.handlers["connect"]()
    assert sio.emitted == [("authenticate", {"campaignId": CID})]
    sio.push("authenticated", {"campaignId": "wrong"})
    assert not ws.authenticated
    sio.push("authenticated", {"campaignId": CID})
    sio.handlers["connect"]()  # 迟到回调不能清除已认证状态
    assert ws.authenticated


def test_business_rejection_is_visible_and_never_confirmed():
    ws = make_ws()
    ws.sio.push("connected", {})
    ws.sio.push("authenticated", {"campaignId": CID})
    receipt = ws.emit("dice.roll", {"expression": "invalid"})
    ws.sio.push("error", {"message": "Invalid dice expression"})
    assert receipt["confirmed"] is False and receipt["status"] == "pending"
    out = ws.poll(since=receipt["since"])
    assert out["events"][-1]["event"] == "system.error"
    assert "Invalid dice expression" in out["last_error"]
    assert ws.authenticated  # 业务错误不能打掉正常连接


class AutoSIO(FakeSIO):
    def __init__(self):
        super().__init__()
        self.done = threading.Event()
    def connect(self, url, headers=None, wait_timeout=None):
        super().connect(url, headers, wait_timeout)
        self.push("connected", {})
    def emit(self, event, data):
        super().emit(event, data)
        if event == "authenticate":
            self.push("authenticated", {"campaignId": CID, "role": "DM"})
        if event == "initiative.request_state":
            self.done.set()


def test_supervisor_reconnects_refreshes_cookie_and_ignores_old_callbacks():
    instances = []
    cookies = ["sid=old"]
    def factory():
        sio = AutoSIO()
        instances.append(sio)
        return sio
    ws = WSListener(BASE, CID, lambda: cookies[0], sio_factory=factory, reconnect_backoff=(0.01,))
    try:
        ws.start(wait_timeout=2)
        old = ws.sio
        assert old.connect_headers == {"Cookie": "sid=old"}
        cookies[0] = "sid=new"
        old.disconnect()
        # 等管理线程进入新连接；Event/Condition 避免任意长 sleep。
        with ws._state:
            assert ws._state.wait_for(lambda: ws.sio is not old and ws.authenticated, timeout=2)
        assert ws.sio.connect_headers == {"Cookie": "sid=new"}
        old.push("authenticated", {"campaignId": CID})
        old.handlers["disconnect"]()
        assert ws.authenticated
        assert any(e["event"] == "system.reconnected" for e in ws.poll()["events"])
        ctx = Ctx(None, None, ws, CID)
        ctx.ensure_ws()
        assert len(instances) == 3  # 初始未连接对象 + 两次连接，没有竞争连接
    finally:
        ws.stop()
    assert not ws._thread.is_alive() and not ws.connected


def test_supervisor_auth_rejection_refreshes_and_recovers():
    created = []
    refreshed = threading.Event()
    class RejectFirst(AutoSIO):
        def emit(self, event, data):
            if event == "authenticate" and not refreshed.is_set():
                self.push("error", {"message": "Unauthorized"})
            else:
                super().emit(event, data)
    def factory():
        sio = RejectFirst()
        created.append(sio)
        return sio
    ws = WSListener(BASE, CID, lambda: "sid=dummy", sio_factory=factory,
                    auth_refresh=refreshed.set, reconnect_backoff=(0.01,))
    try:
        try:
            ws.start(wait_timeout=2)
        except RuntimeError:
            pass
        assert refreshed.wait(2)
        with ws._state:
            assert ws._state.wait_for(lambda: ws.authenticated, timeout=2)
        assert ws.last_error is None
    finally:
        ws.stop()


def test_supervisor_auth_timeout_and_stop():
    ws = WSListener(BASE, CID, lambda: "", sio_factory=FakeSIO,
                    auth_timeout=0.02, reconnect_backoff=(10,))
    try:
        with pytest.raises(RuntimeError, match="认证超时"):
            ws.start(wait_timeout=2)
    finally:
        ws.stop()
    assert not ws._thread.is_alive()


def test_real_fastmcp_schema_and_result():
    from fastmcp import Client, FastMCP
    from tools import register_all
    async def run():
        mcp = FastMCP("offline-contracts")
        ctx = make_ctx()
        register_all(mcp, lambda: ctx)
        async with Client(mcp) as client:
            listed = await client.list_tools()
            assert len(listed) == 20
            dice = next(t for t in listed if t.name == "dice_roll")
            assert "is_secret" in dice.input_schema["properties"]
            result = await client.call_tool("dice_roll", {"expression": "1d20", "is_secret": True})
            assert result.data["data"]["confirmed"] is False
            assert ctx.ws.emitted[-1][1]["secret"] is True
            bad = await client.call_tool("token_move", {"map_id": "m", "token_id": "t", "x": "bad", "y": 0}, raise_on_error=False)
            assert bad.is_error
    asyncio.run(run())


def test_cursor_reset_replays_new_process_events():
    ws = make_ws()
    ws._append("chat.message", {"content": "new process"})
    out = ws.poll(since=100)
    assert out["cursor_reset"] and out["events"][0]["seq"] == 1


def test_disconnect_still_allows_polling_errors():
    ws = make_ws()
    ws.sio.push("error", {"message": "Unauthorized"})
    ws.start = Mock(side_effect=RuntimeError("WS 尚未认证"))
    ctx = make_ctx(ws=ws)
    out = build_tools(ctx)["events_poll"]()
    assert out["ok"] and not out["data"]["authenticated"]
    assert out["data"]["events"][0]["event"] == "system.error"
    assert "connection_error" in out["data"]


def test_concurrent_ensure_ws_uses_one_worker():
    created = []
    def factory():
        sio = AutoSIO()
        created.append(sio)
        return sio
    ws = WSListener(BASE, CID, lambda: "", sio_factory=factory)
    ctx = make_ctx(ws=ws)
    try:
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: ctx.ensure_ws(), range(4)))
        assert len(created) == 2
        assert ctx._ws_started and ws.authenticated
    finally:
        ws.stop()


def test_transport_failure_retries_without_replaying_writes():
    created = []
    class FailingFirst(AutoSIO):
        def connect(self, *args, **kwargs):
            if len(created) == 2:
                raise RuntimeError("offline failure")
            super().connect(*args, **kwargs)
    def factory():
        sio = FailingFirst()
        created.append(sio)
        return sio
    ws = WSListener(BASE, CID, lambda: "", sio_factory=factory, reconnect_backoff=(0.01,))
    try:
        try:
            ws.start(wait_timeout=2)
        except RuntimeError:
            pass
        with ws._state:
            assert ws._state.wait_for(lambda: ws.authenticated, timeout=2)
        assert all(not any(e == "dice.roll" for e, _ in c.emitted) for c in created)
    finally:
        ws.stop()


def test_smoke_roll_matching_rejects_other_user_old_purpose_and_wrong_secrecy():
    from scripts.smoke_write import matches_roll
    payload = {"id": "roll-id", "userId": "u1", "purpose": "unique", "expression": "1d20", "secret": True}
    assert matches_roll(payload, "u1", "unique", "1d20", True)
    for change in ({"userId": "u2"}, {"purpose": "old"}, {"secret": False}, {"id": None}):
        assert not matches_roll({**payload, **change}, "u1", "unique", "1d20", True)


def test_read_smoke_rejects_success_envelope_without_business_data():
    from scripts.smoke import valid_result
    assert not valid_result("map_list", {"ok": True, "data": {"_raw": "<html>"}}, CID)
    assert valid_result("map_list", {"ok": True, "data": {"maps": []}}, CID)
    assert not valid_result("campaign_status", {"ok": True, "data": {"campaign": {"id": None}}}, CID)


def test_secret_contract_routes_to_dm_only_for_dm_roller():
    # 契约 fixture：1.2.2 dice handler 读取 secret；忽略未知的 isSecret。
    # 模拟服务端路由而不生成随机数或连接真实玩家。
    ctx = make_ctx()
    build_tools(ctx)["dice_roll"]("1d20", is_secret=True, purpose="privacy-contract")
    _, payload = ctx.ws.emitted[-1]
    audience = ["dm"] if payload.get("secret") else ["dm", "player"]
    assert audience == ["dm"] and payload["purpose"] == "privacy-contract"


@responses.activate
def test_network_login_failure_also_enters_cooldown():
    import requests
    auth = AuthManager(BASE, "dummy", "dummy")
    responses.post(f"{BASE}/api/auth/login", body=requests.ConnectionError("offline"))
    with pytest.raises(AuthError, match="网络错误"):
        auth.relogin()
    with pytest.raises(AuthError, match="冷却"):
        auth.relogin()
    assert len(responses.calls) == 1
