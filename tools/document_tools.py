"""Documents: separate JSON/multipart metadata from raw content reads; upstream enforces permissions."""
from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from tools import wrap

Scope = Literal["USER", "CAMPAIGN", "GLOBAL"]
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


def segment(value: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError("Resource ID must not be empty or a relative path")
    return quote(value, safe="")


def scope_fields(ctx, scope, campaign_id):
    if scope not in {None, "USER", "CAMPAIGN", "GLOBAL"}:
        raise ValueError("scope must be USER/CAMPAIGN/GLOBAL")
    fields = {} if scope is None else {"scope": scope}
    if scope == "CAMPAIGN":
        fields["campaignId"] = campaign_id or ctx.campaign_id
    elif campaign_id is not None:
        fields["campaignId"] = campaign_id
    return fields


def validate_content(content):
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content.encode("utf-8")) > 900 * 1024:
        raise ValueError("Typed document creation/editing is limited to 900 KiB UTF-8; use document_upload for larger files")
    if any((ord(c) < 32 and c not in "\t\n\f\r") or ord(c) == 127 for c in content):
        raise ValueError("Document contains disallowed control characters")


def register(mcp, get_ctx):
    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def document_upload(
        file_path: Annotated[
            str, Field(description=(
                'Existing .pdf/.txt/.md path on the MCP server filesystem; ~ is expanded. '
                'Container users must mount the file.'
            )),
        ],
        scope: Annotated[
            Scope, Field(description=(
                'Visibility/ownership scope: USER (default personal), CAMPAIGN, or GLOBAL; '
                'upstream enforces creation rights. CAMPAIGN uses campaign_id.'
            )),
        ] = "USER",
        campaign_id: Annotated[
            str | None, Field(description=(
                'Explicit campaign ID; null uses the configured campaign when scope=CAMPAIGN, '
                'otherwise omits the field. If supplied, it is forwarded for any scope.'
            )),
        ] = None,
        name: Annotated[
            str | None, Field(description=(
                'Optional asset display name; null omits it so upstream uses its upload '
                'default.'
            )),
        ] = None,
        description: Annotated[
            str | None, Field(description=(
                'Optional asset metadata text; null omits it. Upstream validates upload '
                'metadata.'
            )),
        ] = None,
        tags: Annotated[
            list[str] | None, Field(description=(
                'Optional tag strings joined by commas for multipart upload; null omits tags, '
                'while [] sends an empty field.'
            )),
        ] = None,
    ) -> dict:
        """Upload an existing local PDF/txt/md file as a document asset using multipart REST.
        Use document_create for inline text, or document_share to link an existing asset.
        Reads the MCP server/container filesystem; client-local paths must be mounted.
        Creates an asset on each accepted call, so retries may duplicate it. Scope controls
        access: CAMPAIGN requires that campaign's DM; other scope rights are upstream-enforced.
        The server checks signatures and its size cap (default 50 MiB), with a shared
        upload/create limit of 30/minute/user by default. A 400 is preserved without fallback upload.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        path = Path(file_path).expanduser()
        mime = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown"}.get(path.suffix.lower())
        if mime is None or not path.is_file():
            raise ValueError("file_path must reference an existing PDF/txt/md file")
        ctx = get_ctx()
        fields = {"type": "DOCUMENT", **scope_fields(ctx, scope, campaign_id)}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        if tags is not None:
            fields["tags"] = ",".join(tags)
        with path.open("rb") as stream:
            return ctx.client.post_multipart("/api/assets/upload", fields,
                                             {"file": (path.name, stream, mime)})

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def document_create(
        name: Annotated[
            str, Field(description=(
                'Asset title, trimmed to 1..200 characters.'
            )),
        ],
        format: Annotated[
            Literal['txt', 'md'], Field(description=(
                'Text representation: txt or md; PDFs require document_upload.'
            )),
        ],
        content: Annotated[
            str, Field(description=(
                'Complete text, including empty text, at most 900 KiB UTF-8; rejects C0/DEL '
                'controls except TAB/LF/FF/CR.'
            )),
        ],
        scope: Annotated[
            Scope, Field(description=(
                'Visibility/ownership scope: USER (default personal), CAMPAIGN, or GLOBAL; '
                'upstream enforces creation rights. CAMPAIGN uses campaign_id.'
            )),
        ] = "USER",
        campaign_id: Annotated[
            str | None, Field(description=(
                'Explicit campaign ID; null uses the configured campaign when scope=CAMPAIGN, '
                'otherwise omits the field. If supplied, it is forwarded for any scope.'
            )),
        ] = None,
        description: Annotated[
            str | None, Field(description=(
                'Optional metadata, trimmed to at most 1000 characters; null omits it.'
            )),
        ] = None,
    ) -> dict:
        """Create a new txt/md document from inline content through REST.
        Use document_upload for PDF or larger local files, and document_update to replace
        existing text. Name and description are trimmed; content preserves whitespace.
        USER is personal, CAMPAIGN requires its DM, and GLOBAL rights are enforced upstream.
        Each accepted call creates a separate asset. Content is capped at 900 KiB UTF-8;
        upstream JSON limits also apply. Upload/create share a default 30 requests/minute/user limit.
        Creation does not separately link a personal asset; use document_share for that.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        name = name.strip()
        if not 1 <= len(name) <= 200 or format not in {"txt", "md"}:
            raise ValueError("name must be 1..200 characters; format must be txt/md")
        validate_content(content)
        ctx = get_ctx()
        payload = {"name": name, "format": format, "content": content,
                   **scope_fields(ctx, scope, campaign_id)}
        if description is not None:
            if len(description.strip()) > 1000:
                raise ValueError("description must be at most 1000 characters")
            payload["description"] = description.strip()
        return ctx.client.post("/api/assets/documents", payload)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def document_list(
        scope: Annotated[
            Scope | None, Field(description=(
                'Optional USER/CAMPAIGN/GLOBAL filter; null leaves scope unspecified. USER '
                'means current-user personal assets.'
            )),
        ] = None,
        campaign_id: Annotated[
            str | None, Field(description=(
                'Explicit campaign ID; null uses the configured campaign when scope=CAMPAIGN, '
                'otherwise omits the field. If supplied, it is forwarded for any scope.'
            )),
        ] = None,
        page: Annotated[
            int, Field(description=(
                'One-based page number, integer >=1; defaults to the first page.'
            )),
        ] = 1,
        limit: Annotated[
            int, Field(description=(
                'Assets per page, integer 1..100; defaults to 50.'
            )),
        ] = 50,
        search: Annotated[
            str | None, Field(description=(
                'Optional search text passed upstream; null omits the filter, empty text is '
                'sent explicitly.'
            )),
        ] = None,
    ) -> dict:
        """List document assets with scope, search, and page filters.
        Use campaign_document_list to discover personal documents shared into this campaign;
        use document_read for content. Read-only; upstream filters by access. USER restricts
        to personal assets (including an explicit current-user filter for admins); null scope
        leaves filtering to upstream. Repeated listing does not consume or modify assets.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("page must be >=1 and limit must be in 1..100")
        ctx = get_ctx()
        params = {"type": "DOCUMENT", "page": page, "limit": limit,
                  **scope_fields(ctx, scope, campaign_id)}
        if search is not None:
            params["search"] = search
        me = ctx.auth.user or {}
        if scope == "USER" and me.get("platformRole") == "ADMIN":
            if not me.get("id"):
                raise ValueError("Cannot determine the current admin ID; refusing to list other users' personal documents")
            params["uploadedBy"] = me["id"]
        return ctx.client.get("/api/assets", params=params)

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def campaign_document_list() -> dict:
        """List native and shared documents visible in the configured campaign.
        Use document_list for asset-library filters and document_read for content.
        Read-only for members. shared=false identifies a native document without a separate
        link to revoke; document_unshare cannot remove that source of access.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/documents")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def document_read(
        document_id: Annotated[
            str, Field(description=(
                'Document asset ID; only ASCII letters, digits, underscores, and hyphens are '
                'accepted because it also names a local file.'
            )),
        ],
        etag: Annotated[
            str | None, Field(description=(
                'Previous response ETag, passed unchanged as If-None-Match; null requests '
                'content without a cache condition.'
            )),
        ] = None,
    ) -> dict:
        """Read an accessible document as text or a local binary download.
        Use document_list/campaign_document_list to discover IDs; use document_update to
        replace txt/md content. Upstream checks asset visibility, including shared access.
        Does not modify upstream data, but binary reads atomically replace downloads/<id>.pdf
        (or .bin) locally; readOnlyHint refers to the upstream resource. Text returns
        mime_type/etag/content. Binary returns mime_type/etag/file_path/file_size, never inline
        bytes. Pass etag unchanged for a conditional read: 304 returns not_modified=true
        without content, so retain your cached copy. Repeated reads may refresh the local file.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        # This ID is both a URL segment and a local filename; reject path traversal.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
            raise ValueError("document_id may contain only letters, digits, underscores, and hyphens")
        headers = {"If-None-Match": etag} if etag is not None else {}
        result = get_ctx().client.get_raw(f"/api/assets/documents/{document_id}", headers=headers)
        if result.get("not_modified") or isinstance(result["content"], str):
            return result
        content = result["content"]
        DOWNLOAD_DIR.mkdir(exist_ok=True)
        if DOWNLOAD_DIR.is_symlink():
            raise ValueError("downloads must not be a symlink directory")
        suffix = ".pdf" if result["mime_type"] == "application/pdf" else ".bin"
        target = DOWNLOAD_DIR / f"{document_id}{suffix}"
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=DOWNLOAD_DIR, delete=False) as output:
                temp_path = Path(output.name)
                output.write(content)
            os.replace(temp_path, target)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return {"mime_type": result["mime_type"], "etag": result["etag"],
                "file_path": str(target.resolve()), "file_size": len(content)}

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def document_update(
        document_id: Annotated[
            str, Field(description=(
                'Existing editable text document asset ID, not a share-link ID.'
            )),
        ],
        content: Annotated[
            str, Field(description=(
                'Complete text, including empty text, at most 900 KiB UTF-8; rejects C0/DEL '
                'controls except TAB/LF/FF/CR.'
            )),
        ],
    ) -> dict:
        """Replace an entire txt/md document's content (uploader/admin only).
        Use document_read first to avoid losing text; use document_create for a new asset or
        document_upload for PDF. This is replacement, not merge-patching, and cannot edit
        PDF content. Empty content clears the text. Repeated writes can update metadata or
        notifications; no full-operation idempotency is promised. Returns upstream REST data.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        validate_content(content)
        return get_ctx().client.put(f"/api/assets/documents/{segment(document_id)}/content", {"content": content})

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False,
        idempotentHint=False, openWorldHint=True,
    ))
    @wrap
    def document_share(
        document_id: Annotated[
            str, Field(description=(
                'Shareable DOCUMENT asset UUID, not a local filename; must satisfy upstream '
                'ownership/scope checks.'
            )),
        ],
    ) -> dict:
        """Link a shareable document asset into the configured campaign (DM only).
        Use campaign_document_list to inspect existing links, or document_upload to create
        an asset first. Upstream validates asset ownership, UUID, and sharing rights.
        Sharing broadens access without copying the asset. Duplicate-link behavior is
        upstream-controlled; do not assume retries are idempotent. Use document_unshare
        to revoke this link, not document_delete, which removes the asset everywhere.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/documents", {"assetId": document_id})

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def document_unshare(
        document_id: Annotated[
            str, Field(description=(
                'Document asset ID with an explicit share link in this campaign; native '
                'documents cannot be unshared this way.'
            )),
        ],
    ) -> dict:
        """Remove one document share link from the configured campaign (DM only).
        Inspect campaign_document_list first: shared=false denotes a native document with
        no separate link to revoke. Use document_delete only to delete the asset everywhere.
        This leaves the asset, other links, local downloads, and GLOBAL/native read access
        intact. Repeating removes no additional link but may return a not-found error.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        ctx = get_ctx()
        return ctx.client.delete(f"/api/campaigns/{ctx.campaign_id}/documents/{segment(document_id)}")

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True,
        idempotentHint=True, openWorldHint=True,
    ))
    @wrap
    def document_delete(
        document_id: Annotated[
            str, Field(description=(
                'Document asset ID to remove globally, including all campaign sharing links; '
                'not a single link ID.'
            )),
        ],
    ) -> dict:
        """Delete a document asset and all of its share links.
        Use document_unshare to remove only one campaign link and document_read to inspect
        content first. Upstream enforces uploader/DM/admin permissions according to scope.
        Deletion is destructive; local downloaded copies remain. Repeating leaves the asset
        absent but can return a not-found error. This tool does not offer an undo operation.
        Returns {ok:true,data} on success; tool-body failures return {ok:false,error} with optional
        diagnostic data. Argument-schema errors are MCP errors."""
        return get_ctx().client.delete(f"/api/assets/{segment(document_id)}")
