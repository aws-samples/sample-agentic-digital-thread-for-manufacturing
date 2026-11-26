# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Two things a live check on a rehearsal deployment found (2026-10-27).

A cleared row exclusion stayed in the system agent's prompt. The ROW
EXCLUSIONS fact was baked into the system prompt when the agent was built,
and the agent is a long-lived runtime singleton. With a rule set when the
runtime started, a steward cleared it; the next question on the same runtime
still read the rule in the prompt, added its predicate to the query and told
the user it had excluded those rows. The engine had stopped asking for it,
so nothing refused the query, and a real rule cleared because it was wrong
would have kept changing figures until the runtime was rebuilt. Now the fact
is read for each question, from the state read again for it
(``row_exclusion.current_schema_lines``), and sent with the question, as the
KNOWN CONCEPT MAPPINGS are; the system prompt never carries it.

A same-table race answered 500. Two steward changes of one table sent at once
(set against set) made the graph abort one compare-and-set of the manifest on
a concurrent modification. The write seam answered that as "refused", and the
route answered 500 for a change that only lost a race. A conflict aborts the
statement, so it did not run, and every row-exclusion write is a
compare-and-set read again before it is resent: the write is now read again
and resent, and only a change that keeps losing answers 409
``concurrent_change``.

Fully offline (``_row_exclusion_graph.RxGraph``); synthetic names.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

import agents.system_agent as sa
from agents import admin_routes
from agents import connection_resolver as cr
from agents import record_key
from agents import row_exclusion as rx
from agents import row_exclusion_manifest as manifest
from tests.unit._row_exclusion_graph import RxGraph

_SYS = "system-a"
_COLUMNS = {"pipeline": "varchar", "run_ref": "varchar", "state": "varchar"}
_DESC = "job_events(pipeline: varchar; run_ref: varchar; state: varchar)"
_RULE = [{"column": "pipeline", "op": "not_empty"}, {"column": "run_ref", "op": "not_empty"}]
_OTHER = [{"column": "state", "op": "in", "values": ["SHADOW"]}]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(record_key, "_graph_query", lambda: (lambda *_a, **_k: [{}]))
    monkeypatch.setattr(cr, "audit_connection_event", lambda *_a, **_k: None)
    monkeypatch.setattr(manifest, "_conflict_pause", lambda _attempt: None, raising=False)
    rx.reset()
    record_key.reset()
    yield
    rx.reset()
    record_key.reset()


def _graph() -> RxGraph:
    return RxGraph({_SYS: {"job_events": _COLUMNS}})


def _set(g, conditions=_RULE):
    return rx.set_row_exclusion(_SYS, "job_events", conditions, label="shadow copies",
                                actor="dana", write=g.write, query=g.query)


def _clear(g):
    return rx.clear_row_exclusion(_SYS, "job_events", actor="dana", write=g.write,
                                  query=g.query)


def _questions(monkeypatch, g, steps) -> tuple:
    """Build the system agent's prompt at bake (the runtime starting), then run
    each step: a callable (a steward write) or a question through
    ``_run_mode_a_query`` on the same agent. Returns ``(prompt, [the text each
    question reached the model with])``."""
    monkeypatch.setattr(rx, "_graph_query", g.seam())
    sa._remember_turn_time_facts(_SYS, [], _DESC, connection_type="Athena")
    prompt = sa._compose_system_prompt(_SYS, "System A", "lake_db", "", _DESC, "Athena")
    sent: list = []

    def invoke(_agent, question):
        sent.append(question)
        return "The count is 42.", {}
    monkeypatch.setattr(sa, "_invoke_mode_a_agent_with_state", invoke)
    monkeypatch.setattr(sa, "_augment_question_with_kg_context", lambda _s, q, _k: q)
    monkeypatch.setattr(sa, "_concept_mappings_block", lambda *_a, **_k: "")
    for step in steps:
        if callable(step):
            step()
        else:
            asyncio.run(sa._run_mode_a_query(object(), _SYS, step, ""))
    return prompt, sent


# --------------------------------------------------------------------------- #
# The fact reaches each question, never the baked prompt.
# --------------------------------------------------------------------------- #
def test_a_rule_cleared_after_the_runtime_started_reaches_no_later_question(monkeypatch):
    g = _graph()
    _set(g)
    prompt, (before, after) = _questions(monkeypatch, g, [
        "How many steps failed?", lambda: _clear(g), "How many steps failed?"])
    assert "ROW EXCLUSIONS" not in prompt
    assert "ROW EXCLUSIONS (facts" in before and "shadow copies" in before
    assert rx.predicate_sql(rx.exclusion_from("shadow copies", _RULE)) not in after
    assert "job_events: no row exclusion now" in after


def test_a_rule_set_after_the_runtime_started_reaches_the_next_question(monkeypatch):
    g = _graph()
    prompt, (before, after) = _questions(monkeypatch, g, [
        "How many steps failed?", lambda: _set(g), "How many steps failed?"])
    assert "ROW EXCLUSIONS" not in prompt and "ROW EXCLUSIONS" not in before
    assert "ROW EXCLUSIONS (facts" in after
    assert rx.predicate_sql(rx.exclusion_from("shadow copies", _RULE)) in after


