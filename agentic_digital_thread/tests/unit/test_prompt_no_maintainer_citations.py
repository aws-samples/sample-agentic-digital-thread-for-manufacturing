# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The model-facing prompts carry no maintainer citations.

ADR numbers, merge-request ids and review rounds explain to maintainers why a rule
exists; the model cannot open them, so in a prompt they only spend tokens. They
belong in code comments and docs. This reads what the models actually receive:
all four composed prompts (full and AWS Quick, both query modes), the
onboarding agent's prompt, every ``@tool`` docstring under agents/ (the tool
description), the concept definitions block onboarding's field mapping sends,
the graph metamodel the orchestrator appends to its prompt, the JSON schema of
the orchestrator's structured answer (both ``FinalAnswerDraft`` and the system
agents' ``SystemAnswerDraft``), and the generated AWS Quick skill.

The forbidden set also covers the history of OUR review process — finding ids
(``HIGH-1``/``MED-2``/``LOW-3``), review rounds, and ``re-review`` — which carry
even less for the model than an ADR number does.

A final tripwire scans EVERY string literal under ``agents/`` (ast walk,
docstrings included) for review-process citations, guarding against new residue
landing in a string the model might one day receive. Each current non-model hit
is named in ``_REVIEW_HISTORY_ALLOWLIST`` with its reason; that allowlist should
only ever SHRINK — a new entry means new residue, not a new exception.

A repository scan applies the same review-process forms to the shipped non-test
source and docs line by line, comments included, since the repository itself is
shared. The steward's ``re-review`` product wording is named line by line;
both of its allowlists only shrink.
"""

import ast
import base64
import functools
import json
import os
import pathlib
import re
import shutil
import subprocess  # nosec B404 - fixed argv list, no shell, internal inputs only

import pytest

import agents.orchestrator as orch
from agents.workflow_guidance import compose_quick_system_prompt

_CITATION = re.compile(
    r"\bADR[- ]?\d{2,4}\b|\bMR\s*[!#]\d+|(?-i:\bMR)\s+\d{2,4}\b|\breview\s*#\d+"
    r"|\breview[ -]+round\s*#?\s*\d+"
    r"|\breview\s+r\d+\b"
    # the history of OUR review process:
    r"|\b(?:HIGH|MED|MEDIUM|LOW)-\d+\b"
    r"|\breview\s+(?:fix(?:es)?|finding|follow-?up|round)s?\b"
    r"|\badversarial[- ]review\b"
    r"|\bF\d+\s*\((?:HIGH|MED|MEDIUM|LOW)\b"
    r"|\bround\s+\d+\s+Task\b"
    r"|\bre-review\b",
    re.I)

# The regex itself is pinned: each citation form must match, and the prompts' own
# uses of "review" (steward review, human review) must not.
_MUST_MATCH = ("see ADR-0035", "ADR 87", "per MR !123", "MR #99", "per MR 123", "review #5",
               "review round 7", "review-round 3", "review round #2", "review r1",
               "(review r2, 2001-02-03)",
               # review-process history forms:
               "HIGH-1", "MED-2", "MEDIUM-10", "LOW-3", "the review fix landed",
               "review finding F2", "review follow-up", "review followup",
               "round 2 Task D", "sent back for re-review",
               "since the adversarial-review F1 fix", "adversarial review notes",
               "review fixes", "two review findings", "review follow-ups",
               "review rounds", "F1 (HIGH)", "F6/F7 (LOW, skipped)", "F3 (MEDIUM)")
_MUST_NOT_MATCH = ("a steward must review it", "queued for human review",
                   "review the 3 open orders", "steward review queue",
                   "review rows before accepting", "review rules in the queue",
                   # ordinary review wording that must stay allowed:
                   "the independent reviewer's calibrated verdict",
                   "code review covers those cases", "the reviewer still decides",
                   "an adversarial input is rejected", "press F5 (refresh)",
                   "the steward reviews findings in the queue",
                   "Mr 99 Street is the site address", "the MR10 sensor", "mr 99")


@pytest.mark.parametrize("sample", _MUST_MATCH)
def test_citation_pattern_catches(sample):
    assert _CITATION.search(sample)


@pytest.mark.parametrize("sample", _MUST_NOT_MATCH)
def test_citation_pattern_ignores_ordinary_review_wording(sample):
    assert not _CITATION.search(sample)


def _prompts():
    for mode in ("agent", "orchestrator"):
        yield f"full-{mode}", orch._build_orchestrator_prompt(mode)
        yield f"quick-{mode}", compose_quick_system_prompt(mode)


@pytest.mark.parametrize("name,text", list(_prompts()), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "")
def test_composed_prompt_has_no_maintainer_citations(name, text):
    hits = [ln.strip() for ln in text.splitlines() if _CITATION.search(ln)]
    assert not hits, f"{name} cites maintainer docs the model cannot read: {hits}"


_AGENTS_DIR = pathlib.Path(orch.__file__).resolve().parent


def _decorator_names(node) -> set:
    names = set()
    for d in node.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        names.add(getattr(target, "id", None) or getattr(target, "attr", None))
    return names


def _tool_docstrings():
    """Every ``@tool`` docstring under agents/: Strands sends it to the model as
    the tool's description. Read statically, so modules that need cloud
    clients at import are covered too."""
    for path in sorted(_AGENTS_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and "tool" in _decorator_names(node)):
                yield f"{path.relative_to(_AGENTS_DIR)}::{node.name}", ast.get_docstring(node) or ""


_TOOL_DOCS = list(_tool_docstrings())


def test_tool_scan_reaches_the_onboarding_tools():
    names = {name.split("::")[1] for name, _ in _TOOL_DOCS}
    assert {"connect_new_system", "assess_vocabulary_fit"} <= names


def test_tool_descriptions_have_no_maintainer_citations():
    hits = [f"{name}: {ln.strip()}" for name, doc in _TOOL_DOCS
            for ln in doc.splitlines() if _CITATION.search(ln)]
    assert not hits, f"tool descriptions cite maintainer docs the model cannot read: {hits}"


def test_onboarding_agent_prompt_has_no_maintainer_citations():
    import agents.onboard_agent as oa
    hits = [ln.strip() for ln in oa.ONBOARD_AGENT_PROMPT.splitlines() if _CITATION.search(ln)]
    assert not hits, f"onboarding prompt cites maintainer docs the model cannot read: {hits}"


def _citation_lines(text: str) -> list:
    return [ln.strip() for ln in text.splitlines() if _CITATION.search(ln)]


def test_concept_definitions_block_has_no_maintainer_citations(monkeypatch):
    """The CONCEPT DEFINITIONS block field mapping puts into its prompt; no
    overlays, so it is the curated core definitions only."""
    import agents.catalog as catalog
    import agents.db as db
    monkeypatch.setattr(db, "neptune_query", lambda *a, **k: [])
    text = catalog.concept_hints_block()
    assert "oee-score" in text
    hits = _citation_lines(text)
    assert not hits, f"concept definitions cite maintainer docs the model cannot read: {hits}"


def test_graph_metamodel_has_no_maintainer_citations(monkeypatch):
    """The metamodel section the orchestrator appends to its prompt, rendered by
    the real fetch path from exactly what ``write_metamodel`` stores."""
    import agents.metamodel as mm

    def _query(cypher, params=None):
        if "MATCH (mn:MetaNode)" in cypher:
            return [dict(n) for n in mm.META_NODES]
        if "MATCH (me:MetaEdge)" in cypher:
            return [dict(e) for e in mm.META_EDGES]
        return []

    monkeypatch.setattr(mm, "neptune_query", _query)
    text = mm.fetch_metamodel_text()
    assert "AuthorityRule" in text and "LINKS_CONCEPT" in text
    hits = _citation_lines(text)
    assert not hits, f"graph metamodel cites maintainer docs the model cannot read: {hits}"


def test_structured_answer_schema_has_no_maintainer_citations():
    """The orchestrator streams with ``structured_output_model=FinalAnswerDraft``
    and the system agents with ``SystemAnswerDraft``; the field descriptions of
    BOTH reach the model through their JSON schemas."""
    from agents.response_integrity_contracts import FinalAnswerDraft, SystemAnswerDraft
    final = json.dumps(FinalAnswerDraft.model_json_schema(), indent=1)
    assert "proposed_concept_id" in final
    system = json.dumps(SystemAnswerDraft.model_json_schema(), indent=1)
    hits = _citation_lines(final) + _citation_lines(system)
    assert not hits, f"answer schema cites maintainer docs the model cannot read: {hits}"


def test_quick_skill_has_no_maintainer_citations():
    """The generated AWS Quick skill (docs/amazon-quick-skill.md) is loaded by
    Quick's model as skill text."""
    from agents.workflow_guidance import render_quick_skill_markdown
    hits = _citation_lines(render_quick_skill_markdown())
    assert not hits, f"Quick skill cites maintainer docs the model cannot read: {hits}"


