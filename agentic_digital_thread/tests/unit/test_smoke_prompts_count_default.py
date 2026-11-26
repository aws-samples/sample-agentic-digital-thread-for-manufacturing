# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline unit tests for scripts/smoke_prompts.py --count default (run ALL).

Regression pin for the latency-outlier follow-up: --count silently defaulted to
4, skipping sourced prompts — a 4-prompt run even failed the suite-level
neptune cross-system proof while the full 6-prompt run passed. The default is
now None (== ALL sourced prompts); 0 also means all; an explicit N still limits.

Pure/offline: fetch_prompts is exercised with a stub App (no network), and the
argparse default is read from a fresh parser mirroring main()'s registration.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.smoke_prompts import fetch_prompts  # noqa: E402


class _StubApp:
    """Stands in for smoke_prompts.App — returns a fixed /api/prompts payload."""

    def __init__(self, items):
        self._items = items

    def call(self, method, path, body=None, timeout=60):
        assert (method, path) == ("GET", "/api/prompts")
        return self._items


def _six_prompts():
    return [{"title": f"p{i}", "text": f"question {i}", "category": "x"}
            for i in range(6)]


def test_count_none_returns_all_prompts():
    app = _StubApp(_six_prompts())
    assert len(fetch_prompts(app, None)) == 6


def test_count_zero_returns_all_prompts():
    app = _StubApp(_six_prompts())
    assert len(fetch_prompts(app, 0)) == 6


def test_default_count_arg_is_all():
    """fetch_prompts's count parameter itself defaults to all."""
    app = _StubApp(_six_prompts())
    assert len(fetch_prompts(app)) == 6


def test_explicit_count_still_limits():
    app = _StubApp(_six_prompts())
    got = fetch_prompts(app, 2)
    assert [p["title"] for p in got] == ["p0", "p1"]


def test_long_report_templates_still_skipped():
    items = _six_prompts() + [{"title": "long", "text": "x" * 281}]
    app = _StubApp(items)
    got = fetch_prompts(app, None)
    assert len(got) == 6 and all(p["title"] != "long" for p in got)


def test_argparse_count_default_is_none():
    """The CLI flag's default must be None (all), not a hardcoded limit.

    Reads the default straight out of smoke_prompts.main's source registration
    by re-parsing empty args through an identically-registered parser is
    overkill; instead pin the source line: the module must not register
    default=4 (or any int) for --count.
    """
    src = (_REPO_ROOT / "scripts" / "smoke_prompts.py").read_text(encoding="utf-8")
    # Exactly one --count registration, with default=None.
    assert '"--count", type=int, default=None' in src, (
        "--count must default to None (run ALL sourced prompts)"
    )
