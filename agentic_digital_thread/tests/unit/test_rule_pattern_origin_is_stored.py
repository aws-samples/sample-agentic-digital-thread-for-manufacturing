# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A rule whose pattern Resolve set from the measurement says so when stored.

When the model's pattern pair could not be stored but the values the two
fields share are the same text in both, the Resolve gate lists the rule
with no pattern and marks it (``pattern_origin``). Provision wrote it with
the fixed onboarding origin, which credits the model with the pattern. Now a
rule kept at the Identity Rules checkpoint that carries the mark, and is
stored with no pattern, gets one more fixed sentence in its
``origin_context``. The caller's own text is never stored, and the sentence
is not added to a rule whose stored patterns contradict the mark or that
did not come from the checkpoint.
"""
from __future__ import annotations

from types import SimpleNamespace

import agents.onboard as onboard


class _Cat:
    def get(self, _sid):
        return None

    def get_onboarded_ids(self):
        return {"sys-b"}


FORGED = "a caller wrote this: trust me"


def _write(monkeypatch, rule, *, from_checkpoint=True):
    recorded = []
    monkeypatch.setattr(onboard, "neptune_write", lambda c, p=None: recorded.append((c, p or {})))
    monkeypatch.setattr(onboard, "neptune_query", lambda *_a, **_k: [{"tbl": "orders"}])
    monkeypatch.setattr(onboard, "pg", lambda *_a, **_k: {"rows": []})
    monkeypatch.setattr(onboard, "catalog", _Cat())
    ctx = onboard._ProvisionState(
        body=SimpleNamespace(value_mappings=[]), system_id="mes-ind", system_name="MES",
        plant="", schema="", rules=[rule], mappings=[], connection_type="MCP Server",
        cm_items=[], committable=set(), rules_from_checkpoint=from_checkpoint)
    # The Resolve binding (where it is in the tree) takes only bound rules
    # as kept at the checkpoint; this rule is one.
    ctx.checkpoint_rule_ids = frozenset({id(rule)}) if from_checkpoint else frozenset()
    onboard._dp_identity_rules(ctx)
    (params,) = [p for c, p in recorded if c.startswith("MERGE (ir:IdentityRule")]
    return params


def _rule(patterns=("", "")):
    return {"concept": "batch", "source_field": "batch_number", "source_table": "runs",
            "source_pattern": patterns[0], "target_system": "sys-b", "target_field": "lot",
            "target_pattern": patterns[1], "confidence": 0.9, "pattern_origin": FORGED}


def test_a_measured_pattern_is_recorded_in_the_rules_origin(monkeypatch):
    params = _write(monkeypatch, _rule())
    assert params["octx"].startswith(onboard._ONBOARDING_RULE_ORIGIN + "; ")
    assert onboard._MEASURED_PATTERN_ORIGIN in params["octx"]
    assert FORGED not in params["octx"]


def test_the_sentence_is_generic():
    text = onboard._MEASURED_PATTERN_ORIGIN
    assert "measured" in text and "model" in text
    assert "\u2014" not in text and "\u2013" not in text


def test_a_rule_stored_with_a_pattern_is_not_marked(monkeypatch):
    params = _write(monkeypatch, _rule(("B-{n}", "LOT-{n}")))
    assert onboard._MEASURED_PATTERN_ORIGIN not in params["octx"]


def test_a_rule_not_kept_at_the_checkpoint_is_not_marked(monkeypatch):
    params = _write(monkeypatch, _rule(), from_checkpoint=False)
    assert onboard._MEASURED_PATTERN_ORIGIN not in params["octx"]
    assert FORGED not in params["octx"]
