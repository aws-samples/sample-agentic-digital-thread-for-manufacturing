# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Query-intelligence primitives for the intelligence-summary payload.

Pure, stdlib-only (import purity per ADR-0020/0021 — no boto3, no I/O).
Single source of truth for:

- the graph-query predicate (``is_graph_query`` — the semantics of
  ``runtime_app._is_explore_graph``, both the bare in-process spelling and
  the AgentCore Gateway target-prefixed spelling);
- the FAILED/empty-result envelope markers (``is_failed_result`` /
  ``count_wasted_calls`` — moved verbatim from the runtime agent-analysis
  block so the summary's wasted-call count and the analysis prompt can never
  drift);
- rule-label formatting (``format_rule_label`` — an accepted DynamoDB
  proposal row rendered as a short human label);
- the intelligence-summary payload shape (``build_intelligence_summary`` —
  the ONE builder both emitters use; caps ``rules_applied`` at
  ``MAX_RULES_APPLIED`` entries of ``MAX_LABEL_LEN``-char labels, bounding
  the SSE metadata event to ≈4 KB).

The impure halves live elsewhere: the accepted-row lookup in
``self_learning.get_accepted_rules_by_node_ids`` and the usage bump in
``self_learning.record_rule_usage`` (runtime finally-block hook only).
"""
from __future__ import annotations

import re
from typing import Iterable

#: Hard cap on ``rules_applied`` entries in the summary payload.
MAX_RULES_APPLIED = 20

#: Hard cap on a single rule label's length (chars).
MAX_LABEL_LEN = 160

#: Hard cap on ``planned_with.shared_concepts`` entries in the summary payload.
MAX_PLANNED_CONCEPTS = 10

#: Hard cap on a single planned-with concept's length (chars).
MAX_PLANNED_CONCEPT_LEN = 80

#: Hard cap on ``composed_context.artifacts`` entries in the summary payload.
MAX_COMPOSED_ARTIFACTS = 20

#: Prefix marking one accumulated LEARNED note inside a system's
#: ``query_guidance``. Single home for the marker: ``self_learning`` imports it
#: rather than spelling it again, so the write format and every reader
#: (guidance split, agent mirror, composed-context telemetry) cannot drift.
LEARNED_NOTE_PREFIX = "LEARNED: "

#: Length of the short content digest identifying one composed artifact.
ARTIFACT_DIGEST_CHARS = 12


def learned_notes(guidance: str) -> list[str]:
    """Return all scoped notes; legacy flat notes remain legacy_unscoped."""
    try:
        from .guidance_scope import scoped_entries
    except ImportError:  # standalone script import compatibility
        from agents.guidance_scope import scoped_entries
    return [text for _scope, text in scoped_entries(guidance)]


def artifact_digest(text: str) -> str:
    """Short, stable content digest for one composed artifact. Pure.

    Identifies WHICH note was composed without carrying its text into the
    telemetry payload (notes are untrusted steward/machine content, and the SSE
    metadata event is size-bounded). Same text ⇒ same digest across turns and
    processes, so a ledger can join usage to a specific artifact.
    """
    import hashlib
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:ARTIFACT_DIGEST_CHARS]


def build_composed_context(guidance_by_system: dict) -> dict | None:
    """Report which scoped artifacts were composed, never whether obeyed."""
    from .guidance_scope import GuidanceScope, scoped_entries
    artifacts: list[dict] = []
    systems_with_context = 0
    counts = {scope.value: 0 for scope in GuidanceScope}
    for system_id in sorted(guidance_by_system or {}):
        entries = scoped_entries(guidance_by_system.get(system_id))
        if not entries:
            continue
        systems_with_context += 1
        for scope, note in entries:
            counts[scope.value] += 1
            artifacts.append({
                "system": str(system_id)[:MAX_LABEL_LEN],
                "kind": "guidance_note",
                "scope": scope.value,
                "digest": artifact_digest(note),
            })
    if not artifacts:
        return None
    return {
        "artifacts": artifacts[:MAX_COMPOSED_ARTIFACTS],
        "artifact_count": len(artifacts),
        "systems_with_context": systems_with_context,
        "scope_counts": counts,
    }

# Failure markers match error/empty *envelopes* — NOT the bare word
# "error", which appears legitimately in manufacturing results (defect
# reasons, error codes, error_rate columns) and would otherwise mislabel
# successful queries as FAILED and corrupt the wasted-call analysis.
#
# ENVELOPE SCOPE (2026-09-24): markers are tested ONLY against the result's
# HEAD — its first ENVELOPE_HEAD_CHARS characters (lines included, so a
# wrapped traceback or a one-line preamble before "Error:" still counts) —
# and a result that carries a markdown data table is never an envelope. A
# Mode-A system agent returns a PROSE answer; matching anywhere in it
# classified an honest, complete
# answer as failed (observed live: an ERP answer ending "There are no
# results to display ... does not exist in the current dataset" — eight
# successful queries — scored failed and rendered a red "failed" node).
# Needles are word-boundary regexes (ADR-0054: never a bare substring).
# The frontend mirror is frontend/src/lib/arch-store.ts classifyToolResult;
# both read frontend/src/test/fixtures/tool-result-outcome-cases.json (each
# case pins the UI outcome and this server verdict independently).
ENVELOPE_HEAD_CHARS = 240
_EMPTY_RE = re.compile(
    r"\b(?:no results|no records|no matching|no rows|returned no rows|empty result)\b")
# `\w*` before error:/exception: keeps CamelCase envelopes (ValueError:,
# OperationalError:, McpException:) that the old substrings caught.
_ERROR_RE = re.compile(
    r"\b\w*(?:error|exception):|\berror executing\b|\bquery failed\b"
    r"|\bfailed to execute\b|\btraceback \(most recent\b")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s:\-|]+\|?$")
_RECORDS_FOUND_RE = re.compile(r"^\d+\s+records?\s+found\b", re.IGNORECASE)

#: The fixed engine line a Mode A system agent appends to its answer when no
#: data query the call ran returned data: every attempt failed or came back
#: with no rows (``system_agent._no_data_fact``). Test event 32 (2026-10-15):
#: an agent whose every query hit a service error answered in prose that
#: carries no envelope marker, so both classifiers called the call a success
#: and Sources showed it like an answered one. The line is code-authored, so
#: it is matched ANYWHERE in the result (past the envelope head, inside a JSON
#: note member, inside a repr), ahead of the table check; the parenthesis
#: holds the closed capture categories, plus ``no rows`` for an empty attempt.
#: ``no rows`` alone is an empty outcome, anything else an error. The UI
#: mirror is frontend/src/lib/arch-store.ts NO_DATA_LINE_RE (shared fixture).
NO_DATA_LINE_PREFIX = "[engine] No query this call ran returned data ("
NO_DATA_NO_ROWS = "no rows"
_NO_DATA_LINE_RE = re.compile(  # possessive repeats, "no rows" tried first so a label never stops at "no"
    r"\[engine\] No query this call ran returned data "
    r"\(((?:no rows|[a-z_]{1,40}+)(?:, (?:no rows|[a-z_]{1,40}+)){0,15}+)\)\.")
_NO_DATA_NEEDLE = "no query returned data"


def no_data_line(labels) -> str:
    """The exact engine no-data line for ``labels`` (first-seen order), or
    ``""`` when there is none to list."""
    listed = [label for label in labels or () if isinstance(label, str) and label]
    return f"{NO_DATA_LINE_PREFIX}{', '.join(listed)})." if listed else ""


def no_data_line_in(text: str) -> str:
    """The engine no-data line ``text`` carries, verbatim, or ``""``."""
    match = _NO_DATA_LINE_RE.search(text) if isinstance(text, str) else None
    return match.group(0) if match else ""


#: A copy of the line in model or data text: the prefix and, when present, its
#: parenthesis (well-formed or not). The parenthesis stops at a newline, a quote
#: or a backslash, so a copy inside a JSON string never eats its delimiter.
_NO_DATA_COPY_RE = re.compile(
    r"\[engine\] No query this call ran returned data(?: ?\([^)\n\"\\\\]{0,800}\)?\.?)?")
#: What a copy becomes: no ``[engine]`` tag and no failure needle, so neither
#: the engine-line match nor the head-scoped envelope needles can read it.
NO_DATA_COPY_TEXT = "(an engine note copied into this text was removed)"


def neutralize_no_data(text):
    """``text`` with every copy of the engine no-data line replaced whole by
    ``NO_DATA_COPY_TEXT``, so a model-written or data-carried line can never
    classify a call (its ``(no rows)`` label would still match the head-scoped
    empty needle); the engine appends its own line after this runs. A copy
    longer than that text is padded with spaces to its own length, so no text
    after it moves into the envelope head."""
    if not isinstance(text, str) or "[engine] No query this call ran returned data" not in text:
        return text
    return _NO_DATA_COPY_RE.sub(lambda m: NO_DATA_COPY_TEXT.ljust(len(m.group(0))), text)


def _no_data_marker(text: str) -> "tuple[str, str] | None":
    match = _NO_DATA_LINE_RE.search(text)
    if not match:
        return None
    return ("empty" if match.group(1) == NO_DATA_NO_ROWS else "error"), _NO_DATA_NEEDLE


def envelope_head(text: str) -> str:
    """The HEAD a failure envelope must appear in (see the marker comment):
    the first ``ENVELOPE_HEAD_CHARS`` characters of the stripped text, so
    prose deeper in an answer can never classify it."""
    return text.strip()[:ENVELOPE_HEAD_CHARS]


def carries_rows(text: str) -> bool:
    """True when the result carries data rows — never a failure envelope.

    ``N records found`` headers or a markdown table (header, separator, and
    at least one piped row) — so data VALUES ("no results", "error: E42" in
    a cell) can never classify the result (ADR-0054).
    """
    if _RECORDS_FOUND_RE.match(text.strip()):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # The line ABOVE the separator must be a piped header row — a bare
    # markdown rule ("---") under prose is not a table.
    sep = next((i for i, line in enumerate(lines)
                if i >= 1 and "-" in line and "|" in lines[i - 1]
                and _TABLE_SEPARATOR_RE.match(line)), -1)
    return sep >= 1 and any("|" in line for line in lines[sep + 1:])


def failure_marker(text: str) -> "tuple[str, str] | None":
    """``(kind, needle)`` for the envelope marker ``text`` carries, else None.

    ``kind`` is ``"error"`` or ``"empty"``; ``needle`` is the matched phrase
    (bounded, lowercased) so callers can say WHY a result classified. The
    engine no-data line counts anywhere and before the table check (see
    ``NO_DATA_LINE_PREFIX``).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    engine = _no_data_marker(text)
    if engine or carries_rows(text):
        return engine
    head = envelope_head(text).lower()
    for kind, pattern in (("error", _ERROR_RE), ("empty", _EMPTY_RE)):
        match = pattern.search(head)
        if match:
            return kind, match.group(0)[:MAX_LABEL_LEN]
    return None




