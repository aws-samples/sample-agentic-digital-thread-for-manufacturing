# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline contract tests for the ADR-0066 metric-definition graph store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess  # nosec B404 - fixed local interpreter command only
import sys

import pytest

import agents.metric_definition_store as store
import agents.metamodel as metamodel


def _canonical(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse(raw: str | dict) -> dict:
    doc = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if not isinstance(doc, dict):
        raise ValueError("document must be an object")
    for field in ("id", "label", "kind"):
        if not isinstance(doc.get(field), str) or not doc[field]:
            raise ValueError(f"{field} is required")
    return doc


def _doc(metric_id: str = "line-utilization") -> dict:
    return {
        "aggregation": "avg",
        "concept": "utilization",
        "id": metric_id,
        "kind": "direct",
        "label": "Line Utilization",
        "synonyms": ["LU"],
        "unit": "ratio",
    }


def _row(doc: dict) -> dict:
    return {"id": doc["id"], "definition_json": _canonical(doc)}


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch):
    store.invalidate()
    monkeypatch.setattr(store, "_parse_definition", _parse)
    monkeypatch.setattr(store, "_canonical_json", _canonical)
    monkeypatch.setattr(store, "_code_metric_ids", lambda: frozenset())
    monkeypatch.setenv("METRIC_DEFINITION_TTL_SECONDS", "300")
    yield
    store.invalidate()


def test_snapshot_makes_exactly_one_graph_call_per_ttl_window(monkeypatch):
    calls: list[str] = []
    now = [1000.0]

    def query(cypher, parameters=None):
        calls.append(cypher)
        return [_row(_doc())]

    monkeypatch.setattr(store, "neptune_query", query)
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])

    assert store.load_definitions() == {"line-utilization": _doc()}
    assert store.load_definitions() == {"line-utilization": _doc()}
    assert len(calls) == 1
    assert "LIMIT 500" in calls[0]

    now[0] += 301
    assert store.load_definitions() == {"line-utilization": _doc()}
    assert len(calls) == 2


def test_invalidate_forces_reload_inside_ttl(monkeypatch):
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [_row(_doc())]

    monkeypatch.setattr(store, "neptune_query", query)
    assert store.load_definitions()
    store.invalidate()
    assert store.load_definitions()
    assert calls == 2


def test_successful_empty_graph_uses_sentinel_not_fabricated_failure(monkeypatch):
    monkeypatch.setattr(
        store, "neptune_query",
        lambda *_a, **_k: [{"id": None, "definition_json": None}],
    )
    assert store.load_definitions() == {}
    assert store.shadowed_or_invalid() == []


def test_cold_load_failure_raises_and_never_caches_empty(monkeypatch):
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(store, "neptune_query", query)
    with pytest.raises(RuntimeError, match="graph unavailable"):
        store.load_definitions()
    # Still RAISES on every caller — the cooldown changes where the raise comes
    # from, never whether it happens (ADR-0066 D4).
    with pytest.raises(RuntimeError, match="graph unavailable"):
        store.load_definitions()
    assert calls == 1
    with pytest.raises(RuntimeError):
        store.load_definitions()
    assert calls == 1


@pytest.mark.parametrize("raw,fragment", [
    ("inf", "finite"),
    ("Infinity", "finite"),
    ("-inf", "finite"),
    ("nan", "finite"),
    ("0", "positive"),
    ("-1", "positive"),
    ("-0.5", "positive"),
    ("", "number of seconds"),
    ("abc", "number of seconds"),
    ("300s", "number of seconds"),
])
def test_ttl_must_be_a_finite_positive_number_of_seconds(monkeypatch, raw, fragment):
    """An infinite TTL is the ABSENCE of a staleness bound, not a large one.

    ADR-0066 D4 states the staleness guarantee AS the TTL, so ``inf`` (which
    would freeze the snapshot forever and stop cross-process convergence) raises
    exactly as hard as ``nan``, zero, a negative value, or garbage — the
    Athena-budget posture.
    """
    monkeypatch.setenv("METRIC_DEFINITION_TTL_SECONDS", raw)
    monkeypatch.setattr(
        store, "neptune_query",
        lambda *_a, **_k: pytest.fail("a bad TTL must fail before the graph read"),
    )
    with pytest.raises(ValueError, match="METRIC_DEFINITION_TTL_SECONDS") as excinfo:
        store.load_definitions()
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize("raw", ["1", "0.5", "300", "86400"])
def test_finite_positive_ttl_values_are_accepted(monkeypatch, raw):
    monkeypatch.setenv("METRIC_DEFINITION_TTL_SECONDS", raw)
    monkeypatch.setattr(store, "neptune_query", lambda *_a, **_k: [_row(_doc())])
    assert store.load_definitions() == {"line-utilization": _doc()}


