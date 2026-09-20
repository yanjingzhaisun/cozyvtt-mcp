"""Write tools: chat_send / dice_roll / map_switch / token_add / token_move /
token_hp_update / initiative_manage / character_update / character_create /
token_place_creature / session_manage"""
from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def chat_send(
        content: Annotated[
            str, Field(description=(
                'Nonblank message text, at most 2000 characters; whitespace is preserved when '
                'sent.'
            )),
        ],
        type: Annotated[
            str, Field(description=(
                'Message category: DM (default narration) or PLAYER. This does not change the '
                'authenticated role.'
            )),
        ] = "DM",
    ) -> dict:
        """Post one campaign chat message as narration or player dialogue.
        Use chat_read to inspect history; use dice_roll for dice, not chat text pretending
        to be a roll. Authenticated campaign membership is required; type is a message
        category, not a role grant. Upstream campaign chat cooldown may reject messages.
        Creates a new message on each accepted send; do not retry blindly.
        Returns sent:true,confirmed:false,status:"pending" inside data: dispatch is not a business ACK.
        Read events_poll for results/system.error and inspect state before further action; never blindly
        resend.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        if type not in {"DM", "PLAYER"}:
            raise ValueError("type must be DM / PLAYER")
        if not content.strip() or len(content) > 2000:
            raise ValueError("content must be a nonempty string of at most 2000 characters")
        ctx = get_ctx()
        ctx.ensure_ws()
        return {**ctx.ws.emit("chat.message", {"content": content, "type": type}),
                "content": content, "type": type}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def dice_roll(
        expression: Annotated[
            str, Field(description=(
                'Server-parsed dice expression, e.g. 1d20+5, 2d6, or 4d6kh3; invalid syntax is '
                'reported asynchronously.'
            )),
        ],
        is_secret: Annotated[
            bool, Field(description=(
                'False publishes to the campaign; true requests secret delivery to the roller '
                'and DMs (wire field secret).'
            )),
        ] = False,
        purpose: Annotated[
            str, Field(description=(
                'Optional broadcast label explaining the roll; empty string omits it, without '
                'changing dice calculation.'
            )),
        ] = "",
        character_name: Annotated[
            str | None, Field(description=(
                'Optional display attribution (wire characterName), not a character ID or '
                'permission credential; null omits it.'
            )),
        ] = None,
    ) -> dict:
        """Roll server-authoritative dice and publish the result to the permitted audience.
        Use saved_roll_list for stored expressions; saving a macro does not execute it.
        Authenticated campaign users may roll. Public rolls broadcast to the campaign;
        v1.4.0 secret rolls go to the roller and DMs, not exclusively to DMs if a player rolls.
        Each send creates a new roll; never retry on missing results. Calls queue with a
        2.1-second minimum between actual sends; upstream allows 30 rolls/minute/user.
        The bridge performs no random generation or rules calculations.
        Returns sent:true,confirmed:false,status:"pending" inside data: dispatch is not a business ACK.
        Read events_poll for results/system.error and inspect state before further action; never blindly
        resend.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        ctx.ensure_ws()
        payload = {"expression": expression, "secret": is_secret}
        if purpose:
            payload["purpose"] = purpose
        if character_name is not None:
            payload["characterName"] = character_name
        return {**ctx.send_dice(payload), "expression": expression, "secret": is_secret,
                "purpose": purpose, "character_name": character_name}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def map_switch(
        map_id: Annotated[
            str, Field(description=(
                'Existing map ID in the configured campaign, obtained from map_list; becomes '
                'the active map for the table.'
            )),
        ],
    ) -> dict:
        """Set the current campaign map through REST, then notify via WS map.change (DM only).
        Use map_list to choose an existing map; token_move changes a token, not the active map.
        REST persistence and WS dispatch are separate: persisted=true does not prove clients
        received the change. A WS failure returns ok=false with data.persisted=true; do not
        replay or roll back the saved change. Broadcast receipt remains sent=true,
        confirmed=false,status="pending"; inspect events_poll for results/errors, not an ACK.
        Repeating may repeat notifications even when the current map is already correct.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def token_add(
        map_id: Annotated[
            str, Field(description=(
                'Destination map ID in this campaign; discover it and its dimensions with '
                'map_list.'
            )),
        ],
        name: Annotated[
            str, Field(description=(
                'Display label for the new token; upstream requires nonempty text.'
            )),
        ],
        image_url: Annotated[
            str, Field(description=(
                'Token artwork URL passed upstream; empty text is accepted as a placeholder by '
                'v1.4.0, with older behavior unverified.'
            )),
        ],
        x: Annotated[
            float, Field(description=(
                'Horizontal position in map units; upstream requires 0 <= x < map width.'
            )),
        ],
        y: Annotated[
            float, Field(description=(
                'Vertical position in map units; upstream requires 0 <= y < map height.'
            )),
        ],
        character_id: Annotated[
            str, Field(description=(
                'Existing sheet ID to link; empty string creates a token without a character '
                'link.'
            )),
        ] = "",
        width: Annotated[
            int, Field(description=(
                'Horizontal token size in map units, integer 1..10; defaults to 1.'
            )),
        ] = 1,
        height: Annotated[
            int, Field(description=(
                'Vertical token size in map units, integer 1..10; defaults to 1.'
            )),
        ] = 1,
        layer: Annotated[
            str, Field(description=(
                'Rendering layer: token (default) or spirit; upstream validates it and applies '
                'visibility rules.'
            )),
        ] = "token",
        visible: Annotated[
            bool, Field(description=(
                'True requests a visible token; false requests a hidden token. Layer and role '
                'may further restrict visibility.'
            )),
        ] = True,
        controlled_by: Annotated[
            str | None, Field(description=(
                'User ID allowed to control the token, subject to upstream checks; null omits '
                'explicit assignment.'
            )),
        ] = None,
    ) -> dict:
        """Create a manually configured token on a campaign map (DM only).
        Use token_place_creature for a library template's name/image, token_move for an
        existing token, and character_create when a new sheet is needed. This creates only
        a token; character_id optionally links an existing sheet. Position uses map units;
        upstream checks map bounds and layer. Width/height are integer sizes in 1..10.
        visible and controlled_by govern display/control subject to server permissions.
        REST returns persisted=true,broadcast_confirmed=false; inspect events_poll or the
        VTT for visibility, since persistence does not confirm a broadcast. Every accepted
        call creates another token, so repeated calls are not idempotent.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def token_move(
        map_id: Annotated[
            str, Field(description=(
                'Map ID containing the existing token, not necessarily the active map.'
            )),
        ],
        token_id: Annotated[
            str, Field(description=(
                'Existing token ID on that map; this is not a character or creature ID.'
            )),
        ],
        x: Annotated[
            float, Field(description=(
                'Absolute horizontal position in map units; upstream requires 0 <= x < map '
                'width.'
            )),
        ],
        y: Annotated[
            float, Field(description=(
                'Absolute vertical position in map units; upstream requires 0 <= y < map '
                'height.'
            )),
        ],
    ) -> dict:
        """Set an existing token's absolute position through REST.
        Use token_add to create a token or map_switch to change the active map. The bridge
        rejects SPECTATOR/unknown roles; upstream requires DM or the controlling player.
        The same coordinates produce the same position, but repeated writes may produce
        notifications. Returns persisted=true,broadcast_confirmed=false; REST does not
        itself emit map.changed. Inspect events_poll or the VTT before assuming others
        saw the move; this result is not a correlated broadcast ACK.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        role = ctx.get_role()
        if role not in {"DM", "PLAYER"}:
            raise ValueError(f"token_move requires DM/PLAYER; current role: {role or 'unknown'}. SPECTATOR cannot move tokens")
        result = ctx.client.put(
            f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens/{token_id}",
            {"position": {"x": x, "y": y}})
        return {**result, "persisted": True, "broadcast_confirmed": False,
                "note": "Persisted; broadcast not confirmed. REST updates do not automatically emit map.changed."}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def token_hp_update(
        character_id: Annotated[
            str, Field(description=(
                'Campaign-assigned character ID whose sheet HP is changed; do not pass a map '
                'token ID.'
            )),
        ],
        delta: Annotated[
            int, Field(description=(
                'Signed integer HP adjustment: positive heals, negative damages, zero requests '
                'no numerical change. Upstream applies system limits.'
            )),
        ],
    ) -> dict:
        """Apply a signed HP delta to a character sheet through WS, not to a token ID.
        Use character_get to inspect HP or character_update for an absolute sheet patch.
        Upstream requires character ownership or DM authority and campaign assignment;
        its system-specific HP rules determine the result. Positive delta heals, negative
        delta damages. Repeating applies the delta again and is not idempotent.
        Returns sent:true,confirmed:false,status:"pending" inside data: dispatch is not a business ACK.
        Read events_poll for results/system.error and inspect state before further action; never blindly
        resend.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        ctx.ensure_ws()
        return {**ctx.ws.emit("character.hp.update", {"characterId": character_id, "delta": delta}),
                "characterId": character_id, "delta": delta}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def initiative_manage(
        action: Annotated[
            str, Field(description=(
                'One of add, remove, roll, set, reorder, start, next, end; case-insensitive. '
                'Determines required companion parameters.'
            )),
        ],
        token_id: Annotated[
            str, Field(description=(
                'Token to add/remove/roll/set; required for those actions. Empty default is '
                'appropriate for reorder/start/next/end.'
            )),
        ] = "",
        map_id: Annotated[
            str, Field(description=(
                'Map containing the token; required for add/roll/set, otherwise unused (empty '
                'default).'
            )),
        ] = "",
        value: Annotated[
            float | None, Field(description=(
                'Manual initiative number required for set; null means unspecified. Other '
                'actions ignore it.'
            )),
        ] = None,
        ordered_token_ids: Annotated[
            list[str] | None, Field(description=(
                'Nonempty ordered list of combatant token IDs for reorder; overrides value '
                'ordering. Null is appropriate for other actions.'
            )),
        ] = None,
        expression: Annotated[
            str, Field(description=(
                'Optional roll fallback for a DM when system derivation has no value; empty '
                'uses the server default. Ignored outside roll.'
            )),
        ] = "",
        character_name: Annotated[
            str, Field(description=(
                'Optional display label passed only for roll; empty omits it and does not '
                'affect permissions.'
            )),
        ] = "",
    ) -> dict:
        """Modify campaign combatants, initiative values, order, or combat lifecycle.
        Use initiative_read for current state and events_poll for results/errors. Structural
        actions are DM-only; v1.4.0 also permits a controlling player to roll for an already
        added token before combat, subject to upstream checks. Older permission behavior
        is unverified. Actions may remove entries, clear state, reroll, or advance turns;
        the combined tool is not idempotent.
        add needs token_id/map_id; remove needs token_id; set needs token_id/map_id/value;
        reorder needs ordered_token_ids; start/next/end need only action. roll needs token_id
        and map_id and is gated to DND_5E/PATHFINDER_2E/SHADOWRUN_6E. The server derives
        system rolls; expression is a DM fallback, not a guaranteed override. CoC7e uses
        DEX ordering: use add/start or set. The dice_roll 2.1-second queue does not wrap
        initiative_manage; do not assume it throttles initiative rolls.
        Returns sent:true,confirmed:false,status:"pending" inside data: dispatch is not a business ACK.
        Read events_poll for results/system.error and inspect state before further action; never blindly
        resend.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def character_update(
        character_id: Annotated[
            str, Field(description=(
                'Existing sheet ID to modify; upstream enforces owner or campaign DM access.'
            )),
        ],
        data: Annotated[
            dict, Field(description=(
                'Nonempty request patch containing only name, data, tokenImageUrl. Sheet '
                'changes go inside data as an object; arrays replace, null is explicit.'
            )),
        ],
    ) -> dict:
        """Patch an existing sheet while preserving unspecified fields (owner or campaign DM).
        Use character_get first; use token_hp_update for a signed HP change and character_create
        for a new sheet. data is a request-field object, e.g. {"data":{"hp":{"current":5}}}.
        Only name/data/tokenImageUrl are allowed at the top level. Nested data dictionaries
        merge recursively after a GET; arrays/scalars replace whole values and null is an
        explicit value, not deletion. The caller calculates rules values using the sheet's
        own gameSystem. Updates are locked only within this process; concurrent browser
        saves may be lost. Reapplying values is stable absent concurrent edits, but upstream
        update side effects are not guaranteed idempotent. REST errors retain diagnostics.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def character_create(
        name: Annotated[
            str, Field(description=(
                'New sheet display name, 1..200 characters; passed without trimming.'
            )),
        ],
        data: Annotated[
            dict | None, Field(description=(
                'Optional initial sheet-field object for the campaign system, not a nested '
                'request patch; null omits it and lets upstream choose defaults.'
            )),
        ] = None,
        token_image_url: Annotated[
            str, Field(description=(
                'Optional artwork URL for the sheet; empty string omits it. This does not '
                'create a map token.'
            )),
        ] = "",
    ) -> dict:
        """Create a character sheet and ensure it appears in the current campaign roster.
        Use character_update for an existing sheet and token_add to place a linked token;
        creation does not place one. Campaign gameSystem determines the initial schema;
        upstream validates membership and sheet data. Do not trust character_validate as a
        save gate. After POST, the bridge reads the roster and assigns only if not found.
        This is not atomic or idempotent: assignment failure returns ok=false with the
        created character ID in data; missing ID means creation status is unknown. Inspect
        the roster/UI and recover the existing sheet instead of creating a duplicate.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def token_place_creature(
        creature_id: Annotated[
            str, Field(description=(
                'Library template ID from creature_search; the template must have a usable '
                'imageUrl, tokenImageUrl, or image.'
            )),
        ],
        map_id: Annotated[
            str, Field(description=(
                'Destination map ID in this campaign; discover dimensions through map_list.'
            )),
        ],
        x: Annotated[
            float, Field(description=(
                'Horizontal position in map units; upstream requires 0 <= x < map width.'
            )),
        ],
        y: Annotated[
            float, Field(description=(
                'Vertical position in map units; upstream requires 0 <= y < map height.'
            )),
        ],
    ) -> dict:
        """Create a visible 1x1 token from a library template's name and image (DM only).
        Use creature_search to obtain a template ID; use token_add for custom size, layer,
        controller, visibility, or a character link. This reads the template and copies
        only name/image into a token: it does not create a sheet or copy creature stats.
        Fails before creation if no usable image exists. Position must be within the map.
        Each accepted call creates another token. Returns upstream REST data without a
        broadcast receipt; use events_poll or the VTT to inspect subsequent visibility.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def session_manage(
        action: Annotated[
            str, Field(description=(
                'Lifecycle transition: start, pause, or end, case-insensitive. Pause/end target '
                'the currently active session.'
            )),
        ],
        notes: Annotated[
            str | None, Field(description=(
                'Shared recap for end, at most 2000 characters; null omits it. Empty end text '
                'does not clear old notes; use session_notes_update.'
            )),
        ] = None,
        save_state: Annotated[
            bool, Field(description=(
                'True (default) requests saving state on end; false skips that snapshot. Must '
                'remain true for start/pause.'
            )),
        ] = True,
    ) -> dict:
        """Start, pause, or end a campaign session through REST (DM only).
        Use session_list to inspect history and session_notes_update to edit or clear a
        recap without another lifecycle transition. start creates a session; pause/end
        resolve campaign.activeSession.id and fail if none exists. Lifecycle changes are
        not guaranteed idempotent: do not repeat start/end to repair notes. notes and
        save_state apply only to end; other actions reject nondefault values. End notes
        are campaign-wide, and empty text does not clear an existing recap. save_state
        requests the upstream end-of-session snapshot; it is not a bridge-side backup.
        Returns upstream REST data; it does not wait for a WS business ACK.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
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