# ---------------------------------------------------------------------------
# Permanent tripwire: no review-process history in ANY string literal under
# agents/. The checks above cover the surfaces the model is KNOWN to receive;
# this one is the backstop — a finding id or review round in any agents/ string
# literal (a prompt fragment, a tool description, a field description assembled
# at runtime) is residue that could reach the model. It scans review-process
# forms only (finding ids, review rounds, re-review, MR numbers) — not bare ADR
# numbers, which live legitimately in developer docstrings the model never
# sees. An MR number points at an internal tracker the reader cannot open.
# Every current hit is a non-model developer string named below; the
# allowlist should only ever SHRINK.
_REVIEW_HISTORY = re.compile(
    r"\b(?:HIGH|MED|MEDIUM|LOW)-\d+\b"
    r"|\breview\s+(?:fix(?:es)?|finding|follow-?up|round)s?\b"
    r"|\badversarial[- ]review\b"
    r"|\bF\d+\s*\((?:HIGH|MED|MEDIUM|LOW)\b"
    r"|\breview\s*#\d+|\breview[ -]+round\s*#?\s*\d+|\breview\s+r\d+\b"
    r"|\bround\s+\d+\s+Task\b"
    r"|\bre-review\b"
    # internal plan ids and review-round reviewer references:
    r"|\bplan\s+task\b"
    r"|\br\d+\s+reviewer\b"
    r"|\br\d+\s+(?:FINDING|HIGH|MED|MEDIUM|LOW|version|reproduction)\b"
    # a severity-letter finding id after "review" or "reviewer" (``review M2``,
    # ``reviewer H-1``); the letter is case-sensitive so ``review EC2`` /
    # ``review s3`` stay clean:
    r"|\breview(?:er)?\s+(?-i:[CHML]-?\d+)\b"
    # numbered review defects / items, with or without punctuation before the
    # number (``review defect 4``, ``Review items (2001-``, ``review item, 2001-``):
    r"|\breview(?:er)?\s+(?:defect|item)s?\W{0,3}\d"
    # a hyphen-numbered review round (``Review-2 hardening``):
    r"|\breview-\d+\b"
    # the compact round-task tag (``R2-A`` = "round 2 Task A"), case-sensitive:
    r"|(?-i:\bR\d-[A-Z]\b)"
    # review-loop iterations, any separator, singular or plural; a test file
    # NAME such as ``test_response_integrity_iteration5`` has no word boundary
    # before ``iteration`` and so is not matched:
    r"|\biterations?[ _-]*\d+\b"
    # an MR number named as one: ``MR !123``, ``MR #123``, and, case-sensitive,
    # ``MRs !123`` and the number-only ``MR 123`` (2-4 digits after a space, so
    # ``MR10`` and ``Mr 99`` stay clean); ``merge request 123`` with or without
    # the ``!``. A bare ``!123`` is ``_BARE_MR_NUMBER``:
    r"|\bMR\s*[!#]\d+|(?-i:\bMRs?)(?:\s*[!#]\s*|\s+)\d{2,4}\b"
    r"|\bmerge\s+requests?\s*(?:[!#]\s*\d+|\s\d{2,4}\b)"
    # a numbered review round named with its finding, severity or step
    # (``round 2, finding 1``, ``MED (round 4)``, ``the round 1 fix``, ``the
    # round 2 advisory``, ``as round 3 wrote it``, ``Round 3's follow-up``), a
    # possessive round (``round 2's notes``), any HYPHEN-numbered round
    # (``round-1's advisory``, ``the round-2 rules``), and a section header that
    # opens with one (``── Round 2: ...``). The severity word is case-sensitive
    # so ``stays low (round to 2 places)`` is clean; the i3X project-phase label
    # (``i3X round-2 types``, ``pre-round-2 graphs``) is a project-phase label:
    r"|\bround[ -]?\d+\s*,\s*(?:finding|item|defect)s?\b"
    r"|(?-i:\b(?:HIGH|MED|MEDIUM|LOW))\s*\(\s*round\b"
    r"|\bround[ -]?\d+(?:'s)?\s+(?:\S+\s+)?"
    r"(?:fix(?:es)?|walk|follow-?up|findings?|reproduction|wrote|advisor(?:y|ies)"
    r"|substitution|baseline|contract|injection|rules?)\b"
    r"|\bround\s+\d+'s\b"
    r"|(?<!i3x )(?<!pre-)\bround-\d+\b"
    r"|──\s*round[ -]?\d+\b"
    # a compact finding id after the word, in the three shapes review notes
    # use (``finding F6``, ``finding R3-4``, ``findings #1``). Only those
    # shapes: ``finding EC2``, ``finding S3``, ``finding L3``, ``finding
    # ISO9001``, ``finding F1 scores`` and a part number such as ``finding
    # R12-34-B7`` are ordinary prose:
    r"|\bfindings?\s+(?:(?-i:F\d+)(?!\s*-?\s*scores?\b)|(?-i:R\d+(?:-\d+)+)|#\d+)(?![\w-])",
    re.I)

# An MR number written bare (``the !123 fix``, ``!123 review``, ``REGRESSION
# (!123)``). In code and data ``!10`` is ordinary (a negated number in
# JavaScript, a value in JSON or YAML, an argument in shell), so a ``!`` before
# 2-4 digits counts only in PROSE context, the same rule in every file:
#   - a prose word directly before it (``_PROSE_BEFORE``: words that never
#     come right before a negated number in working code),
#   - a review-process word directly after it (``_PROCESS_AFTER``), not inside
#     a token (``abc!123``, ``a/!123``, ``!!10``), or
#   - an all-caps label and an opening parenthesis before it.
# A bare number with none of these around it (``(!123)`` alone) is not caught;
# a named one (``MR !123``, ``MR 123``) always is.
_PROSE_BEFORE = ("the", "a", "an", "of", "in", "on", "at", "with", "from", "by", "per", "see",
                 "via", "after", "before", "since", "and", "or", "to", "into", "landed",
                 "merged", "reverted", "fixed", "draft")
_PROCESS_AFTER = ("review", "reviews", "reviewed", "fix", "fixes", "live", "live-check",
                  "merge", "merged", "landed", "regression", "rehearsal", "round", "follow-up",
                  "followup", "revision", "head", "branch", "pipeline", "description", "thread")
_BARE_MR_NUMBER = re.compile(
    r"\b(?:" + "|".join(_PROSE_BEFORE) + r")\s+![1-9]\d{1,3}\b"
    r"|(?<![\w/!])![1-9]\d{1,3}\s+(?:" + "|".join(map(re.escape, _PROCESS_AFTER)) + r")\b"
    r"|(?-i:\b[A-Z]{3,})\s+\(\s*![1-9]\d{1,3}\b",
    re.I)

# The nicknames of our own test accounts mean nothing to a reader; write "a
# rehearsal account" instead. A nickname has one of two shapes, and neither is
# spelled out here, so this file never carries one:
#   - a two-letter initialism joined to ``demo`` (``xx-demo``, ``xxdemo``, any
#     case), matched by shape. It is not caught inside a hyphenated slug
#     (``...-routing-in-demo-layer.md``) or a longer word (``react-demo``).
#   - an ordinary word used as the account's name before ``rehearsal``. Those
#     words are kept base64-encoded in ``_NICKNAME_WORDS`` and decoded here.
# The AWS Organizations term for the accounts in an organization stays clean.
_NICKNAME_WORDS = tuple(base64.b64decode(w).decode("ascii") for w in ("bWVtYmVy",))
_ACCOUNT_NICKNAME = re.compile(
    r"(?<![\w-])[a-z]{2}-?demo\b"
    r"|\b(?:" + "|".join(map(re.escape, _NICKNAME_WORDS)) + r")\s+rehearsal\b",
    re.I)
_NICKNAME_WORD = _NICKNAME_WORDS[0]

# The same nickname word used as an account's name in dated live evidence:
# right before a date (``<word>, 2001-02-03``, ``<word> live run 2001-02-03``),
# right after one (``the 2001-02-03 <word> walk``), as ``<word> run`` /
# ``<word> walk``, inside a run id, as the possessive ``<word> account's``,
# and as the singular ``<word> account`` with evidence tied to it (a date on
# the line, or an evidence phrase right before or after the account phrase:
# ``observed on the <word> account``, ``<word> account, live run``). The
# singular without that evidence is the AWS Organizations
# term for an account in an organization (``the <word> account is managed by
# AWS Organizations``) and stays clean, as do the plural and the word's
# ordinary programming senses.
_NW = "(?:" + "|".join(map(re.escape, _NICKNAME_WORDS)) + ")"
_DATE = r"\d{4}-\d{2}-\d{2}"
_NICKNAME_CONTEXT = re.compile(
    rf"\b{_NW}\b[\s,:;(]{{0,3}}(?:(?:live\s+)?(?:run|walk|rehearsal)\b[\s,:;(]{{0,3}})?{_DATE}"
    rf"|\b{_DATE}\W{{0,3}}{_NW}\b"
    rf"|\b{_NW}\s+account['’]s\b"
    rf"|\b{_NW}\s+(?:live\s+)?(?:run|walk)\b"
    rf"|(?<![A-Za-z]){_NW}-[0-9a-f]{{8}}\b",
    re.I)
