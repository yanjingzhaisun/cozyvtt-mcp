"""Saved Rolls, DM transfer, Hit Dice, and session history. Fixed campaign; no cross-user macro operations."""
from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

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
    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def saved_roll_list() -> dict:
        """List complete Saved Roll macros owned by the current user in this campaign.
        Use dice_roll with a returned expression to execute it, or saved_roll_update to edit.
        Read-only for campaign members; no individual get is needed and no dice are rolled.
        Repeating the list does not consume macros.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/macros")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def saved_roll_create(
        name: Annotated[
            str, Field(description=(
                'Macro display label, trimmed to 1..60 characters.'
            )),
        ],
        expression: Annotated[
            str, Field(description=(
                'Saved dice expression, trimmed to 1..200 characters; upstream parses it '
                'without rolling.'
            )),
        ],
    ) -> dict:
        """Save a private dice-expression macro for the current user and campaign.
        Use dice_roll to execute an expression; use saved_roll_update to edit an existing
        macro without creating another. Members manage only their own macros. The server
        parses expressions and limits each user to 50 per campaign. Creation is serialized
        only within this process; each accepted call may create another macro. No dice are
        rolled and no campaign-visible message is sent by this tool.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        payload = macro_fields(name, expression)
        ctx = get_ctx()
        with ctx._saved_roll_lock:
            return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/macros", payload)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def saved_roll_update(
        macro_id: Annotated[
            str, Field(description=(
                'Existing Saved Roll ID owned by the current user in this campaign.'
            )),
        ],
        name: Annotated[
            str | None, Field(description=(
                'Replacement display label, trimmed to 1..60 characters; null preserves it. At '
                'least one of name/expression is required.'
            )),
        ] = None,
        expression: Annotated[
            str | None, Field(description=(
                'Replacement expression, trimmed to 1..200 characters and parsed upstream; null '
                'preserves it. At least one field is required.'
            )),
        ] = None,
    ) -> dict:
        """Change the name or expression of your Saved Roll in this campaign.
        Use saved_roll_list to find its ID; use dice_roll to execute it. Supply at least one
        field; null leaves that field unchanged. Upstream validates expressions and enforces
        per-user/per-campaign ownership. Replacing values does not roll dice; repeated writes
        may change update metadata, so full-operation idempotency is not guaranteed.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        payload = macro_fields(name, expression)
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/macros/{segment(macro_id)}", payload)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def saved_roll_delete(
        macro_id: Annotated[
            str, Field(description=(
                'Saved Roll ID owned by the authenticated user in the configured campaign; '
                'other users/campaigns are inaccessible.'
            )),
        ],
    ) -> dict:
        """Delete your Saved Roll from the configured campaign.
        Use saved_roll_list to identify it or saved_roll_update to change it without deletion.
        Any member may delete their own macro; cross-user/cross-campaign access returns 404.
        Destructive to the macro only, not roll history. Repeating leaves it absent but may
        return a not-found error; it does not roll dice.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.delete(f"/api/campaigns/{ctx.campaign_id}/macros/{segment(macro_id)}")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def campaign_transfer_dm(
        user_id: Annotated[
            str, Field(description=(
                'Existing campaign member UUID to become DM; the owner passes their own user '
                'UUID to reclaim the role.'
            )),
        ],
    ) -> dict:
        """Transfer the campaign DM role to an existing member, or reclaim it as owner.
        Use campaign_get to inspect current role/ownership first. Upstream permits DM,
        owner, or admin; owner reclaim uses the owner's own ID. The old DM becomes PLAYER,
        ownerId stays unchanged, and local role/system caches are invalidated after success.
        Do not simulate this with separate role edits. Role changes and broadcasts are not
        guaranteed idempotent; inspect campaign_get before considering another transfer.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        result = ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/dm", {"userId": user_id})
        ctx.invalidate_campaign()  # Refresh even if the best-effort broadcast was missed.
        return result

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def character_hitdice_spend(
        character_id: Annotated[
            str, Field(description=(
                'Campaign-assigned DND_5E sheet ID whose Hit Dice pool is spent; not a map '
                'token ID.'
            )),
        ],
        index: Annotated[
            int, Field(description=(
                'Zero-based integer >=0 selecting an entry in sheet data.hitDice; inspect '
                'character_get for valid pools and remaining uses.'
            )),
        ],
    ) -> dict:
        """Spend one remaining use from a DND_5E character's Hit Dice pool through WS.
        Use character_get to inspect pools first; dice_roll and token_hp_update perform
        rolling and healing separately, with no shared transaction. Upstream requires the
        character owner or DM and campaign assignment. index selects an array entry, not a
        dice count. Repeating spends again. Only the system is gated locally: there is no
        reliable capability probe, and older servers may silently ignore this event.
        Returns sent:true,confirmed:false,status:"pending" inside data: dispatch is not a business ACK.
        Read events_poll for results/system.error and inspect state before further action; never blindly
        resend.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        if not character_id or type(index) is not int or index < 0:
            raise ValueError("character_id is required; index must be an integer >=0")
        ctx = get_ctx()
        require_system(ctx, HITDICE_SYSTEMS, "Hit Dice spending")
        ctx.ensure_ws()
        receipt = ctx.ws.emit("character.hitdice.spend", {"characterId": character_id, "index": index})
        return {**receipt, "confirmed": False, "status": "pending", "note": E_PENDING}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def session_list() -> dict:
        """List this campaign's latest 50 sessions in descending sessionNumber order,
        including active sessions. Use session_manage for lifecycle changes and
        session_notes_update for recaps. Read-only for members; notes are shared with all
        members, and repeated reads do not end or resume sessions.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/sessions")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def session_notes_update(
        session_id: Annotated[
            str, Field(description=(
                'Existing session ID in the configured campaign, obtained with session_list; '
                'need not be the active session.'
            )),
        ],
        notes: Annotated[
            str, Field(description=(
                'Complete replacement recap, at most 2000 characters before trimming; empty or '
                'whitespace-only text clears it upstream.'
            )),
        ],
    ) -> dict:
        """Replace a session's shared recap without another lifecycle transition (DM only).
        Use session_list to find the ID; use session_manage to start/pause/end instead.
        All campaign members can read notes. Empty text after upstream trimming clears
        the recap, unlike empty notes on session_manage end. Does not end the session or
        broadcast a recap event. Repeated writes may alter metadata, so full-operation
        idempotency is not guaranteed.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        if len(notes) > 2000:
            raise ValueError("notes must be at most 2000 characters")
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/sessions/{segment(session_id)}/notes", {"notes": notes})
