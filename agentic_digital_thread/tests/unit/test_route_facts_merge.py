# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each system delegation's ROUTE context carries the route-seed facts of THAT call.

The orchestrator model is asked to write a fenced ROUTE block into each
delegation's optional ``kg_context`` and mostly leaves it out. The gateway
client merges the route seed's facts in at the call boundary, scoped to the
call (``route_facts.call_facts``):

* a concept comes from an identity rule the question carried values along
  (values an earlier successful call to the rule's other system returned
  under that rule's field: provenance first, the rule's pattern only for an
  unlabelled value), or from the delegated question's own words when the
  target shares the concept;
* a join comes only from such a carried rule, and only when it is specific
  (the question names the target-side field, or one field pair fits);
* a field the model filled stays the model's; with nothing for the call it
  goes out as the model wrote it;
* the decline guard reads the merged block, so a repeat that carries a
  declined call's identifier is refused, while a pronoun-only follow-up
  carries nothing (the documented limit) and an unrelated call is sent.

Every case runs on two differently named schemas (``_route_facts_world``),
through Strands' real ``MCPAgentTool.stream`` and the real per-turn gateway
client. Offline: no AWS / LLM / gateway / Neptune.
"""
from __future__ import annotations

import dataclasses

import pytest

import agents.system_agent as sa
from agents import answer_ids, answer_names
from agents import live_concept_mappings as lcm
from agents.delegation_decline import REFUSAL_PREFIX
from tests.unit._route_facts_world import (  # noqa: F401 - isolated_turn is a fixture
    WORLDS, Client, block, gateway, isolated_turn, label, peer_then_target, result, seed,
    sent, stream,
)

UNRELATED = "Which checks failed last week on line 4?"
PRONOUN = "What about that one?"


@pytest.fixture(autouse=True)
def _fresh_turn(isolated_turn, monkeypatch):
    # A result carrying an IDENTIFIER-MISS block makes the gateway client's
    # miss observer count a failure signal on a background thread; no store
    # is reached from these tests.
    monkeypatch.setattr("agents.failure_signals.record_failure_signal",
                        lambda *_a, **_k: False)
    yield


@pytest.fixture(params=WORLDS, ids=lambda w: w.name)
def world(request):
    return request.param


def _target_kg(fake):
    return sent(fake, 1).get("kg_context", "")


# --------------------------------------------------------------------------- #
# the workshop shape: run ids the peer returned as its batch field
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("empty", [False, True], ids=["omitted", "empty"])
def test_carried_run_ids_bring_the_rule_concept_and_only_its_join(monkeypatch, world, empty):
    target_input = {"question": world.target_question}
    if empty:
        target_input["kg_context"] = ""
    fake = peer_then_target(monkeypatch, world, target_input)
    kg = _target_kg(fake)

    assert kg.startswith(f"```ROUTE:\ntarget_system: {world.target}\n")
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    # Only the join over the rule whose target-side field the question names.
    assert world.join_line in block(kg).splitlines()
    assert world.second_join["id"] not in kg and world.unconfirmed["id"] not in kg
    assert label(kg, "translated_identifiers") == ["translated_identifiers: not given"]
    assert label(kg, "scope") == ["scope: not given"]
    assert sent(fake, 1)["question"] == world.target_question


def test_the_peer_call_gets_the_concept_its_own_question_names(monkeypatch, world):
    seed(world)
    fake = Client()
    client = gateway(monkeypatch, fake)
    stream(client, world.tool(world.peer), {"question": world.peer_question}, "t-peer")
    kg = sent(fake, 0)["kg_context"]
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert label(kg, "join_rule") == ["join_rule: none"]


def test_the_system_agent_reads_the_merged_block(monkeypatch, world):
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    kg = _target_kg(fake)
    join = world.join
    own = join["from_field"] if join["from_system"] == world.target else join["to_field"]
    assert sa._route_targets(f"{world.target_question}\n{kg}", world.target) == \
        {tuple(own.split("."))}
    assert lcm.route_concepts(kg) == [world.concept]


def test_a_question_that_names_no_field_keeps_the_concept_but_no_join(monkeypatch, world):
    question = f"For {world.run_ids[0]} and {world.run_ids[1]}: which line made each?"
    fake = peer_then_target(monkeypatch, world, {"question": question})
    kg = _target_kg(fake)
    # Both rules fit the peer's field; neither target field is named.
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert label(kg, "join_rule") == ["join_rule: none"]


# --------------------------------------------------------------------------- #
# provenance first, the pattern only for an unlabelled value
# --------------------------------------------------------------------------- #
def _with_peer_pattern(world, pattern):
    rules = [dict(r) for r in world.identity_rules]
    rules[0]["source_pattern"] = rules[0]["target_pattern"] = pattern
    return dataclasses.replace(world, identity_rules=tuple(rules))


def _prose(world):
    return f"Shipped {world.run_ids[0]} and {world.run_ids[1]} today."


def test_an_unlabelled_value_with_no_rule_pattern_carries_nothing(monkeypatch, world):
    world = dataclasses.replace(world, peer_answer=_prose(world))
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    assert sent(fake, 1) == {"question": world.target_question}


def test_an_unlabelled_value_is_matched_by_the_rule_pattern(monkeypatch, world):
    head = world.run_ids[0].split("-")[0]
    world = dataclasses.replace(_with_peer_pattern(world, f"{head}-{{a}}-{{b}}"),
                                peer_answer=_prose(world))
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    kg = _target_kg(fake)
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert world.join_line in block(kg).splitlines()


@pytest.mark.parametrize("line_for", [answer_ids.engine_line, answer_names.engine_line],
                         ids=["identifiers-line", "names-line"])
def test_a_value_only_an_orchestrator_engine_line_quotes_carries_nothing(
        monkeypatch, world, line_for):
    """The system agent appends an orchestrator-only engine line when its
    answer gave values no result of the call returned, and the line quotes
    them. Those values were not returned, so they are not carried, even when
    the rule's pattern fits them (the control above shows it would)."""
    head = world.run_ids[0].split("-")[0]
    answer = "No rows matched.\n\n" + line_for(list(world.run_ids[:2]))
    world = dataclasses.replace(_with_peer_pattern(world, f"{head}-{{a}}-{{b}}"),
                                peer_answer=answer)
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    assert sent(fake, 1) == {"question": world.target_question}