_NICKNAME_SINGULAR_ACCOUNT = re.compile(rf"\b{_NW}\s+account\b(?!s)", re.I)
# Evidence tied to the account phrase itself: a date anywhere on the line, or
# an evidence phrase right before it (``observed on the <word> account``,
# ``captured from the <word> account``) or right after it (``<word> account,
# live run``, ``<word> account (rehearsal``). A bare evidence word elsewhere
# on the line (``the <word> account runs live workloads``) is ordinary prose.
_EVIDENCE_WORDS = r"(?:rehearsal|live\s+(?:run|check|walk|test)|measured|observed|captured)"
_NICKNAME_EVIDENCE = re.compile(
    rf"{_DATE}"
    rf"|\b{_EVIDENCE_WORDS}\s+(?:on|in|from|against)\s+(?:the\s+|a\s+)?{_NW}\s+account\b"
    rf"|\b{_NW}\s+account\b[\s,:;(]{{1,3}}{_EVIDENCE_WORDS}\b",
    re.I)


def _dated_nickname(text: str) -> bool:
    """The nickname word used as an account's name in live evidence."""
    if not _NICKNAME_GATE.search(text):
        return False
    return bool(_NICKNAME_CONTEXT.search(text) or (
        _NICKNAME_SINGULAR_ACCOUNT.search(text) and _NICKNAME_EVIDENCE.search(text)))

# A private planning brief named as a source (``<BRIEF>-some-decision.md``,
# ``<brief>-x1/notes``). The word is split so this file never spells a brief's
# name; a workshop page slug (``7b-<brief>-the-plant-manager``) stays clean.
_BRIEF_WORD = "BRI" + "EF"
_PRIVATE_BRIEF = re.compile(
    r"(?<![\w-])(?-i:" + _BRIEF_WORD + r")-[A-Za-z0-9][A-Za-z0-9-]*"
    r"|(?<![\w-])" + _BRIEF_WORD.lower() + r"-[A-Za-z0-9-]+(?:\.md\b|/)",
    re.I)

# A development branch named in prose (``<feat>/some-change``, ``<fix>/x``, and
# a hyphenated ``<docs>/some-change`` that is not a path: no ``/``, ``.`` or
# word character follows it). The prefixes are split for the same reason.
# CI configuration may name branches; it is not scanned for this form.
_BRANCH_PREFIXES = ("fe" + "at", "fi" + "x")
_DOCS_PREFIX = "do" + "cs"
_BRANCH_NAME = re.compile(
    r"(?<![\w./-])(?:" + "|".join(_BRANCH_PREFIXES) + r")/[a-z0-9][a-z0-9-]*"
    r"|(?<![\w./-])" + _DOCS_PREFIX + r"/[a-z0-9]+(?:-[a-z0-9]+)+(?![\w./-])")
_BRANCH_SCAN_SKIP = (".gitlab-ci.yml",)

# A commit hash given as evidence. After an unambiguous commit word (``commit``,
# ``commits``, ``sha``, ``revision``, in any letter case) any 8 to 40 hex
# characters count, digits only or letters only, and a 7-character
# abbreviation counts when it holds a letter (seven digits alone read as a row
# count). After a word that also has other senses (``build``, ``merge``,
# ``head``, ``main``, ``app``, ``tree``) the 7-40 characters must hold a letter
# and a digit, so a count, a compact date or a word is not read as a commit.
# A hash backticked right after a backticked branch name also counts. A
# 64-character content digest is never matched.
_HEX = r"(?=[0-9a-f]{0,39}[a-f])(?=[0-9a-f]{0,39}[0-9])[0-9a-f]{7,40}"
_ANY_HEX = r"(?:[0-9a-f]{8,40}|(?=[0-9a-f]{0,6}[a-f])[0-9a-f]{7})"
_COMMIT_HASH = re.compile(
    r"\b(?i:commits?|sha|revision)[:\s]+`?" + _ANY_HEX + r"`?(?![\w-])"
    r"|\b(?:build|merge|HEAD|head|main|mainline|app|tree)[:\s]+`?" + _HEX + r"`?(?![\w-])"
    r"|`(?:" + "|".join(_BRANCH_PREFIXES + (_DOCS_PREFIX,)) + r")/[^`\s]+`\s+`" + _HEX + "`")


def _internal_coordinate(rel: str, line: str) -> bool:
    """An account nickname in dated evidence, a private brief, a branch name
    outside CI configuration, or a commit hash given as evidence."""
    if (_dated_nickname(line) or _COMMIT_HASH.search(line)
            or (_has_word(line, (_BRIEF_WORD.lower(),)) and _PRIVATE_BRIEF.search(line))):
        return True
    return (not rel.startswith(_BRANCH_SCAN_SKIP) and "/" in line
            and bool(_BRANCH_NAME.search(line)))


# A comment or docstring wraps where it wraps, so a nickname phrase can start at
# the end of one line and finish on the next (``... on the <word>`` /
# ``# rehearsal account, 2001-02-03``). Each pair of lines is also read joined,
# the second without its comment marker.
_CONTINUATION_MARKER = re.compile(r"^\s*(?:#:?|//+|/?\*+|--)?\s*")


def _wrapped_nickname(line: str, next_line: str) -> str:
    """The two lines joined when only the joined text names a nickname, else ''."""
    joined = line.rstrip() + " " + _CONTINUATION_MARKER.sub("", next_line, count=1)
    def named(text):
        return _ACCOUNT_NICKNAME.search(text) or _dated_nickname(text)
    if named(joined) and not named(line) and not named(next_line):
        return joined.strip()
    return ""


# (path under agents/, substring that appears in the hit, one-line reason).
# The only remaining hits are the steward-facing product term. This list only
# shrinks.
_REVIEW_HISTORY_ALLOWLIST = (
    ("self_learning.py", "steward re-review", "steward re-review is the product action name, not our review process"),
    ("server.py", "pending for steward re-review", "steward re-review is the product action name"),
)


def _string_literal_citation_hits():
    return list(_agents_string_literal_hits(_AGENTS_DIR))


@functools.lru_cache(maxsize=None)
def _agents_string_literal_hits(agents_dir):
    """The hits under *agents_dir*, parsed once per test process: the two
    tests below read the same unchanged tree, so they share one scan."""
    hits = []
    for path in sorted(agents_dir.rglob("*.py")):
        rel = str(path.relative_to(agents_dir))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for line in node.value.splitlines():
                    if _REVIEW_HISTORY.search(line) or _BARE_MR_NUMBER.search(line):
                        hits.append((rel, line.strip()))
    return tuple(hits)


def _is_allowlisted(rel: str, line: str) -> bool:
    return any(rel == a_rel and a_sub in line
               for a_rel, a_sub, _ in _REVIEW_HISTORY_ALLOWLIST)


def test_agents_string_literals_have_no_review_history():
    unlisted = [f"{rel}: {line}" for rel, line in _string_literal_citation_hits()
                if not _is_allowlisted(rel, line)]
    assert not unlisted, (
        "review-process history in an agents/ string literal (not allowlisted):\n"
        + "\n".join(unlisted))


def test_review_history_allowlist_only_shrinks():
    """Every allowlist entry must still match a real hit. A stale entry means the
    residue was cleaned (good) — remove the entry so the allowlist keeps shrinking.
    """
    hits = _string_literal_citation_hits()
    stale = [f"{rel} :: {sub}" for rel, sub, _ in _REVIEW_HISTORY_ALLOWLIST
             if not any(rel == h_rel and sub in h_line for h_rel, h_line in hits)]
    assert not stale, (
        "allowlist entries no longer match any hit — delete them (the allowlist "
        "only shrinks):\n" + "\n".join(stale))


