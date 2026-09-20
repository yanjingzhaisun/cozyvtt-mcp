#!/usr/bin/env python3
"""Measure offline tools/list JSON bytes by preset; tokens are estimated as bytes // 4.

Uses an interactive initialize -> initialized -> tools/list handshake.
--json emits machine-readable measurements; --probe checks three stdio selections.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tools.toolsets import PRESETS

FAMILIES = PRESETS


def list_tools(args: tuple[str, ...] = ()) -> list[dict]:
    # Discovery must never access an instance, even if credentials are present.
    bootstrap = (
        "import socket,runpy; "
        "socket.socket.connect=socket.create_connection=lambda *a,**k: "
        "(_ for _ in ()).throw(RuntimeError('network disabled')); "
        f"runpy.run_path({str(REPO / 'server.py')!r},run_name='__main__')"
    )
    env = os.environ.copy()
    env.pop("COZYVTT_MCP_TOOLSETS", None)
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap, *args], cwd=REPO, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdin and proc.stdout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)

    def request(obj: dict, want_id: int) -> dict:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        if not selector.select(timeout=20):
            raise TimeoutError(f"server did not reply to id={want_id}")
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed stdout before replying to id={want_id}")
        msg = json.loads(line)
        if msg.get("id") != want_id or "error" in msg:
            raise RuntimeError(f"Unexpected MCP response: {msg}")
        return msg

    try:
        request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "measure", "version": "1"}}}, 1)
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        reply = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2)
        proc.stdin.close()
        proc.wait(timeout=20)
        if proc.returncode:
            raise RuntimeError(f"server exited with status {proc.returncode}")
        return reply["result"]["tools"]
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()
        if not proc.stdin.closed:
            proc.stdin.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    tools = list_tools()
    assert {t["name"] for t in tools} == PRESETS["all"]
    if args.probe:
        print(f"no arguments: {len(tools)} tools (registry: {len(PRESETS['all'])})")
        for name in ("play", "docs"):
            selected = list_tools(("--toolsets", name))
            assert {t["name"] for t in selected} == PRESETS[name]
            print(f"--toolsets {name}: {len(selected)} tools (preset: {len(PRESETS[name])})")
        return 0
    sizes = {t["name"]: len(json.dumps(t, ensure_ascii=False).encode("utf-8")) for t in tools}
    total = sum(sizes.values())
    out = {"tools_total": len(tools), "bytes_total": total, "families": {}}
    for name, names in FAMILIES.items():
        size = sum(sizes[n] for n in names)
        out["families"][name] = {"tools": len(names), "bytes": size, "tokens": size // 4,
                                  "saving_pct": round((total - size) * 100 / total)}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("Preset   Tools   JSON bytes   Est. tokens   Savings")
        for name, row in out["families"].items():
            print(f"{name:<8} {row['tools']:>5} {row['bytes']:>12} {row['tokens']:>13} {row['saving_pct']:>8}%")
        print("Bytes sum UTF-8 JSON tool definitions; exclude the JSON-RPC envelope. Tokens = bytes // 4.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
