#!/usr/bin/env python3
"""写操作冒烟（主人手动执行）：chat_send + dice_roll + events_poll 验证能读到自己的骰子。

⚠️ 会向测试战役写入聊天与骰子记录。只对测试战役运行。
用法同 smoke.py，需 COZYVTT_SMOKE=1。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth import AuthManager
from client import CozyClient
from tools import Ctx, register_all
from ws_listener import WSListener


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def main() -> int:
    if os.environ.get("COZYVTT_SMOKE") != "1":
        print("需要 COZYVTT_SMOKE=1 显式开启")
        return 2

    base = os.environ["COZYVTT_URL"]
    auth = AuthManager(base, os.environ["COZYVTT_EMAIL"], os.environ["COZYVTT_PASSWORD"])
    cid = os.environ["COZYVTT_CAMPAIGN_ID"]
    auth.login()  # 只登录一次
    print("[login] ok")

    ctx = Ctx(CozyClient(base, auth), auth, WSListener(base, cid, auth.cookie_header), cid)
    ctx.ensure_ws()
    # 等 authenticate 完成
    for _ in range(30):
        if ctx.ws.authenticated:
            break
        time.sleep(0.2)
    print(f"[ws] connected={ctx.ws.connected} authenticated={ctx.ws.authenticated}")

    mcp = FakeMCP()
    register_all(mcp, lambda: ctx)
    tools = mcp.tools

    failed = 0
    marker = f"smoke-{int(time.time())}"

    r = tools["chat_send"](content=f"[冒烟] {marker}", type="DM")
    print(f"[chat_send] {'PASS' if r.get('ok') else 'FAIL ' + str(r.get('error'))}")
    failed += 0 if r.get("ok") else 1

    r = tools["dice_roll"](expression="1d20+3", is_secret=False)
    print(f"[dice_roll] {'PASS' if r.get('ok') else 'FAIL ' + str(r.get('error'))}")
    failed += 0 if r.get("ok") else 1

    # 等 WS 广播回来
    found_chat = found_dice = False
    deadline = time.time() + 10
    while time.time() < deadline and not (found_chat and found_dice):
        out = tools["events_poll"](since=0, limit=200)
        if not out.get("ok"):
            break
        for e in out["data"]["events"]:
            if e["event"] == "chat.message" and marker in str(e.get("payload")):
                found_chat = True
            if e["event"] in ("dice.rolled", "dice.rolled.secret") and "1d20+3" in str(e.get("payload")):
                found_dice = True
        if not (found_chat and found_dice):
            time.sleep(0.5)

    print(f"[events_poll 读到自己的聊天] {'PASS' if found_chat else 'FAIL'}")
    print(f"[events_poll 读到自己的骰子] {'PASS' if found_dice else 'FAIL'}")
    failed += (not found_chat) + (not found_dice)

    ctx.ws.stop()
    auth.stop()
    print("\n写冒烟", "全部通过" if not failed else f"{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
