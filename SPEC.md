# cozyvtt-mcp — SPEC v2 (v0.3.0, 2026-09-21)

A Python FastMCP stdio bridge for CozyVTT, fixed to one campaign, allowing an AI DM/KP to read and write game state through the API. Dual baselines: CozyVTT **v1.2.2 / v1.4.0**. This specification describes bridge behavior; when upstream v1.4.0 documentation and source disagree, the source takes precedence. The adaptation follows the R1 report, `cozyvtt-v1.4.0-adaptation.md`, sections 3–8 and 11, and the R2 HUMAN DECISIONS.

## 0. Project conventions and compatibility

- Version: 0.3.0; tools: **37**; no redundant `saved_roll_get`.
- Python ≥3.11, FastMCP stdio, requests + python-socketio; dependencies managed with uv.
- One `COZYVTT_CAMPAIGN_ID`; all campaign-specific tools use this campaign. Document asset scopes may explicitly specify campaign_id; CAMPAIGN defaults to the current campaign when omitted.
- The bridge handles API protocols and state transfer only. The caller calculates CoC success levels, SAN loss, healing, and other rules. The character's own gameSystem determines the sheet schema; the campaign system must not be used to rewrite character data.
- Upstream permissions are authoritative: DM, PLAYER, SPECTATOR, platform ADMIN, and owner are distinct concepts. ownerId does not establish DM privileges.

| CozyVTT baseline | Supported scope | Verification boundary |
|---|---|---|
| v1.2.2 | Retain the original 20 tools; read only the latest chat page unless the response supplies nextCursor; degrade new REST features based on precise errors | Old snapshots and existing offline regressions |
| v1.4.0 | 37 tools; Documents / Saved Rolls / DM transfer / Hit Dice / session history; cursor pagination | Source contracts + offline HTTP/WS mocks; no instance connection during this adaptation |

Do not infer the version from an arbitrary 404, an empty list, or a WS timeout. New API capabilities in `campaign_get.features` default to `unknown`. Hit Dice is false outside DND_5E and remains unknown in DND_5E: passing the system gate does not establish that the server implements the event.

### 0.1 Tool metadata and migration

Every parameter has an Annotated/Pydantic Field description, and every tool has
explicit readOnlyHint/destructiveHint/idempotentHint/openWorldHint annotations.
Descriptions explain alternatives, permissions, side effects, and error boundaries.
Use campaign_get, initiative_read, and token_hp_update; the previous names have no
aliases (see CHANGELOG Breaking). These are metadata/name changes only: all parameter
defaults, validation, channels, and result envelopes remain unchanged.

Read-only annotations describe upstream resources. document_read additionally writes
or replaces local binary downloads. Delete/unshare hints describe repeated resource
effects, not identical success responses. Other writes conservatively avoid full-call
idempotency promises. All tools may contact an external CozyVTT service. Shared auth
limits are also disclosed in MCP initialize instructions, avoiding repetition in each
tool. See [accuracy audit and evidence](ForAI/TDQS_Quality_Report.md).

## 1. Authentication and REST client

- `POST /api/auth/login`, JSON email/password/rememberMe=true; store session Cookies in requests.Session. Session requests and login are serialized for thread safety.
- Lazy initialization; after login, start a `GET /api/auth/ping` keepalive every 10 minutes. Initialization failures can be retried after a 180-second cooldown.
- Each REST request receiving 401 may re-log in and retry once. Login cooldown across calls is at least 3 minutes, including failures; Retry-After may extend it. Never loop through re-logins.
- For 429, back off 1/2/4 seconds, at most three retries. Network failures and other errors do not automatically replay writes.
- REST 401 invalidates Ctx role/system caches and existing WS authentication, triggering reconnection with a fresh Cookie. Upstream session revocation does not guarantee disconnection of established WS connections and has no dedicated revocation 401 code.
- `ApiError` preserves status/message/upstream JSON, including validationErrors and codes such as PASSWORD_CHANGE_REQUIRED. Error results may include `data:{status,upstream}`.
- JSON GET/POST/PUT/PATCH/DELETE; multipart POST uses requests `data=` + `files=` to generate the boundary. Every 401/429 retry rewinds file streams to their initial position to avoid uploading an empty file.
- `get_raw` returns text or bytes according to Content-Type and preserves ETag. PDFs never pass through JSON or `_raw`. Text without charset uses UTF-8; 304 returns `{not_modified:true}`.
- Credential environment variables remain `COZYVTT_URL / COZYVTT_EMAIL / COZYVTT_PASSWORD / COZYVTT_CAMPAIGN_ID`; no new configuration or MCP registration changes.

