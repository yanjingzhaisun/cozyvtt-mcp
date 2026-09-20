"""Read tools: campaign_get / chat_read / events_poll / map_list /
initiative_read / character_list / character_get / character_validate / creature_search"""
from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from tools import (E_CURSOR, INITIATIVE_ROLL_SYSTEMS, SYSTEM_DND5E, require_system, wrap)


def register(mcp, get_ctx) -> None:

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def campaign_get() -> dict:
        """Read campaign identity, role, health, current map, and capability evidence.
        Use this for orientation; use map_list for all maps and session_list for sessions.
        Read-only for campaign members; /health reachability is not feature support, and
        unprobed capabilities remain unknown. Repeating reads does not change game state.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        health = {"reachable": False}
        try:
            h = ctx.client.get("/health")
            health = {"reachable": True, "detail": h if isinstance(h, dict) else {}}
        except Exception:
            # The frontend SPA may handle /health; a 200 response still indicates reachability
            try:
                ctx.client.get("/api/auth/ping")
                health = {"reachable": True, "detail": {"via": "/api/auth/ping"}}
            except Exception as e2:
                health = {"reachable": False, "error": str(e2)}
        camp = ctx.read_campaign()
        current_map = None
        if camp.get("currentMapId"):
            try:
                maps = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/maps").get("maps", [])
                current_map = next((m for m in maps if m.get("id") == camp["currentMapId"]), None)
            except Exception:
                pass
        system = camp.get("gameSystem")
        # Report features allowed by the campaign system; see tool docstrings for individual gates
        features = {
            "srd_creature_library": system == SYSTEM_DND5E,  # Open5e provides 5e content only
            "homebrew_creature_library": True,
            "initiative_roll": system in INITIATIVE_ROLL_SYSTEMS,  # CoC7e sorts by DEX without rolling
            "documents": "unknown",
            "saved_rolls": "unknown",
            "dm_transfer": "unknown",
            "hitdice_spend": "unknown" if system == SYSTEM_DND5E else False,
        }
        return {
            "health": health,
            "campaign": {k: camp.get(k) for k in
                         ("id", "name", "status", "gameSystem", "currentMapId", "description", "ownerId")},
            "features": features,
            "feature_evidence": {"new_api": "New endpoints have not been probed; unknown does not mean supported",
                                 "hitdice_spend": "Only DND_5E passes the system gate; WS has no reliable capability probe"},
            "role": ctx.role_from_campaign(camp),
            "owner": {"id": camp.get("ownerId"),
                      "is_me": camp["ownerId"] == (ctx.auth.user or {}).get("id") if camp.get("ownerId") else None},
            "current_map": current_map and {k: current_map.get(k) for k in ("id", "name", "width", "height")},
            "me": ctx.auth.user,
        }

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def chat_read(
        limit: Annotated[
            int, Field(description=(
                'Page size, 1..100 messages; defaults to 20.'
            )),
        ] = 20,
        cursor: Annotated[
            str | None, Field(description=(
                'Opaque pagination.nextCursor from the previous response; null starts with the '
                'latest page. Do not construct cursors or offsets.'
            )),
        ] = None,
    ) -> dict:
        """Read persisted campaign messages with opaque cursor pagination.
        Use chat_read for narration history; use events_poll for dice results and live events.
        Read-only for campaign members. Start without a cursor, then pass nextCursor unchanged;
        stop at null. Older servers support only the latest page and reject cursor history.
        DICE_ROLL entries are excluded. Repeated reads do not consume messages.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be in 1..100")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("cursor must be a nonempty string or null")
        ctx = get_ctx()
        if cursor is not None and ctx._cursor_supported is False:
            raise ValueError(E_CURSOR)
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        result = ctx.client.get(
            f"/api/campaigns/{ctx.campaign_id}/messages",
            params=params)
        ctx._cursor_supported = isinstance(result.get("pagination"), dict) and "nextCursor" in result["pagination"]
        if cursor is not None and not ctx._cursor_supported:
            raise ValueError(E_CURSOR)
        return result

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def events_poll(
        since: Annotated[
            int, Field(description=(
                'Last next_seq received, integer >=0; 0 starts at the oldest retained event. A '
                'cursor beyond this process resets to 0.'
            )),
        ] = 0,
        limit: Annotated[
            int, Field(description=(
                'Maximum events per page, 1..500; defaults to 100. Continue with next_seq when '
                'has_more is true.'
            )),
        ] = 100,
    ) -> dict:
        """Read buffered campaign events and asynchronous write errors without consuming them.
        Use after WS writes; use chat_read for persisted chat history. May connect WS lazily,
        but does not change game state. Returns earliest seq > since first; advance using
        next_seq (latest_seq is its alias), not high_water_seq. Check gap for eviction from
        the 500-event buffer, cursor_reset after process restart, and has_more for pagination.
        Inspect system.error and relevant state to assess pending writes; broadcasts are not
        correlated ACKs. Buffered events remain available on connection failure, marked stale;
        this is not durable dice history. Repeated reads can include newly arriving events.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        connection_error = None
        try:
            ctx.ensure_ws()
        except RuntimeError as exc:
            connection_error = str(exc)
        result = ctx.ws.poll(since=since, limit=limit)
        result["role"] = result.get("role") or ctx.cached_role()
        result["stale"] = not ctx.ws.authenticated
        result["campaign_cache_stale"] = ctx.cached_role() is None
        if connection_error:
            result["connection_error"] = connection_error
        return result

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def map_list() -> dict:
        """List maps accessible in the configured campaign to choose a map ID and inspect dimensions.
        Use map_switch to activate one; listing does not switch maps or place tokens.
        Read-only for campaign members; repeated calls leave game state unchanged.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/maps")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def initiative_read(
        refresh: Annotated[
            bool, Field(description=(
                'True requests and waits for fresh state; false uses cached state, which may be '
                'absent or stale.'
            )),
        ] = True,
    ) -> dict:
        """Read initiative state without changing turn order or advancing combat.
        Use initiative_manage to modify combat and events_poll for broadcast history.
        Campaign members may read. By default, request a new WS state and wait up to two
        seconds; timeout returns state=null/stale=true, not proof that combat is inactive.
        refresh=false reads the current connection's cache and marks it stale. Repeated
        requests do not change initiative; WS authentication is still required.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        ctx.ensure_ws()
        rec = ctx.ws.latest("initiative.state")
        if not ctx.ws.authenticated:
            return {"state": None, "note": "WS is not authenticated; initiative state is unavailable"}
        if refresh:
            receipt = ctx.ws.emit("initiative.request_state", {})
            rec = ctx.ws.wait_for_event("initiative.state", receipt["since"])
        if not rec:
            return {"state": None, "stale": True, "note": "Initiative state is unknown: no fresh initiative.state received. Read events_poll."}
        return {"state": rec["payload"], "seq": rec["seq"], "ts": rec["ts"], "stale": not refresh}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def character_list() -> dict:
        """List the configured campaign roster to discover character IDs and assignments.
        Use character_get for one full sheet or character_create to add a sheet.
        Read-only for campaign members; no creation or roster changes occur on repeat calls.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/characters")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def character_get(
        character_id: Annotated[
            str, Field(description=(
                'Existing character ID, normally discovered with character_list; the sheet may '
                'use its own gameSystem.'
            )),
        ],
    ) -> dict:
        """Read a full character sheet by ID, preserving unknown fields.
        Use character_list to discover roster IDs and character_update to patch a sheet.
        Upstream allows the owner or campaign members to read; keeper data.notes are not
        private DM fields. Interpret data using the character's own gameSystem, preserving
        CoC fields and DND hitDice structures. Repeated reads do not alter the sheet.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/characters/{character_id}")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def character_validate(
        character_id: Annotated[
            str, Field(description=(
                'Existing character ID owned by the authenticated user; fixed-system validation '
                'is required upstream.'
            )),
        ],
    ) -> dict:
        """Read upstream validation diagnostics for an owned character with a fixed gameSystem.
        Use character_get to inspect fields; do not use this as a reliable save gate.
        v1.4.0 ignores validation failures, so isValid=true does not prove validity;
        older-version reliability is unknown. Always adds validation_reliable=false and
        validation_note. Owner-only upstream read; repeating it does not repair or save data.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        result = ctx.client.get(f"/api/characters/{character_id}/validate")
        return {**result, "validation_reliable": False,
                "validation_note": "The upstream v1.4 validation route ignores validation.success=false and may incorrectly report isValid=true; reliability on older versions is unknown."}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def creature_search(
        search: Annotated[
            str, Field(description=(
                'Name/search filter passed upstream; empty string omits the filter.'
            )),
        ] = "",
        source: Annotated[
            str, Field(description=(
                'srd or custom (case-insensitive); empty chooses both for DND_5E and custom for '
                'other or flexible campaigns.'
            )),
        ] = "",
        cr: Annotated[
            str, Field(description=(
                'Challenge-rating filter passed as text, for example 1/2 or 3; empty omits it. '
                'Applicability is upstream-defined.'
            )),
        ] = "",
        limit: Annotated[
            int, Field(description=(
                'Requested page size, normally 1..100; defaults to 20. Values above 100 are '
                'capped; other validation is upstream.'
            )),
        ] = 20,
        offset: Annotated[
            int, Field(description=(
                'Number of search results to skip, normally >=0; defaults to 0. Forwarded '
                'without local range validation.'
            )),
        ] = 0,
    ) -> dict:
        """Search library templates for later placement with token_place_creature.
        Use character_list for campaign character sheets; this does not create either a
        character or a token. Read-only for campaign members. Explicit srd requires DND_5E;
        custom works across systems. Empty source searches both in DND_5E and custom otherwise.
        Search, challenge rating and offset are forwarded; limit is capped at 100 without
        local lower-bound validation. Repeating a search leaves the library unchanged.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        params = {"limit": min(limit, 100), "offset": offset}
        if search:
            params["search"] = search
        source = source.strip().lower()
        if source not in {"", "srd", "custom"}:
            raise ValueError("source must be srd, custom, or an empty string")
        if source == "srd":
            require_system(ctx, (SYSTEM_DND5E,), "SRD creature library (Open5e provides D&D 5e content)")
        elif not source and ctx.get_system() != SYSTEM_DND5E:
            source = "custom"
        if source:
            params["source"] = source
        if cr:
            params["cr"] = cr
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/creatures", params=params)
