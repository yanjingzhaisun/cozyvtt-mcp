#!/usr/bin/env python3
"""Check public tool metadata offline through a real FastMCP in-memory client."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HINTS = ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint')


async def check() -> None:
    from fastmcp import Client
    import server

    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    assert len(tools) == 37, f'Expected 37 tools, got {len(tools)}'
    assert len({tool.name for tool in tools}) == 37, 'Duplicate tool names'
    described = total = annotated = sufficient = 0
    failures = []
    print(f'{"Tool":26} {"Params":7} {"Hints":6} {"Doc chars/min":13} Result')
    for tool in sorted(tools, key=lambda item: item.name):
        # Serialize the actual MCP wire names, independent of SDK Python aliases.
        wire = tool.model_dump(by_alias=True)
        properties = wire['inputSchema'].get('properties', {})
        count = sum(isinstance(p.get('description'), str) and bool(p['description'].strip())
                    for p in properties.values())
        annotations = wire.get('annotations') or {}
        hints = sum(type(annotations.get(hint)) is bool for hint in HINTS)
        length = len((tool.description or '').strip())
        minimum = 200 if properties else 120
        valid = count == len(properties) and hints == 4 and length >= minimum
        described += count
        total += len(properties)
        annotated += hints == 4
        sufficient += length >= minimum
        print(f'{tool.name:26} {count:2}/{len(properties):<4} {hints}/4    '
              f'{length:4}/{minimum:<8} {"PASS" if valid else "FAIL"}')
        if not valid:
            failures.append(tool.name)
    print(f'Parameter descriptions: {described}/{total} = {100 * described / total:.2f}%')
    print(f'Tool annotations: {annotated}/{len(tools)} = {100 * annotated / len(tools):.2f}%')
    print(f'Description length: {sufficient}/{len(tools)} = {100 * sufficient / len(tools):.2f}%')
    assert not failures, f'Metadata coverage failed: {", ".join(failures)}'
    assert server._ctx is None, 'Tool discovery must not initialize campaign context'


def main() -> None:
    # Even with credentials in the parent shell, listing must remain offline.
    with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden during schema discovery')), \
         patch('socket.create_connection', side_effect=AssertionError('Network forbidden during schema discovery')):
        asyncio.run(asyncio.wait_for(check(), timeout=15))


if __name__ == '__main__':
    main()
