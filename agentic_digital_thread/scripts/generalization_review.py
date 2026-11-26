#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""generalization_review.py — deployment-learnings generalization review (ADR-0060).

A SCRIPT, not a service: it reads a graph-snapshot export file
(``adt-graph-snapshot/1`` — the ``scripts/graph_snapshot.py export`` output),
classifies each learned artifact as customer-specific vs dialect-generic vs
unclear with COMPUTED heuristics, and writes one markdown review report a
human reads before PRing anything. It files nothing, promotes nothing, and
writes to no deployment, graph, or engine file — the only output is the
report. Live-API mode (``--base-url``) is the ADR-0060 D2 follow-on; snapshot
mode needs zero new API and ships first.

Usage:
    python3 scripts/generalization_review.py --snapshot graph.json \
        [--out report.md] [--include-text] [--llm-assist] [--llm-model <id>]

Inputs (read-only projections of the learned set, ADR-0060 D2):
- promoted query guidance — each System node's ``query_guidance``, split by
  the single-source ``LEARNED:`` parser (``agents/query_intelligence.py``,
  loaded standalone);
- steward-confirmed rules — JoinPath / ValueMapping / IdentityRule nodes
  whose ``method`` is in the privileged set the orchestrator already trusts;
- business context — hash + length ONLY by default (untrusted steward prose,
  ADR-0054); ``--include-text`` opts in explicitly.

Classification (ADR-0060 D3): customer-specific markers are DERIVED from the
deployment being analyzed (system ids/names, site literals, quoted rule
values) — never a hardcoded list. Dialect-generic markers are engine
vocabulary (temporal literals, quoting, pagination, retention, SQL keywords)
plus connector TYPE names from ``agents/connection_types.py``. Anything else
is ``unclear`` — never silently generic. ``--llm-assist`` adds an ADVISORY
suggestion column for guidance-note text only (rule rows quote sampled
production values, which never enter a model prompt — claim-check policy);
model code is imported lazily inside ``classify_with_llm`` so a run without
the flag never touches it.

Conventions mirror ``graph_snapshot.py``: stdlib-only module top level,
import-pure, loadable standalone via importlib.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve()
_ADT_ROOT = _HERE.parents[1]          # agentic_digital_thread/

#: Snapshot file format this lane accepts (graph_snapshot.py export output).
SNAPSHOT_FORMAT = "adt-graph-snapshot/1"

#: The steward-confirmed method set the orchestrator already privileges as
#: applied truth (see the route-seed ordering in agents/server.py).
CONFIRMED_METHODS = ("crosswalk_confirmed", "user_confirmed")

#: Graph node labels that carry accepted, steward-confirmed rules.
RULE_LABELS = ("IdentityRule", "JoinPath", "ValueMapping")

#: Graph node labels whose id/name are site literals (deployment markers).
SITE_LABELS = ("Plant", "Site")

VERDICT_CUSTOMER = "customer-specific"
VERDICT_GENERIC = "dialect-generic"
VERDICT_UNCLEAR = "unclear"
HASH_ONLY_VERDICT = "not-classified (hash-only)"

#: Rule props whose VALUES are deployment markers (quoted values, prefixes,
#: and the system ids the rule bridges).
_VALUE_MARKER_KEYS = (
    "from_system", "from_value", "prefix", "source_prefix", "source_system",
    "target_prefix", "target_system", "to_system", "to_value",
)
#: Markers shorter than this (or digit-only) are too noisy to match on.
_MARKER_MIN_LEN = 3
#: Rendered markdown cell budget (whitespace-collapsed, pipe-escaped).
_CELL_MAX_LEN = 160
#: max_tokens for the optional advisory model call (mandatory by project rule).
_ASSIST_MAX_TOKENS = 1500