def _miss_block(token: str, fenced: bool = True) -> str:
    """An IDENTIFIER-MISS block as the protocol writes it, naming ``token``."""
    body = (f"IDENTIFIER-MISS:\ntoken: {token}\nlooked_in: some_table.some_ref\n"
            "my_format: values are shaped like ZZ-9999\n"
            f"context: {token} has the form of another field.")
    return f"```\n{body}\n```" if fenced else body


def _unpaired_fact(token: str) -> str:
    """The engine's unpaired-forms line and the guard block it carries."""
    from agents.unresolved_miss import MARKER
    return f"{MARKER} figures were not paired.\n\n{_miss_block(token)}"


_BLOCKS = {"agent-fenced": _miss_block, "agent-unfenced": lambda t: _miss_block(t, False),
           "engine-fact": _unpaired_fact}


@pytest.mark.parametrize("kind", sorted(_BLOCKS))
def test_a_value_a_miss_block_repeats_still_follows_its_rule(monkeypatch, world, kind):
    """A value the answer states in prose keeps its identity rule when a miss
    block repeats it under the block's own ``token`` label (every carried
    value is repeated, so none carries the rule by itself)."""
    head = world.run_ids[0].split("-")[0]
    blocks = "\n\n".join(_BLOCKS[kind](v) for v in world.run_ids[:2])
    answer = f"{_prose(world)}\n\n{blocks}"
    world = dataclasses.replace(_with_peer_pattern(world, f"{head}-{{a}}-{{b}}"),
                                peer_answer=answer)
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    kg = _target_kg(fake)
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert world.join_line in block(kg).splitlines()


@pytest.mark.parametrize("kind", sorted(_BLOCKS))
def test_a_value_only_a_miss_block_names_carries_nothing(monkeypatch, world, kind):
    head = world.run_ids[0].split("-")[0]
    answer = "No rows matched.\n\n" + "\n\n".join(_BLOCKS[kind](v) for v in world.run_ids[:2])
    world = dataclasses.replace(_with_peer_pattern(world, f"{head}-{{a}}-{{b}}"),
                                peer_answer=answer)
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    assert sent(fake, 1) == {"question": world.target_question}


def test_miss_blocks_are_removed_and_everything_else_kept():
    from agents.route_facts import without_miss_blocks
    plain = "Run AB-1 shipped.\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert without_miss_blocks(plain) is plain
    text = (f"Before.\n\n{_miss_block('AB-12')}\n\nMiddle.\n\n{_miss_block('AB-13', False)}"
            "\n\nAfter.\n\n```\ncode 1\n```")
    assert without_miss_blocks(text) == "Before.\n\n\nMiddle.\n\n\nAfter.\n\n```\ncode 1\n```"
    mention = "The answer has no IDENTIFIER-MISS block for AB-12."
    assert without_miss_blocks(mention) == mention


def test_the_field_labels_are_the_protocols_and_the_guards():
    from agents import identifier_shape_guard as g
    from agents.route_facts import MISS_FIELD_LABELS
    assert MISS_FIELD_LABELS == sa._MISS_PROTOCOL_LABELS
    col = g.Column("t", "c", "k", ("x",), "AA-9")
    block = g._miss_block("AB-12", col, g.Column("u", "c", "k", ("y",), "BB-9"))
    labels = [line.split(":", 1)[0] for line in block.splitlines()[1:]]
    assert labels == list(MISS_FIELD_LABELS)