def _delivery_truncation_summary(systems: list[dict]) -> "dict | None":
    """Bounded report of guidance the DELIVERY cap could not fit in the prompt.

    Composition/delivery facts only (which system, how many notes and chars did
    not fit) — never note text, and never a claim about what the model read.
    ``None`` when every system's guidance fitted.
    """
    entries: list[dict] = []
    notes_dropped = 0
    chars_dropped = 0
    for system in systems or []:
        if not isinstance(system, dict):
            continue
        report = (system.get("guidance_composition") or {}).get(
            "delivery_truncation")
        if not isinstance(report, dict) or not report.get("truncated"):
            continue
        notes_dropped += int(report.get("notes_dropped") or 0)
        chars_dropped += int(report.get("chars_dropped") or 0)
        if len(entries) < MAX_COMPOSED_ARTIFACTS:
            entries.append({
                "system": str(system.get("id") or "")[:MAX_LABEL_LEN],
                "fields": [str(f)[:MAX_LABEL_LEN]
                           for f in (report.get("fields") or [])],
                "notes_dropped": int(report.get("notes_dropped") or 0),
                "notes_truncated": int(report.get("notes_truncated") or 0),
                "chars_dropped": int(report.get("chars_dropped") or 0),
            })
    if not entries:
        return None
    return {"systems": entries, "notes_dropped": notes_dropped,
            "chars_dropped": chars_dropped}


