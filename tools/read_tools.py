"""只读工具：campaign_status / chat_read / events_poll / map_list /
initiative_state / character_list / character_get / character_validate / creature_search"""
from __future__ import annotations

from tools import (E_CURSOR, INITIATIVE_ROLL_SYSTEMS, SYSTEM_DND5E, require_system, wrap)


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
        camp = ctx.read_campaign()
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
            "documents": "unknown",
            "saved_rolls": "unknown",
            "dm_transfer": "unknown",
            "hitdice_spend": "unknown" if system == SYSTEM_DND5E else False,
        }
        return {
            "health": health,
            "campaign": {k: camp.get(k) for k in
                         ("id", "name", "status", "gameSystem", "currentMapId", "description", "ownerId")},
            "features": features,
            "feature_evidence": {"new_api": "未探测新端点；unknown 不代表已支持",
                                 "hitdice_spend": "仅 DND_5E 通过系统门；WS 无可靠能力探测"},
            "role": ctx.role_from_campaign(camp),
            "owner": {"id": camp.get("ownerId"),
                      "is_me": camp["ownerId"] == (ctx.auth.user or {}).get("id") if camp.get("ownerId") else None},
            "current_map": current_map and {k: current_map.get(k) for k in ("id", "name", "width", "height")},
            "me": ctx.auth.user,
        }

    @mcp.tool
    @wrap
    def chat_read(limit: int = 20, cursor: str | None = None) -> dict:
        """最新消息；下一页原样传 pagination.nextCursor。v0.2 移除 offset。
        无 nextCursor 的旧实例只读最新页；DICE_ROLL 不入聊天史，骰史看 events_poll。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1..100")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError("cursor 必须为非空字符串或 null")
        ctx = get_ctx()
        if cursor is not None and ctx._cursor_supported is False:
            raise ValueError(E_CURSOR)
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        result = ctx.client.get(
            f"/api/campaigns/{ctx.campaign_id}/messages",
            params=params)
        ctx._cursor_supported = isinstance(result.get("pagination"), dict) and "nextCursor" in result["pagination"]
        if cursor is not None and not ctx._cursor_supported:
            raise ValueError(E_CURSOR)
        return result

    @mcp.tool
    @wrap
    def events_poll(since: int = 0, limit: int = 100) -> dict:
        """拉取 seq > since 的实时事件（玩家发言/骰子/移动），含骰史。
        返回 events/next_seq/high_water_seq/gap；以 next_seq 作为下次 since。
        latest_seq 为 next_seq 的兼容别名；gap 表示缓冲淘汰，cursor_reset 表示游标超出当前进程。"""
        ctx = get_ctx()
        connection_error = None
        try:
            ctx.ensure_ws()
        except RuntimeError as exc:
            connection_error = str(exc)
        result = ctx.ws.poll(since=since, limit=limit)
        result["role"] = result.get("role") or ctx.cached_role()
        result["stale"] = not ctx.ws.authenticated
        result["campaign_cache_stale"] = ctx.cached_role() is None
        if connection_error:
            result["connection_error"] = connection_error
        return result

    @mcp.tool
    @wrap
    def map_list() -> dict:
        """地图列表。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/maps")

    @mcp.tool
    @wrap
    def initiative_state(refresh: bool = True) -> dict:
        """默认请求最新 initiative.state；refresh=false 读缓存，超时为未知。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        rec = ctx.ws.latest("initiative.state")
        if not ctx.ws.authenticated:
            return {"state": None, "note": "WS 尚未认证，先攻状态不可用"}
        if refresh:
            receipt = ctx.ws.emit("initiative.request_state", {})
            rec = ctx.ws.wait_for_event("initiative.state", receipt["since"])
        if not rec:
            return {"state": None, "stale": True, "note": "先攻状态未知：未收到最新 initiative.state，请读取 events_poll"}
        return {"state": rec["payload"], "seq": rec["seq"], "ts": rec["ts"], "stale": not refresh}

    @mcp.tool
    @wrap
    def character_list() -> dict:
        """角色列表（战役 roster）。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/characters")

    @mcp.tool
    @wrap
    def character_get(character_id: str) -> dict:
        """读卡全量，保留未知字段；以角色自身 gameSystem 为准。
        CoC conditions/appearance/spellsAndMythos/notes 与 DND hitDice 均原样保留。
        Keeper notes 是普通 data.notes，并非 DM 私密字段。"""
        ctx = get_ctx()
        return ctx.client.get(f"/api/characters/{character_id}")

    @mcp.tool
    @wrap
    def character_validate(character_id: str) -> dict:
        """仅 owner 可调用。保留上游结果，但 validation_reliable 固定 false。
        v1.4 忽略校验失败结果，不能凭 isValid=true 判定合法；旧版可靠性未知。"""
        ctx = get_ctx()
        result = ctx.client.get(f"/api/characters/{character_id}/validate")
        return {**result, "validation_reliable": False,
                "validation_note": "上游 v1.4 校验路由忽略 validation.success=false，可能误报 isValid=true；旧版可靠性未知。"}

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
        source = source.strip().lower()
        if source not in {"", "srd", "custom"}:
            raise ValueError("source 只能为 srd / custom 或空字符串")
        if source == "srd":
            require_system(ctx, (SYSTEM_DND5E,), "SRD 怪库（Open5e 为 D&D 5e 数据源）")
        elif not source and ctx.get_system() != SYSTEM_DND5E:
            source = "custom"
        if source:
            params["source"] = source
        if cr:
            params["cr"] = cr
        return ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/creatures", params=params)
