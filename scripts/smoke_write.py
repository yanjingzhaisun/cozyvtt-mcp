#!/usr/bin/env python3
"""手动写冒烟：独立测试战役、COZYVTT_SMOKE=1；验证聊天及公骰/暗骰广播。"""
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import Ctx, register_all
from scripts.smoke import FakeMCP


def matches_roll(payload, user_id, purpose, expression, secret):
    """按上游 1.2.2 广播字段匹配，避免把别人的相同骰式当成自己的结果。"""
    return (isinstance(payload, dict) and bool(payload.get("id"))
            and payload.get("userId") == user_id and payload.get("purpose") == purpose
            and payload.get("expression") == expression and payload.get("secret") is secret)


def run_checks(ctx) -> int:
    ctx.ensure_ws()
    user_id = (ctx.auth.user or {}).get("id")
    if not user_id:
        raise RuntimeError("登录响应没有 user.id，无法验证广播归属")
    mcp = FakeMCP()
    register_all(mcp, lambda: ctx)
    tools = mcp.tools
    marker = f"smoke-{uuid4()}"
    cursor = ctx.ws.poll()["high_water_seq"]
    chat = tools["chat_send"](content=f"[冒烟] {marker}", type="DM")
    rolls = {}
    for secret in (False, True):
        purpose = f"{marker}-{secret}"
        rolls[secret] = tools["dice_roll"]("1d20+3", is_secret=secret, purpose=purpose)
    if not chat["ok"] or not all(r["ok"] for r in rolls.values()):
        print("发送失败:", chat, rolls)
        return 1
    print("聊天、公骰、暗骰已发送，等待业务广播确认（pending）")
    found_chat = False
    found_rolls = set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        out = tools["events_poll"](since=cursor, limit=200)
        if not out["ok"]:
            print("轮询失败:", out)
            return 1
        batch = out["data"]
        if batch["gap"] or batch["cursor_reset"]:
            print("事件存在缺口，无法证明本次冒烟成功")
            return 1
        cursor = batch["next_seq"]
        for event in batch["events"]:
            payload = event["payload"]
            if event["event"] == "system.error":
                print("WS 错误:", payload)
                return 1
            if (event["event"] == "chat.message" and isinstance(payload, dict)
                    and payload.get("userId") == user_id
                    and payload.get("content") == f"[冒烟] {marker}"):
                found_chat = True
            if event["event"] == "dice.rolled":
                for secret in (False, True):
                    if matches_roll(payload, user_id, f"{marker}-{secret}", "1d20+3", secret):
                        found_rolls.add(secret)
        if found_chat and len(found_rolls) == 2:
            print("PASS：收到自己的聊天、公骰和标记为 secret 的暗骰")
            # TODO(#25): 单个 DM 连接不能证明玩家未收到暗骰；端到端隐私验收
            # 需要独立 PLAYER 账号/连接。离线契约测试验证 secret 字段与广播分支。
            return 0
        time.sleep(0.1)
    print("FAIL：未完整收到自己的聊天、公骰和暗骰")
    return 1


def main() -> int:
    if os.environ.get("COZYVTT_SMOKE") != "1":
        print("需要 COZYVTT_SMOKE=1 显式开启，只用于测试战役")
        return 2
    ctx = Ctx.from_env()
    try:
        ctx.auth.login()
        return run_checks(ctx)
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
