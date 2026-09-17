"""Saved Rolls、DM 移交、Hit Dice、场次历史。固定当前战役，无跨用户宏操作。"""
from __future__ import annotations

from tools import E_PENDING, HITDICE_SYSTEMS, require_system, wrap
from tools.document_tools import segment


def macro_fields(name=None, expression=None):
    fields = {}
    for key, value, maximum in (("name", name, 60), ("expression", expression, 200)):
        if value is not None:
            value = value.strip()
            if not 1 <= len(value) <= maximum:
                raise ValueError(f"{key} trim 后必须为 1..{maximum} 字符")
            fields[key] = value
    if not fields:
        raise ValueError("至少提供 name/expression 之一")
    return fields


def register(mcp, get_ctx):
    @mcp.tool
    @wrap
    def saved_roll_list() -> dict:
        """当前用户在本战役的 Saved Rolls，完整 Macro 列表，无需逐条 get。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/macros")

    @mcp.tool
    @wrap
    def saved_roll_create(name: str, expression: str) -> dict:
        """保存骰式，name 1..60/expression 1..200（trim）；服务端解析并限制每人每战役 50 条。
        本进程串行创建；不执行骰子，使用时取 expression 调 dice_roll。"""
        payload = macro_fields(name, expression)
        ctx = get_ctx()
        with ctx._saved_roll_lock:
            return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/macros", payload)

    @mcp.tool
    @wrap
    def saved_roll_update(macro_id: str, name: str | None = None,
                          expression: str | None = None) -> dict:
        """更新自己的本战役宏；至少一个字段，表达式合法性由上游校验。"""
        payload = macro_fields(name, expression)
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/macros/{segment(macro_id)}", payload)

    @mcp.tool
    @wrap
    def saved_roll_delete(macro_id: str) -> dict:
        """删除自己的本战役宏；跨用户/跨战役隐藏为 404。"""
        ctx = get_ctx()
        return ctx.client.delete(f"/api/campaigns/{ctx.campaign_id}/macros/{segment(macro_id)}")

    @mcp.tool
    @wrap
    def campaign_transfer_dm(user_id: str) -> dict:
        """移交 DM 给已有成员（user_id UUID）；owner 收回时传自己的 ID。
        caller 须 DM/owner/admin，ownerId 不变；上游事务授权，绝不以 owner 推断 DM。"""
        ctx = get_ctx()
        result = ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/dm", {"userId": user_id})
        ctx.invalidate_campaign()  # 即使漏收到 best-effort 广播，也必须刷新。
        return result

    @mcp.tool
    @wrap
    def character_hitdice_spend(character_id: str, index: int) -> dict:
        """仅 DND_5E；发送 character.hitdice.spend 扣一次，不掷骰、不治疗。
        WS 无能力探测/业务 ACK；无论是否收到其他广播均只返回 pending，结果读 events_poll。"""
        if not character_id or type(index) is not int or index < 0:
            raise ValueError("character_id 必填，index 必须为 >=0 的整数")
        ctx = get_ctx()
        require_system(ctx, HITDICE_SYSTEMS, "Hit Dice 花费")
        ctx.ensure_ws()
        receipt = ctx.ws.emit("character.hitdice.spend", {"characterId": character_id, "index": index})
        return {**receipt, "confirmed": False, "status": "pending", "note": E_PENDING}

    @mcp.tool
    @wrap
    def session_list() -> dict:
        """读取本战役最近 50 场（含活动场次），sessionNumber 降序；notes 全桌可读。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/sessions")

    @mcp.tool
    @wrap
    def session_notes_update(session_id: str, notes: str) -> dict:
        """DM 修改场次共享摘要（最多 2000 字符）；trim 后空串清空，不再次结束会话。"""
        if len(notes) > 2000:
            raise ValueError("notes 最多 2000 字符")
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/sessions/{segment(session_id)}/notes", {"notes": notes})
