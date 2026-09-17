"""Saved Rolls, DM transfer, Hit Dice, and session history. Fixed campaign; no cross-user macro operations."""
from __future__ import annotations

from tools import E_PENDING, HITDICE_SYSTEMS, require_system, wrap
from tools.document_tools import segment


def macro_fields(name=None, expression=None):
    fields = {}
    for key, value, maximum in (("name", name, 60), ("expression", expression, 200)):
        if value is not None:
            value = value.strip()
            if not 1 <= len(value) <= maximum:
                raise ValueError(f"{key} must be 1..{maximum} characters after trimming")
            fields[key] = value
    if not fields:
        raise ValueError("Provide at least one of name/expression")
    return fields


def register(mcp, get_ctx):
    @mcp.tool
    @wrap
    def saved_roll_list() -> dict:
        """List the current user's Saved Rolls in this campaign; returns complete Macros without individual get calls."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/macros")

    @mcp.tool
    @wrap
    def saved_roll_create(name: str, expression: str) -> dict:
        """Save a dice expression; trimmed name: 1..60, expression: 1..200 characters. The server parses it and limits each user to 50 per campaign.
        Creation is serialized in this process. This does not roll dice; pass expression to dice_roll to use it."""
        payload = macro_fields(name, expression)
        ctx = get_ctx()
        with ctx._saved_roll_lock:
            return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/macros", payload)

    @mcp.tool
    @wrap
    def saved_roll_update(macro_id: str, name: str | None = None,
                          expression: str | None = None) -> dict:
        """Update your own macro in this campaign; provide at least one field. Upstream validates the expression."""
        payload = macro_fields(name, expression)
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/macros/{segment(macro_id)}", payload)

    @mcp.tool
    @wrap
    def saved_roll_delete(macro_id: str) -> dict:
        """Delete your own macro in this campaign; cross-user/cross-campaign access returns 404."""
        ctx = get_ctx()
        return ctx.client.delete(f"/api/campaigns/{ctx.campaign_id}/macros/{segment(macro_id)}")

    @mcp.tool
    @wrap
    def campaign_transfer_dm(user_id: str) -> dict:
        """Transfer DM to an existing member (user_id UUID); owners reclaim the role by passing their own ID.
        Caller must be DM/owner/admin; ownerId is unchanged. Upstream authorizes the transaction; ownership does not imply DM."""
        ctx = get_ctx()
        result = ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/dm", {"userId": user_id})
        ctx.invalidate_campaign()  # Refresh even if the best-effort broadcast was missed.
        return result

    @mcp.tool
    @wrap
    def character_hitdice_spend(character_id: str, index: int) -> dict:
        """DND_5E only; send character.hitdice.spend to spend once, without rolling or healing.
        WS has no capability probe/business ACK. Always return pending regardless of other broadcasts; read results with events_poll."""
        if not character_id or type(index) is not int or index < 0:
            raise ValueError("character_id is required; index must be an integer >=0")
        ctx = get_ctx()
        require_system(ctx, HITDICE_SYSTEMS, "Hit Dice spending")
        ctx.ensure_ws()
        receipt = ctx.ws.emit("character.hitdice.spend", {"characterId": character_id, "index": index})
        return {**receipt, "confirmed": False, "status": "pending", "note": E_PENDING}

    @mcp.tool
    @wrap
    def session_list() -> dict:
        """Read this campaign's latest 50 sessions, including active sessions, in descending sessionNumber order; notes are visible to all members."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/sessions")

    @mcp.tool
    @wrap
    def session_notes_update(session_id: str, notes: str) -> dict:
        """DM: update a shared session recap (at most 2000 characters). Empty text after trimming clears it without ending the session again."""
        if len(notes) > 2000:
            raise ValueError("notes must be at most 2000 characters")
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/sessions/{segment(session_id)}/notes", {"notes": notes})
