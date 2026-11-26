# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The eval_core golden parse seam — ONE validator home, ONE sha home.

Offline, stdlib-only. ``agents/eval_core.py`` now splits the loader into
``parse_golden_set(payload)`` (all validation/normalisation) and a thin
``load_golden_set(path)`` = read bytes → delegate, and ``golden_file_sha(path)``
delegates to ``golden_bytes_sha(data)``. Pins here:

* parse/load EQUIVALENCE — the same fixture parses to the same questions from
  the file, from its bytes and from its parsed dict, so a golden set that
  parses server-side runs on the CLI BY CONSTRUCTION (ADR-0061 D2: the file
  schema stays the sole interchange format);
* ``golden_bytes_sha(Path(p).read_bytes()) == golden_file_sha(p)`` — the sha
  binds the EXACT bytes the steward holds, whether or not they touched a disk;
* a malformed payload raises the SAME error type and the SAME message through
  BOTH entry points (a server-side refusal is reproducible on the CLI);
* BOM / non-UTF-8 bytes are rejected HONESTLY — never stripped, never
  re-encoded (either would change the bytes the sha binds) — and the refusal
  surfaces the computed sha so the steward can compare it against their file;
* ``parse_golden_set`` performs NO filesystem access (seam spies over every
  read primitive + an AST scan of the function bodies);
* ``load_golden_set``/``golden_file_sha`` stay the SAME objects
  ``scripts/eval_answers.py`` re-exports (the CLI lane is unchanged).
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _doc(**overrides) -> dict:
    """A minimal VALID golden set exercising the markings validator too."""
    doc = {
        "version": eval_core.GOLDEN_SET_VERSION,
        "questions": [
            {"id": "q-one", "text": "first question",
             "expect": {"systems_min": 1, "answer_must_match": ["alpha"]},
             "improvable": True, "leak_class": "fact",
             "treatment_phase": "query-time"},
            {"id": "q-two", "text": "second question", "expect": {},
             "improvable": False},
        ],
    }
    doc.update(overrides)
    return doc


def _write(tmp_path: Path, raw: bytes, name: str = "golden.json") -> Path:
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def _raised(call) -> tuple[type, str]:
    """(exception type, message) for a call that must raise."""
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - shape is the pin
        call()
    return type(excinfo.value), str(excinfo.value)


# ── parse/load equivalence ───────────────────────────────────────────────────

def test_parse_and_load_agree_over_bytes_text_dict_and_file(tmp_path):
    doc = _doc()
    raw = json.dumps(doc).encode("utf-8")
    path = _write(tmp_path, raw)
    from_file = eval_core.load_golden_set(str(path))
    assert from_file == eval_core.parse_golden_set(raw)
    assert from_file == eval_core.parse_golden_set(bytearray(raw))
    assert from_file == eval_core.parse_golden_set(raw.decode("utf-8"))
    assert from_file == eval_core.parse_golden_set(doc)
    assert [q["id"] for q in from_file] == ["q-one", "q-two"]


def test_packaged_demo_golden_parses_identically_from_bytes_and_path():
    path = eval_core.default_golden_set_path()
    raw = Path(path).read_bytes()
    assert eval_core.load_golden_set(path) == eval_core.parse_golden_set(raw)


def test_parse_golden_set_returns_the_questions_it_was_given_unmutated():
    doc = _doc()
    before = json.dumps(doc, sort_keys=True)
    questions = eval_core.parse_golden_set(doc)
    assert questions is doc["questions"]
    assert json.dumps(doc, sort_keys=True) == before


# ── the sha binds the exact bytes ────────────────────────────────────────────

def test_golden_bytes_sha_matches_golden_file_sha_for_the_same_file(tmp_path):
    raw = json.dumps(_doc()).encode("utf-8")
    path = _write(tmp_path, raw)
    expected = hashlib.sha256(raw).hexdigest()[:12]
    assert eval_core.golden_bytes_sha(raw) == expected
    assert eval_core.golden_file_sha(str(path)) == expected
    assert eval_core.golden_bytes_sha(Path(path).read_bytes()) == \
        eval_core.golden_file_sha(str(path))


def test_packaged_golden_sha_agrees_between_bytes_and_path():
    path = eval_core.default_golden_set_path()
    assert eval_core.golden_bytes_sha(Path(path).read_bytes()) == \
        eval_core.golden_file_sha(path)