# ---------------------------------------------------------------------------
# Repository residue: the same review-process forms in the shipped source,
# docs AND test trees — comments included — because the repository itself is
# shared, not only what reaches the model. Line-based text scan (no parsing),
# so it covers TypeScript, Markdown and shell as well as Python comments.
# This file is skipped: its sample strings are the forms it pins.
# ``re-review`` is also the steward product action ("Requeue for re-review");
# those uses are named one by one in ``_REPO_PRODUCT_ALLOWLIST`` rather than
# exempted wholesale, so a ``re-review`` anywhere else still fails. The only
# residue entries are ADR sections that record review iterations as part of the
# decision's evidence. Both lists only shrink. The same scan also refuses a
# rehearsal-account nickname (``_ACCOUNT_NICKNAME``). It reads every TRACKED
# file (``git ls-files``), so no
# shipped tree can be left out of a hand-kept list and untracked local output
# (CDK synth dirs holding copies of older source, logs, venvs) is never read.
# Outside a git checkout (an exported tree) it walks every file instead, minus
# generated and vendored trees. Every file type is read (SQL seeds, CSS, TOML,
# HTML, Dockerfiles and other extensionless files ship too) except an explicit
# skip policy: binary formats by suffix, and lock files, which hold no prose.
# A file outside that policy must decode as UTF-8 text (pinned below), so the
# decode-or-skip in the reader never hides one.
_REPO_ROOT = _AGENTS_DIR.parents[1]
_REPO_SCAN_SKIP = ("agentic_digital_thread/tests/unit/test_prompt_no_maintainer_citations.py",)
_REPO_SCAN_BINARY_SUFFIXES = (".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
                              ".woff", ".woff2", ".ttf", ".otf", ".eot",
                              ".zip", ".gz", ".tgz", ".whl", ".jar")
_REPO_SCAN_LOCK_SUFFIXES = (".lock",)
_REPO_SCAN_SKIP_PARTS = ("node_modules", "__pycache__", ".git", ".venv", "venv", ".kiro", "dist",
                         ".pytest_cache", ".mypy_cache", ".ruff_cache")
_REPO_SCAN_SKIP_NAMES = ("package-lock.json",)
# Known shipped files, one per kind of tree, that the scan must read.
_REPO_SCAN_SENTINELS = (
    "README.md", "docker-compose.local.yml", "config/dependency-license-policy.json",
    "agentic_digital_thread/agents/orchestrator.py",
    "agentic_digital_thread/frontend/src/test/fixtures/tool-result-outcome-cases.json",
    "agentic_digital_thread/tests/unit/test_metamodel.py",
    "erp_simulation/shipping_simulator.py", "docs/graph-shape-contract.md",
    "scripts/reonboard.py", "highbyte/components/opc-server/clock.mjs",
    "lambda-sitewise-feed/handler.py",
    # shipped text that is not source code: seed SQL, CSS, TOML, HTML,
    # openCypher, Cedar, Dockerfiles and other extensionless files
    "lambda-adt-seed/003_mes_line3.sql", "agentic_digital_thread/frontend/src/pages/app/user-menu.css",
    "agentic_digital_thread/pyproject.toml", "agentic_digital_thread/frontend/index.html",
    "agentic_digital_thread/data/neptune/pack_discrete.cypher", "agentic_digital_thread/Dockerfile",
    "agentic_digital_thread/Dockerfile.runtime", "NOTICE", ".gitattributes",
)
_SL_CARDS = "agentic_digital_thread/frontend/src/views/architecture/SlCards.tsx"
_REPO_PRODUCT_ALLOWLIST = (
    ("agentic_digital_thread/agents/self_learning.py", "back to pending for steward re-review",
     "the steward Requeue action (requeue_proposal docstring)"),
    ("agentic_digital_thread/agents/server.py", "back to pending for steward re-review",
     "the steward Requeue action (requeue route docstring)"),
    (_SL_CARDS, "re-review; the server undoes the graph artifact",
     "the steward Requeue action (button component comment)"),
    (_SL_CARDS, "Send back to pending for re-review", "the steward Requeue button tooltip"),
    (_SL_CARDS, "Requeue action (back to pending for re-review", "the steward Requeue action (card comment)"),
    ("agentic_digital_thread/frontend/src/views/architecture/proposals.ts",
     "Requeue an ACCEPTED proposal for steward re-review", "the steward Requeue API client"),
    ("agentic_digital_thread/tests/unit/test_requeue_mapping.py",
     "back to ``pending`` for steward re-review", "the steward Requeue action (its test module)"),
)
_ADR_REVIEW_RECORD = "ADR section recording review iterations as decision evidence"
_REPO_RESIDUE_ALLOWLIST = tuple((f"docs/adr/{adr}", sub, _ADR_REVIEW_RECORD) for adr, sub in (
    ("0046-deterministic-response-evidence-contract.md", "## Final targeted iteration 8 checkpoint"),
    ("0063-proposal-precision-and-requirement-promotion.md", "Iteration 2 narrows the denominator"),
    ("0063-proposal-precision-and-requirement-promotion.md", "Iteration 2 makes eligibility action-specific"),
    ("0063-proposal-precision-and-requirement-promotion.md", "Iteration 2 tightens the data boundary"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "(Amended, review iteration 3.)"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "(review iteration 2); a derived metric"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "## Review iteration 1 (2026-09-27)"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "## Review iteration 2 (2026-09-27)"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "## Review iteration 3 (2026-09-27)"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "### Review iteration 4 (orchestrator-closed"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "### Review iteration 5 (measured on the deployed"),
    ("0078-defined-metric-terms-answered-by-the-metric-layer.md", "### Review iteration 6 (measured on the deployed"),
    ("0079-quick-gateway-external-oidc-delegated-user-tokens.md", "### Review iteration 1 (independent reviewer"),
))
# Upstream and sample lines the internal-coordinate forms match on purpose.
_REPO_PROVENANCE_ALLOWLIST = (
    ("agentic_digital_thread/frontend/src/test/evals-logic.test.ts", "detail: 'sha ",
     "the refusal parser's input sample: a golden-set content sha, not a commit"),
)
_REPO_ALLOWLISTS = _REPO_PRODUCT_ALLOWLIST + _REPO_RESIDUE_ALLOWLIST + _REPO_PROVENANCE_ALLOWLIST


# Cheap gates in front of the patterns. The repository scan reads about 40 MB
# of text, and a pattern search costs far more than a substring test, so each
# pattern runs only on a line that holds a literal every one of its matches
# must hold. A gate never decides a verdict: it only skips a search that could
# not match, and the sample tests below pass through the same gates.
#   - every ``_REVIEW_HISTORY`` form spells one of ``_HISTORY_WORDS`` in any
#     case, except the case-sensitive round-task tag (``R2-A``), which
#     ``_ROUND_TASK_TAG`` gates;
#   - every nickname form (``_ACCOUNT_NICKNAME``, ``_NICKNAME_CONTEXT``,
#     ``_NICKNAME_SINGULAR_ACCOUNT``) spells ``demo`` or a ``_NICKNAME_WORDS``
#     word; ``_NICKNAME_GATE`` has the same flags, so it folds case as they do;
#   - every ``_BARE_MR_NUMBER`` form holds a ``!``, every ``_BRANCH_NAME`` form
#     a ``/`` and every ``_PRIVATE_BRIEF`` form the brief word.
_HISTORY_WORDS = ("review", "round", "plan", "finding", "iteration", "merge", "mr",
                  "high", "med", "low", "version", "reproduction")
_ROUND_TASK_TAG = re.compile(r"R\d-")
_NICKNAME_GATE = re.compile("|".join(map(re.escape, ("demo",) + _NICKNAME_WORDS)), re.I)


def _has_word(line: str, words) -> bool:
    """Whether *line* spells one of the lower-case *words* in any case. A
    non-ASCII line always passes: case-insensitive matching folds some
    non-ASCII letters to ASCII ones, which ``str.lower`` does not."""
    if not line.isascii():
        return True
    low = line.lower()
    return any(w in low for w in words)


def _review_history_form(line: str) -> bool:
    return bool((_has_word(line, _HISTORY_WORDS) or _ROUND_TASK_TAG.search(line))
                and _REVIEW_HISTORY.search(line))


def _repo_form(line: str, rel: str = "") -> bool:
    """A review-process form, a rehearsal-account nickname, an MR number
    written bare in prose, or an internal coordinate (a nickname in dated
    evidence, a private brief, a branch name, a commit hash given as evidence)."""
    return bool(_review_history_form(line)
                or (_NICKNAME_GATE.search(line) and _ACCOUNT_NICKNAME.search(line))
                or ("!" in line and _BARE_MR_NUMBER.search(line))
                or _internal_coordinate(rel, line))


def _repo_line_is_residue(rel: str, line: str) -> bool:
    """A shipped line is residue when it matches a review-process form (or names
    a rehearsal account or another internal coordinate) and no allowlist entry
    for ITS file names a substring of it."""
    return _repo_form(line, rel) and not any(
        rel == a_rel and a_sub in line for a_rel, a_sub, _ in _REPO_ALLOWLISTS)


def _repo_scan_skips(path) -> bool:
    return (path.name in _REPO_SCAN_SKIP_NAMES
            or path.suffix.lower() in _REPO_SCAN_BINARY_SUFFIXES + _REPO_SCAN_LOCK_SUFFIXES
            or any(part in _REPO_SCAN_SKIP_PARTS or part.startswith("cdk.out")
                   for part in path.parts))