def _delivery_truncation_from_aggregate(decoded: dict, aggregate) -> "dict | None":
    """The same summary, decoded from the seed's ONE bounded payload-level
    ``guidance_delivery_truncation`` aggregate (the serialized seed carries
    no per-system report). ``None`` when absent or empty."""
    if not decoded["entries"]:
        return None
    entries = [{
        "system": entry["system"][:MAX_LABEL_LEN],
        "fields": [str(f)[:MAX_LABEL_LEN] for f in entry["fields"]],
        "notes_dropped": entry["notes_dropped"],
        "notes_truncated": entry["notes_truncated"],
        "chars_dropped": entry["chars_dropped"],
    } for entry in decoded["entries"][:MAX_COMPOSED_ARTIFACTS]]
    out = {"systems": entries,
           "notes_dropped": int(aggregate.get("notes_dropped") or 0),
           "chars_dropped": int(aggregate.get("chars_dropped") or 0)}
    if decoded["systems_omitted"]:
        out["systems_omitted"] = decoded["systems_omitted"]
    return out


def _shared_guidance_delivered(systems: list[dict], shared) -> dict:
    """``{section_key: [system ids]}`` for systems whose base was DELIVERED
    through a non-empty shared seed section (bounded per section)."""
    from .guidance_scope import shared_reference_key
    if not isinstance(shared, dict):
        return {}
    delivered: dict = {}
    for system in systems or []:
        if not isinstance(system, dict):
            continue
        key = shared_reference_key(system.get("query_guidance"))
        if key and isinstance(shared.get(key), str) and shared[key]:
            ids = delivered.setdefault(str(key)[:MAX_LABEL_LEN], [])
            if len(ids) < MAX_COMPOSED_ARTIFACTS:
                ids.append(str(system.get("id") or "")[:MAX_LABEL_LEN])
    return delivered


