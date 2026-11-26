# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Answer honesty — PROACTIVE metric-term resolution.

Fully offline (no AWS, no sockets, no graph). Pins the deterministic
prompt-composition pre-check that closes the measured hole in the REACTIVE
``METRIC-TERM MISS`` rule: asked for an undefined three-letter rate, the model
invented a definition and computed an unrelated ratio, so no lookup ever
"missed" and the never-substitute rule could not fire. Three live runs produced
three different numbers.

What is pinned here:

1. EXTRACTION is shape-based and domain-literal-free — letters-only all-caps
   acronyms and Title-Cased phrases, gated to a metric-request context.
2. RESOLUTION never flags a term the deployment knows (metric registry, concept
   vocabulary, or this turn's routing seed) and never flags engine/tech
   acronyms.
3. The NOTICE names the exact unresolved term(s) and carries the
   never-substitute requirement verbatim.
4. FAIL-OPEN: any lookup failure ⇒ no notice; nothing unresolved ⇒ the composed
   turn is BYTE-IDENTICAL to the pre-change composition.
5. FALSE-POSITIVE HARDENING. The gates are now:
   the "what is/are the <term>" shape requires the COPULA (a bare "What <term>
   …" is a question about equipment, not a metric request) and is word-boundary
   anchored; a candidate immediately followed by a plant/data noun head is
   adjectival; a phrase candidate loses any sentence-initial auxiliary/imperative
   verb; and resolution tries progressive LEADING-token drops as well as trailing
   measure words, so a term the deployment DOES define can never be flagged.
   ``PM`` is deliberately NOT stoplisted (a real maintenance metric family);
   ``YTD``/``MTD`` remain (period qualifiers, not quantities).

Fixture terms are synthetic placeholders (the neutrality ratchet scans tests).
"""
from __future__ import annotations

import pytest

import agents.metric_definition_store as mds
import agents.metric_term_resolution as mtr
import agents.orchestrator as orch
import agents.runtime_app as runtime_app
from agents.memory_labels import strip_turn_preambles

# Synthetic, deliberately meaningless metric-term shapes — no deployment
# defines these, so they must always come back unresolved.
_UNKNOWN_ACRONYM = "QRS"
_OTHER_UNKNOWN_ACRONYM = "ZTX"
_THIRD_UNKNOWN_ACRONYM = "WVU"
_FOURTH_UNKNOWN_ACRONYM = "JKM"


# --------------------------------------------------------------------------- #
# 0. Graph-definition store default — successfully EMPTY, isolated per test
# --------------------------------------------------------------------------- #
#: One all-null row is the store's OPTIONAL MATCH sentinel for a SUCCESSFUL
#: read of a deployment that defines no graph metrics ([] is the agents.db
#: failure signal and would make a cold load raise — ADR-0066 D4).
_EMPTY_STORE_SENTINEL = ({"id": None, "definition_json": None},)


@pytest.fixture(autouse=True)
def _empty_graph_definition_store(monkeypatch):
    """Default every test to a successfully-empty TTL snapshot, isolated.

    ``known_metric_vocabulary`` now also reads the graph-resident metric
    definitions through ``metric_definition_store.load_definitions()``. This
    offline lane has no graph, and the store deliberately RAISES on a cold load
    failure (a partial vocabulary would fabricate "undefined" claims), so
    without this fixture every pre-existing pin would exercise the fail-open
    arm instead of the vocabulary it was written against. A successfully-empty
    snapshot IS that vocabulary — byte-identical behavior to the pre-graph
    module. Tests that want definitions (or a failing store) re-patch the seam
    and re-invalidate; the surrounding ``invalidate()`` calls keep the
    module-level snapshot from leaking across tests or files."""
    mds.invalidate()
    monkeypatch.setattr(
        mds, "neptune_query", lambda *_a, **_k: [dict(r) for r in _EMPTY_STORE_SENTINEL])
    yield
    mds.invalidate()


def _graph_doc(*, metric_id: str = "line-utilization",
               label: str = "Line Utilization",
               synonyms: tuple = ("LU",)) -> dict:
    """A minimal VALID C1 direct definition (synthetic placeholder terms)."""
    return {
        "id": metric_id,
        "kind": "direct",
        "label": label,
        "synonyms": list(synonyms),
        "unit": "ratio",
        "concept": "utilization",
        "aggregation": "avg",
    }


def _install_graph_definitions(monkeypatch, docs: list) -> list:
    """Serve ``docs`` from the store seam via the REAL snapshot machinery.

    Rows carry the real ``canonical_json`` bytes, so the store's parse +
    canonical-byte check runs for real. Returns the seam call list so tests
    can pin the zero-per-turn-round-trip invariant."""
    from agents.metric_definition import canonical_json
    rows = [{"id": doc["id"], "definition_json": canonical_json(doc)}
            for doc in docs]
    calls: list[str] = []

    def _query(cypher, parameters=None):
        calls.append(cypher)
        return [dict(row) for row in rows]

    mds.invalidate()
    monkeypatch.setattr(mds, "neptune_query", _query)
    return calls


# --------------------------------------------------------------------------- #
# 1. Extraction — shape + metric-request gating
# --------------------------------------------------------------------------- #
def test_unknown_acronym_adjacent_to_measure_word_is_a_candidate():
    """The measured failure shape: '<ACRONYM> rate' in a question."""
    assert mtr.extract_metric_term_candidates(
        f"What is the {_UNKNOWN_ACRONYM} rate for site A for 8/20?"
    ) == [_UNKNOWN_ACRONYM]


@pytest.mark.parametrize("question", [
    "What is the {t} ratio?",
    "Show the {t} percentage by line.",
    "{t} score for last week?",
    "Give me the {t} attainment for August.",
    "How is our {t} efficiency trending?",
    "Which metric drives {t} percent?",
])
def test_every_measure_word_gate_admits_the_candidate(question):
    """Each generic measurement word makes the turn a metric request."""
    text = question.format(t=_UNKNOWN_ACRONYM)
    assert mtr.extract_metric_term_candidates(text) == [_UNKNOWN_ACRONYM]


def test_what_is_the_x_shape_admits_without_a_measure_word():
    """'What is the X' is itself a metric request (the spec's second gate)."""
    assert mtr.extract_metric_term_candidates(
        f"What is the {_UNKNOWN_ACRONYM} for last month?"
    ) == [_UNKNOWN_ACRONYM]


def test_non_metric_context_is_never_flagged():
    """An acronym that is merely mentioned is NOT a metric request."""
    for question in (
        f"Does the {_UNKNOWN_ACRONYM} system have a maintenance window?",
        f"Email the {_UNKNOWN_ACRONYM} team about downtime.",
        f"List work orders where the note mentions {_UNKNOWN_ACRONYM}.",
    ):
        assert mtr.extract_metric_term_candidates(question) == [], question


def test_identifier_shaped_tokens_are_never_candidates():
    """Digits ⇒ identifier shape ⇒ the ADR-0048 identifier-miss loop's job.

    A metric acronym is letters only; a site/line/lot token carries digits and
    MAY legitimately be substituted with disclosure, so it must never reach the
    never-substitute notice."""
    for question in ("What is the rate for AB12 line?",
                     "What is the L3X9 ratio?",
                     "What is the rate for X1?"):
        assert mtr.extract_metric_term_candidates(question) == [], question


def test_single_letter_and_overlong_shapes_are_not_candidates():
    """Below 2 / above 6 letters is not acronym shape."""
    assert mtr.extract_metric_term_candidates("What is the X rate?") == []
    assert mtr.extract_metric_term_candidates("What is the ABCDEFGH rate?") == []


def test_shouted_question_skips_the_acronym_pass():
    """An ALL-CAPS question cannot be shape-parsed — stay silent (fail-open)."""
    assert mtr.extract_metric_term_candidates(
        f"WHAT IS THE {_UNKNOWN_ACRONYM} RATE FOR THE PLANT TODAY"
    ) == []


def test_candidates_are_bounded_and_first_appearance_ordered():
    """At most MAX_CANDIDATES per turn, in the order they appear."""
    question = (f"What is the {_UNKNOWN_ACRONYM} rate, the "
                f"{_OTHER_UNKNOWN_ACRONYM} rate, the {_THIRD_UNKNOWN_ACRONYM} "
                f"rate and the {_FOURTH_UNKNOWN_ACRONYM} rate?")
    candidates = mtr.extract_metric_term_candidates(question)
    assert len(candidates) == mtr.MAX_CANDIDATES == 3
    assert candidates == [_UNKNOWN_ACRONYM, _OTHER_UNKNOWN_ACRONYM,
                          _THIRD_UNKNOWN_ACRONYM]


def test_repeated_term_is_reported_once():
    question = (f"What is the {_UNKNOWN_ACRONYM} rate and the "
                f"{_UNKNOWN_ACRONYM} percentage?")
    assert mtr.extract_metric_term_candidates(question) == [_UNKNOWN_ACRONYM]


def test_capitalised_multiword_phrase_is_reported_as_asked():
    """A Title-Cased phrase is admitted on its measure-word-stripped form and
    REPORTED as the question wrote it."""
    assert mtr.extract_metric_term_candidates(
        "What is the Sigma Delta Rate for last week?"
    ) == ["Sigma Delta Rate"]
    # …and the phrase is the metric request even without an adjacent measure
    # word, because the measure word sits INSIDE the span.
    assert mtr.extract_metric_term_candidates(
        "Show the Sigma Delta Rate by line."
    ) == ["Sigma Delta Rate"]


def test_phrase_resolution_also_tries_the_stripped_form():
    """A defined metric LABEL may itself end in a measurement word, so both the
    asked phrase and its stripped form are resolution forms."""
    metric = next(m for m in _metric_layer().list_metrics()
                  if len(m.label.split()) >= 2
                  and m.label.split()[-1].lower() in mtr.METRIC_CONTEXT_WORDS
                  and m.label.replace(" ", "").isalpha())
    stripped = mtr._strip_trailing_context_words(metric.label)
    assert stripped and stripped != metric.label
    assert mtr.unresolved_metric_terms(
        f"What is the {metric.label} for line 3?", "seed") == []


def test_single_capitalised_word_is_too_weak_to_flag():
    """One Title-Cased word left after the strip is not a phrase candidate."""
    assert mtr.extract_metric_term_candidates(
        "What is the Utilization Rate for last week?"
    ) == []


# --------------------------------------------------------------------------- #
# 2. Resolution — the stoplist, and anything the deployment knows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("acronym", [
    "SQL", "API", "URL", "JSON", "CSV", "UTC", "ID", "AWS", "ERP", "MES",
    "CMMS", "PLM", "MQTT", "OPC", "SCADA", "TTL", "YTD", "MTD",
])
def test_stoplisted_engine_acronyms_are_never_flagged(acronym):
    """Engine/tech/time vocabulary is not a metric term — in extraction AND in
    the end-to-end notice."""
    question = f"What is the {acronym} rate for last week?"
    assert mtr.extract_metric_term_candidates(question) == []
    assert mtr.metric_term_gap_notice(question, "ROUTE SEED payload") == ""


def test_stoplist_carries_no_metric_or_site_literal():
    """The stoplist must stay engine-neutral: it names only technology, system
    CATEGORIES and time vocabulary. A DOMAIN metric term belongs to the
    vocabulary (where it resolves) — never to a hardcoded exemption."""
    registry_ids = {mtr.normalize_term(m.id).upper()
                    for m in _metric_layer().list_metrics()}
    overlap = {a for a in mtr.TECH_ACRONYM_STOPLIST if a in registry_ids}
    assert not overlap, (
        f"stoplisted terms that the metric registry DEFINES: {sorted(overlap)} "
        f"— a defined term must resolve through the vocabulary, not a stoplist")


def _metric_layer():
    from agents.metric_layer import get_metric_layer
    return get_metric_layer()


def test_registry_defined_metric_id_resolves_and_emits_nothing():
    """A term the metric-definition registry defines by ID never flags."""
    metric = next(m for m in _metric_layer().list_metrics()
                  if len(m.id) <= 6 and m.id.isalpha())
    question = f"What is the {metric.id.upper()} rate for line 3?"
    assert mtr.extract_metric_term_candidates(question) == [metric.id.upper()]
    assert mtr.unresolved_metric_terms(question, "ROUTE SEED payload") == []
    assert mtr.metric_term_gap_notice(question, "ROUTE SEED payload") == ""


def test_registry_label_resolves_a_spelled_out_phrase():
    """A Title-Cased phrase equal to a metric LABEL resolves."""
    metric = next(m for m in _metric_layer().list_metrics()
                  if len(m.label.split()) == 3 and m.label.replace(" ", "").isalpha())
    question = f"What is the {metric.label} for line 3?"
    assert mtr.unresolved_metric_terms(question, "seed") == []


def test_label_initialism_resolves_an_acronym():
    """An acronym resolves against the initials of a spelled-out label, so a
    deployment gets acronym coverage with zero configuration."""
    vocabulary = mtr.known_metric_vocabulary()
    assert "oee" in vocabulary  # id form
    metric = next(m for m in _metric_layer().list_metrics()
                  if len(m.label.split()) == 3 and m.label.replace(" ", "").isalpha())
    initials = "".join(w[0] for w in metric.label.split()).lower()
    assert initials in vocabulary
    assert mtr.unresolved_metric_terms(
        f"What is the {initials.upper()} rate?", "seed") == []


def test_concept_vocabulary_resolves_a_curated_concept_phrase():
    """The curated concept vocabulary (ids/names/ISA-95 synonyms) resolves."""
    from agents.catalog import CONCEPT_NAME
    vocabulary = mtr.known_metric_vocabulary()
    assert mtr.normalize_term("work-order") in vocabulary
    assert any(mtr.normalize_term(name) in vocabulary
               for name in CONCEPT_NAME.values())


def test_term_present_in_this_turns_route_seed_resolves():
    """The graph-derived seed the prelude ALREADY built is a resolution source —
    that is how live graph vocabulary counts with zero extra queries."""
    question = f"What is the {_UNKNOWN_ACRONYM} rate for line 3?"
    seed = ('[ROUTE SEED] {"systems": [{"id": "sys-a", "shared_concepts": '
            f'[{{"concept": "{_UNKNOWN_ACRONYM.lower()}"}}]}}]}}')
    assert mtr.unresolved_metric_terms(question, seed) == []
    assert mtr.metric_term_gap_notice(question, seed) == ""
    # …and with the term absent from the seed it IS unresolved.
    assert mtr.unresolved_metric_terms(
        question, '[ROUTE SEED] {"systems": [{"id": "sys-a"}]}'
    ) == [_UNKNOWN_ACRONYM]


def test_seed_match_is_word_boundary_not_substring():
    """A term that is merely a substring of a seed token does not resolve."""
    question = f"What is the {_UNKNOWN_ACRONYM} rate?"
    seed = f'[ROUTE SEED] {{"col": "{_UNKNOWN_ACRONYM.lower()}xyz9"}}'
    assert mtr.unresolved_metric_terms(question, seed) == [_UNKNOWN_ACRONYM]


def test_normalize_term_is_separator_insensitive():
    assert mtr.normalize_term("Spare-Parts_On  Hand") == "spare parts on hand"
    assert mtr.normalize_term("") == ""


# --------------------------------------------------------------------------- #
# 3. The notice text
# --------------------------------------------------------------------------- #
def test_notice_names_the_exact_unresolved_term_and_pins_never_substitute():
    """The contract sentences, verbatim: undefined in THIS deployment, a
    substitute is a WRONG ANSWER not an approximation, and the required
    response is to name the term and ask which definition is meant."""
    notice = mtr.metric_term_gap_notice(
        f"What is the {_UNKNOWN_ACRONYM} rate for site A for 8/20?",
        '[ROUTE SEED] {"systems": [{"id": "sys-a"}]}')
    assert notice.startswith(mtr.NOTICE_HEADER)
    assert f"'{_UNKNOWN_ACRONYM}'" in notice
    assert "UNDEFINED IN THIS DEPLOYMENT" in notice
    assert mtr.NEVER_SUBSTITUTE_SENTENCE in notice
    assert "WRONG ANSWER, not an approximation" in notice
    assert "name the unresolved term(s) explicitly" in notice
    assert "ask which definition is meant" in notice
    # the "authoritative source is not connected" branch, and the escape hatch
    assert "authoritative source for this term is not connected" in notice
    assert "disregard this notice for that term" in notice


def test_notice_lists_every_unresolved_term_it_reports():
    notice = mtr.metric_term_gap_notice(
        f"What is the {_UNKNOWN_ACRONYM} rate and the "
        f"{_OTHER_UNKNOWN_ACRONYM} ratio?", "seed")
    assert f"'{_UNKNOWN_ACRONYM}'" in notice
    assert f"'{_OTHER_UNKNOWN_ACRONYM}'" in notice


def test_render_is_pure_and_empty_for_no_terms():
    assert mtr.render_metric_term_gap_notice([]) == ""
    assert mtr.render_metric_term_gap_notice(["  "]) == ""
    assert mtr.render_metric_term_gap_notice(None) == ""


def test_render_bounds_term_count_and_length():
    long_term = "A" * (mtr.MAX_TERM_CHARS + 40)
    notice = mtr.render_metric_term_gap_notice(
        [long_term, "BB", "CC", "DD", "EE"])
    assert long_term not in notice
    assert "A" * mtr.MAX_TERM_CHARS in notice
    assert "'EE'" not in notice  # capped at MAX_CANDIDATES


# --------------------------------------------------------------------------- #
# 4. Fail-open
# --------------------------------------------------------------------------- #
def test_vocabulary_lookup_failure_emits_nothing(monkeypatch):
    """A broken vocabulary source can never fabricate an 'undefined' claim."""
    def _boom():
        raise RuntimeError("vocabulary source unavailable")

    monkeypatch.setattr(mtr, "known_metric_vocabulary", _boom)
    question = f"What is the {_UNKNOWN_ACRONYM} rate for line 3?"
    assert mtr.metric_term_gap_notice(question, "seed") == ""
    with pytest.raises(RuntimeError):
        mtr.unresolved_metric_terms(question, "seed")  # raises INSIDE, wrapper opens


def test_extraction_failure_emits_nothing(monkeypatch):
    monkeypatch.setattr(mtr, "extract_metric_term_candidates",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("x")))
    assert mtr.metric_term_gap_notice("What is the rate?", "seed") == ""


