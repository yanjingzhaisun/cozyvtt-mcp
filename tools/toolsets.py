"""Named tool selections and a registration-time decorator adapter."""
from __future__ import annotations

PRESETS = {
    "play": frozenset({
        "campaign_get", "chat_read", "chat_send", "dice_roll", "events_poll",
        "map_list", "map_switch", "map_create", "map_delete", "token_add",
        "token_move", "token_hp_update", "token_place_creature", "token_delete",
        "initiative_read", "initiative_manage", "session_list", "session_manage",
        "session_notes_update", "creature_search",
    }),
    "docs": frozenset({
        "document_upload", "document_create", "document_list", "document_read",
        "document_update", "document_share", "document_unshare", "document_delete",
        "campaign_document_list",
    }),
    "roster": frozenset({
        "character_list", "character_get", "character_create", "character_update",
        "character_validate", "character_hitdice_spend", "character_delete",
    }),
    "macros": frozenset({
        "saved_roll_list", "saved_roll_create", "saved_roll_update", "saved_roll_delete",
    }),
    "admin": frozenset({"campaign_transfer_dm"}),
}
PRESETS["all"] = frozenset().union(*PRESETS.values())


def describe_toolsets() -> str:
    """List exact preset membership and the default without initializing a campaign."""
    return "\n".join(
        f"{name} ({len(names)} tools){' [default]' if name == 'all' else ''}: "
        + ", ".join(sorted(names))
        for name, names in PRESETS.items()
    )


def select_tools(value: str | None = None) -> frozenset[str] | None:
    """Resolve comma-separated names; None means all, including future registrations."""
    names = [name.strip() for name in ("all" if value is None else value).split(",")]
    invalid = sorted(set(names) - PRESETS.keys())
    if invalid:
        valid = ", ".join(f"{name} ({len(tools)} tools)" for name, tools in PRESETS.items())
        raise ValueError(f"Unknown or empty toolset name: {invalid!r}. Valid presets: {valid}")
    if "all" in names:
        return None
    return frozenset().union(*(PRESETS[name] for name in names))


class ToolsetRegistration:
    """Forward selected decorators only; preserve every original tool definition."""

    def __init__(self, mcp, selected: frozenset[str]):
        self.mcp = mcp
        self.selected = selected

    def tool(self, *args, **kwargs):
        def decorate(fn):
            name = kwargs.get("name") or fn.__name__
            if name in self.selected:
                return self.mcp.tool(*args, **kwargs)(fn)
            return fn
        return decorate
