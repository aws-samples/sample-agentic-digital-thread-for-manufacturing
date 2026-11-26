# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""How the Evals page reads AgentCore evaluation results, the figure check's included.

AgentCore writes one record per scored trace to the config's results log
group, in the OpenTelemetry GenAI evaluation shape: ``name`` is
``gen_ai.evaluation.result`` and the evaluator, score and label are
``attributes.gen_ai.evaluation.name`` / ``.score.value`` / ``.score.label``.
Measured on a deployment's real results log: the reader's earlier three
guessed formats returned no rows, while queries on these fields returned
every built-in's scored traces. So the reader runs two queries on these
fields over one log group and window (``EVAL_SCORE_QUERY``,
``EVAL_LABEL_QUERY``):

* every evaluator's value average (the judges' scores, ``Builtin.`` dropped);
* the figure check's results counted by its label field: PASS (1.0) and FAIL
  (0.0) give a pass rate, where 0 is a real result; NO_FIGURES and UNCHECKED
  carry no value, are counted beside the rate and never averaged in. Its
  ``name`` may be its name or its id (``<name>-<suffix>``).

The records below have the real shape, anonymised. ``_insights`` runs a
query over them the way Logs Insights does for the commands the reader's
queries (old and new) use, JSON flattened to dotted fields and ``@message``
the raw record, so the reader is checked end to end offline; the live log is
checked separately.

