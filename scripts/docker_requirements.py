#!/usr/bin/env python3
"""Emit runtime wheel URLs and hashes from uv.lock for the current build platform.

No resolution, downloads, or installation occur here. Docker uses pip's bundled
packaging; a development environment may provide packaging directly. Unsupported
platforms fail rather than fetching an unlocked source-build dependency.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import argparse
import re
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

# uv.lock records the index that produced it, which may be a regional mirror
# (e.g. mirrors.aliyun.com/pypi/packages/...). Mirrors serve byte-identical blobs, so the
# recorded sha256 stays valid when the base changes — pip's --require-hashes verifies it.
# Default to the canonical CDN so builds outside the maintainer's network work; pass
# --wheel-base https://mirrors.aliyun.com/pypi/packages for faster builds from mainland China.
CANONICAL_BASE = "https://files.pythonhosted.org/packages"


def canonical_url(url: str, base: str) -> str:
    """Rewrite a wheel URL onto `base`, keeping the blob path. "" keeps the lock URL."""
    if not base:
        return url
    match = re.match(r"https?://[^?]+(/packages/.+)$", url)
    if not match:
        return url
    path = match.group(1)
    base = base.rstrip("/")
    if base.endswith("/packages"):
        path = path[len("/packages"):]
    return f"{base}{path}"


def requirements(lock_path: Path, wheel_base: str = CANONICAL_BASE) -> list[str]:
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
        result.append(f'{name} @ {canonical_url(url, wheel_base)} --hash={digest}')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Emit locked runtime wheel URLs for the current platform.')
    parser.add_argument('lock', nargs='?', default=None, help='path to uv.lock (default: repo root)')
    parser.add_argument('--wheel-base', default=CANONICAL_BASE,
                        help=f'base URL serving the blobs, or "" to keep the lock URL (default: {CANONICAL_BASE})')
    options = parser.parse_args()
    lock_path = Path(options.lock) if options.lock else Path(__file__).resolve().parent.parent / 'uv.lock'
    print('\n'.join(requirements(lock_path, options.wheel_base)))