def _system_drop_report(system: dict, decoded: "dict | None") -> "tuple[dict | None, bool]":
    """``(report, verifiable)`` for one system.

    In-memory payloads (and seeds serialized without the aggregate) carry the
    report on the system's own ``guidance_composition``. A serialized seed
    carries it in the payload aggregate (``decoded``); there a system with a
    malformed row, or ANY unlisted system once rows were omitted, cannot be
    verified either way.
    """
    sid = str(system.get("id") or "")
    own = (system.get("guidance_composition") or {}).get("delivery_truncation")
    if decoded is None:
        return own, True
    if sid in decoded["unverified"]:
        return None, False
    listed = next((e for e in decoded["entries"] if e["system"] == sid), None)
    if listed is not None:
        return listed, True
    if own is not None:
        return own, True
    return None, not decoded["systems_omitted"]


def build_composed_context_from_metadata(systems: list[dict],
                                         delivery_aggregate=None,
                                         shared_guidance=None) -> dict | None:
    """Report scoped composition from digest-only route metadata.

    A dropped note never reached the prompt: it is excluded from ``artifacts``
    and reported under ``delivery_truncation``. Reports come from each
    system's own ``guidance_composition`` or, for a serialized seed, from
    ``delivery_aggregate``; dropped notes are identified EXACTLY from per-scope
    counts (``guidance_scope.dropped_note_digests``), and a system whose drops
    cannot be verified contributes NO artifacts (``accounting: "incomplete"``,
    ``unverified_systems``). ``shared_guidance`` sections are reported as
    delivered for every referencing system.
    """
    from .guidance_scope import GuidanceScope, decode_delivery_aggregate
    counts = {scope.value: 0 for scope in GuidanceScope}
    decoded = (decode_delivery_aggregate(delivery_aggregate)
               if delivery_aggregate is not None else None)
    artifacts, systems_with_context, unverified, withheld = _collect_note_artifacts(
        systems, decoded, counts)
    truncation = ((_delivery_truncation_from_aggregate(decoded, delivery_aggregate)
                   if decoded is not None else None)
                  or _delivery_truncation_summary(systems))
    if unverified:
        truncation = dict(truncation or {"systems": [], "notes_dropped": 0,
                                         "chars_dropped": 0})
        truncation.update(accounting="incomplete", unverified_notes=withheld,
                          unverified_systems=unverified[:MAX_COMPOSED_ARTIFACTS])
    shared = _shared_guidance_delivered(systems, shared_guidance)
    if not artifacts and truncation is None and not shared:
        return None
    out = {"artifacts": artifacts[:MAX_COMPOSED_ARTIFACTS],
           "artifact_count": len(artifacts),
           "systems_with_context": systems_with_context,
           "scope_counts": counts}
    if truncation is not None:
        out["delivery_truncation"] = truncation
    if shared:
        out["shared_guidance"] = shared
    return out