Run: pytest agentic_digital_thread/tests/unit/test_eval_figure_check_scores.py -q
"""
from __future__ import annotations

import json
import re

import pytest

import agents.agents_routes as ar
from agents.agents_routes import (
    FIGURE_CHECK_KEY,
    figure_check_counts,
    merge_figure_check,
    parse_eval_rows,
)
from agents.eval_figure_check import EVALUATOR_NAME, LABELS

_CONFIG = "adt_orchestrator_eval-AbCdE12345"
_FIGURE_CHECK_ID = f"{EVALUATOR_NAME}-FgHiJ67890"


# --------------------------------------------------------------------------- #
# Records in the real shape (anonymised), and a small Logs Insights.
# --------------------------------------------------------------------------- #
def _result(evaluator: str, value, label: str, *, n: int = 0,
            explanation: str = "The answer is supported.") -> dict:
    """One AgentCore evaluation result record; ``value`` None leaves it out,
    as for a label-only result."""
    attributes = {
        "gen_ai.evaluation.name": evaluator,
        "session.id": f"session-{n:028x}",
        "gen_ai.response.id": f"{n:032x}",
        "gen_ai.evaluation.explanation": explanation,
        "gen_ai.evaluation.score.label": label,
        "aws.bedrock_agentcore.online_evaluation_config.name": "adt_orchestrator_eval",
        "aws.bedrock_agentcore.evaluator.arn": f"arn:aws:bedrock-agentcore:::evaluator/{evaluator}",
        "aws.bedrock_agentcore.evaluation_level": "Trace",
    }
    if value is not None:
        attributes["gen_ai.evaluation.score.value"] = value
    record = {"resource": {"attributes": {"service.name": "adt_orchestrator.DEFAULT"}},
              "traceId": f"{n:032x}", "timeUnixNano": 1792692363801828082 + n,
              "severityNumber": 9, "name": "gen_ai.evaluation.result",
              "attributes": attributes, "onlineEvaluationConfigId": _CONFIG, "label": label}
    if value is not None:
        record["_aws"] = {"CloudWatchMetrics": [{"Namespace": "Bedrock-AgentCore/Evaluations",
                                                 "Metrics": [{"Name": evaluator}]}]}
        record[evaluator] = value
    return record


def _judges() -> list[dict]:
    return [_result("Builtin.Correctness", 1.0, "Correct", n=1),
            _result("Builtin.Correctness", 0.5, "Partially Correct", n=2),
            _result("Builtin.ResponseRelevance", 1.0, "Completely Yes", n=3),
            _result("Builtin.ResponseRelevance", 1.0, "Completely Yes", n=4),
            _result("Builtin.ResponseRelevance", 0.25, "Partially", n=5),
            _result("Builtin.Faithfulness", 0.9, "Mostly Yes", n=6)]


def _figure_checks(name: str = EVALUATOR_NAME) -> list[dict]:
    plan = [("PASS", 1.0), ("PASS", 1.0), ("PASS", 1.0), ("FAIL", 0.0),
            ("NO_FIGURES", None), ("NO_FIGURES", None), ("UNCHECKED", None)]
    return [_result(name, value, label, n=20 + i, explanation=f"The figure check said {label}.")
            for i, (label, value) in enumerate(plan)]


_JUDGE_SCORES = {"Correctness": {"avg": 0.75, "count": 2},
                 "ResponseRelevance": {"avg": 0.75, "count": 3},
                 "Faithfulness": {"avg": 0.9, "count": 1}}
_FIGURE_CHECK = {FIGURE_CHECK_KEY: {"avg": 0.75, "count": 4, "no_figures": 2, "unchecked": 1}}


def _flat(node, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    for key, value in node.items():
        if isinstance(value, dict):
            _flat(value, f"{prefix}{key}.", out)
        else:
            out[f"{prefix}{key}"] = value
    return out


def _field(name: str) -> str:
    return name.strip().strip("`")


def _insights(query: str, records: list[dict]) -> list[list[dict]]:
    """``query`` over ``records`` as Logs Insights runs it, for the commands
    the reader's queries use; an unknown command fails the test."""
    rows = [{**_flat(r), "@message": json.dumps(r, separators=(",", ":"))} for r in records]
    for command in (" ".join(c.split()) for c in re.split(r"\n\s*\|", query)):
        if command.startswith(("fields ", "sort ")):
            continue
        if m := re.fullmatch(r"filter (\S+) = \"([^\"]*)\"", command):
            rows = [r for r in rows if r.get(_field(m.group(1))) == m.group(2)]
        elif m := re.fullmatch(r"filter @message like /(.*)/", command):
            rows = [r for r in rows if re.search(m.group(1), r["@message"])]
        elif m := re.fullmatch(r"filter (ispresent\(\S+\)(?: and ispresent\(\S+\))*)", command):
            names = [_field(n) for n in re.findall(r"ispresent\((\S+?)\)", m.group(1))]
            rows = [r for r in rows if all(n in r for n in names)]
        elif m := re.fullmatch(r"parse @message /(.*)/", command):
            pattern = re.compile(m.group(1).replace("(?<", "(?P<"))
            rows = [{**r, **(found.groupdict() if (found := pattern.search(r["@message"]))
                             else {})} for r in rows]
        elif m := re.fullmatch(r"stats (.+) by (.+)", command):
            return _stats(m.group(1), m.group(2), rows)
        else:
            raise AssertionError(f"_insights does not know: {command!r}")
    return [[{"field": k, "value": str(v)} for k, v in r.items()] for r in rows]


def _stats(aggregates: str, keys: str, rows: list[dict]) -> list[list[dict]]:
    by = [re.fullmatch(r"(\S+)(?: as (\w+))?", k.strip()).groups() for k in keys.split(",")]
    groups: dict = {}
    for r in rows:
        groups.setdefault(tuple(r.get(_field(f)) for f, _a in by), []).append(r)
    out = []
    for key, members in groups.items():
        row = [{"field": alias or _field(f), "value": str(v)}
               for (f, alias), v in zip(by, key) if v is not None]
        for agg in aggregates.split(", "):
            func, arg, alias = re.fullmatch(r"(avg|count)\((\S*)\) as (\w+)", agg.strip()).groups()
            if func == "count":
                row.append({"field": alias, "value": str(len(members) if not arg else
                                                           sum(_field(arg) in m for m in members))})
                continue
            values = [float(m[_field(arg)]) for m in members if _field(arg) in m
                      and re.fullmatch(r"-?[\d.]+", str(m[_field(arg)]))]
            if values:
                row.append({"field": alias, "value": str(sum(values) / len(values))})
        out.append(row)
    return out


