# cozyvtt-mcp

MCP (Model Context Protocol) bridge for [CozyVTT](https://github.com/CheekyChinchilla/CozyVTT) — the self-hosted, open-source virtual tabletop. It lets an AI agent join a campaign as **DM/KP**: narrate over chat, roll server-authoritative dice on the server, move tokens, switch maps, manage initiative, and settle character sheets.

Built for and tested with [Hermes Agent](https://github.com/NousResearch/hermes-agent), but works with any MCP client (stdio transport).

## Compatibility

| cozyvtt-mcp | CozyVTT | Notes |
|---|---|---|
| 0.1.0 | **v1.2.2** | Developed and smoke-tested against v1.2.2 |

> **API stability warning.** The CozyVTT author has stated the REST/WS API is *subject to change* — v1.3.0 ships a large amount of changes (see [CozyVTT#32](https://github.com/CheekyChinchilla/CozyVTT/issues/32)). There are no compatibility promises yet. This bridge tracks the upstream changelog and pins its compatibility table per release. If your instance runs a newer CozyVTT, expect to adjust.

## Features

18 tools, all returning a uniform `{ok, data, error}` shape (exceptions never escape the MCP layer):

- **Session/campaign**: `campaign_status`, `session_manage`, `map_list`, `map_switch`
- **Narration**: `chat_send` (DM / PLAYER), `chat_read`
- **Dice**: `dice_roll` (server-side true random; `isSecret=true` for DM-only rolls, auditable via server logs), `events_poll` (incl. dice history — DICE_ROLL events are *not* in chat history)
- **Tokens/maps**: `token_add`, `token_move`, `token_hp`, `token_place_creature`, `creature_search` (SRD + custom library)
- **Combat**: `initiative_manage`, `initiative_state` (note: CoC7e initiative is DEX-ordered, no roll — this is upstream rules behavior)
- **Characters**: `character_list`, `character_get`, `character_update` (rules math is done by the agent; the bridge just writes values)

## Architecture

```
MCP client (stdio)
  └─ server.py (FastMCP, lazy init, non-blocking self-check)
      ├─ auth.py        — rememberMe login, 10-min keepalive, 3-min re-login spacing, 429 backoff
      ├─ client.py      — REST wrapper: one 401→re-login→retry, 429 exponential backoff (1/2/4s, ≤3)
      ├─ ws_listener.py — socket.io listener, 500-event ring buffer, auto-reconnect
      └─ tools/         — the 18 MCP tools
```

Design notes:

- **Dice discipline**: the agent never touches random numbers. All rolls are generated server-side, visible to the table, and persisted. Secret rolls are DM-only but auditable after the session.
- **Rules live outside the bridge**: skill checks, SAN loss, damage — computed by the agent/GM, the bridge only performs authoritative rolls and writes results. The bridge is game-system agnostic.
- `token_move` uses the documented REST PUT (server broadcasts `map.changed` over WS), not the undocumented drag-stream WS protocol.

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

(`scripts/smoke_write.py` performs write operations — run it manually and only on a throwaway campaign.)

## Troubleshooting

- **Logs**: `logs/cozyvtt-mcp.log` (auth events, WS state, tool calls; never contains passwords)
- **Repeated 401s**: upstream auth rate limit is 5 logins / 15 min / IP. The bridge spaces re-logins ≥3 min; if a session dies inside the spacing window, it recovers automatically once the window passes
- **`events_poll` empty**: WS not connected. Tool calls auto-`ensure_ws()`; check the log for `WS connected / campaign authenticated`
- **CoC7e initiative doesn't roll dice**: upstream behavior — CoC7e initiative is DEX-ordered, no roll is produced

## License

MIT (see [LICENSE](LICENSE)). CozyVTT itself is AGPLv3 — this project is an independent API client and contains no CozyVTT code.

## Links

- CozyVTT upstream: https://github.com/CheekyChinchilla/CozyVTT
- AI-integration discussion: https://github.com/CheekyChinchilla/CozyVTT/issues/32
