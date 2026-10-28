"""Validate a stable release version and stamp the four tracked version files."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def normalize(value: str) -> str:
    version = value.removeprefix('v')
    component = r'(?:0|[1-9][0-9]*)'
    if not re.fullmatch(rf'{component}\.{component}\.{component}', version):
        raise ValueError('Use X.Y.Z or vX.Y.Z, for example 0.2.0 (no prerelease suffix).')
    if any(int(part) > 2**64-1 for part in version.split('.')):
        raise ValueError('Version component exceeds the Cargo/SemVer limit.')
    return version


def stamp(root: Path, version: str) -> None:
    version = normalize(version)
    changes = {}
    for name, pattern, replacement in (
        ('pyproject.toml', r'(?m)^version = "[^"]+"$', f'version = "{version}"'),
        ('daemon/Cargo.toml', r'(?m)^version = "[^"]+"$', f'version = "{version}"'),
        ('cwm_panel/__init__.py', r'(?m)^__version__ = "[^"]+"$', f'__version__ = "{version}"'),
        ('daemon/Cargo.lock', r'(\[\[package\]\]\nname = "cairnd"\nversion = ")[^"]+(")', rf'\g<1>{version}\2'),
    ):
        path = root / name
        content, count = re.subn(pattern, replacement, path.read_text())
        if count != 1:
            raise ValueError(f'Expected exactly one version in {name}; found {count}.')
        changes[path] = content
    # Validate all files before writing any of them.
    for path, content in changes.items():
        path.write_text(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('--check', action='store_true', help='validate without changing files')
    args = parser.parse_args()
    try:
        version = normalize(args.version)
        if not args.check:
            stamp(ROOT, version)
    except ValueError as error:
        parser.exit(1, f'{error}\n')
    print(version)


if __name__ == '__main__':
    main()