@pytest.fixture
def log(monkeypatch):
    """``_get_eval_scores`` over records per log group, through ``_insights``."""
    groups: dict = {}
    monkeypatch.setattr(ar, "_eval_candidate_groups", lambda _logs, _cid: list(groups))
    monkeypatch.setattr(ar, "_run_insights",
                        lambda _logs, group, query, _hours: _insights(query, groups[group]))

    class _Session:
        def client(self, _name):
            return object()

    def scores(by_group: dict) -> dict:
        groups.clear()
        groups.update(by_group)
        return ar._get_eval_scores(_Session(), _CONFIG, 24)
    return scores


# --------------------------------------------------------------------------- #
# The reader over the real shape.
# --------------------------------------------------------------------------- #
def test_every_evaluator_in_a_mixed_batch_is_read(log):
    assert log({"/results/a": _judges() + _figure_checks()}) == {**_JUDGE_SCORES,
                                                                **_FIGURE_CHECK}


def test_the_built_ins_show_when_there_is_no_figure_check_result(log):
    assert log({"/results/a": _judges()}) == _JUDGE_SCORES


def test_the_figure_check_is_read_under_its_id(log):
    assert log({"/results/a": _judges() + _figure_checks(_FIGURE_CHECK_ID)}) == {
        **_JUDGE_SCORES, **_FIGURE_CHECK}


def test_the_figure_check_alone_is_read(log):
    assert log({"/results/a": _figure_checks()}) == _FIGURE_CHECK


def test_only_answers_without_figures_show_no_pass_rate(log):
    records = [r for r in _figure_checks() if r["label"] in ("NO_FIGURES", "UNCHECKED")]
    assert log({"/results/a": records}) == {FIGURE_CHECK_KEY: {
        "avg": None, "count": 0, "no_figures": 2, "unchecked": 1}}


def test_the_label_comes_from_the_label_field_not_any_word_in_the_record(log):
    """A judge's explanation naming the figure check, and a figure check
    record whose explanation is another label, count nothing extra."""
    records = [_result("Builtin.Correctness", 1.0, "Correct", n=1,
                       explanation="FAIL"),
               _result("Builtin.Faithfulness", 1.0, "Yes", n=2,
                       explanation=f"{EVALUATOR_NAME} said NO_FIGURES"),
               _result(EVALUATOR_NAME, 1.0, "PASS", n=3, explanation="FAIL")]
    assert log({"/results/a": records})[FIGURE_CHECK_KEY] == {
        "avg": 1.0, "count": 1, "no_figures": 0, "unchecked": 0}


def test_another_custom_evaluator_is_averaged_like_a_built_in(log):
    records = _judges() + [_result("team_custom_eval", 0.5, "Half", n=40)]
    assert log({"/results/a": records}) == {**_JUDGE_SCORES,
                                            "team_custom_eval": {"avg": 0.5, "count": 1}}


def test_records_of_another_kind_are_ignored(log):
    other = {**_result("Builtin.Correctness", 0.1, "Wrong", n=50), "name": "something.else"}
    assert log({"/results/a": _judges() + [other]}) == _JUDGE_SCORES


def test_the_first_log_group_with_results_is_read(log):
    assert log({"/results/empty": [], "/results/a": _judges() + _figure_checks()}) == {
        **_JUDGE_SCORES, **_FIGURE_CHECK}


def test_no_results_anywhere_is_no_scores(log):
    assert log({"/results/a": [], "/results/b": []}) == {}


# --------------------------------------------------------------------------- #
# The two queries, as measured on the real results log.
# --------------------------------------------------------------------------- #
def _one_line(query: str) -> str:
    return " ".join(query.split())


def test_the_score_query_is_the_measured_one():
    assert _one_line(ar.EVAL_SCORE_QUERY) == _one_line(
        'fields @timestamp | filter name = "gen_ai.evaluation.result" '
        "| stats avg(`attributes.gen_ai.evaluation.score.value`) as avg_score, count() as count "
        "by `attributes.gen_ai.evaluation.name` as evaluator")