def test_empty_and_none_questions_emit_nothing():
    assert mtr.metric_term_gap_notice("", "seed") == ""
    assert mtr.metric_term_gap_notice(None, None) == ""
    assert mtr.extract_metric_term_candidates(None) == []


def test_runtime_wrapper_swallows_a_module_level_failure(monkeypatch):
    """``runtime_app._metric_term_gap_notice`` is the second fail-open layer."""
    monkeypatch.setattr(mtr, "metric_term_gap_notice",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError()))
    assert runtime_app._metric_term_gap_notice("What is the rate?", "seed") == ""


# --------------------------------------------------------------------------- #
# 5. Prelude integration — injection point + byte-identical ordinary turn
# --------------------------------------------------------------------------- #
def _install_route_seed(monkeypatch, seed: str) -> None:
    monkeypatch.delenv("SYSTEM_QUERY_MODE", raising=False)  # default = Mode A
    monkeypatch.setattr(orch, "build_route_seed", lambda candidate=None, question=None: seed)


def test_prelude_composition_is_byte_identical_when_nothing_unresolved(monkeypatch):
    """An ordinary turn must be indistinguishable from the pre-change build."""
    _install_route_seed(monkeypatch, "ROUTE-SEED")
    prompt = "How many open work orders are there?"
    augmented, _events, _planned = runtime_app._graph_first_prelude(prompt)
    assert augmented == f"ROUTE-SEED\n\n---\n\n{prompt}"


