"""只读工具：campaign_status / chat_read / events_poll / map_list /
initiative_state / character_list / character_get / character_validate / creature_search"""
from __future__ import annotations

from tools import (INITIATIVE_ROLL_SYSTEMS, SYSTEM_DND5E, require_system, wrap)


def register(mcp, get_ctx) -> None:

    @mcp.tool
    @wrap
    def campaign_status() -> dict:
        """实例健康 + 战役状态 + 当前地图。健康检查用 GET /health（200 即活）。"""
        ctx = get_ctx()
        health = {"reachable": False}
        try:
            h = ctx.client.get("/health")
            health = {"reachable": True, "detail": h if isinstance(h, dict) else {}}
        except Exception:
            # /health 可能被前端 SPA 兜底接管；能拿到 200 就算活
            try:
                ctx.client.get("/api/auth/ping")
                health = {"reachable": True, "detail": {"via": "/api/auth/ping"}}
            except Exception as e2:
                health = {"reachable": False, "error": str(e2)}
        camp = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}").get("campaign", {})
        current_map = None
        if camp.get("currentMapId"):
            try:
                maps = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/maps").get("maps", [])
                current_map = next((m for m in maps if m.get("id") == camp["currentMapId"]), None)
            except Exception:
                pass
        system = camp.get("gameSystem")
        # 系统门控特性面：哪些能力在当前战役系统下可用（门控明细见各工具 docstring）
        features = {
            "srd_creature_library": system == SYSTEM_DND5E,  # Open5e 数据源仅 5e
            "homebrew_creature_library": True,
            "initiative_roll": system in INITIATIVE_ROLL_SYSTEMS,  # CoC7e 为 DEX 排序不骰
        }
        return {
            "health": health,
            "campaign": {k: camp.get(k) for k in
                         ("id", "name", "status", "gameSystem", "currentMapId", "description")},
            "features": features,
            "current_map": current_map and {k: current_map.get(k) for k in ("id", "name", "width", "height")},
            "me": ctx.auth.user,
        }

    @mcp.tool
    @wrap
    def chat_read(limit: int = 20, offset: int = 0) -> dict:
        """翻聊天记录（注意：DICE_ROLL 类不入聊天史，骰史看 events_poll）。"""
        ctx = get_ctx()
        return ctx.client.get(
            f"/api/campaigns/{ctx.campaign_id}/messages",
            params={"limit": min(limit, 100), "offset": offset})

    @mcp.tool
    @wrap
    def events_poll(since: int = 0, limit: int = 100) -> dict:
        """拉取 seq > since 的实时事件（玩家发言/骰子/移动），含骰史。
        返回 {events, latest_seq}；把 latest_seq 存下当下次的 since 即可增量拉。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        return ctx.ws.poll(since=since, limit=limit)

    @mcp.tool
    @wrap
    def map_list() -> dict:
        """地图列表。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/maps")

    @mcp.tool
    @wrap
    def initiative_state() -> dict:
        """查先攻（最近一条 initiative.state 广播；无则提示尚未开战）。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        rec = ctx.ws.latest("initiative.state")
        if not rec:
            return {"state": None, "note": "缓冲中尚无 initiative.state（可能未开战或 WS 未收到）"}
        return {"state": rec["payload"], "seq": rec["seq"], "ts": rec["ts"]}

    @mcp.tool
    @wrap
    def character_list() -> dict:
        """角色列表（战役 roster）。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/characters")

    @mcp.tool
    @wrap
    def character_get(character_id: str) -> dict:
        """读角色卡全量（卡面结构随战役系统：5e/PF2e/SR6/CoC7e 各自 schema）。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/characters/{character_id}")

    @mcp.tool
    @wrap
    def character_validate(character_id: str) -> dict:
        """用角色卡自身 gameSystem 的 Zod schema 重验 data（服务器侧校验）。
        返回 {isValid, errors?}——校验不通过也是 200，看 isValid 字段。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/characters/{character_id}/validate")

    @mcp.tool
    @wrap
    def creature_search(search: str = "", source: str = "", cr: str = "",
                        limit: int = 20, offset: int = 0) -> dict:
        """Open5e SRD + 战役自定义怪库检索。source: srd|custom。
        系统门：source=srd 仅 DND_5E 战役（Open5e 是 D&D 5e 数据源）；custom 不限系统。"""
        ctx = get_ctx()
        params = {"limit": min(limit, 100), "offset": offset}
        if search:
            params["search"] = search
        if source:
            if source.strip().lower() == "srd":
                require_system(ctx, (SYSTEM_DND5E,), "SRD 怪库（Open5e 为 D&D 5e 数据源）")
            params["source"] = source
        if cr:
            params["cr"] = cr
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/creatures", params=params)