def _collect_note_artifacts(systems, decoded, counts: dict):
    """``(artifacts, systems_with_context, unverified_ids, withheld_notes)``."""
    artifacts: list[dict] = []
    systems_with_context = withheld = 0
    unverified: list[str] = []
    for system in systems or []:
        if not isinstance(system, dict):
            continue
        report, verifiable = _system_drop_report(system, decoded)
        added = _system_note_artifacts(system, report, counts) if verifiable else None
        if added is None:
            notes = _note_digest_count(system)
            if notes:
                withheld += notes
                unverified.append(str(system.get("id") or "")[:MAX_LABEL_LEN])
            continue
        artifacts.extend(added)
        systems_with_context += int(bool(added))
    return artifacts, systems_with_context, unverified, withheld


def _note_digest_count(system: dict) -> int:
    digests = (system.get("guidance_composition") or {}).get("scope_digests") or {}
    return sum(len(v) for v in digests.values() if isinstance(v, list))


def _system_note_artifacts(system: dict, report, counts: dict) -> "list[dict] | None":
    """One system's DELIVERED note artifacts, or ``None`` when its dropped
    notes cannot be identified exactly (the caller then withholds them all).
    Increments ``counts`` per scope."""
    from .guidance_scope import GuidanceScope, dropped_note_digests
    digests = (system.get("guidance_composition") or {}).get("scope_digests") or {}
    dropped = dropped_note_digests(digests, report) if report else set()
    if dropped is None:
        return None
    artifacts = []
    for scope in GuidanceScope:
        for digest in digests.get(scope.value) or []:
            if digest in dropped:
                continue
            counts[scope.value] += 1
            artifacts.append({
                "system": str(system.get("id") or "")[:MAX_LABEL_LEN],
                "kind": "guidance_note", "scope": scope.value,
                "digest": str(digest)[:ARTIFACT_DIGEST_CHARS],
            })
    return artifacts


def is_graph_query(name: str) -> bool:
    """True for the Neptune knowledge-graph traversal tool, whether it arrives
    as the bare in-process name (``explore_graph``, Mode-B / orchestrator) or
    the gateway-prefixed name the AgentCore Gateway emits in agent mode
    (``digital-thread___explore_graph``). Matching only the bare name made the
    ``neptune_queries`` counter always report 0 under ``--query-mode agent``."""
    return name == "explore_graph" or name.endswith("___explore_graph")


def is_failed_result(text: str) -> bool:
    """True when a tool-result envelope signals an error or an empty result.

    Case-insensitive. Envelope-scoped: the word-boundary ``_ERROR_RE`` /
    ``_EMPTY_RE`` needles are tested only against ``envelope_head`` and never
    against a result carrying data rows — deliberately NOT the bare word
    "error" (see the marker comment above). Non-string input is defensively
    not-failed.
    """
    return failure_marker(text) is not None


def classify_empty_join(envelopes: dict) -> str | None:
    """PURE empty-join classifier (ADR-0059 shape 2, hook helper).

    ``envelopes`` maps system_id -> that system's tool-result text for one
    cross-system turn. Returns the system id of the EMPTY/FAILED leg iff
    exactly one leg failed while at least one other leg returned rows —
    the shape that suggests an identifier translation gap between the two.
    Single-system turns, all-legs-failed (systemic outage, not a join
    problem) and all-legs-healthy turns classify as None.
    """
    if not isinstance(envelopes, dict) or len(envelopes) < 2:
        return None
    failed = [s for s, text in envelopes.items() if is_failed_result(text)]
    if len(failed) == 1 and len(envelopes) - len(failed) >= 1:
        return failed[0]
    return None


