# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``story_fields`` goldens (both eval lanes).

A workshop page asks about the account's own run by name, and the run
number changes with every account. A golden
that asks the page's question names the run through ``story_fields``: each
``{name}`` in its text is filled from the deployed story's answer key (the body
``GET /api/admin/workshop/story`` serves) before the CLI lane asks it. When
there is no key or it lacks the value the golden is SKIPPED with a named
reason, so the model never sees a bare placeholder: every {name} in the text
must be a declared field, and a {name} still in the text after filling (one
a story value brought in) skips too. The server lane has no key (it stays
free of the demo package), so it always skips such a golden. Story values
here are synthetic sentinels; the real run format is the story lookup's to
prove (test_workshop_story_lookup.py).
"""
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT), str(_ROOT / "agentic_digital_thread")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents import eval_core, eval_runs  # noqa: E402
from scripts import eval_answers  # noqa: E402

_EXPECT = {"systems_min": 1}
_NAMED = {"id": "g-named", "text": "Which customers got batch {run}?",
          "expect": _EXPECT, "story_fields": {"run": "failed_batch.history_run_number"}}
_STORY = {"story": "live", "failed_batch": {"run_number": "SENTINEL-NEWEST-RUN",
                                            "history_run_number": "SENTINEL-HISTORY-RUN"}}
_FILLED = "Which customers got batch SENTINEL-HISTORY-RUN?"
_PACKAGED_ID = "g-workshop-live-m7b-lot-total"


def _no_story():
    raise AssertionError("a golden without story_fields must not read the story")


def test_fills_each_field_from_the_answer_key():
    filled, reason = eval_core.fill_story_fields(_NAMED, lambda: _STORY)
    assert reason is None
    assert filled["text"] == _FILLED
    assert _NAMED["text"] == "Which customers got batch {run}?"  # input untouched


def test_a_plain_golden_never_reads_the_story():
    plain = {"id": "g-plain", "text": "q {not a field}", "expect": _EXPECT}
    assert eval_core.fill_story_fields(plain, _no_story) == (plain, None)


_NO_KEY = "story_fields: no story answer key in this lane or deployment"


@pytest.mark.parametrize("story", [None, []])
def test_no_answer_key_skips_with_a_named_reason(story):
    filled, reason = eval_core.fill_story_fields(_NAMED, lambda: story)
    assert reason == _NO_KEY
    assert "{run}" in filled["text"]


@pytest.mark.parametrize("story", [{}, {"failed_batch": None},
                                   {"failed_batch": {"history_run_number": None}},
                                   {"failed_batch": {"history_run_number": "  "}},
                                   {"failed_batch": {"history_run_number": 7}}])
def test_a_missing_value_skips_with_a_named_reason(story):
    filled, reason = eval_core.fill_story_fields(_NAMED, lambda: story)
    assert reason == ("story_fields run: failed_batch.history_run_number "
                      "not in the story answer key")
    assert "{run}" in filled["text"]


@pytest.mark.parametrize("fields", ["run", {}, {"run": ""}, {"run": 1},
                                    {"Run": "failed_batch.history_run_number"},
                                    {"other": "failed_batch.history_run_number"}])
def test_a_malformed_field_map_raises_at_parse_and_at_fill(fields):
    q = dict(_NAMED, story_fields=fields)
    with pytest.raises(ValueError, match="story_fields"):
        eval_core.parse_golden_set({"version": 1, "questions": [q]})
    with pytest.raises(ValueError, match="story_fields"):
        eval_core.fill_story_fields(q, lambda: _STORY)


# Two placeholders, one mapping: the second {name} would reach the model bare.
_TWO_ONE = dict(_NAMED, text="Which customers got batch {run} at {site}?")


@pytest.mark.parametrize("text", [
    "Which customers got batch {run} at {site}?",
    "Which customers got batch {run} at {Site}?",
    "Which customers got batch {run} on {line_2}?",
])
def test_every_placeholder_in_the_text_must_be_declared(text):
    q = dict(_NAMED, text=text)
    with pytest.raises(ValueError, match="story_fields"):
        eval_core.parse_golden_set({"version": 1, "questions": [q]})
    with pytest.raises(ValueError, match="story_fields"):
        eval_core.fill_story_fields(q, lambda: _STORY)


def test_braces_that_are_not_placeholders_are_left_alone():
    q = dict(_NAMED, text='Which customers got batch {run}? Reply as {"rows": [ ]} or { }.')
    eval_core.parse_golden_set({"version": 1, "questions": [q]})
    filled, reason = eval_core.fill_story_fields(q, lambda: _STORY)
    assert reason is None
    assert filled["text"] == ('Which customers got batch SENTINEL-HISTORY-RUN? '
                              'Reply as {"rows": [ ]} or { }.')


def test_two_declared_fields_fill_in_one_pass():
    q = dict(_NAMED, text="Batch {run} or {newest}, and {run} again?",
             story_fields={"run": "failed_batch.history_run_number",
                           "newest": "failed_batch.run_number"})
    filled, reason = eval_core.fill_story_fields(q, lambda: _STORY)
    assert reason is None
    assert filled["text"] == ("Batch SENTINEL-HISTORY-RUN or SENTINEL-NEWEST-RUN, and "
                              "SENTINEL-HISTORY-RUN again?")


@pytest.mark.parametrize("value,left", [
    ("SENTINEL {site}", "{site}"),
    ("SENTINEL {run}", "{run}"),
    ("SENTINEL {newest}", "{newest}"),
])
def test_a_placeholder_a_story_value_brings_in_skips(value, left):
    # One pass over the question text: a value is never itself filled, so a
    # {name} it carries stays and the golden is skipped, never asked.
    q = dict(_NAMED, text="Batch {run} or {newest}?",
             story_fields={"run": "failed_batch.history_run_number",
                           "newest": "failed_batch.run_number"})
    story = {"failed_batch": {"history_run_number": value, "run_number": "SENTINEL-NEWEST-RUN"}}
    filled, reason = eval_core.fill_story_fields(q, lambda: story)
    assert reason == f"story_fields: {left} still in the question after filling"
    assert filled is q


def _packaged():
    return next(q for q in eval_core.load_golden_set(eval_core.default_golden_set_path())
                if q["id"] == _PACKAGED_ID)


def test_the_lot_golden_names_the_run_the_way_the_page_does():
    # The module page has the attendee paste the run they wrote down; an
    # unnamed "the batch that failed" picks a different run on an aged story.
    question = _packaged()
    assert question["story_fields"] == {"run": "failed_batch.history_run_number"}
    assert "parts from batch {run} or from any other batch" in question["text"]
    assert "the batch that failed" not in question["text"]
    filled, reason = eval_core.fill_story_fields(question, lambda: _STORY)
    assert reason is None
    assert "parts from batch SENTINEL-HISTORY-RUN or from any other batch" in filled["text"]


class _App:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        outcome = self.routes[path]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _record_asks(monkeypatch):
    asked = []

    def ask(app, args, namespace, question, index, total, unsticks):
        asked.append(question["text"])
        return eval_core._skipped_row(question, "stub")
    monkeypatch.setattr(eval_answers, "_repeat_rows", ask)
    return asked


def test_cli_fills_from_the_story_route_once_per_run(monkeypatch):
    asked = _record_asks(monkeypatch)
    app = _App({eval_answers.STORY_ROUTE: _STORY})
    plain = {"id": "g-plain", "text": "q", "expect": _EXPECT}
    eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False), None,
                               [plain, _NAMED, dict(_NAMED, id="g-named-2")], [])
    assert asked == ["q", _FILLED, _FILLED]
    assert app.calls == [("GET", "/api/admin/workshop/story")]


def test_cli_without_story_goldens_never_reads_the_story(monkeypatch):
    _record_asks(monkeypatch)
    app = _App({})
    plain = {"id": "g-plain", "text": "q", "expect": _EXPECT}
    eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False), None,
                               [plain], [])
    assert app.calls == []


def test_cli_unreadable_story_skips_the_named_golden(monkeypatch, capsys):
    asked = _record_asks(monkeypatch)
    app = _App({eval_answers.STORY_ROUTE: urllib.error.HTTPError(
        "u", 404, "Not Found", {}, None)})
    rows = eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False),
                                      None, [_NAMED], [])
    assert asked == []
    assert rows[0]["verdict"] == "skipped"
    assert rows[0]["skip_reason"] == _NO_KEY
    assert "story_fields goldens skip" in capsys.readouterr().out


def test_cli_refuses_a_set_with_an_undeclared_placeholder(monkeypatch, tmp_path, capsys):
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"version": 1, "questions": [_TWO_ONE]}), encoding="utf-8")

    def no_app(*a, **k):
        raise AssertionError("a refused set must not log in or ask")
    monkeypatch.setattr(eval_answers.smoke, "App", no_app)
    code = eval_answers.main(["--base-url", "http://127.0.0.1:9", "--password", "p",
                              "--golden-set", str(golden)])
    out = capsys.readouterr().out
    assert code == 2
    assert "golden set unreadable" in out and "story_fields" in out


def test_cli_rows_never_ask_a_question_with_an_undeclared_placeholder(monkeypatch):
    # The set parser refuses it first; called directly the runner still
    # fails closed rather than sending the text.
    asked = _record_asks(monkeypatch)
    app = _App({eval_answers.STORY_ROUTE: _STORY})
    with pytest.raises(ValueError, match="story_fields"):
        eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False), None,
                                   [_TWO_ONE], [])
    assert asked == []


def test_cli_skips_when_a_story_value_brings_a_placeholder(monkeypatch):
    asked = _record_asks(monkeypatch)
    story = {"failed_batch": {"history_run_number": "SENTINEL {site}"}}
    app = _App({eval_answers.STORY_ROUTE: story})
    rows = eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False),
                                      None, [_NAMED], [])
    assert asked == []
    assert rows[0]["verdict"] == "skipped"
    assert rows[0]["skip_reason"] == "story_fields: {site} still in the question after filling"


def test_cli_env_gate_wins_before_the_story_is_read(monkeypatch):
    _record_asks(monkeypatch)
    app = _App({})
    gated = dict(_NAMED, skip_unless_env_equals={"EVAL_STORY_FIELDS_TEST": "on"})
    monkeypatch.delenv("EVAL_STORY_FIELDS_TEST", raising=False)
    rows = eval_answers._collect_rows(app, SimpleNamespace(no_seed_fixtures=False),
                                      None, [gated], [])
    assert rows[0]["skip_reason"].startswith("skip_unless_env_equals")
    assert app.calls == []


def _server_asks(monkeypatch):
    monkeypatch.setattr(eval_runs, "_connected_types", lambda: None)
    asked = []

    def ask(text, session_id, deadline):
        asked.append(text)
        return {"done": True, "answer": "a", "summary": {"system_queries": 1}}
    monkeypatch.setattr(eval_runs, "_ask_question", ask)
    return asked


def test_server_lane_skips_a_story_golden_without_asking(monkeypatch):
    # The server lane stays free of the demo package, so it has no answer
    # key: the golden is skipped by name, never asked with a bare {run}.
    asked = _server_asks(monkeypatch)
    for redact in (False, True):
        row = eval_runs._question_row("rid", 1, _NAMED, 1e18, redact)
        assert row["verdict"] == "skipped"
        assert row["skip_reason"] == _NO_KEY
    assert asked == []


def test_server_lane_still_asks_a_plain_golden(monkeypatch):
    asked = _server_asks(monkeypatch)
    row = eval_runs._question_row(
        "rid", 1, {"id": "g-plain", "text": "q", "expect": _EXPECT}, 1e18, False)
    assert asked == ["q"]
    assert row["verdict"] == "pass"
