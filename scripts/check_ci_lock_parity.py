#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CI lock parity — the unit lane must test the VERSIONS the image deploys.

CI installs the app with ``pip install -e 'agentic_digital_thread[test]'``,
which resolves pyproject's ranges live, while the image installs
``requirements/app-py312-linux-arm64.lock`` with ``--require-hashes``. The lock
is arm64-only (hashes for arm64 wheels), so it cannot simply be installed on an
x86_64 runner; this gate closes the drift window another way: every
``name==version`` in the lock that is installed in the current environment
MUST be installed at exactly that version. A mismatch means CI is testing a
different dependency than production runs.

Usage: python3 scripts/check_ci_lock_parity.py [--lock PATH] [--allow NAME ...]
Exit 0 on parity, 1 on drift, 2 on usage error. Offline; stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "agentic_digital_thread" / "requirements" / "app-py312-linux-arm64.lock"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s\\;]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line.strip())
        if m:
            pins[_norm(m.group(1))] = m.group(2)
    return pins


def installed_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            out[_norm(name)] = dist.version
    return out


def compare(pins: dict[str, str], installed: dict[str, str], allow: set[str]):
    drift, missing = [], []
    for name, want in sorted(pins.items()):
        have = installed.get(name)
        if have is None:
            missing.append(name)
        elif have != want and name not in allow:
            drift.append((name, want, have))
    return drift, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--allow", nargs="*", default=[],
                        help="package names allowed to differ (documented exceptions only)")
    parser.add_argument("--emit-constraints", action="store_true",
                        help="print the lock as a hash-free pip constraints file "
                             "(name==version per line) and exit — CI feeds it to "
                             "`pip install -c` so the editable install resolves to the "
                             "lock's versions on any platform")
    args = parser.parse_args(argv)
    if not args.lock.is_file():
        print(f"lock-parity: lock not found: {args.lock}", file=sys.stderr)
        return 2
    pins = lock_pins(args.lock)
    if args.emit_constraints:
        for name, version in sorted(pins.items()):
            print(f"{name}=={version}")
        return 0
    drift, missing = compare(pins, installed_versions(), {_norm(a) for a in args.allow})
    checked = len(pins) - len(missing)
    for name, want, have in drift:
        print(f"lock-parity: DRIFT {name}: lock={want} installed={have}")
    print(f"lock-parity: {checked} locked packages checked, {len(missing)} not installed here, "
          f"{len(drift)} drift")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