def test_prelude_injects_the_notice_between_seed_and_separator(monkeypatch):
    """The notice rides with the seed, BEFORE the documented separator, so the
    question the user typed is still recoverable byte-for-byte."""
    _install_route_seed(monkeypatch, "[ROUTE SEED] {\"systems\": []}")
    prompt = f"What is the {_UNKNOWN_ACRONYM} rate for site A for 8/20?"
    augmented, events, _planned = runtime_app._graph_first_prelude(prompt)
    assert mtr.NOTICE_HEADER in augmented
    assert _UNKNOWN_ACRONYM in augmented
    assert mtr.NEVER_SUBSTITUTE_SENTENCE in augmented
    assert augmented.endswith(f"\n\n---\n\n{prompt}")
    assert augmented.index(mtr.NOTICE_HEADER) < augmented.index("\n\n---\n\n")
    assert strip_turn_preambles(augmented) == prompt
    # the visible graph step is unchanged (no new event, no new tool)
    assert [e.get("current_tool_use", {}).get("name")
            for e in events if "current_tool_use" in e] == ["explore_graph"]


def test_prelude_mode_b_also_injects_the_notice(monkeypatch):
    """Both modes: the rule this complements applies to both."""
    monkeypatch.setenv("SYSTEM_QUERY_MODE", "orchestrator")
    monkeypatch.setattr(orch, "build_graph_first_seed", lambda: "GRAPH-FIRST-SEED")
    prompt = f"What is the {_UNKNOWN_ACRONYM} ratio?"
    augmented, _events, planned = runtime_app._graph_first_prelude(prompt)
    assert mtr.NOTICE_HEADER in augmented
    assert augmented.startswith("GRAPH-FIRST-SEED")
    assert augmented.endswith(f"\n\n---\n\n{prompt}")
    assert planned is None


