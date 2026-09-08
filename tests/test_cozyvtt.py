"""cozyvtt-mcp 单测：auth 重试 / 429 退避 / 缓冲 seq 语义 / 工具返回结构。
HTTP 层用 responses mock，WS 层用 fake socketio。"""
import time

import pytest
import responses as resp_lib

from auth import AuthManager, AuthError
from client import CozyClient, ApiError
from ws_listener import WSListener

BASE = "http://test.local"
CID = "camp-123"


# ---------- fakes ----------

class FakeSIO:
    def __init__(self):
        self.handlers = {}
        self.emitted = []
        self.connected = False
        self.connect_headers = None

    def on(self, event, fn):
        self.handlers[event] = fn

    def emit(self, event, data):
        self.emitted.append((event, data))

    def connect(self, url, headers=None, wait_timeout=None):
        self.connected = True
        self.connect_headers = headers() if callable(headers) else headers
        self.handlers["connect"]()

    def disconnect(self):
        self.connected = False
        self.handlers["disconnect"]("server disconnect")

    # 测试辅助：模拟服务端推送 / 重连
    def push(self, event, data):
        self.handlers[event](data)

    def simulate_reconnect(self):
        self.handlers["connect"]()


def make_auth():
    return AuthManager(BASE, "dm@x.local", "pw", min_relogin_interval=180,
                       backoff=(0.01, 0.02, 0.04), sleep=lambda s: None)


# ---------- auth ----------

@resp_lib.activate
def test_login_success():
    resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login",
                 json={"message": "Login successful", "user": {"email": "dm@x.local"}}, status=200)
    am = make_auth()
    data = am.login()
    assert data["user"]["email"] == "dm@x.local"
    assert am.user["email"] == "dm@x.local"
    # rememberMe 字段带上了
    import json as _j
    assert _j.loads(resp_lib.calls[0].request.body)["rememberMe"] is True


@resp_lib.activate
def test_login_429_backoff_then_success():
    for _ in range(3):
        resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login", json={"message": "rate limited"}, status=429)
    resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login", json={"user": {"email": "dm@x.local"}}, status=200)
    am = make_auth()
    am.login()
    assert len(resp_lib.calls) == 4  # 3 次 429 + 第 4 次成功


@resp_lib.activate
def test_login_429_exhausted_raises():
    for _ in range(4):
        resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login", json={"message": "rate limited"}, status=429)
    am = make_auth()
    with pytest.raises(AuthError):
        am.login()
    assert len(resp_lib.calls) == 4  # 最多 backoff+1 次，不循环撞墙


@resp_lib.activate
def test_relogin_min_interval_skip():
    resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login", json={"user": {}}, status=200)
    am = make_auth()
    am.login()
    assert am.relogin() is False           # 距上次 <180s → 跳过
    assert len(resp_lib.calls) == 1        # 没有第二次登录请求
    am._next_login_ts -= 400               # 模拟过了 400s
    resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login", json={"user": {}}, status=200)
    assert am.relogin() is True
    assert len(resp_lib.calls) == 2


# ---------- client ----------

@resp_lib.activate
def test_client_401_relogin_retry():
    resp_lib.add(resp_lib.GET, f"{BASE}/api/x", json={"message": "unauthorized"}, status=401)
    resp_lib.add(resp_lib.POST, f"{BASE}/api/auth/login", json={"user": {}}, status=200)
    resp_lib.add(resp_lib.GET, f"{BASE}/api/x", json={"hello": 1}, status=200)
    am = make_auth()
    am.min_relogin_interval = 0
    c = CozyClient(BASE, am, sleep=lambda s: None)
    assert c.get("/api/x") == {"hello": 1}
    assert len(resp_lib.calls) == 3


