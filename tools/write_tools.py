"""写工具：chat_send / dice_roll / map_switch / token_add / token_move /
token_hp / initiative_manage / character_update / character_create /
token_place_creature / session_manage"""
from __future__ import annotations

from tools import INITIATIVE_ROLL_SYSTEMS, require_system, wrap


def register(mcp, get_ctx) -> None:

    @mcp.tool
    @wrap
    def chat_send(content: str, type: str = "DM") -> dict:
        """DM 叙事 / NPC 台词。type 仅接受 DM / PLAYER（上游校验，2026-09-04 实测）。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        ctx.ws.emit("chat.message", {"content": content, "type": type})
        return {"sent": True, "content": content, "type": type}

    @mcp.tool
    @wrap
    def dice_roll(expression: str, is_secret: bool = False) -> dict:
        """公证骰，如 1d20+5 / 2d6 / 4d6kh3。is_secret=true 暗骰（仅 DM 可见）。
        客户端侧最小间隔 2.1s（WS 限流 30/min），排队不报错。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        ctx.dice_throttle()
        ctx.ws.emit("dice.roll", {"expression": expression, "isSecret": is_secret})
        return {"rolled": True, "expression": expression, "isSecret": is_secret,
                "note": "结果经 events_poll 的 dice.rolled 事件回收"}

    @mcp.tool
    @wrap
    def map_switch(map_id: str) -> dict:
        """切当前图（WS 会广播 map.changed）。"""
        ctx = get_ctx()
        return ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/set-current")

    @mcp.tool
    @wrap
    def token_add(map_id: str, name: str, image_url: str, x: float, y: float,
                  character_id: str = "", width: float = 1, height: float = 1,
                  layer: str = "token", visible: bool = True) -> dict:
        """放 token 到地图（DM only）。"""
        ctx = get_ctx()
        payload = {
            "name": name, "imageUrl": image_url,
            "position": {"x": x, "y": y},
            "size": {"width": width, "height": height},
            "layer": layer, "visible": visible,
        }
        if character_id:
            payload["characterId"] = character_id
        return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens", payload)

    @mcp.tool
    @wrap
    def token_move(map_id: str, token_id: str, x: float, y: float) -> dict:
        """移动 token（REST PUT position；WS 广播 map.changed 同步全桌）。"""
        ctx = get_ctx()
        return ctx.client.put(
            f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens/{token_id}",
            {"position": {"x": x, "y": y}})

    @mcp.tool
    @wrap
    def token_hp(character_id: str, delta: int) -> dict:
        """改 token HP（delta 正=治疗 负=伤害，播报全桌 character.hp.updated）。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        ctx.ws.emit("character.hp.update", {"characterId": character_id, "delta": delta})
        return {"sent": True, "characterId": character_id, "delta": delta}

    @mcp.tool
    @wrap
    def initiative_manage(action: str, token_id: str = "", map_id: str = "",
                          value: float | None = None,
                          ordered_token_ids: list[str] | None = None,
                          expression: str = "", character_name: str = "") -> dict:
        """先攻管理（全 DM-only）。
        action: add / remove / roll / set / reorder / start / next / end。
        - add: token_id + map_id
        - remove: token_id
        - roll: token_id + map_id（expression 可选；服务器按战役系统自行推导骰式——
          5e=敏捷+卡面 initiativeBonus，PF2e 用 usedStat，SR6 用自身先攻骰，客户端给的
          expression 仅在无法推导时兜底。系统门：CoC7e 不骰先攻、DEX 排序，请 add 后直接
          start，或用 set 手动定值）
        - set: token_id + map_id + value（手动定先攻值，服务器按值重排序）
        - reorder: ordered_token_ids（自定义回合顺序，覆盖值排序）
        状态经 initiative_state / events_poll 回收。"""
        ctx = get_ctx()
        action = action.strip().lower()
        allowed = {"add", "remove", "roll", "set", "reorder", "start", "next", "end"}
        if action not in allowed:
            raise ValueError(f"action 必须是 {sorted(allowed)} 之一")
        payload = {}
        if action == "add":
            if not token_id or not map_id:
                raise ValueError("add 需要 token_id 和 map_id")
            payload = {"tokenId": token_id, "mapId": map_id}
        elif action == "remove":
            if not token_id:
                raise ValueError("remove 需要 token_id")
            payload = {"tokenId": token_id}
        elif action == "roll":
            require_system(ctx, INITIATIVE_ROLL_SYSTEMS,
                           "initiative.roll（CoC7e 先攻按 DEX 排序不骰骰）")
            if not token_id or not map_id:
                raise ValueError("roll 需要 token_id 和 map_id")
            payload = {"tokenId": token_id, "mapId": map_id}
            if expression:
                payload["expression"] = expression
            if character_name:
                payload["characterName"] = character_name
        elif action == "set":
            if not token_id or not map_id or value is None:
                raise ValueError("set 需要 token_id、map_id 和 value")
            payload = {"tokenId": token_id, "mapId": map_id, "value": value}
        elif action == "reorder":
            if not ordered_token_ids:
                raise ValueError("reorder 需要 ordered_token_ids（token id 列表）")
            payload = {"orderedTokenIds": ordered_token_ids}
        ctx.ensure_ws()
        ctx.ws.emit(f"initiative.{action}", payload)
        return {"sent": True, "action": action, "payload": payload,
                "note": "状态经 initiative_state / events_poll 回收"}

    @mcp.tool
    @wrap
    def character_update(character_id: str, data: dict) -> dict:
        """局部更新角色卡（SAN/HP/Luck/MP/法术位等结算由调用方算好传入，桥不做规则计算）。
        data 为要 PUT 的字段 dict。"""
        ctx = get_ctx()
        return ctx.client.put(f"/api/characters/{character_id}", data)

    @mcp.tool
    @wrap
    def character_create(name: str, data: dict | None = None,
                         token_image_url: str = "") -> dict:
        """创建角色卡并直接加入当前战役。gameSystem 自动继承战役系统
        （服务器会按该系统 Zod schema 校验 data，校验失败返回 400）。
        data 为卡面字段 dict（结构随系统：5e 见上游 dnd5e.ts，CoC7e 见 callOfCthulhu7e.ts）。
        建完可用 character_validate 复查。"""
        ctx = get_ctx()
        payload = {"name": name, "campaignId": ctx.campaign_id}
        if data:
            payload["data"] = data
        if token_image_url:
            payload["tokenImageUrl"] = token_image_url
        return ctx.client.post("/api/characters", payload)

    @mcp.tool
    @wrap
    def token_place_creature(creature_id: str, map_id: str, x: float, y: float) -> dict:
        """调怪上图：读怪库模板 → 以模板名/图放置 token。"""
        ctx = get_ctx()
        tpl = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/creatures/{creature_id}")
        creature = tpl.get("creature", tpl)
        name = creature.get("name", "creature")
        image_url = (creature.get("imageUrl") or creature.get("tokenImageUrl")
                     or creature.get("image") or "")
        if not image_url:
            raise ValueError(f"怪库模板 {creature_id} 无可用图片字段，无法放置 token")
        payload = {
            "name": name, "imageUrl": image_url,
            "position": {"x": x, "y": y},
            "size": {"width": 1, "height": 1},
            "layer": "token", "visible": True,
        }
        return ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens", payload)

    @mcp.tool
    @wrap
    def session_manage(action: str) -> dict:
        """场次管理（DM only）。action: start / pause / end。"""
        ctx = get_ctx()
        action = action.strip().lower()
        allowed = {"start", "pause", "end"}
        if action not in allowed:
            raise ValueError(f"action 必须是 {sorted(allowed)} 之一")
        ctx.ensure_ws()
        ctx.ws.emit(f"session.{action}", {})
        return {"sent": True, "action": action,
                "note": "结果经 events_poll 的 session.* 事件回收"}