def count_wasted_calls(results: Iterable) -> int:
    """Count results matching the failure envelope across ``results``.

    Deterministic, no I/O. Non-string entries are skipped defensively (tool
    results are free-form), mirroring ``usage_tracking.extract_rule_node_ids``.
    """
    count = 0
    for res in results or []:
        if isinstance(res, str) and is_failed_result(res):
            count += 1
    return count


def _endpoint(system: str, field: str, value: str) -> str:
    """``system.field 'value'`` fragment — any part may be missing."""
    loc = ".".join(p for p in (system, field) if p)
    if value:
        quoted = f"'{value[:40]}'"
        return f"{loc} {quoted}" if loc else quoted
    return loc


def format_rule_label(row: dict) -> str:
    """Human label for an accepted proposal row — pure, defensive.

    Renders ``proposed_type`` plus the from/to endpoints
    (``from_system``/``from_field``/``from_value`` →
    ``to_system``/``to_field``/``to_value``) and the ``concept`` when present.
    One-sided rows (e.g. ``field_annotation``) render their single endpoint.
    Every field read is defensive (missing/None/non-str tolerated); the result
    is truncated to ``MAX_LABEL_LEN``.
    """
    if not isinstance(row, dict):
        return ""

    def _s(key: str) -> str:
        val = row.get(key, "")
        return val if isinstance(val, str) else ("" if val is None else str(val))

    kind = (_s("proposed_type") or "rule").replace("_", " ")
    left = _endpoint(_s("from_system"), _s("from_field"), _s("from_value"))
    right = _endpoint(_s("to_system"), _s("to_field"), _s("to_value"))
    concept = _s("concept")

    if left and right:
        body = f"{left} -> {right}"
    else:
        body = left or right

    label = f"{kind}: {body}" if body else kind
    if concept:
        label += f" (concept: {concept})"
    return label[:MAX_LABEL_LEN]


def _capped_planned_with(planned_with: dict) -> dict:
    """Validate/cap the additive ``planned_with`` block (pure, defensive).

    Counts are coerced to ints ≥ 0 (non-numeric ⇒ 0); ``shared_concepts``
    keeps only non-empty strings, capped at ``MAX_PLANNED_CONCEPTS`` entries
    of ``MAX_PLANNED_CONCEPT_LEN`` chars each.

    ``authority_rules`` (T1.6, additive) is HONEST-NULL: present only when
    the input carries a real numeric count (the runtime measured it from the
    route seed); a missing or non-numeric input stays ABSENT — unknown is
    never rendered as a fabricated zero (ADR-0053/0045). An input of 0 is an
    honest measurement (seed consulted authority rules, none loaded) and
    passes through. No new cap constant: the count is bounded upstream by
    the seed builder's per-kind rule cap.
    """
    def _count(key: str) -> int:
        value = planned_with.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    concepts: list[str] = []
    for item in planned_with.get("shared_concepts") or []:
        if isinstance(item, str) and item:
            concepts.append(item[:MAX_PLANNED_CONCEPT_LEN])
        if len(concepts) >= MAX_PLANNED_CONCEPTS:
            break
    capped = {
        "systems": _count("systems"),
        "shared_concepts": concepts,
        "join_paths": _count("join_paths"),
        "value_mappings": _count("value_mappings"),
        "identity_rules": _count("identity_rules"),
    }
    authority = planned_with.get("authority_rules")
    if isinstance(authority, (int, float)) and not isinstance(authority, bool):
        capped["authority_rules"] = max(0, int(authority))
    if planned_with.get("tryit_candidate") is True:
        capped["tryit_candidate"] = True
    return capped


def _capped_rules_applied(rules_applied: list[dict]) -> list[dict]:
    """Cap ``rules_applied`` to ``MAX_RULES_APPLIED`` defensive ``{id, label}``
    rows (labels ≤ ``MAX_LABEL_LEN``; missing label falls back to the id)."""
    capped: list[dict] = []
    for entry in (rules_applied or [])[:MAX_RULES_APPLIED]:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("id", "")
        rule_id = rule_id if isinstance(rule_id, str) else str(rule_id or "")
        label = entry.get("label", "")
        label = label if isinstance(label, str) else str(label or "")
        capped.append({"id": rule_id, "label": (label or rule_id)[:MAX_LABEL_LEN]})
    return capped


