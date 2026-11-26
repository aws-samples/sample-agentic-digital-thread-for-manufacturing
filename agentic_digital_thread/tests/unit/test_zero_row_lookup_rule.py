# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A lookup that returns no rows for an identifier is reported as a miss.

Measured on a rehearsal account (2026-10-15, a Quick lot question): the ERP
system agent was sent a batch identifier in the form its shipment field holds.
It looked the same identifier up in its lot-consumption field, which holds a
different form, and got no rows; two more consumption lookups with that form
also came back empty. It then answered anyway: it read the product's bill of
materials and the lots received that week, named the seed lots as the ones
the batch drew, and priced the batch from a list price. Its reply carried no
miss, so the orchestrator's rule that no rows after sending another field's
form is a wrong-form miss had nothing to react to, and the one follow-up that
system gets went on the guessed lots.

The existing IDENTIFIER-MISS protocol covers an identifier that matches
NOTHING in the data; this one matched rows in another field, so the agent did
not treat it as a miss. ZERO-ROW LOOKUPS makes each empty lookup reported for
that identifier, in words the orchestrator already acts on, and forbids
filling the gap. It reuses the IDENTIFIER-MISS block (no new marker) and
leaves that protocol's honest-zero case as it is. Since 2026-10-19 it also
says when the empty lookup is the answer (no record there yet: the field holds
that form) and that the request's other asks are still answered
(test_no_record_under_known_link.py).

Fully offline (no Bedrock, no AWS, no DB, no sockets). Pins:

1. The rule is in the policy tail of EVERY connection type, once, between
   EFFICIENCY and ROW FIGURES, in both integrity modes, and a custom
   registration prompt cannot displace it.
2. The key clauses, and nothing that asks the agent to broaden a search.
3. It agrees with IDENTIFIER-MISS (block only for the wrong-vocabulary case,
   which still says never to emit it for a genuine empty result) and the
   orchestrator side still reads such a reply as a miss: the Mode-A prompts
   keep the no-rows wrong-form clause, and the engine's empty-result
   classifier matches the rule's own wording.