def test_golden_file_sha_still_returns_none_for_an_unreadable_path(tmp_path):
    # Pre-existing contract (test_eval_answers_report.py pins it too): the
    # delegation must not turn an unreadable path into an exception.
    assert eval_core.golden_file_sha(str(tmp_path / "missing.json")) is None


def test_a_reserialized_payload_does_not_reproduce_the_file_sha(tmp_path):
    # WHY the upload body must be the RAW bytes, not a re-serialized object.
    raw = json.dumps(_doc(), indent=2).encode("utf-8")
    path = _write(tmp_path, raw)
    reserialized = json.dumps(json.loads(raw)).encode("utf-8")
    assert eval_core.golden_bytes_sha(reserialized) != \
        eval_core.golden_file_sha(str(path))


# ── malformed payloads: identical refusal through both entry points ──────────

_MALFORMED = (
    ("bad_version", _doc(version=eval_core.GOLDEN_SET_VERSION + 1)),
    ("not_a_mapping", ["version", 1]),
    ("no_questions_key", {"version": eval_core.GOLDEN_SET_VERSION}),
    ("empty_questions", _doc(questions=[])),
    ("questions_not_a_list", _doc(questions={"id": "q"})),
    ("require_markings_not_bool", _doc(require_markings="yes")),
    ("missing_text", {"version": eval_core.GOLDEN_SET_VERSION, "questions": [
        {"id": "q-one", "expect": {}}]}),
    ("expect_not_a_dict", {"version": eval_core.GOLDEN_SET_VERSION,
                           "questions": [{"id": "q", "text": "t",
                                          "expect": []}]}),
    ("duplicate_ids", _doc(questions=[
        {"id": "dup", "text": "a", "expect": {}, "improvable": False},
        {"id": "dup", "text": "b", "expect": {}, "improvable": False}])),
    ("bad_leak_class", _doc(questions=[
        {"id": "q", "text": "t", "expect": {}, "leak_class": "nope"}])),
    ("improvable_without_phase", _doc(questions=[
        {"id": "q", "text": "t", "expect": {}, "leak_class": "fact"}])),
    ("require_markings_unmarked", _doc(
        require_markings=True,
        questions=[{"id": "q", "text": "t", "expect": {}}])),
)


@pytest.mark.parametrize("label,doc", _MALFORMED, ids=[c[0] for c in _MALFORMED])
def test_malformed_raises_the_same_error_from_both_entry_points(
        tmp_path, label, doc):
    raw = json.dumps(doc).encode("utf-8")
    path = _write(tmp_path, raw, f"{label}.json")
    from_file = _raised(lambda: eval_core.load_golden_set(str(path)))
    assert from_file == _raised(lambda: eval_core.parse_golden_set(raw))
    assert from_file == _raised(lambda: eval_core.parse_golden_set(doc))
    assert from_file[0] is ValueError, from_file


def test_unparseable_json_raises_the_same_decode_error_both_ways(tmp_path):
    raw = b'{"version": 1, "questions": [oops]}'
    path = _write(tmp_path, raw, "broken.json")
    from_file = _raised(lambda: eval_core.load_golden_set(str(path)))
    assert from_file == _raised(lambda: eval_core.parse_golden_set(raw))
    assert from_file[0] is json.JSONDecodeError
    assert issubclass(from_file[0], ValueError)


def test_load_golden_set_still_raises_oserror_for_a_missing_path(tmp_path):
    # The wrapper delegates VALIDATION only — filesystem errors stay OSError
    # (callers distinguish "no golden set" from "malformed golden set").
    with pytest.raises(OSError):
        eval_core.load_golden_set(str(tmp_path / "nope.json"))


# ── BOM / non-UTF-8: rejected honestly, with the sha surfaced ────────────────

def test_bom_is_rejected_not_stripped_and_the_sha_is_surfaced(tmp_path):
    raw = b"\xef\xbb\xbf" + json.dumps(_doc()).encode("utf-8")
    path = _write(tmp_path, raw, "bom.json")
    sha = eval_core.golden_bytes_sha(raw)
    for entry in (lambda: eval_core.load_golden_set(str(path)),
                  lambda: eval_core.parse_golden_set(raw)):
        kind, message = _raised(entry)
        assert kind is ValueError, message
        assert "BOM" in message
        assert sha in message
    # The sha of the file on disk is the sha the refusal reports — the steward
    # can compare it directly, and it is NOT the sha of the stripped bytes.
    assert eval_core.golden_file_sha(str(path)) == sha
    assert eval_core.golden_bytes_sha(raw[3:]) != sha


