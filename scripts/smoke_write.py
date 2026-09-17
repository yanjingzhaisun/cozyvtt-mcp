#!/usr/bin/env python3
"""Manual write smoke test: use a separate test campaign and COZYVTT_SMOKE=1; verify chat and public/secret roll broadcasts."""
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import Ctx, register_all
from scripts.smoke import FakeMCP


def matches_roll(payload, user_id, purpose, expression, secret):
    """Match upstream 1.2.2 broadcast fields to avoid confusing another user's identical expression with our roll."""
    return (isinstance(payload, dict) and bool(payload.get("id"))
            and payload.get("userId") == user_id and payload.get("purpose") == purpose
            and payload.get("expression") == expression and payload.get("secret") is secret)


def run_checks(ctx) -> int:
    ctx.ensure_ws()
    user_id = (ctx.auth.user or {}).get("id")
    if not user_id:
        raise RuntimeError("Login response is missing user.id; cannot verify broadcast ownership")
    mcp = FakeMCP()
    register_all(mcp, lambda: ctx)
    tools = mcp.tools
    marker = f"smoke-{uuid4()}"
    cursor = ctx.ws.poll()["high_water_seq"]
    chat = tools["chat_send"](content=f"[smoke] {marker}", type="DM")
    rolls = {}
    for secret in (False, True):
        purpose = f"{marker}-{secret}"
        rolls[secret] = tools["dice_roll"]("1d20+3", is_secret=secret, purpose=purpose)
    if not chat["ok"] or not all(r["ok"] for r in rolls.values()):
        print("Dispatch failed:", chat, rolls)
        return 1
    print("Chat, public roll, and secret roll sent; awaiting business broadcasts (pending)")
    found_chat = False
    found_rolls = set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        out = tools["events_poll"](since=cursor, limit=200)
        if not out["ok"]:
            print("Polling failed:", out)
            return 1
        batch = out["data"]
        if batch["gap"] or batch["cursor_reset"]:
            print("Event gap detected; cannot confirm smoke test success")
            return 1
        cursor = batch["next_seq"]
        for event in batch["events"]:
            payload = event["payload"]
            if event["event"] == "system.error":
                print("WS error:", payload)
                return 1
            if (event["event"] == "chat.message" and isinstance(payload, dict)
                    and payload.get("userId") == user_id
                    and payload.get("content") == f"[smoke] {marker}"):
                found_chat = True
            if event["event"] == "dice.rolled":
                for secret in (False, True):
                    if matches_roll(payload, user_id, f"{marker}-{secret}", "1d20+3", secret):
                        found_rolls.add(secret)
        if found_chat and len(found_rolls) == 2:
            print("PASS: received our chat, public roll, and roll marked secret")
            # TODO(#25): A single DM connection cannot prove players did not receive secret rolls; end-to-end privacy verification
            # requires a separate PLAYER account/connection. Offline contracts check the secret field and broadcast branch.
            return 0
        time.sleep(0.1)
    print("FAIL: did not receive all of our chat, public roll, and secret roll")
    return 1


def main() -> int:
    if os.environ.get("COZYVTT_SMOKE") != "1":
        print("Set COZYVTT_SMOKE=1 explicitly; use only a test campaign")
        return 2
    ctx = Ctx.from_env()
    try:
        ctx.auth.login()
        return run_checks(ctx)
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
