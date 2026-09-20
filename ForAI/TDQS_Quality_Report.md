---
KEYWORD_EN: CozyVTT,TDQS,ToolMetadata,AccuracyAudit,Annotations,Docker,Verification
KEYWORD_CN: 虚拟桌面,工具元数据,准确性审计,容器,验证
---

# v0.3.0 tool metadata quality report

**All 37 tools now have explicit behavior annotations, and all 90 parameters have
schema descriptions.** The work targets the six TDQS dimensions without changing
business behavior. This is an offline quality assessment, not a new Glama grade.
The release removes exactly three old tool names; their migration mapping appears
only in [CHANGELOG Breaking](../CHANGELOG.md#breaking).

## Delivery and verification

- Purpose and usage: every description starts with its action/resource and names
  alternatives. Complex tools explain conditional parameters and partial outcomes;
  zero-parameter tools remain short. Schema descriptions carry detailed parameter
  semantics rather than duplicating them in long Args sections.
- Behavior: permissions, destructive effects, repeated-call effects, WS pending
  receipts, REST persistence limits, and errors are explicit. Shared authentication
  limits appear in MCP initialization instructions; dice and upload limits appear
  on the relevant tools. Auth is still lazy.
- Schema: Annotated/Field adds descriptions only. A comparison against master found
  all 37 public input schemas identical after removing description fields and
  mapping the three renamed tools. An AST comparison also found tool bodies,
  original parameter types, and defaults unchanged after removing metadata/docstrings.
- Offline suite: Python 3.12.13, FastMCP 4.0.2; 176 tests pass. Existing test assertions
  and upstream contracts were preserved. The test and manual-smoke FakeMCP adapters now accept the decorator's
  annotations keyword; test tool-name references and one test name were renamed.
- `scripts/schema_coverage.py`: real in-memory Client.list_tools, TCP blocked,
  no campaign context initialized, 90/90 parameter descriptions, 37/37 annotation
  sets, and 37/37 descriptions meeting 120/200-character thresholds. Length and
  coverage checks do not prove a subjective score of 4/5 in every dimension.
- Actual `server.py` entrypoint with an entirely empty environment: initialize
  succeeded, tools/list returned 37, and the process remained alive with stdin
  open. Closing stdin ended it cleanly. EOF is normal stdio shutdown; redirecting
  stdin from /dev/null is not a valid test that a server stays alive.
- Dockerfile uses python:3.12-slim and the unchanged stdio entrypoint. The exporter
  selects runtime dependency closure including requested extras from uv.lock and
  emits 78 compatible wheel URLs/hashes on the local Linux/Python 3.12 platform.
  Installed package metadata agrees with that base dependency closure. Pip uses
  --no-index, --no-deps, --only-binary, and --require-hashes, so it cannot resolve or
  fetch an unlocked build dependency. No editable installation or uv bootstrap.
  No Docker build or dependency installation was run.
- glama.json contains only maintainers. pyproject.toml and the root uv.lock package
  both declare 0.3.0. Docker context excludes credentials, environments, runtime
  downloads/logs, tests, and Git metadata by allowlisting build inputs.

## Annotation decisions

All tools have openWorldHint=true: even buffered event access can initialize a
connection to an external campaign. Annotations are hints, not permission checks.

| Tool group | readOnlyHint | destructiveHint | idempotentHint | Reason |
|---|---|---|---|---|
| campaign_get, chat_read, events_poll, map_list, initiative_read, character_list, character_get, character_validate, creature_search, document_list, campaign_document_list, document_read, saved_roll_list, session_list | true | false | true | No upstream resource mutation. Reads may refresh caches/connect; repeated reads need not return identical data. document_read's local file exception is explicitly disclosed. |
| chat_send, dice_roll, token_add, character_create, token_place_creature, document_upload, document_create, document_share, saved_roll_create | false | false | false | Append/create/link operations; retries can duplicate results or repeat effects. Sharing expands access but does not remove data. No guaranteed deduplication assumed. |
| document_unshare, document_delete, saved_roll_delete | false | true | true | Repeated removal leaves the same resource absent. A subsequent call may return 404; equal effects do not imply equal responses. |
| map_switch, token_move, token_hp_update, initiative_manage, character_update, document_update, saved_roll_update, campaign_transfer_dm, character_hitdice_spend, session_manage, session_notes_update | false | true | false | Change existing state, consume resources, replace content, alter permissions, or advance lifecycle. Absolute assignments may stabilize values but notifications, timestamps, concurrency, or mixed actions prevent a full-operation idempotency promise. |

`document_read` is marked read-only as explicitly requested for upstream reads,
but it atomically replaces local binary downloads. Clients that interpret read-only
as forbidding *all local writes* must use the description rather than that hint alone.
The local write behavior was not changed.

## Documentation–implementation discrepancies

These findings were recorded while rewriting, not repaired by changing behavior.
Upstream evidence below is the clean local **CozyVTT v1.4.0**, revision
`8d91e5270aba017305025f82a169386e462c3768`, under
`/opt/data/cache/CozyVTT-v1.4.0/backend/src/`. Evidence is source review, not a live
permission or recipient-isolation test. Earlier-version behavior is not inferred.

| Previous wording or metadata | Actual behavior and evidence | Disposition |
|---|---|---|
| Initiative description said all actions were DM-only. | `websocket/handlers/initiative.ts:162`, `:203`, `:225`: a controlling player may roll for an already added combatant before combat, with further upstream checks. Structural actions remain DM-only. The bridge itself only applies the roll system gate. | Corrected tool description and SPEC; no authorization code or assertions changed. |
| Dice description/READMEs called secret rolls DM-only and broadly described all rolls as table-visible. | `websocket/handlers/dice.ts:113` sends a secret result to the roller, then `:124` to other DMs. A player sees their own secret roll. | Corrected schema, docstring, READMEs, and SPEC. No new live isolation claim. |
| READMEs claimed server-side "true random" and referred to events_poll as dice history. | Bridge `tools/write_tools.py` delegates rolling; it cannot establish an entropy-quality claim. `ws_listener.py:22` has a 500-event in-memory buffer, lost on restart and subject to eviction. | Use server-authoritative and recent buffered events; explicitly distinguish durable history. |
| HP update description promised a campaign broadcast. | `tools/write_tools.py` only calls ws.emit; `ws_listener.py:278` reports pending. Business failure or unsupported behavior can follow dispatch. | Corrected description to pending, with events_poll/state follow-up. No ACK invented. |
| README architecture called the self-check non-blocking. | `server.py:get_ctx` synchronously logs in and reads campaign state on the first business call. Import/initialize/tools/list remain offline. | Corrected both README architecture labels. |
| Auth guidance called the limit 5 logins/15 minutes/IP without distinguishing success. | `routes/auth.ts:49` credentialLimiter sets max=5 and `:55` skips successful requests. | Corrected MCP instructions, READMEs, and SPEC for reviewed v1.4.0; older accounting remains unverified. Cooldown behavior retained. |
| SPEC describes upload/create 30/minute/user as a fixed limit. | `routes/assets.ts:41` defaults to 30 but reads ASSET_UPLOAD_RATE_LIMIT. | Tool descriptions now say default. The SPEC rate paragraph is corrected to configurable default; no rate logic changed. |
| pyproject.toml/AI entry were 0.2.1, while uv.lock root metadata and current SPEC heading still said 0.2.0. | Parent revision metadata disagree. README compatibility rows describe historical tested releases. | Synchronized current metadata/heading to 0.3.0. Kept historical compatibility evidence and explicitly separated current offline checks. |
| Historical 0.2.0 CHANGELOG says loss of membership drops auth "instead of reconnect-looping". | `ws_listener.py:108` clears authentication; the worker at `:222` can continue connection recovery. Membership loss is not a permanent retry stop. | Retained the historical entry and record this limitation here; do not repeat the promise in current descriptions. |
| A uniform pure-read label for document_read would imply no local effects. | `tools/document_tools.py:document_read` writes/replaces downloads/<id>.pdf or .bin atomically. | Retained the requested readOnlyHint=true for upstream resources, disclosed the local exception in the tool, SPEC, and MCP instructions. |

The known character_validate defect was rechecked: `routes/characters.ts:417`
discards the validation result; `:424` explains why the success flag is unreliable.
Descriptions preserve validation_reliable=false; this is an existing acknowledged
upstream defect, not a newly introduced behavior. token_place_creature was also
clarified: it copies name/image only and creates no character sheet or statistics.

## Evidence for other behavior disclosures

- DM map activation/token creation and map bounds/layers: v1.4.0
  `routes/maps.ts:772`, `:848`, `:895`, `:905`; token control: `:1062`.
- Character read/update permissions: `routes/characters.ts:336`, `:510`;
  HP and Hit Dice ownership checks: `websocket/handlers/characters.ts:74`, `:172`.
- Chat cooldown and category validation: `websocket/handlers/chat.ts:40`, `:45`;
  actual dice limit: `websocket/handlers/dice.ts:35` and bridge `Ctx.send_dice`.
- Repeated macro/link removal: `routes/campaigns.ts:1777`, `:1942` uses scoped
  deleteMany and returns 404 once absent. Asset deletion: `routes/assets.ts:538`,
  `:629`. Recap replacement/trim: `routes/campaigns.ts:2012`, `:2024`; session-end
  empty-note behavior: `:2253`.
- Additional scope, validation, and fallback contracts remain in
  [SPEC](../SPEC.md#4-tool-inventory-41-tools) and the existing offline tests.

## Unresolved items and limits

- Glama has not re-evaluated this branch. The six dimensions were reviewed locally;
  the requested >=4/5 cannot be certified by schema coverage or text length alone.
- No Docker daemon is available in the development container. Dockerfile syntax, input
  allowlist, dependency closure, and local equivalent startup were checked there.
  **Separately verified after tagging:** the image builds on a host with Docker (`28.5.2`,
  x86_64, `--build-arg WHEEL_BASE=<mirror>`) and boots with no environment variables —
  `initialize` succeeds, `tools/list` returns 37 tools, every parameter carries a
  description, annotations are emitted in protocol form (`readOnlyHint` etc.), and stdin
  EOF exits 0. The dependency layer was also reproduced in a clean Python 3.12 venv with
  `--no-index --no-deps --only-binary=:all: --require-hashes` from both the canonical CDN
  and a mirror: 78/78 wheels install. Other architectures remain unverified.
- No campaign was contacted. Older-server role details, secret recipients, and
  behavior of optional WS events remain unverified; no version probe or ACK was
  fabricated. Permission/privacy claims need separately authorized live tests to
  go beyond the pinned source evidence.
- Mixed-action and notification-producing writes use conservative idempotentHint=false.
  No cross-client transaction/optimistic-lock guarantee is added. The local-download
  readOnlyHint tradeoff is documented above.
- Maintainer metadata is ready, but ownership recognition and any TDQS score update
  depend on external Glama processing. Nothing was published, pushed, or tagged.
