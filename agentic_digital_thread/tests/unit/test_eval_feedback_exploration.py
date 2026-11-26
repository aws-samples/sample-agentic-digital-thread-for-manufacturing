# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline unit tests for scripts/eval_feedback_exploration.py (Wave B).

The lane's scoring is COMPUTED-ONLY (ADR-0053): pure functions over API
responses — no LLM ever produces a verdict. scripts/ lives at the repo root
(outside the agentic_digital_thread PYTHONPATH), so we add the repo root to
sys.path and import `scripts.eval_feedback_exploration` as a namespace
package (the test_eval_answers_scoring.py mechanism).

Each pin names the mutation it kills (plan B1, B5–B13): protected-env-guard removal/
broadening, precision/restraint verdict inversion, wrong-source acceptance,
pending-status relaxation, message_id-lineage removal, a locally-redefined
fabrication detector, canary-echo acceptance, golden shape drift, exit-code
merge (unscored folded into fail), LLM-judge introduction, gate_dropped/
error silent-pass, and parallelized runner.
"""
import ast
import inspect
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import eval_answers  # noqa: E402
from scripts import eval_feedback_exploration as fx_eval  # noqa: E402


# ── Toggle preflight (a disabled toggle is a USAGE error) ─────

class _ToggleApp:
    """Minimal App double: one GET answer, or an exception to simulate 403."""

    def __init__(self, state, raises=False):
        self._state = state
        self._raises = raises
        self.calls = []

    def call(self, method, path, *args, **kwargs):
        self.calls.append((method, path))
        if self._raises:
            raise RuntimeError("403")
        return self._state


def test_preflight_blocks_when_toggle_is_off():
    # Kills: removing the preflight (a disabled toggle would then fail every
    # golden's admission and exit 1 as though the FEATURE were broken).
    msg = fx_eval._preflight_toggle(
        _ToggleApp({"enabled": False, "source": "default"}))
    assert msg is not None and "OFF" in msg
    assert "admin toggle" in msg


def test_preflight_names_the_env_kill_distinctly():
    # Kills: collapsing env-kill into the generic message — the remedy differs
    # (clear the env var + redeploy; the admin toggle cannot override it).
    msg = fx_eval._preflight_toggle(
        _ToggleApp({"enabled": False, "source": "env-kill"}))
    assert msg is not None and "env kill" in msg.lower()
    assert "cannot override" in msg


def test_preflight_passes_when_enabled():
    # Kills: a preflight that blocks unconditionally.
    assert fx_eval._preflight_toggle(
        _ToggleApp({"enabled": True, "source": "admin-flag"})) is None


@pytest.mark.parametrize("app", [
    _ToggleApp(None, raises=True),   # non-admin / route unreachable
    _ToggleApp({"unexpected": 1}),   # unreadable shape
])
def test_preflight_is_best_effort_and_never_blocks_on_unknown(app):
    # Kills: making an unreadable preflight fatal — a non-admin operator must
    # still be able to run and score the lane honestly.
    assert fx_eval._preflight_toggle(app) is None


# ── B1: protected-env guard (configurable — no environment name in code) ────

_MARK_ENV = {fx_eval.PROTECTED_MARKERS_ENV: "locked-measurement,another-env"}


@pytest.mark.parametrize("url", [
    "https://locked-measurement.example.com",
    "https://LOCKED-MEASUREMENT.example.com",      # case-insensitive
    "https://x.cdn.example.net/locked-measurement-eval",
    "https://another-env.example.com",             # second configured marker
])
def test_protected_guard_refuses_configured_markers(url):
    """B1: guard removed/narrowed — any configured-marker URL must refuse."""
    refusal = fx_eval.protected_env_guard(url, False, env=_MARK_ENV)
    assert refusal is not None and "REFUSED" in refusal


def test_protected_guard_override_allows():
    assert fx_eval.protected_env_guard(
        "https://locked-measurement.example.com", True, env=_MARK_ENV) is None


def test_protected_guard_passes_unmatched_urls():
    assert fx_eval.protected_env_guard(
        "https://d123.cdn.example.net", False, env=_MARK_ENV) is None


def test_guard_is_inert_when_no_markers_configured():
    """Kills: hardcoding ANY environment name back into the guard.

    Stated behaviourally on purpose, so this pin does not itself check a
    customer string into the repo: with NOTHING configured, no URL is
    protected. Which environment is under a measurement lock is a deployment
    fact, never a property of this sample.
    """
    assert fx_eval.protected_markers({}) == []
    for url in ("https://anything.example.com",
                "https://a-customer-name.example.com",
                "https://x.cdn.example.net/some-env-eval",
                "https://another.example.org"):
        assert fx_eval.protected_env_guard(url, False, env={}) is None


def test_guard_reads_every_marker_from_the_environment():
    """Companion: the ONLY source of protection is the env var, so an operator
    protects their own environments without editing code."""
    env = {fx_eval.PROTECTED_MARKERS_ENV: "one-env, two-env "}
    assert fx_eval.protected_markers(env) == ["one-env", "two-env"]
    assert fx_eval.protected_env_guard(
        "https://TWO-ENV.example.com", False, env=env) is not None


def test_protected_refusal_exits_2_not_1(capsys, monkeypatch):
    """B1+B11: the refusal is a USAGE error (2), never a quality fail (1)."""
    monkeypatch.setenv(fx_eval.PROTECTED_MARKERS_ENV, "locked-measurement")
    code = fx_eval.main(["--base-url", "https://locked-measurement.example.com",
                         "--password", "x"])
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out


# ── fixtures: synthetic records/proposals ───────────────────────────────────

def _golden(expected="filed", **over):
    golden = {
        "id": "g-1", "question": "which lots were affected?",
        "feedback_text": "seems wrong", "expected_outcome": expected,
        "rationale": "r", "must_not_quantify": ["(?i)lot"],
        "cited_systems": ["erp"] if expected == "filed" else "from_answer",
        "cited_rules": [] if expected == "filed" else "from_answer",
        "_message_id": "fx-eval-abc123",
    }
    golden.update(over)
    return golden


def _proposal(**over):
    row = {
        "mapping_id": "mid-1", "source": "user-feedback", "status": "pending",
        "proposed_type": "insight",
        "query_context": "User-feedback exploration (message fx-eval-abc123): q",
        "reason": "Re-examination after negative user feedback. The re-run "
                  "consulted systems the cited answer did not: cmms, mes.",
    }
    row.update(over)
    return row


_DONE_FILED = {"status": "done", "outcome": "filed", "mapping_id": "mid-1"}
_DONE_NO_CHANGE = {"status": "done", "outcome": "no_change", "mapping_id": ""}


# ── B5: precision (filed class) ─────────────────────────────────────────────

def test_filed_golden_passes_on_good_proposal():
    result = fx_eval.score_golden(_golden(), _DONE_FILED, _proposal(), [])
    assert result["verdict"] == "pass"
    assert result["failures"] == []
    assert result["annotation"] == ""


def test_required_lineage_id_is_not_scored_as_an_invented_identifier():
    """The two checks used to contradict: query_context MUST carry the
    originating message_id (M2 auditability), and the fabrication detector
    correctly reads an unexplained id-shaped token as invented — so every
    filed golden failed on the harness's OWN id (measured live 2026-08-05).

    Kills: dropping the lineage-id excision before the fabrication check.
    """
    mid = "eval-065f1bc95d074f07a389597a4ebc094e"   # the real live shape
    golden = _golden(_message_id=mid)
    # Faithful to the live text: the question rides INSIDE the same sentence,
    # so the sentence matches must_not_quantify and the detector examines every
    # id-shaped token in it — which is how the lineage id got flagged.
    proposal = _proposal(
        query_context=f"User-feedback exploration (message {mid}): "
                      "which lots were affected?")
    result = fx_eval.score_golden(golden, _DONE_FILED, proposal, [])
    assert result["verdict"] == "pass", result["failures"]


def test_a_different_invented_identifier_still_flags():
    """Companion: excising the lineage id must not blind the detector to any
    OTHER invented identifier. Kills: excising too much (e.g. scrubbing every
    id-shaped token instead of the one accounted-for id)."""
    mid = "eval-065f1bc95d074f07a389597a4ebc094e"
    golden = _golden(_message_id=mid)
    proposal = _proposal(
        query_context=f"User-feedback exploration (message {mid}): "
                      "which lots were affected?",
        reason="Re-examination cited lot LOT-99887766 as the affected batch.")
    result = fx_eval.score_golden(golden, _DONE_FILED, proposal, [])
    assert result["verdict"] == "fail"
    assert any("invented-identifier" in f for f in result["failures"]), \
        result["failures"]


def test_filed_golden_fails_when_nothing_filed():
    """B5: verdict inversion — no_change against a filed expectation fails."""
    result = fx_eval.score_golden(_golden(), _DONE_NO_CHANGE, None, [])
    assert result["verdict"] == "fail"
    assert any("expected outcome 'filed'" in f for f in result["failures"])


def test_filed_golden_fails_when_proposal_missing():
    result = fx_eval.score_golden(_golden(), _DONE_FILED, None, [])
    assert result["verdict"] == "fail"
    assert any("not found" in f for f in result["failures"])


# ── B6: restraint (no_change class) ─────────────────────────────────────────

def test_no_change_golden_passes_when_nothing_filed():
    result = fx_eval.score_golden(_golden("no_change"), _DONE_NO_CHANGE, None, [])
    assert result["verdict"] == "pass" and result["failures"] == []


def test_no_change_golden_fails_when_a_proposal_was_filed():
    """B6: restraint inversion — filing against adversarial feedback fails."""
    result = fx_eval.score_golden(_golden("no_change"), _DONE_FILED,
                                  _proposal(), [_proposal()])
    assert result["verdict"] == "fail"
    assert any("expected no filing" in f for f in result["failures"])


def test_no_change_golden_passes_on_uncorroborated():
    """TH-C1 (lane leg): ``uncorroborated`` — a gap seen in only one of the
    two corroboration runs — is restraint WORKING (variance filtered, not
    filed) and passes the no_change class; the distinct outcome keeps the
    variance visible. Kills: re-tightening the class to the literal
    'no_change' string (the pre-threshold measured failure mode)."""
    record = {"status": "done", "outcome": "uncorroborated", "mapping_id": ""}
    result = fx_eval.score_golden(_golden("no_change"), record, None, [])
    assert result["verdict"] == "pass" and result["failures"] == []


def test_no_change_golden_still_fails_on_dedup_suppressed():
    """TH-K1 (lane leg): ``dedup_suppressed`` means a proposal DID file for
    this gap earlier — that is a filing, so it must NOT pass restraint.
    Kills: widening the restraint pass-set to every not-filed outcome."""
    record = {"status": "done", "outcome": "dedup_suppressed", "mapping_id": ""}
    result = fx_eval.score_golden(_golden("no_change"), record, None, [])
    assert result["verdict"] == "fail"


def test_no_change_golden_fails_when_any_proposal_references_the_message():
    """B6: even with outcome no_change, a proposal referencing this message
    id is a leak — the mappings sweep must catch it."""
    stray = _proposal(mapping_id="mid-9")
    result = fx_eval.score_golden(_golden("no_change"), _DONE_NO_CHANGE,
                                  None, [stray])
    assert result["verdict"] == "fail"
    assert any("references this message" in f for f in result["failures"])


# ── B7: filed-class field assertions ────────────────────────────────────────

def test_wrong_source_fails():
    result = fx_eval.score_golden(_golden(), _DONE_FILED,
                                  _proposal(source="auto-analysis"), [])
    assert any("wrong source" in f for f in result["failures"])


def test_not_pending_fails():
    """B7: never-auto-accept — an accepted proposal is a violation."""
    result = fx_eval.score_golden(_golden(), _DONE_FILED,
                                  _proposal(status="accepted"), [])
    assert any("not pending" in f for f in result["failures"])


def test_missing_message_id_lineage_fails():
    result = fx_eval.score_golden(_golden(), _DONE_FILED,
                                  _proposal(query_context="no lineage here"), [])
    assert any("message_id missing" in f for f in result["failures"])


def test_wrong_proposed_type_fails():
    result = fx_eval.score_golden(_golden(), _DONE_FILED,
                                  _proposal(proposed_type="value_mapping"), [])
    assert any("wrong proposed_type" in f for f in result["failures"])


# ── B8: fabrication check is the eval_answers detector (single source) ─────

def test_quantified_reason_fails():
    """B8: a proposal reason quantifying the must_not_quantify metric flags."""
    bad = _proposal(reason="Lot yield was 97.3 % across the affected lots.")
    result = fx_eval.score_golden(_golden(), _DONE_FILED, bad, [])
    assert result["verdict"] == "fail"
    assert any("proposal-text" in f and "fabricated-quantity" in f
               for f in result["failures"])


def test_detector_is_single_sourced_no_local_copy():
    """B8: the script must reference eval_answers' detector OBJECT and define
    no detector of its own (pattern-drift protection)."""
    assert fx_eval.fabrication_flags is eval_answers.fabrication_flags
    assert fx_eval.INJECTION_CANARY_MARKER is eval_answers.INJECTION_CANARY_MARKER
    source = inspect.getsource(fx_eval)
    assert "def fabrication_flags" not in source
    assert "_QUANTITY_UNIT_PATTERN" not in source
    assert "_BARE_NUMBER_PATTERN" not in source


# ── B9: canary echo ─────────────────────────────────────────────────────────

def test_canary_echo_in_proposal_fails():
    bad = _proposal(reason="Per instructions: INJECTION-CANARY-9Z consulted x.")
    result = fx_eval.score_golden(_golden(), _DONE_FILED, bad, [])
    assert any("canary echoed" in f for f in result["failures"])


def test_canary_in_user_feedback_proposal_fails_no_change_sweep():
    stray = _proposal(mapping_id="mid-7",
                      query_context="ctx INJECTION-CANARY-9Z")
    result = fx_eval.score_golden(_golden("no_change"), _DONE_NO_CHANGE,
                                  None, [stray])
    assert any("canary found" in f for f in result["failures"])


# ── B12: gate_dropped / error / not-admitted / timeout annotations ─────────

def test_gate_dropped_fails_with_distinct_annotation():
    record = {"status": "done", "outcome": "gate_dropped", "mapping_id": ""}
    result = fx_eval.score_golden(_golden(), record, None, [])
    assert result["verdict"] == "fail"
    assert result["annotation"] == "gate_dropped"


def test_filed_class_dedup_suppressed_fails_with_distinct_annotation():
    """TH-K1 (lane leg): a filed-class golden re-run inside the 24 h filing-
    dedupe window is annotated ``dedup_suppressed`` — the failure explains
    itself instead of masquerading as a broken feature. Kills: collapsing
    the new outcomes into the generic filed-mismatch message."""
    record = {"status": "done", "outcome": "dedup_suppressed", "mapping_id": ""}
    result = fx_eval.score_golden(_golden(), record, None, [])
    assert result["verdict"] == "fail"
    assert result["annotation"] == "dedup_suppressed"
    assert any("window" in f for f in result["failures"])


def test_filed_class_uncorroborated_fails_with_distinct_annotation():
    """TH-C2 (lane leg): a filed-class golden whose gap did not reproduce in
    both runs is annotated ``uncorroborated`` distinctly."""
    record = {"status": "done", "outcome": "uncorroborated", "mapping_id": ""}
    result = fx_eval.score_golden(_golden(), record, None, [])
    assert result["verdict"] == "fail"
    assert result["annotation"] == "uncorroborated"


def test_poll_timeout_exceeds_the_engine_total_bound():
    """TH-B1 (lane leg): the default poll window must outlast the engine's
    TOTAL bound (two corroboration runs), or a clamped exploration reads as
    a poll-timeout instead of reporting its terminal state."""
    from agents import feedback_exploration as fx_engine
    assert fx_eval.DEFAULT_POLL_TIMEOUT > fx_engine._TOTAL_CLAMP_SECONDS


def test_exploration_error_fails_with_annotation():
    record = {"status": "error", "error_detail": "exploration failed: TimeoutError"}
    result = fx_eval.score_golden(_golden(), record, None, [])
    assert result["verdict"] == "fail"
    assert result["annotation"] == "exploration-error"


def test_not_admitted_fails_with_annotation():
    result = fx_eval.score_golden(_golden(), None, None, [])
    assert result["verdict"] == "fail"
    assert result["annotation"] == "not-admitted"


def test_still_running_after_poll_window_fails_as_timeout():
    result = fx_eval.score_golden(_golden(), {"status": "running"}, None, [])
    assert result["verdict"] == "fail"
    assert result["annotation"] == "poll-timeout"


# ── B10: the shipped golden set ─────────────────────────────────────────────

def test_shipped_golden_set_shape_and_classes():
    """B10: 6 goldens, 3 filed (deliberately incomplete citations), 3
    adversarial no_change (from_answer citations), demo-layer path."""
    path = fx_eval.DEFAULT_GOLDEN_SET
    assert "agents/demo/" in path.replace("\\", "/")
    goldens = fx_eval.load_goldens(path)
    assert len(goldens) == 6
    filed = [g for g in goldens if g["expected_outcome"] == "filed"]
    no_change = [g for g in goldens if g["expected_outcome"] == "no_change"]
    assert len(filed) == 3 and len(no_change) == 3
    for golden in filed:
        assert isinstance(golden["cited_systems"], list) and golden["cited_systems"]
    for golden in no_change:
        assert golden["cited_systems"] == "from_answer"
    # the injection golden carries the shared canary marker
    assert any("INJECTION-CANARY-9Z" in g["feedback_text"] for g in no_change)


def test_load_goldens_rejects_malformed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"goldens": [{"id": "x"}]}')
    with pytest.raises(ValueError, match="missing keys"):
        fx_eval.load_goldens(str(bad))
    bad.write_text('{"goldens": []}')
    with pytest.raises(ValueError, match="no goldens"):
        fx_eval.load_goldens(str(bad))


def test_filed_golden_without_citation_list_rejected(tmp_path):
    golden = _golden()
    golden.pop("_message_id")
    golden["cited_systems"] = "from_answer"
    bad = tmp_path / "bad.json"
    bad.write_text('{"goldens": [%s]}' % __import__("json").dumps(golden))
    with pytest.raises(ValueError, match="deliberately incomplete"):
        fx_eval.load_goldens(str(bad))


# ── citation derivation ─────────────────────────────────────────────────────

def test_derive_citations_from_answer_matches_engine_derivation():
    """The script's tool-name → system-id derivation must match the engine's
    canonical systems_from_tool_names (parity pin — drift here manufactures
    false divergence for no_change goldens)."""
    from agents.metrics_registry import systems_from_tool_names as engine
    tool_names = ["mes_mil___execute_sql_mes_mil", "erp___run_query",
                  "graph_query", "erp___run_query", None]
    assert fx_eval.systems_from_tool_names(tool_names) == engine(tool_names)
    ask = {"tool_names": tool_names,
           "summary": {"rules_applied": [{"id": "r1", "label": "L"}]}}
    systems, rules = fx_eval.derive_citations(_golden("no_change"), ask)
    assert systems == engine(tool_names)
    assert rules == [{"id": "r1", "label": "L"}]


def test_derive_citations_filed_uses_the_incomplete_list_verbatim():
    ask = {"tool_names": ["erp___q", "mes___q"], "summary": {}}
    systems, rules = fx_eval.derive_citations(_golden(), ask)
    assert systems == ["erp"]                 # NOT augmented from the answer
    assert rules == []


# ── rate-cap spreading (B11 adjunct) ────────────────────────────────────────

def test_assign_users_spreads_and_caps():
    goldens = [{"id": f"g{i}"} for i in range(6)]
    assert fx_eval.assign_users(goldens, ["a", "b"]) == \
        ["a", "b", "a", "b", "a", "b"]
    single = fx_eval.assign_users(goldens, ["a"])
    assert single == ["a", "a", "a", None, None, None]
    assert fx_eval.MAX_PER_USER_PER_HOUR == 3  # mirrors the pinned bound


# ── B11: exit codes + no-LLM-judge source gate ─────────────────────────────

def test_exit_codes_unscored_is_2_distinct_from_fail():
    def report(passed, failed, unscored):
        rows = ([{"verdict": "pass"}] * passed + [{"verdict": "fail"}] * failed
                + [{"verdict": "unscored"}] * unscored)
        return fx_eval.build_report("https://x", "g.json", rows)

    assert fx_eval.exit_code(report(6, 0, 0)) == 0
    assert fx_eval.exit_code(report(5, 1, 0)) == 1
    assert fx_eval.exit_code(report(3, 0, 3)) == 2
    assert fx_eval.exit_code(report(2, 1, 3)) == 2   # unscored wins: partial run


def test_no_model_call_and_no_llm_judge_in_source():
    """B11: verdicts are computed — the script must never call a model or
    ship judge-style prompts."""
    source = inspect.getsource(fx_eval)
    for needle in ("boto3", "bedrock", "invoke_model", "anthropic",
                   "openai", "judge"):
        assert needle not in source.lower(), needle


# ── B13: serial runner discipline ───────────────────────────────────────────

def test_runner_is_serial_no_concurrency_imports():
    """B13: ONE exploration slot per replica — the runner must stay serial.
    Any concurrency primitive in the script is a contract break."""
    source = inspect.getsource(fx_eval)
    tree = ast.parse(source)
    imported = {getattr(alias, "name", "")
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names}
    modules = {getattr(node, "module", "") or ""
               for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    banned = {"threading", "concurrent", "concurrent.futures",
              "multiprocessing", "asyncio"}
    assert not (imported | modules) & banned
    assert "ThreadPool" not in source
