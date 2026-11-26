# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Six patterns were rewritten so no repeat can split an input two ways
(possessive repeats, or one character per repeat). The rewrite must not change
what they match: each new pattern is compared with the pattern it replaced on
fixed cases and on a seeded random corpus, through the same call its module
makes (``sub``, ``findall``, ``fullmatch``, ``match`` or ``search``). A timing
check confirms the worst-case inputs stay fast.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from agents import duckdb_tools, eval_core, listed_keys, pii, query_intelligence

_OLD = {
    "field": r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\s*(?:[<>]=?|[!=]=|=)\s*\d+(?:\.\d+)?",
    "key": r"(?<![\w-])[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)+(?![\w-])",
    "bare": r"^(?:\"?[A-Za-z_][A-Za-z0-9_$]*\"?\s*\.\s*)*\"?([A-Za-z_][A-Za-z0-9_$]*)\"?$",
    "idtail": r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*-\Z",
    "nodata": (r"\[engine\] No query this call ran returned data "
               r"\(((?:[a-z_]{1,40}|no rows)(?:, (?:[a-z_]{1,40}|no rows)){0,15})\)\."),
    "ident": r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$",
}
_NEW = {
    "field": eval_core._FIELD_PREDICATE_PATTERN,
    "key": listed_keys._KEY_TOKEN,
    "bare": listed_keys._BARE_REF,
    "idtail": pii._ID_PREFIX_TAIL,
    "nodata": query_intelligence._NO_DATA_LINE_RE,
    "ident": duckdb_tools._SAFE_IDENTIFIER_RE,
}
_OPS = {
    "field": ("sub",), "key": ("findall", "fullmatch", "sub"), "bare": ("match",),
    "idtail": ("search",), "nodata": ("search",), "ident": ("match",),
}
_ALPHABET = {
    "field": "a_b1_ =<>!.9 x", "key": "aB1-_/. x", "bare": 'ab_$1". x', "idtail": "aB1- _",
    "ident": "aZ_9.\n :/",
}
_FIXED = {
    "field": ["oee_count = 0", "a_b>=1.5", "x_y_z != 3", "OEE = 87%", "a__b = 1", "a_ = 1", "_a_b = 2"],
    "key": ["WO-1001", "ab-cd/ef-", "ab-cd.", "RUN_2026_03/lot.A1", "-x-y", "a--b", "x/y/z w-v"],
    "bare": ['"s"."t"', "a . b . c", "a.", ".a", 'x."y"', "a.b.c$1", "1a.b"],
    "idtail": ["WO-", "RUN-2026-", "lot_A1-", "a--", "-", "ab-cd-", "9a-"],
    "ident": ["s3t.ns.tbl", "a", "_x.y_1", "a.", ".a", "a..b", "a.1b", "1a", "a.b\n", "a b", "s3://x"],
    "nodata": [
        "[engine] No query this call ran returned data (no rows).",
        "[engine] No query this call ran returned data (access_denied, no rows).",
        "[engine] No query this call ran returned data (no, no rows).",
        "[engine] No query this call ran returned data (no rowsx).",
        "x [engine] No query this call ran returned data (" + "a" * 41 + ").",
    ],
}


def _result(rx, op, text):
    if op == "sub":
        return rx.sub(" ", text)
    if op == "findall":
        return rx.findall(text)
    match = getattr(rx, op)(text)
    return (match.span(), match.groups()) if match else None


def _random_nodata(rng):
    items = [rng.choice(["no rows", "no", "rows", "a" * rng.randint(1, 45), "x_y", "bad-1", ""])
             for _ in range(rng.randint(0, 18))]
    sep = rng.choice([", ", ",", "  , "])
    return (rng.choice(["", "pre "]) + "[engine] No query this call ran returned data ("
            + sep.join(items) + rng.choice([").", ")", "). y"]))


@pytest.mark.parametrize("name", sorted(_OLD))
def test_the_rewrite_matches_what_the_old_pattern_matched(name):
    old, new = re.compile(_OLD[name]), _NEW[name]
    rng = random.Random(1118)
    corpus = list(_FIXED[name])
    for _ in range(20_000):
        if name == "nodata":
            corpus.append(_random_nodata(rng))
        else:
            corpus.append("".join(rng.choice(_ALPHABET[name]) for _ in range(rng.randint(0, 14))))
    for text in corpus:
        for op in _OPS[name]:
            assert _result(new, op, text) == _result(old, op, text), (name, op, text)


_WORST = {
    "field": [lambda n: "a_" * (n // 2) + "!", lambda n: "a_b" + " " * n + "x"],
    "key": [lambda n: "a-" * (n // 2) + "-", lambda n: "a" * n + "-"],
    "bare": [lambda n: "a." * (n // 2) + "!", lambda n: '"a" . ' * (n // 5) + "!"],
    "nodata": [lambda n: "[engine] No query this call ran returned data (" + "a, " * (n // 3) + "!"],
    "ident": [lambda n: "a." * (n // 2) + "!", lambda n: "a" * n + ".!"],
}


@pytest.mark.parametrize("name", sorted(_WORST))
def test_worst_case_inputs_stay_fast(name):
    rx = _NEW[name]
    for gen in _WORST[name]:
        text = gen(16_000)
        start = time.perf_counter()
        for op in _OPS[name]:
            _result(rx, op, text)
        assert time.perf_counter() - start < 0.5, (name, gen(20))