def test_prelude_without_a_seed_never_notices(monkeypatch):
    """No seed (graph read failed / no systems) ⇒ no vocabulary evidence ⇒ the
    pre-check stays silent and the prompt is untouched."""
    _install_route_seed(monkeypatch, "")
    prompt = f"What is the {_UNKNOWN_ACRONYM} rate?"
    assert runtime_app._graph_first_prelude(prompt) == (prompt, [], None)


def test_prelude_notice_absent_for_a_resolved_term(monkeypatch):
    """A term the deployment defines produces a byte-identical turn."""
    _install_route_seed(monkeypatch, "ROUTE-SEED")
    prompt = "What is the OEE rate for line 3?"
    augmented, _events, _planned = runtime_app._graph_first_prelude(prompt)
    assert augmented == f"ROUTE-SEED\n\n---\n\n{prompt}"


# --------------------------------------------------------------------------- #
# 6. False-positive hardening: the "what is/are the X" shape
# --------------------------------------------------------------------------- #
#: A seed that names no vocabulary at all: anything flagged under it is flagged
#: on SHAPE alone, which is exactly what the false-positive cases must not do.
_BARE_SEED = '[ROUTE SEED] {"systems": [{"id": "sys-a"}]}'


@pytest.mark.parametrize("question", [
    "What CNC machines are down?",
    "What SPC charts show violations?",
    "What RPM did the spindle hold overnight?",
    "What PSI readings are high?",
    "What WIP units are staged?",
    "What AGV units are idle?",
])
def test_bare_what_without_a_copula_never_gates_an_acronym(question):
    """The copula is MANDATORY.

    With the trailing groups all optional a bare ``"What "`` gated every acronym
    in the question, so a question ABOUT equipment ("What CNC machines are
    down?") claimed the equipment class was an undefined METRIC."""
    assert mtr.extract_metric_term_candidates(question) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


