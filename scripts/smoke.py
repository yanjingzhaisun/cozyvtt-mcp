#!/usr/bin/env python3
"""只读冒烟：campaign_status / chat_read / map_list / character_list / creature_search。

用法：
    COZYVTT_SMOKE=1 COZYVTT_URL=... COZYVTT_EMAIL=... COZYVTT_PASSWORD=... \
    COZYVTT_CAMPAIGN_ID=... .venv/bin/python scripts/smoke.py

只登录一次（auth 限流 5次/15min/IP），全部通过退出码 0。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth import AuthManager
from client import CozyClient
from tools import register_all


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def valid_result(name, result, campaign_id):
    if not result.get("ok") or not isinstance(result.get("data"), dict):
        return False
    data = result["data"]
    if name == "campaign_status":
        campaign = data.get("campaign")
        return (isinstance(campaign, dict) and campaign.get("id") == campaign_id
                and bool(campaign.get("status")) and data.get("health", {}).get("reachable") is True)
    key = {"chat_read": "messages", "map_list": "maps", "character_list": "roster",
           "creature_search": "creatures"}[name]
    return isinstance(data.get(key), list)


def main() -> int:
    if os.environ.get("COZYVTT_SMOKE") != "1":
        print("需要 COZYVTT_SMOKE=1 显式开启")
        return 2

    base = os.environ["COZYVTT_URL"]
    email = os.environ["COZYVTT_EMAIL"]
    password = os.environ["COZYVTT_PASSWORD"]
    cid = os.environ["COZYVTT_CAMPAIGN_ID"]

    auth = AuthManager(base, email, password)
    client = CozyClient(base, auth)

    from tools import Ctx

    class NoWS:
        def start(self):
            raise AssertionError("只读冒烟不应连接 WS")

        def stop(self):
            pass
    ctx = Ctx(client, auth, NoWS(), cid)

    mcp = FakeMCP()
    register_all(mcp, lambda: ctx)
    tools = mcp.tools

    try:
        # 只登录一次
        auth.login()
        print(f"[login] ok, user={auth.user.get('displayName') if auth.user else '?'}")

        checks = [
            ("campaign_status", lambda: tools["campaign_status"]()),
            ("chat_read", lambda: tools["chat_read"](limit=5)),
            ("map_list", lambda: tools["map_list"]()),
            ("character_list", lambda: tools["character_list"]()),
            ("creature_search", lambda: tools["creature_search"](search="", limit=3)),
        ]
        failed = 0
        for name, fn in checks:
            r = fn()
            if valid_result(name, r, cid):
                data = r["data"]
                hint = ""
                if name == "campaign_status":
                    c = data.get("campaign", {})
                    hint = f"战役「{c.get('name')}」status={c.get('status')} map={c.get('currentMapId')}"
                elif name == "map_list":
                    hint = f"{len(data.get('maps', []))} 张图"
                elif name == "chat_read":
                    hint = f"total={data.get('pagination', {}).get('total')}"
                elif name == "character_list":
                    hint = f"roster={len(data.get('roster', []))} 人"
                elif name == "creature_search":
                    hint = f"keys={list(data.keys())[:4]}"
                print(f"[{name}] PASS {hint}")
            else:
                failed += 1
                print(f"[{name}] FAIL {r.get('error', '响应结构不符合契约')}")

        if failed:
            print(f"\n{failed}/{len(checks)} 失败")
            return 1
        print(f"\n全部 {len(checks)} 项只读冒烟通过")
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
