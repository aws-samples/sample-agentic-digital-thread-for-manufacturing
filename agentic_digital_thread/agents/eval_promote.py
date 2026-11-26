# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Promoted-golden DRAFT builder — transient, deterministic (ADR-0062 D5).

A promoted draft is CUSTOMER-DERIVED data: its ``text`` is a real customer
question joined live from the source store. So the draft is classified as
*transient response data* — it is built in memory, handed straight to the
download response, and persisted NOWHERE server-side (no JobStore row, no
DynamoDB item, no ``/tmp`` file, no draft content in any log record). This
module is what makes that pinnable: it is import-pure and stdlib-only, it
owns no store handle, and it opens no file.

Deliberately weak content (D5 — the strong version was rejected):

* **No ``answer_must_match``, and no code path that can emit one.** An
  auto-derived phrase gate enshrines yesterday's phrasing; a routing
  identifier that appeared in one good answer need not appear in a better
  one. :data:`DEFAULT_EXPECT` is a fixed house skeleton — identical for every
  draft, derived from NOTHING in the turn — that the steward then edits.
* Observed counters go ONLY into the top-level, **non-gating** :data:`hints`
  block. Scoring reads ``question["expect"]`` and never looks at ``hints``,
  so a hint can neither pass nor fail a question. Hint keys are a disjoint
  namespace from the gate keys (pinned).
* Provenance is minimized to ``{source: "promoted", source_ref_sha}``. There
  is **no ``decided_by``**, no steward id, no raw source ref and no
  timestamp: the destination file is CUSTOMER-owned, and identity must not
  leak into it. Enforced by a recursive leaf allowlist (the same walk the
  decision store uses), so a renamed or nested field cannot slip through.

DETERMINISTIC — no LLM, no model client, no network: two calls over the same
inputs return byte-identical drafts (no timestamp, no uuid, no randomness).

The destination is an EXTERNAL, customer-owned golden set;
``scripts/merge_golden_draft.py`` is the only sanctioned landing path and it
refuses any destination beneath this repository (ADR-0062 D1/D5).
"""
from __future__ import annotations

import copy
import re

from . import eval_core
# ONE home for the recursive leaf-allowlist walk: the same mechanism that
# keeps customer text out of a persisted decision row keeps steward identity
# out of an exported draft (ADR-0062 D1).
from .eval_queue import _unexpected_leaves, ref_sha, source_of

#: The one provenance source value a machine-built draft may carry.
DRAFT_SOURCE = "promoted"
DRAFT_ID_PREFIX = "promoted-"
FILENAME_PREFIX = "golden-draft-"

#: Bounded draft question text (the queue item's excerpt is already bounded;
#: this is the belt-and-braces cap that the exported file obeys).
TEXT_MAX_CHARS = 400

#: Download headers — POST-only response, never cacheable, opaque filename.
CACHE_CONTROL = "private, no-store"
PRAGMA = "no-cache"
MEDIA_TYPE = "application/json"

#: The DEFAULT expect skeleton. A fixed constant: nothing here is derived from
#: the turn, and ``answer_must_match`` is deliberately ABSENT (D5). The
#: negative guard is house boilerplate (it appears in every packaged golden)
#: and is not generated from the answer.
DEFAULT_EXPECT = {
    "systems_min": 1,
    "neptune_min": 0,
    "rules_applied_min": 0,
    "answer_must_not_match": ["(?i)as an ai"],
    "absence_honesty": False,
    "injection_guard": False,
}

#: The gate key that must NEVER appear in a generated draft.
FORBIDDEN_EXPECT_KEY = "answer_must_match"

#: Observed counters, read null-safe from an ``intelligence_summary``.
_HINT_COUNTERS = (
    ("observed_systems", "system_queries"),
    ("observed_neptune_queries", "neptune_queries"),
    ("observed_tool_calls", "total_tool_calls"),
)
_HINT_LENGTHS = (("observed_rules_applied", "rules_applied"),)

HINT_KEYS = frozenset([name for name, _ in _HINT_COUNTERS]
                      + [name for name, _ in _HINT_LENGTHS])
PROVENANCE_KEYS = frozenset({"source", "source_ref_sha"})
DRAFT_TOP_KEYS = ("id", "text", "expect", "hints", "provenance")

#: Recursive leaf allowlist for a built draft. Anything outside it — a
#: ``decided_by``, a ``source_ref``, a ``steward_note``, a session id — is a
#: hard error, not a silently exported field.
#: The advice leaves ``eval_core.with_stability_advice`` may add inside
#: ``hints``. Named here so the leak guard admits them EXPLICITLY — a future
#: advice key must extend this (and the ADR) rather than slip into a
#: customer-owned file unnoticed.
ALLOWED_DRAFT_LEAVES = (frozenset(DRAFT_TOP_KEYS) | frozenset(DEFAULT_EXPECT)
                        | HINT_KEYS | PROVENANCE_KEYS
                        | eval_core.STABILITY_ADVICE_KEYS)

_SHA16_RE = re.compile(r"^[0-9a-f]{16}$")


def _counter(value) -> "int | None":
    """A measured non-negative counter, or ``None`` when not measured.

    Honest-data posture (ADR-0045): a missing counter yields an ABSENT hint
    key, never a fabricated ``0``. Bools are not counters.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def observed_hints(source_turn: dict) -> dict:
    """NON-GATING observed counters for the steward writing the real gates.

    Read from the turn's ``intelligence_summary`` when present, else from the
    queue item's live-joined ``cited_systems``. Never a verdict, never a
    threshold: scoring never reads this block.
    """
    summary = source_turn.get("intelligence_summary")
    summary = summary if isinstance(summary, dict) else {}
    hints: dict = {}
    for name, key in _HINT_COUNTERS:
        count = _counter(summary.get(key))
        if count is not None:
            hints[name] = count
    for name, key in _HINT_LENGTHS:
        value = summary.get(key)
        if isinstance(value, (list, tuple)):
            hints[name] = len(value)
    cited = source_turn.get("cited_systems")
    if "observed_systems" not in hints and isinstance(cited, (list, tuple)):
        hints["observed_systems"] = len(cited)
    return hints


