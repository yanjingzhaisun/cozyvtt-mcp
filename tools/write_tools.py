"""Write tools: chat_send / dice_roll / map_switch / token_add / token_move /
token_hp / initiative_manage / character_update / character_create /
token_place_creature / session_manage"""
from __future__ import annotations

from copy import deepcopy

from tools import INITIATIVE_ROLL_SYSTEMS, PartialFailure, require_system, wrap


def merge_patch(current: dict, patch: dict) -> dict:
    """Merge dictionaries recursively; replace lists and scalars. null is an explicit value, not a deletion."""
    result = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def register(mcp, get_ctx) -> None:

    @mcp.tool
    @wrap
    def chat_send(content: str, type: str = "DM") -> dict:
        """Send DM narration or NPC dialogue. type accepts only DM / PLAYER (upstream validation, verified 2026-09-04)."""
        if type not in {"DM", "PLAYER"}:
            raise ValueError("type must be DM / PLAYER")
        if not content.strip() or len(content) > 2000:
            raise ValueError("content must be a nonempty string of at most 2000 characters")
        ctx = get_ctx()
        ctx.ensure_ws()
        return {**ctx.ws.emit("chat.message", {"content": content, "type": type}),
                "content": content, "type": type}

    @mcp.tool
    @wrap
    def dice_roll(expression: str, is_secret: bool = False, purpose: str = "",
                  character_name: str | None = None) -> dict:
        """Roll server-authoritative dice, e.g. 1d20+5 / 2d6 / 4d6kh3. is_secret=true requests a DM-only roll.
        Optional purpose is passed through upstream to identify the roll in broadcasts.
        Calls queue with a minimum 2.1s interval (WS limit: 30/min)."""
        ctx = get_ctx()
        ctx.ensure_ws()
        payload = {"expression": expression, "secret": is_secret}
        if purpose:
            payload["purpose"] = purpose
        if character_name is not None:
            payload["characterName"] = character_name
        return {**ctx.send_dice(payload), "expression": expression, "secret": is_secret,
                "purpose": purpose, "character_name": character_name}

    @mcp.tool
    @wrap
    def map_switch(map_id: str) -> dict:
        """Save the current map through REST, then send WS map.change to notify the campaign; dispatch does not confirm broadcast success."""
        ctx = get_ctx()
        result = ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/set-current")
        data = {"persisted": True, "response": result, "broadcast_pending": False,
                "confirmed": False}
        try:
            ctx.ensure_ws()
            receipt = ctx.ws.emit("map.change", {"mapId": map_id})
        except Exception as exc:
            raise PartialFailure(f"Current map saved, but broadcast dispatch failed; do not replay the REST write: {exc}", data) from exc
        return {**data, "broadcast_pending": True, "broadcast": receipt}

    @mcp.tool
    @wrap
    def token_add(map_id: str, name: str, image_url: str, x: float, y: float,
                  character_id: str = "", width: int = 1, height: int = 1,
                  layer: str = "token", visible: bool = True,
                  controlled_by: str | None = None) -> dict:
        """Place a token on the map (DM only)."""
        if any(type(n) is not int or not 1 <= n <= 10 for n in (width, height)):
            raise ValueError("width/height must be integers in 1..10")
        ctx = get_ctx()
        payload = {
            "name": name, "imageUrl": image_url,
            "position": {"x": x, "y": y},
            "size": {"width": width, "height": height},
            "layer": layer, "visible": visible,
        }
        if character_id:
            payload["characterId"] = character_id
        if controlled_by is not None:
            payload["controlledBy"] = controlled_by
        result = ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens", payload)
        return {**result, "persisted": True, "broadcast_confirmed": False}

    @mcp.tool
    @wrap
    def token_move(map_id: str, token_id: str, x: float, y: float) -> dict:
        """Move a token through REST; reject SPECTATOR. The server checks DM/controlledBy; persistence does not confirm a broadcast."""
        ctx = get_ctx()
        role = ctx.get_role()
        if role not in {"DM", "PLAYER"}:
            raise ValueError(f"token_move requires DM/PLAYER; current role: {role or 'unknown'}. SPECTATOR cannot move tokens")
        result = ctx.client.put(
            f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens/{token_id}",
            {"position": {"x": x, "y": y}})
        return {**result, "persisted": True, "broadcast_confirmed": False,
                "note": "Persisted; broadcast not confirmed. REST updates do not automatically emit map.changed."}

    @mcp.tool
    @wrap
    def token_hp(character_id: str, delta: int) -> dict:
        """Update token HP (positive delta heals, negative damages); broadcast character.hp.updated to the campaign."""
        ctx = get_ctx()
        ctx.ensure_ws()
        return {**ctx.ws.emit("character.hp.update", {"characterId": character_id, "delta": delta}),
                "characterId": character_id, "delta": delta}

    @mcp.tool
    @wrap
    def initiative_manage(action: str, token_id: str = "", map_id: str = "",
                          value: float | None = None,
                          ordered_token_ids: list[str] | None = None,
                          expression: str = "", character_name: str = "") -> dict:
        """Manage initiative (all actions are DM-only).
        action: add / remove / roll / set / reorder / start / next / end。
        - add: token_id + map_id
        - remove: token_id
        - roll: token_id + map_id (optional expression; the server derives the roll from the system:
          5e uses Dexterity + sheet initiativeBonus, PF2e uses usedStat, and SR6 uses its initiative dice.
          The supplied expression is a fallback only. CoC7e uses DEX order without rolling:
          use add then start, or set a value manually.)
        - set: token_id + map_id + value (set initiative manually; the server reorders by value)
        - reorder: ordered_token_ids (custom turn order, overriding value order)
        Read state through initiative_state / events_poll."""
        ctx = get_ctx()
        action = action.strip().lower()
        allowed = {"add", "remove", "roll", "set", "reorder", "start", "next", "end"}
        if action not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        payload = {}
        if action == "add":
            if not token_id or not map_id:
                raise ValueError("add requires token_id and map_id")
            payload = {"tokenId": token_id, "mapId": map_id}
        elif action == "remove":
            if not token_id:
                raise ValueError("remove requires token_id")
            payload = {"tokenId": token_id}
        elif action == "roll":
            require_system(ctx, INITIATIVE_ROLL_SYSTEMS,
                           "initiative.roll (CoC7e orders initiative by DEX without rolling)")
            if not token_id or not map_id:
                raise ValueError("roll requires token_id and map_id")
            payload = {"tokenId": token_id, "mapId": map_id}
            if expression:
                payload["expression"] = expression
            if character_name:
                payload["characterName"] = character_name
        elif action == "set":
            if not token_id or not map_id or value is None:
                raise ValueError("set requires token_id, map_id, and value")
            payload = {"tokenId": token_id, "mapId": map_id, "value": value}
        elif action == "reorder":
            if not ordered_token_ids:
                raise ValueError("reorder requires ordered_token_ids (a list of token IDs)")
            payload = {"orderedTokenIds": ordered_token_ids}
        ctx.ensure_ws()
        return {**ctx.ws.emit(f"initiative.{action}", payload),
                "action": action, "payload": payload}

    @mcp.tool
    @wrap
    def character_update(character_id: str, data: dict) -> dict:
        """Patch a character sheet (the caller calculates SAN/HP/Luck/MP/spell slots; the bridge does no rules math).
        data contains request fields, e.g. {"data": {"hp": {"current": 5}}}. Sheet data is merged recursively,
        preserving unspecified fields. Lists replace whole values; null is explicit. Top-level fields: name/data/tokenImageUrl.
        """
        allowed = {"name", "data", "tokenImageUrl"}
        if not data or set(data) - allowed:
            raise ValueError("Provide name/data/tokenImageUrl fields; put sheet patches inside the data object")
        if "data" in data and not isinstance(data["data"], dict):
            raise ValueError("The sheet data patch must be an object; clearing the entire sheet is not allowed")
        ctx = get_ctx()
        path = f"/api/characters/{character_id}"
        with ctx._character_lock:
            payload = deepcopy(data)
            if "data" in payload:
                current = ctx.client.get(path)
                character = current.get("character") if isinstance(current, dict) else None
                if not isinstance(character, dict) or not isinstance(character.get("data"), dict):
                    raise ValueError("Upstream character response is missing the data object; refusing to overwrite")
                payload["data"] = merge_patch(character["data"], payload["data"])
            # TODO(#2): 1.2.2 has no ETag/If-Match or atomic JSON patch; this lock protects only this process
            # during character_update. Concurrent browser/other-client saves still need upstream optimistic locking.
            return ctx.client.put(path, payload)

    @mcp.tool
    @wrap
    def character_create(name: str, data: dict | None = None,
                         token_image_url: str = "") -> dict:
        """Create a character sheet and add it to the current campaign. gameSystem inherits the campaign system.
        The server validates data against that system's Zod schema and returns 400 on failure.
        data is a sheet-field dict (system-specific: upstream dnd5e.ts or callOfCthulhu7e.ts).
        Do not treat character_validate isValid=true as proof of validity (upstream defect)."""
        if not name or len(name) > 200:
            raise ValueError("name must be 1..200 characters")
        ctx = get_ctx()
        payload = {"name": name, "campaignId": ctx.campaign_id}
        if data is not None:
            payload["data"] = data
        if token_image_url:
            payload["tokenImageUrl"] = token_image_url
        created = ctx.client.post("/api/characters", payload)
        character = created.get("character") if isinstance(created, dict) else None
        if not isinstance(character, dict) or not character.get("id"):
            raise PartialFailure("Character creation response is missing the character ID; creation status is unknown. Check upstream; do not create again",
                                 {"created": None, "response": created})
        # v1.4 creation automatically adds to the roster; character.campaignId alone does not prove assignment.
        # Keep the original assign path if an older instance does not include this card in the roster.
        try:
            roster = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/characters").get("roster", [])
            assigned = any(c.get("id") == character["id"] for member in roster
                           for c in member.get("characters", []))
        except Exception:
            assigned = False
        if assigned:
            return {**created, "assigned": True}
        try:
            ctx.client.post(f"/api/characters/{character['id']}/assign", {"campaignId": ctx.campaign_id})
        except Exception as exc:
            raise PartialFailure(
                f"Character {character['id']} was created but roster assignment failed; assign it in the UI, do not create again: {exc}",
                {"created": True, "assigned": False, "character": character}) from exc
        return {**created, "assigned": True}

    @mcp.tool
    @wrap
    def token_place_creature(creature_id: str, map_id: str, x: float, y: float) -> dict:
        """Place a creature on the map: read its library template, then place a token using its name/image."""
        ctx = get_ctx()
        tpl = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/creatures/{creature_id}")
        creature = tpl.get("creature", tpl)
        name = creature.get("name", "creature")
        image_url = (creature.get("imageUrl") or creature.get("tokenImageUrl")
                     or creature.get("image") or "")
        if not image_url:
            raise ValueError(f"Creature template {creature_id} has no usable image field; cannot place a token")
        payload = {
            "name": name, "imageUrl": image_url,
            "position": {"x": x, "y": y},
            "size": {"width": 1, "height": 1},
            "layer": "token", "visible": True,
        }
        return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens", payload)

    @mcp.tool
    @wrap
    def session_manage(action: str, notes: str | None = None, save_state: bool = True) -> dict:
        """Manage sessions (DM only). notes/save_state apply only to end; notes are a campaign-wide recap.
        Empty end notes do not clear existing notes; use session_notes_update to clear them."""
        ctx = get_ctx()
        action = action.strip().lower()
        allowed = {"start", "pause", "end"}
        if action not in allowed:
            raise ValueError(f"action must be one of {sorted(allowed)}")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
            raise ValueError("notes must be a string of at most 2000 characters")
        if action != "end" and (notes is not None or save_state is not True):
            raise ValueError("notes/save_state apply only to end")
        path = f"/api/campaigns/{ctx.campaign_id}"
        if action == "start":
            return ctx.client.post(f"{path}/sessions")
        campaign = ctx.client.get(path).get("campaign", {})
        active = campaign.get("activeSession")
        if not isinstance(active, dict) or not active.get("id"):
            raise ValueError("The current campaign has no active session to pause or end")
        payload = {}
        if action == "end":
            payload["saveState"] = save_state
            if notes is not None:
                payload["notes"] = notes
        return ctx.client.put(f"{path}/sessions/{active['id']}/{action}", payload)