@pytest.mark.parametrize("question", [
    "What is the {t} for August?",
    "What are the {t} for August?",
    "What was the {t} yesterday?",
    "What's the {t} for August?",
    "What is {t} for August?",
])
def test_the_real_what_is_shape_still_gates(question):
    """The genuine request shape — copula present, article optional."""
    text = question.format(t=_UNKNOWN_ACRONYM)
    assert mtr.extract_metric_term_candidates(text) == [_UNKNOWN_ACRONYM], text


@pytest.mark.parametrize("question", [
    "Tell me somewhat is the {t} today.",
    "Explain somewhat the {t} moved.",
])
def test_what_is_gate_is_word_boundary_anchored(question):
    """``somewhat is the <term>`` is not a "what is" request."""
    text = question.format(t=_UNKNOWN_ACRONYM)
    assert mtr.extract_metric_term_candidates(text) == [], text


@pytest.mark.parametrize("noun", sorted(mtr._NOUN_HEAD_WORDS))
def test_candidate_used_as_an_adjective_before_a_noun_head_is_not_flagged(noun):
    """A term modifying a plant/data noun is not the target quantity."""
    question = f"What is the {_UNKNOWN_ACRONYM} {noun} rate?"
    assert mtr.extract_metric_term_candidates(question) == [], question
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == "", question


def test_noun_head_gate_applies_only_to_the_immediately_following_word():
    """A noun further along is the metric's SCOPE, not its head — still flagged."""
    question = f"What is the {_UNKNOWN_ACRONYM} rate for open orders?"
    assert mtr.extract_metric_term_candidates(question) == [_UNKNOWN_ACRONYM]
    assert _UNKNOWN_ACRONYM in mtr.metric_term_gap_notice(question, _BARE_SEED)


# --------------------------------------------------------------------------- #
# 7. False-positive hardening: leading verbs and leading-drop resolution
# --------------------------------------------------------------------------- #
def test_leading_auxiliary_verb_is_stripped_from_a_phrase_candidate():
    """``_PHRASE_RE`` absorbs a sentence-initial capitalised verb.

    The reported candidate must be the metric name the question used, never
    ``"Did <phrase>"`` — which additionally could not resolve, so a DEFINED term
    was flagged as undefined (the never-flag-a-resolving-term invariant)."""
    for question in ("Did Line Utilization Rate drop last week?",
                     "Compare Line Utilization Rate across lines",
                     "Show Line Utilization Rate by shift",
                     "Was Line Utilization Rate below target?"):
        assert mtr.extract_metric_term_candidates(question) == [
            "Line Utilization"], question


@pytest.mark.parametrize("verb", sorted(mtr.LEADING_VERB_WORDS))
def test_no_candidate_ever_retains_a_leading_verb(verb):
    question = f"{verb.capitalize()} Line Utilization Rate for last week?"
    for candidate in mtr.extract_metric_term_candidates(question):
        first = candidate.split()[0].lower()
        assert first not in mtr.LEADING_VERB_WORDS, candidate


def test_defined_phrase_is_never_flagged_after_the_verb_strip():
    """The invariant: a term this deployment's seed defines never flags."""
    question = "Did Line Utilization Rate drop last week?"
    defining_seed = ('[ROUTE SEED] {"systems": [{"id": "sys-a", '
                     '"query_guidance": "line utilization is tracked here"}]}')
    assert mtr.unresolved_metric_terms(question, defining_seed) == []
    assert mtr.metric_term_gap_notice(question, defining_seed) == ""
    # …and with nothing defining it, the honest notice names the STRIPPED term.
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == ["Line Utilization"]


def test_verb_plus_one_word_phrase_is_too_weak_to_flag():
    """Only the verb made it a 2+ word phrase ⇒ not a candidate at all."""
    for question in ("Did Line Efficiency Rate drop last week?",
                     "Was Production Efficiency below target?"):
        assert mtr.extract_metric_term_candidates(question) == [], question
        assert mtr.metric_term_gap_notice(question, _BARE_SEED) == "", question


