# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow semver.

## [0.2.1] — 2026-09-17

### Changed

- English is now the standard project language: tool docstrings (MCP tool descriptions), tool return values and error messages, logs, code comments, `scripts/` output, and `SPEC.md` are all in English. Upstream error payloads are still passed through verbatim; no behavior changes.

### Added

- `README.zh-CN.md` — full Chinese translation of the README; both files carry a language switcher (`README.md` stays the English default).

## [0.2.0] — 2026-09-17

CozyVTT v1.4.0 support. Dual baseline: v1.2.2 and v1.4.0. Adaptation driven by a full evidence-checked diff of upstream docs and sources (REST 123 → 153 endpoints, zero removals).

### Added

- **17 new tools (20 → 37)**: Documents (`document_upload/create/list/read/update/share/unshare/delete`, `campaign_document_list`), Saved Rolls (`saved_roll_list/create/update/delete`), `campaign_transfer_dm` (DM handover and owner reclaim), `character_hitdice_spend` (DND_5E-gated), `session_list`, `session_notes_update`.
- `dice_roll` gains `character_name` (Custom Roll attribution); `session_manage end` accepts `notes`.
- `events_poll` buffers `character.updated`, `campaign.dm.transferred`, `roster.updated`, `dice.historyCleared`.
- `campaign_status` reports capability keys (`documents`, `saved_rolls`, `dm_transfer`, `hitdice_spend`), `unknown` when undetectable.
- Explicit degradation on pre-v1.4.0 instances: route-missing 404 vs resource 404 are distinguished and reported with actionable messages.

### Changed

- **Breaking**: `chat_read` uses cursor pagination (`limit`, `cursor`); `offset` removed (upstream ignores it since v1.4.0).
- `map_switch` reports REST persistence separately from WS broadcast dispatch; `token_move` rejects spectators client-side; `token_add` enforces integer size 1..10.
- REST 401 invalidates WS authentication and campaign caches; losing campaign membership drops WS auth state instead of reconnect-looping.
- `character_validate` always returns `validation_reliable: false` (upstream v1.4.0 discards validation failures).

### Verified

- Offline suite 176/176; live smoke against a real v1.4.0 instance (read/write + Documents/Saved Rolls round-trips), 2026-09-17.

## [0.1.1] — 2026-09-08

### Fixed

- Codex review patch: secret dice field name (`isSecret` → `secret`) so secret rolls stopped leaking to the table; `character_update` now merge-patches instead of replacing whole sheets; `session_manage` moved to REST; roster assign flow; WebSocket lifecycle (wait for `connected` before `authenticate`).

## [0.1.0] — 2026-09-05

### Added

- Initial release: MCP bridge for CozyVTT (FastMCP, stdio) — chat narration, server-authoritative dice (open/secret), token/map control, initiative, character sheet read/write, creature library search, session management.
- System gating by campaign `gameSystem` enum (`creature_search source=srd` → DND_5E only; `initiative_manage action=roll` → DND_5E/PATHFINDER_2E/SHADOWRUN_6E; fail-closed on flexible campaigns); `campaign_status().features` reports available gated capabilities.
- Auth discipline: `rememberMe` login, 10-min keepalive, ≥3-min re-login spacing, 429 exponential backoff.
- WS listener with 500-event ring buffer, single reconnect worker, `events_poll` cursor semantics.

[0.2.1]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.1.0