def _source_ref_sha(decision: dict) -> str:
    """The OPAQUE sha-16 of the decided source ref (the only ref form that
    may leave the server), or ``""`` when the decision cannot supply one."""
    sha = decision.get("ref_sha")
    if isinstance(sha, str) and _SHA16_RE.match(sha):
        return sha
    source_ref = decision.get("source_ref")
    return ref_sha(source_ref) if source_of(source_ref) else ""


def question_text(source_turn: dict) -> str:
    """Bounded question text joined LIVE from the source turn.

    Also the PROMOTABILITY predicate: the route asks this before drafting, so
    "has promotable text" has exactly one definition.
    """
    for key in ("question", "text"):
        value = source_turn.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:TEXT_MAX_CHARS]
    return ""


def draft_golden(decision: dict, source_turn: dict) -> dict:
    """Build ONE transient golden draft. Deterministic; writes nothing.

    ``decision`` supplies only the OPAQUE ref sha (never identity);
    ``source_turn`` supplies the live question text and the observed counters.
    Raises ``ValueError`` when either input cannot produce an honest draft —
    the caller surfaces that as a distinct state, never a fabricated draft.
    """
    decision = decision if isinstance(decision, dict) else {}
    source_turn = source_turn if isinstance(source_turn, dict) else {}
    sha = _source_ref_sha(decision)
    text = question_text(source_turn)
    if not sha or not text:
        raise ValueError("a promoted draft needs a decided source ref and "
                         "live source question text")
    draft = {
        "id": f"{DRAFT_ID_PREFIX}{sha}",
        "text": text,
        "expect": copy.deepcopy(DEFAULT_EXPECT),
        # ADR-0063: the observed counters, PLUS the non-gating late-arrival
        # advice derived from them. Shipped 2026-08-18 with tests but NO
        # caller; wired 2026-08-19 after two goldens failed on exactly the
        # drift it exists to warn about (a count that outgrew its pinned
        # band, and a rate that gained a decimal place). Advice only — it
        # never writes answer_must_match (ADR-0062).
        "hints": eval_core.with_stability_advice(observed_hints(source_turn)),
        "provenance": {"source": DRAFT_SOURCE, "source_ref_sha": sha},
    }
    leaked = _unexpected_leaves(draft, ALLOWED_DRAFT_LEAVES)
    if leaked:
        # Defensive: a future field must extend the allowlist (and the ADR)
        # rather than silently exporting into a customer-owned file.
        raise ValueError(
            f"draft carries non-allowlisted keys: {sorted(leaked)}")
    return draft


def draft_bundle(draft: dict) -> dict:
    """Wrap ONE draft as a standalone golden-set document.

    This is the downloaded file's shape, so the download round-trips through
    ``eval_core.load_golden_set`` unchanged — and a committed draft trips the
    repository boundary gate (which scans for exactly this shape).
    """
    return {"version": eval_core.GOLDEN_SET_VERSION, "questions": [draft]}


def draft_filename(draft: dict) -> str:
    """The OPAQUE attachment filename — sha-16 only, never question text."""
    sha = (draft.get("provenance") or {}).get("source_ref_sha")
    if not isinstance(sha, str) or not _SHA16_RE.match(sha):
        raise ValueError("draft provenance carries no sha-16 source ref")
    return f"{FILENAME_PREFIX}{sha}.json"


def download_headers(draft: dict) -> dict:
    """The pinned download-response headers (ONE home for the contract)."""
    return {
        "Content-Disposition": f'attachment; filename="{draft_filename(draft)}"',
        "Cache-Control": CACHE_CONTROL,
        "Pragma": PRAGMA,
    }