4. Bounded size; no story identifier, no digits, no identifier-shaped token.
"""
import re

import pytest

import agents.orchestrator as orch
import agents.system_agent as sa
from agents.demo import story_lookup
from agents.query_intelligence import failure_marker
from agents.workflow_guidance import compose_quick_system_prompt

_ALL_TYPES = ("PostgreSQL", "Athena", "SiteWise (Direct)", "SiteWise (i3X)", "MCP Server",
              "REST API", "")
_HEADER = "ZERO-ROW LOOKUPS (required):"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _rule() -> str:
    return sa._zero_row_lookup_prompt()


# --------------------------------------------------------------------------- #
# 1. Every connection type, placed after EFFICIENCY
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_every_connection_type_carries_the_rule_once(connection_type, monkeypatch):
    monkeypatch.delenv("RESPONSE_INTEGRITY_MODE", raising=False)
    assert sa._policy_tail(connection_type).count(_HEADER) == 1, connection_type
    report_only = sa._compose_system_prompt("erp", "ERP", "public", "", "", connection_type)
    monkeypatch.setenv("RESPONSE_INTEGRITY_MODE", "enforce")
    enforce = sa._compose_system_prompt("erp", "ERP", "public", "", "", connection_type)
    assert report_only.count(_HEADER) == 1, connection_type
    assert enforce.count(_HEADER) == 1, connection_type
    assert enforce.startswith(report_only)


@pytest.mark.parametrize("connection_type", _ALL_TYPES)
def test_rule_sits_between_efficiency_and_row_figures(connection_type):
    tail = sa._policy_tail(connection_type)
    efficiency = sa._efficiency_prompt(connection_type)
    assert tail.index(efficiency) + len(efficiency) == tail.index(_rule())
    assert tail.index(_rule()) + len(_rule()) == tail.index(sa._row_figures_prompt(connection_type))


def test_custom_registration_prompt_cannot_displace_the_rule():
    custom = "Custom registration prompt. When nothing matches, answer from similar records."
    prompt = sa._compose_system_prompt("erp", "ERP", "public", custom, "", "PostgreSQL")
    assert prompt.startswith(custom)
    assert prompt.index(_HEADER) > len(custom)


# --------------------------------------------------------------------------- #
# 2. What it says
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fragment", [
    "when a lookup for an identifier you were sent returns no rows",
    "report that lookup as empty for that identifier",
    "even when the same identifier matches rows in another table or field",
    "Say which identifier you sent, in which form, which table or field you looked in, "
    "and that it returned no rows",
    "Then stop that part of the answer and every part that depends on it",
    "never fill the gap from other records, a bill of materials, seed, template or default "
    "rows, or a guess",
    "never rewrite the identifier into another form yourself",
])
def test_rule_carries_the_key_clauses(fragment):
    assert fragment in _flat(_rule()), fragment


def test_rule_never_asks_for_a_wider_search():
    low = _rule().lower()
    for word in ("broaden", "widen", "try another", "look elsewhere", "similar", "closest",
                 "nearest", "instead use", "fall back"):
        assert word not in low, word


def test_rule_puts_the_whole_value_first():
    # When the question asks for one sent identifier, its whole value comes
    # before any partial search. Rows a partial filter returns share text with
    # it, which does not show they belong to it (it may be among them).
    flat = _flat(_rule())
    assert flat.startswith("ZERO-ROW LOOKUPS (required): when asked for one identifier you "
                           "were sent, look it up by its whole value before any partial "
                           "search;")
    assert ("rows a partial filter returns share text with it but are not proof they belong "
            "to it, so label them partial") in flat
    assert "never its own" not in flat


# --------------------------------------------------------------------------- #
# 3. Agrees with IDENTIFIER-MISS, and the orchestrator reads it as a miss
# --------------------------------------------------------------------------- #
def test_rule_reuses_the_identifier_miss_block_for_its_own_case_only():
    flat = _flat(_rule())
    assert "use the IDENTIFIER-MISS block only when its wrong-vocabulary case applies" in flat
    assert flat.count("IDENTIFIER-MISS") == 1
    # The honest-zero case of the existing protocol is unchanged.
    miss = _flat(sa._identifier_miss_prompt())
    assert "WRONG VOCABULARY" in miss
    assert "NEVER emit IDENTIFIER-MISS for a genuinely empty result" in miss


@pytest.mark.parametrize("name,prompt", [
    ("full-agent", orch._build_orchestrator_prompt("agent")),
    ("quick-agent", compose_quick_system_prompt("agent")),
])
def test_mode_a_orchestrator_reads_no_rows_as_a_miss(name, prompt):
    flat = _flat(prompt)
    assert ("No rows after sending another field's form is a wrong-form miss even without "
            "an IDENTIFIER-MISS block") in flat, name


def test_the_engine_classifies_a_reply_in_the_rules_words_as_empty():
    # The empty-join failure signal reads a tool result's head; a reply that
    # leads with the miss in the rule's own words must classify as empty.
    reply = ("The lookup of the batch identifier in the consumption table's batch field "
             "returned no rows.")
    assert failure_marker(reply) == ("empty", "returned no rows")


# --------------------------------------------------------------------------- #
# 4. Size and neutrality
# --------------------------------------------------------------------------- #
def test_rule_fits_its_budget():
    # Paid on every system-agent call, for every connection type. Raised
    # 650 -> 900 on 2026-10-19 for the no-record-yet case (a field that
    # holds the identifier's form) and the request's other asks
    # (test_no_record_under_known_link.py). Raised 900 -> 1100 for the
    # whole-value-first clause (test_rule_puts_the_whole_value_first).
    assert 0 < len(_rule()) <= 1100, len(_rule())


def test_rule_names_no_story_identifier():
    for token in (story_lookup.STORY_EQUIPMENT_ID, story_lookup.STORY_MES_LINE,
                  story_lookup.STORY_CMMS_ASSET_NUM, story_lookup.DEFECT_CHECK_TYPE,
                  story_lookup.BEARING_FAILURE_CODE):
        assert token.lower() not in _rule().lower(), token


def test_rule_has_no_identifier_shaped_literal():
    text = _rule()
    assert not re.search(r"\d", text)
    assert not re.search(r"\b\w+_\w+\b", text)
    assert not re.search(r"\b[A-Z]{2,}-\d", text)
    assert "$" not in text