#: Dialect-generic marker patterns — ENGINE vocabulary only (SQL/connector
#: behavior), never customer terms. The sql-keyword class is deliberately
#: case-sensitive: uppercase keywords signal dialect prose, lowercase prose
#: words ("where", "select") do not.
_DIALECT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("temporal-literal",
     r"(?i)\b(temporal|timestamp|datetime|date|time)[ -]"
     r"(literal|format|filter|predicate|column|value)s?\b|\bTIMESTAMP\b"),
    ("quoting",
     r"(?i)\b(quote|quoted|quoting|backtick|unquoted)\b"
     r"|\b(double|single)[- ]quot\w+"),
    ("pagination",
     r"(?i)\bpaginat\w+\b|\bpage size\b|\bnext[_ ]?token\b"),
    ("retention", r"(?i)\bretention\b"),
    ("sql-keyword",
     r"\b(CAST|COALESCE|INTERVAL|VARCHAR|GROUP BY|ORDER BY|ILIKE"
     r"|LIMIT|OFFSET)\b"),
    ("dialect-vocabulary",
     r"(?i)\bdialect\b|\bsyntax\b|\bcase[- ]sensitiv\w+\b"
     r"|\bschema[- ]qualif\w+\b|\bpartition\w*\b"),
)

_LLM_LABELS = (VERDICT_CUSTOMER, VERDICT_GENERIC, VERDICT_UNCLEAR)

_LLM_SYSTEM_PROMPT = (
    "You label short learned-guidance notes from a data platform. For each "
    "numbered note, answer with exactly one label: 'customer-specific' (the "
    "note names one particular deployment's systems, sites, or data values), "
    "'dialect-generic' (the note describes engine, SQL-dialect, or connector "
    "behavior that would apply to any deployment), or 'unclear'. Reply with "
    "a single JSON object mapping each note number to its label and nothing "
    "else."
)


# ── standalone loads of single-source engine vocabulary ─────────────────────

def _load_agents_module(filename: str, alias: str):
    """Load one agents/ module standalone via importlib (stdlib-only top
    levels only — the graph_snapshot.py ``_load_core`` pattern)."""
    if alias in sys.modules:
        return sys.modules[alias]
    path = _ADT_ROOT / "agents" / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[alias] = module
    return module


def learned_notes_parser():
    """The single-source ``LEARNED:`` note parser — never re-implemented here
    (the format is single-sourced in query_intelligence)."""
    return _load_agents_module(
        "query_intelligence.py", "adt_query_intelligence_core").learned_notes


def connector_type_markers() -> tuple[str, ...]:
    """Connector TYPE names from ``agents/connection_types.py`` — engine
    vocabulary; a note naming a TYPE (not an instance) is generic evidence."""
    module = _load_agents_module(
        "connection_types.py", "adt_connection_types_core")
    names = set(getattr(module, "NON_SQL_CONNECTION_TYPES", ()) or ())
    for attr in ("POSTGRESQL", "KNOWLEDGE_GRAPH_CONNECTION_TYPE"):
        value = getattr(module, attr, "")
        if value:
            names.add(value)
    return tuple(sorted(names))


# ── snapshot loading ─────────────────────────────────────────────────────────