@resp_lib.activate
def test_client_429_backoff():
    resp_lib.add(resp_lib.GET, f"{BASE}/api/y", status=429)
    resp_lib.add(resp_lib.GET, f"{BASE}/api/y", status=429)
    resp_lib.add(resp_lib.GET, f"{BASE}/api/y", json={"ok": 1}, status=200)
    sleeps = []
    am = make_auth()
    c = CozyClient(BASE, am, backoff=(1, 2, 4), sleep=sleeps.append)
    assert c.get("/api/y") == {"ok": 1}
    assert sleeps == [1, 2]  # 指数退避序列


@resp_lib.activate
def test_client_error_contains_upstream_message():
    resp_lib.add(resp_lib.GET, f"{BASE}/api/z", json={"error": "Not Found", "message": "nope"}, status=404)
    c = CozyClient(BASE, make_auth(), sleep=lambda s: None)
    with pytest.raises(ApiError) as ei:
        c.get("/api/z")
    assert ei.value.status == 404
    assert "nope" in ei.value.message


# ---------- ws 缓冲 ----------

def make_ws():
    ws = WSListener(BASE, CID, cookie_getter=lambda: "sid=abc", capacity=5, sio_factory=FakeSIO)
    ws.sio.connect(BASE, headers=lambda: {"Cookie": ws._cookie_getter()})
    return ws


def test_ws_authenticate_on_connect():
    ws = make_ws()
    # 新握手：等服务端 'connected' 事件后才发 authenticate（2026-09-04 修复竞态）
    assert ("authenticate", {"campaignId": CID}) not in ws.sio.emitted
    ws.sio.push("connected", {"userId": "u1"})
    assert ("authenticate", {"campaignId": CID}) in ws.sio.emitted
    assert ws.sio.connect_headers == {"Cookie": "sid=abc"}


def test_ws_buffer_seq_and_poll():
    ws = make_ws()
    ws.sio.push("chat.message", {"content": "hi"})
    ws.sio.push("dice.rolled", {"total": 7})
    out = ws.poll(since=0)
    assert [e["seq"] for e in out["events"]] == [1, 2]
    assert out["events"][0]["event"] == "chat.message"
    assert out["latest_seq"] == 2
    # 增量拉取
    ws.sio.push("map.changed", {"id": "m1"})
    out2 = ws.poll(since=2)
    assert [e["event"] for e in out2["events"]] == ["map.changed"]
    assert out2["latest_seq"] == 3


def test_ws_ring_capacity_trim():
    ws = make_ws()
    for i in range(8):  # 容量 5
        ws.sio.push("chat.message", {"i": i})
    out = ws.poll(since=0, limit=100)
    assert len(out["events"]) == 5
    assert out["events"][0]["payload"]["i"] == 3  # 最老的被挤掉
    assert out["latest_seq"] == 8


def test_ws_reconnect_marks_system_event_only_after_authentication():
    ws = make_ws()
    ws._ever_authenticated = True
    ws.sio.push("connected", {"userId": "u1"})
    assert not ws.poll()["events"]
    ws.sio.push("authenticated", {"campaignId": CID, "role": "DM"})
    assert ws.poll()["events"][0]["event"] == "system.reconnected"
    assert ("initiative.request_state", {}) in ws.sio.emitted


def test_ws_latest():
    ws = make_ws()
    ws.sio.push("initiative.state", {"round": 1})
    ws.sio.push("chat.message", {"content": "x"})
    rec = ws.latest("initiative.state")
    assert rec["payload"]["round"] == 1
    assert ws.latest("session.ended") is None


# ---------- 工具返回结构 ----------

