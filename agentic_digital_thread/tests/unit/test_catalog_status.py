# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for the S4.1 system-onboarding status rename (T7).

The not-yet-onboarded lifecycle state was renamed 'available' -> 'registered'.
These tests pin the authoritative in-memory SystemCatalog vocabulary: fresh seeds
initialize to 'registered', update_status -> 'onboarded' is reflected by the
onboarded reader, reset_all returns everything to 'registered', and the string
'available' is never a system's status.

No AWS, no Neptune, no DB, no LLM, no fixtures — runs offline in the default
(unmarked) pytest lane. Mirrors tests/unit/test_metric_layer.py.
"""
from __future__ import annotations

from agents.catalog import SystemCatalog

try:
    from unit._demo_catalog import DEMO_SYSTEMS
except ImportError:  # pragma: no cover - alternate invocation path
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from _demo_catalog import DEMO_SYSTEMS


def _fresh_catalog() -> SystemCatalog:
    """A brand-new catalog seeded from the test-local demo systems fixture."""
    return SystemCatalog(DEMO_SYSTEMS)


def test_fresh_seeds_are_all_registered():
    catalog = _fresh_catalog()
    systems = catalog.get_all()
    assert systems, "expected at least one seed system"
    assert all(s['status'] == 'registered' for s in systems), \
        [(s['id'], s['status']) for s in systems if s['status'] != 'registered']


def test_no_seed_status_is_available():
    catalog = _fresh_catalog()
    assert all(s['status'] != 'available' for s in catalog.get_all()), \
        [s['id'] for s in catalog.get_all() if s['status'] == 'available']


def test_update_status_to_onboarded_is_read_by_reader():
    catalog = _fresh_catalog()
    sid = catalog.get_all()[0]['id']

    assert sid not in catalog.get_onboarded_ids()

    catalog.update_status(sid, 'onboarded')

    assert sid in catalog.get_onboarded_ids()
    assert catalog.get(sid)['status'] == 'onboarded'


def test_reset_all_returns_everything_to_registered():
    catalog = _fresh_catalog()
    # Onboard a couple of systems first.
    ids = [s['id'] for s in catalog.get_all()][:2]
    for sid in ids:
        catalog.update_status(sid, 'onboarded')
    assert catalog.get_onboarded_ids()  # sanity: some are onboarded

    catalog.reset_all()

    systems = catalog.get_all()
    assert all(s['status'] == 'registered' for s in systems), \
        [(s['id'], s['status']) for s in systems if s['status'] != 'registered']
    assert catalog.get_onboarded_ids() == set()


def test_remove_drops_single_entry_and_leaves_others():
    """Per-system reset removes the registration entirely (no connection-less stub)."""
    catalog = _fresh_catalog()
    ids = [s['id'] for s in catalog.get_all()]
    assert ids, "expected seed systems"
    target = ids[0]
    assert catalog.remove(target) is True
    remaining = [s['id'] for s in catalog.get_all()]
    assert target not in remaining
    assert remaining == ids[1:], "only the target entry should be removed, order preserved"


def test_remove_unknown_returns_false_and_no_change():
    catalog = _fresh_catalog()
    before = [s['id'] for s in catalog.get_all()]
    assert catalog.remove("no-such-system") is False
    assert [s['id'] for s in catalog.get_all()] == before