def test_a_replaced_rule_reaches_the_next_question_as_the_new_one(monkeypatch):
    g = _graph()
    _set(g)
    _prompt, (before, after) = _questions(monkeypatch, g, [
        "How many steps failed?", lambda: _set(g, _OTHER), "How many steps failed?"])
    old = rx.predicate_sql(rx.exclusion_from("shadow copies", _RULE))
    new = rx.predicate_sql(rx.exclusion_from("shadow copies", _OTHER))
    assert old in before and new not in before
    assert new in after and old not in after


def test_no_rule_is_stated_while_the_manifest_cannot_be_read(monkeypatch):
    """A rule last read may have been replaced since: it is not stated, the
    table stated earlier is named as not confirmed, and the engine holds every
    data read until a read succeeds."""
    g = _graph()
    _set(g)

    def outage():
        g.fail.add("manifest")
    _prompt, (before, during) = _questions(monkeypatch, g, [
        "How many steps failed?", outage, "How many steps failed?"])
    assert "ROW EXCLUSIONS (facts" in before
    assert rx.predicate_sql(rx.exclusion_from("shadow copies", _RULE)) not in during
    assert "job_events: its row exclusion cannot be confirmed now" in during


def test_a_held_table_is_not_stated(monkeypatch):
    """A listed table whose rule is not confirmed (here: a pending entry with
    nothing stored) is held by the engine and not stated."""
    g = _graph()
    g.list_table(_SYS, "job_events", pending=True)
    _prompt, (sent,) = _questions(monkeypatch, g, ["How many steps failed?"])
    assert "ROW EXCLUSIONS" not in sent


# --------------------------------------------------------------------------- #
# A write the graph aborted on a concurrent modification.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["swap", "set_fact"])
def test_a_write_aborted_on_a_concurrent_modification_is_read_again_and_resent(kind):
    g = _graph()
    g.conflict_next[kind] = 2
    out = _set(g)
    assert out["status"] == "ok"
    assert g.stored(_SYS, "job_events")["conditions"] == _RULE
    assert g.manifest(_SYS)["job_events"]["pending"] is False


def test_a_clear_aborted_on_a_concurrent_modification_completes():
    g = _graph()
    _set(g)
    g.conflict_next["swap"] = 1
    g.conflict_next["set_fact"] = 1
    assert _clear(g)["status"] == "ok"
    assert g.stored(_SYS, "job_events") is None and "job_events" not in g.manifest(_SYS)


@pytest.mark.parametrize("kind", ["swap", "set_fact"])
def test_a_change_that_keeps_losing_answers_409_and_leaves_the_table_listed(kind):
    g = _graph()
    g.conflict_next[kind] = 100
    response = admin_routes.set_row_exclusion_sync(
        _SYS, {"table": "job_events", "label": "shadow copies", "conditions": _RULE}, "dana",
        connection_type="Athena", seams=(g.write, g.query))
    assert getattr(response, "status_code", 200) == 409
    assert b"concurrent_change" in response.body
    g.conflict_next.clear()
    # Whatever landed is listed: a stored fact is never left unlisted.
    if g.stored(_SYS, "job_events") is not None:
        assert "job_events" in g.manifest(_SYS)


def test_a_rewrite_marker_aborted_on_a_concurrent_modification_is_written():
    g = _graph()
    _set(g)
    g.conflict_next["swap"] = 2
    carried = rx.begin_rewrite(_SYS, owner="run-a", query=g.query, write=g.write)
    assert set(carried) == {"job_events"}
    assert g.rewrite_marker(_SYS)["owner"] == "run-a"


def test_the_route_and_provision_seams_answer_a_conflict_as_a_conflict():
    """The graph seams both writers use raise ``GraphWriteConflict`` for a
    write the graph aborted on a concurrent modification, and answer
    ``None`` for any other failure, as before. (The lane's conftest patches
    the provision seam itself offline, so its source is read.)"""
    import ast
    import inspect
    db = importlib.import_module("agents.db")
    write, _query = admin_routes._row_exclusion_graph_seams()
    assert write is db.neptune_write_returning_cas
    tree = ast.parse(inspect.getsource(rx))
    (provision,) = [n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_provision_graph"]
    returned = [n for n in ast.walk(provision) if isinstance(n, ast.Return)]
    assert ast.unparse(returned[-1].value) == "(neptune_write_returning_cas, neptune_query_checked)"


def test_the_cas_seam_raises_a_conflict_and_answers_none_for_any_other_failure(monkeypatch):
    from botocore.exceptions import ClientError
    db = importlib.import_module("agents.db")
    monkeypatch.setattr(db, "_graph_backend", lambda: "neptune")

    def conflict(*_a, **_k):
        raise ClientError({"Error": {"Code": "ConflictException", "Message": "conflict"},
                           "ResponseMetadata": {"HTTPStatusCode": 409},
                           "reason": "CONCURRENT_MODIFICATION"}, "ExecuteQuery")

    def denied(*_a, **_k):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"},
                           "ResponseMetadata": {"HTTPStatusCode": 403}}, "ExecuteQuery")
    monkeypatch.setattr(db, "_neptune_execute", conflict)
    with pytest.raises(db.GraphWriteConflict):
        db.neptune_write_returning_cas("MATCH (n) SET n.x = 1 RETURN n")
    monkeypatch.setattr(db, "_neptune_execute", denied)
    assert db.neptune_write_returning_cas("MATCH (n) SET n.x = 1 RETURN n") is None
    monkeypatch.setattr(db, "_neptune_execute", lambda *_a, **_k: [{"id": "x"}])
    assert db.neptune_write_returning_cas("MATCH (n) SET n.x = 1 RETURN n") == [{"id": "x"}]