def _tracked_files():
    """The TRACKED files (``git ls-files``) as repo-relative paths, or ``None``
    outside a git checkout or without git."""
    if not (_REPO_ROOT / ".git").exists():
        return None
    try:
        out = subprocess.run(  # nosec B603 B607 - fixed argv list, no shell
            ["git", "ls-files", "-z"], cwd=_REPO_ROOT, check=True,
            capture_output=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return [pathlib.PurePosixPath(p) for p in out.decode("utf-8").split("\0") if p]


def _walked_files():
    """Every file under the repository root, pruning the skipped directories."""
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        rel_dir = pathlib.PurePosixPath(pathlib.Path(dirpath).relative_to(_REPO_ROOT).as_posix())
        dirnames[:] = sorted(d for d in dirnames if not _repo_scan_skips(rel_dir / d))
        for name in sorted(filenames):
            yield rel_dir / name


def _repo_scan_paths():
    """The repo-relative files the residue scan reads."""
    tracked = _tracked_files()
    candidates = tracked if tracked is not None else _walked_files()
    return sorted(p for p in candidates
                  if not _repo_scan_skips(p) and p.as_posix() not in _REPO_SCAN_SKIP)


def _repo_review_hits():
    hits = []
    form_memo = {}
    for rel_path in _repo_scan_paths():
        path = _REPO_ROOT / rel_path
        # a tracked symlink (CLAUDE.md -> AGENTS.md) is read through its
        # target; a tracked file deleted from the working tree has no text
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        hits += _repo_text_hits(rel_path.as_posix(), text, form_memo)
    return hits


@functools.lru_cache(maxsize=None)
def _checkout_hits_at(root) -> tuple:
    return tuple(_repo_review_hits())


def _checkout_review_hits() -> list:
    """The checkout's hits, scanned once per test process: the two whole-repo
    tests read the same unchanged tree, so they share one scan. Keyed by the
    root, so a test that points ``_REPO_ROOT`` elsewhere never reads it."""
    return list(_checkout_hits_at(_REPO_ROOT))


def _repo_text_hits(rel: str, text: str, form_memo: dict) -> list:
    """One file's hits. ``_repo_form`` reads *rel* only to know whether the
    file is CI configuration (``_BRANCH_SCAN_SKIP``), so its verdict on a line
    is memoised per (line, that flag) across the whole scan: a line repeated
    in many files (an import, a closing brace) is matched once. A line pair
    joins to a nickname only when one of its lines passes ``_NICKNAME_GATE``:
    the join adds only a space, which no gate word holds."""
    ci_config = rel.startswith(_BRANCH_SCAN_SKIP)
    lines = text.splitlines()
    gated = ([bool(_NICKNAME_GATE.search(line)) for line in lines]
             if _NICKNAME_GATE.search(text) else None)
    hits = []
    for i, line in enumerate(lines):
        form = form_memo.get((line, ci_config))
        if form is None:
            form = form_memo[(line, ci_config)] = _repo_form(line, rel)
        if form:
            hits.append((rel, line.strip()))
        elif (gated and i + 1 < len(lines) and (gated[i] or gated[i + 1])
              and (joined := _wrapped_nickname(line, lines[i + 1]))):
            hits.append((rel, joined))
    return hits


def test_repo_scan_reaches_shipped_source_and_docs():
    """Guard against an enumeration that silently scans nothing or drops a
    whole tree: known shipped files from every kind of tree are read."""
    scanned = {p.as_posix() for p in _repo_scan_paths()}
    missing = [s for s in _REPO_SCAN_SENTINELS if s not in scanned]
    assert not missing, missing


def test_repo_scan_reads_every_tracked_text_file():
    """In a git checkout nothing tracked is left out but the skip policy
    (``_repo_scan_skips``: binaries, lock files, generated trees) and this
    file, so a new file type or top-level tree is scanned the day it is added."""
    tracked = _tracked_files()
    if tracked is None:
        pytest.skip("not a git checkout (an exported tree?): the tracked set is unknown")
    scanned = set(_repo_scan_paths())
    left_out = [p.as_posix() for p in tracked
                if p not in scanned and not _repo_scan_skips(p) and p.as_posix() not in _REPO_SCAN_SKIP]
    assert not left_out, left_out


def test_every_scanned_file_decodes_as_utf8_text():
    """The scan skips a file it cannot decode, so this pins that it never has
    to: every file it reads is UTF-8 text. A new binary type is named in the
    skip policy instead of being skipped silently."""
    undecodable = []
    for rel in _repo_scan_paths():
        path = _REPO_ROOT / rel
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            undecodable.append(rel.as_posix())
    assert not undecodable, undecodable


def test_repo_scan_walks_an_exported_tree_without_generated_dirs(tmp_path, monkeypatch):
    """Without git the walk reads every shipped text file and prunes synth
    output, dependencies, build output and local notes."""
    for rel in ("README.md", "docs/a.md", "config/p.json", "cdk.out/x.json",
                "workshop_infra/cdk.out.engine/asset.1/a.py", "frontend/node_modules/m/i.js",
                ".kiro/notes.md", "frontend/dist/app.js", "highbyte/package-lock.json",
                "docs/diagram.png", "seed/001.sql", "Dockerfile", "requirements/app.lock",
                "fonts/x.woff2"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)
    assert _tracked_files() is None
    assert [p.as_posix() for p in _repo_scan_paths()] == [
        "Dockerfile", "README.md", "config/p.json", "docs/a.md", "seed/001.sql"]


# Every kind of shipped text file, each with the comment syntax it uses.
_SHIPPED_TEXT_KINDS = (
    ("lambda-seed/001_seed.sql", "-- {}"),
    ("frontend/src/app.css", "/* {} */"),
    ("pyproject.toml", "# {}"),
    ("service/Dockerfile", "# {}"),
    ("NOTICE", "{}"),
    ("frontend/index.html", "<!-- {} -->"),
)
# One sample per form the repository scan refuses.
_FORBIDDEN_SHAPES = (
    "sent back for re-review",
    f"measured on the {_NICKNAME_WORD} rehearsal account",
    "landed in the !123 fix",
    f"({_NICKNAME_WORD}, 2001-02-03: the card kept one)",
    f"see {_BRIEF_WORD}-private-plan.md",
    f"fixed on {_BRANCH_PREFIXES[1]}/customer-leak",
    "see commit 12345678 for the behavior",
)


def _write_shipped_kinds(root, shapes):
    for rel, template in _SHIPPED_TEXT_KINDS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("plain text\n" + "\n".join(template.format(s) for s in shapes) + "\n",
                        encoding="utf-8")


@pytest.mark.parametrize("rel,_template", _SHIPPED_TEXT_KINDS)
@pytest.mark.parametrize("shape", _FORBIDDEN_SHAPES)
def test_repo_scan_flags_each_form_in_every_shipped_text_kind(tmp_path, monkeypatch, rel, _template,
                                                              shape):
    """SQL, CSS, TOML, HTML and extensionless files ship too: each refused
    form is caught in each of them, not only in source code and Markdown."""
    _write_shipped_kinds(tmp_path, [shape])
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)
    assert rel in {p.as_posix() for p in _repo_scan_paths()}
    hits = [line for hit_rel, line in _repo_review_hits() if hit_rel == rel]
    assert len(hits) == 1 and _repo_line_is_residue(rel, hits[0]), hits


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_repo_scan_reads_tracked_files_of_every_shipped_text_kind(tmp_path, monkeypatch):
    """The same through ``git ls-files``: a tracked file of any text kind is read."""
    _write_shipped_kinds(tmp_path, _FORBIDDEN_SHAPES)
    for argv in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(argv, cwd=tmp_path, check=True, capture_output=True)  # nosec B603 B607 - fixed argv, no shell
    monkeypatch.setitem(globals(), "_REPO_ROOT", tmp_path)
    assert _tracked_files() is not None
    per_file = {}
    for rel, _line in _repo_review_hits():
        per_file[rel] = per_file.get(rel, 0) + 1
    assert per_file == {rel: len(_FORBIDDEN_SHAPES) for rel, _ in _SHIPPED_TEXT_KINDS}


