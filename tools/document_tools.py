"""Documents: separate JSON/multipart metadata from raw content reads; upstream enforces permissions."""
from __future__ import annotations

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
    @mcp.tool
    @wrap
    def document_upload(file_path: str, scope: Scope = "USER", campaign_id: str | None = None,
                        name: str | None = None, description: str | None = None,
                        tags: list[str] | None = None) -> dict:
        """Upload a local PDF/txt/md file with multipart type=DOCUMENT. CAMPAIGN defaults to the current campaign.
        Upstream validates scope permissions and file signatures; the instance sets size limits (default 50 MiB). A 400 does not trigger a fallback upload."""
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

    @mcp.tool
    @wrap
    def document_create(name: str, format: Literal["txt", "md"], content: str,
                        scope: Scope = "USER", campaign_id: str | None = None,
                        description: str | None = None) -> dict:
        """Create txt/md directly (at most 900 KiB UTF-8); USER is private, GLOBAL is global, and CAMPAIGN belongs to a campaign."""
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

    @mcp.tool
    @wrap
    def document_list(scope: Scope | None = None, campaign_id: str | None = None,
                      page: int = 1, limit: int = 50, search: str | None = None) -> dict:
        """List documents by scope (USER=personal); use campaign_document_list for private documents shared with the campaign."""
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

    @mcp.tool
    @wrap
    def campaign_document_list() -> dict:
        """List shared and native documents in the current campaign; shared=false means there is no separate link to revoke."""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/documents")

    @mcp.tool
    @wrap
    def document_read(document_id: str, etag: str | None = None) -> dict:
        """Read txt/md as mime_type/etag/content; save PDFs and other binary files to downloads/ and return their path/byte count.
        Send etag unchanged as If-None-Match; 304 returns {not_modified:true} so the caller can reuse existing content."""
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

    @mcp.tool
    @wrap
    def document_update(document_id: str, content: str) -> dict:
        """Replace the entire txt/md content, at most 900 KiB; uploader/admin only. PDF editing is not supported."""
        validate_content(content)
        return get_ctx().client.put(f"/api/assets/documents/{segment(document_id)}/content", {"content": content})

    @mcp.tool
    @wrap
    def document_share(document_id: str) -> dict:
        """DM: link a shareable DOCUMENT to the current campaign; upstream validates UUID and ownership."""
        ctx = get_ctx()
        return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/documents", {"assetId": document_id})

    @mcp.tool
    @wrap
    def document_unshare(document_id: str) -> dict:
        """DM: revoke a document link from the current campaign without deleting the asset or other read permissions.
        Native shared=false documents cannot simply be unshared; check their source with campaign_document_list first."""
        ctx = get_ctx()
        return ctx.client.delete(f"/api/campaigns/{ctx.campaign_id}/documents/{segment(document_id)}")

    @mcp.tool
    @wrap
    def document_delete(document_id: str) -> dict:
        """Delete the document asset and all its sharing links (upstream enforces scope permissions); this is not unshare."""
        return get_ctx().client.delete(f"/api/assets/{segment(document_id)}")
