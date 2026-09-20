#!/usr/bin/env python3
"""Emit runtime wheel URLs and hashes from uv.lock for the current build platform.

No resolution, downloads, or installation occur here. Docker uses pip's bundled
packaging; a development environment may provide packaging directly. Unsupported
platforms fail rather than fetching an unlocked source-build dependency.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import sys
import tomllib

try:
    from packaging.markers import Marker, default_environment
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename
except ImportError:
    from pip._vendor.packaging.markers import Marker, default_environment
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import parse_wheel_filename


def requirements(lock_path: Path) -> list[str]:
    with lock_path.open('rb') as stream:
        packages = tomllib.load(stream)['package']
    # This lock has one version per name. Fail closed if a future lock branches.
    by_name = {package['name']: package for package in packages}
    if len(by_name) != len(packages):
        raise ValueError('Multiple versions per package require explicit lock-branch selection')
    environment = default_environment()
    pending = deque([('cozyvtt-mcp', '')])
    seen = set()
    selected = set()
    while pending:
        name, extra = pending.popleft()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        package = by_name[name]
        selected.add(name)
        dependencies = list(package.get('dependencies', []))
        dependencies += package.get('optional-dependencies', {}).get(extra, [])
        for dependency in dependencies:
            marker = dependency.get('marker')
            if marker and not Marker(marker).evaluate({**environment, 'extra': extra}):
                continue
            dep_name = dependency['name']
            if dependency.get('version', by_name[dep_name]['version']) != by_name[dep_name]['version']:
                raise ValueError(f'Lock version mismatch for {dep_name}')
            pending.append((dep_name, ''))
            pending.extend((dep_name, e) for e in dependency.get('extra', []))
    priority = {tag: i for i, tag in enumerate(sys_tags())}
    result = []
    for name in sorted(selected - {'cozyvtt-mcp'}):
        candidates = []
        for wheel in by_name[name].get('wheels', []):
            _, _, _, tags = parse_wheel_filename(wheel['url'].rsplit('/', 1)[-1])
            ranks = [priority[tag] for tag in tags if tag in priority]
            if ranks:
                candidates.append((min(ranks), wheel['url'], wheel['hash']))
        if not candidates:
            raise ValueError(f'No locked wheel for {name} on this Python/platform')
        _, url, digest = min(candidates)
        if not digest.startswith('sha256:'):
            raise ValueError(f'Expected SHA-256 for {name}')
        result.append(f'{name} @ {url} --hash={digest}')
    return result


if __name__ == '__main__':
    lock = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / 'uv.lock'
    print('\n'.join(requirements(lock)))