def load_from_snapshot(path: str) -> dict:
    """Read-only projection of one snapshot file. Refuses any format other
    than SNAPSHOT_FORMAT. Returns {systems, rules, sites, source}."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    fmt = payload.get("format") if isinstance(payload, dict) else None
    if fmt != SNAPSHOT_FORMAT:
        raise ValueError(
            f"unsupported snapshot format {fmt!r} — expected {SNAPSHOT_FORMAT!r}")
    systems, rules, sites = [], [], []
    for node in payload.get("nodes", []) or []:
        labels = set(node.get("labels", []) or [])
        props = dict(node.get("props", {}) or {})
        if "System" in labels and props.get("id"):
            systems.append({
                "system_id": str(props["id"]),
                "name": str(props.get("name") or ""),
                "query_guidance": str(props.get("query_guidance") or ""),
                "business_context": str(props.get("business_context") or ""),
                "business_context_enabled":
                    bool(props.get("business_context_enabled")),
            })
        rule_labels = sorted(labels & set(RULE_LABELS))
        if rule_labels and props.get("method") in CONFIRMED_METHODS:
            rules.append({"kind": rule_labels[0], "props": props})
        if labels & set(SITE_LABELS):
            sites.append(props)
    systems.sort(key=lambda s: s["system_id"])
    rules.sort(key=lambda r: (r["kind"], str(r["props"].get("id") or "")))
    return {"systems": systems, "rules": rules, "sites": sites,
            "source": f"snapshot `{path}` "
                      f"(exported_at {payload.get('exported_at', '?')})"}


# ── computed classification (pure) ───────────────────────────────────────────

def _usable_marker(marker: str) -> bool:
    marker = (marker or "").strip()
    return len(marker) >= _MARKER_MIN_LEN and not marker.isdigit()


def deployment_marker_set(systems: list[dict], rules: list[dict],
                          sites: list[dict]) -> set[str]:
    """Customer-specific markers DERIVED from the analyzed deployment —
    system ids/names, site literals, quoted rule values/prefixes. Empty
    input yields an empty set: nothing is ever hardcoded (ADR-0060 D3)."""
    markers: set[str] = set()
    for system in systems:
        markers.add(str(system.get("system_id") or ""))
        markers.add(str(system.get("name") or ""))
    for site in sites:
        markers.add(str(site.get("id") or ""))
        markers.add(str(site.get("name") or ""))
    for rule in rules:
        props = rule.get("props", {}) or {}
        markers.update(str(props.get(key) or "") for key in _VALUE_MARKER_KEYS)
    return {m.strip() for m in markers if _usable_marker(m)}


def _marker_hits(text: str, markers) -> list[str]:
    """Word-boundary marker matches (alnum lookarounds — snake_case-safe),
    case-insensitive, deterministic order."""
    hits = []
    for marker in sorted(markers, key=lambda m: (m.lower(), m)):
        pattern = (r"(?<![A-Za-z0-9])" + re.escape(marker)
                   + r"(?![A-Za-z0-9])")
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(marker)
    return hits


def classify_note(text: str, deployment_markers,
                  connector_types=()) -> dict:
    """Computed verdict for one artifact text. Customer markers WIN over
    dialect vocabulary (safer: a borderline row stays in the deployment);
    anything matching neither falls to unclear — never silently to generic."""
    text = text or ""
    customer_hits = _marker_hits(text, deployment_markers)
    if customer_hits:
        return {"verdict": VERDICT_CUSTOMER,
                "matched": [f"deployment:{m}" for m in customer_hits]}
    dialect_hits = [f"dialect:{name}" for name, pattern in _DIALECT_PATTERNS
                    if re.search(pattern, text)]
    dialect_hits += [f"connector-type:{m}"
                     for m in _marker_hits(text, connector_types)]
    if dialect_hits:
        return {"verdict": VERDICT_GENERIC, "matched": sorted(dialect_hits)}
    return {"verdict": VERDICT_UNCLEAR, "matched": []}


# ── row builders ─────────────────────────────────────────────────────────────

def _rule_text(props: dict) -> str:
    """Deterministic human-readable rendering of one rule node's props."""
    keys = ("from_system", "from_table", "from_field", "from_value",
            "to_system", "to_table", "to_field", "to_value",
            "source_system", "target_system", "prefix", "source_prefix",
            "target_prefix", "concept", "transform", "time_key")
    parts = [f"{key}={props[key]}" for key in keys
             if props.get(key) not in (None, "")]
    return "; ".join(parts) or "(no renderable properties)"


def guidance_rows(systems, markers, connector_types, notes_of) -> list[dict]:
    rows = []
    for system in systems:
        for note in notes_of(system.get("query_guidance") or ""):
            verdict = classify_note(note, markers, connector_types)
            rows.append({"system": system["system_id"], "text": note,
                         **verdict})
    return rows


def rule_rows(rules, markers, connector_types) -> list[dict]:
    rows = []
    for rule in rules:
        text = _rule_text(rule.get("props", {}) or {})
        verdict = classify_note(text, markers, connector_types)
        rows.append({"kind": rule["kind"], "text": text,
                     "method": str(rule["props"].get("method") or ""),
                     **verdict})
    return rows


def context_rows(systems, markers, connector_types,
                 include_text: bool) -> list[dict]:
    """Business context is untrusted steward prose (ADR-0054): hash + length
    only by default; classification happens only with --include-text."""
    rows = []
    for system in systems:
        text = system.get("business_context") or ""
        if not text:
            continue
        row = {"system": system["system_id"],
               "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
               "length": len(text),
               "enabled": bool(system.get("business_context_enabled"))}
        if include_text:
            row["text"] = text
            row.update(classify_note(text, markers, connector_types))
        else:
            row.update(text="", verdict=HASH_ONLY_VERDICT, matched=[])
        rows.append(row)
    return rows


