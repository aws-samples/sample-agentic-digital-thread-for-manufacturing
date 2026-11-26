#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
merge_golden_draft.py — land a downloaded golden DRAFT in an EXTERNAL golden set.

A promoted draft (downloaded from the Evals review queue —
``POST /api/admin/evals/queue/golden-draft``) is CUSTOMER-DERIVED data: its
``text`` is a real customer question. Its destination is therefore the
steward's own, customer-owned golden set — NEVER this repository (ADR-0062
D1/D5). This helper is the sanctioned landing path:

1. it validates the draft through ``agents/eval_core.py::load_golden_set``
   (the same loader ``scripts/eval_answers.py --golden-set`` uses, so a merge
   that validates here runs there);
2. it REFUSES — exit 2, with no write of any kind — any ``--golden-set`` whose
   RESOLVED path is beneath this repository root, including the sanctioned
   demo golden itself. ``Path.resolve()`` first, then
   ``os.path.commonpath``, so a ``..`` traversal or a symlink pointing back
   into the checkout is refused too;
3. it validates the MERGED document before it replaces the destination (write
   to a temp file in the DESTINATION directory, load it, then ``os.replace``),
   so a validation failure never leaves a half-written golden set behind.

Generic by design: it merges any golden-question-shaped payload — a full
``{version, questions: [...]}`` document (the download's shape) or a bare
question object — and it does not require the promoted provenance, so a
steward may edit the draft (write the real ``answer_must_match``, add
markings) before merging.

Exit codes: ``0`` merged, ``2`` usage/input/validation error or a refused
destination. There is no other code.

Usage:
  python3 scripts/merge_golden_draft.py --draft ~/Downloads/golden-draft-<sha>.json \\
      --golden-set ~/my-goldens/customer_golden.json
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def _import_eval_core():
    """Import ``agents.eval_core`` — the golden loader, stdlib-only.

    ``agents`` FIRST (the ``audit_answer.py`` idiom): the unit lane runs with
    ``agentic_digital_thread`` on ``sys.path``, and binding the same module
    instance the server imports keeps ONE home for the loader. The fallback
    covers ``python3 scripts/merge_golden_draft.py`` from anywhere.
    """
    try:
        from agents import eval_core
        return eval_core
    except ImportError:
        adt_dir = str(Path(__file__).resolve().parents[1]
                      / "agentic_digital_thread")
        if adt_dir not in sys.path:
            sys.path.insert(0, adt_dir)
        from agents import eval_core
        return eval_core


eval_core = _import_eval_core()

#: Markers that identify THIS repository's root (this file lives in scripts/).
REPO_MARKERS = ("agentic_digital_thread", "cdk.json")


def repo_root() -> Path:
    """The root of this repository — the nearest ancestor carrying the markers.

    Marker-based (not a fixed ``parents[1]``) so a relocated or vendored copy
    of this script still refuses the right tree; the ``parents[1]`` fallback
    keeps the refusal FAIL-CLOSED when the markers are absent.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if all((candidate / marker).exists() for marker in REPO_MARKERS):
            return candidate
    return here.parents[1]


def is_inside_repo(path: Path) -> bool:
    """True when ``path`` resolves to the repo root or anything beneath it.

    Resolution happens first (``strict=False`` — the destination need not
    exist yet), so ``..`` traversal and symlinks into the checkout collapse to
    their real location before the comparison.
    """
    root = repo_root()
    target = Path(path).expanduser().resolve()
    try:
        return os.path.commonpath([str(root), str(target)]) == str(root)
    except ValueError:  # different drives / unrelated roots (Windows)
        return False


def draft_questions(doc) -> list:
    """The question list inside a draft document (bundle or bare question).

    An empty list is an ERROR, not a no-op merge: a steward who ran this
    expects a question to land.
    """
    if isinstance(doc, dict) and isinstance(doc.get("questions"), list):
        if not doc["questions"]:
            raise ValueError("draft carries no questions")
        return doc["questions"]
    if isinstance(doc, dict):
        return [doc]
    raise ValueError("draft must be a JSON object")


def _load_json(path: Path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _destination_document(path: Path) -> dict:
    """The existing golden document, or a fresh empty one.

    An existing destination is loaded AND validated first: this helper never
    appends to a file it could not already run.
    """
    if not path.exists():
        return {"version": eval_core.GOLDEN_SET_VERSION, "questions": []}
    doc = _load_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("questions"), list):
        raise ValueError(f"{path} is not a golden-set document")
    eval_core.load_golden_set(str(path))
    return doc


def _merged_document(dest_doc: dict, questions: list) -> dict:
    """``dest_doc`` plus ``questions``; duplicate ids are a hard error."""
    merged = dict(dest_doc)
    existing = list(dest_doc.get("questions") or [])
    known = {question.get("id") for question in existing
             if isinstance(question, dict)}
    added = []
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("each draft question must be a JSON object")
        qid = question.get("id")
        if qid in known:
            raise ValueError(
                f"question id already present in the golden set: {qid}")
        known.add(qid)
        added.append(question)
    merged["questions"] = existing + added
    merged.setdefault("version", eval_core.GOLDEN_SET_VERSION)
    return merged


def write_validated(dest: Path, doc: dict) -> None:
    """Validate ``doc`` as a golden set, then atomically become ``dest``.

    The temp file lives in the DESTINATION directory (outside this repo — the
    refusal above already guaranteed that), so nothing is ever written inside
    the checkout, and a failed validation leaves ``dest`` untouched.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False,
        dir=str(dest.parent), prefix=f".{dest.name}.")
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(doc, handle, indent=1)
            handle.write("\n")
        eval_core.load_golden_set(str(temp_path))
        os.replace(str(temp_path), str(dest))
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge a downloaded golden draft into an EXTERNAL, "
                    "customer-owned golden set. Destinations inside this "
                    "repository are refused (ADR-0062).")
    parser.add_argument("--draft", required=True,
                        help="path to the downloaded draft JSON")
    parser.add_argument("--golden-set", required=True, dest="golden_set",
                        help="path to your external golden set (created when "
                             "it does not exist); must be OUTSIDE this "
                             "repository")
    return parser


def main(argv=None) -> int:
    """Merge one draft; 0 = merged, 2 = refused / invalid input. No other code."""
    args = build_parser().parse_args(argv)
    dest = Path(args.golden_set).expanduser()
    if is_inside_repo(dest):
        print("refused: --golden-set resolves inside this repository "
              f"({repo_root()}); customer golden sets live in your OWN "
              "external repo (ADR-0062). Nothing was written.", flush=True)
        return 2
    if not dest.parent.is_dir():
        print(f"input error: destination directory does not exist: "
              f"{dest.parent}", flush=True)
        return 2
    try:
        questions = draft_questions(_load_json(Path(args.draft).expanduser()))
        merged = _merged_document(_destination_document(dest), questions)
        write_validated(dest, merged)
    except (OSError, ValueError) as exc:
        print(f"input error: {exc}", flush=True)
        return 2
    print(f"== merged {len(questions)} question(s) into {dest} — "
          f"{len(merged['questions'])} total ==", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