def test_cold_outage_makes_at_most_one_graph_attempt_per_cooldown_window(
    monkeypatch,
):
    """The pre-check vocabulary build and the D6 usage bump both
    read this store directly on every metric-shaped turn, so a cold outage used
    to cost ~2 failing graph reads PER TURN. One attempt per window, all callers.
    """
    now = [1000.0]
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(store, "neptune_query", query)
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])

    # Ten turns, two direct store callers each.
    for _turn in range(10):
        for _caller in range(2):
            with pytest.raises(RuntimeError):
                store.load_definitions()
        now[0] += 1.0

    assert calls == 1

    # One further attempt only after the cooldown window elapses.
    now[0] += store.COLD_FAILURE_COOLDOWN_SECONDS
    with pytest.raises(RuntimeError):
        store.load_definitions()
    assert calls == 2


def test_cooldown_raise_preserves_the_original_failure_detail(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        store, "neptune_query",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("neptune 503 boom")),
    )
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])

    with pytest.raises(ValueError, match="neptune 503 boom"):
        store.load_definitions()

    now[0] += 1.0
    with pytest.raises(RuntimeError) as excinfo:
        store.load_definitions()
    message = str(excinfo.value)
    assert "cold-failure cooldown" in message
    assert "ValueError: neptune 503 boom" in message


def test_cold_cooldown_recovers_after_the_window(monkeypatch):
    now = [1000.0]
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("graph unavailable")
        return [_row(_doc())]

    monkeypatch.setattr(store, "neptune_query", query)
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])

    with pytest.raises(RuntimeError):
        store.load_definitions()
    with pytest.raises(RuntimeError):
        store.load_definitions()
    assert calls == 1

    now[0] += store.COLD_FAILURE_COOLDOWN_SECONDS + 0.1
    assert store.load_definitions() == {"line-utilization": _doc()}
    assert calls == 2
    # A successful load clears the cooldown: the TTL owns the cadence again.
    assert store.load_definitions() == {"line-utilization": _doc()}
    assert calls == 2


def test_invalidate_clears_the_cold_failure_cooldown(monkeypatch):
    """An accept must be able to retry the graph at once, not wait out a cooldown."""
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(store, "neptune_query", query)
    with pytest.raises(RuntimeError):
        store.load_definitions()
    store.invalidate()
    with pytest.raises(RuntimeError, match="graph unavailable"):
        store.load_definitions()
    assert calls == 2


def test_cold_cooldown_is_shorter_than_the_default_ttl():
    assert 0 < store.COLD_FAILURE_COOLDOWN_SECONDS < store._DEFAULT_TTL_SECONDS


def test_warm_snapshot_is_never_subject_to_the_cold_cooldown(monkeypatch):
    """A warm process keeps the stale-serve path — the cooldown is cold-only."""
    now = [1000.0]
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [_row(_doc())]
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(store, "neptune_query", query)
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])
    assert store.load_definitions() == {"line-utilization": _doc()}

    now[0] += 301
    assert store.load_definitions() == {"line-utilization": _doc()}
    now[0] += 301
    assert store.load_definitions() == {"line-utilization": _doc()}
    assert calls == 3


def test_empty_result_is_a_cold_failure_not_an_empty_snapshot(monkeypatch):
    monkeypatch.setattr(store, "neptune_query", lambda *_a, **_k: [])
    with pytest.raises(RuntimeError, match="no sentinel"):
        store.load_definitions()