## 2. WebSocket and caches

- The Socket.IO handshake carries a URL-filtered session Cookie. The first WS tool call connects lazily, waits for namespace connect and server connected, sends `authenticate {campaignId}`, then waits for authenticated with the matching campaignId.
- A single background worker handles reconnection; generation isolates callbacks from old connections. Send `initiative.request_state {}` after authentication.
- In-memory ring buffer capacity: 500. Records are `{seq,ts,event,payload,generation}`. Preserve raw payloads; character.updated does not guarantee a character field.
- Subscribe to chat.message, dice.rolled, dice.rolled.secret, map.changed, token.moved, initiative.state, session.started/paused/ended/resumed, character.hp.updated, plus **character.updated, campaign.dm.transferred, roster.updated, dice.historyCleared**.
- Server error → `system.error`, preserving the original detail. Unauthorized, no longer a member, and not a member cancel authenticated and clear caches; ordinary business errors, such as invalid rolls, do not cancel authentication.
- `campaign.dm.transferred` clears Ctx role/system caches and updates the listener's known DM/PLAYER role for the current user. A successful REST transfer also clears caches rather than relying on a best-effort broadcast.
- `Ctx.get_system()` caches lazily; `read_campaign()` updates role and system; token_move refreshes the role before writing. Cache reads do not hold locks during network I/O, avoiding deadlocks with WS callbacks.
- WS writes may only return `{sent:true,confirmed:false,status:'pending',since,...}`. Nearby broadcasts are not correlated ACKs for a call. Read results and errors through events_poll; do not automatically replay writes.
- events_poll returns events/next_seq/latest_seq/high_water_seq/oldest_seq/has_more/gap/cursor_reset/connected/authenticated/last_error, plus role/stale/campaign_cache_stale. Use next_seq for the next since; the high-water mark is not a read cursor. Buffered errors remain readable while disconnected.

## 3. Rate limits and concurrency

- Authentication: 5 failed credential attempts per 15 minutes/IP on the reviewed v1.4.0 source; successful credential requests do not count there. Older accounting is unverified; retain conservative cooldown behavior.

- REST defaults to a global 300/min/IP limit. Handle 429 without spending quota on proactive probes.
- Dice: 30/min/user. dice_roll queues calls serially with a minimum 2.1-second interval.
- Document uploads and direct creation share an additional limit, default 30/min/user (configurable upstream via ASSET_UPLOAD_RATE_LIMIT). The instance determines upload size limits, defaulting to 50 MiB; the bridge does not treat this default as an immutable cap.
- Saved Rolls: 50 per user per campaign. Creation is serialized within this bridge; the server validates expressions and enforces the quota. Cross-client races remain possible; the bridge does not promise a hard database constraint.
- character_update serializes read–merge–write within this process. Upstream has no If-Match or atomic patch, so concurrent browser saves may still overwrite data.

## 4. Tool inventory (37 tools)

Tool bodies return `{ok:bool,data?:any,error?:string}`. FastMCP handles argument schema validation errors. In the paths below, `{id}` is the current campaign_id.

