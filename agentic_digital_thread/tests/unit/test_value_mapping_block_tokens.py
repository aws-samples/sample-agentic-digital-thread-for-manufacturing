# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Identifiers with ``#`` or letters and digits outside ASCII reach the
IDENTIFIER VALUE MAPPINGS block by exact match only (ADR-0035 amendment
2026-10-18).

The block's question reader used to see only ASCII letters and digits, so a
stored mapping for ``ASSET#1`` or ``MÜ-1`` could never reach the seed even
though the block renders such values unchanged. Such a token now matches a
stored value exactly (case aside, composed and decomposed Unicode alike),
never by trailing segment; ASCII tokens keep their rules, and dates and
times stay out. The graph compares the stored text as written, and its
``toLower`` may lower ASCII letters only (Memgraph 3.12: ``toLower('MÜ-1')``
is ``'mÜ-1'``), so the read looks for every spelling such a graph could give.
Synthetic values only.
"""
from __future__ import annotations

import unicodedata

import pytest

import agents.identifier_translation as it
import agents.orchestrator as orch
from tests.unit.test_route_seed_value_mappings import Graph, _inventory, _vm

NFC_MU = unicodedata.normalize("NFC", "MÜ-1")
NFD_MU = unicodedata.normalize("NFD", "MÜ-1")


def _ids(question, rows):
    return [i["id"] for i in it.find_value_mappings(question, rows)]


# ---------------------------------------------------------------- the tokens

@pytest.mark.parametrize("question, token", [
    ("Is ASSET#1 down?", "ASSET#1"),
    ("Is (ASSET#1) down?", "ASSET#1"),
    ("Check ASSET#1.", "ASSET#1"),
    ('Was "ASSET#1", or not?', "ASSET#1"),
    ("Is #A12 open?", "A12"),                 # a leading # is punctuation
    ("Is A/B#2 open?", "A/B#2"),
    (f"Is {NFC_MU} down?", NFC_MU),
    (f"Is {NFD_MU} down?", NFC_MU),          # read in NFC
    (f"[{NFC_MU}]: down?", NFC_MU),
    ("Is 设备-1 down?", "设备-1"),
    ("Is 设备1 down?", "设备1"),
])
def test_a_hash_or_non_ascii_identifier_is_a_token(question, token):
    assert it.value_mapping_tokens(question) == [token]


@pytest.mark.parametrize("question, token", [("Is ASSET#1 down?", "asset#1"),
                                             (f"Is {NFC_MU} down?", NFC_MU.lower())])
def test_such_a_token_has_exact_terms_and_no_suffix(question, token):
    exact, suffixes = it.value_mapping_terms(question)
    assert exact[0] == token
    assert suffixes == []


def test_an_ascii_token_with_a_hash_has_one_term():
    assert it.value_mapping_terms("Is ASSET#1 down?") == (["asset#1"], [])


@pytest.mark.parametrize("question", [f"Is {NFC_MU} down?", f"Is {NFD_MU} down?",
                                      "Is mü-1 down?"])
def test_every_spelling_a_graph_could_give_a_non_ascii_token_is_read(question):
    """Composed and decomposed, and each lowered the Unicode way or the ASCII
    way (the latter of the upper case too, for a value stored in capitals)."""
    exact, _suffixes = it.value_mapping_terms(question)
    assert exact == ["mü-1", "mÜ-1", unicodedata.normalize("NFD", "mü-1")]


class AsciiLowerGraph(Graph):
    """The fake graph, but its ``toLower`` lowers ASCII letters only, as
    Memgraph 3.12 does."""

    def _vm_read(self, params):
        exact = params.get("exact") or params.get("linked") or []

        def lower(value):
            return "".join(c.lower() if c.isascii() else c for c in str(value))
        return [dict(vm) for vm in self.mappings
                if lower(vm["from_value"]) in exact or lower(vm["to_value"]) in exact]


@pytest.mark.parametrize("question, tokens", [
    ("Is ASSET#1-A2 down?", ["ASSET#1-A2"]),       # not 1-A2 on its own
    ("Is AB1#2 down?", ["AB1#2"]),                 # not AB1 on its own
    (f"Is {NFC_MU} or QX-101 down?", ["QX-101", NFC_MU]),
    ("Is PLANT/LINE.1 down?", ["PLANT/LINE.1"]),   # slash and dot stay ASCII separated
])
def test_an_ascii_token_inside_one_is_not_a_token_of_its_own(question, tokens):
    assert it.value_mapping_tokens(question) == tokens


@pytest.mark.parametrize("question", [
    "Show maintenance since 2026-10-18", "since 2026-10-18#1", "since Oct-18#2",
    "since 2026年10月18日", "at 10時30分", "at 10:30#2", "Follow-up on re-run#",
])
def test_dates_and_times_are_still_no_token(question):
    assert it.value_mapping_tokens(question) == []


# ---------------------------------------------------------------- the match

@pytest.mark.parametrize("value, token, match", [
    ("ASSET#1", "ASSET#1", it.MATCH_EXACT),
    ("asset#1", "ASSET#1", it.MATCH_EXACT),
    ("LINE-ASSET#1", "ASSET#1", None),            # exact only: no trailing-segment match
    (NFD_MU, NFC_MU, it.MATCH_EXACT),
    (NFC_MU, NFD_MU, it.MATCH_EXACT),
    ("mü-1", NFC_MU, it.MATCH_EXACT),
    (f"PLANT-{NFC_MU}", NFC_MU, None),
    ("设备-1", "设备-1", it.MATCH_EXACT),
    ("A-设备-1", "设备-1", None),
    ("X-PLANT/LINE.1", "PLANT/LINE.1", it.MATCH_SEGMENT),   # an ASCII token keeps its rule
])
def test_such_a_token_matches_a_stored_value_exactly_only(value, token, match):
    assert it.value_match(value, token) == match


@pytest.mark.parametrize("question, stored", [
    ("Is ASSET#1 down?", "ASSET#1"), (f"Is {NFC_MU} down?", NFD_MU),
    (f"Is {NFD_MU} down?", NFC_MU), ("Is 设备-1 down?", "设备-1")])
def test_a_stored_mapping_for_such_an_identifier_reaches_the_block(question, stored):
    rows = [_vm("vm-1", "sys-a", stored, "sys-b", "K-55"),
            _vm("vm-2", "sys-a", f"LINE-{stored}", "sys-b", "K-56")]
    assert [i["to_value"] for i in it.find_value_mappings(question, rows)] == ["K-55"]


@pytest.mark.parametrize("graph_kind", [Graph, AsciiLowerGraph])
@pytest.mark.parametrize("question, stored", [
    ("Is ASSET#1 down?", "ASSET#1"), (f"Is {NFC_MU} down?", NFD_MU),
    (f"Is {NFD_MU} down?", NFC_MU), ("Is mü-1 down?", NFC_MU), (f"Is {NFC_MU} down?", "mü-1")])
def test_the_seed_carries_it_through_the_graph_read(question, stored, graph_kind):
    graph = graph_kind([_vm("vm-1", "sys-a", stored, "sys-b", "K-55")],
                       inventory=_inventory(("sys-a", "sys-b", "sys-c")))
    seed = orch.build_route_seed(query=graph, question=question)
    assert [m["to_value"] for m in it.parse_value_mappings(seed)] == ["K-55"]
    (_cypher, params), = [c for c in graph.vm_reads() if "$exact" in c[0]]
    assert not any(k[:1] == "s" and k[1:].isdigit() for k in params)   # no suffix term


@pytest.mark.parametrize("graph_kind", [Graph, AsciiLowerGraph])
def test_a_non_ascii_value_one_link_further_is_read_on_either_graph(graph_kind):
    rows = [_vm("vm-1", "sys-a", "QX-101", "sys-b", "MÜ-9"),
            _vm("vm-2", "sys-b", "MÜ-9", "sys-c", "P-1")]
    graph = graph_kind(rows, inventory=_inventory(("sys-a", "sys-b", "sys-c")))
    seed = orch.build_route_seed(query=graph, question="Is QX-101 down?")
    assert [(m["from_value"], m["to_value"], m["match"]) for m in it.parse_value_mappings(seed)] \
        == [("QX-101", "MÜ-9", "exact"), ("MÜ-9", "P-1", "hop")]
    assert it.hop_values("Is QX-101 down?", rows) == ["mÜ-9", "mü-9", "qx-101"]


# ------------------------------------- every capital the match calls the same

SHARP_S, CAPITAL_SHARP_S = "\u00df", "\u1e9e"     # ß and ẞ: "ß".upper() is "SS", not "ẞ"
KELVIN = "\u212a"                                 # the Kelvin sign: its lower case is ASCII k
DZ = ("\u01c6", "\u01c5", "\u01c4")               # ǆ, ǅ, Ǆ: one lower case, three spellings


@pytest.mark.parametrize("graph_kind", [Graph, AsciiLowerGraph])
@pytest.mark.parametrize("question, stored", [
    (f"Is {SHARP_S}1 down?", f"{CAPITAL_SHARP_S}1"),
    (f"Is {CAPITAL_SHARP_S}1 down?", f"{SHARP_S}1"),
    ("Is \u00c4k1 down?", f"\u00c4{KELVIN}1"),
])
def test_a_stored_value_in_any_capital_the_match_accepts_is_read(question, stored, graph_kind):
    """A stored value whose lower case is the token's is an exact match, so the
    read finds it on either graph, even where the token's own upper case is
    another text (``ß`` upper-cases to ``SS``) or the capital is outside ASCII
    for an ASCII lower case (the Kelvin sign)."""
    (token,) = it.value_mapping_tokens(question)
    assert it.value_match(stored, token) == it.MATCH_EXACT
    graph = graph_kind([_vm("vm-1", "sys-a", stored, "sys-b", "K-55")],
                       inventory=_inventory(("sys-a", "sys-b", "sys-c")))
    seed = orch.build_route_seed(query=graph, question=question)
    assert [m["to_value"] for m in it.parse_value_mappings(seed)] == ["K-55"]


@pytest.mark.parametrize("question", [f"Is {SHARP_S}1 down?", f"Is {CAPITAL_SHARP_S}1 down?"])
def test_a_term_the_match_would_refuse_is_not_read(question):
    """``ß`` upper-cases to ``SS``, but ``SS1`` is no exact match for ``ß1``
    (:func:`value_match` compares lower cases), so ``ss1`` is no term."""
    (token,) = it.value_mapping_tokens(question)
    exact, _suffixes = it.value_mapping_terms(question)
    assert "ss1" not in exact
    assert {it.value_match(term, token) for term in exact} == {it.MATCH_EXACT}


@pytest.mark.parametrize("graph_kind", [Graph, AsciiLowerGraph])
def test_rows_the_match_refuses_do_not_crowd_a_real_one_out_of_the_read(graph_kind):
    """One stored ``ẞ1`` row and enough ``SS1`` rows to fill the read's cap:
    the ``SS1`` rows are not read, so the cap is not reached and the ``ẞ1``
    mapping still reaches the block."""
    cap = orch._ROUTE_SEED_VALUE_MAPPING_READ_CAP
    rows = [_vm("vm-0", "sys-a", f"{CAPITAL_SHARP_S}1", "sys-b", "K-55")]
    rows += [_vm(f"vm-{i}", "sys-a", "SS1", "sys-c", f"K-{100 + i}") for i in range(1, cap)]
    graph = graph_kind(rows, inventory=_inventory(("sys-a", "sys-b", "sys-c")))
    seed = orch.build_route_seed(query=graph, question=f"Is {SHARP_S}1 down?")
    assert [m["to_value"] for m in it.parse_value_mappings(seed)] == ["K-55"]


def test_each_capital_of_each_letter_is_a_term():
    exact, _suffixes = it.value_mapping_terms(f"Is {DZ[0]}{DZ[0]}1 down?")
    assert {a + b + "1" for a in DZ for b in DZ} <= set(exact)


def test_a_token_with_more_spellings_than_the_cap_keeps_its_own_terms_and_says_so(caplog):
    token = DZ[0] * 4 + "1"
    assert len(DZ) ** 4 > it.VALUE_MAPPING_SPELLING_CAP
    with caplog.at_level("INFO", logger=it.__name__):
        exact, suffixes = it.value_mapping_terms(f"Is {token} down?")
    assert (exact, suffixes) == ([token, DZ[2] * 4 + "1"], [])
    assert [r.getMessage() for r in caplog.records if "VALUE_MAPPING_SPELLING_CAP" in r.getMessage()]


@pytest.mark.parametrize("graph_kind", [Graph, AsciiLowerGraph])
def test_a_value_one_link_further_in_another_capital_is_read(graph_kind):
    rows = [_vm("vm-1", "sys-a", "QX-101", "sys-b", f"{SHARP_S}9"),
            _vm("vm-2", "sys-b", f"{CAPITAL_SHARP_S}9", "sys-c", "P-1")]
    graph = graph_kind(rows, inventory=_inventory(("sys-a", "sys-b", "sys-c")))
    seed = orch.build_route_seed(query=graph, question="Is QX-101 down?")
    assert [(m["to_value"], m["match"]) for m in it.parse_value_mappings(seed)] \
        == [(f"{SHARP_S}9", "exact"), ("P-1", "hop")]


SMALL_SIGMA, CAPITAL_SIGMA = "\u03c3", "\u03a3"   # σ and Σ: "ΑΣ1".lower() is "ας1" (final sigma)


def test_a_hop_term_the_hop_would_refuse_is_not_read():
    """``Σ`` lowers to ``σ`` letter by letter but to ``ς`` at the end of a
    word, so ``ΑΣ1`` is no hop match for ``ασ1`` and is no hop term."""
    value = f"\u03b1{SMALL_SIGMA}1"
    rows = [_vm("vm-1", "sys-a", "QX-101", "sys-b", value)]
    terms = it.hop_values("Is QX-101 down?", rows)
    assert f"\u0391{CAPITAL_SIGMA}1" not in terms
    assert {t.lower() for t in terms if not t.isascii()} == {value}


@pytest.mark.parametrize("graph_kind", [Graph, AsciiLowerGraph])
def test_hop_rows_the_hop_refuses_do_not_crowd_a_real_one_out_of_the_read(graph_kind):
    """A direct row to ``ασ1``, the real hop from it, and enough rows holding
    ``ΑΣ1`` to fill the read's cap: they are not read, so the hop survives."""
    cap = orch._ROUTE_SEED_VALUE_MAPPING_READ_CAP
    value = f"\u03b1{SMALL_SIGMA}1"
    rows = [_vm("vm-1", "sys-a", "QX-101", "sys-b", value),
            _vm("vm-2", "sys-b", value, "sys-c", "P-1")]
    rows += [_vm(f"vm-{i}", "sys-b", f"\u0391{CAPITAL_SIGMA}1", "sys-c", f"P-{100 + i}")
             for i in range(3, cap + 2)]
    graph = graph_kind(rows, inventory=_inventory(("sys-a", "sys-b", "sys-c")))
    seed = orch.build_route_seed(query=graph, question="Is QX-101 down?")
    assert [(m["to_value"], m["match"]) for m in it.parse_value_mappings(seed)] \
        == [(value, "exact"), ("P-1", "hop")]