def test_resolution_tries_progressive_leading_token_drops():
    """A sub-phrase that resolves suppresses the notice.

    The candidate carries a leading qualifier the metric label does not, so only
    a leading-dropped form matches the registry label."""
    metric = next(m for m in _metric_layer().list_metrics()
                  if len(m.label.split()) == 2
                  and m.label.replace(" ", "").isalpha()
                  and m.label.split()[-1].lower() in mtr.METRIC_CONTEXT_WORDS)
    question = f"What is the Plant {metric.label} for line 3?"
    assert mtr.extract_metric_term_candidates(question) == [
        f"Plant {metric.label}"]
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_sub_phrases_never_offer_a_bare_measure_word():
    """The widening must not make every phrase resolve on the word 'rate'."""
    forms = mtr._sub_phrases("Sigma Delta Rate")
    assert forms[0] == "Sigma Delta Rate"
    assert all(len(form.split()) >= 2 for form in forms[1:])
    assert "Rate" not in forms
    # a seed made ONLY of measure words resolves nothing
    assert mtr.unresolved_metric_terms(
        "What is the Sigma Delta Rate?",
        "[ROUTE SEED] rate ratio score percent efficiency attainment",
    ) == ["Sigma Delta Rate"]


def test_sub_phrases_keeps_a_single_word_candidate_intact():
    assert mtr._sub_phrases(_UNKNOWN_ACRONYM) == [_UNKNOWN_ACRONYM]
    assert mtr._sub_phrases("") == []
    assert mtr._strip_leading_verbs("Did Line Utilization") == "Line Utilization"
    assert mtr._strip_leading_verbs("Did Was") == ""


# --------------------------------------------------------------------------- #
# 8. 'PM' is a maintenance metric family, not stoplisted vocabulary
# --------------------------------------------------------------------------- #
def test_pm_is_not_stoplisted_but_period_qualifiers_remain():
    """Stoplisting PM bought silence on a real metric family; a period
    QUALIFIER is still not a quantity."""
    assert "PM" not in mtr.TECH_ACRONYM_STOPLIST
    assert {"YTD", "MTD", "QTD", "AM"} <= mtr.TECH_ACRONYM_STOPLIST


def test_pm_metric_request_is_surfaced():
    """Shape 1 — a genuine maintenance-metric request reaches the notice."""
    question = "What is the PM completion rate?"
    assert mtr.extract_metric_term_candidates(question) == ["PM"]
    notice = mtr.metric_term_gap_notice(question, _BARE_SEED)
    assert "'PM'" in notice
    assert mtr.NEVER_SUBSTITUTE_SENTENCE in notice


@pytest.mark.parametrize("question", [
    "Can we move the meeting at 3 PM?",
    "The line stopped at 3 PM yesterday.",
    "Send the shift report at 6 PM.",
])
def test_pm_clock_time_is_never_a_candidate(question):
    """Shape 2 — the metric-context gate already excludes the clock reading."""
    assert mtr.extract_metric_term_candidates(question) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_pm_resolves_when_the_deployment_defines_it():
    question = "What is the PM completion rate?"
    seed = '[ROUTE SEED] {"systems": [{"id": "sys-a", "concepts": ["pm"]}]}'
    assert mtr.unresolved_metric_terms(question, seed) == []
    assert mtr.metric_term_gap_notice(question, seed) == ""


# --------------------------------------------------------------------------- #
# 9. The adversarial false-positive MATRIX
# --------------------------------------------------------------------------- #
# (question, expected candidates, expected unresolved under _BARE_SEED).
# A BENIGN case expects NO candidate and therefore NO notice — those are the
# measured false positives. The last two rows are CONTROLS:
# the hardening must not silence a genuine undefined-metric request.
_ADVERSARIAL_MATRIX: tuple[tuple[str, list, list], ...] = (
    # a bare "What " must not gate an acronym
    ("What CNC machines are down?", [], []),
    ("What SPC charts show violations?", [], []),
    ("What RPM did the spindle hold overnight?", [], []),
    ("What PSI readings are high?", [], []),
    ("What WIP units are staged?", [], []),
    ("What AGV units are idle?", [], []),
    ("Tell me somewhat is the CNC today.", [], []),
    # adjectival use before a plant/data noun head
    ("What is the CNC machines rate?", [], []),
    ("What is the SPC charts percentage?", [], []),
    # sentence-initial verbs absorbed by the phrase shape
    ("Did Line Efficiency Rate drop last week?", [], []),
    ("Was Production Efficiency below target?", [], []),
    ("Compare Line Utilization across lines", [], []),
    ("Did Line Utilization Rate drop last week?",
     ["Line Utilization"], ["Line Utilization"]),
    ("Compare Line Utilization Rate across lines",
     ["Line Utilization"], ["Line Utilization"]),
    # PM: metric request vs clock time
    ("What is the PM completion rate?", ["PM"], ["PM"]),
    ("Can we move the meeting at 3 PM?", [], []),
    # CONTROLS — a real undefined-metric request must still be surfaced
    (f"What is the {_UNKNOWN_ACRONYM} rate for site A for 8/20?",
     [_UNKNOWN_ACRONYM], [_UNKNOWN_ACRONYM]),
    ("What is the RPM rate of the spindle?", ["RPM"], ["RPM"]),
)


@pytest.mark.parametrize("question,candidates,unresolved", _ADVERSARIAL_MATRIX)
def test_adversarial_matrix_classification(question, candidates, unresolved):
    assert mtr.extract_metric_term_candidates(question) == candidates, question
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == unresolved, question


def test_zero_false_positives_across_the_adversarial_matrix():
    """No BENIGN question in the matrix may produce a notice — zero, not fewer."""
    false_positives = [
        question for question, _candidates, unresolved in _ADVERSARIAL_MATRIX
        if not unresolved and mtr.metric_term_gap_notice(question, _BARE_SEED)
    ]
    assert false_positives == []


