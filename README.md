# cozyvtt-mcp

**English** | [中文](README.zh-CN.md)

MCP (Model Context Protocol) bridge for [CozyVTT](https://github.com/CheekyChinchilla/CozyVTT) — the self-hosted, open-source virtual tabletop. It lets an AI agent join a campaign as **DM/KP**: narrate over chat, roll server-authoritative dice on the server, move tokens, switch maps, manage initiative, and settle character sheets.

Built for and tested with [Hermes Agent](https://github.com/NousResearch/hermes-agent), but works with any MCP client (stdio transport).

## Compatibility

| cozyvtt-mcp | CozyVTT | Notes |
|---|---|---|
| **0.2.0** | **v1.2.2 / v1.4.0** | Dual baseline: retain original tools; new REST routes degrade explicitly on old instances. Offline contract tests 176/176; live v1.4.0 smoke (read/write + Documents/Saved Rolls round-trips) passed 2026-09-17. |
| 0.1.1 | v1.2.2 | Previous 20-tool release |

The compatibility table describes supported contracts, not an inferred server version. New feature availability is `unknown` until established; an empty list or a business 404 is not evidence that the route is missing. See [SPEC v2](SPEC.md) for the complete 37-tool contract.

## v0.2.0 changes and migration

- **17 new tools**: Documents (9), Saved Rolls (4), DM transfer/reclaim (1), Hit Dice spend (1), session history/notes (2). `saved_roll_list` already returns complete macros; there is no `saved_roll_get`.
- **Breaking change — `chat_read`**: remove `offset`. Call `chat_read(limit=20)` first, then pass the returned `pagination.nextCursor` as `cursor`. Messages and pagination pass through unchanged. If `nextCursor` is null, stop. Old instances without cursor metadata support only the latest page; history requests return `This instance does not support reliable cursor pagination for history; only the latest page can be read.` instead of repeating it.
- Raw document reads preserve MIME type and ETag. Text returns `{mime_type,etag,content}`; PDFs are saved to project `downloads/<document_id>.pdf` and return `{mime_type,etag,file_path,file_size}`. Pass `etag` for `If-None-Match`; 304 returns `{not_modified:true}` for the caller to reuse existing content. Downloads are Git-ignored; upstream deletion does not remove local copies.
- REST 401 invalidates existing WS authentication and campaign caches. New events include `character.updated`, `campaign.dm.transferred`, `roster.updated`, and `dice.historyCleared`. DM transfer clears role/system caches; losing membership cancels WS authentication.
- `character_validate` always includes `validation_reliable:false`: upstream v1.4.0 can discard validation failures and incorrectly report `isValid:true`; reliability on older servers is unknown.
- Token sizes are integers 1..10; `token_move` rejects spectators. Map switching reports REST persistence separately from WS broadcast dispatch. `initiative_state(refresh=true)` requests fresh state and reports unknown on timeout.

REST route-missing 404 with the exact upstream message `The requested resource does not exist` returns `This CozyVTT instance does not provide this feature; upgrade to a supported version and retry.`. Other 404s return `Resource not found or inaccessible to the current account (HTTP 404): <upstream message>`. Error `data` preserves status and upstream details. Uploading DOCUMENT to an old upload route may return 400; that error is preserved without retrying with a different type or scope.

## Features

37 tools returning `{ok, data?, error?}` for tool-body results. MCP argument validation remains handled by FastMCP:

- **Session/campaign**: `campaign_status` (role and owner reported separately; new capability keys may be `unknown`), `session_manage`, `session_list`, `session_notes_update`, `campaign_transfer_dm` (owner reclaim uses the same tool), `map_list`, `map_switch`
- **Narration**: `chat_send` (DM / PLAYER), `chat_read`
- **Dice**: `dice_roll` (server-side true random; `is_secret=true` (wire field: `secret`) for DM-only rolls, auditable via server logs), `events_poll` (incl. dice history — DICE_ROLL events are *not* in chat history)
- **Tokens/maps**: `token_add`, `token_move`, `token_hp`, `token_place_creature`, `creature_search` (SRD + custom library)
- **Combat**: `initiative_manage` (add / remove / **roll** / **set** / **reorder** / start / next / end), `initiative_state` (note: CoC7e initiative is DEX-ordered, no roll — this is upstream rules behavior, and `roll` is gated accordingly)
- **Characters**: `character_list`, `character_get`, `character_create`, `character_validate`, `character_update` (rules math is done by the agent; the bridge just writes values)
- **Documents**: `document_upload`, `document_create`, `document_list`, `campaign_document_list`, `document_read`, `document_update`, `document_share`, `document_unshare`, `document_delete`
- **Saved Rolls**: `saved_roll_list`, `saved_roll_create`, `saved_roll_update`, `saved_roll_delete` (private to the current user and campaign; 50 macros per user/campaign, server-validated expressions)
- **Hit Dice**: `character_hitdice_spend` (DND_5E only; dispatches one spend without rolling dice or healing)

## System gating

The bridge stays game-system agnostic, but a few capabilities only make sense under a specific rule system. Those are gated against the campaign's `gameSystem` (fetched once, cached; enum: `DND_5E` / `PATHFINDER_2E` / `SHADOWRUN_6E` / `CALL_OF_CTHULHU_7E`):

| Capability | Allowed systems | Why |
|---|---|---|
| `creature_search source=srd` | `DND_5E` | The SRD library is seeded from Open5e — a D&D 5e data source |
| `initiative_manage action=roll` | `DND_5E`, `PATHFINDER_2E`, `SHADOWRUN_6E` | The server derives the initiative dice per system; CoC7e doesn't roll at all (DEX order) |
| `character_hitdice_spend` | `DND_5E` | Only the system gate is enforced locally. No reliable WS capability probe exists; dispatch always remains pending. |

Gated calls return a clear `{ok: false, error}` explaining which systems are allowed, instead of emitting an event the server would ignore or misinterpret. Campaigns with no `gameSystem` set (flexible) fail closed. With no `source` specified, non-5e campaigns search only `custom`; 5e campaigns may search both sources. `campaign_status().features` reports the current campaign's available gated capabilities.

## Architecture

```
MCP client (stdio)
  └─ server.py (FastMCP, lazy init, non-blocking self-check)
      ├─ auth.py        — rememberMe login, 10-min keepalive, 3-min re-login spacing, 429 backoff
      ├─ client.py      — REST wrapper: one 401→re-login→retry, 429 exponential backoff (1/2/4s, ≤3)
      ├─ ws_listener.py — socket.io listener, 500-event ring buffer, one reconnect worker
      └─ tools/         — 37 MCP tools (read/write, documents, campaign additions)
```

Design notes:

- **Dice discipline**: the agent never touches random numbers. All rolls are generated server-side, visible to the table, and persisted. Secret rolls are DM-only but auditable after the session.
- **Rules live outside the bridge**: skill checks, SAN loss, damage — computed by the agent/GM, the bridge only performs authoritative rolls and writes results. The bridge is game-system agnostic.
- `token_move` uses REST PUT; the server checks DM/controlledBy permissions, and the bridge additionally rejects spectators. REST persistence does not imply a `map.changed` broadcast. `map_switch` saves through REST then explicitly dispatches `map.change` through WS; a WS failure preserves the successful REST result and never replays it.

## Result and update contracts

- `dice_roll`, `chat_send`, `token_hp`, `initiative_manage`, and `character_hitdice_spend` return `sent: true`, `confirmed: false`, `status: "pending"`. Read business broadcasts and `system.error` with `events_poll`; do not blindly replay writes. Neither baseline provides correlated business ACKs. Dice may carry `purpose` and `character_name` (wire: `characterName`) for Custom Roll display. Hit Dice spend, rolling, and healing are separate operations, not a transaction; an old server may silently ignore the spend event.
- `events_poll` returns the oldest unread events first. Save `next_seq` for the next `since`; `latest_seq` is its compatibility alias. `high_water_seq` is the buffer high-water mark, not a pagination cursor. Check `gap`, `cursor_reset`, `has_more`, `connected`, and `authenticated`.
- `character_update(character_id, data={"data": {"hp": {"current": 5}}})` recursively merges sheet fields before PUT. Unspecified fields survive; arrays/scalars replace values and `null` is explicit. Top-level fields are `name`, `data`, and `tokenImageUrl`. A process lock serializes updates from this bridge; concurrent browser saves still need upstream optimistic locking.
- `character_create` creates the card, checks the roster, then assigns it only if not confirmed as assigned. If assignment fails, the error includes the created character ID: assign that card in the UI instead of creating another. CoC conditions/Mythos/spells/appearance/notes and DND legacy/new hit-dice fields survive character merges; Keeper notes are not private from campaign members.
- `session_manage` uses REST. Pause/end resolve `campaign.activeSession.id`; start creates a session. End accepts `notes` (≤2000 characters, shared with the campaign) and `save_state=true`. Empty end notes do not clear old notes; use `session_notes_update(session_id,notes="")` to clear. `session_list` includes active sessions among the most recent 50.
- Documents use scope `USER` (personal), `CAMPAIGN`, or `GLOBAL`. `document_list` filters the asset library; `campaign_document_list` discovers shared private documents too. Typed txt/md create/update is limited to 900 KiB UTF-8; file uploads use the instance limit (default 50 MiB). PDF content cannot be edited. Unsharing removes only one link and cannot revoke native/global access; `shared:false` indicates a native campaign document. Deleting removes the asset and all its links.
- WS reconnects use fresh, URL-scoped Cookies and request current initiative state after campaign authentication. Use HTTPS for remote deployments.

## Requirements

- Python ≥ 3.11
- A CozyVTT v1.2.2 or v1.4.0 instance and a campaign where your account has the permissions required by the tools
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
.venv/bin/python -m pytest
```

Read-only smoke test against a live instance:

```bash
COZYVTT_SMOKE=1 COZYVTT_URL=... COZYVTT_EMAIL=... COZYVTT_PASSWORD=... \
  COZYVTT_CAMPAIGN_ID=... .venv/bin/python scripts/smoke.py
```

(`scripts/smoke_write.py` writes chat, a public roll, and a secret roll — run it manually and only on a throwaway campaign. It checks sender and unique purpose; proving that players do not receive secret rolls additionally requires an independent player connection.)

Offline tests block TCP connections and include real FastMCP in-memory and stdio checks; they do not require campaign credentials. Live integration against a v1.4.0 instance was verified 2026-09-17 (read/write smoke plus Documents and Saved Rolls round-trips). The local venv used for verification runs Python 3.12.13; Python 3.13 remains unverified.

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