_FIELDS = ("token: AB-12\nlooked_in: some_table.some_ref\n"
           "my_format: values are shaped like ZZ-9999")
_TABLE = "| run | qty |\n|---|---|\n| RUN-42 | 8 |"


@pytest.mark.parametrize("text,kept", [
    (f"Before.\n```\nIDENTIFIER-MISS:\n{_FIELDS}\nREAL-DATA: RUN-42 shipped 8 units\n```\nAfter.",
     "REAL-DATA: RUN-42 shipped 8 units"),
    (f"Before.\n```\nIDENTIFIER-MISS:\n{_FIELDS}\n{_TABLE}\n```\nAfter.", _TABLE),
    (f"Before.\n```\nIDENTIFIER-MISS:\n{_FIELDS}\n\nRUN-42 shipped 8 units.", "RUN-42 shipped"),
    (f"Before.\n```\nIDENTIFIER-MISS:\n{_FIELDS}\n{_TABLE}", _TABLE),
    (f"Before.\nIDENTIFIER-MISS:\n{_FIELDS}\nRUN-42 shipped 8 units.", "RUN-42 shipped 8 units."),
], ids=["data-before-the-fence-closes", "table-before-the-fence-closes",
        "data-after-an-unclosed-block", "table-after-an-unclosed-block",
        "data-right-after-an-unfenced-block"])
def test_real_data_after_the_block_fields_is_kept(text, kept):
    from agents.route_facts import without_miss_blocks
    out = without_miss_blocks(text)
    assert kept in out and "Before." in out
    assert "AB-12" not in out and "IDENTIFIER-MISS" not in out


def test_a_closing_fence_before_a_marker_is_not_taken_for_its_opening():
    from agents.route_facts import without_miss_blocks
    text = f"```\ncode 1\n```\nIDENTIFIER-MISS:\n{_FIELDS}\n\nAfter RUN-42."
    assert without_miss_blocks(text) == "```\ncode 1\n```\n\nAfter RUN-42."


@pytest.mark.parametrize("closed", [True, False], ids=["before-the-fence-closes", "unclosed"])
def test_a_value_inside_a_block_after_its_fields_still_follows_its_rule(
        monkeypatch, world, closed):
    """Real data the answer writes after the block fields, before the fence
    closes or with no closing fence, is read: its values keep their rule."""
    head = world.run_ids[0].split("-")[0]
    opened = "\n".join(_miss_block("ZZ-1234").split("\n")[:-1])
    answer = f"Before.\n\n{opened}\n{_prose(world)}" + ("\n```" if closed else "")
    world = dataclasses.replace(_with_peer_pattern(world, f"{head}-{{a}}-{{b}}"),
                                peer_answer=answer)
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    kg = _target_kg(fake)
    assert label(kg, "canonical_concept") == [f"canonical_concept: {world.concept}"]
    assert world.join_line in block(kg).splitlines()


def test_a_json_answers_note_loses_its_miss_blocks():
    import json
    from agents.route_facts import without_miss_blocks
    from agents.tool_result_capture import RESULT_CUT_NOTE_KEY
    text = json.dumps({"rows": [["AB-12", 3]], RESULT_CUT_NOTE_KEY: _unpaired_fact("AB-12")})
    out = json.loads(without_miss_blocks(text))
    assert out["rows"] == [["AB-12", 3]]
    assert "IDENTIFIER-MISS" not in out[RESULT_CUT_NOTE_KEY]


def test_a_label_naming_no_rule_field_wins_over_the_pattern(monkeypatch, world):
    head = world.run_ids[0].split("-")[0]
    answer = ("| Shipment | Batch |\n|---|---|\n"
              f"| S-1 | {world.run_ids[0]} |\n| S-2 | {world.run_ids[1]} |\n")
    world = dataclasses.replace(_with_peer_pattern(world, f"{head}-{{a}}-{{b}}"),
                                peer_answer=answer)
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question})
    assert sent(fake, 1) == {"question": world.target_question}


def test_a_failed_peer_call_carries_nothing(monkeypatch, world):
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question},
                            peer_status="error")
    assert sent(fake, 1) == {"question": world.target_question}


