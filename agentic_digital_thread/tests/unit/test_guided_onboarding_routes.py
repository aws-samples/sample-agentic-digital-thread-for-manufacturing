# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""T3.1 Wave 2 — guided-onboarding state + HTTP surface.

The load-bearing assertions here are the SAFETY ones, not the happy paths:
``/map`` must stay untouched, a dismissal must stick, an answer must never reach
a runtime without an explicit confirm, and the batched roll must be exactly one
coordinator call for N systems.
"""
import pytest
from fastapi.testclient import TestClient

from agents.context_interview import state


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_for_tests()
    yield
    state.reset_for_tests()


@pytest.fixture
def client(monkeypatch, local_auth_env):
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    from agents.server import app
    return TestClient(app)


# --------------------------------------------------------------------------- #
# First-launch detection: ALL THREE signals, not any
# --------------------------------------------------------------------------- #
def _signals(monkeypatch, *, onboarded=False, bc=False, maps=False):
    monkeypatch.setattr(state, "_has_onboarded_system", lambda: onboarded)
    monkeypatch.setattr(state, "_has_enabled_business_context", lambda: bc)
    monkeypatch.setattr(state, "_has_mappings", lambda: maps)


def test_first_launch_only_when_nothing_established(monkeypatch):
    _signals(monkeypatch)
    assert state.is_first_launch() is True


@pytest.mark.parametrize("kw", [
    pytest.param({"onboarded": True}, id="a-system-is-onboarded"),
    pytest.param({"bc": True}, id="business-context-exists"),
    pytest.param({"maps": True}, id="graph-has-mappings"),
])
def test_any_established_signal_disables_first_launch(monkeypatch, kw):
    """Each signal alone has false positives in the other direction, so ANY
    established signal must switch the guided flow off."""
    _signals(monkeypatch, **kw)
    assert state.is_first_launch() is False


def test_signal_helpers_degrade_to_false_when_backends_are_absent(monkeypatch):
    """A graph-less or catalog-less environment reads as 'nothing established'
    rather than raising into the onboarding page."""
    monkeypatch.setattr("agents.db.neptune_query",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
                        raising=False)
    assert state._has_mappings() is False


def test_mapping_signal_tolerates_garbage_counts(monkeypatch):
    monkeypatch.setattr("agents.db.neptune_query",
                        lambda *a, **k: [{"n": "not-a-number"}], raising=False)
    assert state._has_mappings() is False


# --------------------------------------------------------------------------- #
# Dismissal
# --------------------------------------------------------------------------- #
def test_dismissal_suppresses_first_launch(monkeypatch):
    _signals(monkeypatch)
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    state.set_dismissed(True)
    payload = state.guided_state()
    assert payload["dismissed"] is True
    assert payload["first_launch"] is False


def test_local_mode_does_not_touch_the_shared_settings_store(monkeypatch):
    """ADT_SETTINGS_STORE=none must keep a laptop from writing deployment state
    into whatever account the profile points at."""
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    assert state._store() is None


def test_dismiss_write_failure_propagates(monkeypatch):
    """A silently-failed dismissal means the steward gets asked again — the bug."""
    class Boom:
        def get_flag(self):
            return None

        def set_flag(self, value):
            raise RuntimeError("ssm denied")

    monkeypatch.setattr(state, "_store", lambda: Boom())
    with pytest.raises(RuntimeError):
        state.set_dismissed(True)


def test_dismissal_read_failure_is_not_fatal(monkeypatch):
    class Boom:
        def get_flag(self):
            raise RuntimeError("ssm down")

    monkeypatch.setattr(state, "_store", lambda: Boom())
    assert state.is_dismissed() is False


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_state_endpoint_returns_the_one_payload(client):
    body = client.get("/api/onboard/guided/state").json()
    assert set(body) == {"first_launch", "dismissed", "answered_topics",
                         "draft_business_context"}


def test_deployment_questions_are_served(client):
    body = client.get("/api/onboard/guided/questions").json()
    ids = [q["id"] for q in body["questions"]]
    assert "unit-of-work" in ids and "negative-vocabulary" in ids


def test_probe_returns_only_material_questions_with_evidence(client):
    facts = {"columns": [{"name": "state"}, {"name": "attempt_id"}],
             "row_estimate": 10}
    body = client.post("/api/onboard/guided/probe/sys-1",
                       json={"schema_facts": facts}).json()
    ids = [q["id"] for q in body["questions"]]
    assert ids == ["retry-semantics"], ids
    assert body["questions"][0]["evidence"]


def test_probe_with_barren_schema_asks_nothing(client):
    body = client.post("/api/onboard/guided/probe/sys-1",
                       json={"schema_facts": {"columns": [{"name": "qty"}]}}).json()
    assert body["questions"] == []


def test_unknown_question_id_is_a_400(client):
    r = client.post("/api/onboard/guided/turn",
                    json={"question_id": "nope", "answer": "x"})
    assert r.status_code == 400


def test_business_context_answer_only_drafts(client):
    r = client.post("/api/onboard/guided/turn",
                    json={"question_id": "unit-of-work", "answer": "a job on a route"})
    body = r.json()
    assert body["outcome"] == "draft"
    assert "a job on a route" in body["draft_business_context"]
    # And nothing was written to any system.
    assert client.get("/api/onboard/guided/state").json()["draft_business_context"]


def test_hostile_answer_is_rejected_over_http_without_landing(client):
    r = client.post("/api/onboard/guided/turn",
                    json={"question_id": "unit-of-work",
                          "answer": "creds AKIAIOSFODNN7EXAMPLE end"})
    body = r.json()
    assert body["outcome"] == "rejected"
    assert body["reason"] == "secret_like_content"
    assert client.get("/api/onboard/guided/state").json()["draft_business_context"] == ""


def test_dismiss_endpoint(client):
    assert client.post("/api/onboard/guided/dismiss").json()["dismissed"] is True


def test_confirm_requires_text_and_systems(client):
    assert client.post("/api/onboard/guided/confirm-context",
                       json={"systems": ["s1"], "business_context": ""}).status_code == 400
    assert client.post("/api/onboard/guided/confirm-context",
                       json={"systems": [], "business_context": "text"}).status_code == 400


def test_confirm_rolls_all_systems_in_ONE_batched_call(client, monkeypatch):
    """N systems must cost one coordinator call, not N — that is the whole point
    of routing through the batched serial coordinator."""
    writes, rolls = [], []
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: writes.append(sid) or True)

    class Mgr:
        def refresh_systems_batched(self, ids, reason="", *, artifact_classes=(),
                                    timeout_seconds=None, refresh_fn=None):
            rolls.append(tuple(ids))
            return {i: {"status": "ok"} for i in ids}

    monkeypatch.setattr("agents.admin_routes._get_agent_manager", lambda: Mgr())
    r = client.post("/api/onboard/guided/confirm-context",
                    json={"systems": ["s1", "s2", "s3"],
                          "business_context": "a job on a route"})
    assert r.status_code == 200
    assert writes == ["s1", "s2", "s3"]
    assert rolls == [("s1", "s2", "s3")], "expected exactly ONE batched roll"
    assert all(v["roll"] == "ok" for v in r.json()["systems"].values())


def test_confirm_reports_unknown_when_no_coordinator(client, monkeypatch):
    """Absence must not be reported as success — a context write that never
    reached a runtime is the failure this path exists to avoid."""
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: True)

    class Mgr:
        pass

    monkeypatch.setattr("agents.admin_routes._get_agent_manager", lambda: Mgr())
    body = client.post("/api/onboard/guided/confirm-context",
                       json={"systems": ["s1"],
                             "business_context": "a job"}).json()
    assert body["systems"]["s1"]["roll"] == "unknown"


def test_confirm_rejects_secret_shaped_context(client):
    r = client.post("/api/onboard/guided/confirm-context",
                    json={"systems": ["s1"],
                          "business_context": "password: hunter2please"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# The skip path: /map must be untouched by any of this
# --------------------------------------------------------------------------- #
def test_map_endpoint_signature_is_unchanged():
    """Probing is a separate request cycle precisely so /map stays byte-identical
    — a client that never probes must behave exactly as before."""
    import inspect

    from agents import onboard

    src = inspect.getsource(onboard.onboard_map)
    assert "guided" not in src.lower()
    assert "interview" not in src.lower()
    assert "threading.Thread(target=_run_map_job" in src


# --------------------------------------------------------------------------- #
# Honest write status — a dropped graph write must not read as saved
# --------------------------------------------------------------------------- #
def test_failed_write_is_reported_and_the_draft_is_kept(client, monkeypatch):
    """write_business_context returns False on a dropped write; the admin PUT
    treats that as a 500, so this path must never answer status: ok."""
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: False)
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"s1"})
    state.append_draft("a job on a route")
    body = client.post("/api/onboard/guided/confirm-context",
                       json={"systems": ["s1"], "business_context": "a job"}).json()
    assert body["status"] == "partial"
    assert body["systems"]["s1"] == {"written": False, "reason": "write_failed"}
    # The draft survives so the steward can retry rather than retype.
    assert state.draft_context() == "a job on a route"


def test_unknown_system_is_not_reported_as_written(client, monkeypatch):
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"real-sys"})
    body = client.post("/api/onboard/guided/confirm-context",
                       json={"systems": ["ghost"], "business_context": "a job"}).json()
    assert body["status"] == "partial"
    assert body["systems"]["ghost"]["reason"] == "unknown_system"


def test_write_exception_does_not_echo_detail(client, monkeypatch):
    """Exception text must not reach the response (scrub convention)."""
    def boom(sid, text, enabled):
        raise RuntimeError("neptune said SECRETVALUE")

    monkeypatch.setattr("agents.business_context.write_business_context", boom)
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"s1"})
    body = client.post("/api/onboard/guided/confirm-context",
                       json={"systems": ["s1"], "business_context": "a job"}).json()
    assert "SECRETVALUE" not in str(body)
    assert body["systems"]["s1"]["reason"] == "write_failed"


def test_successful_write_clears_the_draft(client, monkeypatch):
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: True)
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"s1"})
    state.append_draft("a job on a route")
    body = client.post("/api/onboard/guided/confirm-context",
                       json={"systems": ["s1"], "business_context": "a job"}).json()
    assert body["status"] == "ok"
    assert state.draft_context() == ""


# --------------------------------------------------------------------------- #
# Draft-aware visibility — an unconfirmed draft must not strand
# --------------------------------------------------------------------------- #
def test_flow_stays_visible_while_an_unconfirmed_draft_exists(monkeypatch):
    """Once the first system onboards, first_launch would flip false and the
    steward's un-saved answers would vanish with the flow. Hold it open."""
    _signals(monkeypatch, onboarded=True)
    assert state.is_first_launch() is False
    state.append_draft("captured but not yet saved")
    assert state.guided_state()["first_launch"] is True