def build_rows(loaded: dict, *, include_text: bool = False,
               connector_types=None, notes_of=None) -> dict:
    """Assemble all report rows from one load_from_snapshot() projection."""
    markers = deployment_marker_set(
        loaded["systems"], loaded["rules"], loaded["sites"])
    if connector_types is None:
        connector_types = connector_type_markers()
    if notes_of is None:
        notes_of = learned_notes_parser()
    return {
        "guidance": guidance_rows(loaded["systems"], markers,
                                  connector_types, notes_of),
        "rules": rule_rows(loaded["rules"], markers, connector_types),
        "context": context_rows(loaded["systems"], markers, connector_types,
                                include_text),
        "markers": sorted(markers),
    }


# ── optional LLM assist (advisory only; lazy imports — flag-gated) ──────────

def parse_llm_labels(reply: str, count: int) -> list[str]:
    """Parse advisory labels out of a model reply. Anything unparseable or
    outside the allowed label set becomes '' (no suggestion) — the advisory
    column NEVER invents a verdict (ADR-0060 D3)."""
    labels = [""] * count
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not match:
        return labels
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return labels
    if not isinstance(data, dict):
        return labels
    for key, value in data.items():
        if str(key).isdigit() and int(key) < count and value in _LLM_LABELS:
            labels[int(key)] = value
    return labels


def classify_with_llm(texts: list[str], model_id: str = "") -> list[str]:
    """Advisory suggestion labels for guidance-note TEXT only (never rule
    rows — those quote sampled production values, claim-check policy).
    Model code is imported HERE, lazily: a run without --llm-assist never
    imports it (pinned by test). build_model gets a mandatory max_tokens."""
    if not texts:
        return []
    if str(_ADT_ROOT) not in sys.path:
        sys.path.insert(0, str(_ADT_ROOT))
    from agents.model_catalog import AGENT, default_model  # lazy: flag-gated
    from agents.model_provider import build_model          # lazy: flag-gated
    from strands import Agent                              # lazy: flag-gated
    model = build_model(model_id or default_model(AGENT),
                        max_tokens=_ASSIST_MAX_TOKENS)
    payload = json.dumps({str(i): text for i, text in enumerate(texts)},
                         ensure_ascii=False)
    agent = Agent(model=model, tools=[], system_prompt=_LLM_SYSTEM_PROMPT)
    return parse_llm_labels(str(agent(payload)), len(texts))


def _assist_or_warn(texts: list[str], model_id: str):
    """Best-effort advisory call — a model failure degrades to no column."""
    try:
        return classify_with_llm(texts, model_id)
    except Exception as exc:  # noqa: BLE001 - advisory only, never gates
        print(f"WARNING: --llm-assist failed ({exc}); "
              "report proceeds without the suggestion column")
        return None


# ── report rendering ─────────────────────────────────────────────────────────

def _cell(value, limit: int = _CELL_MAX_LEN) -> str:
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text.replace("|", "\\|")


def _table(header: list[str], body_rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in body_rows]
    return lines


def _render_header(rows: dict, source: str, today: str) -> list[str]:
    return [
        "# Generalization review — deployment learnings vs engine candidates",
        "",
        f"- Source: {source}",
        f"- Generated: {today}",
        f"- Learned guidance notes: {len(rows['guidance'])} · "
        f"steward-confirmed rules: {len(rows['rules'])} · "
        f"business-context entries: {len(rows['context'])}",
        f"- Deployment-derived markers: {len(rows['markers'])} "
        "(derived from THIS snapshot — never a hardcoded list)",
        "",
        "**How to read this report.** Every verdict is a COMPUTED heuristic; "
        "the matched markers shown per row are its full evidence. Where "
        "present, the LLM suggestion column is ADVISORY only and gates "
        "nothing. **The human decides**: this lane files nothing, promotes "
        "nothing, and writes to no deployment, graph, or engine file — "
        "promotion is a human PR (checklist at the end).",
        "",
    ]


def _render_guidance(rows: list[dict], suggestions) -> list[str]:
    out = ["## Learned query guidance (`LEARNED:` notes)", ""]
    if not rows:
        return out + ["_None found._", ""]
    with_llm = suggestions is not None
    header = ["#", "system", "note", "heuristic verdict", "matched markers"]
    if with_llm:
        header.append("LLM suggestion")
    header.append("candidate engine home")
    body = []
    for i, row in enumerate(rows):
        cells = [str(i + 1), _cell(row["system"]), _cell(row["text"]),
                 row["verdict"], _cell(", ".join(row["matched"])) or "—"]
        if with_llm:
            cells.append(suggestions[i] or "—")
        cells.append("")  # blank on purpose — the human fills it (D4)
        body.append(cells)
    return out + _table(header, body) + [""]