class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class StubClient:
    def __init__(self, fail=False, system=None):
        self.fail = fail
        self.system = system
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(("GET", path, kw))
        if self.fail:
            raise ApiError(500, "boom")
        if path == "/health":
            return {"status": "ok"}
        if path == f"/api/campaigns/{CID}":
            return {"campaign": {"id": CID, "name": "阿卡姆夜话", "status": "PREPARATION",
                                 "gameSystem": self.system, "activeSession": {"id": "session-1"}}}
        for suffix, key in (("messages", "messages"), ("maps", "maps"),
                            ("characters", "roster"), ("creatures", "creatures")):
            if path == f"/api/campaigns/{CID}/{suffix}":
                return {key: []}
        if path == "/api/characters/c1/validate":
            return {"isValid": True}
        raise AssertionError(f"未定义的 GET 契约: {path}")

    def post(self, path, payload=None, **kw):
        self.calls.append(("POST", path, payload))
        if path == "/api/characters":
            return {"character": {"id": "c1", **payload}}
        if path == "/api/characters/c1/assign":
            return {"character": {"id": "c1", "campaignId": payload["campaignId"]}}
        if path == f"/api/campaigns/{CID}/sessions":
            return {"session": {"id": "session-1"}}
        raise AssertionError(f"未定义的 POST 契约: {path}")

    def put(self, path, payload=None, **kw):
        self.calls.append(("PUT", path, payload))
        return {"message": "updated"}


class StubWS:
    connected = True
    authenticated = True

    def __init__(self):
        self.emitted = []

    def start(self):
        pass

    def emit(self, event, payload):
        self.emitted.append((event, payload))
        return {"sent": True, "confirmed": False, "status": "pending", "since": 0}

    def poll(self, since=0, limit=100):
        return {"events": [], "latest_seq": 0}

    def latest(self, event):
        return None


class StubAuth:
    user = {"id": "u1", "email": "dm@x.local"}

    def cookie_header(self):
        return "sid=x"


def make_ctx(client=None, ws=None):
    from tools import Ctx
    return Ctx(client or StubClient(), StubAuth(), ws or StubWS(), CID)


def build_tools(ctx):
    from tools import register_all
    m = FakeMCP()
    register_all(m, lambda: ctx)
    return m.tools


def test_tools_ok_structure():
    tools = build_tools(make_ctx())
    r = tools["chat_read"]()
    assert r["ok"] is True and "data" in r
    r = tools["campaign_status"]()
    assert r["ok"] is True
    assert r["data"]["campaign"]["name"] == "阿卡姆夜话"


def test_tools_error_structure_never_raises():
    tools = build_tools(make_ctx(client=StubClient(fail=True)))
    r = tools["chat_read"]()
    assert r["ok"] is False
    assert "HTTP 500" in r["error"] and "boom" in r["error"]


def test_chat_send_and_session_rest():
    ws = StubWS()
    tools = build_tools(make_ctx(ws=ws))
    r = tools["chat_send"](content="夜幕降临", type="DM")
    assert r["ok"] is True
    assert ("chat.message", {"content": "夜幕降临", "type": "DM"}) in ws.emitted
    r = tools["session_manage"](action="start")
    assert r["ok"] is True
    assert r["data"]["session"]["id"] == "session-1"
    assert not any(e.startswith("session.") for e, _ in ws.emitted)
    r = tools["session_manage"](action="bogus")
    assert r["ok"] is False and "action" in r["error"]


def test_dice_roll_throttle_queues():
    import tools as tools_pkg
    ctx = make_ctx()
    tools = build_tools(ctx)
    t0 = time.time()
    tools["dice_roll"](expression="1d20")
    tools["dice_roll"](expression="2d6", is_secret=True)
    elapsed = time.time() - t0
    assert elapsed >= tools_pkg.DICE_MIN_INTERVAL  # 第二次排队等待
    assert ctx.ws.emitted[0] == ("dice.roll", {"expression": "1d20", "secret": False})
    assert ctx.ws.emitted[1] == ("dice.roll", {"expression": "2d6", "secret": True})


def test_initiative_manage_validation():
    tools = build_tools(make_ctx())
    assert tools["initiative_manage"](action="add")["ok"] is False  # 缺 token/map
    assert tools["initiative_manage"](action="nope")["ok"] is False
    assert tools["initiative_manage"](action="next")["ok"] is True


def test_token_move_uses_rest_put():
    client = StubClient()
    tools = build_tools(make_ctx(client=client))
    r = tools["token_move"](map_id="m1", token_id="t1", x=3, y=4)
    assert r["ok"] is True
    assert ("PUT", f"/api/campaigns/{CID}/maps/m1/tokens/t1",
            {"position": {"x": 3, "y": 4}}) in client.calls


