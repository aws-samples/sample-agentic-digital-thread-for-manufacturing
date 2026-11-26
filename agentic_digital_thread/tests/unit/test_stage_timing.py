# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``agents.stage_timing`` and the resolve job's per-stage timing.

The timer records durations and counts under caller-chosen stage names and
nothing else; outside a ``recording()`` block every call is a no-op, so code
shared with other callers behaves exactly as before. The resolve job returns
the record as the additive ``timings`` key, and it must never carry a sampled
value or a table/field name (it is logged and shown on customer installs).
"""
import json
import threading
from unittest.mock import patch

import pytest

from agents import stage_timing


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_stage_and_count_are_noops_without_a_recording():
    with stage_timing.stage("concept_pass"):
        pass
    stage_timing.count("rules", 3)
    stage_timing.incr("io.model.throttled")  # nothing bound, nothing raised


def test_recording_accumulates_stages_and_counts_in_start_order():
    clock = _Clock()
    with stage_timing.recording(clock) as timer:
        with stage_timing.stage("overlap_gate"):
            clock.now += 2.0
        with stage_timing.stage("concept_pass"):
            clock.now += 1.0
        with stage_timing.stage("overlap_gate"):
            clock.now += 0.5
        stage_timing.count("rules", 7)
        stage_timing.count("affinity.new_fields", "not-a-number")
        stage_timing.incr("io.model.retried")
        stage_timing.incr("io.model.retried")
    data = timer.as_dict()
    assert list(data["stages_s"]) == ["overlap_gate", "concept_pass"]
    assert data["stages_s"] == {"overlap_gate": 2.5, "concept_pass": 1.0}
    assert data["counts"] == {"rules": 7, "io.model.retried": 2}
    assert data["total_s"] == 3.5
    assert timer.summary() == ("total=3.50s overlap_gate=2.50s concept_pass=1.00s"
                               " | rules=7 io.model.retried=2")


def test_a_failing_stage_still_records_its_time_and_the_error_propagates():
    clock = _Clock()
    with stage_timing.recording(clock) as timer:
        with pytest.raises(ValueError):
            with stage_timing.stage("join_discovery"):
                clock.now += 4.0
                raise ValueError("x")
    assert timer.as_dict()["stages_s"] == {"join_discovery": 4.0}


def test_recording_is_bound_to_its_own_context_only():
    seen = {}

    def other_thread():
        # A new thread starts with an empty context: no timer bound.
        stage_timing.count("rules", 1)
        seen["bound"] = stage_timing._current.get()

    with stage_timing.recording() as timer:
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
    assert seen["bound"] is None
    assert "rules" not in timer.as_dict()["counts"]
    assert stage_timing._current.get() is None


# ---------------------------------------------------------------------------
# The resolve job's timings
# ---------------------------------------------------------------------------
_NEW = "sys-new"


def _catalog_get(sid):
    return {_NEW: {"schema": "new_schema", "connection_type": "PostgreSQL"},
            "sys-erp": {"connection_type": "PostgreSQL", "schema": "erp_schema"}}.get(sid, {})


def _resolve_scenario():
    """A new system sharing the ``work-order`` concept with one peer field."""
    from agents.models import OnboardResolveRequest
    existing = [{"sys_id": "sys-erp", "sys_name": "ERP", "sys_schema": "erp_schema",
                 "field_name": "order_number", "concept": "work-order",
                 "table_name": "orders", "role": "identifier"}]

    def nq(query, params=None):
        return [] if "IDENTIFIED_BY" in query else existing

    def sample(**kw):
        return ["WO-1", "WO-2"] if kw.get("field") in ("wo_ref", "order_number") else []

    body = OnboardResolveRequest(system_id=_NEW, mappings=[
        {"table": "records", "field": "wo_ref", "concept": "work-order", "confidence": 0.9,
         "mappings": [{"concept": "work-order", "role": "identifier", "confidence": 0.9}]},
    ])
    return body, nq, sample


def test_timed_decorator_counts_calls_and_time_only_inside_a_recording():
    clock = _Clock()
    calls = []

    @stage_timing.timed("io.model")
    def thing(x):
        """Doc."""
        calls.append(x)
        clock.now += 1.5
        return x * 2

    assert thing(1) == 2 and thing.__name__ == "thing" and thing.__doc__ == "Doc."
    with stage_timing.recording(clock) as timer:
        assert thing(2) == 4
        assert thing(3) == 6
    assert calls == [1, 2, 3]
    assert timer.as_dict()["stages_s"] == {"io.model": 3.0}
    assert timer.as_dict()["counts"] == {"io.model.calls": 2}


def test_io_seams_record_time_by_category_and_count_throttles(monkeypatch):
    """The real decorated seams (graph read, model call, source sample and
    probe) record under ``io.*``; a throttled model attempt is counted, a
    memoized sample is a memo hit (no backend call), and a probe that never
    reaches the source (non-PostgreSQL) is not counted at all."""
    from botocore.exceptions import ClientError
    from agents import db, resolve_evidence

    monkeypatch.setattr(db, "_graph_backend", lambda: "local")
    monkeypatch.setattr(db, "_memgraph_run", lambda cypher, parameters=None: [{"n": 1}])
    monkeypatch.setattr(db, "pg", lambda schema, sql, params=None: {"rows": [["v1"], ["v2"]]})
    monkeypatch.setattr(resolve_evidence, "_sample_memo", {})
    monkeypatch.setattr("agents.aws_session.bedrock_runtime_client", lambda region: object())
    monkeypatch.setattr("time.sleep", lambda _s: None)
    throttle = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}},
                           "Converse")
    answers = iter([throttle, throttle, "answer"])

    def invoke(client, prompt, model_id, attempt):
        item = next(answers)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(db, "_invoke_bedrock", invoke)
    sample = dict(system_id="sys-timing-test", connection_type="PostgreSQL", schema="s",
                  table="t", field="f")
    with stage_timing.recording() as timer:
        assert db.neptune_query("MATCH (n) RETURN n") == [{"n": 1}]
        assert db.bedrock_call("prompt", model="m") == "answer"
        assert resolve_evidence.sample_field_values(**sample) == ["v1", "v2"]
        assert resolve_evidence.sample_field_values(**sample) == ["v1", "v2"]  # memo hit
        assert resolve_evidence.sample_field_values(**dict(sample, schema="")) == []  # guard
        assert resolve_evidence.probe_field_values("sys-x", "Athena", "s", "t", "f", ["v"]) is None
        assert resolve_evidence.probe_field_values(
            "sys-x", "PostgreSQL", "s", "t", "f", ["v1"]) == ["v1", "v2"]
    data = timer.as_dict()
    assert list(data["stages_s"]) == ["io.graph_read", "io.model", "io.source_sample",
                                      "io.source_probe"]
    assert data["counts"] == {"io.graph_read.calls": 1, "io.model.calls": 1,
                              "io.model.throttled": 2, "io.source_sample.calls": 1,
                              "io.source_sample.memo_hits": 1, "io.source_probe.calls": 1}


def test_a_seam_inside_another_is_not_double_counted():
    clock = _Clock()

    @stage_timing.timed("io.graph_read")
    def inner():
        clock.now += 1.0

    @stage_timing.timed("io.source_sample")
    def outer():
        clock.now += 2.0
        inner()

    with stage_timing.recording(clock) as timer:
        outer()
        inner()
    data = timer.as_dict()
    assert data["stages_s"] == {"io.source_sample": 3.0, "io.graph_read": 1.0}
    assert data["counts"] == {"io.source_sample.calls": 1, "io.graph_read.calls": 1}


def test_a_timer_reports_only_the_known_names_and_counts_what_it_dropped():
    with stage_timing.recording() as timer:
        with stage_timing.stage("customer_ssn"):
            pass
        stage_timing.count("orders.order_number", 3)
        stage_timing.incr("rules")
    data = timer.as_dict()
    assert data["stages_s"] == {}
    assert data["counts"] == {"rules": 1, "timing.unknown_names": 2}
    assert "customer_ssn" not in timer.summary()


def test_seams_in_two_copied_contexts_never_drop_each_other():
    """The 'inside a seam' flag is per context: two threads running seams at
    the same time on one shared timer both record."""
    import contextvars

    both_inside = threading.Barrier(2)

    @stage_timing.timed("io.model")
    def model():
        both_inside.wait(timeout=5)

    @stage_timing.timed("io.graph_read")
    def graph():
        both_inside.wait(timeout=5)

    @stage_timing.timed("io.graph_read")
    def quick():
        return None

    with stage_timing.recording() as timer:
        threads = [threading.Thread(target=contextvars.copy_context().run, args=(fn,))
                   for fn in (model, graph)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        quick()  # the flag is clear in this context too
    counts = timer.as_dict()["counts"]
    assert counts == {"io.model.calls": 1, "io.graph_read.calls": 2}


def _timing_name_args(tree):
    """(func, node) for every stage/count/incr/timed call on ``stage_timing``."""
    import ast
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "stage_timing"
                and node.func.attr in ("stage", "count", "incr", "timed")):
            yield node.func.attr, node.args[0] if node.args else None


def _literal_names(arg):
    import ast
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.IfExp):
        return _literal_names(arg.body) + _literal_names(arg.orelse)
    return None


def test_every_timing_name_in_the_engine_is_a_fixed_known_literal():
    """No caller may pass a computed name (a table, a field, a system id):
    every name is a literal in KNOWN_STAGES / KNOWN_COUNTS, and each timed
    seam's ``<name>.calls`` is a known count. Only the ``stage_timing.<fn>``
    form is used, so the scan sees every call: no aliased or from-import."""
    import ast
    from pathlib import Path
    agents_dir = Path(stage_timing.__file__).resolve().parent
    seen = 0
    for path in sorted(agents_dir.rglob("*.py")):
        if path.name == "stage_timing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").endswith("stage_timing"), \
                    f"{path.name}: import stage_timing itself, not names from it"
                for alias in node.names:
                    if alias.name == "stage_timing":
                        assert alias.asname is None, f"{path.name}: no alias for stage_timing"
            if isinstance(node, ast.Import):
                assert not any(a.name.endswith("stage_timing") for a in node.names), path.name
        for func, arg in _timing_name_args(tree):
            names = _literal_names(arg)
            assert names, f"{path.name}: stage_timing.{func} needs a literal name"
            for name in names:
                seen += 1
                known = stage_timing.KNOWN_COUNTS if func in ("count", "incr") \
                    else stage_timing.KNOWN_STAGES
                assert name in known, f"{path.name}: {name!r} is not a known timing name"
                if func == "timed":
                    assert f"{name}.calls" in stage_timing.KNOWN_COUNTS, name
    assert seen >= 20


def test_resolve_job_result_carries_timings_and_no_value_or_name():
    from agents import onboard
    from agents.job_store import JobStore
    body, nq, sample = _resolve_scenario()
    store = JobStore("resolve", table=None)
    with patch.object(onboard, "_resolve_jobs", store), \
            patch("agents.onboard.neptune_query", side_effect=nq), \
            patch("agents.onboard.catalog") as cat, \
            patch("agents.onboard.bedrock_call", return_value='{"rules": []}'), \
            patch("agents.connection_resolver.resolve_connection", return_value={}), \
            patch("agents.resolve_evidence.sample_field_values", side_effect=sample), \
            patch("agents.join_discovery.discover_joins_for_system",
                  return_value={"joins": [], "crosswalks": []}):
        cat.get.side_effect = _catalog_get
        cat.get_onboarded_ids.return_value = {"sys-erp"}
        onboard._run_resolve_job("job-t", body)
    job = store.get("job-t")
    assert job["status"] == "done", job
    timings = job["result"]["timings"]
    # Sampling, graph and model are patched above the seams; the exact pass's
    # membership probe still runs the real (offline, failing) probe seam.
    passes = [s for s in timings["stages_s"] if not s.startswith("io.")]
    assert passes == [
        "concept_pass", "overlap_gate", "affinity_pass", "affinity.sample_new",
        "affinity.sample_peers", "affinity.match_and_probe", "join_discovery"]
    assert timings["counts"]["affinity.new_fields"] == 1
    assert "timing.unknown_names" not in timings["counts"]  # every real name is listed
    # Durations and counts only: no sampled value, table, field or system id.
    dumped = json.dumps(timings)
    for leaked in ("WO-1", "records", "wo_ref", "orders", "order_number", "sys-erp", _NEW):
        assert leaked not in dumped
    assert all(isinstance(v, (int, float)) for v in timings["stages_s"].values())
    assert all(isinstance(v, int) for v in timings["counts"].values())
