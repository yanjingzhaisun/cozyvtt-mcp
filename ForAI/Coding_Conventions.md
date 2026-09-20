---
KEYWORD_EN: CozyVTT,MCP,CodingConventions,Invariants,DocumentationMaintenance,Evidence
KEYWORD_CN: 虚拟桌面,模型上下文协议,编码规范,项目不变量,文档维护,证据纪律
---

# Coding conventions and maintenance

**Preserve observable contracts and failure semantics; ship evidence and documentation with the change.** The six general rules below and the 18 project-specific MUST clauses are the maintenance standard. They prevent regressions identified in the [historical review](00_Index.md#local-research-and-authoring-sources) and the v1.4.0 adaptation. Historical reports motivate the rules; current [SPEC.md](../SPEC.md), implementation, and approved task scope determine what is delivered.

Repository links are relative. `file.py:line` labels identify current implementation anchors; re-check and update them when editing the referenced code. Upstream evidence must also name its version or revision.

## Six general rules

### G01. Propagate failures

Failures hidden behind success envelopes cause unsafe retries and false game state. MUST preserve actionable errors and upstream diagnostics. Use the existing `{ok,data?,error?}` wrapper for tool-body failures; FastMCP argument validation remains a separate boundary. A successful HTTP status or socket dispatch alone is not proof of the intended business result. Use `PartialFailure` when a side effect already happened. See [tools/__init__.py:158](../tools/__init__.py#L158) and P01/P11.

### G02. Validate external boundaries

Agents, HTTP responses, file paths, and WS payloads are external inputs. MUST validate required types, bounds, enums, response shape, and destination paths at the relevant boundary before consuming or overwriting state. Keep the `/health` reachability exception separate from business-response validation; HTML or a missing object must not become verified business data. Preserve unknown character fields and diagnostic details rather than silently stripping them. See [client.py:90](../client.py#L90), [character_update](../tools/write_tools.py#L437), and P12.

### G03. Protect network credentials

Unscoped Cookies and truncated secret logs still leak credentials. MUST use the existing auth/client abstractions, bounded timeouts/retries, environment-supplied credentials, and Cookie policy for the actual handshake URL. Never print passwords, Cookie values or fragments, authentication bodies, or document contents. Use HTTPS for remote credentials. Debugging and tests do not authorize network or real-campaign access. See [auth.py:163](../auth.py#L163) and P07/P18.

### G04. Lock shared state and write files atomically

FastMCP tools, keepalive, and WS callbacks can run concurrently. MUST preserve explicit ownership and lock ordering for context publication, Session mutation, WS state, character merges, and dice sends. Do not hold cache locks across network I/O or introduce callback/worker deadlocks. Write downloads to a temporary file in the target directory, atomically replace the destination, and clean up temporary files; a process lock is not a cross-client transaction. See [Ctx](../tools/__init__.py#L47) and [document_read](../tools/document_tools.py#L255).

### G05. Isolate side effects

Imports, probes, and multi-step operations can alter a real campaign before a caller sees a result. MUST keep new initialization and scripts free of implicit network/write work on import, and do not import the existing manual probe scripts for discovery. Respect task authorization for live reads and writes. Never replay successful steps to conceal a failed later step; retain identifiers and completed-step status. See P11/P18.

### G06. Bound resources and clean up

Leaked connections, workers, and unbounded buffers outlive tool calls. MUST retain finite timeouts, bounded retry loops, the bounded ring buffer, and lifecycle cleanup: stop WS/keepalive workers, wait with bounded joins, close Session, and clean partially initialized contexts. Keep blocking cleanup off the MCP event loop. See [server.py:25](../server.py#L25), [Ctx.close:139](../tools/__init__.py#L139), [auth.py:154](../auth.py#L154), and [ws_listener.py:300](../ws_listener.py#L300).

## Project-specific MUST clauses

### P01. WS write receipts

**Why:** the review found dispatch reported as completion while business errors were invisible.

**MUST:** WS writes remain `sent:true,confirmed:false,status:"pending"`. `emit()` is not business completion. Do not fabricate an ACK or associate an unrelated broadcast with a call. Buffer server errors as `system.error`, expose results through `events_poll`, and never automatically replay a WS write. An outer `ok:true` means the dispatch path succeeded, not that the game action completed. Preserve this contract even when an old server might ignore the event.

Anchors: [ws_listener.py:278](../ws_listener.py#L278), [tests/test_contracts.py:235](../tests/test_contracts.py#L235); full contract: [SPEC section 2](../SPEC.md#2-websocket-and-caches).

### P02. Exact wire fields and secret dice

**Why:** sending `isSecret` previously allowed a supposedly secret roll to follow the public path.

**MUST:** map the tool parameter `is_secret` to the wire field **`secret`**, never `isSecret`. Obtain field names, event names, authorization, and response shapes from the pinned upstream source rather than a guessed translation, stale document, or permissive fake. Preserve upstream error/message values verbatim. Offline field/routing checks are not proof that a real PLAYER cannot receive a secret roll; that claim needs an independently observed, authorized player connection.

Anchors: [tools/write_tools.py:72](../tools/write_tools.py#L72), [tests/test_contracts.py:119](../tests/test_contracts.py#L119). Upstream evidence: `/opt/data/cache/CozyVTT-v1.4.0/backend/src/websocket/handlers/dice.ts:20` declares `secret`, and line 27 reads it. Follow P13 for other versions.

### P03. Character read-merge-write

**Why:** a bare partial PUT can replace the entire character `data` object and erase unrelated fields.

**MUST:** `character_update` reads the current card before patching sheet data, recursively merges dictionaries, and replaces arrays/scalars as whole values. `null` is explicit, not deletion. Never send the caller's partial sheet object as the replacement data. Keep top-level allowlisting, reject a missing/non-object current data value, preserve unknown fields, and hold the process character lock around read–merge–write. Use the character's own system; do not invent CoC Mythos synchronization or rewrite legacy Hit Dice fields. State the remaining cross-client lost-update risk honestly.

Anchors: [merge_patch:16](../tools/write_tools.py#L16), [character_update:437](../tools/write_tools.py#L437), [tests/test_contracts.py:25](../tests/test_contracts.py#L25); [SPEC section 4.5](../SPEC.md#45-character-map-and-session-changes).

### P04. Single context initialization and recovery

**Why:** concurrent first calls once created competing contexts; a transient login failure could then be cached forever.

**MUST:** serialize context construction and publication with the initialization lock so concurrent successful callers share one context. Clean up failed partial contexts, retain the monotonic retry deadline, and permit retry after cooldown instead of permanently caching a transient exception. Do not make REST-only tools depend on a healthy WS connection or open a connection just to list tools.

Anchors: [server.py:59](../server.py#L59), [tests/test_server.py:29](../tests/test_server.py#L29), [tests/test_server.py:51](../tests/test_server.py#L51).

### P05. WS generations, fresh Cookies, and invalidation

**Why:** a historical “started” flag, stale Cookie headers, and out-of-order callbacks caused unrecoverable or falsely authenticated connections.

**MUST:** use the single reconnect worker and re-evaluate the latest URL-scoped Cookie for each connection. Treat transport connection and campaign authentication separately: wait for namespace connect and server connected before authenticate, then require the matching campaignId. Protect transitions with the existing state lock/Condition and generation checks; old callbacks cannot restore current authentication. Mark reconnection only after campaign authentication. Preserve REST-401 invalidation, membership-error deauthentication, and DM-transfer role/system cache invalidation; never infer DM from ownerId. Do not describe membership loss as permanently stopping retries unless the code proves that behavior.

Anchors: [ws_listener.py:61](../ws_listener.py#L61), [ws_listener.py:222](../ws_listener.py#L222), [Ctx invalidation:92](../tools/__init__.py#L92), [tests/test_v020.py:310](../tests/test_v020.py#L310).

### P06. Loss-aware event pagination

**Why:** returning the newest `limit` events silently skipped older unread entries still in the buffer.

**MUST:** return the earliest events with `seq > since` first. Advance `next_seq` only through returned events; retain `latest_seq` as its compatibility alias. Keep `high_water_seq` separate from the read cursor. Report `gap` for evicted unread events, `cursor_reset` for a cursor beyond the process sequence, and `has_more` accurately. Disconnects must not hide already buffered errors; generation-aware state reads must not present an old connection's initiative state as fresh.

Anchors: [ws_listener.py:149](../ws_listener.py#L149), [tests/test_contracts.py:198](../tests/test_contracts.py#L198); field contract: [SPEC section 2](../SPEC.md#2-websocket-and-caches).

### P07. Authentication cooldown and bounded retries

**Why:** a lock serializes login attempts but does not stop repeated failures from exhausting the upstream auth quota.

**MUST:** preserve a cross-call re-login cooldown of at least **180 seconds**, including failed attempts. Extend it for Retry-After, use a monotonic deadline, and convert an HTTP-date Retry-After to a duration before applying it. Keep shared Session requests/login/Cookie updates serialized. Each REST call gets at most one 401 re-login/retry; 429 backoff remains bounded. Do not bypass cooldown by creating another AuthManager or ad hoc requests.Session. The bounded retries inside one login attempt are distinct from starting another cross-call login sequence.

Anchors: [auth.py:56](../auth.py#L56), [auth.py:111](../auth.py#L111), [client.py:72](../client.py#L72), [tests/test_contracts.py:155](../tests/test_contracts.py#L155).

### P08. Throttle actual dice sends

**Why:** throttling tool entry and then waiting for authentication allowed queued rolls to burst together after connection recovery.

**MUST:** send rolls through `Ctx.send_dice`; keep its lock across the wait, actual `dice.roll` emit, and monotonic timestamp update. Preserve the **2.1-second** minimum actual-send interval. Do not move timestamp updates to tool entry or release the lock before emit. Do not locally roll dice to work around rate limits; no retry may silently duplicate a roll.

Anchors: [tools/__init__.py:147](../tools/__init__.py#L147), [tools/write_tools.py:72](../tools/write_tools.py#L72), [tests/test_cozyvtt.py](../tests/test_cozyvtt.py).

### P09. Initial state and broadcast coverage

**Why:** late joiners missed active initiative state, and omitted movement/resume events made polling incomplete.

**MUST:** send `initiative.request_state` after each successful campaign authentication. A refresh waits for a new current-generation state; timeout means unknown, not “combat has not started.” Before adding an event, inspect the upstream broadcast sites and payloads, then align `LISTEN_EVENTS` with the bridge's documented supported broadcast set and add a buffer test. Preserve `token.moved`, `session.resumed`, and the v1.4 additions `character.updated`, `campaign.dm.transferred`, `roster.updated`, `dice.historyCleared`. Do not blindly subscribe to every upstream event or require optional payload fields.

Anchors: [ws_listener.py:12](../ws_listener.py#L12), [on_authenticated:84](../ws_listener.py#L84), [initiative_read:177](../tools/read_tools.py#L177), [tests/test_v020.py:501](../tests/test_v020.py#L501). Upstream inventory and sender evidence: R1 report sections 7 and 9, located in the [index](00_Index.md#local-research-and-authoring-sources).

### P10. Precise fallback and system gates

**Why:** a business 404 can hide a resource or permission failure, and an unknown capability is not evidence of support or absence.

**MUST:** use E-old only when HTTP 404 has the exact upstream message `The requested resource does not exist`; use E-resource for other 404s. Preserve status and upstream details; retain upload-type 400 rather than mislabeling it as a missing route. Report unproven capabilities as `unknown`, never guessed true. Gate system-dependent tools through `require_system`, including default creature-source selection. Hit Dice uses only the DND_5E system gate; after dispatch always return pending, with no version probe, timeout-based capability inference, or PUT simulation. Preserve chat_read's opaque cursor and latest-page-only fallback; never restore offset-based history.

Anchors: [wrap:172](../tools/__init__.py#L172), [chat_read:77](../tools/read_tools.py#L77), [Hit Dice:162](../tools/campaign_tools.py#L162), [tests/test_v020.py:126](../tests/test_v020.py#L126). Upstream catch-all evidence: `/opt/data/cache/CozyVTT-v1.4.0/backend/src/server.ts:187`. Canonical messages and limits: [SPEC section 5](../SPEC.md#5-fallback-and-error-rules).

### P11. Channels and partial outcomes

**Why:** invented session WS commands did nothing, character creation did not prove roster assignment, and a saved map did not prove broadcast delivery.

**MUST:** preserve the channels in [SPEC's tool table](../SPEC.md#4-tool-inventory-37-tools). Session lifecycle writes use REST with the resolved activeSession ID. After character creation, inspect the roster and assign when necessary; if assignment fails, return the created ID and explicit partial status, never create again. Map switching persists through REST before WS map.change and reports persistence separately from pending broadcast; WS failure must not replay or roll back the successful REST write. Hit Dice spending, rolling, and healing remain independent operations. DM transfer/reclaim uses its dedicated endpoint and invalidates cached roles; do not simulate it with two role edits.

Anchors: [map_switch:125](../tools/write_tools.py#L125), [character_create:485](../tools/write_tools.py#L485), [session_manage:603](../tools/write_tools.py#L603), [DM transfer:136](../tools/campaign_tools.py#L136).

### P12. Content and validation boundaries

**Why:** binary documents can be corrupted by JSON/text handling, and an upstream `isValid:true` can overstate actual validation.

**MUST:** keep multipart upload separate from JSON and rewind streams on approved retries. Read by Content-Type, preserve ETag/304 semantics, and write binary content atomically to the controlled downloads directory; do not inline PDF bytes or convert them to `_raw` text. Preserve share/unshare/delete distinctions and per-user/per-campaign Saved Roll scope. Keep `character_validate.validation_reliable:false` and its explanation; never advertise it as a reliable validation gate. Do not turn HTTP 200 or an empty result into evidence beyond its actual contract.

Anchors: [client.py:49](../client.py#L49), [document_read:255](../tools/document_tools.py#L255), [character_validate:247](../tools/read_tools.py#L247). Upstream validation-defect evidence: R1 report section 4; detailed bridge contracts: [SPEC section 4.3](../SPEC.md#43-documents) and [4.5](../SPEC.md#45-character-map-and-session-changes).

### P13. Evidence and source precedence

**Why:** upstream documents, changelog promises, implementation behavior, and old plans can disagree; passing a self-invented fake proves little.

**MUST:** every asserted upstream behavior has either a pinned-version **file:line** citation or an actual execution record naming the version/revision, date, request/event, relevant role, observed result, and verification limits. Cite both baselines when asserting a change. A newer source tree cannot prove exactly what an unavailable older implementation did. Mark unresolved behavior **UNCERTAIN**, name the missing evidence, and specify a read-only request or passive observation that could resolve it; do not perform it without authorization. If read-only observation cannot prove a write/permission claim, say so.

Upstream server source is the truth when upstream documentation or changelog disagrees: write down the discrepancy rather than silently repeating the promise. For the bridge, SPEC is the intended contract and current source/tests describe implementation; record and resolve discrepancies within task scope rather than quietly changing the contract. Approved task decisions supersede historical proposals. In particular, R1 section 11 does not authorize reintroducing `saved_roll_get`, a Hit Dice version-probe rejection, or a different 304 contract. Keep mocks tied to cited source and preserve sanitized real evidence without credentials or private document contents.

Source locations and roles are maintained once in [00_Index.md](00_Index.md). Treat old review line numbers as historical and re-locate current code before using them.

### P14. Tool-change documentation workflow

**Why:** stale tool counts, channels, and examples previously disagreed with the actual registered interface.

**MUST:** for a tool addition, removal, signature change, channel change, or result/error change:

1. Read the current SPEC entry and establish the upstream contract under P13 before implementation.
2. Update the owning tool, registration/system-gate surface as needed, and meaningful offline tests for the changed contract and its failure paths.
3. In the **same commit**, synchronize the **SPEC.md tool table and relevant contract sections**, the **README.md feature list and migration examples**, and **tests**. Update README.zh-CN.md when the task permits translation edits; if explicitly excluded, report that translation gap rather than claiming synchronization.
4. Reconcile tool counts in registration/schema checks and user/developer entries, including [00_AI_Readme.md](00_AI_Readme.md). Update the index and cross-links if paths or documents change; do not maintain a second full tool table in ForAI.
5. Run P18 checks and report what was actually verified. A documentation-only task does not authorize incidental source or test changes.

### P15. Release documentation and compatibility

**Why:** a release tag without its change record or evidence leaves users unable to assess compatibility and breaking changes.

**MUST:** write the new version section in **CHANGELOG.md before creating a tag**, following its existing Keep a Changelog format and appropriate Added/Changed/Fixed categories. Include breaking-call migration instructions and evidence limits. For an authorized version bump, synchronize pyproject.toml and the root package metadata in uv.lock without unrelated dependency churn; check README/SPEC consistency and test results before tagging. Do not change versions in an ordinary documentation task.

Compatibility tables may list **only tested baselines**, with offline-contract and live-smoke evidence explicitly distinguished. Source review or a green mock suite is not live verification; a recorded historical run is not a new run performed by the current agent. Never claim broader version/dependency support by extrapolation. Tagging, pushing, and publishing need task authorization; permission to prepare documentation or make a local commit does not grant them.

Canonical records: [CHANGELOG.md](../CHANGELOG.md), [README compatibility](../README.md#compatibility), [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock).

### P16. Upstream release adaptation

**Why:** changing the bridge directly from release notes can miss schema, permission, pagination, and WS lifecycle changes.

**MUST:** before bridge edits, identify both upstream versions/revisions and **diff their `backend/docs` REST and WS documents**. An extracted old snapshot is acceptable if its origin is recorded. Separate documentation additions from proven implementation additions. Then inspect changed routes, middleware, validators, socket handlers, broadcasts, migration/startup behavior, and relevant environment settings in the source. Map every bridge-used contract to the change evidence, record source/document conflicts, define capability/fallback behavior, and add offline fixtures before implementing the adaptation. Missing old source stays UNCERTAIN, not an invented historical behavior.

Use the local snapshot/report locations in the [index](00_Index.md#local-research-and-authoring-sources). Follow P14 for implementation and P15 for compatibility claims. Do not deploy, migrate a database, or exercise a real instance as an implicit part of research.

### P17. English and document navigation

**Why:** tool docstrings are public MCP descriptions, and duplicated or unindexed rules drift across clients and languages.

**MUST:** English is the standard for tool docstrings, bridge-authored results/errors, logs, comments, scripts, and maintained documentation. README.zh-CN.md is the Chinese prose translation. Preserve upstream error/message text and user business data verbatim; do not translate protocol fields. The mandatory **KEYWORD_CN front-matter metadata** is an explicit exception to English-only prose, not permission to add Chinese code messages or a Chinese SPEC copy.

For new ForAI documents, use UTF-8 and `[optional number_]Pascal_Case.md`, retaining uppercase acronyms; number entry documents only. Keep AGENTS.md as the requested thin-entry filename. Write conclusions before steps, use real paths, and cross-reference existing contracts. Entry documents explain how to work; indexes explain what exists. On document add/remove/rename, synchronize the local index and entry links in the same commit. As the final authoring step, summarize the document's scope in a first-byte `---` block containing comma-separated, no-space `KEYWORD_EN` PascalCase/acronym keywords and `KEYWORD_CN` Chinese keywords. Update those keywords whenever scope changes.

The [authoring guide](00_Index.md#local-research-and-authoring-sources) supplies formatting rules. Apply this repository's Git/task workflow, not unrelated host-specific Perforce instructions. Do not retrofit unrelated existing documents during a scoped four-file addition.

### P18. Offline verification and scope

**Why:** a fake that repeats an implementation mistake can pass while real behavior is broken; manual probes can also mutate a campaign unexpectedly.

**MUST:** run the authorized offline suite with `.venv/bin/python -m pytest -q`, retaining responses/FakeSIO isolation and the real-TCP guard in [tests/conftest.py](../tests/conftest.py). Preserve real FastMCP schema/stdio checks, deterministic lifecycle/cooldown regressions, and failure-path evidence. The baseline at this documentation addition is **176 tests**; report the actual count and Python version used, not an assumed environment. Do not install/update dependencies or access a network when the task is offline.

If the task must leave local runtime files untouched, run that exact command in an isolated copy of the current working tree and existing environment; report the isolation explicitly. The stdio test invokes main(), which creates logs, so running it in the original tree is not file-neutral ([tests/test_server.py:81](../tests/test_server.py#L81), [server.py:97](../server.py#L97)).

Before committing, check the requested file scope, internal links/front matter when relevant, `git diff --check`, and the staged diff. Do not touch config.yaml/MCP registration, sibling projects, real campaigns, versions, or release state unless the task calls for it. Keep live smoke separate and explicitly authorized; never import probe scripts as a test shortcut. After committing, inspect the commit's file list and working-tree status, report limitations, and stop at the authorized delivery boundary.