| Tool | Channel | Endpoint/event | Parameters and result summary |
|---|---|---|---|
| `campaign_get` | REST | GET /api/campaigns/{id} + /health (fallback /api/auth/ping) | No parameters; health/campaign/current_map/me/features/feature_evidence/role/owner |
| `chat_send` | WS | chat.message | Nonempty content≤2000, type=DM/PLAYER; pending |
| `chat_read` | REST | GET /api/campaigns/{id}/messages | limit:int=20[1..100], cursor:str/null; messages+pagination unchanged |
| `events_poll` | Buffer | Subscribed events above | since:int=0≥0, limit:int=100[1..500]; pagination/role/stale state |
| `dice_roll` | WS | dice.roll | expression, is_secret=false, purpose='', character_name=null → secret/purpose/characterName; pending |
| `map_list` | REST | GET /api/campaigns/{id}/maps | No parameters; map list |
| `map_switch` | REST+WS | PUT /api/campaigns/{id}/maps/{mapId}/set-current → map.change | map_id; report persisted and broadcast_pending separately |
| `token_add` | REST | POST /api/campaigns/{id}/maps/{mapId}/tokens | map_id/name/image_url/x/y, character_id='', width/height:int1..10=1, layer='token', visible=true, controlled_by=null |
| `token_move` | REST | PUT /api/campaigns/{id}/maps/{mapId}/tokens/{tokenId} | map_id/token_id/x/y → position; reject SPECTATOR/unknown role; persisted with broadcast unconfirmed |
| `token_hp_update` | WS | character.hp.update | character_id/delta; pending |
| `initiative_read` | WS read+buffer | initiative.request_state → initiative.state | refresh:bool=true; only new events count as a refresh, timeout returns state=null/stale=true |
| `initiative_manage` | WS | initiative.add/remove/roll/set/reorder/start/next/end | action, token_id/map_id/value/ordered_token_ids/expression/character_name; roll has a system gate |
| `character_list` | REST | GET /api/campaigns/{id}/characters | roster unchanged |
| `character_get` | REST | GET /api/characters/{characterId} | character_id; full sheet, preserving unknown fields |
| `character_update` | REST | GET + PUT /api/characters/{characterId} | character_id, data as top-level patch; recursively merge sheet data, replace whole arrays |
| `character_create` | REST | POST /api/characters; GET roster; POST /api/characters/{characterId}/assign if needed | name 1..200, data object/null, token_image_url=''; do not create duplicates |
| `character_validate` | REST | GET /api/characters/{characterId}/validate | character_id; upstream result + validation_reliable:false/validation_note |
| `creature_search` | REST | GET /api/campaigns/{id}/creatures | search/source/cr/limit/offset; srd requires DND_5E |
| `token_place_creature` | REST | GET /api/campaigns/{id}/creatures/{creatureId} → POST /api/campaigns/{id}/maps/{mapId}/tokens | creature_id/map_id/x/y; use the template name/image |
| `session_manage` | REST | POST /api/campaigns/{id}/sessions; PUT /api/campaigns/{id}/sessions/{sessionId}/pause or /end | action=start/pause/end, notes:null/string≤2000, save_state=true; last two apply only to end |
| `document_upload` | REST multipart | POST /api/assets/upload | file_path, scope=USER, optional campaign_id/name/description/tags; type fixed to DOCUMENT |
| `document_create` | REST | POST /api/assets/documents | name, format:txt/md, content, scope=USER, optional campaign_id/description |
| `document_list` | REST | GET /api/assets?type=DOCUMENT | Optional scope/campaign_id/search, page=1≥1, limit=50[1..100] |
| `campaign_document_list` | REST | GET /api/campaigns/{id}/documents | No parameters; shared links and native CAMPAIGN documents |
| `document_read` | REST raw+local file | GET /api/assets/documents/{documentId} | document_id, optional etag; text or downloads/ file path, no content on 304 |
| `document_update` | REST | PUT /api/assets/documents/{documentId}/content | document_id/content; replace entire text, not PDFs |
| `document_share` | REST | POST /api/campaigns/{id}/documents | document_id → assetId; DM only, server validates asset sharing permissions |
| `document_unshare` | REST | DELETE /api/campaigns/{id}/documents/{documentId} | document_id; revoke only this link |
| `document_delete` | REST | DELETE /api/assets/{documentId} | document_id; delete the asset and all sharing links |
| `saved_roll_list` | REST | GET /api/campaigns/{id}/macros | No parameters; all fields of the current user's macros |
| `saved_roll_create` | REST | POST /api/campaigns/{id}/macros | name:trim1..60, expression:trim1..200 |
| `saved_roll_update` | REST | PUT /api/campaigns/{id}/macros/{macroId} | macro_id, at least one of name/expression |
| `saved_roll_delete` | REST | DELETE /api/campaigns/{id}/macros/{macroId} | macro_id; current user and campaign only |
| `campaign_transfer_dm` | REST+buffer | PUT /api/campaigns/{id}/dm; listen for campaign.dm.transferred | user_id → userId; caller must be DM/owner/admin, target must already be a member |
| `character_hitdice_spend` | WS | character.hitdice.spend | character_id/index:int≥0; DND_5E only, pending, no rolling/healing |
| `session_list` | REST | GET /api/campaigns/{id}/sessions | No parameters; at most 50 sessions, descending order, including active sessions |
| `session_notes_update` | REST | PUT /api/campaigns/{id}/sessions/{sessionId}/notes | session_id/notes≤2000; DM, empty string clears notes |

