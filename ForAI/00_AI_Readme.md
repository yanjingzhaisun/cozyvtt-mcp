---
KEYWORD_EN: CozyVTT,MCP,AI,Onboarding,TaskRouting
KEYWORD_CN: 虚拟桌面,模型上下文协议,智能体入口,任务路由
---

# AI developer entry

**Preserve the bridge contract before extending it.** cozyvtt-mcp is a Python FastMCP **stdio** bridge exposing **41 tools** for an AI DM/KP to operate one CozyVTT campaign. The current release is v0.4.0; read [pyproject.toml](../pyproject.toml) for package metadata and [README compatibility](../README.md#compatibility) for tested upstream baselines. The bridge transports server-authoritative rolls and state; it does not implement game-rule calculations.

## Start here

1. Read [Coding_Conventions.md](Coding_Conventions.md) before editing anything. Its general rules and project-specific MUST clauses apply to implementation, reviews, documentation, and release work.
2. Inspect `git status --short` and the task's allowed scope. Preserve unrelated changes; do not infer permission to access a live campaign, change MCP registration, or publish a release.
3. Use the task routing table below. Read the relevant [SPEC.md](../SPEC.md) contract and current implementation together; use [00_Index.md](00_Index.md) to locate supporting documents.
4. Collect evidence before claiming upstream behavior. Follow [P13](Coding_Conventions.md#p13-evidence-and-source-precedence) when documentation, source, or historical reports disagree.
5. Make the scoped change, synchronize its documentation, and perform the required offline checks. Follow [P14](Coding_Conventions.md#p14-tool-change-documentation-workflow) and [P18](Coding_Conventions.md#p18-offline-verification-and-scope) rather than inventing a separate completion checklist.

## Repository map

Paths are relative to the repository root, `/opt/data/projects/cozyvtt-mcp/` in the current workspace. Use relative links when working in another checkout.

| Actual path | Responsibility |
|---|---|
| [server.py](../server.py) | FastMCP registration, locked lazy context initialization, recoverable startup failures, stdio entry point, and shutdown lifespan |
| [auth.py](../auth.py) | Shared requests.Session, login/re-login, cooldown, keepalive, and URL-scoped Cookie selection |
| [client.py](../client.py) | REST transport, bounded 401/429 retries, ApiError details, multipart upload, and raw text/bytes/ETag handling |
| [ws_listener.py](../ws_listener.py) | Socket.IO authentication, one reconnect worker, generation isolation, bounded event buffer, and pending receipts |
| [tools/__init__.py](../tools/__init__.py) | Shared Ctx, system gates, role/system invalidation, actual-send dice throttling, result wrapper, and registration |
| [tools/read_tools.py](../tools/read_tools.py) | Campaign/chat/map/initiative/character/creature reads and event polling |
| [tools/write_tools.py](../tools/write_tools.py) | Chat/dice/map/token/initiative/character/session writes and character merge-patching |
| [tools/document_tools.py](../tools/document_tools.py) | Document scopes, upload/create/read/edit/share/delete, raw downloads, and atomic local output |
| [tools/campaign_tools.py](../tools/campaign_tools.py) | Saved Rolls, DM transfer/reclaim, Hit Dice spending, and session history/notes |
| [tests/](../tests/) | Offline HTTP/WS contracts, authentication and concurrency regressions, real FastMCP schema/stdio checks; TCP guard in [conftest.py](../tests/conftest.py) |
| [scripts/](../scripts/) | Manual smoke/probe utilities; some probes log in or write on import, so do not import or execute them casually |
| [SPEC.md](../SPEC.md) | Canonical bridge contract: tool table, schemas, channels, compatibility, and fallback behavior |
| [CHANGELOG.md](../CHANGELOG.md) | Keep a Changelog release history and recorded verification, not a substitute for current source evidence |
| [README.md](../README.md), [README.zh-CN.md](../README.zh-CN.md) | English user entry, installation/configuration/features/compatibility, and its Chinese translation |
| [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock) | Declared package version/dependencies and reproducible dependency resolution |

`logs/`, `downloads/`, and `.venv/` are local runtime/environment directories, not source or documentation targets. Do not put credentials, downloaded documents, or generated logs into commits.

## Task routing

| Task | Read first, in order | Then inspect / completion rule |
|---|---|---|
| Add or change a tool | [Conventions P01–P03](Coding_Conventions.md#p01-ws-write-receipts), [SPEC tool inventory](../SPEC.md#4-tool-inventory-41-tools), relevant SPEC subsection, [P14](Coding_Conventions.md#p14-tool-change-documentation-workflow) | `tools/__init__.py`, the owning `tools/*.py` module, upstream route/handler, and `tests/test_contracts.py` or `tests/test_v020.py`; update contract, user feature list, and tests in the same commit |
| Adapt to a new CozyVTT release | [P16](Coding_Conventions.md#p16-upstream-release-adaptation), [P13](Coding_Conventions.md#p13-evidence-and-source-precedence), [SPEC fallback rules](../SPEC.md#5-fallback-and-error-rules), [local research sources](00_Index.md#local-research-and-authoring-sources) | Diff both upstream `backend/docs` snapshots before bridge edits, then inspect routes/middleware/validators/WS handlers; record unknowns and test both baselines |
| Change authentication or WS behavior | [Conventions P04–P09](Coding_Conventions.md#p04-single-context-initialization-and-recovery), SPEC sections [1](../SPEC.md#1-authentication-and-rest-client) and [2](../SPEC.md#2-websocket-and-caches) | `server.py`, `auth.py`, `client.py`, `ws_listener.py`, `tools/__init__.py`; run lifecycle, cooldown, pagination, error, and reconnect regressions in `tests/test_server.py` and `tests/test_contracts.py` |
| Prepare a release | [P15](Coding_Conventions.md#p15-release-documentation-and-compatibility), [CHANGELOG.md](../CHANGELOG.md), [README compatibility](../README.md#compatibility), `pyproject.toml`, `uv.lock` | Write the new Keep a Changelog version section before any tag; verify the tested baselines and metadata. Tag/push/release only within the task's explicit authorization |

## MUST checklist

This is a navigation checklist, not a second copy of the rules. Every change MUST satisfy the applicable clauses in [Coding_Conventions.md](Coding_Conventions.md):

- Preserve truthful receipts and exact upstream field names: **P01–P02**.
- Preserve whole character sheets, initialization, connection generations, and event pagination: **P03–P06**.
- Preserve authentication recovery, actual-send throttling, and initial/subsequent state delivery: **P07–P09**.
- Preserve fallback distinctions, partial outcomes, and content/validation boundaries: **P10–P12**.
- Supply evidence and maintain contracts, release records, upstream diffs, and English documentation together: **P13–P17**.
- Use offline regression checks and stay inside the authorized scope: **P18**.

For v0.3.0 metadata decisions, accuracy findings, and offline verification, see [TDQS_Quality_Report.md](TDQS_Quality_Report.md).

Current baseline: 228 offline tests. Tool or behavior changes may legitimately change that count; never delete coverage merely to retain it. Commands, limits, and evidence reporting are defined once in P18.