def test_warm_refresh_failure_serves_stale_and_warns_once_per_ttl(
    monkeypatch, caplog,
):
    now = [1000.0]
    calls = 0

    def query(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [_row(_doc())]
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr(store, "neptune_query", query)
    monkeypatch.setattr(store.time, "monotonic", lambda: now[0])
    assert store.load_definitions() == {"line-utilization": _doc()}

    now[0] += 301
    with caplog.at_level("WARNING", logger=store.__name__):
        assert store.load_definitions() == {"line-utilization": _doc()}
        assert store.load_definitions() == {"line-utilization": _doc()}

    assert calls == 2
    warnings = [r for r in caplog.records if "serving stale snapshot" in r.message]
    assert len(warnings) == 1


def test_invalid_and_code_colliding_rows_are_excluded_and_reported(monkeypatch):
    valid = _doc("graph-only-rate")
    colliding = _doc("code-owned-rate")
    rows = [
        _row(valid),
        _row(colliding),
        {"id": "broken-rate", "definition_json": "{not-json"},
    ]
    monkeypatch.setattr(store, "_code_metric_ids", lambda: frozenset({"code-owned-rate"}))
    monkeypatch.setattr(store, "neptune_query", lambda *_a, **_k: rows)

    assert store.load_definitions() == {"graph-only-rate": valid}
    reported = {row["id"]: row["reason"] for row in store.shadowed_or_invalid()}
    assert reported["code-owned-rate"] == "code-id collision"
    assert reported["broken-rate"].startswith("invalid definition_json:")


def test_node_id_mismatch_and_noncanonical_bytes_are_invalid(monkeypatch):
    mismatch = _doc("inside-id")
    noncanonical = _doc("noncanonical-id")
    rows = [
        {"id": "outside-id", "definition_json": _canonical(mismatch)},
        {"id": "noncanonical-id", "definition_json": json.dumps(noncanonical)},
    ]
    monkeypatch.setattr(store, "neptune_query", lambda *_a, **_k: rows)

    assert store.load_definitions() == {}
    reasons = {row["id"]: row["reason"] for row in store.shadowed_or_invalid()}
    assert "does not match" in reasons["outside-id"]
    assert "not canonical" in reasons["noncanonical-id"]


def test_write_merges_all_c3_properties_and_exact_readback(monkeypatch):
    doc = _doc()
    writes: list[tuple[str, dict]] = []

    def write(cypher, parameters=None):
        writes.append((cypher, dict(parameters)))
        return True

    monkeypatch.setattr(store, "neptune_write", write)
    monkeypatch.setattr(
        store, "neptune_query",
        lambda cypher, parameters=None: [{"definition_json": _canonical(doc)}],
    )

    assert store.write_definition(doc, actor="steward-user", version=3) == \
        "metricdef:line-utilization"
    assert len(writes) == 1
    cypher, params = writes[0]
    assert "MERGE (m:MetricDefinition {id: $id})" in cypher
    assert not cypher.startswith("CREATE")
    assert params["definition_json"] == _canonical(doc)
    assert params["synonyms_json"] == '["LU"]'
    assert params["source"] == "custom"
    assert params["actor"] == "steward-user"
    assert params["version"] == 3
    for prop in (
        "label", "unit", "kind", "definition_json", "synonyms_json", "source",
        "created_by", "version", "created_at", "updated_at",
    ):
        assert f"m.{prop}" in cypher


def test_write_readback_mismatch_raises(monkeypatch):
    monkeypatch.setattr(store, "neptune_write", lambda *_a, **_k: True)
    monkeypatch.setattr(
        store, "neptune_query",
        lambda *_a, **_k: [{"definition_json": _canonical(_doc("different-id"))}],
    )
    with pytest.raises(RuntimeError, match="exact readback mismatch"):
        store.write_definition(_doc(), actor="steward-user", version=1)


def test_write_failure_raises_without_readback(monkeypatch):
    reads = 0

    def query(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return []

    monkeypatch.setattr(store, "neptune_write", lambda *_a, **_k: False)
    monkeypatch.setattr(store, "neptune_query", query)
    with pytest.raises(RuntimeError, match="write failed"):
        store.write_definition(_doc(), actor="steward-user", version=1)
    assert reads == 0


def test_metric_definition_metamodel_entry_is_standalone_and_exact():
    entries = [row for row in metamodel.META_NODES if row["label"] == "MetricDefinition"]
    assert len(entries) == 1
    entry = entries[0]
    assert "STANDALONE node" in entry["description"]
    assert "written ONLY by the accepted metric_definition lifecycle apply" in entry["description"]
    assert entry["key_properties"].split(", ") == [
        "id", "label", "unit", "kind", "definition_json", "synonyms_json",
        "source", "created_by", "version", "created_at", "updated_at",
    ]


def test_module_imports_without_graph_or_aws_dependencies():
    code = (
        "import sys\n"
        "for name in ('pg8000', 'agents.pgdriver', 'boto3', 'botocore'):\n"
        "    sys.modules[name] = None\n"
        "import agents.metric_definition_store as store\n"
        "assert callable(store.neptune_query)\n"
        "assert callable(store.neptune_write)\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    result = subprocess.run(  # nosec B603 - fixed argv, no shell, local inputs only
        [sys.executable, "-c", code], cwd=adt_root, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "IMPORT-PURE-OK" in result.stdout