### 4.1 System gates and capability status

- `creature_search source=srd`: DND_5E. When source is omitted, non-5e/flexible campaigns use custom.
- `initiative_manage action=roll`: DND_5E / PATHFINDER_2E / SHADOWRUN_6E. CoC7e uses DEX order; use add/set/start. Structural actions are DM-only; reviewed v1.4.0 permits controlled-player rolls for an existing combatant before combat, subject to additional upstream checks. This is not a new bridge permission gate.
- `character_hitdice_spend`: only `require_system(DND_5E)`; flexible campaigns fail. There is no reliable WS capability probe, no additional version rejection, and no fabricated confirmation of spending.
- Other tools are not restricted by game system; the server validates membership, role, and resource permissions.
- `campaign_get.features` retains srd_creature_library/homebrew_creature_library/initiative_roll and adds documents/saved_rolls/dm_transfer/hitdice_spend. The first three new API capabilities are unknown; Hit Dice reports false/unknown according to the system gate. Unknown never establishes support.
- `campaign_get.role` comes from userRole or the current user's membership. owner is `{id,is_me}`, with null where evidence is missing. Never treat the owner as DM by inference.

### 4.2 Breaking chat_read migration

The old call `chat_read(limit=20,offset=20)` is no longer accepted. Start with `chat_read(limit=20)`; if the response contains a non-null `pagination.nextCursor`, call `chat_read(limit=20,cursor=<original value>)` next. Pass messages/pagination through unchanged. Do not construct offset, before, total, or custom cursors; dice rolls do not occupy message pages.

Older instances without nextCursor allow only the latest page. Subsequent cursor calls return: "This instance does not support reliable cursor pagination for history; only the latest page can be read." If the first call already supplies a cursor, inspect the actual response and reject missing nextCursor too; never present a repeated latest page as history. Invalid cursor/limit errors do not trigger fallback retries.

### 4.3 Documents

