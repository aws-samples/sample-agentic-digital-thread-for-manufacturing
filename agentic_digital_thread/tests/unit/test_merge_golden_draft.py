# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/merge_golden_draft.py — the EXTERNAL-only landing path for a
downloaded golden draft (ADR-0062 D1/D5).

Offline. ``scripts/`` lives at the repo root (outside the
``agentic_digital_thread`` PYTHONPATH), so the repo root goes on ``sys.path``
and the module is imported as ``scripts.merge_golden_draft`` — the
``test_audit_answer.py`` mechanism.

Pins:

* a destination beneath THIS repository is REFUSED (exit 2) with no write —
  including ``agents/demo/golden_evals.json`` itself, a ``..`` traversal that
  resolves back into the checkout, and a symlink pointing at the checkout;
* an external destination merges, and the result revalidates through
  ``eval_core.load_golden_set`` (so ``eval_answers.py --golden-set`` can run
  it);
* a draft that does not validate, or a duplicate question id, is exit 2 with
  the destination byte-unchanged (no half-written golden set);
* exit codes are 0/2 only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import agents.eval_core as eval_core  # noqa: E402
import agents.eval_promote as ep  # noqa: E402
import agents.eval_queue as eq  # noqa: E402
from scripts import merge_golden_draft as mgd  # noqa: E402

SANCTIONED_GOLDEN = (_REPO_ROOT / "agentic_digital_thread" / "agents"
                     / "demo" / "golden_evals.json")


# ── fixtures ────────────────────────────────────────────────────────────────

def _draft(text: str = "why is the assembly line stopped?") -> dict:
    ref = eq.feedback_source_ref(
        "fb-" + hashlib.sha256(text.encode()).hexdigest()[:32])
    decision = {"source_ref": ref, "ref_sha": eq.ref_sha(ref)}
    return ep.draft_golden(decision, {"question": text})


def _draft_file(tmp_path: Path, doc=None, name="golden-draft.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc if doc is not None
                               else ep.draft_bundle(_draft())),
                    encoding="utf-8")
    return path


def _external_golden(tmp_path: Path, questions=None) -> Path:
    """A customer-owned golden set OUTSIDE this repository."""
    path = tmp_path / "external" / "customer_golden.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": eval_core.GOLDEN_SET_VERSION,
           "questions": questions if questions is not None else [{
               "id": "existing-1", "text": "existing question",
               "expect": {"systems_min": 1}}]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_state() -> dict:
    """sha of every file the refusal pins must leave untouched."""
    return {"golden": _sha(SANCTIONED_GOLDEN)}


# ── the refusal: nothing lands inside this repository ────────────────────────

def test_refuses_the_sanctioned_demo_golden_without_writing(tmp_path, capsys):
    """The ONE tracked golden is the most tempting destination — and the one
    the boundary gate exists to protect."""
    before = _repo_state()
    code = mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(SANCTIONED_GOLDEN)])
    assert code == 2
    assert "refused" in capsys.readouterr().out.lower()
    assert _repo_state() == before


@pytest.mark.parametrize("relative", [
    "agentic_digital_thread/agents/demo/new_golden.json",
    "agentic_digital_thread/tests/unit/customer_golden.json",
    "customer_golden.json",
    "docs/golden.json",
], ids=["demo-dir", "tests-dir", "repo-root", "docs"])
def test_refuses_any_destination_beneath_the_repo_root(tmp_path, relative,
                                                       capsys):
    dest = _REPO_ROOT / relative
    code = mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(dest)])
    assert code == 2
    assert "refused" in capsys.readouterr().out.lower()
    assert not dest.exists()


def test_refuses_a_dot_dot_traversal_back_into_the_repo(tmp_path, capsys):
    """The path LOOKS external (it starts under tmp) but resolves into the
    checkout — resolution happens before the comparison."""
    traversal = (tmp_path / os.path.relpath(_REPO_ROOT, tmp_path)
                 / "traversed_golden.json")
    assert ".." in str(traversal)
    code = mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(traversal)])
    assert code == 2
    assert "refused" in capsys.readouterr().out.lower()
    assert not (_REPO_ROOT / "traversed_golden.json").exists()


def test_refuses_a_symlink_that_points_into_the_repo(tmp_path, capsys):
    link = tmp_path / "looks-external"
    link.symlink_to(_REPO_ROOT, target_is_directory=True)
    before = _repo_state()
    dest = link / "agentic_digital_thread" / "agents" / "demo" / "golden_evals.json"
    code = mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(dest)])
    assert code == 2
    assert "refused" in capsys.readouterr().out.lower()
    assert _repo_state() == before
    assert not (_REPO_ROOT / "agentic_digital_thread" / "agents" / "demo"
                / "golden-draft.json").exists()


def test_repo_root_is_marker_based_and_covers_this_checkout():
    root = mgd.repo_root()
    assert root == _REPO_ROOT
    assert all((root / marker).exists() for marker in mgd.REPO_MARKERS)
    assert mgd.is_inside_repo(SANCTIONED_GOLDEN)
    assert mgd.is_inside_repo(root)


