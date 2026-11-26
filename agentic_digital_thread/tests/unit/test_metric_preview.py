# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0080 — dry-run preview of a PENDING metric definition.

Pins: the preview resolves the pending document through the accept path's
parse/validate/build and the live card's resolver, returning the SAME card
statuses (ok / no-data / unavailable / invariant-violation /
unresolved-predicate) plus ``unresolved-input`` (nothing executed) and
``invalid`` (accept would reject; nothing executed); a ``with=`` pair resolves
together; the multi-site gate mirrors the dashboard; cost is measured-only;
and the preview WRITES NOTHING (no layer invalidation, no snapshot drop, no
usage telemetry, no proposal write, no graph write, no dashboard cache).
Offline — every graph/DB seam patched.
"""
from __future__ import annotations

import json

import pytest

import agents.artifact_usage as artifact_usage
import agents.dashboard as dashboard
import agents.db as db
import agents.graph_metric_layer as graph_metric_layer
import agents.memory_client as memory_client
import agents.metric_definition_store as metric_definition_store
import agents.metric_layer as metric_layer_mod
import agents.metric_multisite as metric_multisite
import agents.metric_preview as mp
import agents.metric_resolver as mr
import agents.self_learning as self_learning
from agents.catalog import catalog
from agents.metric_definition import canonical_json
from agents.metric_layer import CodeMetricLayer

_CONCEPTS = frozenset({"widget-event", "widget-color", "availability"})
_DIRECT = {"id": "pending-count", "kind": "direct", "label": "Pending Count",
           "synonyms": [], "unit": "events", "concept": "widget-event",
           "aggregation": "count", "max_exclusive": False}
_RATIO = {"id": "pending-ratio", "kind": "derived", "label": "Pending Ratio",
          "synonyms": [], "unit": "ratio", "max_exclusive": False,
          "formula": {"op": "ratio", "inputs": [{"metric": "pending-count"},
                                                 {"metric": "availability"}]}}


def _context():
    code_ids = frozenset(m.id for m in CodeMetricLayer().list_metrics())
    return _CONCEPTS, code_ids, code_ids, {}


def _raw(doc):
    return canonical_json(doc)


class _WriteAttempted(BaseException):
    """Raised AT the writer boundary. A ``BaseException`` on purpose: a
    production ``except Exception`` best-effort guard cannot swallow it, so a
    write attempt fails the test at the call site, not after the fact."""


#: Every writer a dry-run preview must never reach: metric-layer / definition
#: snapshot invalidation, the definition store's write path, ADR-0066 D6 usage
#: telemetry, graph writes, proposal / status writes, the dashboard cards
#: cache, and conversational memory.
_WRITERS = [
    (metric_layer_mod, "invalidate_metric_layer"),
    (graph_metric_layer, "invalidate_definitions"),
    (metric_definition_store, "invalidate"),
    (metric_definition_store, "write_definition"),
    (metric_definition_store, "neptune_write"),
    (artifact_usage, "bump_artifact_usage"),
    (mr, "neptune_write"),
    (mr, "_record_graph_definition_usage"),
    (db, "neptune_write"),
    (self_learning, "_neptune_write"),
    (self_learning, "_neptune_write_loud"),
    (self_learning, "_put_item"),
    (self_learning, "_update_status"),
    (self_learning, "log_proposed_mapping"),
    (dashboard, "update_cards_cache"),
    (dashboard, "invalidate_cards_cache"),
    (memory_client.MemoryClient, "__init__"),
    (memory_client.MemoryClient, "delete_record"),
]


def _install_write_tripwires(monkeypatch, state: dict) -> None:
    """Arm every writer. ``state['armed']`` is cleared at fixture teardown so
    lane-wide autouse cleanup (which legitimately resets caches after the
    test) is not mistaken for a preview write."""
    for target, name in _WRITERS:
        label = f"{getattr(target, '__name__', target)}.{name}"

        def boom(*_a, _label=label, **_k):
            if not state["armed"]:
                return None
            state["writes"].append(_label)
            raise _WriteAttempted(f"a dry-run preview reached writer {_label}")
        monkeypatch.setattr(target, name, boom)


@pytest.fixture
def seams(monkeypatch):
    """Resolver seams stubbed (direct reads return ``state['row']``) and every
    WRITE seam turned into a fail-fast tripwire (raises at the call; the call
    log is ALSO asserted empty in case a broad guard somewhere swallowed it)."""
    state = {"row": [7, 3], "sql": [], "writes": [], "armed": True}
    _install_write_tripwires(monkeypatch, state)
    monkeypatch.setattr(catalog, "get_onboarded", lambda: [{"id": "sys-a"}])
    monkeypatch.setattr(mr, "get_metric_layer", lambda: CodeMetricLayer())
    monkeypatch.setattr(mr, "_resolve_authority", lambda c, a: mr.AuthoritySource(
        system_id="sys-a", source="authority_rule"))
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mr.MappingProvenance(
        table="events", column="event_id", role=None, confidence=None,
        source="onboarding"))
    monkeypatch.setattr(mr, "get_schema", lambda sid: "plant")
    monkeypatch.setattr(mr, "_connection_type", lambda sid: "PostgreSQL")
    monkeypatch.setattr(mr.catalog, "get", lambda sid: None)

    def _pg(schema, sql):
        state["sql"].append(sql)
        row = state["row"]
        if isinstance(row, Exception):
            raise row
        return json.dumps({"rows": [row]})
    monkeypatch.setattr(mr, "query_pg", _pg)
    yield state
    state["armed"] = False
    assert state["writes"] == [], "a dry-run preview attempted a write"


def _dry(doc, with_docs=None):
    return mp.dry_run(_raw(doc), with_docs or {}, _context())


def test_ok_preview_is_the_card_entry_plus_preview_stamp(seams):
    out = _dry(_DIRECT)

    assert out["preview"] is True
    assert out["metric_id"] == "pending-count"
    assert out["status"] == "ok" and out["value"] == 7
    assert out["sample_count"] == 3
    assert out["label"] == "Pending Count" and out["unit"] == "events"
    assert out["window"] is None
    assert out["computed_at"].endswith("Z") or "+" in out["computed_at"]
    assert out["cost"] == {"query_count": 1, "systems": [
        {"system_id": "sys-a", "query_count": 1}]}
    assert out["lineage"]["kind"] == "direct"


def test_preview_entry_matches_the_dashboard_card_entry(seams, monkeypatch):
    """Same resolver + same renderer as a live card (shape parity)."""
    pending = mp._build_pending(_raw(_DIRECT), {})[1]["pending-count"]
    monkeypatch.setattr(mr, "_utcnow_iso", lambda: "2026-09-25T00:00:00Z")
    live = dashboard.card_entry_from_resolved(mr.resolve_definition(pending))
    preview = _dry(_DIRECT)
    for key in ("preview", "window", "computed_at", "cost"):
        preview.pop(key)
    assert preview == live


def test_no_data_surfaces_unchanged(seams):
    seams["row"] = [None, 0]
    out = _dry(_DIRECT)
    assert out["status"] == "no-data" and out["value"] is None


def test_unavailable_carries_the_reason(seams):
    seams["row"] = mr.MetricResolverError("connection refused")
    out = _dry(_DIRECT)
    assert out["status"] == "unavailable"
    assert "connection refused" in out["error"]
    assert out["preview"] is True


def test_invariant_violation_surfaces_unchanged(seams):
    out = _dry({**_DIRECT, "valid_range": [0.0, 1.0]})
    assert out["status"] == "invariant-violation"
    assert out["value"] is None and out["detail"]


def test_unresolved_predicate_surfaces_unchanged(seams, monkeypatch):
    def unbound(sid, table, predicates):
        raise mr.UnresolvedPredicateError(
            "predicate concept 'widget-color' has no bound field",
            concept="widget-color")
    monkeypatch.setattr(mr, "_predicate_bindings_for", unbound)
    doc = {**_DIRECT, "filter": [{"concept": "widget-color", "op": "eq",
                                   "value": "BLUE"}]}
    out = _dry(doc)
    assert out["status"] == "unresolved-predicate"
    assert "widget-color" in out["detail"]
    assert seams["sql"] == []


def test_derived_with_missing_input_names_it_and_executes_nothing(seams):
    out = _dry(_RATIO)
    assert out["status"] == "unresolved-input"
    assert out["missing_inputs"] == ["pending-count"]
    assert "'pending-count'" in out["detail"]
    assert out["value"] is None
    assert out["cost"] == {"query_count": 0, "systems": []}
    assert seams["sql"] == []


def test_with_pair_resolves_the_pending_set_together(seams, monkeypatch):
    monkeypatch.setattr(mr, "_resolve_table_column", lambda sid, c: mr.MappingProvenance(
        table="events", column=c.replace("-", "_"), role=None, confidence=None,
        source="onboarding"))
    out = _dry(_RATIO, {"proposal-count": _raw(_DIRECT)})
    assert out["status"] == "ok"
    assert out["value"] == 1.0
    assert out["cost"]["query_count"] == 2


def test_invalid_definition_is_never_resolved(seams):
    out = _dry({**_DIRECT, "concept": "no-such-concept"})
    assert out["status"] == "invalid"
    assert "no-such-concept" in out["detail"]
    assert seams["sql"] == []


def test_unparseable_definition_is_invalid(seams):
    out = mp.dry_run("{not json", {}, _context())
    assert out["status"] == "invalid" and out["metric_id"] is None


def test_code_collision_is_invalid(seams):
    out = _dry({**_DIRECT, "id": "availability"})
    assert out["status"] == "invalid"
    assert "code-defined" in out["detail"]


def test_duplicate_metric_id_across_with_is_invalid(seams):
    out = _dry(_DIRECT, {"other": _raw(_DIRECT)})
    assert out["status"] == "invalid"
    assert "more than one" in out["detail"]


def test_multi_site_fan_out_mirrors_the_dashboard_gate(seams, monkeypatch):
    systems = [{"id": "sys-a"}, {"id": "sys-b"}]
    monkeypatch.setattr(catalog, "get_onboarded", lambda: systems)
    covering = [{"system_id": s["id"], "name": s["id"], "site": s["id"]}
                for s in systems]
    seen_layers = []

    def _covering(target, *, metric_layer=None, systems=None, **_k):
        seen_layers.append(metric_layer)
        return covering
    monkeypatch.setattr(metric_multisite, "systems_covering", _covering)

    out = _dry(_DIRECT)

    assert out["status"] == "ok" and out["value"] == 14  # count ⇒ summed
    assert [s["system_id"] for s in out["sites"]] == ["sys-a", "sys-b"]
    assert all(layer.get_metric("pending-count") for layer in seen_layers)
    assert out["cost"]["query_count"] == 2


def test_multi_site_invariant_violation_is_withheld_not_no_data(seams, monkeypatch):
    """Both sites compute a value their declared invariant withholds: the
    aggregate AND every site say ``invariant-violation`` — never no-data."""
    systems = [{"id": "sys-a"}, {"id": "sys-b"}]
    monkeypatch.setattr(catalog, "get_onboarded", lambda: systems)
    covering = [{"system_id": s["id"], "name": s["id"], "site": s["id"]}
                for s in systems]
    monkeypatch.setattr(metric_multisite, "systems_covering",
                        lambda target, **_k: covering)

    out = _dry({**_DIRECT, "valid_range": [0.0, 1.0]})

    assert out["status"] == "invariant-violation"
    assert out["value"] is None
    assert "sys-a" in out["detail"] and "sys-b" in out["detail"]
    assert [s["status"] for s in out["sites"]] == ["invariant-violation"] * 2
    assert all(s["value"] is None for s in out["sites"])


def test_computed_at_is_stamped_after_resolution(seams, monkeypatch):
    ticks = iter(range(1000))
    monkeypatch.setattr(mr, "_utcnow_iso", lambda: f"T{next(ticks):04d}")
    seen = []
    original = mr.query_pg

    def _pg(schema, sql):
        seen.append(mr._utcnow_iso())
        return original(schema, sql)
    monkeypatch.setattr(mr, "query_pg", _pg)

    out = _dry(_DIRECT)

    assert seen, "the value query must have executed"
    assert out["computed_at"] > max(seen)


@pytest.mark.parametrize("target, name", _WRITERS)
def test_write_tripwires_fail_fast_at_the_writer(seams, target, name):
    """The tripwire itself: reaching ANY writer raises at the call site and a
    broad ``except Exception`` guard cannot swallow it."""
    writer = getattr(target, name)
    with pytest.raises(_WriteAttempted):
        try:
            writer(object()) if name == "__init__" else writer()
        except Exception:  # noqa: BLE001 - proves a best-effort guard cannot hide it
            pytest.fail("tripwire was swallowed by an ordinary except Exception")
    seams["writes"].clear()


def test_served_layer_is_read_not_mutated(seams):
    served = CodeMetricLayer()
    before = [m.id for m in served.list_metrics()]
    mp.dry_run(_raw(_DIRECT), {}, _context(), layer=served)
    assert [m.id for m in served.list_metrics()] == before
    with pytest.raises(KeyError):
        served.get_metric("pending-count")


# --- with= parameter ----------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (None, ()), ("", ()), (" ", ()), ("a", ("a",)), ("a, b-2", ("a", "b-2")),
])
def test_parse_with_ids_accepts(raw, expected):
    assert mp.parse_with_ids(raw, "self") == expected


@pytest.mark.parametrize("raw, needle", [
    (",".join(f"p{i}" for i in range(9)), "at most"),
    ("a,a", "twice"),
    ("self", "itself"),
    ("bad id", "malformed"),
    ("a,,b", "malformed"),
    ("../x", "malformed"),
])
def test_parse_with_ids_rejects(raw, needle):
    with pytest.raises(mp.PreviewParameterError, match=needle):
        mp.parse_with_ids(raw, "self")


# --- the self_learning helper (proposal table read-only) -----------------------

class _Table:
    def __init__(self, items):
        self.items = items

    def query(self, **kwargs):
        mid = kwargs["ExpressionAttributeValues"][":mid"]
        return {"Items": [i for i in self.items if i["mapping_id"] == mid]}

    def __getattr__(self, name):
        raise AssertionError(f"preview touched table.{name}")


def _item(mapping_id, doc, status="pending"):
    return {"mapping_id": mapping_id, "proposed_type": "metric_definition",
            "status": status, "query_context": _raw(doc)}


@pytest.fixture
def table(seams, monkeypatch):
    items = [_item("prop-ratio", _RATIO), _item("prop-count", _DIRECT),
             _item("prop-done", _DIRECT, status="accepted")]
    monkeypatch.setattr(self_learning, "_get_table", lambda: _Table(items))
    monkeypatch.setattr(self_learning, "_metric_definition_context", _context)
    return items


def test_helper_without_resolve_is_the_structural_preview(table):
    out = self_learning.preview_metric_definition_mapping("prop-count")
    assert set(out) == {"current", "proposed", "diff", "validation", "collision"}
    assert out["validation"] == {"valid": True, "error": ""}


def test_helper_with_resolve_adds_dry_run_and_leaves_status_pending(table):
    out = self_learning.preview_metric_definition_mapping(
        "prop-ratio", resolve=True, with_ids=("prop-count",))
    assert out["dry_run"]["status"] == "ok"
    assert {i["status"] for i in table} == {"pending", "accepted"}
    assert [i["status"] for i in table][:2] == ["pending", "pending"]


@pytest.mark.parametrize("companion, status, error", [
    ("prop-missing", 404, "with_mapping_not_found"),
    ("prop-done", 409, "with_metric_definition_preview_unavailable"),
])
def test_helper_rejects_unusable_companions(table, companion, status, error):
    out = self_learning.preview_metric_definition_mapping(
        "prop-ratio", resolve=True, with_ids=(companion,))
    assert out["http_status"] == status
    assert out["error"] == error
    assert out["mapping_id"] == companion