def test_the_label_query_is_the_measured_one():
    assert _one_line(ar.EVAL_LABEL_QUERY) == _one_line(
        'fields @timestamp | filter name = "gen_ai.evaluation.result" '
        "| stats count() as count by `attributes.gen_ai.evaluation.name` as evaluator, "
        "`attributes.gen_ai.evaluation.score.label` as label")


# --------------------------------------------------------------------------- #
# The pure parts.
# --------------------------------------------------------------------------- #
def _row(evaluator, avg_score, count):
    return [{"field": "evaluator", "value": evaluator},
            {"field": "avg_score", "value": avg_score},
            {"field": "count", "value": count}]


def _label_row(evaluator, label, count):
    return [{"field": "evaluator", "value": evaluator}, {"field": "label", "value": label},
            {"field": "count", "value": count}]


def test_figure_check_rows_keep_a_zero_pass_rate_under_their_own_key():
    rows = [_row(_FIGURE_CHECK_ID, "0.0", "4"), _row("Builtin.Correctness", "0.9", "4")]
    assert parse_eval_rows(rows) == {FIGURE_CHECK_KEY: {"avg": 0.0, "count": 4},
                                     "Correctness": {"avg": 0.9, "count": 4}}


def test_a_lookalike_name_is_not_the_figure_check():
    # Both rows keep their own names; neither is keyed as the figure check.
    rows = [_row(f"{EVALUATOR_NAME}er", "0.0", "2"), _row(f"x{EVALUATOR_NAME}-a", "0.5", "1")]
    assert parse_eval_rows(rows) == {f"{EVALUATOR_NAME}er": {"avg": 0.0, "count": 2},
                                     f"x{EVALUATOR_NAME}-a": {"avg": 0.5, "count": 1}}


def test_label_counts_are_the_figure_checks_only():
    rows = [_label_row(EVALUATOR_NAME, "PASS", "6"), _label_row(_FIGURE_CHECK_ID, "FAIL", "2.0"),
            _label_row(EVALUATOR_NAME, "NO_FIGURES", "5"),
            _label_row(EVALUATOR_NAME, "UNCHECKED", "1"),
            _label_row("Builtin.Correctness", "PASS", "9"),
            _label_row(f"{EVALUATOR_NAME}er", "PASS", "9")]
    assert figure_check_counts(rows) == {"PASS": 6, "FAIL": 2, "NO_FIGURES": 5, "UNCHECKED": 1}


def test_unknown_labels_and_unreadable_counts_are_skipped():
    rows = [_label_row(EVALUATOR_NAME, "MAYBE", "3"), _label_row(EVALUATOR_NAME, "PASS", "junk"),
            [{"field": "evaluator", "value": EVALUATOR_NAME}, {"field": "count", "value": "2"}]]
    assert figure_check_counts(rows) == {}


def test_label_counts_give_the_pass_rate_and_the_counts_beside_it():
    builtin = {"Correctness": {"avg": 0.9, "count": 4}}
    counts = {"PASS": 6, "FAIL": 2, "NO_FIGURES": 5, "UNCHECKED": 1}
    assert merge_figure_check(builtin, counts) == {**builtin, FIGURE_CHECK_KEY: {
        "avg": 0.75, "count": 8, "no_figures": 5, "unchecked": 1}}


def test_label_counts_replace_the_value_average():
    merged = merge_figure_check({FIGURE_CHECK_KEY: {"avg": 1.0, "count": 3}},
                                {"PASS": 2, "FAIL": 2})
    assert merged[FIGURE_CHECK_KEY] == {"avg": 0.5, "count": 4, "no_figures": 0, "unchecked": 0}


def test_no_label_rows_leave_the_scores_alone():
    scores = {"Correctness": {"avg": 0.9, "count": 4}}
    assert merge_figure_check(scores, {}) == scores


def test_every_label_the_figure_check_writes_is_counted():
    rows = [_label_row(EVALUATOR_NAME, label, "1") for label in LABELS]
    assert figure_check_counts(rows) == dict.fromkeys(LABELS, 1)