def test_non_utf8_bytes_are_rejected_with_the_sha_and_never_re_encoded(tmp_path):
    raw = json.dumps(_doc()).encode("utf-8").replace(b"first", b"fi\xffrst")
    path = _write(tmp_path, raw, "latin1.json")
    sha = eval_core.golden_bytes_sha(raw)
    for entry in (lambda: eval_core.load_golden_set(str(path)),
                  lambda: eval_core.parse_golden_set(raw)):
        kind, message = _raised(entry)
        assert kind is ValueError, message
        assert "not valid UTF-8" in message
        assert sha in message
    # A lenient re-encode would have "worked" and produced a DIFFERENT sha —
    # that divergence is exactly what the explicit refusal prevents.
    assert eval_core.golden_bytes_sha(
        raw.decode("latin-1").encode("utf-8")) != sha


# ── parse_golden_set touches no filesystem ──────────────────────────────────

def test_parse_golden_set_performs_no_filesystem_access(monkeypatch):
    """Seam spies over every read primitive the module could reach."""
    def _boom(*args, **kwargs):  # pragma: no cover - must never fire
        raise AssertionError("parse_golden_set touched the filesystem")

    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(io, "open", _boom)
    monkeypatch.setattr(os, "open", _boom)
    monkeypatch.setattr(Path, "open", _boom)
    monkeypatch.setattr(Path, "read_bytes", _boom)
    monkeypatch.setattr(Path, "read_text", _boom)
    raw = json.dumps(_doc()).encode("utf-8")
    assert [q["id"] for q in eval_core.parse_golden_set(raw)] == \
        ["q-one", "q-two"]
    assert eval_core.golden_bytes_sha(raw)
    # And the wrapper is the ONLY reader: with the spies installed it fails.
    with pytest.raises(AssertionError):
        eval_core.load_golden_set("whatever.json")


def _function_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(Path(eval_core.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in eval_core")


@pytest.mark.parametrize("name", ["parse_golden_set", "_golden_text",
                                  "golden_bytes_sha"])
def test_parse_seam_source_names_no_read_primitive(name):
    """AST pin: the parse path cannot acquire a filesystem home later."""
    forbidden = {"open", "read_bytes", "read_text", "Path", "load"}
    seen = set()
    for node in ast.walk(_function_node(name)):
        if isinstance(node, ast.Call):
            func = node.func
            label = getattr(func, "id", None) or getattr(func, "attr", None)
            if label in forbidden:
                seen.add(label)
    assert not seen, f"{name} reaches a filesystem primitive: {seen}"


@pytest.mark.parametrize("name", ["parse_golden_set", "load_golden_set",
                                  "_golden_text", "golden_bytes_sha",
                                  "golden_file_sha"])
def test_seam_functions_stay_under_the_50_line_gate(name):
    node = _function_node(name)
    span = int(node.end_lineno or node.lineno) - node.lineno + 1
    assert span <= 50, f"{name} is {span} lines"


# ── the CLI re-export still resolves to the same objects ────────────────────

def test_cli_reexports_resolve_to_the_same_loader_and_sha():
    from scripts import eval_answers  # noqa: PLC0415 - repo-root script

    # Identity is pinned against the module object the CLI itself re-exports
    # from (test_eval_core_extraction.py's convention: the script-context
    # fallback can load eval_core under a second module name).
    cli_core = eval_answers.eval_core
    assert Path(cli_core.__file__) == Path(eval_core.__file__)
    assert eval_answers.load_golden_set is cli_core.load_golden_set
    assert eval_answers.golden_file_sha is cli_core.golden_file_sha
    assert eval_answers._validate_golden_markings is \
        cli_core._validate_golden_markings
    # The new seam is reachable through the same module the CLI holds.
    assert callable(cli_core.parse_golden_set)
    assert callable(cli_core.golden_bytes_sha)
    path = eval_answers.DEFAULT_GOLDEN_SET
    assert eval_answers.load_golden_set(path) == \
        cli_core.parse_golden_set(Path(path).read_bytes())
    assert eval_answers.golden_file_sha(path) == \
        cli_core.golden_bytes_sha(Path(path).read_bytes())