def _render_rules(rows: list[dict]) -> list[str]:
    out = ["## Steward-confirmed rules "
           "(JoinPath / ValueMapping / IdentityRule)", "",
           "_No LLM suggestions here: rule rows quote sampled production "
           "values, which never enter a model prompt (claim-check policy)._",
           ""]
    if not rows:
        return out + ["_None found._", ""]
    header = ["#", "kind", "rule", "method", "heuristic verdict",
              "matched markers", "candidate engine home"]
    body = [[str(i + 1), row["kind"], _cell(row["text"]), row["method"],
             row["verdict"], _cell(", ".join(row["matched"])) or "—", ""]
            for i, row in enumerate(rows)]
    return out + _table(header, body) + [""]


def _render_context(rows: list[dict], include_text: bool) -> list[str]:
    out = ["## Business context", "",
           "_Untrusted steward prose (ADR-0054) — hash and length only by "
           "default; `--include-text` opts in and keeps the report inside "
           "the deployment trust boundary._", ""]
    if not rows:
        return out + ["_None found._", ""]
    header = ["system", "sha256 (first 16)", "length", "enabled",
              "heuristic verdict"]
    if include_text:
        header += ["text", "matched markers"]
    body = []
    for row in rows:
        cells = [_cell(row["system"]), row["sha256"], str(row["length"]),
                 "yes" if row["enabled"] else "no", row["verdict"]]
        if include_text:
            cells += [_cell(row["text"]),
                      _cell(", ".join(row["matched"])) or "—"]
        body.append(cells)
    return out + _table(header, body) + [""]


def _render_footer() -> list[str]:
    return [
        "## Promotion checklist (dialect-generic candidates only)",
        "",
        "Before ANY candidate enters engine defaults (the observed "
        "precedent: a learned dialect rule lifted by human PR into the "
        "engine prompt):",
        "",
        "1. **Write the engine test first** — pin the behavior the rule "
        "encodes.",
        "2. **Run the neutrality ratchet** "
        "(`tests/unit/test_engine_domain_neutrality.py`) — it must stay "
        "green with the candidate in place.",
        "3. **Open a human PR** — this lane never auto-promotes; the report "
        "is a review aid, not a change.",
        "",
        "**Neutrality reminder (ADR-0044):** customer-specific rows must "
        "NEVER enter engine defaults — they stay in the deployment that "
        "earned them. When in doubt, a row is customer-specific.",
        "",
    ]


def render_report(rows: dict, source: str, *, include_text: bool = False,
                  llm_suggestions=None, today: str = "") -> str:
    today = today or date.today().isoformat()
    lines = _render_header(rows, source, today)
    lines += _render_guidance(rows["guidance"], llm_suggestions)
    lines += _render_rules(rows["rules"])
    lines += _render_context(rows["context"], include_text)
    lines += _render_footer()
    return "\n".join(lines)


# ── entrypoint ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--snapshot", required=True,
        help=f"graph-snapshot export file ({SNAPSHOT_FORMAT} — "
             "scripts/graph_snapshot.py export)")
    parser.add_argument(
        "--out", default="",
        help="report path (default: generalization-review-<date>.md)")
    parser.add_argument(
        "--include-text", action="store_true",
        help="include business-context TEXT (untrusted steward prose; "
             "hash+length only without this flag)")
    parser.add_argument(
        "--llm-assist", action="store_true",
        help="add an ADVISORY suggestion column for guidance notes "
             "(model code is imported only with this flag)")
    parser.add_argument(
        "--llm-model", default="",
        help="model id for --llm-assist (default: the catalog agent default)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        loaded = load_from_snapshot(args.snapshot)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    rows = build_rows(loaded, include_text=args.include_text)
    suggestions = None
    if args.llm_assist:
        suggestions = _assist_or_warn(
            [row["text"] for row in rows["guidance"]], args.llm_model)
    report = render_report(rows, loaded["source"],
                           include_text=args.include_text,
                           llm_suggestions=suggestions)
    out_path = args.out or f"generalization-review-{date.today().isoformat()}.md"
    Path(out_path).write_text(report, encoding="utf-8")
    print(f"Report written to {out_path} "
          f"(guidance={len(rows['guidance'])} rules={len(rows['rules'])} "
          f"context={len(rows['context'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
