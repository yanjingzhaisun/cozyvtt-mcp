"""写工具：chat_send / dice_roll / map_switch / token_add / token_move /
token_hp / initiative_manage / character_update / character_create /
token_place_creature / session_manage"""
from __future__ import annotations

from copy import deepcopy

from tools import INITIATIVE_ROLL_SYSTEMS, PartialFailure, require_system, wrap


def merge_patch(current: dict, patch: dict) -> dict:
    """字典递归合并；列表和标量整体替换；null 是显式值，不表示删除。"""
    result = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_patch(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def register(mcp, get_ctx) -> None:

    @mcp.tool
    @wrap
    def chat_send(content: str, type: str = "DM") -> dict:
        """DM 叙事 / NPC 台词。type 仅接受 DM / PLAYER（上游校验，2026-09-04 实测）。"""
        if type not in {"DM", "PLAYER"}:
            raise ValueError("type 只能为 DM / PLAYER")
        if not content.strip() or len(content) > 2000:
            raise ValueError("content 必须为非空字符串且不超过 2000 字符")
        ctx = get_ctx()
        ctx.ensure_ws()
        return {**ctx.ws.emit("chat.message", {"content": content, "type": type}),
                "content": content, "type": type}

    @mcp.tool
    @wrap
    def dice_roll(expression: str, is_secret: bool = False, purpose: str = "",
                  character_name: str | None = None) -> dict:
        """公证骰，如 1d20+5 / 2d6 / 4d6kh3。is_secret=true 暗骰（仅 DM 可见）。
        purpose 可选，用于在广播中关联此次骰子（上游会原样携带）。
        客户端侧最小间隔 2.1s（WS 限流 30/min），排队不报错。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        payload = {"expression": expression, "secret": is_secret}
        if purpose:
            payload["purpose"] = purpose
        if character_name is not None:
            payload["characterName"] = character_name
        return {**ctx.send_dice(payload), "expression": expression, "secret": is_secret,
                "purpose": purpose, "character_name": character_name}

    @mcp.tool
    @wrap
    def map_switch(map_id: str) -> dict:
        """先 REST 保存当前图，再 WS map.change 通知全桌；发送不等于广播成功。"""
        ctx = get_ctx()
        result = ctx.client.put(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/set-current")
        data = {"persisted": True, "response": result, "broadcast_pending": False,
                "confirmed": False}
        try:
            ctx.ensure_ws()
            receipt = ctx.ws.emit("map.change", {"mapId": map_id})
        except Exception as exc:
            raise PartialFailure(f"当前地图已保存，广播发送失败；勿重放 REST: {exc}", data) from exc
        return {**data, "broadcast_pending": True, "broadcast": receipt}

    @mcp.tool
    @wrap
    def token_add(map_id: str, name: str, image_url: str, x: float, y: float,
                  character_id: str = "", width: int = 1, height: int = 1,
                  layer: str = "token", visible: bool = True,
                  controlled_by: str | None = None) -> dict:
        """放 token 到地图（DM only）。"""
        if any(type(n) is not int or not 1 <= n <= 10 for n in (width, height)):
            raise ValueError("width/height 必须为 1..10 的整数")
        ctx = get_ctx()
        payload = {
            "name": name, "imageUrl": image_url,
            "position": {"x": x, "y": y},
            "size": {"width": width, "height": height},
            "layer": layer, "visible": visible,
        }
        if character_id:
            payload["characterId"] = character_id
        if controlled_by is not None:
            payload["controlledBy"] = controlled_by
        result = ctx.client.post(f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens", payload)
        return {**result, "persisted": True, "broadcast_confirmed": False}

    @mcp.tool
    @wrap
    def token_move(map_id: str, token_id: str, x: float, y: float) -> dict:
        """REST 移动；拒绝 SPECTATOR，服务端核验 DM/controlledBy。落库不代表广播。"""
        ctx = get_ctx()
        role = ctx.get_role()
        if role not in {"DM", "PLAYER"}:
            raise ValueError(f"token_move 需要 DM/PLAYER，当前角色：{role or 'unknown'}；SPECTATOR 不可移动")
        result = ctx.client.put(
            f"/api/campaigns/{ctx.campaign_id}/maps/{map_id}/tokens/{token_id}",
            {"position": {"x": x, "y": y}})
        return {**result, "persisted": True, "broadcast_confirmed": False,
                "note": "已落库，广播未确认；REST 更新不会自动发送 map.changed。"}

    @mcp.tool
    @wrap
    def token_hp(character_id: str, delta: int) -> dict:
        """改 token HP（delta 正=治疗 负=伤害，播报全桌 character.hp.updated）。"""
        ctx = get_ctx()
        ctx.ensure_ws()
        return {**ctx.ws.emit("character.hp.update", {"characterId": character_id, "delta": delta}),
                "characterId": character_id, "delta": delta}

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
        return {**ctx.ws.emit(f"initiative.{action}", payload),
                "action": action, "payload": payload}

    @mcp.tool
    @wrap
    def character_update(character_id: str, data: dict) -> dict:
        """局部更新角色卡（SAN/HP/Luck/MP/法术位等结算由调用方算好传入，桥不做规则计算）。
        data 是请求字段，例如 {"data": {"hp": {"current": 5}}}；卡面 data 递归合并，
        保留未提供字段。列表整体替换，null 为显式值。顶层允许 name/data/tokenImageUrl。
        """
        allowed = {"name", "data", "tokenImageUrl"}
        if not data or set(data) - allowed:
            raise ValueError("请提供 name/data/tokenImageUrl 字段；卡面补丁需放在 data 对象内")
        if "data" in data and not isinstance(data["data"], dict):
            raise ValueError("卡面 data 补丁必须是对象，不能整块清空")
        ctx = get_ctx()
        path = f"/api/characters/{character_id}"
        with ctx._character_lock:
            payload = deepcopy(data)
            if "data" in payload:
                current = ctx.client.get(path)
                character = current.get("character") if isinstance(current, dict) else None
                if not isinstance(character, dict) or not isinstance(character.get("data"), dict):
                    raise ValueError("上游角色响应缺少 data 对象，拒绝覆盖")
                payload["data"] = merge_patch(character["data"], payload["data"])
            # TODO(#2): 1.2.2 不支持 ETag/If-Match 或原子 JSON patch；此锁仅保护本进程
            # 的 character_update。与浏览器/其他客户端同时保存仍需上游乐观锁支持。
            return ctx.client.put(path, payload)

    @mcp.tool
    @wrap
    def character_create(name: str, data: dict | None = None,
                         token_image_url: str = "") -> dict:
        """创建角色卡并直接加入当前战役。gameSystem 自动继承战役系统
        （服务器会按该系统 Zod schema 校验 data，校验失败返回 400）。
        data 为卡面字段 dict（结构随系统：5e 见上游 dnd5e.ts，CoC7e 见 callOfCthulhu7e.ts）。
        不凭 character_validate 的 isValid=true 判定合法（上游缺陷）。"""
        if not name or len(name) > 200:
            raise ValueError("name 必须为 1..200 字符")
        ctx = get_ctx()
        payload = {"name": name, "campaignId": ctx.campaign_id}
        if data is not None:
            payload["data"] = data
        if token_image_url:
            payload["tokenImageUrl"] = token_image_url
        created = ctx.client.post("/api/characters", payload)
        character = created.get("character") if isinstance(created, dict) else None
        if not isinstance(character, dict) or not character.get("id"):
            raise PartialFailure("建卡响应缺少角色 ID，无法确认创建状态；请检查上游，勿重复创建",
                                 {"created": None, "response": created})
        # v1.4 创建会自动加入 roster；不能仅凭 character.campaignId 推断已分配。
        # 旧实例 roster 未包含此卡时，保留原 assign 路径。
        try:
            roster = ctx.client.get(f"/api/campaigns/{ctx.campaign_id}/characters").get("roster", [])
            assigned = any(c.get("id") == character["id"] for member in roster
                           for c in member.get("characters", []))
        except Exception:
            assigned = False
        if assigned:
            return {**created, "assigned": True}
        try:
            ctx.client.post(f"/api/characters/{character['id']}/assign", {"campaignId": ctx.campaign_id})
        except Exception as exc:
            raise PartialFailure(
                f"角色 {character['id']} 已创建但 roster 分配失败；请在 UI 分配此角色，勿重复创建: {exc}",
                {"created": True, "assigned": False, "character": character}) from exc
        return {**created, "assigned": True}

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
    def session_manage(action: str, notes: str | None = None, save_state: bool = True) -> dict:
        """场次管理（DM only）。notes/save_state 仅 end 使用；notes 为全桌共享摘要。
        end 空串不清空已有笔记，清空请用 session_notes_update。"""
        ctx = get_ctx()
        action = action.strip().lower()
        allowed = {"start", "pause", "end"}
        if action not in allowed:
            raise ValueError(f"action 必须是 {sorted(allowed)} 之一")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
            raise ValueError("notes 必须为最多 2000 字符的字符串")
        if action != "end" and (notes is not None or save_state is not True):
            raise ValueError("notes/save_state 仅用于 end")
        path = f"/api/campaigns/{ctx.campaign_id}"
        if action == "start":
            return ctx.client.post(f"{path}/sessions")
        campaign = ctx.client.get(path).get("campaign", {})
        active = campaign.get("activeSession")
        if not isinstance(active, dict) or not active.get("id"):
            raise ValueError("当前战役没有活动场次，无法暂停或结束")
        payload = {}
        if action == "end":
            payload["saveState"] = save_state
            if notes is not None:
                payload["notes"] = notes
        return ctx.client.put(f"{path}/sessions/{active['id']}/{action}", payload)
