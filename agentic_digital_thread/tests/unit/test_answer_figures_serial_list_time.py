# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``answer_figures._SERIAL_LIST`` reads a serial list in linear time.

The pattern finds ``a, b and c`` in the label before a figure. That label is
text from a model answer, so its length is not bounded by the app. The first
form of the pattern started a fresh scan at every position and took time
quadratic in the length of a long comma-separated sentence (about 3 s for
16,000 characters). It now anchors on the last comma, so the same text takes a
few milliseconds. The answers the pattern accepts and refuses are unchanged.
"""
import re
import time

import pytest

from agents import answer_figures as af

#: ``_SERIAL_LIST`` as it was before the linear form, kept to prove both agree.
_FIRST_FORM = re.compile(r"[^,;:()]+(?:,[^,;:()]+)+,?\s+(?:and|or|&)\s+\S", re.I)

_LABELS = [
    "a, b and c",
    "a, b, and c",
    "in a, b, or c yield",
    "a,b and c",
    "a, b & c",
    "a , b , and c",
    "a, b  and  c",
    "a, b\nand c",
    "A, B AND C",
    "a, b and",
    "a, b and ",
    "a and b",
    "a, b",
    ", b and c",
    "a,, b and c",
    "a; b, c and d",
    "a: b, c and d",
    "(a, b) and c",
    "a, b android c",
    "a, b, ,and c",
    "1,110 records in a, b and c",
    "",
]


@pytest.mark.parametrize("label", _LABELS)
def test_serial_list_accepts_what_the_first_form_accepted(label):
    assert bool(af._SERIAL_LIST.search(label)) == bool(_FIRST_FORM.search(label))


@pytest.mark.parametrize("label,expected", [
    ("a, b and c", True),
    ("a, b, and c", True),
    ("a and b", False),
    ("a, b", False),
    ("1,110 records", False),
])
def test_serial_list_known_shapes(label, expected):
    assert bool(af._SERIAL_LIST.search(label)) is expected


@pytest.mark.parametrize("make", [
    lambda n: "a," * (n // 2),
    lambda n: "a, " * (n // 3) + "and",
    lambda n: "a," + "b" * n,
    lambda n: "a,b" + " " * n + "x",
    lambda n: " ," * (n // 2),
])
def test_serial_list_is_linear_on_a_long_label(make):
    label = make(20_000)
    start = time.perf_counter()
    af._SERIAL_LIST.search(label)
    assert time.perf_counter() - start < 0.5