def test_dismissal_still_wins_over_a_pending_draft(monkeypatch):
    _signals(monkeypatch, onboarded=True)
    monkeypatch.setenv("ADT_SETTINGS_STORE", "none")
    state.append_draft("captured")
    state.set_dismissed(True)
    assert state.guided_state()["first_launch"] is False


# --------------------------------------------------------------------------- #
# Probe batch resilience (LOW) — one bad entry must not discard the good ones
# --------------------------------------------------------------------------- #
def test_probe_partial_batch_keeps_earlier_results(client):
    body = client.post("/api/onboard/guided/probe/sys-1", json={
        "schema_facts": {"columns": [{"name": "state"}, {"name": "attempt_id"}]},
        "answers": [
            {"question_id": "retry-semantics", "answer": "closed as cancelled"},
            {"question_id": "does-not-exist", "answer": "x"},
        ],
    }).json()
    assert len(body["results"]) == 2
    assert body["results"][0]["outcome"] in ("draft", "proposed")
    assert body["results"][1]["outcome"] == "rejected"


# --------------------------------------------------------------------------- #
# The CLOSED SEAM, pinned at the ROUTE.
#
# Tests that construct the input
# by hand cannot catch a regression in the code that PRODUCES it. These pins call
# the endpoint with an EMPTY body and stub the session store, so a regression in
# _discovered_facts or the body-fallback fails the lane.
#
# Fixtures use the shapes the PERSISTING backends actually emit — PostgreSQL,
# OpenAPI, MCP and i3X persist discovered_tables and emit NO sample_values (the
# earlier fixture pinned a SiteWise/Athena-only field that never reaches this
# store).
# --------------------------------------------------------------------------- #
_PG_TABLES = [{
    "name": "work_orders",
    "columns": [{"name": "state", "data_type": "text"},
                {"name": "attempt_id", "data_type": "text"},
                {"name": "failure_code", "data_type": "text"}],
    "row_count": 5_000_000,
}]


