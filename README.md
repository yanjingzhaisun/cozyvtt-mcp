# cozyvtt-mcp

MCP (Model Context Protocol) bridge for [CozyVTT](https://github.com/CheekyChinchilla/CozyVTT) — the self-hosted, open-source virtual tabletop. It lets an AI agent join a campaign as **DM/KP**: narrate over chat, roll server-authoritative dice on the server, move tokens, switch maps, manage initiative, and settle character sheets.

Built for and tested with [Hermes Agent](https://github.com/NousResearch/hermes-agent), but works with any MCP client (stdio transport).

## Compatibility

| cozyvtt-mcp | CozyVTT | Notes |
|---|---|---|
| 0.1.1 | **v1.2.2** | Critical-bugfix patch following Codex review; still pinned to CozyVTT v1.2.2 |

> **API stability warning.** The CozyVTT author has stated the REST/WS API is *subject to change* — v1.3.0 ships a large amount of changes (see [CozyVTT#32](https://github.com/CheekyChinchilla/CozyVTT/issues/32)). There are no compatibility promises yet. This bridge tracks the upstream changelog and pins its compatibility table per release. If your instance runs a newer CozyVTT, expect to adjust.

## Features

20 tools returning `{ok, data?, error?}` for tool-body results. MCP argument validation remains handled by FastMCP:

- **Session/campaign**: `campaign_status` (incl. per-system feature surface), `session_manage`, `map_list`, `map_switch`
- **Narration**: `chat_send` (DM / PLAYER), `chat_read`
- **Dice**: `dice_roll` (server-side true random; `is_secret=true` (wire field: `secret`) for DM-only rolls, auditable via server logs), `events_poll` (incl. dice history — DICE_ROLL events are *not* in chat history)
- **Tokens/maps**: `token_add`, `token_move`, `token_hp`, `token_place_creature`, `creature_search` (SRD + custom library)
- **Combat**: `initiative_manage` (add / remove / **roll** / **set** / **reorder** / start / next / end), `initiative_state` (note: CoC7e initiative is DEX-ordered, no roll — this is upstream rules behavior, and `roll` is gated accordingly)
- **Characters**: `character_list`, `character_get`, `character_create`, `character_validate`, `character_update` (rules math is done by the agent; the bridge just writes values)

## System gating

The bridge stays game-system agnostic, but a few capabilities only make sense under a specific rule system. Those are gated against the campaign's `gameSystem` (fetched once, cached; enum: `DND_5E` / `PATHFINDER_2E` / `SHADOWRUN_6E` / `CALL_OF_CTHULHU_7E`):

| Capability | Allowed systems | Why |
|---|---|---|
| `creature_search source=srd` | `DND_5E` | The SRD library is seeded from Open5e — a D&D 5e data source |
| `initiative_manage action=roll` | `DND_5E`, `PATHFINDER_2E`, `SHADOWRUN_6E` | The server derives the initiative dice per system; CoC7e doesn't roll at all (DEX order) |

Gated calls return a clear `{ok: false, error}` explaining which systems are allowed, instead of emitting an event the server would ignore or misinterpret. Campaigns with no `gameSystem` set (flexible) fail closed. With no `source` specified, non-5e campaigns search only `custom`; 5e campaigns may search both sources. `campaign_status().features` reports the current campaign's available gated capabilities.

## Architecture

```
MCP client (stdio)
  └─ server.py (FastMCP, lazy init, non-blocking self-check)
      ├─ auth.py        — rememberMe login, 10-min keepalive, 3-min re-login spacing, 429 backoff
      ├─ client.py      — REST wrapper: one 401→re-login→retry, 429 exponential backoff (1/2/4s, ≤3)
      ├─ ws_listener.py — socket.io listener, 500-event ring buffer, one reconnect worker
      └─ tools/         — the 20 MCP tools
```

Design notes:

- **Dice discipline**: the agent never touches random numbers. All rolls are generated server-side, visible to the table, and persisted. Secret rolls are DM-only but auditable after the session.
- **Rules live outside the bridge**: skill checks, SAN loss, damage — computed by the agent/GM, the bridge only performs authoritative rolls and writes results. The bridge is game-system agnostic.
- `token_move` uses the documented REST PUT (server broadcasts `map.changed` over WS), not the undocumented drag-stream WS protocol.

## Result and update contracts

- `dice_roll`, `chat_send`, `token_hp`, and `initiative_manage` return `sent: true`, `confirmed: false`, `status: "pending"`. This confirms dispatch only. Read business broadcasts and `system.error` with `events_poll`; do not blindly replay writes. CozyVTT 1.2.2 does not provide correlated business ACKs. Dice can carry an optional `purpose` string to identify a result.
- `events_poll` returns the oldest unread events first. Save `next_seq` for the next `since`; `latest_seq` is its compatibility alias. `high_water_seq` is the buffer high-water mark, not a pagination cursor. Check `gap`, `cursor_reset`, `has_more`, `connected`, and `authenticated`.
- `character_update(character_id, data={"data": {"hp": {"current": 5}}})` recursively merges sheet fields before PUT. Unspecified fields survive; arrays/scalars replace values and `null` is explicit. Top-level fields are `name`, `data`, and `tokenImageUrl`. A process lock serializes updates from this bridge; concurrent browser saves still need upstream optimistic locking.
- `character_create` creates the card and then assigns it to the roster. If assignment fails, the error includes the created character ID: assign that card in the UI instead of creating another.
- `session_manage` uses REST. Pause/end resolve `campaign.activeSession.id`; start creates a session.
- WS reconnects use fresh, URL-scoped Cookies and request current initiative state after campaign authentication. Use HTTPS for remote deployments.

## Requirements

- Python ≥ 3.11
- A running CozyVTT instance (tested: v1.2.2) and a campaign where your account is **DM**
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install

```bash
git clone https://github.com/yanjingzhaisun/cozyvtt-mcp.git
cd cozyvtt-mcp
uv sync   # or: python -m venv .venv && .venv/bin/pip install fastmcp requests "python-socketio[client]" websocket-client
```

## Configuration

Environment variables (no secrets in the repo):

| Var | Example | Notes |
|---|---|---|
| `COZYVTT_URL` | `http://localhost:8899` | Your instance URL |
| `COZYVTT_EMAIL` | `dm@example.local` | DM account |
| `COZYVTT_PASSWORD` | — | DM password |
| `COZYVTT_CAMPAIGN_ID` | `uuid` | Target campaign |

### Hermes Agent (`config.yaml`)

```yaml
mcp_servers:
  cozyvtt:
    command: /path/to/cozyvtt-mcp/.venv/bin/python
    args: [/path/to/cozyvtt-mcp/server.py]
    env:
      COZYVTT_URL: "http://localhost:8899"
      COZYVTT_EMAIL: "dm@example.local"
      COZYVTT_PASSWORD: "<secret>"
      COZYVTT_CAMPAIGN_ID: "<campaign-uuid>"
```

Restart Hermes after registering (MCP servers are not hot-reloaded).

### Generic MCP client

Any stdio-capable client: command = the venv python, args = `server.py`, env as above.

## Testing

```bash
uv run pytest
```

Read-only smoke test against a live instance:

```bash
COZYVTT_SMOKE=1 COZYVTT_URL=... COZYVTT_EMAIL=... COZYVTT_PASSWORD=... \
  COZYVTT_CAMPAIGN_ID=... .venv/bin/python scripts/smoke.py
```

(`scripts/smoke_write.py` writes chat, a public roll, and a secret roll — run it manually and only on a throwaway campaign. It checks sender and unique purpose; proving that players do not receive secret rolls additionally requires an independent player connection.)

Offline tests block TCP connections and include real FastMCP in-memory and stdio checks; they do not require campaign credentials.

## Troubleshooting

- **Logs**: `logs/cozyvtt-mcp.log` (auth events, WS state, tool calls; never contains passwords)
- **Repeated 401s**: upstream auth rate limit is 5 logins / 15 min / IP. The bridge spaces re-logins ≥3 min; failed attempts also enter cooldown, and `Retry-After` can extend it; requests/background recovery can retry after the window passes
- **`events_poll` empty**: may mean no new events. Inspect `connected`, `authenticated`, `last_error`, and `connection_error`; the single WS worker retries disconnected or rejected connections. Initialization failures can be retried after a 180-second cooldown without restarting.
- **CoC7e initiative doesn't roll dice**: upstream behavior — CoC7e initiative is DEX-ordered, no roll is produced

## License

MIT (see [LICENSE](LICENSE)). CozyVTT itself is AGPLv3 — this project is an independent API client and contains no CozyVTT code.

## Links

- CozyVTT upstream: https://github.com/CheekyChinchilla/CozyVTT
- AI-integration discussion: https://github.com/CheekyChinchilla/CozyVTT/issues/32

## Ecosystem

- [dnd5e-rules](https://github.com/yanjingzhaisun/dnd5e-rules) — deterministic D&D 5e rules calculations (pure functions, SRD 5.1 data under CC-BY-4.0). The rules layer we pair with this bridge: the server rolls the dice, the bridge carries them, this library does the math, the agent narrates.
