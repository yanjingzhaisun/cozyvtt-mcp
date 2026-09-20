"""v0.2 dual-baseline offline contracts; responses/FakeSIO, with real TCP blocked by conftest."""
import asyncio
import inspect
import io
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from client import CozyClient
from tools import E_CURSOR, E_OLD, E_PENDING, E_RESOURCE, Ctx
from tools import document_tools
from test_cozyvtt import BASE, CID, StubClient, build_tools, make_auth, make_ctx, make_ws
from test_contracts import http_ctx

C = f"{BASE}/api/campaigns/{CID}"
DOC = "6d452c88-ffba-44ad-af06-51806b51b4b3"


@responses.activate
def test_chat_cursor_roundtrip_and_raw_pagination():
    first = {"messages": [{"id": "m2"}], "pagination": {"limit": 1, "hasMore": True, "nextCursor": "opaque-_"}}
    last = {"messages": [{"id": "m1"}], "pagination": {"limit": 1, "hasMore": False, "nextCursor": None}}
    responses.get(C + "/messages", json=first)
    responses.get(C + "/messages", json=last)
    tool = build_tools(http_ctx())["chat_read"]
    assert tool(limit=1)["data"] == first
    assert tool(limit=1, cursor=first["pagination"]["nextCursor"])["data"] == last
    assert parse_qs(urlsplit(responses.calls[0].request.url).query) == {"limit": ["1"]}
    assert parse_qs(urlsplit(responses.calls[1].request.url).query) == {"limit": ["1"], "cursor": ["opaque-_"]}
    assert "offset" not in inspect.signature(tool).parameters
    assert not tool(offset=1)["ok"]
    assert len(responses.calls) == 2


@responses.activate
@pytest.mark.parametrize("latest_first", [True, False])
def test_old_chat_never_returns_repeated_page_as_history(latest_first):
    old = {"messages": [{"id": "latest"}]}
    responses.get(C + "/messages", json=old)
    tool = build_tools(http_ctx())["chat_read"]
    if latest_first:
        assert tool()["data"] == old
    out = tool(cursor="ignored-by-old-server")
    assert not out["ok"] and E_CURSOR in out["error"]
    assert len(responses.calls) == 1


@pytest.mark.parametrize("limit", [0, -1, 101, 1.5, True])
def test_chat_bad_limit_no_request(limit):
    client = StubClient()
    assert not build_tools(make_ctx(client=client))["chat_read"](limit=limit)["ok"]
    assert not client.calls


@responses.activate
@pytest.mark.parametrize("status", [401, 429])
def test_multipart_retries_rewind_file(status):
    seen = []
    def callback(request):
        seen.append(request.body)
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        return (status if len(seen) == 1 else 201, {"Content-Type": "application/json"}, '{"asset":{"id":"a"}}')
    responses.add_callback(responses.POST, BASE + "/api/assets/upload", callback=callback)
    if status == 401:
        responses.post(BASE + "/api/auth/login", json={"user": {"id": "me"}})
    client = CozyClient(BASE, make_auth(), sleep=lambda _: None)
    stream = io.BytesIO(b"prefix-unique-document-bytes")
    stream.seek(7)
    out = client.post_multipart("/api/assets/upload", {"type": "DOCUMENT", "scope": "USER"},
                                {"file": ("notes.md", stream, "text/markdown")})
    assert out == {"asset": {"id": "a"}}
    assert len(seen) == 2
    assert all(b"unique-document-bytes" in body and b"prefix-" not in body for body in seen)
    assert all(b'name="type"' in body and b'DOCUMENT' in body for body in seen)


@responses.activate
@pytest.mark.parametrize("mime,body,expected", [
    ("text/plain; charset=utf-8", "\u8c03\u67e5\u5458".encode(), "\u8c03\u67e5\u5458"),
    ("text/markdown", "# \u9ab0\u5b50".encode(), "# \u9ab0\u5b50"),
    ("application/pdf", b"%PDF-1.7\x00\xff", b"%PDF-1.7\x00\xff"),
    ("application/octet-stream", b"\xff\x00", b"\xff\x00"),
])
def test_raw_client_uses_content_type(mime, body, expected):
    responses.get(BASE + "/raw", body=body, headers={"Content-Type": mime, "ETag": 'W/"v1"'})
    out = CozyClient(BASE, make_auth()).get_raw("/raw")
    assert out == {"mime_type": mime.split(";")[0], "etag": 'W/"v1"', "content": expected}