# --------------------------------------------------------------------------- #
# the model's own fields win; nothing for the call means no change
# --------------------------------------------------------------------------- #
def test_model_supplied_fields_win(monkeypatch, world):
    model_route = (f"ROUTE:\ntranslated_identifiers: {world.target_field}={world.run_ids[0]}\n"
                   "canonical_concept: production run\nscope: site 2, last 7 days\n"
                   "join_rule: none\n\nAsk only about failed checks.")
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question,
                                                 "kg_context": model_route})
    kg = _target_kg(fake)
    assert kg.startswith(f"```ROUTE:\ntarget_system: {world.target}\n"
                         f"translated_identifiers: {world.target_field}={world.run_ids[0]}\n"
                         "canonical_concept: production run\n"
                         "scope: site 2, last 7 days\n"
                         f"{world.join_line}\n```")
    assert "\n\nAsk only about failed checks." in kg


def test_a_complete_model_block_goes_out_as_written(monkeypatch, world):
    model_route = (f"```ROUTE:\ntarget_system: {world.target}\ntranslated_identifiers: none\n"
                   f"canonical_concept: {world.concept}\nscope: site 2\n"
                   "join_rule: j-0 a.b -> c.d\n```")
    fake = peer_then_target(monkeypatch, world, {"question": world.target_question,
                                                 "kg_context": model_route})
    # The concept notes the turn matched still follow the model's own block.
    assert _target_kg(fake).startswith(model_route + "\n\n")


def test_no_seed_means_no_change(monkeypatch, world):
    fake = Client([result(world.peer_answer)])
    client = gateway(monkeypatch, fake)
    stream(client, world.tool(world.peer), {"question": world.peer_question}, "t-peer")
    stream(client, world.tool(world.target), {"question": world.target_question}, "t-target")
    assert sent(fake, 0) == {"question": world.peer_question}
    assert sent(fake, 1) == {"question": world.target_question}


def test_a_target_the_seed_has_nothing_for_goes_out_as_written(monkeypatch, world):
    seed(world)
    fake = Client()
    client = gateway(monkeypatch, fake)
    stream(client, world.tool(world.other), {"question": world.target_question}, "t-other")
    assert sent(fake, 0) == {"question": world.target_question}


def test_an_unrelated_second_question_gets_neither_concept_nor_join(monkeypatch, world):
    fake = peer_then_target(monkeypatch, world, {"question": UNRELATED})
    assert sent(fake, 1) == {"question": UNRELATED}


def test_a_non_delegation_tool_is_untouched(monkeypatch, world):
    seed(world)
    fake = Client()
    client = gateway(monkeypatch, fake)
    model_input = {"query": f"MATCH (n) WHERE n.id = '{world.run_ids[0]}' RETURN n"}
    stream(client, f"{world.target}___save_correlation", dict(model_input), "t-x")
    assert sent(fake, 0) == model_input


# --------------------------------------------------------------------------- #
# the decline guard reads the merged, call-specific block
# --------------------------------------------------------------------------- #
def _declined(concept):
    return result(f"I cannot give that.\n\n[engine] declined: reason_kind=not_connected; "
                  f"concept={concept}")


def _declined_turn(monkeypatch, world):
    seed(world)
    fake = Client([result(world.peer_answer), _declined(world.concept)])
    client = gateway(monkeypatch, fake)
    stream(client, world.tool(world.peer), {"question": world.peer_question}, "t1")
    stream(client, world.tool(world.target), {"question": world.target_question}, "t2")
    return fake, client


def test_a_repeat_carrying_a_declined_calls_identifier_is_refused(monkeypatch, world):
    fake, client = _declined_turn(monkeypatch, world)
    events = stream(client, world.tool(world.target),
                    {"question": f"And {world.run_ids[0]} on its own?"}, "t3")
    assert len(fake.calls) == 2
    assert REFUSAL_PREFIX in str(events[-1])


def test_a_pronoun_only_follow_up_gets_no_concept_and_is_sent(monkeypatch, world):
    fake, client = _declined_turn(monkeypatch, world)
    stream(client, world.tool(world.target), {"question": PRONOUN}, "t3")
    # The documented limit: nothing in it to carry a concept.
    assert len(fake.calls) == 3
    assert sent(fake, 2) == {"question": PRONOUN}


def test_after_a_decline_unrelated_calls_and_other_systems_go_through(monkeypatch, world):
    fake, client = _declined_turn(monkeypatch, world)
    stream(client, world.tool(world.target), {"question": UNRELATED}, "t3")
    stream(client, world.tool(world.peer), {"question": world.peer_question}, "t4")
    assert [name for name, _ in fake.calls] == [world.tool(world.peer), world.tool(world.target),
                                                world.tool(world.target), world.tool(world.peer)]


def test_the_guard_still_refuses_the_models_own_repeat(monkeypatch, world):
    fake, client = _declined_turn(monkeypatch, world)
    events = stream(client, world.tool(world.target), {
        "question": "List the open rows.",
        "kg_context": f"ROUTE:\ntarget_system: {world.target}\ncanonical_concept: {world.concept}\n"},
        "t3")
    assert len(fake.calls) == 2
    assert REFUSAL_PREFIX in str(events[-1])
