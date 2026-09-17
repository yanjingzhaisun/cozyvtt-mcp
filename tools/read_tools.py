"""Read tools: campaign_status / chat_read / events_poll / map_list /
initiative_state / character_list / character_get / character_validate / creature_search"""
from __future__ import annotations

from tools import (E_CURSOR, INITIATIVE_ROLL_SYSTEMS, SYSTEM_DND5E, require_system, wrap)


def register(mcp, get_ctx) -> None:

    @mcp.tool
    @wrap
    def campaign_status() -> dict:
        """Get instance health, campaign status, and current map. GET /health returning 200 indicates reachability."""
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

    @mcp.tool
    @wrap
    def chat_read(limit: int = 20, cursor: str | None = None) -> dict:
        """Read the latest messages; pass pagination.nextCursor unchanged for the next page. v0.2 removes offset.
        Older instances without nextCursor support only the latest page. DICE_ROLL is excluded; use events_poll for dice history."""
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

    @mcp.tool
    @wrap
    def events_poll(since: int = 0, limit: int = 100) -> dict:
        """Read events with seq > since, including player messages, dice rolls, and movement.
        Returns events/next_seq/high_water_seq/gap; use next_seq as the next since.
        latest_seq aliases next_seq; gap indicates evicted events, and cursor_reset indicates a cursor beyond this process."""
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

    @mcp.tool
    @wrap
    def map_list() -> dict:
        """List campaign maps."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/maps")

    @mcp.tool
    @wrap
    def initiative_state(refresh: bool = True) -> dict:
        """Request fresh initiative.state by default; refresh=false reads the cache. A timeout means unknown state."""
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

    @mcp.tool
    @wrap
    def character_list() -> dict:
        """List characters in the campaign roster."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/characters")

    @mcp.tool
    @wrap
    def character_get(character_id: str) -> dict:
        """Read the full character sheet, preserving unknown fields; use the character's own gameSystem.
        Preserve CoC conditions/appearance/spellsAndMythos/notes and DND hitDice unchanged.
        Keeper notes are ordinary data.notes, not private DM fields."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/characters/{character_id}")

    @mcp.tool
    @wrap
    def character_validate(character_id: str) -> dict:
        """Owner only. Preserve upstream results, but always set validation_reliable=false.
        v1.4 ignores validation failures, so isValid=true does not prove validity; older versions have unknown reliability."""
        ctx = get_ctx()
        result = ctx.client.get(f"/api/characters/{character_id}/validate")
        return {**result, "validation_reliable": False,
                "validation_note": "The upstream v1.4 validation route ignores validation.success=false and may incorrectly report isValid=true; reliability on older versions is unknown."}

    @mcp.tool
    @wrap
    def creature_search(search: str = "", source: str = "", cr: str = "",
                        limit: int = 20, offset: int = 0) -> dict:
        """Search Open5e SRD and campaign custom creatures. source: srd|custom.
        source=srd requires DND_5E (Open5e is a D&D 5e source); custom supports all systems."""
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