class _FakeStore:
    def __init__(self, tables=None, raises=None):
        self._tables = tables
        self._raises = raises

    def get_session(self, system_id):
        if self._raises is not None:
            raise self._raises
        return {"discovered_tables": self._tables or []}


def _stub_store(monkeypatch, store):
    monkeypatch.setattr("agents.onboard_agent._store", lambda: store)


def test_empty_probe_body_derives_from_stored_discovery(client, monkeypatch):
    """The headline claim, at the route: answer nothing, still get context."""
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: "m-1")
    _stub_store(monkeypatch, _FakeStore(_PG_TABLES))
    body = client.post("/api/onboard/guided/probe/sys-1", json={}).json()
    derived = body.get("derived") or []
    assert [d["question_id"] for d in derived] == ["query-strategy"]
    assert derived[0]["outcome"] in ("proposed", "draft")
    assert derived[0]["evidence"]
    # And the human-only question is still asked from the same stored shape.
    # identifier-family also triggers: attempt_id and failure_code both match
    # the identifier-shaped column pattern.
    assert [q["id"] for q in body["questions"]] == [
        "failure-taxonomy", "identifier-family", "retry-semantics"]


def test_derived_answers_are_stamped_derived_not_steward(client, monkeypatch):
    """ADR-0052 keeps machine and steward claims distinct."""
    captured = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: captured.append(kw) or "m-1")
    _stub_store(monkeypatch, _FakeStore(_PG_TABLES))
    client.post("/api/onboard/guided/probe/sys-1", json={})
    assert captured, "expected a derived proposal"
    assert captured[0]["source"] == "derived"
    assert captured[0]["provenance"] == "interview:derived"