- scope uses USER/CAMPAIGN/GLOBAL, not personal. USER means personal documents; an admin USER listing automatically adds uploadedBy=current user ID. GLOBAL is readable after login; CAMPAIGN creation requires that campaign's DM role.
- Upload local PDF/txt/md with type=DOCUMENT; join tags as a comma-separated string. The server validates file signatures, UTF-8/control characters, actual size limits, and scope permissions.
- Direct creation: name trim1..200, description trim≤1000; content may be empty, UTF-8≤900KiB, with NUL/C0/DEL forbidden except TAB/LF/FF/CR. Content updates have the same limits. JSON bodies also face the server's 1MiB limit.
- `MAX_DOCUMENT_SIZE_MB` is an upstream environment variable, default 50; admin settings only display it, and changes require restart. The bridge does not impose a fixed 50MiB upload cap.
- Discover private documents shared with the campaign via campaign_document_list, then read content via document_read. Do not first check permissions using `/api/assets/{id}` metadata: that endpoint can reject shared USER files.
- txt/md returns `{mime_type,etag,content}` (upstream commonly serves md as text/plain). PDFs return `{mime_type:'application/pdf',etag,file_path,file_size}`, with raw bytes atomically written to project `downloads/<document_id>.pdf`; other binary content uses `.bin`. Reject filename traversal and a symlinked downloads directory. downloads is excluded from Git.
- Send etag unchanged as If-None-Match; 304 → `{not_modified:true}`. The caller reuses existing results; the bridge does not automatically download again. Local users manage downloaded content; upstream deletion does not remove local copies.
- Editing requires uploader/admin and does not support PDFs. share/unshare requires the current campaign's DM role. shared=false means a native document with no link to revoke. Unshare neither deletes the asset nor revokes access granted by GLOBAL, native CAMPAIGN scope, or other shares.
- Creation/editing/upload passes through upstream JSON such as `{asset}`; lists pass through assets/pagination or documents. Asset deletion may be allowed for uploader/DM/admin depending on scope, as enforced by the server.

### 4.4 Saved Rolls, DM transfer, and Hit Dice

- Saved Rolls are DiceMacro, strictly per-user/per-campaign; any member can manage their own macros. List returns complete Macros. Callers cannot impersonate others with userId; there is no `saved_roll_get` or invented execute endpoint. The server parses expressions when saving; the bridge does not roll locally. To use a macro, pass expression to dice_roll.
- DM transfer and owner reclaim use the same PUT dm; for reclaim, user_id is the owner's own ID. The former DM becomes PLAYER, the new DM must already be a member, and ownerId remains unchanged. Clear caches after REST success; do not simulate transfer with two role updates.
- `character_hitdice_spend {characterId,index}` spends one remaining use. Success normally broadcasts character.updated; failure appears as error → system.error. Dispatch returns only: `Operation sent; the result is not yet confirmed. Read events or state before taking further action; do not repeat the operation.`
- On reviewed v1.4.0, secret rolls are delivered to the roller and DMs; a player roller sees their own secret result. No new live recipient-isolation verification was performed.
- Custom Roll reuses `dice_roll(expression,is_secret,purpose,character_name)`, mapping to characterName; there is no new event. The name is a display label, not an authorization credential.
- Hit Dice spending, rolling, and token_hp_update healing are three separate steps with no transaction or ACK. Older instances may silently ignore the new WS event. A timeout cannot establish support/failure or justify an automatic resend.

### 4.5 Character, map, and session changes