def test_matrix_controls_still_produce_a_notice():
    """The complement: the hardening did not silence the real thing."""
    flagged = [question for question, _c, unresolved in _ADVERSARIAL_MATRIX
               if unresolved and mtr.metric_term_gap_notice(question, _BARE_SEED)]
    assert len(flagged) == len(
        [q for q, _c, u in _ADVERSARIAL_MATRIX if u]) >= 4


def test_matrix_terms_never_flag_when_the_deployment_defines_them():
    """The never-flag-a-resolving-term invariant across the whole matrix."""
    for question, _candidates, unresolved in _ADVERSARIAL_MATRIX:
        if not unresolved:
            continue
        seed = ('[ROUTE SEED] {"defined": '
                + repr([mtr.normalize_term(t) for t in unresolved]) + "}")
        assert mtr.unresolved_metric_terms(question, seed) == [], question
        assert mtr.metric_term_gap_notice(question, seed) == "", question


# --------------------------------------------------------------------------- #
# 10. Graph-resident metric definitions (ADR-0066 D4) — the TTL-snapshot source
# --------------------------------------------------------------------------- #
def test_graph_defined_metric_id_suppresses_the_notice(monkeypatch):
    """A term matching a graph definition's ID resolves — a metric a steward
    defines post-deploy STOPS being flagged with zero redeploys.

    The label is deliberately unrelated so only the ID can match."""
    _install_graph_definitions(monkeypatch, [_graph_doc(
        metric_id="line-utilization", label="Zeta Composite", synonyms=())])
    question = "Did Line Utilization Rate drop last week?"
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_graph_defined_label_suppresses_the_notice(monkeypatch):
    """The same question resolves through the LABEL when the id is unrelated."""
    _install_graph_definitions(monkeypatch, [_graph_doc(
        metric_id="zeta-composite", label="Line Utilization", synonyms=())])
    question = "Did Line Utilization Rate drop last week?"
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_graph_defined_synonym_suppresses_the_notice(monkeypatch):
    """An acronym resolves through a definition's SYNONYM — id and label are
    deliberately unrelated so only the synonym can match."""
    _install_graph_definitions(monkeypatch, [_graph_doc(
        metric_id="zeta-composite", label="Zeta Composite", synonyms=("LU",))])
    question = "What is the LU rate for the plant?"
    assert mtr.extract_metric_term_candidates(question) == ["LU"]
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_graph_defined_label_initialism_resolves_an_acronym(monkeypatch):
    """The acronym also resolves against the label's INITIALISM with NO synonym
    configured — the same zero-configuration coverage code labels get."""
    _install_graph_definitions(monkeypatch, [_graph_doc(
        metric_id="zeta-composite", label="Line Utilization", synonyms=())])
    question = "What is the LU rate for the plant?"
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_graph_definition_presence_is_exactly_the_difference(monkeypatch):
    """The plan's acceptance pair: WITHOUT the definition the acronym is
    honestly unresolved; WITH it the notice is suppressed."""
    question = "What is the LU rate for the plant?"
    # autouse default: a successfully EMPTY store ⇒ unresolved.
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == ["LU"]
    notice = mtr.metric_term_gap_notice(question, _BARE_SEED)
    assert "'LU'" in notice
    assert mtr.NEVER_SUBSTITUTE_SENTENCE in notice
    # the definition lands (fresh snapshot) ⇒ resolved.
    _install_graph_definitions(monkeypatch, [_graph_doc()])
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""


def test_undefined_term_still_flagged_with_graph_definitions_present(monkeypatch):
    """The graph source widens RESOLUTION only — a genuinely undefined term
    still produces the honest notice."""
    _install_graph_definitions(monkeypatch, [_graph_doc()])
    question = f"What is the {_UNKNOWN_ACRONYM} rate for line 3?"
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == [_UNKNOWN_ACRONYM]
    notice = mtr.metric_term_gap_notice(question, _BARE_SEED)
    assert f"'{_UNKNOWN_ACRONYM}'" in notice
    assert mtr.NEVER_SUBSTITUTE_SENTENCE in notice


def test_warm_snapshot_means_zero_per_turn_graph_calls(monkeypatch):
    """The no-per-turn-round-trip pin: repeated turns inside one TTL window
    make exactly ONE graph read, total."""
    calls = _install_graph_definitions(monkeypatch, [_graph_doc()])
    question = "What is the LU rate for the plant?"
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.unresolved_metric_terms(question, _BARE_SEED) == []
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""
    assert mtr.metric_term_gap_notice(
        f"What is the {_UNKNOWN_ACRONYM} rate?", _BARE_SEED) != ""
    assert len(calls) == 1


@pytest.mark.parametrize("failing_seam", [
    lambda *_a, **_k: [],  # agents.db returns [] on any graph failure
    lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("graph outage")),
])
def test_store_cold_load_failure_suppresses_the_notice(monkeypatch, failing_seam):
    """Fail-OPEN, not fail-partial: a cold store load failure suppresses the
    notice entirely — a graph outage must NEVER let the system claim a term is
    undefined (ADR-0066 D4). The raise-on-broken-source posture is preserved:
    the failure propagates out of ``unresolved_metric_terms`` and is caught
    only at the notice boundary."""
    mds.invalidate()
    monkeypatch.setattr(mds, "neptune_query", failing_seam)
    question = f"What is the {_UNKNOWN_ACRONYM} rate for line 3?"
    assert mtr.metric_term_gap_notice(question, _BARE_SEED) == ""
    with pytest.raises(Exception):
        mtr.unresolved_metric_terms(question, _BARE_SEED)