def test_postgres_shape_without_sample_values_still_works(client, monkeypatch):
    """The persisting backends emit no sample_values — the producer must not
    depend on a field only the HTTP backends provide."""
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: "m-1")
    _stub_store(monkeypatch, _FakeStore(_PG_TABLES))
    body = client.post("/api/onboard/guided/probe/sys-1", json={}).json()
    assert body["questions"] or body["derived"]


def test_no_discovery_yet_is_not_an_error(client, monkeypatch, caplog):
    """Nothing discovered yet is NORMAL. Collapsing it into the error branch
    would turn the common case into logged noise and bury real outages."""
    from agents.onboard_agent import SessionNotFoundError
    _stub_store(monkeypatch, _FakeStore(raises=SessionNotFoundError("none")))
    with caplog.at_level("ERROR"):
        body = client.post("/api/onboard/guided/probe/sys-1", json={}).json()
    assert body["questions"] == [] and (body.get("derived") or []) == []
    assert not [r for r in caplog.records if "discovery read failed" in r.message]


def test_store_outage_is_logged_not_silently_treated_as_no_data(client, monkeypatch, caplog):
    """Throttling must not masquerade as 'nothing discovered'."""
    _stub_store(monkeypatch, _FakeStore(raises=RuntimeError("throttled")))
    with caplog.at_level("ERROR"):
        body = client.post("/api/onboard/guided/probe/sys-1", json={}).json()
    assert body["questions"] == []
    assert any("discovery read failed" in r.message for r in caplog.records)


