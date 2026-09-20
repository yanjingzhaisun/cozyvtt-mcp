# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow semver.

## [0.4.0] — 2026-09-21

### Added

- Optional `--toolsets` / `COZYVTT_MCP_TOOLSETS` presets: `play`, `docs`, `roster`, `macros`, `admin`, and default `all`. Comma-separated selections use union semantics; filtering happens at registration. `--list-toolsets` prints memberships and counts offline.
- Four additive REST tools: `map_create`, `map_delete`, `token_delete`, and `character_delete` (37 → 41 tools). No existing tool name, description, input/output schema, or annotation changed; existing calls need no migration.
- Offline preset/transport regressions and a portable tool-budget measurement script with interactive stdio probes. README tables record measured JSON bytes and estimated token savings.

### Verified

- 228 offline tests; stdio counts: default 41, play 20, docs 9. All 37 existing tool definitions compared unchanged. CozyVTT v1.4.0 endpoint contracts reviewed from the pinned API snapshot and source; no live campaign access or new live compatibility claim.
- The `Dockerfile` was replayed against the real base image without a Docker daemon: the `python:3.12-slim` linux/amd64 layers were pulled from a registry mirror with each layer's sha256 checked, extracted into a chroot, the seven `COPY` paths replayed, the `uv.lock` closure installed with `pip --require-hashes` using the image's own Python 3.12.14, and `ENTRYPOINT` then run with an empty environment. It answers `initialize`, lists 41 tools (20 with `--toolsets play`), and exits 0 on stdin EOF. No OCI image was assembled and no container runtime was exercised.

## [0.3.0] — 2026-09-21

### Added

- JSON Schema descriptions for all 90 parameters across 37 tools, using Annotated and Pydantic Field without changing defaults or validation.
- Explicit MCP ToolAnnotations on every tool, with conservative side-effect and idempotency hints.
- Offline `scripts/schema_coverage.py` for parameter/annotation coverage and description-length checks through a real FastMCP client.
- Python 3.12 stdio Dockerfile, .dockerignore, and a lock-only runtime-wheel exporter; discovery requires neither credentials nor a live instance.
- `glama.json` with the maintainer claim and no additional fields.

### Changed

- Rewrote tool descriptions for purpose, alternatives, permissions, side effects, pending receipts, and errors; documented shared authentication limits in MCP initialization instructions.
- Renamed three tools to the object/action convention; synchronized references and root lock metadata. Tool count, defaults, business logic, routes, events, and result envelopes are unchanged.
- Recorded documentation/implementation discrepancies and annotation tradeoffs in `ForAI/TDQS_Quality_Report.md`, including secret-roll recipients and initiative permissions.

### Breaking

- `campaign_status` → `campaign_get`.
- `initiative_state` → `initiative_read`.
- `token_hp` → `token_hp_update`.
- Update client tool selections to the new names; no aliases are registered. Older release prose below uses current names for consistency.

### Verified

- 176 offline tests; parameter descriptions 90/90 and annotations 37/37. The `Dockerfile` was also built and booted on x86_64 (`docker 28.5.2`): the 220 MB image answers `initialize` and lists 37 tools with no environment variables set and no CozyVTT instance reachable. No live campaign access; no new Glama score is claimed.

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
- `campaign_get` reports capability keys (`documents`, `saved_rolls`, `dm_transfer`, `hitdice_spend`), `unknown` when undetectable.
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
- System gating by campaign `gameSystem` enum (`creature_search source=srd` → DND_5E only; `initiative_manage action=roll` → DND_5E/PATHFINDER_2E/SHADOWRUN_6E; fail-closed on flexible campaigns); `campaign_get().features` reports available gated capabilities.
- Auth discipline: `rememberMe` login, 10-min keepalive, ≥3-min re-login spacing, 429 exponential backoff.
- WS listener with 500-event ring buffer, single reconnect worker, `events_poll` cursor semantics.

[0.4.0]: https://github.com/yanjingzhaisun/cozyvtt-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/yanjingzhaisun/cozyvtt-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/yanjingzhaisun/cozyvtt-mcp/releases/tag/v0.1.0