@pytest.mark.parametrize("rel,skipped", [
    ("workshop_infra/cdk.out.engine/asset.0a1b/agents/orchestrator.py", True),
    ("cdk.out/manifest.json", True),
    ("highbyte/components/opc-server/package-lock.json", True),
    ("agentic_digital_thread/frontend/src/node_modules/x/index.js", True),
    ("agentic_digital_thread/frontend/dist/assets/index.js", True),
    (".kiro/rehearsal-logs/notes.md", True),
    (".venv/lib/python3.12/site.py", True),
    ("agentic_digital_thread/agents/orchestrator.py", False),
    ("agentic_digital_thread/frontend/src/test/fixtures/tool-result-outcome-cases.json", False),
    ("highbyte/components/opc-server/package.json", False),
    ("config/dependency-license-policy.json", False),
    # binaries and lock files are skipped by name; every other text kind is read
    ("docs/diagrams/adt-arch-slide-1-main.png", True),
    ("agentic_digital_thread/frontend/public/favicon.ico", True),
    ("agentic_digital_thread/frontend/public/fonts/outfit-latin.woff2", True),
    ("agentic_digital_thread/requirements/app-py312-linux-arm64.lock", True),
    ("lambda-adt-seed/003_mes_line3.sql", False),
    ("agentic_digital_thread/frontend/src/pages/app/user-menu.css", False),
    (".gitleaks.toml", False), ("agentic_digital_thread/Dockerfile", False), ("NOTICE", False),
    ("docs/adaptability.html", False), ("requirements.txt", False),
])
def test_repo_scan_skips_generated_trees_only(rel, skipped):
    """Synth output holds copies of OLDER source and would fail a local run on
    lines the checkout no longer has; lock files hold no prose."""
    assert _repo_scan_skips(pathlib.PurePosixPath(rel)) is skipped


@pytest.mark.parametrize("rel,line", [
    # a non-product re-review is residue, in a doc or in a file that also has
    # allowlisted product uses
    ("docs/example.md", "sent to re-review after maintainer feedback"),
    (_SL_CARDS, "// sent to re-review after the r2 round"),
    # a product phrase moved to a file it is not allowlisted for still fails
    ("docs/example.md", "back to pending for steward re-review"),
    # internal plan ids and review-round reviewer references
    ("docs/example.md", "Steward digest (ADR-0052, plan task 2.4)"),
    ("agentic_digital_thread/tests/unit/test_example.py", "Pinned invariants (plan Task 3):"),
    ("agentic_digital_thread/tests/unit/test_example.py", "The exact r1 reviewer reproduction"),
    ("agentic_digital_thread/tests/unit/test_example.py", "# ── review r1 regressions ──"),
    ("agentic_digital_thread/tests/unit/test_example.py", "# r2 FINDING 2 — error-NAME matching"),
    ("agentic_digital_thread/tests/unit/test_example.py", '"""r1 HIGH: Mode B let the error escape'),
    ("agentic_digital_thread/tests/unit/test_example.py", "The r1 version called _tool_error directly"),
    ("agentic_digital_thread/tests/unit/test_example.py", "as the r3 reproduction showed"),
    ("agentic_digital_thread/tests/unit/test_example.py", "Iteration-4 fix: the iteration-3 gate"),
    ("agentic_digital_thread/agents/example.py", "#: REQUEST for a single-word form (review iteration 3)."),
    ("agentic_digital_thread/tests/unit/test_example.py", '"""Fix-pass iteration 6: the emitter is gated'),
    ("agentic_digital_thread/tests/unit/test_example.py", "# pinned since review iteration_2 of the probe"),
    # severity-letter finding ids and plural / compact iteration labels
    ("agentic_digital_thread/agents/example.py", "Mode-aware honesty (review M2): under enforce"),
    ("docs/example.md", "   reject (round-2 review C1)."),
    ("agentic_digital_thread/frontend/src/example.ts", " * (citedSystemIds; review H1 — never display labels)"),
    ("docs/example.md", "  (review iterations 1-3) — scheduled execution"),
    ("agentic_digital_thread/agents/example.py", "# frozen by the maintainability gates (iteration5/6)"),
    ("agentic_digital_thread/tests/unit/test_example.py", "the existing iteration7 pins keep running"),
    ("agentic_digital_thread/agents/example.py", "# --- per-key merge-policy registry (review defect 1) ---"),
    ("agentic_digital_thread/agents/example.py", "# undefined metric (reviewer H-1). The article stays"),
    ("docs/example.md", "  plain `[]` (2001-02-03, review items 2001-02-04)."),
    ("agentic_digital_thread/agents/example.py", "# NOTE (R2-A): chart delivery moved server-side."),
    ("docs/example.md", "  Review-2 hardening: composition is session-independent"),
    ("agentic_digital_thread/agents/example.py", "# Review items (2001-02-03): the probe span starts"),
    ("agentic_digital_thread/agents/example.py", "#: a clean zero-point read (review item, 2001-02-03)."),
    # MR numbers, review rounds named with a finding / severity / step, a
    # round-numbered section header, and rehearsal-account nicknames
    ("agentic_digital_thread/agents/example.py", "#: is never read as a cut (review of !123, round 2). Shape"),
    ("agentic_digital_thread/tests/unit/test_example.py", '"""REGRESSION (!123): an onboarded system'),
    (".gitlab-ci.yml", "  # lines above (MR !123 and !1234)."),
    ("agentic_digital_thread/frontend/src/test/fixtures/example.json",
     '"note": "the summarizer unwraps it (review of !123 round 2)."'),
    ("agentic_digital_thread/tests/unit/test_example.py", "# ── round 2, finding 1: a sessioned discover ──"),
    ("agentic_digital_thread/tests/unit/test_example.py", '"""MED (round 4): the service reports'),
    ("agentic_digital_thread/tests/unit/test_example.py",
     "(passes) because the round-1 transitional-retry fix already landed there"),
    ("agentic_digital_thread/tests/unit/test_example.py", "# Each one was counted as a cut by the round-1 walk."),
    ("agentic_digital_thread/tests/unit/test_example.py", "# The equipment anchor as round 3 wrote it"),
    ("agentic_digital_thread/tests/unit/test_example.py", "the mapping anchored to it only. Round 3's follow-up anchor"),
    ("agentic_digital_thread/tests/unit/test_example.py", "# ── Round 2: a system the runtime owns ──"),
    ("agentic_digital_thread/agents/example.py",
     f"#: guarantee: on the {_NICKNAME_WORD} rehearsal account, 2001-02-03"),
    ("docs/example.md", "replay reproduced the captured xx-demo seed of 2001-02-03."),
    ("erp_simulation/example.py", f"list past the plan ({_NICKNAME_WORD} rehearsal, 2001-02-03: lot total"),
    ("agentic_digital_thread/tests/unit/test_example.py", "Live (XX-Demo, 2001-02-03): the hub was removed"),
    ("docs/example.md", "measured on xxdemo before the change"),
    ("docs/example.md", f"the {_NICKNAME_WORD.upper()}  Rehearsal account"),
    # one case per alternative: MR ``#`` and a bulleted ``!``, each step noun
    # after a spaced round, a possessive round, any hyphen-numbered round, and
    # compact finding ids
    (".gitlab-ci.yml", "  # see MR #123 for the runner change"),
    ("docs/example.md", "- !123 landed the next day"),
    ("docs/example.md", "  round 3 findings were all fixed"),
    ("docs/example.md", "because the round 1 transitional-retry fix landed"),
    ("docs/example.md", "counted as a cut by the round 1 walk"),
    ("docs/example.md", "the round 3 follow-up anchor"),
    ("docs/example.md", "the round 2 reproduction of the stall"),
    ("docs/example.md", "closes the round 2 advisory on chart tags"),
    ("docs/example.md", "the round 1 forbidden substitution"),
    ("docs/example.md", "strings (tracked by the 16-hit round 1 grep baseline)."),
    ("docs/example.md", "as the round 2 contract required"),
    ("docs/example.md", "- round 1 `chat_client` injection is retired"),
    ("docs/example.md", "apply the round 2 honesty rules"),
    ("docs/example.md", "see round 2's notes"),
    ("docs/example.md", "fixed after the round-3 regression"),
    ("docs/example.md", "as noted in round 2, finding 3 above"),
    ("agentic_digital_thread/agents/example.py",
     '# or re-queued (closes round-1\'s "chart injection has no tool-name gate"'),
    ("agentic_digital_thread/agents/example.py", '"""Apply the round-2 answer-honesty rules at the seams.'),
    ("agentic_digital_thread/agents/example.py", "edge exists (contract §3, finding F6) and none must appear"),
    ("agentic_digital_thread/agents/example.py", "# discovery — finding R3-4). Mapped server-side"),
    ("scripts/example.py", "# (ADR-0044 Decision 3 — closes finding F2, the localhost incident"),
    ("docs/example.md", "the mismatch (§3, finding #1) is a code-level hazard"),
    ("docs/example.md", "two findings #3 and #4 remain open"),
    # a bare MR number in a TypeScript/JavaScript comment, whole-line or
    # trailing (also glued to the code), JSDoc and JSX; and a NAMED MR number
    # in a string, where the bare form is not looked for
    ("agentic_digital_thread/frontend/src/example.ts", "// fixed in !123 after the live check"),
    ("agentic_digital_thread/frontend/src/example.ts", "   * the !123 live-check fix"),
    ("agentic_digital_thread/frontend/src/example.tsx", "  {/* see !123 */}"),
    ("agentic_digital_thread/frontend/src/example.js", "doThing(); // !123 regression"),
    ("highbyte/components/example.mjs", "doThing();// !123 regression"),
    ("agentic_digital_thread/frontend/src/example.ts", "foo(/* see !123 */ !10)"),
    ("agentic_digital_thread/frontend/src/example.ts", "foo('a'); // it's the !123 fix"),
    ("agentic_digital_thread/frontend/src/example.ts", "throw new Error('see MR !123 for the runner');"),
    ("agentic_digital_thread/frontend/src/example.ts", "const note = 'review of !123';"),
    ("agentic_digital_thread/frontend/src/example.ts", "label: 'merge request !123',"),
    # a line inside a multi-line block comment
    ("agentic_digital_thread/frontend/src/example.ts", "  fixed in !123 after the live check"),
    # the number-only form (three and two digits), and bare uses with no cue
    # word before or after the number
    ("agentic_digital_thread/agents/example.py", "# shape (MR 123, round 2): the cut note rides"),
    ("agentic_digital_thread/tests/unit/test_example.py", '"""Review of MR 123: the record keys'),
    ("docs/example.md", "The MR 99 connector design keeps one adapter per vendor."),
    ("docs/example.md", "MR 99's mock tenant answers the same paths."),
    ("docs/example.md", "merge request 123 moved the lane to a bigger runner"),
    ("docs/example.md", "MRs !123 and !1234 sized the runner"),
    ("docs/example.md", "see mr !123 for the runner change"),
    ("agentic_digital_thread/tests/unit/test_example.py",
     '"""REGRESSION (!123 xx-demo): an onboarded system can have no onboarding'),
    ("agentic_digital_thread/tests/unit/test_example.py",
     '"""!123 review r8 MED 1: "no node" and "a node with no pointer" were one answer.'),
    ("agentic_digital_thread/tests/unit/test_example.py",
     "    (the !123 live-check fix), through the offline seam's node-state answer.\"\"\""),
    ("agentic_digital_thread/tests/unit/test_example.py",
     '"""Preflight on a user MCP system after a task restart (a live run of !123,'),
    ("agentic_digital_thread/tests/unit/test_example.py", "Follow-ups from the !123 review (2001-02-03):"),
    ("agentic_digital_thread/tests/unit/test_example.py",
     "    # Live 2001-02-03 (rehearsal with !99): a log id came back"),
])
def test_repo_scan_flags_residue(rel, line):
    assert _repo_line_is_residue(rel, line)