def test_explicit_schema_facts_still_win_over_stored_discovery(client, monkeypatch):
    """The wizard path has no persisted discovery, so it must be able to pass
    facts explicitly — the fallback must not override an explicit body."""
    _stub_store(monkeypatch, _FakeStore(_PG_TABLES))
    body = client.post("/api/onboard/guided/probe/sys-1", json={
        "schema_facts": {"columns": [{"name": "qty"}], "row_estimate": 1},
    }).json()
    assert body["questions"] == [] and (body.get("derived") or []) == []


# --------------------------------------------------------------------------- #
# Raw tables in the probe body (T-H wizard integration).
#
# The wizard has no persisted discovery, so it posts its discover
# tables VERBATIM and the server maps them through the ONE producer,
# schema_facts_from_tables. Precedence is pinned: schema_facts > tables > stored
# discovery. Client tables are UNTRUSTED — bounded server-side, and everything
# still lands through the sanitizing landing path.
# --------------------------------------------------------------------------- #
def test_probe_accepts_raw_tables_in_the_body(client, monkeypatch):
    """Kills dropping the ``body.tables`` branch: the store stub RAISES, so a
    store fallback degrades to EMPTY facts (the read is guarded) and neither the
    derived note nor the three questions can appear. Also kills mapping tables
    through anything but the one producer (the sets would differ)."""
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: "m-1")
    _stub_store(monkeypatch,
                _FakeStore(raises=AssertionError("store must not be read")))
    body = client.post("/api/onboard/guided/probe/sys-1",
                       json={"tables": _PG_TABLES}).json()
    derived = body.get("derived") or []
    assert [d["question_id"] for d in derived] == ["query-strategy"]
    assert [q["id"] for q in body["questions"]] == [
        "failure-taxonomy", "identifier-family", "retry-semantics"]


def test_explicit_schema_facts_beat_raw_tables(client):
    """Kills reordering the precedence chain: were tables consulted first, the
    rich tables would produce questions despite the barren explicit facts."""
    body = client.post("/api/onboard/guided/probe/sys-1", json={
        "schema_facts": {"columns": [{"name": "qty"}], "row_estimate": 1},
        "tables": _PG_TABLES,
    }).json()
    assert body["questions"] == [] and (body.get("derived") or []) == []


def test_raw_tables_beat_stored_discovery(client, monkeypatch):
    """Kills an ``or``-style fallback that prefers the store when the body
    tables produce empty facts — barren body tables must stay authoritative.

    The posted table yields NO columns at all, the emptiest facts the producer
    can emit, so any fallback keyed on empty/falsy facts would reach the rich
    stored discovery and surface its questions.
    """
    _stub_store(monkeypatch, _FakeStore(_PG_TABLES))
    body = client.post("/api/onboard/guided/probe/sys-1", json={
        "tables": [{"name": "t", "columns": []}],
    }).json()
    assert body["questions"] == [] and (body.get("derived") or []) == []


def test_absurd_table_payload_is_bounded_not_500(client, monkeypatch):
    """Kills removing the producer caps: an absurd client payload must be capped
    (house style), answered with 200, and never widen the question set."""
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: "m-1")
    payload = [{"name": f"t{i}",
                "columns": [{"name": f"t{i}_x{j}"} for j in range(50)],
                "row_count": 3}
               for i in range(300)]
    r = client.post("/api/onboard/guided/probe/sys-1", json={"tables": payload})
    assert r.status_code == 200
    assert len(r.json()["questions"]) <= 4


