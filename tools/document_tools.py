"""Documents：JSON/multipart 元数据与原文读取分开；权限由上游判定。"""
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
        raise ValueError("资源 ID 不能为空或为相对路径")
    return quote(value, safe="")


def scope_fields(ctx, scope, campaign_id):
    if scope not in {None, "USER", "CAMPAIGN", "GLOBAL"}:
        raise ValueError("scope 必须为 USER/CAMPAIGN/GLOBAL")
    fields = {} if scope is None else {"scope": scope}
    if scope == "CAMPAIGN":
        fields["campaignId"] = campaign_id or ctx.campaign_id
    elif campaign_id is not None:
        fields["campaignId"] = campaign_id
    return fields


def validate_content(content):
    if not isinstance(content, str):
        raise ValueError("content 必须为字符串")
    if len(content.encode("utf-8")) > 900 * 1024:
        raise ValueError("直接创建/编辑文档最多 900 KiB UTF-8；更大文件请使用 document_upload")
    if any((ord(c) < 32 and c not in "\t\n\f\r") or ord(c) == 127 for c in content):
        raise ValueError("文档包含不允许的控制字符")


def register(mcp, get_ctx):
    @mcp.tool
    @wrap
    def document_upload(file_path: str, scope: Scope = "USER", campaign_id: str | None = None,
                        name: str | None = None, description: str | None = None,
                        tags: list[str] | None = None) -> dict:
        """上传本地 PDF/txt/md，multipart type=DOCUMENT。CAMPAIGN 默认当前战役。
        scope 权限及魔数由上游校验；大小取实例配置（默认 50 MiB），400 不自动降级重传。"""
        path = Path(file_path).expanduser()
        mime = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown"}.get(path.suffix.lower())
        if mime is None or not path.is_file():
            raise ValueError("file_path 必须是存在的 PDF/txt/md 文件")
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
        """直接创建 txt/md（≤900 KiB UTF-8）；USER 私有，GLOBAL 全局，CAMPAIGN 战役文档。"""
        name = name.strip()
        if not 1 <= len(name) <= 200 or format not in {"txt", "md"}:
            raise ValueError("name 必须为 1..200 字符，format 必须为 txt/md")
        validate_content(content)
        ctx = get_ctx()
        payload = {"name": name, "format": format, "content": content,
                   **scope_fields(ctx, scope, campaign_id)}
        if description is not None:
            if len(description.strip()) > 1000:
                raise ValueError("description 最多 1000 字符")
            payload["description"] = description.strip()
        return ctx.client.post("/api/assets/documents", payload)

    @mcp.tool
    @wrap
    def document_list(scope: Scope | None = None, campaign_id: str | None = None,
                      page: int = 1, limit: int = 50, search: str | None = None) -> dict:
        """按 scope 列文档（USER=personal）；共享给战役的私人文档用 campaign_document_list。"""
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("page 必须 >=1，limit 必须在 1..100")
        ctx = get_ctx()
        params = {"type": "DOCUMENT", "page": page, "limit": limit,
                  **scope_fields(ctx, scope, campaign_id)}
        if search is not None:
            params["search"] = search
        me = ctx.auth.user or {}
        if scope == "USER" and me.get("platformRole") == "ADMIN":
            if not me.get("id"):
                raise ValueError("无法确认当前管理员 ID，拒绝列出其他人的个人文档")
            params["uploadedBy"] = me["id"]
        return ctx.client.get("/api/assets", params=params)

    @mcp.tool
    @wrap
    def campaign_document_list() -> dict:
        """列当前战役共享及原生文档；shared=false 无独立分享链接可撤销。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/documents")

    @mcp.tool
    @wrap
    def document_read(document_id: str, etag: str | None = None) -> dict:
        """txt/md 返回 mime_type/etag/content；PDF 等落盘 downloads/ 后返回路径/字节数。
        etag 原样作为 If-None-Match，304 返回 {not_modified:true}，由调用方复用已有内容。"""
        # 该 ID 同时用于路径段和本地文件名，拒绝路径穿越。
        if not re.fullmatch(r"[A-Za-z0-9_-]+", document_id):
            raise ValueError("document_id 只能包含字母、数字、下划线和连字符")
        headers = {"If-None-Match": etag} if etag is not None else {}
        result = get_ctx().client.get_raw(f"/api/assets/documents/{document_id}", headers=headers)
        if result.get("not_modified") or isinstance(result["content"], str):
            return result
        content = result["content"]
        DOWNLOAD_DIR.mkdir(exist_ok=True)
        if DOWNLOAD_DIR.is_symlink():
            raise ValueError("downloads 不得为符号链接目录")
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
        """整体替换 txt/md 正文，≤900 KiB；仅上传者/admin，PDF 不支持编辑。"""
        validate_content(content)
        return get_ctx().client.put(f"/api/assets/documents/{segment(document_id)}/content", {"content": content})

    @mcp.tool
    @wrap
    def document_share(document_id: str) -> dict:
        """DM 将可分享的 DOCUMENT 链接到当前战役；上游验证 UUID/所有权。"""
        ctx = get_ctx()
        return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/documents", {"assetId": document_id})

    @mcp.tool
    @wrap
    def document_unshare(document_id: str) -> dict:
        """DM 撤销当前战役的文档链接，不删除资产或其他读取授权。
        原生 shared=false 文档无法仅 unshare；先用 campaign_document_list 确认来源。"""
        ctx = get_ctx()
        return ctx.client.delete(f"/api/campaigns/{ctx.campaign_id}/documents/{segment(document_id)}")

    @mcp.tool
    @wrap
    def document_delete(document_id: str) -> dict:
        """删除文档资产及其全部分享链接（scope 权限由上游核验）；不是 unshare。"""
        return get_ctx().client.delete(f"/api/assets/{segment(document_id)}")