def test_prelude_notice_absent_for_a_graph_defined_term(monkeypatch):
    """End to end: with the definition in the snapshot, the composed turn is
    byte-identical to an ordinary turn (same pin as a code-defined term)."""
    _install_graph_definitions(monkeypatch, [_graph_doc()])
    _install_route_seed(monkeypatch, "ROUTE-SEED")
    prompt = "What is the LU rate for the plant?"
    augmented, _events, _planned = runtime_app._graph_first_prelude(prompt)
    assert augmented == f"ROUTE-SEED\n\n---\n\n{prompt}"


def test_notice_literals_are_byte_pinned():
    """The two contract literals, byte-exact (this task may not reword them)."""
    assert mtr.NOTICE_HEADER == "[UNRESOLVED METRIC TERM — DETERMINISTIC PRE-CHECK]"
    assert mtr.NEVER_SUBSTITUTE_SENTENCE == (
        "Computing a substitute quantity — a related ratio, the nearest "
        "available metric, or a formula inferred from column or property names "
        "— is a WRONG ANSWER, not an approximation, even if you disclose it."
    )


# --------------------------------------------------------------------------- #
# resolution_seed_view — guidance prose is instructions, not vocabulary
# --------------------------------------------------------------------------- #

_MARKER = "ROUTE SEED RESULT:\n"


def _seed_with(payload: dict) -> str:
    import json as _json
    return "preamble text\n" + _MARKER + _json.dumps(payload)


def test_guidance_note_prose_does_not_resolve_a_term():
    """A learned note that MENTIONS a term (typically to forbid deriving it)
    must not count as the deployment DEFINING it — the deterministic notice
    must still fire and compose with the note (measured live: prose-resolution
    left grain compliance to per-turn model obedience)."""
    seed = _seed_with({"systems": [{
        "id": "system-a",
        "mapped_concepts": ["record-count"],
        "query_guidance": "State the Widget Throughput Rate denominator with "
                          "every figure; never derive it from record rows.",
    }]})
    view = mtr.resolution_seed_view(seed, _MARKER)
    assert "widget throughput" not in view.lower()
    assert mtr.unresolved_metric_terms(
        "What is the Widget Throughput Rate for line 3?", view
    ) == ["Widget Throughput Rate"]
    # The UNFILTERED seed would have resolved it — the view is the difference.
    assert mtr.unresolved_metric_terms(
        "What is the Widget Throughput Rate for line 3?", seed) == []


def test_structured_seed_vocabulary_still_resolves():
    """mapped/shared concepts, fields and plant names stay resolution-eligible."""
    seed = _seed_with({"systems": [{
        "id": "system-a",
        "mapped_concepts": ["widget-units"],
        "query_guidance": "unrelated instruction prose",
    }]})
    view = mtr.resolution_seed_view(seed, _MARKER)
    assert "widget-units" in view
    assert mtr.unresolved_metric_terms(
        "How many Widget Units were produced yesterday?", view) == []


def test_all_guidance_prose_keys_are_stripped_recursively():
    payload = {"systems": [{"id": "s", "query_hints": ["Alpha Beta Rate note"],
                            "answer_requirements": ["Gamma Delta Index note"],
                            "legacy_unscoped": ["Epsilon Zeta Ratio note"],
                            "business_context": "Eta Theta Score prose"}]}
    view = mtr.resolution_seed_view(_seed_with(payload), _MARKER)
    for fragment in ("Alpha Beta", "Gamma Delta", "Epsilon Zeta", "Eta Theta"):
        assert fragment not in view


def test_malformed_payload_returns_original_seed_unchanged():
    """Fail-open toward the pre-change behavior: a bad seed can never ADD flags."""
    seed = "preamble\n" + _MARKER + "{not json"
    assert mtr.resolution_seed_view(seed, _MARKER) == seed


def test_seed_without_marker_is_returned_unchanged():
    assert mtr.resolution_seed_view("plain prose seed", _MARKER) == "plain prose seed"
    assert mtr.resolution_seed_view(None, _MARKER) == ""


# Text after the seed JSON (the IDENTIFIER TRANSLATIONS block, the ADR-0086
# wildcard note) must not make the view fail open: the guidance is still
# stripped and the trailing text survives verbatim.
from agents.identifier_translation import WILDCARD_SEED_NOTE  # noqa: E402

_TRAILING_BLOCK = ("\nIDENTIFIER TRANSLATIONS (deterministic, from graph IdentityRules):\n"
                   "- ITM-5101 (sys-a.code) → PRT-5101 (sys-b.item_id) "
                   "[value_core_match, confidence 0.85]\n")


def _guided_seed() -> str:
    return _seed_with({"systems": [{
        "id": "system-a", "mapped_concepts": ["record-count"],
        "query_guidance": "State the Widget Throughput Rate denominator every time.",
    }]})


@pytest.mark.parametrize("trailing", [
    _TRAILING_BLOCK,
    "\n" + WILDCARD_SEED_NOTE,
    _TRAILING_BLOCK + "\n" + WILDCARD_SEED_NOTE,
])
def test_text_after_the_seed_json_still_strips_guidance(trailing):
    seed = _guided_seed() + trailing
    view = mtr.resolution_seed_view(seed, _MARKER)
    assert "widget throughput" not in view.lower()
    assert view.endswith(trailing)
    assert view.startswith("preamble text\n" + _MARKER)
    assert mtr.unresolved_metric_terms(
        "What is the Widget Throughput Rate for line 3?", view) == ["Widget Throughput Rate"]


def test_whitespace_before_the_seed_json_is_tolerated():
    seed = "preamble text\n" + _MARKER + "  " + _guided_seed().split(_MARKER, 1)[1]
    assert "widget throughput" not in mtr.resolution_seed_view(seed, _MARKER).lower()
