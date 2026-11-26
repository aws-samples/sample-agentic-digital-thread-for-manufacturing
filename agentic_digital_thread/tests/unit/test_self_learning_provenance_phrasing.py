# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pins for the session-flavored provenance phrasing normalizer (2026-08-04
live diagnosis: 3 pilot-env rows carried 'from prior sessions' in machine-authored
reasons; answers quoting them parroted 'prior session' to fresh users and
tripped the smoke memory-reference gate).

Offline — no AWS, no network. The DynamoDB table is replaced by an in-memory
``FakeTable`` wired via ``monkeypatch.setattr(self_learning, "_get_table", ...)``
(same seam style as ``test_self_learning_lifecycle.py``).

Each pin names the mutation it kills:
  * machine-sourced reason normalized (each phrase variant) — kills REMOVING
    the ``_normalize_provenance_phrasing`` call from ``log_proposed_mapping``;
  * plurality preserved — kills a replacement that hardcodes singular/plural;
  * steward (source='user') reason byte-identical — kills OVER-APPLYING the
    normalizer to steward text (ADR-0054: steward text is never rewritten);
  * session-free text byte-identical — kills an overreaching rewrite;
  * word-boundary discipline ('sessional', 'session-store') — kills relaxing
    the regex to a bare substring.
"""
from __future__ import annotations

import pytest

import agents.self_learning as self_learning


# --------------------------------------------------------------------------- #
# In-memory table double (minimal subset of the lifecycle FakeTable — the
# normalizer pins only need put/query/scan).
# --------------------------------------------------------------------------- #
class FakeTable:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def __len__(self) -> int:
        return len(self._items)

    def all_items(self) -> list[dict]:
        return [dict(v) for v in self._items.values()]

    def by_id(self, mapping_id: str) -> dict:
        for v in self._items.values():
            if v.get("mapping_id") == mapping_id:
                return dict(v)
        raise KeyError(mapping_id)

    def put_item(self, Item: dict) -> None:
        self._items[(Item["mapping_id"], Item["discovered_at"])] = dict(Item)

    def query(self, **kwargs) -> dict:
        index = kwargs.get("IndexName")
        values = kwargs.get("ExpressionAttributeValues", {})
        items = list(self._items.values())
        if index == "subject-key-index":
            result = [i for i in items if i.get("subject_key") == values[":sk"]]
        elif index == "status-index":
            result = [i for i in items if i.get("status") == values[":status"]]
        else:
            result = [i for i in items if i.get("mapping_id") == values[":mid"]]
        reverse = not kwargs.get("ScanIndexForward", True)
        result.sort(key=lambda x: x.get("discovered_at", ""), reverse=reverse)
        return {"Items": [dict(i) for i in result]}

    def scan(self, **kwargs) -> dict:
        return {"Items": self.all_items()}


def _install(monkeypatch, table: FakeTable) -> None:
    monkeypatch.setattr(self_learning, "_get_table", lambda: table)
    monkeypatch.setattr(self_learning, "_neptune_write", lambda *a, **k: True)
    # Disable the ADR-0051 P5 staging buffer so a single call inserts.
    monkeypatch.setattr(self_learning, "PROPOSAL_STAGING_MIN_COREFERENCES", 1)


def _log(*, reason: str, source: str = "agent", to_value: str = "acct-7") -> str:
    return self_learning.log_proposed_mapping(
        "erp", "orders.customer_id", "c-100",
        "crm", "accounts.id", to_value,
        "customer", 0.9, "ctx",
        source=source, reason=reason,
    )


# --------------------------------------------------------------------------- #
# Pure-function pins
# --------------------------------------------------------------------------- #
NORM = self_learning._normalize_provenance_phrasing


@pytest.mark.parametrize("phrase", [
    "prior session", "previous session", "earlier session", "past session",
    "Prior Session", "PREVIOUS SESSION",  # case-insensitive
])
def test_each_singular_variant_rewrites(phrase):
    """Kills: dropping a variant from the pattern alternation."""
    assert NORM(f"known at site9 from {phrase}") == (
        "known at site9 from earlier learned mapping"
    )


@pytest.mark.parametrize("phrase", [
    "prior sessions", "previous sessions", "earlier sessions", "past sessions",
])
def test_plurality_preserved(phrase):
    """Kills: a replacement that hardcodes the singular form."""
    assert NORM(f"granulator model known at site9 from {phrase}") == (
        "granulator model known at site9 from earlier learned mappings"
    )


def test_surrounding_text_preserved_byte_for_byte():
    """Kills: a rewrite that touches anything outside the matched phrase."""
    text = "Seen in ERP (site=site9) via prior sessions; confidence high."
    assert NORM(text) == (
        "Seen in ERP (site=site9) via earlier learned mappings; confidence high."
    )


def test_session_free_text_is_byte_identical():
    """Kills: an overreaching rewrite of text with no session phrasing."""
    text = "matched ERP order_number to MES work_order_id by foreign key"
    assert NORM(text) == text
    assert NORM("") == ""


@pytest.mark.parametrize("text", [
    # \b after the captured s? — 'sessional' is not 'session(s)'.
    "derived from a prior sessional analysis",
    # (?!-) lookahead — hyphenated compounds never match.
    "read from the past session-store snapshot",
    "read from the past sessions-store snapshot",
    # 'session' without a qualifying adjective never matches.
    "the session id was s-42",
])
def test_word_boundary_discipline(text):
    """Kills: relaxing the word-boundary regex to a bare substring
    (the project's error-taxonomy rule: no bare substrings)."""
    assert NORM(text) == text


def test_multiple_occurrences_all_rewritten():
    """Kills: a non-global substitution (count=1)."""
    assert NORM("prior session, then a previous session") == (
        "earlier learned mapping, then a earlier learned mapping"
    )


# --------------------------------------------------------------------------- #
# Writer integration pins (through log_proposed_mapping)
# --------------------------------------------------------------------------- #
def test_machine_sourced_reason_is_normalized_at_write(monkeypatch):
    """Kills: removing the normalizer call from log_proposed_mapping."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    mid = _log(reason="granulator model known at site9 from prior sessions")
    assert mid is not None
    assert fake.by_id(mid)["reason"] == (
        "granulator model known at site9 from earlier learned mappings"
    )


def test_steward_sourced_reason_passes_through_untouched(monkeypatch):
    """Kills: over-applying the normalizer to steward (source='user') text —
    steward text is UNTRUSTED but never silently rewritten (ADR-0054)."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    steward_text = "I confirmed this in a prior session with the plant team"
    mid = _log(reason=steward_text, source="user")
    assert mid is not None
    assert fake.by_id(mid)["reason"] == steward_text  # byte-for-byte


def test_machine_reason_without_session_phrasing_stored_byte_identical(monkeypatch):
    """Kills: a normalizer that rewrites more than the session patterns."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    reason = "value_format_match on serial prefix; verified against MES this turn"
    mid = _log(reason=reason)
    assert mid is not None
    assert fake.by_id(mid)["reason"] == reason


def test_machine_analysis_field_is_session_free(monkeypatch):
    """Kills: routing the generated analysis narrative around the normalizer —
    the stored machine-sourced analysis must never carry session phrasing."""
    fake = FakeTable()
    _install(monkeypatch, fake)
    mid = _log(reason="foreign_key_match")
    assert mid is not None
    analysis = fake.by_id(mid)["analysis"]
    assert not self_learning._SESSION_PHRASE_RE.search(analysis)
    assert NORM(analysis) == analysis


# --------------------------------------------------------------------------- #
# Prompt guidance pin (both modes)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["agent", "orchestrator"])
def test_prompt_carries_proposal_reason_provenance_rule(mode):
    """Kills: dropping the anchored ``_PROPOSAL_REASON_RULE`` post-pass — both
    modes' rendered prompts must instruct that proposal reasons cite THIS
    turn's system/graph evidence, never sessions."""
    import agents.orchestrator as orch
    prompt = orch._build_orchestrator_prompt(mode)
    assert "never conversational" in prompt
    assert "not session memory" in prompt
    # The rule lands INSIDE the shared VALUE-MAPPING PROPOSALS RULES list.
    assert prompt.index("VALUE-MAPPING PROPOSALS") < prompt.index("not session memory")
