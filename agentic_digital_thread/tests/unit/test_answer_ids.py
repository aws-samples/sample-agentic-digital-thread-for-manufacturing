# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``answer_ids``: which identifiers an answer writes in the form of the
schema's example values that no returned data value of the call holds, and
the fixed engine line.

Every schema here is invented and shares no table, column or value with any
demo dataset. The contract is conservative: an identifier counts as returned
when any data string of any result holds it between separators or ends, and
a call with a result too large to read whole reports nothing."""
from __future__ import annotations

import json
import os

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.system_agent as sa  # noqa: E402
from agents import answer_ids as ai  # noqa: E402

DESC = (
    "kiln_lots [view](lot_ref: uuid e.g. 0b6f0e8e-1c1d-4c55-9a41-6f0f3f0a9b11, "
    "6d2b8a51-2f1e-4f7c-8e0d-2a8e5f1b7c42; lot_code: character varying e.g. GLZ-0412-A, "
    "GLZ-0413-B, GLZ-0587-C; kiln_tag: character varying e.g. KX-SOUTH-WEST; "
    "state: character varying e.g. FIRING, COOLING_DOWN; batch_note: text e.g. "
    "Hairline crack near rim, Glaze too thin; fired_at: timestamp without time zone "
    "e.g. 2031-04-02 11:05:00; weight_kg: numeric e.g. 12.50, 9.75)\n"
    "buyers(buyer_ref: uuid e.g. 9a1d2c3b-4e5f-4a6b-8c7d-0e1f2a3b4c5d; buyer_code: "
    "character varying e.g. BYR-77-01, BYR-77-02; buyer_name: character varying "
    "e.g. Harbor Tile Works, Ridge Ceramics)"
)
MADE_UP = "GLZ-RIM-9001"


def _leads(desc: str = DESC) -> frozenset:
    return ai.schema_id_leads(sa._desc_entries(desc))


def _result(payload, status: str = "success") -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {"toolUseId": "t", "status": status, "content": [{"text": text}]}


def _table(rows, columns=("value",), sql: str = "SELECT value FROM t") -> dict:
    return {"columns": list(columns), "rows": rows, "row_count": len(rows),
            "_adt_executed_query": sql}


def _unreturned(answer: str, *payloads, question: str = "", leads=None) -> list:
    leads = _leads() if leads is None else leads
    state: dict = {}
    for payload in payloads:
        result = payload if isinstance(payload, dict) and "content" in payload else _result(payload)
        ai.record_result(state, result, leads)
    return ai.unreturned_ids(answer, leads, state, question)


ROWS = _table([["GLZ-0412-A", "Harbor Tile Works", "BYR-77-01"]],
              ("lot_code", "buyer_name", "buyer_code"))


def test_schema_id_leads_reads_identifier_shaped_text_examples_only():
    # GLZ-* and BYR-* carry digits; KX-SOUTH-WEST has three segments. UUIDs,
    # timestamps, numbers, names and a two-segment enum without a digit do not count.
    assert _leads() == frozenset({"glz", "byr", "kx"})


def test_a_lead_needs_two_letters_and_a_text_column():
    desc = ("t(a: character varying e.g. Q-1001, 7A-1001; b: integer e.g. ZZ-1001; "
            "c: character varying e.g. GLZ-0412-A\u2026, [REDACTED])")
    assert _leads(desc) == frozenset()


def test_an_identifier_in_no_result_is_reported():
    answer = f"Lot GLZ-0412-A went to Harbor Tile Works (BYR-77-01); its glaze code is {MADE_UP}."
    assert _unreturned(answer, ROWS) == [MADE_UP]


def test_returned_identifiers_and_the_question_s_are_left_alone():
    answer = f"Lot GLZ-0412-A ({MADE_UP}) went to BYR-77-01."
    assert _unreturned(answer, ROWS, question=f"Where did {MADE_UP} go?") == []
    assert _unreturned(answer, ROWS, _table([[MADE_UP]], ("lot_code",))) == []


def test_case_is_folded_and_the_answer_s_spelling_is_quoted():
    answer = f"The code is {MADE_UP.lower()}."
    assert _unreturned(answer, ROWS) == [MADE_UP.lower()]
    assert _unreturned(answer, _table([[MADE_UP]], ("lot_code",))) == []


def test_only_lead_forms_with_a_digit_are_checked():
    answer = ("State COOLING_DOWN on kiln KX-SOUTH-WEST; order PO-4410-Z and "
              "GLZ-RIM-NOTE are not lot codes.")
    assert _unreturned(answer, ROWS) == []


def test_a_token_cut_with_an_ellipsis_is_skipped():
    assert _unreturned("Lots GLZ-0412\u2026 and GLZ-05... were fired.", ROWS) == []


def test_a_zero_row_lookup_and_an_error_result_prove_nothing():
    empty = _table([], ("lot_code",), sql=f"SELECT lot_code FROM kiln_lots WHERE lot_code = '{MADE_UP}'")
    failed = _result(f"Error: no lot {MADE_UP}", status="error")
    assert _unreturned(f"No lot {MADE_UP} was found.", ROWS, empty, failed) == [MADE_UP]


def test_engine_keys_are_never_data():
    payload = {"rows_found": 0, "_adt_note": f"looked for {MADE_UP}"}
    assert _unreturned(f"See {MADE_UP}.", payload) == [MADE_UP]


def test_non_tabular_json_and_plain_text_results_are_data():
    assert _unreturned(f"See {MADE_UP}.", {"lot": {"code": MADE_UP}}) == []
    assert _unreturned(f"See {MADE_UP}.", f"lot code: {MADE_UP}, fired") == []


def test_accepted_miss_a_longer_returned_token_holds_the_shorter_one():
    assert _unreturned(f"See {MADE_UP}.", _table([[f"{MADE_UP}-X"]])) == []
    assert _unreturned(f"See {MADE_UP}.", _table([[f"OLD-{MADE_UP}"]])) == []


def test_a_cut_result_turns_the_check_off_for_the_call():
    from agents import answer_names
    huge = "x" * (answer_names.MAX_RESULT_CHARS + 1)
    assert _unreturned(f"See {MADE_UP}.", ROWS, huge) == []


def test_too_many_result_tokens_turn_the_check_off(monkeypatch):
    monkeypatch.setattr(ai, "MAX_RESULT_TOKENS", 3)
    many = _table([[f"GLZ-{k:04d}-A"] for k in range(5)], ("lot_code",))
    assert _unreturned(f"See {MADE_UP}.", many) == []


def test_no_leads_means_no_check():
    assert _unreturned(f"See {MADE_UP}.", ROWS, leads=frozenset()) == []


def test_each_identifier_once_in_answer_order_at_most_max_ids():
    codes = [f"GLZ-RIM-{9000 + k}" for k in range(ai.MAX_IDS + 2)]
    answer = " ".join(codes) + " " + codes[0]
    assert _unreturned(answer, ROWS) == codes[:ai.MAX_IDS]


def test_the_record_keeps_lead_tokens_only():
    state: dict = {}
    ai.record_result(state, _result(ROWS), _leads())
    assert state[ai.RESULT_IDS_STATE_KEY]["tokens"] == {"glz-0412-a", "byr-77-01"}


def test_the_engine_line_is_fixed_and_carries_only_the_identifiers():
    one = ai.engine_line([MADE_UP])
    assert one.startswith(ai.MARKER)
    assert f'"{MADE_UP}"' in one and "it has the form" in one
    two = ai.engine_line([MADE_UP, "BYR-77-09"])
    assert "they have the form" in two and '"BYR-77-09"' in two