def _attach_optional_keys(summary: dict, determinism: dict | None,
                          planned_with: dict | None,
                          cost: dict | None,
                          composed_context: dict | None = None) -> dict:
    """Attach the ADDITIVE optional summary keys in place (returns summary).

    Extracted from ``build_intelligence_summary`` (50-line gate); the
    semantics of each key are documented on the builder — this helper only
    hosts the attachment mechanics. Every key is absent unless its input is
    provided/truthy, so the pinned base key-set never changes.

    ``planned_with``: the Mode-A route-seed routing summary captured by
    ``runtime_app._planned_with_from_seed``, validated/capped via
    :func:`_capped_planned_with` (whose ``authority_rules`` sub-key is T1.6
    honest-null).

    ``cost`` (E7 / ADR-0053): the pinned MEASURED orchestrator-model usage
    payload built by ``runtime_app._usage_cost_payload`` (scope
    ``orchestrator-model-only``) — never zeros-as-absence (honest-data rule:
    absence = absent, measured-only).

    ``composed_context``: the artifact-usage block built by
    :func:`build_composed_context` — WHICH learned artifacts reached this
    turn's prompt (orchestrator scope), the evidence ``rules_applied`` cannot
    give for prompt-text artifacts.
    """
    if determinism is not None:
        summary["grounded_steps"] = int(determinism.get("deterministic_calls") or 0)
        summary["deterministic_pct"] = int(determinism.get("deterministic_pct") or 0)
        summary["wasted_calls"] = len(determinism.get("wasted_steps") or [])
    if planned_with:
        summary["planned_with"] = _capped_planned_with(planned_with)
    if cost:
        summary["cost"] = dict(cost)
    if composed_context:
        summary["composed_context"] = dict(composed_context)
    return summary


def build_intelligence_summary(*, tool_names: list[str], wasted_calls: int,
                               rules_applied: list[dict],
                               new_proposed_mappings: int = 0,
                               determinism: dict | None = None,
                               planned_with: dict | None = None,
                               cost: dict | None = None,
                               composed_context: dict | None = None) -> dict:
    """Build the intelligence-summary payload (the ONE shape both emitters use).

    Keys (exactly — ``deterministic_pct`` / ``graph_guided_steps`` are gone,
    not aliased): ``total_tool_calls``, ``neptune_queries``,
    ``system_queries`` (= total − neptune), ``graph_coverage_pct``
    (neptune-only: ``round(100 * neptune / max(total, 1))``), ``wasted_calls``,
    ``rules_applied`` (≤ ``MAX_RULES_APPLIED`` entries of
    ``{"id", "label"}``, labels ≤ ``MAX_LABEL_LEN``),
    ``new_proposed_mappings``. Zero-safe on empty input.

    ``determinism`` (optional): the mechanical
    ``determinism.score_tool_calls`` result. When provided, the payload gains
    the ADDITIVE keys ``grounded_steps`` (= ``deterministic_calls``) and
    ``deterministic_pct`` (COMPUTED, never LLM-judged — distinct from the
    deleted LLM-era key of the same name), and ``wasted_calls`` is OVERRIDDEN
    with the mechanical count (``len(wasted_steps)`` — hard evidence only).
    Absent/None ⇒ byte-identical output to the legacy call.

    The other optional keys — ``planned_with``, ``cost``,
    ``composed_context`` — are documented on :func:`_attach_optional_keys`;
    each is ABSENT unless its input is truthy, so the pinned base key-set
    never changes.
    """
    names = [n for n in (tool_names or []) if isinstance(n, str)]
    total = len(names)
    neptune = sum(1 for n in names if is_graph_query(n))

    summary = {
        "total_tool_calls": total,
        "neptune_queries": neptune,
        "system_queries": total - neptune,
        "graph_coverage_pct": round(100 * neptune / max(total, 1)),
        "wasted_calls": int(wasted_calls or 0),
        "rules_applied": _capped_rules_applied(rules_applied),
        "new_proposed_mappings": int(new_proposed_mappings or 0),
    }
    return _attach_optional_keys(summary, determinism, planned_with, cost,
                                 composed_context)