def test_hostile_column_name_in_raw_tables_never_lands_unsanitized(client, monkeypatch):
    """Kills bypassing the landing sanitizer for answers derived from raw body
    tables: every description that lands must pass validate_untrusted."""
    from agents.untrusted_text import validate_untrusted
    captured = []
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: captured.append(kw) or "m-1")
    client.post("/api/onboard/guided/probe/sys-1", json={
        "tables": [{"name": "t", "columns": [{"name": "state"}],
                    "row_count": 5_000_000}],
    })
    assert captured, "expected a derived proposal from the raw tables"
    for kw in captured:
        desc = kw["description"]
        assert validate_untrusted(desc, max_len=2000, field="'t'",
                                  label="answer") is None, desc


def test_non_finite_row_count_from_the_store_does_not_500(client, monkeypatch):
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: "m-1")
    _stub_store(monkeypatch, _FakeStore([{
        "name": "t", "columns": [{"name": "state"}], "row_count": float("inf")}]))
    r = client.post("/api/onboard/guided/probe/sys-1", json={})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Audit attribution pin
# --------------------------------------------------------------------------- #
def test_confirm_audits_with_the_authenticated_principal(client, monkeypatch):
    """A constant actor makes a guided write untraceable to a person."""
    events = []
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: True)
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"s1"})
    monkeypatch.setattr("agents.connection_resolver.audit_connection_event",
                        lambda action, sid, actor="", outcome="", **kwargs: events.append(
                            (action, sid, actor, outcome)))
    client.post("/api/onboard/guided/confirm-context",
                json={"systems": ["s1"], "business_context": "a job on a route"})
    assert events and events[0][0] == "business_context"
    # EQUALITY, not "!= the old constant": a regression to any NEW constant would
    # satisfy an inequality assertion while still being untraceable to a person.
    from agents.context_interview.routes import _actor

    class _Req:
        state = type("S", (), {"user": type("U", (), {"email": "dev@localhost"})()})()

    assert _actor(_Req()) == "dev@localhost"
    assert events[0][2] not in ("", "guided-onboarding")


# --------------------------------------------------------------------------- #
# ok-audit forensic extras (parity with the admin PUT).
# --------------------------------------------------------------------------- #
def test_confirm_ok_audit_carries_forensic_extras(client, monkeypatch):
    """Kills dropping ``extra`` entirely, hashing the RAW instead of the
    sanitized text, and hardcoding ``length`` — the expectations are computed
    from the same sanitizer the route uses."""
    from agents.business_context import context_hash, sanitize_business_context
    captured = []
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: True)
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"s1"})
    monkeypatch.setattr(
        "agents.connection_resolver.audit_connection_event",
        lambda action, sid, actor="", outcome="", **kwargs: captured.append(
            (outcome, kwargs)))
    client.post("/api/onboard/guided/confirm-context",
                json={"systems": ["s1"], "business_context": "a job on a route"})
    clean = sanitize_business_context("a job on a route")
    assert captured and captured[0][0] == "ok"
    assert captured[0][1].get("extra") == {"context_hash": context_hash(clean),
                                           "enabled": True,
                                           "length": len(clean)}


def test_confirm_error_audit_carries_no_extra(client, monkeypatch):
    """Admin parity: the error outcome passes no extra (admin_routes.py's
    error path attaches none)."""
    captured = []
    monkeypatch.setattr("agents.business_context.write_business_context",
                        lambda sid, text, enabled: False)
    monkeypatch.setattr("agents.context_interview.routes._known_system_ids",
                        lambda: {"s1"})
    monkeypatch.setattr(
        "agents.connection_resolver.audit_connection_event",
        lambda action, sid, actor="", outcome="", **kwargs: captured.append(
            (outcome, kwargs)))
    client.post("/api/onboard/guided/confirm-context",
                json={"systems": ["s1"], "business_context": "a job on a route"})
    assert captured and captured[0][0] == "error"
    assert "extra" not in captured[0][1]