@pytest.mark.parametrize("line", [
    "REGRESSION (!123): an onboarded system", "the !123 live-check fix", "the !1234 replay",
])
def test_agents_string_literal_scan_still_catches_a_bare_mr_number(line):
    """The agents/ string-literal scan applies the same bare-number rule."""
    assert _BARE_MR_NUMBER.search(line)


@pytest.mark.parametrize("word", _PROSE_BEFORE)
def test_each_prose_word_before_a_bare_mr_number_counts(word):
    assert _BARE_MR_NUMBER.search(f"noted {word} !123 today")


@pytest.mark.parametrize("word", _PROCESS_AFTER)
def test_each_process_word_after_a_bare_mr_number_counts(word):
    assert _BARE_MR_NUMBER.search(f"noted: !123 {word} today")


@pytest.mark.parametrize("line", [
    "the steward reviews findings in the queue", "an independent reviewer decides",
    "the plan's tasks run in order", "planned task list", "r2 is the second register",
    "the r1 coefficient is fitted", "the next iteration retries",
    "review EC2 instance settings", "review s3 bucket policy", "after 3 iterations it stops",
    "frozen by the test_response_integrity_iteration5/6 gates",
    "the reviewer approves each item", "review the defect list", "R2-D2 droid",
    "region r2-a is lower case", "lot L-1 and machine M-1",
    "the review queue holds 2 items", "a pre-review step", "review the items, then 2 more",
    # code and prose near the new forms
    "if x != 10 and not flag", "return !ok || !ready", "f'{value!r}'", "![diagram](arch.png)",
    "a round-trip fix", "rounded to 2 places", "the value stays low (round to 2 places)",
    "round 3 of the retry loop", "i3X round-2 types resolve through the palette",
    "pre-round-2 graphs stay green", "the retry runs a second round",
    "member accounts of the organization", "the member accounts in the OU",
    "an act-demo label", "a react-demo label", "the ADT demo account", "see 0044-records-demo-routing-in-demo-layer.md",
    "the base-vs-demo signal", "a slug such as build-xx-demo-stack",
    "a dress rehearsal", "on a rehearsal account", "the four-site live rehearsal account",
    # a machine tag, an AWS or industrial identifier, a metric or a row count
    # after "finding", and a product node id
    "the agent kept finding L3-M1 under the wrong site", "finding-7 is the node id",
    "the finding 2 rows later", "findings at L3 are grouped",
    "finding EC2 instances in the account", "finding S3 buckets by tag",
    "the agent kept finding L3 under the wrong site", "finding ISO9001 records",
    "the eval kept finding F1 scores below 0.8", "finding R12-34-B7 in the parts list",
    # round wording in product prose and code
    "round 3 of 5 retries", "a round-robin rule", "Math.round(-1)", "round(x, 2)",
])
def test_repo_scan_ignores_ordinary_wording(line):
    assert not _repo_line_is_residue("docs/example.md", line)


_FEAT, _FIX = _BRANCH_PREFIXES
_W = _NICKNAME_WORD


@pytest.mark.parametrize("rel,line", [
    # an account nickname in dated evidence
    ("agentic_digital_thread/agents/example.py", f"is on the event loop ({_W} 2001-02-03: every request stalled)."),
    ("agentic_digital_thread/frontend/src/example.ts", f"  // as rule lines ({_W}, 2001-02-03: the card kept one)."),
    ("agentic_digital_thread/tests/unit/test_example.py", f"Workshop rehearsal, 2001-02-03 ({_W} account): one system"),
    ("agentic_digital_thread/tests/unit/test_example.py", f"#: the {_W} account's ERP agent carried for the batch"),
    ("agentic_digital_thread/tests/unit/test_example.py", f"its first provision ({_W} live run 2001-02-03: host null,"),
    ("agentic_digital_thread/tests/unit/test_example.py", f"# Trimmed from the 2001-02-03 {_W} walk of module 2"),
    ("agentic_digital_thread/agents/example.py", f"    the time line ({_W} run, 2001-02-03). The model still"),
    ("agentic_digital_thread/tests/fixtures/example.json", f'"_comment": "live run x-{_W}-0a1b2c3d (2001-02-03)"'),
    # a private brief named as a source
    ("agentic_digital_thread/tests/unit/test_example.py", f"#: Raised for the merge ({_BRIEF_WORD}-xx-decision.md): the"),
    ("agentic_digital_thread/tests/unit/test_example.py", f'"""A register-only system ({_BRIEF_WORD}-xx-onboard, Part 1).'),
    ("agentic_digital_thread/frontend/src/test/example.ts", f"    // {_BRIEF_WORD.lower()}-x1/run-1 (2001-02-03): every run"),
    # a branch name in prose, and a commit hash given as evidence
    ("docs/adr/example.md", f"**Accepted: 2001-02-03.** Implemented on branch `{_FEAT}/xx-story-switch`."),
    ("agentic_digital_thread/tests/unit/test_example.py", f"#: the {_FEAT}/v9 header growth does not move this"),
    ("docs/example.md", f"fixed on `{_FIX}/xx-1021` before the rescan"),
    ("docs/example.md", f"Sweep of 2001-02-03 findings (branch `{_DOCS_PREFIX}/xx-sweep`)."),
    ("docs/example.md", f"  scan of the content packaged from app `{_FEAT}/v9` `0a1b2c3d`"),
    ("docs/example.md", "  sha `0a1b2c3d4e5f`) uploaded through the route scored"),
    ("agentic_digital_thread/agents/example.py", "      commit 0a1b2c3 — no false negatives)."),
    ("agentic_digital_thread/tests/unit/test_example.py", "Live evidence (2001-02-03, build 0a1b2c3d): a browser"),
    ("agentic_digital_thread/frontend/src/example.ts", " * reload bug, a test event on app 0a1b2c3d: answer above"),
    ("agentic_digital_thread/tests/unit/test_example.py", "# story_loop (main 0a1b2c3d). Moving the math must not"),
    ("agentic_digital_thread/frontend/AUDIT.md", "### Update (2001-02-03, merge 0a1b2c3 — data-mode-aware suite)"),
    # an MR number after the tracker's own draft label
    ("agentic_digital_thread/tests/unit/test_example.py", "#: The note that provision (Draft !123) baked, ahead"),
    # after an unambiguous commit word any 8-40 hex characters count, digits
    # only or letters only, and a 7-character one holding a letter
    ("docs/example.md", "see commit 12345678 for the behavior"),
    ("docs/example.md", "see commit abcdefa for the behavior"),
    ("docs/example.md", "Commit 0123456789 reverted it"),
    ("docs/example.md", "the revision abcdefab of the handler"),
    ("docs/example.md", "commits 12345678 and 23456789"),
    ("docs/example.md", "see COMMIT abcdef12 for the behavior"),
    ("docs/example.md", "REVISION 0123abcd of the handler"),
    ("docs/example.md", "Sha 0a1b2c3d4e5f was the fix"),
    # the nickname word with its singular account in dated or live evidence
    ("docs/example.md", f"traces from the {_W} account, live run: the card kept one"),
    ("docs/example.md", f"observed on the {_W} account before the change"),
    ("agentic_digital_thread/tests/unit/test_example.py", f"#: the {_W} account’s ERP agent carried"),
])
def test_repo_scan_flags_internal_coordinates(rel, line):
    assert _repo_line_is_residue(rel, line)


