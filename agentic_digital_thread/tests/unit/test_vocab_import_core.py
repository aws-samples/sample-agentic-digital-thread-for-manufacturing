# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``agents.vocab_import_core``.

The zero-write import classifier extracted from ``vocabulary_routes.py``.
Covers:

  * classification parity cases ported from the route tests — exact skip,
    normalized (C6) collision, in-batch collision;
  * ``preview_rows`` produces exactly the ``/import/preview`` per-row
    ``{id, name, action, collides_with}`` + counters shape;
  * boundary validation (caps, per-row messages naming the ACTUAL problem);
  * import purity — the module imports with boto3/pg8000/fastapi absent
    (stdlib-only, stricter than the router's own purity pin);
  * ONE classifier home — the router binds to THESE functions (identity),
    and no second ``classify_import_rows`` definition exists in ``agents/``.

Pure — no AWS, no graph, no network. Placeholder names only (widget /
system-a style) per the engine-domain neutrality ratchet.
"""

import os
import subprocess  # nosec B404 - fresh-interpreter import-purity check, fixed argv
import sys
from pathlib import Path

import pytest

import agents.vocab_import_core as core
import agents.vocabulary_routes as vr


# --------------------------------------------------------------------------- #
# ONE classifier home
# --------------------------------------------------------------------------- #

def test_router_binds_to_the_one_classifier_home():
    """The router's import-path aliases must BE the core functions —
    identity, not copies (no drift by construction)."""
    assert vr._classify_import_rows is core.classify_import_rows
    assert vr._parse_import_body is core.parse_import_body
    assert vr._preview_rows is core.preview_rows
    assert vr._slug is core.slugify


def test_classifier_defined_exactly_once_in_agents():
    """Grep-gate: exactly ONE ``classify_import_rows`` definition across
    ``agents/`` — the extraction must not leave a stale copy behind."""
    agents_dir = Path(core.__file__).resolve().parent
    defs = []
    for path in sorted(agents_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("def classify_import_rows(", "def _classify_import_rows("):
            if needle in text:
                defs.append((path.name, needle))
    assert defs == [("vocab_import_core.py", "def classify_import_rows(")]


# --------------------------------------------------------------------------- #
# collision_key / slugify
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, expected", [
    ("widget_rate", "widget_rate"),
    ("widget-rate", "widget_rate"),   # the C6 underscore-drift trap
    ("Widget-Rate ", "widget_rate"),
    ("", ""),
])
def test_collision_key_normalizes_case_and_hyphens(raw, expected):
    assert core.collision_key(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Widget Rate", "widget_rate"),
    ("My-Pack v2!", "my_pack_v2"),
    ("!!!", ""),
    (None, ""),
])
def test_slugify(raw, expected):
    assert core.slugify(raw) == expected


# --------------------------------------------------------------------------- #
# classify_import_rows — parity cases ported from the route tests
# --------------------------------------------------------------------------- #

def _row(cid, name="Widget"):
    return {"id": cid, "name": name, "description": ""}


def test_classify_exact_skip_wins_over_collision():
    """An exact graph match stays ``skip_exact`` even when a hyphen sibling
    also exists in the graph (ported from the route's skipped_ids pin)."""
    out = core.classify_import_rows(
        [_row("widget_rate")], {"widget_rate", "widget-rate"})
    assert out == [({"id": "widget_rate", "name": "Widget", "description": ""},
                    "skip_exact", None)]


def test_classify_normalized_collision_names_the_existing_id():
    out = core.classify_import_rows([_row("widget_rate")], {"widget-rate"})
    assert [(a, c) for _, a, c in out] == [("skip_collision", "widget-rate")]


def test_classify_in_batch_dupe_collides_with_the_earlier_row():
    """First row inserts; the later row normalizing to the same key is a
    collision against the EARLIER row's id (request order preserved)."""
    out = core.classify_import_rows(
        [_row("part_state"), _row("part-state", "Part State Again")], set())
    assert [(r["id"], a, c) for r, a, c in out] == [
        ("part_state", "insert", None),
        ("part-state", "skip_collision", "part_state"),
    ]


def test_classify_collision_target_is_deterministic():
    """Two existing graph ids sharing one normalized key: the SORTED-first id
    is always the reported collision target."""
    existing = {"widget-rate", "Widget-Rate"}  # both normalize to widget_rate
    out = core.classify_import_rows([_row("widget_rate")], existing)
    assert out[0][1] == "skip_collision"
    assert out[0][2] == "Widget-Rate"  # sorted(): uppercase sorts first


def test_classify_is_deterministic_same_input_twice():
    rows = [_row("alpha_thing"), _row("beta-thing"), _row("alpha-thing")]
    existing = {"beta_thing", "gamma-thing"}
    assert (core.classify_import_rows(rows, set(existing))
            == core.classify_import_rows(rows, set(existing)))


# --------------------------------------------------------------------------- #
# preview_rows — the /import/preview projection shape
# --------------------------------------------------------------------------- #

def test_preview_rows_shape_and_counters():
    """Exactly the route's per-row ``{id, name, action, collides_with}`` +
    counters shape, in request order (ported from the route preview test)."""
    rows = [
        _row("existing_concept", "Existing"),
        _row("legacy_metric", "Legacy Metric"),
        _row("fresh_concept", "Fresh"),
        _row("fresh_concept", "Fresh Again"),  # in-batch dupe by key
    ]
    result = core.preview_rows(rows, {"existing_concept", "legacy-metric"})
    assert result == {
        "rows": [
            {"id": "existing_concept", "name": "Existing",
             "action": "skip_exact", "collides_with": None},
            {"id": "legacy_metric", "name": "Legacy Metric",
             "action": "skip_collision", "collides_with": "legacy-metric"},
            {"id": "fresh_concept", "name": "Fresh",
             "action": "insert", "collides_with": None},
            {"id": "fresh_concept", "name": "Fresh Again",
             "action": "skip_collision", "collides_with": "fresh_concept"},
        ],
        "insert_count": 1,
        "skip_count": 1,
        "collision_count": 2,
    }


def test_preview_rows_agrees_with_classify():
    """Property-style drift guard: preview's actions ARE the classifier's."""
    rows = [_row("a_thing"), _row("b-thing"), _row("a-thing")]
    existing = {"b_thing"}
    classified = core.classify_import_rows(rows, set(existing))
    preview = core.preview_rows(rows, set(existing))
    assert [(r["id"], r["action"], r["collides_with"]) for r in preview["rows"]] \
        == [(row["id"], action, cw) for row, action, cw in classified]


# --------------------------------------------------------------------------- #
# parse_import_body / validate_import_row — boundary validation
# --------------------------------------------------------------------------- #

def test_parse_import_body_happy_path_slugifies():
    slug, rows, error = core.parse_import_body({
        "metadata": {"name": "My-Pack v2!"},
        "concepts": [{"id": "Widget Rate", "name": "Widget", "description": "d"}],
    })
    assert error == ""
    assert slug == "my_pack_v2"
    assert rows == [{"id": "widget_rate", "name": "Widget", "description": "d"}]


@pytest.mark.parametrize("body", [
    {},                                                        # nothing
    {"metadata": {}, "concepts": [{"id": "a", "name": "A"}]},  # no metadata.name
    {"metadata": {"name": "P"}, "concepts": []},               # empty concepts
    {"metadata": {"name": "P"}},                               # concepts absent
    {"metadata": {"name": "!!!"}, "concepts": [{"id": "a", "name": "A"}]},  # empty slug
    {"metadata": {"name": "P"}, "concepts": [{"id": "!!!", "name": "A"}]},  # bad id
    {"metadata": {"name": "P"}, "concepts": [{"id": "a"}]},    # row missing name
])
def test_parse_import_body_bad_shapes_error(body):
    slug, rows, error = core.parse_import_body(body)
    assert error and slug == "" and rows == []


def test_parse_import_body_batch_cap():
    concepts = [{"id": f"concept_{i}", "name": f"Concept {i}"} for i in range(501)]
    _, _, error = core.parse_import_body(
        {"metadata": {"name": "P"}, "concepts": concepts})
    assert "500" in error and "got 501" in error
    _, rows, error = core.parse_import_body(
        {"metadata": {"name": "P"}, "concepts": concepts[:500]})
    assert error == "" and len(rows) == 500


@pytest.mark.parametrize("row, expect", [
    ({"id": "a" * 129, "name": "Long Id"}, "128"),
    ({"id": "ok_id", "name": "n" * 257}, "256"),
    ({"id": "ok_id", "name": "Ok", "description": "d" * 4097}, "4096"),
])
def test_validate_import_row_length_caps(row, expect):
    parsed, error = core.validate_import_row(row)
    assert parsed == {} and expect in error


def test_validate_import_row_at_cap_ok():
    row = {"id": "a" * 128, "name": "n" * 256, "description": "d" * 4096}
    parsed, error = core.validate_import_row(row)
    assert error == "" and parsed["id"] == "a" * 128


def test_validate_import_row_messages_name_the_problem():
    _, err = core.validate_import_row({"name": "Orphan Row"})
    assert "missing 'id'" in err and "Orphan Row" in err
    _, err = core.validate_import_row({"id": "some_id"})
    assert "'name'" in err


# --------------------------------------------------------------------------- #
# Import purity — stdlib-only (stricter than the router's pin)
# --------------------------------------------------------------------------- #

def test_module_source_has_no_impure_imports():
    src = open(core.__file__).read()
    for needle in ("import boto3", "import pg8000", "import pgdriver", "import fastapi",
                   "from fastapi", "from .db", "from agents.db"):
        assert needle not in src, needle


def test_module_imports_with_boto3_and_fastapi_absent():
    """Fresh interpreter with boto3/botocore/pg8000/agents.pgdriver/fastapi BOMBED still
    imports the core module (subprocess fresh-interpreter convention — see
    test_vocabulary_routes.py). Stdlib-only means it must survive even a
    missing web framework — the onboarding runtime does not ship one."""
    code = (
        "import sys\n"
        "for _m in ('boto3', 'botocore', 'pg8000', 'agents.pgdriver', 'fastapi'):\n"
        "    sys.modules[_m] = None\n"
        "import agents.vocab_import_core as core\n"
        "out = core.preview_rows(\n"
        "    [{'id': 'widget_rate', 'name': 'Widget', 'description': ''}],\n"
        "    {'widget-rate'})\n"
        "assert out['collision_count'] == 1\n"
        "print('IMPORT-PURE-OK')\n"
    )
    adt_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(adt_root))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(adt_root),  # nosec B603 - fixed argv, no shell
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "IMPORT-PURE-OK" in r.stdout