# ---- 系统门控（gameSystem 枚举裁剪工具面）----

def test_initiative_roll_gated_on_coc():
    tools = build_tools(make_ctx(client=StubClient(system="CALL_OF_CTHULHU_7E")))
    r = tools["initiative_manage"](action="roll", token_id="t1", map_id="m1")
    assert r["ok"] is False and "DND_5E" in r["error"]


def test_initiative_roll_allowed_on_5e():
    ws = StubWS()
    tools = build_tools(make_ctx(client=StubClient(system="DND_5E"), ws=ws))
    r = tools["initiative_manage"](action="roll", token_id="t1", map_id="m1",
                                   expression="1d20+3")
    assert r["ok"] is True
    assert ("initiative.roll", {"tokenId": "t1", "mapId": "m1",
                                "expression": "1d20+3"}) in ws.emitted


def test_initiative_roll_rejected_when_system_unset():
    tools = build_tools(make_ctx())  # StubClient 默认 gameSystem=None（flexible）
    r = tools["initiative_manage"](action="roll", token_id="t1", map_id="m1")
    assert r["ok"] is False and "未设置" in r["error"]


def test_initiative_set_and_reorder():
    ws = StubWS()
    tools = build_tools(make_ctx(ws=ws))
    assert tools["initiative_manage"](action="set", token_id="t1")["ok"] is False  # 缺 value
    r = tools["initiative_manage"](action="set", token_id="t1", map_id="m1", value=15)
    assert r["ok"] is True
    assert ("initiative.set", {"tokenId": "t1", "mapId": "m1", "value": 15}) in ws.emitted
    assert tools["initiative_manage"](action="reorder")["ok"] is False  # 缺列表
    r = tools["initiative_manage"](action="reorder", ordered_token_ids=["t2", "t1"])
    assert r["ok"] is True
    assert ("initiative.reorder", {"orderedTokenIds": ["t2", "t1"]}) in ws.emitted


def test_creature_search_srd_gated_on_coc():
    tools = build_tools(make_ctx(client=StubClient(system="CALL_OF_CTHULHU_7E")))
    r = tools["creature_search"](search="goblin", source="srd")
    assert r["ok"] is False and "5e" in r["error"]


def test_creature_search_srd_allowed_on_5e_and_custom_open():
    client = StubClient(system="DND_5E")
    tools = build_tools(make_ctx(client=client))
    assert tools["creature_search"](search="goblin", source="srd")["ok"] is True
    # custom 源不限系统（CoC 战役自建怪库）
    tools_coc = build_tools(make_ctx(client=StubClient(system="CALL_OF_CTHULHU_7E")))
    assert tools_coc["creature_search"](search="深潜者", source="custom")["ok"] is True


def test_campaign_status_features_surface():
    tools = build_tools(make_ctx(client=StubClient(system="DND_5E")))
    f = tools["campaign_status"]()["data"]["features"]
    assert f["srd_creature_library"] is True and f["initiative_roll"] is True
    tools = build_tools(make_ctx(client=StubClient(system="CALL_OF_CTHULHU_7E")))
    f = tools["campaign_status"]()["data"]["features"]
    assert f["srd_creature_library"] is False and f["initiative_roll"] is False


def test_character_create_and_validate():
    client = StubClient(system="CALL_OF_CTHULHU_7E")
    tools = build_tools(make_ctx(client=client))
    r = tools["character_create"](name="调查员甲", data={"occupation": "医生"})
    assert r["ok"] is True
    assert ("POST", "/api/characters",
            {"name": "调查员甲", "campaignId": CID,
             "data": {"occupation": "医生"}}) in client.calls
    r = tools["character_validate"](character_id="c1")
    assert r["ok"] is True
    assert ("GET", "/api/characters/c1/validate", {}) in client.calls