@pytest.mark.parametrize("line", [
    f"{_W} accounts of the organization", f"a {_W} account of the organization",
    f"the OPERATOR is an ENUM {_W} (no free-text operators)", f"for {_W} in {_W}s:",
    f"the reserved {_W} carries the engine line", f"a Glue-flattened struct {_W} is ONE column",
    f"per-family {_W} count over rows", f"  (SELECT count(*) FROM pg_auth_{_W}s m WHERE m.{_W} = r.oid)",
    '   "page": "7b-' + _BRIEF_WORD.lower() + '-the-plant-manager",', "a daily briefing card",
    "see docs/adr/0065-unattended-provisioning-fresh-account-findings.md",
    
    "https://guide.highbyte.com/release/setup/docker/", "an int/float value", "the docs/ tree",
    "a sha256:" + "0a1b2c3d" * 8 + " digest", "commit 1234567 rows", "build 2001-02-03",
    "app version baked:           abc1234 · 2001-02-03", "head -n 20 build.log",
    "the main deadbeefcafe value", "query 0a1b2c3d returned no rows",
    # AWS Organizations wording for an account in an organization
    f"the {_W} account is managed by AWS Organizations",
    f"deploy the role in the {_W} account first", f"the management account and the {_W} account",
    f"each {_W} account gets the stack set", f"a {_W} account can leave the organization",
    f"the {_W} account runs live workloads", f"a {_W} account with measured spend",
    f"the {_W} account sends captured logs to the log archive account",
    # a word that is not an unambiguous commit word keeps the letter-and-digit rule
    "merge 12345678 rows into one", "the build 20261018 nightly",
])
def test_repo_scan_ignores_ordinary_coordinates(line):
    assert not _repo_line_is_residue("docs/example.md", line)


def test_branch_names_are_allowed_in_ci_configuration():
    line = f"    - if: $CI_COMMIT_BRANCH == \"{_FEAT}/xx-change\""
    assert not _repo_line_is_residue(".gitlab-ci.yml", line)
    assert _repo_line_is_residue("docs/example.md", line)


@pytest.mark.parametrize("line,next_line", [
    (f"# the mcp default 300 s: a call returned its answer on the {_W}",
     "# rehearsal account, 2001-02-03; see the window."),
    ("of 27 rehearsal runs. Traces of a failing and a passing run (" + _W + ",",
     "2001-02-03) took the same first path"),
    ("    or a phrase about data. Live false positive that started this, " + _W,
     "    2001-02-03: an honest gap answer asked the user"),
    ("    run: the history plan already records where that batch went (" + _W,
     "    rehearsal 2001-02-03: the first live shipments landed"),
])
def test_a_nickname_wrapped_across_two_lines_is_read_joined(line, next_line):
    joined = _wrapped_nickname(line, next_line)
    assert joined and _repo_line_is_residue("docs/example.md", joined)


def test_a_line_pair_already_flagged_alone_is_not_read_twice():
    assert _wrapped_nickname(f"Workshop rehearsal ({_W} account): one", "system kept") == ""
    assert _wrapped_nickname("for each item in the", f"{_W} accounts of the organization") == ""


@pytest.mark.parametrize("suffix", (".ts", ".tsx", ".js", ".mjs", ".py", ".md", ".json", ".yml", ".sh"))
@pytest.mark.parametrize("line", [
    "const disabled = !10;", "if (a && !123) return", "x || !10", "return !10;",
    "cond ? !10 : y", "foo(!10)", "y=!10", "typeof !10",
    "foo( !10)", "return (!10);", "const xs = [!10];", "const xs = [ !10 ];",
    "const x = {a: !10};", "const f = () => (!10);", "!10 && doThing();",
    "const url = 'https://example.com/!123';", "const glob = 'src/**/*.ts' && !10;",
    "include: ['src/*.test.ts'], skip: !10,", "const u = `${base}//${!10}`;",
    "see https://example.com/x !10",
    # data and code in other files, and a closing comment or generator line
    '{"expected": "!10"}', 'value: "!10"', 'printf "%s\\n" "!10"',
    "*/ const disabled = !10;", "*generator() { return !10; }", "  !10,",
])
def test_repo_scan_ignores_bang_numbers_in_code_and_data(suffix, line):
    """A ``!`` before digits in code or data is not an MR number: only prose
    around it makes it one, in every kind of file."""
    assert not _repo_line_is_residue(f"agentic_digital_thread/frontend/src/example{suffix}", line)


@pytest.mark.parametrize("line", [
    "x != 10 and not flag", "!flag", "f'{value!r}'", "${!10}", "echo $!10", "a/!123",
    "a data token like abc!123", "the double negation !!10 coerces to true",
    "Mr 99 Street is the site address", "the MR10 sensor", "mr 99 is lowercase",
    "the `!10` expression coerces to false", "Mrs #3 of the roster", "if (!10) return",
    # inside a token, inside a word, and a mixed-case name before the paren
    "token abc!12 review", "path a/!123 merged", "double !!10 head", "lathe !10", "Foo (!10)",
    "X (!10) in a formula",
])
def test_bare_mr_number_ignores_operators_everywhere(line):
    assert not _BARE_MR_NUMBER.search(line)
    assert not _repo_line_is_residue("docs/example.md", line)


@pytest.mark.parametrize("rel,line", [(rel, sub) for rel, sub, _ in _REPO_PRODUCT_ALLOWLIST])
def test_repo_scan_keeps_the_product_requeue_wording(rel, line):
    assert not _repo_line_is_residue(rel, line)


def test_pattern_gates_pass_every_sample_the_patterns_match():
    """A gate only skips a search that could not match: every string in this
    module (the samples above included) that a gated pattern matches passes
    that pattern's gate."""
    def brief(s):
        return _has_word(s, (_BRIEF_WORD.lower(),))
    gates = ((_REVIEW_HISTORY, lambda s: _has_word(s, _HISTORY_WORDS) or _ROUND_TASK_TAG.search(s)),
             (_ACCOUNT_NICKNAME, _NICKNAME_GATE.search), (_NICKNAME_CONTEXT, _NICKNAME_GATE.search),
             (_NICKNAME_SINGULAR_ACCOUNT, _NICKNAME_GATE.search), (_BARE_MR_NUMBER, lambda s: "!" in s),
             (_BRANCH_NAME, lambda s: "/" in s), (_PRIVATE_BRIEF, brief))
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    samples = [line for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               for line in node.value.splitlines()]
    samples += _parametrized_strings() + list(_FORBIDDEN_SHAPES)
    matched = [(p, s) for p, _gate in gates for s in samples if p.search(s)]
    assert len({p.pattern for p, _s in matched}) == len(gates), (
        [p.pattern[:40] for p, _g in gates if p.pattern not in {q.pattern for q, _s in matched}])
    missed = [(p.pattern[:40], s) for p, gate in gates for s in samples if p.search(s) and not gate(s)]
    assert not missed, missed


def _parametrized_strings() -> list:
    """Every string in this module's parametrize arguments, f-strings
    evaluated (the nickname samples are built from the nickname word)."""
    def strings(value):
        if isinstance(value, str):
            return value.splitlines()
        if isinstance(value, (list, tuple)) or type(value).__name__ == "ParameterSet":
            return [s for v in (value.values if hasattr(value, "values") else value) for s in strings(v)]
        return []
    return [s for fn in list(globals().values()) for mark in getattr(fn, "pytestmark", [])
            if mark.name == "parametrize" for s in strings(list(mark.args[1:]))]


def test_repo_source_and_docs_have_no_review_history():
    unlisted = [f"{rel}: {line}" for rel, line in _checkout_review_hits()
                if _repo_line_is_residue(rel, line)]
    assert not unlisted, (
        "review-process history in shipped source/docs (not allowlisted):\n"
        + "\n".join(unlisted))


def test_repo_allowlists_only_shrink():
    hits = _checkout_review_hits()
    stale = [f"{rel} :: {sub}" for rel, sub, _ in _REPO_ALLOWLISTS
             if not any(rel == h_rel and sub in h_line for h_rel, h_line in hits)]
    assert not stale, (
        "repo allowlist entries no longer match any hit — delete them:\n"
        + "\n".join(stale))