- character_get preserves unknown data fields. character_update allows only name/data/tokenImageUrl at the top level; sheet data must be an object. GET, recursively merge, then PUT; replace whole arrays and preserve explicit null. The server validates against the character's own system. Preserve complete 400 validationErrors; do not drop new fields and retry automatically.
- CoC conditions is an object. spellsAndMythos.cthulhuMythos and skills.cthulhuMythos.currentValue are independent and must not be synchronized implicitly. Pass appearance/notes/spells through unchanged. Keeper notes are visible to campaign members, not private fields.
- Preserve legacy total and new die/maximum/remaining/class fields in DND data.hitDice arrays. Do not rewrite pool structures or use PUT to simulate spend.
- character_validate always includes `validation_reliable:false` with the reason: the v1.4 route discards validation.success=false and may falsely report isValid=true; older-version reliability is unknown. Preserve upstream fields; only the owner can call it, and validation requires a fixed gameSystem.
- Character creation accepts name1..200 and optional complete data. GET roster after creation; skip assign if already assigned, otherwise use the original assign path. Failures include created/assigned/character and warn against duplicate creation.
- token_add width/height are integers1..10; controlled_by → controlledBy. token_move requires a known DM/PLAYER role before writing and rejects SPECTATOR/unknown; the server then checks DM or controlledBy=current user. REST token updates do not guarantee broadcasts.
- map_switch saves through REST, then sends WS map.change. persisted=true/broadcast_pending=true still does not confirm broadcast success. WS dispatch failure returns ok=false with persisted=true/broadcast_pending=false/confirmed=false; do not roll back or replay REST.
- initiative_read(refresh=true) sends request_state and waits at most 2 seconds for a new event. A timeout means unknown state, not proof that combat has not started. refresh=false may return cached state marked stale.
- session_manage end sends saveState/notes; pause/end first resolve activeSession.id. notes is limited to 2000 characters and visible to the whole campaign. Empty end notes do not clear existing content; session_notes_update clears notes when empty after trimming. Lists may include unfinished sessions; recap edits do not broadcast or end the session again.

## 5. Fallback and error rules

| Condition | Bridge error / behavior |
|---|---|
| HTTP404 with upstream message exactly `The requested resource does not exist` | **E-old**: `This CozyVTT instance does not provide this feature; upgrade to a supported version and retry.` |
| Other HTTP404 (Document/Macro/Session/Asset/Campaign not found, etc.) | **E-resource**: `Resource not found or inaccessible to the current account (HTTP 404): <upstream message>` |
| HTTP400/401/403/413/429/500, etc. | Preserve `HTTP <status>: <message>`; data.status/upstream retain validation details and codes |
| Old upload route exists but rejects DOCUMENT with 400 | Pass through the unsupported-type error; do not change scope, re-upload, or misreport a missing route |
| Old chat response lacks nextCursor and a history cursor is requested | Return E-cursor; permit the latest page without a cursor |
| WS write dispatched without a business ACK | pending/confirmed=false; Hit Dice uses the fixed E-pending message; errors surface via events_poll |
| Character created but assignment failed / map saved but broadcast failed | ok=false + recovery data; clearly report completed side effects and prohibit blind write retries |

Do not permanently disable features after a business 404, infer missing capabilities from empty lists, turn PDFs into `_raw` strings, fall back to public uploads, treat WS timeouts as proof that nothing was spent, or modify membership to bypass permission failures.

## 6. Logging, testing, and delivery

- Logs go to project logs/. Do not log uploaded content, PDF bytes, Cookies, passwords, or full authentication requests. Preserve business errors for diagnosis. Do not commit logs or downloads.
- Primary verification: `.venv/bin/python -m pytest`. HTTP uses responses mocks; WS uses FakeSIO. Test fixtures block real TCP, including in the stdio subprocess.
- Cover legacy contracts, 37-tool registration, chat cursor/offset rejection, multipart retries/raw/304, both Documents 404 categories, Saved Rolls, DM transfer, Hit Dice system gate/pending, new WS events/lost membership, session notes, unreliable character validation, token roles/sizes, map step results, and fresh initiative state.
- No live smoke tests, config.yaml/MCP registration changes, or instance access during this adaptation. Existing scripts/smoke.py and smoke_write.py require separate authorization and are outside offline verification.
- One local task commit; no push, tag, or release. Deliver source, README, SPEC, version metadata, and tests together.

## 7. Non-goals

Account/invitation management, rules calculations, concurrent campaigns, map uploads, Personal Notes, a separate Saved Roll get, live deployment, and upstream fixes are outside v0.2.0. Document file uploads are included in this release.
