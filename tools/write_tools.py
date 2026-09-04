"""写工具：chat_send / dice_roll / map_switch / token_add / token_move /
token_hp / initiative_manage / character_update / token_place_creature / session_manage"""
from __future__ import annotations

from tools import wrap


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
    def initiative_manage(action: str, token_id: str = "", map_id: str = "") -> dict:
        """先攻管理（全 DM-only）。action: add / remove / start / next / end。
        add 需要 token_id + map_id；remove 需要 token_id。"""
        ctx = get_ctx()
        action = action.strip().lower()
        allowed = {"add", "remove", "start", "next", "end"}
        if action not in allowed:
            raise ValueError(f"action 必须是 {sorted(allowed)} 之一")
        ctx.ensure_ws()
        payload = {}
        if action == "add":
            if not token_id or not map_id:
                raise ValueError("add 需要 token_id 和 map_id")
            payload = {"tokenId": token_id, "mapId": map_id}
        elif action == "remove":
            if not token_id:
                raise ValueError("remove 需要 token_id")
            payload = {"tokenId": token_id}
        ctx.ws.emit(f"initiative.{action}", payload)
        return {"sent": True, "action": action, "payload": payload,
                "note": "状态经 initiative_state / events_poll 回收"}

    @mcp.tool
    @wrap
    def character_update(character_id: str, data: dict) -> dict:
        """局部更新角色卡（SAN/HP/Luck/MP 结算由调用方算好传入，桥不做规则计算）。
        data 为要 PUT 的字段 dict。"""
        ctx = get_ctx()
        return ctx.client.put(f"/api/characters/{character_id}", data)

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