# --------------------------------------------------------------------------- #
# A rejected derivation must not echo the pre-sanitization text.
# --------------------------------------------------------------------------- #
def test_rejected_derivation_does_not_echo_the_text(client, monkeypatch):
    """Kills reverting to the unconditional ``entry["derived_text"] = d.text``:
    a REJECTED landing returns the outcome + reason code only."""
    from agents.context_interview.landing import OUTCOME_REJECTED, LandingResult
    monkeypatch.setattr(
        "agents.context_interview.routes.land_answer",
        lambda q, text, **kw: LandingResult(
            outcome=OUTCOME_REJECTED, question_id="query-strategy",
            landing="query_guidance", reason="secret_like_content"))
    body = client.post("/api/onboard/guided/probe/sys-1", json={
        "tables": [{"name": "t", "columns": [{"name": "state"}],
                    "row_count": 5_000_000}],
    }).json()
    derived = body.get("derived") or []
    assert derived, "expected the query-strategy derivation to fire"
    entry = derived[0]
    assert entry["outcome"] == "rejected"
    assert entry["reason"] == "secret_like_content"
    assert "derived_text" not in entry
    assert "evidence" not in entry


def test_non_rejected_derivation_still_carries_text_and_evidence(client, monkeypatch):
    """Guards against over-removal: accepted derivations keep both keys — the
    steward reviews the derived text against the evidence that produced it."""
    monkeypatch.setattr("agents.self_learning.log_proposed_mapping",
                        lambda **kw: "m-1")
    body = client.post("/api/onboard/guided/probe/sys-1", json={
        "tables": [{"name": "t", "columns": [{"name": "state"}],
                    "row_count": 5_000_000}],
    }).json()
    derived = body.get("derived") or []
    assert derived and derived[0]["outcome"] in ("proposed", "draft")
    assert derived[0]["derived_text"]
    assert derived[0]["evidence"]


# --------------------------------------------------------------------------- #
# conflict is a settled outcome (pin).
#
# routes._apply_turn records the topic for every outcome EXCEPT rejected —
# INCLUDING conflict: a conflict means the question was answered, the answer
# just cannot be applied without deactivating an existing rule. Without the
# record the steward is re-asked an unanswerable question on every reload.
# --------------------------------------------------------------------------- #
def test_conflict_outcome_records_the_topic_so_it_is_not_re_asked(client, monkeypatch):
    """Kills restoring ``outcome in (proposed, draft, skipped)`` — the old
    tuple — in _apply_turn's record condition.

    The load-bearing assertion is the STATE one: authority is a
    deployment-scope topic, so no per-system probe would re-ask it anyway —
    what protects the steward from a re-ask loop is answered_topics carrying
    the topic after a conflict."""
    monkeypatch.setattr(
        "agents.context_interview.landing._active_authority_system",
        lambda cid: "other-sys")
    body = client.post("/api/onboard/guided/turn", json={
        "question_id": "authority-split", "answer": "the sensor system wins",
        "system_id": "sys-1", "concept_id": "machine-state"}).json()
    assert body["outcome"] == "conflict"
    from agents.context_interview import state
    assert "authority" in state.answered_topics("sys-1")


def test_rejected_outcome_does_not_record_the_topic(client):
    """Kills widening the record condition to ALL outcomes (``if True:``),
    which the conflict test alone cannot catch."""
    body = client.post("/api/onboard/guided/turn", json={
        "question_id": "unit-of-work",
        "answer": "creds AKIAIOSFODNN7EXAMPLE end"}).json()
    assert body["outcome"] == "rejected"
    from agents.context_interview import state
    assert "unit-of-work" not in state.answered_topics()
