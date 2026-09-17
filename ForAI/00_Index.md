---
KEYWORD_EN: CozyVTT,ForAI,Documentation,Index,SourceOfTruth
KEYWORD_CN: 虚拟桌面,开发文档,文档索引,权威来源
---

# Documentation index

**Use the AI entry for reading order, the conventions for development rules, and SPEC for tool contracts.** This index lists documents and their roles; it does not duplicate procedures or API tables.

## Repository documents

| Document | Role |
|---|---|
| [AGENTS.md](../AGENTS.md) | Thin repository entry requiring every AI developer to read the AI entry before starting work |
| [ForAI/00_AI_Readme.md](00_AI_Readme.md) | Project orientation, real repository paths, task routing, and links to mandatory rules |
| [ForAI/00_Index.md](00_Index.md) | Document inventory and source locations; this file |
| [ForAI/Coding_Conventions.md](Coding_Conventions.md) | Six general rules, project-specific invariants, and tool/release/upstream/document maintenance workflows |
| [SPEC.md](../SPEC.md) | Canonical intended bridge behavior, including tool schemas/channels and compatibility/fallback contracts |
| [README.md](../README.md) | English user-facing setup, feature list, migration instructions, compatibility, and troubleshooting |
| [README.zh-CN.md](../README.zh-CN.md) | Chinese translation of the user entry; not a competing source of API contracts |
| [CHANGELOG.md](../CHANGELOG.md) | Keep a Changelog release history, dated changes, and recorded verification scope |

The package version and dependency facts live in [pyproject.toml](../pyproject.toml) and [uv.lock](../uv.lock). Current implementation and [tests/](../tests/) supply observable behavior and regression coverage. See [evidence precedence](Coding_Conventions.md#p13-evidence-and-source-precedence) before resolving a conflict among these sources.

## Local research and authoring sources

These are real paths in the current research workspace, **not bundled repository files**. Another checkout may not have them. If needed evidence is unavailable, record `UNCERTAIN` and the missing source; do not invent its contents or access a live instance without authorization.

| Source | Role and limitations |
|---|---|
| [ForAI authoring guide](/opt/data/projects/forai-docs/ai_doc_guide.md) | UTF-8, optional numeric prefix plus Pascal_Case filenames, keyword front matter, conclusion-first writing, and cross-reference discipline; this repository uses the task-authorized Git workflow, not the guide's host-specific Perforce paths |
| [2026-09-08 Codex review](/opt/data/docs/codex-review-cozyvtt-mcp.md) | Historical failure scenarios behind the invariants; its old line numbers and findings are not a statement of current implementation status |
| [R1 adaptation report](/opt/data/docs/cozyvtt-v1.4.0-adaptation.md) | Evidence-backed v1.2.2→v1.4.0 REST/WS research; section 11 was an implementation proposal, superseded where current SPEC and approved decisions differ |
| [v1.2.2 REST snapshot](/opt/data/cache/cozyvtt-api-v1.2.2/API_DOCUMENTATION.yaml) | Old API documentation for comparison; not proof of the old server implementation |
| [v1.2.2 WS snapshot](/opt/data/cache/cozyvtt-api-v1.2.2/WEBSOCKET_DOCUMENTATION.md) | Old event documentation; check actual handlers before treating examples as wire contracts |
| [v1.4.0 documentation](/opt/data/cache/CozyVTT-v1.4.0/backend/docs/) | New REST/WS documentation to diff before adapting the bridge |
| [v1.4.0 server source](/opt/data/cache/CozyVTT-v1.4.0/backend/src/) | Local implementation evidence: routes, middleware, validators, services, and socket handlers |

Maintain this inventory when adding, removing, or renaming documents, following [P17](Coding_Conventions.md#p17-english-and-document-navigation). Do not copy the full tool table or the R1 report into ForAI.