# ── the happy path: an external golden set ───────────────────────────────────

def test_merges_into_an_existing_external_golden_set(tmp_path, capsys):
    dest = _external_golden(tmp_path)
    draft = _draft()
    code = mgd.main(["--draft", str(_draft_file(tmp_path,
                                                ep.draft_bundle(draft))),
                     "--golden-set", str(dest)])
    assert code == 0
    assert "merged 1 question" in capsys.readouterr().out
    questions = eval_core.load_golden_set(str(dest))
    assert [question["id"] for question in questions] == ["existing-1",
                                                          draft["id"]]
    assert questions[1]["text"] == draft["text"]
    assert "answer_must_match" not in questions[1]["expect"]
    assert questions[1]["provenance"] == {"source": "promoted",
                                          "source_ref_sha": draft[
                                              "provenance"]["source_ref_sha"]}


def test_creates_a_new_external_golden_set_when_absent(tmp_path):
    dest = tmp_path / "external" / "fresh_golden.json"
    dest.parent.mkdir(parents=True)
    code = mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(dest)])
    assert code == 0
    assert len(eval_core.load_golden_set(str(dest))) == 1
    assert json.loads(dest.read_text(encoding="utf-8"))["version"] == \
        eval_core.GOLDEN_SET_VERSION


def test_accepts_a_bare_question_object_as_the_draft(tmp_path):
    """Generic: a steward may hand us the question alone (or an edited one with
    real expectations) — not only the download's bundle shape."""
    dest = _external_golden(tmp_path)
    draft = _draft("which orders are late?")
    draft["expect"]["answer_must_match"] = ["(?i)order"]
    code = mgd.main(["--draft", str(_draft_file(tmp_path, draft)),
                     "--golden-set", str(dest)])
    assert code == 0
    questions = eval_core.load_golden_set(str(dest))
    assert questions[1]["expect"]["answer_must_match"] == ["(?i)order"]


def test_no_temp_file_survives_a_successful_merge(tmp_path):
    dest = _external_golden(tmp_path)
    assert mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(dest)]) == 0
    assert sorted(path.name for path in dest.parent.iterdir()) == [dest.name]


# ── failures leave the destination byte-unchanged ────────────────────────────

def test_duplicate_question_id_is_refused_and_changes_nothing(tmp_path, capsys):
    draft = _draft()
    dest = _external_golden(tmp_path, questions=[draft])
    before = _sha(dest)
    code = mgd.main(["--draft", str(_draft_file(tmp_path,
                                                ep.draft_bundle(draft))),
                     "--golden-set", str(dest)])
    assert code == 2
    assert "already present" in capsys.readouterr().out
    assert _sha(dest) == before


@pytest.mark.parametrize("doc", [
    {"version": 1, "questions": [{"id": "x", "text": "t"}]},
    {"version": 1, "questions": [{"text": "no id", "expect": {}}]},
    {"version": 1, "questions": [{"id": "x", "expect": {}}]},
    {"version": 1, "questions": []},
    ["not", "an", "object"],
], ids=["no-expect", "no-id", "no-text", "empty", "not-an-object"])
def test_invalid_draft_is_refused_and_changes_nothing(tmp_path, doc, capsys):
    dest = _external_golden(tmp_path)
    before = _sha(dest)
    code = mgd.main(["--draft", str(_draft_file(tmp_path, doc)),
                     "--golden-set", str(dest)])
    assert code == 2
    assert "input error" in capsys.readouterr().out
    assert _sha(dest) == before


def test_unreadable_draft_and_missing_destination_directory_are_input_errors(
        tmp_path, capsys):
    dest = _external_golden(tmp_path)
    assert mgd.main(["--draft", str(tmp_path / "nope.json"),
                     "--golden-set", str(dest)]) == 2
    assert mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(tmp_path / "no" / "such" / "g.json")]) == 2
    assert "input error" in capsys.readouterr().out


def test_an_unloadable_destination_is_never_appended_to(tmp_path, capsys):
    dest = tmp_path / "external" / "broken.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{not json", encoding="utf-8")
    before = _sha(dest)
    assert mgd.main(["--draft", str(_draft_file(tmp_path)),
                     "--golden-set", str(dest)]) == 2
    assert _sha(dest) == before


# ── CLI surface ─────────────────────────────────────────────────────────────

def test_parser_requires_both_paths():
    parser = mgd.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--draft", "d.json"])
    args = parser.parse_args(["--draft", "d.json", "--golden-set", "g.json"])
    assert (args.draft, args.golden_set) == ("d.json", "g.json")


def test_only_exit_codes_zero_and_two_exist():
    """Annotate-or-refuse: there is no third outcome (and no exit 1)."""
    source = Path(mgd.__file__).read_text(encoding="utf-8")
    returns = {line.strip() for line in source.splitlines()
               if line.strip().startswith("return ")
               and line.strip()[7:].strip().isdigit()}
    assert returns == {"return 0", "return 2"}