@responses.activate
def test_document_upload_fields_and_body(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text("# \u539f\u6587", encoding="utf-8")
    responses.post(BASE + "/api/assets/upload", json={"asset": {"id": DOC}}, status=201)
    out = build_tools(http_ctx())["document_upload"](str(path), scope="CAMPAIGN", name="\u89c4\u5219", tags=["A", "B"])
    assert out["ok"]
    body = responses.calls[0].request.body
    assert b'name="campaignId"' in body and CID.encode() in body
    assert b'name="tags"' in body and b"A,B" in body
    assert b'filename="rules.md"' in body and "# \u539f\u6587".encode() in body


DOCUMENT_CASES = [
    ("document_upload", "POST", "/api/assets/upload", {}),
    ("document_create", "POST", "/api/assets/documents", {"name": "n", "format": "md", "content": ""}),
    ("document_list", "GET", "/api/assets", {}),
    ("campaign_document_list", "GET", f"/api/campaigns/{CID}/documents", {}),
    ("document_read", "GET", f"/api/assets/documents/{DOC}", {"document_id": DOC}),
    ("document_update", "PUT", f"/api/assets/documents/{DOC}/content", {"document_id": DOC, "content": ""}),
    ("document_share", "POST", f"/api/campaigns/{CID}/documents", {"document_id": DOC}),
    ("document_unshare", "DELETE", f"/api/campaigns/{CID}/documents/{DOC}", {"document_id": DOC}),
    ("document_delete", "DELETE", f"/api/assets/{DOC}", {"document_id": DOC}),
]


@responses.activate
@pytest.mark.parametrize("tool,method,path,args", DOCUMENT_CASES)
@pytest.mark.parametrize("message,expected", [
    ("The requested resource does not exist", E_OLD),
    ("Document not found", E_RESOURCE + "Document not found"),
])
def test_all_document_404_categories(tool, method, path, args, message, expected, tmp_path):
    args = dict(args)
    if tool == "document_upload":
        file = tmp_path / "a.txt"
        file.write_text("hello")
        args["file_path"] = str(file)
    responses.add(method, BASE + path, status=404, json={"error": "Not Found", "message": message})
    out = build_tools(http_ctx())[tool](**args)
    assert out == {"ok": False, "error": expected,
                   "data": {"status": 404, "upstream": {"error": "Not Found", "message": message}}}
    assert len(responses.calls) == 1  # Do not try alternative endpoints, repeat deletion, or re-upload


@responses.activate
def test_old_upload_type_400_is_not_version_404(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello")
    responses.post(BASE + "/api/assets/upload", status=400, json={"message": "Invalid asset type"})
    out = build_tools(http_ctx())["document_upload"](str(path))
    assert "HTTP 400: Invalid asset type" == out["error"]
    assert len(responses.calls) == 1


@responses.activate
def test_document_text_pdf_and_304(tmp_path, monkeypatch):
    directory = tmp_path / "downloads"
    monkeypatch.setattr(document_tools, "DOWNLOAD_DIR", directory)
    url = BASE + f"/api/assets/documents/{DOC}"
    responses.get(url, body="# \u539f\u6587", headers={"Content-Type": "text/plain; charset=utf-8", "ETag": '"one"'})
    pdf = b"%PDF-1.7\xff\x00original-bytes"
    responses.get(url, body=pdf, headers={"Content-Type": "application/pdf", "ETag": '"two"'})
    responses.get(url, status=304, headers={"ETag": '"two"'})
    tool = build_tools(http_ctx())["document_read"]
    assert tool(DOC)["data"] == {"mime_type": "text/plain", "etag": '"one"', "content": "# \u539f\u6587"}
    assert not directory.exists()
    out = tool(DOC)["data"]
    assert out == {"mime_type": "application/pdf", "etag": '"two"',
                   "file_path": str(directory / f"{DOC}.pdf"), "file_size": len(pdf)}
    assert Path(out["file_path"]).read_bytes() == pdf
    assert tool(DOC, etag='"two"')["data"] == {"not_modified": True}
    assert responses.calls[-1].request.headers["If-None-Match"] == '"two"'
    assert len(list(directory.iterdir())) == 1


@pytest.mark.parametrize("identifier", ["../outside", "/tmp/file", "..", "a/b", ""])
def test_document_read_rejects_unsafe_file_name(identifier):
    c = StubClient()
    assert not build_tools(make_ctx(client=c))["document_read"](identifier)["ok"]
    assert not c.calls


@responses.activate
def test_document_create_update_share_unshare_and_delete_payloads():
    responses.post(BASE + "/api/assets/documents", json={"asset": {"id": DOC}}, status=201)
    responses.put(BASE + f"/api/assets/documents/{DOC}/content", json={"asset": {"id": DOC}})
    responses.post(C + "/documents", json={"link": {"assetId": DOC}}, status=201)
    responses.delete(C + f"/documents/{DOC}", json={"message": "Document unshared"})
    responses.delete(BASE + f"/api/assets/{DOC}", json={"message": "Asset deleted successfully"})
    t = build_tools(http_ctx())
    assert t["document_create"](" n ", "md", "# \u6b63\u6587", "CAMPAIGN", description=" d ")["ok"]
    assert json.loads(responses.calls[-1].request.body) == {
        "name": "n", "format": "md", "content": "# \u6b63\u6587", "scope": "CAMPAIGN", "campaignId": CID, "description": "d"}
    assert t["document_update"](DOC, "")["ok"]
    assert json.loads(responses.calls[-1].request.body) == {"content": ""}
    assert t["document_share"](DOC)["ok"]
    assert json.loads(responses.calls[-1].request.body) == {"assetId": DOC}
    assert t["document_unshare"](DOC)["ok"]
    assert t["document_delete"](DOC)["ok"]
    assert all(c.request.body is None for c in responses.calls[-2:])


@responses.activate
@pytest.mark.parametrize("scope", [None, "USER", "CAMPAIGN", "GLOBAL"])
def test_document_scopes_and_admin_personal_filter(scope):
    responses.get(BASE + "/api/assets", json={"assets": [], "pagination": {"page": 2}})
    ctx = http_ctx()
    ctx.auth.user = {"id": "admin", "platformRole": "ADMIN"}
    assert build_tools(ctx)["document_list"](scope=scope, page=2, limit=7, search="book")["ok"]
    params = parse_qs(urlsplit(responses.calls[-1].request.url).query)
    expected = {"type": ["DOCUMENT"], "page": ["2"], "limit": ["7"], "search": ["book"]}
    if scope:
        expected["scope"] = [scope]
    if scope == "CAMPAIGN":
        expected["campaignId"] = [CID]
    if scope == "USER":
        expected["uploadedBy"] = ["admin"]
    assert params == expected


@pytest.mark.parametrize("content", ["\x00", "\x7f", "\u754c" * (900 * 1024 // 3 + 1)])
def test_typed_document_limits(content):
    c = StubClient()
    t = build_tools(make_ctx(client=c))
    assert not t["document_create"]("n", "txt", content)["ok"]
    assert not t["document_update"](DOC, content)["ok"]
    assert not c.calls


@responses.activate
def test_saved_roll_crud_full_private_payloads():
    macro = {"id": "macro", "name": "Attack", "expression": "1d20+3", "userId": "me", "campaignId": CID}
    responses.get(C + "/macros", json={"macros": [macro]})
    responses.post(C + "/macros", json={"macro": macro}, status=201)
    responses.put(C + "/macros/macro", json={"macro": {**macro, "name": "Defense"}})
    responses.delete(C + "/macros/macro", json={"message": "Macro deleted"})
    t = build_tools(http_ctx())
    assert "saved_roll_get" not in t
    assert t["saved_roll_list"]()["data"] == {"macros": [macro]}
    assert t["saved_roll_create"](" Attack ", " 1d20+3 ")["data"] == {"macro": macro}
    assert json.loads(responses.calls[-1].request.body) == {"name": "Attack", "expression": "1d20+3"}
    assert t["saved_roll_update"]("macro", name="Defense")["ok"]
    assert json.loads(responses.calls[-1].request.body) == {"name": "Defense"}
    assert t["saved_roll_delete"]("macro")["ok"]


@responses.activate
@pytest.mark.parametrize("message,status", [("Macro not found", 404),
    ("The requested resource does not exist", 404),
    ("You already have 50 macros in this campaign. Delete one to make room.", 400),
    ("Invalid dice expression", 400)])
def test_saved_roll_errors_no_fallback(message, status):
    responses.post(C + "/macros", json={"message": message}, status=status)
    out = build_tools(http_ctx())["saved_roll_create"]("n", "bad-expression")
    assert not out["ok"]
    assert out["error"] == (E_OLD if message.startswith("The requested") else E_RESOURCE + message if status == 404 else f"HTTP 400: {message}")
    assert len(responses.calls) == 1


@pytest.mark.parametrize("tool,args", [
    ("saved_roll_create", {"name": " ", "expression": "1d20"}),
    ("saved_roll_create", {"name": "n", "expression": "x" * 201}),
    ("saved_roll_update", {"macro_id": "m"}),
    ("saved_roll_update", {"macro_id": "m", "name": "x" * 61}),
])
def test_macro_invalid_fields_no_request(tool, args):
    c = StubClient()
    assert not build_tools(make_ctx(client=c))[tool](**args)["ok"]
    assert not c.calls


@responses.activate
@pytest.mark.parametrize("target", ["me-owner", "other-member"])
def test_transfer_and_owner_reclaim_same_endpoint(target):
    responses.get(C, json={"campaign": {"gameSystem": "DND_5E", "userRole": "DM"}})
    responses.put(C + "/dm", json={"memberships": [{"userId": target, "role": "DM"}]})
    responses.get(C, json={"campaign": {"gameSystem": "CALL_OF_CTHULHU_7E", "userRole": "PLAYER"}})
    ctx = http_ctx()
    assert ctx.get_system() == "DND_5E"
    assert build_tools(ctx)["campaign_transfer_dm"](target)["ok"]
    assert ctx.cached_role() is None
    assert ctx.get_system() == "CALL_OF_CTHULHU_7E"
    assert json.loads(responses.calls[1].request.body) == {"userId": target}
    assert not ctx.ws.emitted  # Do not simulate transfer with two membership role writes


@pytest.mark.parametrize("system,allowed", [("DND_5E", True), (None, False),
    ("CALL_OF_CTHULHU_7E", False), ("PATHFINDER_2E", False), ("SHADOWRUN_6E", False)])
def test_hitdice_only_system_gate_and_pending(system, allowed):
    ctx = make_ctx(client=StubClient(system=system))
    out = build_tools(ctx)["character_hitdice_spend"]("c1", 0)
    assert out["ok"] == allowed
    if allowed:
        assert out["data"]["note"] == E_PENDING
        assert out["data"]["confirmed"] is False and out["data"]["status"] == "pending"
        assert ctx.ws.emitted == [("character.hitdice.spend", {"characterId": "c1", "index": 0})]
        assert len(ctx.client.calls) == 1  # Only fetch the system; no version probe or PUT to simulate spending
    else:
        assert not ctx.ws.emitted


@pytest.mark.parametrize("index", [-1, 0.5, True])
def test_invalid_hitdice_index_never_sends(index):
    ctx = make_ctx(client=StubClient(system="DND_5E"))
    assert not build_tools(ctx)["character_hitdice_spend"]("c", index)["ok"]
    assert not ctx.ws.emitted and not ctx.client.calls


def authenticated_ws():
    ws = make_ws()
    ws.sio.push("connected", {"userId": "u1"})
    ws.sio.push("authenticated", {"campaignId": CID, "userId": "u1", "role": "DM"})
    return ws


def test_new_events_preserve_payloads_and_clear_cached_campaign():
    ws = authenticated_ws()
    ctx = make_ctx(client=StubClient(system="DND_5E"), ws=ws)
    assert ctx.get_system() == "DND_5E" and ctx.cached_role() == "DM"
    events = [("character.updated", {"characterId": "c", "tokensChanged": True}),
              ("roster.updated", {"campaignId": CID}), ("dice.historyCleared", {"campaignId": CID}),
              ("campaign.dm.transferred", {"campaignId": CID, "previousDmId": "u1", "newDmId": "u2"})]
    for event, payload in events:
        ws.sio.push(event, payload)
    assert ctx.cached_role() is None
    ctx.client.system = "CALL_OF_CTHULHU_7E"
    assert ctx.get_system() == "CALL_OF_CTHULHU_7E"
    out = build_tools(ctx)["events_poll"]()["data"]
    assert [(e["event"], e["payload"]) for e in out["events"]] == events
    assert out["role"] == "PLAYER" and not out["stale"]


@pytest.mark.parametrize("message", ["You are no longer a member of this campaign", "Unauthorized"])
def test_revocation_cancels_auth_and_surfaces_error(message):
    ws = authenticated_ws()
    ctx = make_ctx(ws=ws)
    ctx.get_system()
    ws.sio.push("error", {"message": message})
    assert not ws.authenticated and ctx.cached_role() is None
    ws.start = Mock(side_effect=RuntimeError("not authenticated"))
    out = build_tools(ctx)["events_poll"]()["data"]
    assert out["stale"] and not out["authenticated"]
    assert out["events"][-1]["payload"]["detail"]["message"] == message
    with pytest.raises(RuntimeError):
        ws.emit("character.hitdice.spend", {"characterId": "c", "index": 0}, wait_auth=0)


@responses.activate
def test_rest_401_invalidates_existing_ws_and_retries_once():
    ws = authenticated_ws()
    auth = make_auth()
    client = CozyClient(BASE, auth)
    ctx = Ctx(client, auth, ws, CID)
    responses.get(C + "/messages", status=401, json={"message": "Unauthorized"})
    responses.post(BASE + "/api/auth/login", json={"user": {"id": "u1"}})
    responses.get(C + "/messages", json={"messages": []})
    assert build_tools(ctx)["chat_read"]()["ok"]
    assert not ws.authenticated and ws.poll()["stale"]
    assert len(responses.calls) == 3


@responses.activate
def test_session_history_notes_and_end_payload():
    history = {"sessions": [{"id": "s", "endedAt": None, "notes": None}]}
    responses.get(C + "/sessions", json=history)
    responses.put(C + "/sessions/s/notes", json={"session": {"id": "s", "notes": None}})
    responses.get(C, json={"campaign": {"activeSession": {"id": "s"}}})
    responses.put(C + "/sessions/s/end", json={"message": "ended", "stateSaved": False})
    ctx = http_ctx()
    t = build_tools(ctx)
    assert t["session_list"]()["data"] == history
    assert t["session_notes_update"]("s", "")["ok"]
    assert json.loads(responses.calls[-1].request.body) == {"notes": ""}
    assert t["session_manage"]("end", notes="\u5171\u4eab\u6458\u8981", save_state=False)["ok"]
    assert json.loads(responses.calls[-1].request.body) == {"notes": "\u5171\u4eab\u6458\u8981", "saveState": False}
    assert not ctx.ws.emitted


@pytest.mark.parametrize("tool,args", [
    ("session_manage", {"action": "pause", "notes": "no"}),
    ("session_manage", {"action": "end", "notes": "x" * 2001}),
    ("session_notes_update", {"session_id": "s", "notes": "x" * 2001}),
])
def test_bad_session_parameters_no_requests(tool, args):
    c = StubClient()
    assert not build_tools(make_ctx(client=c))[tool](**args)["ok"]
    assert not c.calls


@responses.activate
@pytest.mark.parametrize("valid", [True, False])
def test_character_validate_always_marked_unreliable(valid):
    responses.get(BASE + "/api/characters/c/validate", json={"isValid": valid, "errors": ["detail"]})
    data = build_tools(http_ctx())["character_validate"]("c")["data"]
    assert data["isValid"] == valid and data["errors"] == ["detail"]
    assert data["validation_reliable"] is False and "v1.4" in data["validation_note"]


@responses.activate
def test_validation_errors_are_preserved():
    error = {"message": "Invalid character data", "validationErrors": [{"path": "hitDice.0.remaining", "message": "invalid"}]}
    responses.put(BASE + "/api/characters/c", status=400, json=error)
    out = build_tools(http_ctx())["character_update"]("c", {"name": "new"})
    assert not out["ok"] and out["data"]["upstream"] == error
    assert len(responses.calls) == 1


@responses.activate
def test_v14_character_create_already_in_roster_skips_assign():
    responses.post(BASE + "/api/characters", json={"character": {"id": "new", "campaignId": CID}}, status=201)
    responses.get(C + "/characters", json={"roster": [{"userId": "me", "characters": [{"id": "new"}]}]})
    out = build_tools(http_ctx())["character_create"]("Investigator", data={})
    assert out["data"]["assigned"] is True
    assert len(responses.calls) == 2
    assert json.loads(responses.calls[0].request.body)["data"] == {}


@responses.activate
def test_coc_extensions_and_legacy_hitdice_survive_patch():
    original = {"conditions": {"temporaryInsanity": True},
                "spellsAndMythos": {"cthulhuMythos": 7, "spells": ["Sign"]},
                "skills": {"cthulhuMythos": {"currentValue": 9}},
                "appearance": {"age": "old"}, "notes": "Keeper notes",
                "hitDice": [{"class": "Fighter", "total": "5d10", "die": "d10", "maximum": 5, "remaining": 2}],
                "unknownExtension": {"keep": 42}}
    responses.get(BASE + "/api/characters/c", json={"character": {"gameSystem": "CALL_OF_CTHULHU_7E", "data": original}})
    responses.put(BASE + "/api/characters/c", json={"character": {"id": "c"}})
    assert build_tools(http_ctx())["character_update"]("c", {"data": {"notes": "new"}})["ok"]
    assert json.loads(responses.calls[-1].request.body)["data"] == {**original, "notes": "new"}


def test_hitdice_business_error_visible_without_changing_pending_receipt():
    ws = authenticated_ws()
    ctx = make_ctx(client=StubClient(system="DND_5E"), ws=ws)
    out = build_tools(ctx)["character_hitdice_spend"]("c", 0)
    ws.sio.push("error", {"message": "No hit dice remaining to spend"})
    assert out["data"]["confirmed"] is False and out["data"]["note"] == E_PENDING
    events = build_tools(ctx)["events_poll"](since=out["data"]["since"])["data"]["events"]
    assert events[-1]["payload"]["detail"]["message"] == "No hit dice remaining to spend"
    assert ws.authenticated


@responses.activate
def test_status_role_is_separate_from_owner_and_features_unknown():
    responses.get(BASE + "/health", json={"status": "ok"})
    responses.get(C, json={"campaign": {"id": CID, "ownerId": "u1", "userRole": "PLAYER", "gameSystem": "DND_5E"}})
    out = build_tools(http_ctx())["campaign_get"]()["data"]
    assert out["role"] == "PLAYER" and out["owner"] == {"id": "u1", "is_me": True}
    assert all(out["features"][key] == "unknown" for key in ("documents", "saved_rolls", "dm_transfer", "hitdice_spend"))
    assert len(responses.calls) == 2


def test_custom_roll_carries_character_name_without_extra_event():
    ctx = make_ctx()
    out = build_tools(ctx)["dice_roll"]("1d8+2", purpose="Spend a Hit Die", character_name="Fighter")
    assert out["data"]["confirmed"] is False
    assert ctx.ws.emitted == [("dice.roll", {"expression": "1d8+2", "secret": False,
                                           "purpose": "Spend a Hit Die", "characterName": "Fighter"})]


@responses.activate
@pytest.mark.parametrize("role,allowed", [("DM", True), ("PLAYER", True), ("SPECTATOR", False), (None, False)])
def test_token_move_role_check_and_unconfirmed_broadcast(role, allowed):
    responses.get(C, json={"campaign": {"userRole": role, "ownerId": "u1"}})
    if allowed:
        responses.put(C + "/maps/m/tokens/t", json={"token": {"id": "t"}})
    out = build_tools(http_ctx())["token_move"]("m", "t", 1, 2)
    assert out["ok"] == allowed
    if allowed:
        assert out["data"]["persisted"] and not out["data"]["broadcast_confirmed"]
    else:
        assert len(responses.calls) == 1


@pytest.mark.parametrize("size", [0, 11, 1.5, True])
def test_token_size_is_integer_1_to_10(size):
    ctx = make_ctx()
    assert not build_tools(ctx)["token_add"]("m", "n", "url", 1, 2, width=size)["ok"]
    assert not ctx.client.calls


@responses.activate
def test_token_add_controlled_by_and_size():
    responses.post(C + "/maps/m/tokens", json={"token": {"id": "t"}})
    result = build_tools(http_ctx())["token_add"]("m", "n", "url", 1, 2, width=10, controlled_by="u2")
    assert result["data"]["persisted"] and not result["data"]["broadcast_confirmed"]
    body = json.loads(responses.calls[0].request.body)
    assert body["size"] == {"width": 10, "height": 1} and body["controlledBy"] == "u2"


@responses.activate
@pytest.mark.parametrize("ws_failure", [False, True])
def test_map_switch_two_steps_no_rest_replay(ws_failure):
    responses.put(C + "/maps/m/set-current", json={"message": "updated"})
    ctx = http_ctx()
    if ws_failure:
        ctx.ensure_ws = Mock(side_effect=RuntimeError("offline"))
    out = build_tools(ctx)["map_switch"]("m")
    assert out["ok"] is not ws_failure
    assert out["data"]["persisted"] is True
    assert out["data"]["broadcast_pending"] is not ws_failure
    assert len(responses.calls) == 1
    if not ws_failure:
        assert ctx.ws.emitted == [("map.change", {"mapId": "m"})]


def test_initiative_refresh_never_returns_old_state_as_fresh():
    ws = authenticated_ws()
    ws.sio.push("initiative.state", {"round": 1})
    ws.wait_for_event = Mock(return_value=None)
    t = build_tools(make_ctx(ws=ws))
    assert t["initiative_read"](refresh=False)["data"]["state"] == {"round": 1}
    out = t["initiative_read"]()["data"]
    assert out["state"] is None and out["stale"]
    assert ws.sio.emitted[-1] == ("initiative.request_state", {})
    ws.wait_for_event.assert_called_once_with("initiative.state", 1)


def test_initiative_request_accepts_synchronous_new_broadcast():
    ws = authenticated_ws()
    original = ws.sio.emit
    def emit(event, payload):
        original(event, payload)
        if event == "initiative.request_state":
            ws.sio.push("initiative.state", {"round": 2})
    ws.sio.emit = emit
    out = build_tools(make_ctx(ws=ws))["initiative_read"]()["data"]
    assert out["state"] == {"round": 2} and not out["stale"]


def test_real_fastmcp_v020_schemas_and_offset_rejection():
    from fastmcp import Client, FastMCP
    from tools import register_all
    async def run():
        mcp = FastMCP("v020-offline")
        ctx = make_ctx()
        register_all(mcp, lambda: ctx)
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert len(tools) == 37 and "saved_roll_get" not in tools
            chat = tools["chat_read"].input_schema["properties"]
            assert set(chat) == {"limit", "cursor"}
            assert tools["token_add"].input_schema["properties"]["width"]["type"] == "integer"
            result = await client.call_tool("chat_read", {"offset": 2}, raise_on_error=False)
            assert result.is_error or result.data["ok"] is False
            assert not ctx.client.calls
    asyncio.run(run())
