# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Concept synonyms are stored as one JSON string property, read in every shape.

Neptune openCypher rejects a list as a property value ("Unsupported property
value provided"), so a steward's synonym edit SET as a list failed on the live
graph and no concept ever carried synonyms there. The vocabulary apply now
stores the list as a JSON array string; every reader decodes that, and still
accepts the older shapes (a list, as an in-memory graph returns it, or a
comma-separated string). Offline and pure: synthetic, generic fixtures.
"""
from __future__ import annotations

import json

import pytest

import agents.concept_notes as cn
from agents import vocab_import_core as vic
from agents import vocabulary


def test_encode_is_a_json_string_and_round_trips_commas():
    synonyms = ["widget run", "run, widget", "W\u00fc run"]
    encoded = vic.encode_synonyms(synonyms)
    assert isinstance(encoded, str)
    assert json.loads(encoded) == synonyms
    assert vic.decode_synonyms(encoded) == synonyms


@pytest.mark.parametrize("value,expected", [
    (None, []),
    ("", []),
    ([], []),
    (["alpha", "beta"], ["alpha", "beta"]),
    (("alpha",), ["alpha"]),
    ("alpha, beta", ["alpha", "beta"]),
    ('["alpha", "beta, gamma"]', ["alpha", "beta, gamma"]),
    ("[not json", ["[not json"]),
    ('{"a": 1}', ['{"a": 1}']),
    ('["alpha", 3, "", " beta "]', ["alpha", "beta"]),
    (7, []),
])
def test_decode_accepts_every_stored_shape(value, expected):
    assert vic.decode_synonyms(value) == expected


def test_vocabulary_rows_decode_a_json_string():
    assert vocabulary._as_synonym_list('["takt", "line rate, takt"]') == ["takt", "line rate, takt"]
    assert vocabulary._as_synonym_list(["takt"]) == ["takt"]
    assert vocabulary._as_synonym_list("takt, rate") == ["takt", "rate"]


def test_concept_notes_snapshot_decodes_a_json_string_and_matches_on_it():
    rows = [{"id": "widget_class", "name": "Widget class",
             "description": "The class a widget belongs to.",
             "synonyms": vic.encode_synonyms(["gizmo grade", "thing tier"]),
             "systems": ["system-a"], "entity_systems": []}]
    concepts = cn.concepts_from_rows(rows)
    assert concepts[0].synonyms == ("gizmo grade", "thing tier")
    notes = cn.match_notes("How many units per gizmo grade last week?", concepts)
    assert [n.concept for n in notes] == ["widget_class"]
