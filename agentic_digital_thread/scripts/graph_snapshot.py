#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""graph_snapshot.py — export / import / diff the ADT knowledge graph.

Operational backup/restore tooling around the admin snapshot routes
(``agents/graph_snapshot_routes.py``), plus a fully OFFLINE semantic diff.
Distinct from the ADR-0057 ontology export (not implemented) — this is a raw
operational copy of the entire graph.

Usage:
    # Export the entire graph to a JSON snapshot file
    python3 scripts/graph_snapshot.py export \
        --base-url https://<app> --password "$ADT_PASSWORD" --out graph.json

    # DESTRUCTIVE: wipe the graph and restore it from a snapshot file
    python3 scripts/graph_snapshot.py import \
        --base-url https://<app> --password "$ADT_PASSWORD" \
        --file graph.json --yes

    # Offline semantic diff of two snapshot files (no network)
    python3 scripts/graph_snapshot.py diff a.json b.json [--report out.md]

Auth reuses the same cookie-login client every other script lane uses
(``scripts/smoke_prompts.App`` — POST /api/auth/login). Export walks the
paginated ``GET /api/admin/graph/snapshot?cursor=...`` chunks (each bounded,
CloudFront-timeout-safe) and assembles the file locally; import passes the
literal ``confirm="wipe-and-restore"`` token and polls the background job.

The diff normalizes volatile properties before comparing — timestamps /
created_at / updated_at, AWS ARNs, runtime ids, gateway target ids — and
compares float properties with tolerance 1e-6. Exit 0 when the two files are
semantically identical after normalization, 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_ADT_ROOT = _HERE.parents[1]          # agentic_digital_thread/
_REPO_ROOT = _HERE.parents[2]         # repository root (scripts/smoke_prompts.py)

FLOAT_TOLERANCE = 1e-6
IMPORT_POLL_SECONDS = 5.0
IMPORT_TIMEOUT_SECONDS = 45 * 60


def _load_core():
    """Load agents/graph_snapshot.py standalone (module top-level is stdlib-only)."""
    path = _ADT_ROOT / "agents" / "graph_snapshot.py"
    spec = importlib.util.spec_from_file_location("adt_graph_snapshot_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_app(base_url: str, username: str, password: str):
    """Cookie-login client — the SAME helper the other script lanes use."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from scripts.smoke_prompts import App  # noqa: E402 - shared lane helper
    app = App(base_url)
    login = app.call("POST", "/api/auth/login",
                     {"email": username, "password": password})
    if login.get("status") != "ok":
        raise SystemExit("ERROR: login failed")
    return app


# ── export ───────────────────────────────────────────────────────────────────

def cmd_export(args) -> int:
    from urllib.parse import quote
    core = _load_core()
    app = _make_app(args.base_url, args.username, args.password)
    node_items, edge_items = [], []
    cursor = ""
    while True:
        chunk = app.call(
            "GET", f"/api/admin/graph/snapshot?cursor={quote(cursor)}",
            timeout=args.timeout)
        (node_items if chunk.get("kind") == "nodes" else edge_items).extend(
            chunk.get("items", []))
        cursor = chunk.get("next_cursor")
        print(f"  fetched {len(node_items)} nodes / {len(edge_items)} edges",
              flush=True)
        if cursor is None:
            break
    snapshot = core.assemble_snapshot(node_items, edge_items)
    Path(args.out).write_text(json.dumps(snapshot, indent=1, default=str),
                              encoding="utf-8")
    print(f"Exported {snapshot['node_count']} nodes / "
          f"{snapshot['edge_count']} edges -> {args.out}")
    return 0


# ── import ───────────────────────────────────────────────────────────────────

def cmd_import(args) -> int:
    core = _load_core()
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    core.validate_snapshot(payload)  # refuse bad files BEFORE any network call
    if not args.yes:
        print("ERROR: import WIPES the entire graph and restores it from the "
              "file. Re-run with --yes to confirm.")
        return 2
    app = _make_app(args.base_url, args.username, args.password)
    resp = app.call("POST", "/api/admin/graph/snapshot/import",
                    {"confirm": "wipe-and-restore", "snapshot": payload},
                    timeout=args.timeout)
    if resp.get("status") not in ("started", "already-running"):
        print(f"ERROR: import did not start: {resp}")
        return 1
    print(f"Import job {resp['status']}; polling ...", flush=True)
    rc = _poll_import(app, args.timeout)
    if rc != 0 or not getattr(args, "repair", False):
        return rc
    print("Import done; running reconcile --repair ...", flush=True)
    return _reconcile(app, args.timeout, repair=True)


def _print_reconcile_report(report: list) -> bool:
    """Render the husk report; True iff every system is healthy."""
    ok = True
    for row in report:
        mark = "ok" if row.get("healthy") else "HUSK"
        if not row.get("healthy"):
            ok = False
        print(f"  {row.get('system_id')}: {mark} "
              f"(runtime_ok={row.get('runtime_ok')} "
              f"target_ok={row.get('target_ok')})")
    if not report:
        print("  (no onboarded systems in the graph)")
    return ok


def _reconcile(app, timeout: int, *, repair: bool) -> int:
    """Run the reconcile route; with repair, poll the job to completion."""
    resp = app.call("POST", "/api/admin/graph/snapshot/reconcile",
                    {"repair": repair}, timeout=timeout)
    healthy = _print_reconcile_report(resp.get("report", []))
    if not repair:
        return 0 if healthy else 1
    if healthy:
        print("All systems healthy — nothing to repair.")
        return 0
    if resp.get("status") not in ("started", "already-running"):
        print(f"ERROR: repair did not start: {resp}")
        return 1
    print(f"Repair job {resp['status']}; polling ...", flush=True)
    deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(IMPORT_POLL_SECONDS)
        job = app.call("GET", "/api/admin/graph/snapshot/reconcile/status",
                       timeout=timeout)
        state = job.get("state", "none")
        print(f"  state={state}", flush=True)
        if state == "done":
            result = job.get("result", {})
            print(f"Repaired: {result.get('repaired', [])}")
            return 0 if _print_reconcile_report(
                result.get("report_after", [])) else 1
        if state in ("error", "none"):
            print(f"ERROR: repair ended state={state}: {job.get('error', '')}")
            return 1
    print("ERROR: repair polling timed out")
    return 1


def cmd_reconcile(args) -> int:
    """Husk check (exit 0 iff all healthy); --repair re-provisions."""
    app = _make_app(args.base_url, args.username, args.password)
    return _reconcile(app, args.timeout, repair=bool(args.repair))


def _poll_import(app, timeout: int) -> int:
    deadline = time.monotonic() + IMPORT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(IMPORT_POLL_SECONDS)
        job = app.call("GET", "/api/admin/graph/snapshot/import/status",
                       timeout=timeout)
        state = job.get("state", "none")
        print(f"  state={state}", flush=True)
        if state == "done":
            print(f"Import complete: {json.dumps(job.get('result', {}))}")
            return 0
        if state in ("error", "none"):
            print(f"ERROR: import job ended in state={state}: "
                  f"{job.get('error', '')}")
            return 1
    print("ERROR: import polling timed out")
    return 1


# ── offline diff ─────────────────────────────────────────────────────────────

# Volatile property KEYS: timestamps and infra identifiers that legitimately
# differ between two exports of "the same" graph (fresh re-onboarding mints
# new runtime/gateway-target ids; every write stamps new times).
_VOLATILE_KEY_RE = re.compile(
    r"(_at$)|(^|_)(timestamp|created|updated|exported)$"
    r"|(^|_)(runtime|target|gateway)_?\w*_?(id|ids|arn|arns)$",
    re.IGNORECASE,
)
# Volatile VALUES: ISO-8601-shaped strings and AWS ARNs, wherever they live.
_TS_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_ARN_VALUE_RE = re.compile(r"^arn:aws[\w-]*:")


def _normalize_value(key: str, value):
    if _VOLATILE_KEY_RE.search(key):
        return "<VOLATILE>"
    if isinstance(value, str):
        if _TS_VALUE_RE.match(value):
            return "<TS>"
        if _ARN_VALUE_RE.match(value):
            return "<ARN>"
    return value


def normalize_props(props: dict) -> dict:
    return {k: _normalize_value(k, v) for k, v in sorted(props.items())}


def _values_equal(a, b) -> bool:
    """Equality with float tolerance (1e-6), recursing into lists."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=0.0, abs_tol=FLOAT_TOLERANCE)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            _values_equal(x, y) for x, y in zip(a, b))
    return a == b


def props_equal(a: dict, b: dict) -> bool:
    na, nb = normalize_props(a), normalize_props(b)
    return set(na) == set(nb) and all(
        _values_equal(na[k], nb[k]) for k in na)


def _changed_keys(a: dict, b: dict) -> list[str]:
    na, nb = normalize_props(a), normalize_props(b)
    keys = sorted(set(na) | set(nb))
    return [k for k in keys
            if k not in na or k not in nb or not _values_equal(na[k], nb[k])]


def _fingerprint(props: dict) -> str:
    """Order-independent, tolerance-stable serialization of normalized props."""
    def _stable(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, float):
            return round(v, 6)
        if isinstance(v, list):
            return [_stable(x) for x in v]
        return v
    normalized = {k: _stable(v) for k, v in normalize_props(props).items()}
    return json.dumps(normalized, sort_keys=True, default=str)


def node_identity(node: dict) -> tuple[str, str]:
    """(label-group, stable-key) — prefers domain ids, falls back to the full
    normalized fingerprint (nodes need NOT carry an ``id`` property)."""
    label = "|".join(sorted(node.get("labels", []))) or "<unlabeled>"
    props = node.get("props", {})
    for key in ("id", "source_id"):
        value = props.get(key)
        if value not in (None, ""):
            return label, f"{key}={value}"
    return label, f"fp={_fingerprint(props)}"


def _index_nodes(snapshot: dict) -> dict[tuple, list[dict]]:
    index: dict[tuple, list[dict]] = {}
    for node in snapshot.get("nodes", []):
        index.setdefault(node_identity(node), []).append(node)
    return index


def _edge_identity(edge: dict, nodes: list[dict]) -> tuple:
    from_id = node_identity(nodes[edge["from"]])
    to_id = node_identity(nodes[edge["to"]])
    return (edge["type"], from_id, to_id)


def _index_edges(snapshot: dict) -> dict[tuple, list[dict]]:
    nodes = snapshot.get("nodes", [])
    index: dict[tuple, list[dict]] = {}
    for edge in snapshot.get("edges", []):
        index.setdefault(_edge_identity(edge, nodes), []).append(edge)
    return index


def _diff_group(a_items: dict, b_items: dict, describe) -> dict:
    """Shared missing/extra/changed logic over identity-indexed item lists."""
    from collections import Counter
    missing, extra, changed = [], [], []
    for key in sorted(set(a_items) | set(b_items), key=repr):
        in_a, in_b = a_items.get(key, []), b_items.get(key, [])
        if not in_b:
            missing.extend(describe(key, item) for item in in_a)
        elif not in_a:
            extra.extend(describe(key, item) for item in in_b)
        elif len(in_a) == 1 and len(in_b) == 1:
            keys = _changed_keys(in_a[0].get("props", {}),
                                 in_b[0].get("props", {}))
            if keys:
                changed.append({"item": describe(key, in_a[0]),
                                "properties": keys})
        else:  # duplicate identities: compare normalized-fingerprint multisets
            ca = Counter(_fingerprint(i.get("props", {})) for i in in_a)
            cb = Counter(_fingerprint(i.get("props", {})) for i in in_b)
            missing.extend([describe(key, in_a[0])] * sum((ca - cb).values()))
            extra.extend([describe(key, in_b[0])] * sum((cb - ca).values()))
    return {"missing_in_b": missing, "extra_in_b": extra, "changed": changed}


def _group_by(index: dict, group_of) -> dict:
    grouped: dict[str, dict] = {}
    for key, items in index.items():
        grouped.setdefault(group_of(key), {})[key] = items
    return grouped


def diff_snapshots(a: dict, b: dict) -> dict:
    """Semantic diff of two snapshot dicts. Pure/offline; see module doc."""
    result = {"identical": True, "nodes": {}, "edges": {}}
    node_groups_a = _group_by(_index_nodes(a), lambda k: k[0])
    node_groups_b = _group_by(_index_nodes(b), lambda k: k[0])
    for label in sorted(set(node_groups_a) | set(node_groups_b)):
        ga, gb = node_groups_a.get(label, {}), node_groups_b.get(label, {})
        diff = _diff_group(ga, gb, lambda key, item: key[1])
        diff["a_count"] = sum(len(v) for v in ga.values())
        diff["b_count"] = sum(len(v) for v in gb.values())
        result["nodes"][label] = diff
    edge_groups_a = _group_by(_index_edges(a), lambda k: k[0])
    edge_groups_b = _group_by(_index_edges(b), lambda k: k[0])
    for etype in sorted(set(edge_groups_a) | set(edge_groups_b)):
        ga, gb = edge_groups_a.get(etype, {}), edge_groups_b.get(etype, {})
        diff = _diff_group(
            ga, gb, lambda key, item: f"{key[1][1]} -> {key[2][1]}")
        diff["a_count"] = sum(len(v) for v in ga.values())
        diff["b_count"] = sum(len(v) for v in gb.values())
        result["edges"][etype] = diff
    result["identical"] = not any(
        d["missing_in_b"] or d["extra_in_b"] or d["changed"]
        for section in ("nodes", "edges") for d in result[section].values())
    return result


def _summary_lines(diff: dict) -> list[str]:
    lines = []
    for section in ("nodes", "edges"):
        for name, d in diff[section].items():
            marker = ("  " if not (d["missing_in_b"] or d["extra_in_b"]
                                   or d["changed"]) else "! ")
            lines.append(
                f"{marker}{section[:-1]} {name}: A={d['a_count']} "
                f"B={d['b_count']} missing={len(d['missing_in_b'])} "
                f"extra={len(d['extra_in_b'])} changed={len(d['changed'])}")
            for item in d["missing_in_b"][:10]:
                lines.append(f"      missing in B: {item}")
            for item in d["extra_in_b"][:10]:
                lines.append(f"      extra in B:   {item}")
            for entry in d["changed"][:10]:
                lines.append(f"      changed: {entry['item']} "
                             f"({', '.join(entry['properties'])})")
    return lines


def _markdown_report(diff: dict, path_a: str, path_b: str) -> str:
    verdict = ("semantically identical (after normalization)"
               if diff["identical"] else "DIFFERENT")
    out = [f"# Graph snapshot diff", "",
           f"- A: `{path_a}`", f"- B: `{path_b}`", f"- Verdict: **{verdict}**",
           ""]
    for section in ("nodes", "edges"):
        out.append(f"## {section.capitalize()} by "
                   f"{'label' if section == 'nodes' else 'type'}")
        out.append("")
        out.append("| name | A | B | missing in B | extra in B | changed |")
        out.append("|---|---|---|---|---|---|")
        for name, d in diff[section].items():
            out.append(f"| {name} | {d['a_count']} | {d['b_count']} | "
                       f"{len(d['missing_in_b'])} | {len(d['extra_in_b'])} | "
                       f"{len(d['changed'])} |")
        out.append("")
    return "\n".join(out)


def cmd_diff(args) -> int:
    a = json.loads(Path(args.file_a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.file_b).read_text(encoding="utf-8"))
    diff = diff_snapshots(a, b)
    for line in _summary_lines(diff):
        print(line)
    if args.report:
        Path(args.report).write_text(
            _markdown_report(diff, args.file_a, args.file_b), encoding="utf-8")
        print(f"Report written to {args.report}")
    if diff["identical"]:
        print("== RESULT: IDENTICAL (after volatile-property normalization) ==")
        return 0
    print("== RESULT: DIFFERENT ==")
    return 1


# ── entrypoint ───────────────────────────────────────────────────────────────

def _add_remote_args(sub):
    env = os.environ
    sub.add_argument("--base-url", default=env.get("ADT_BASE_URL", ""))
    sub.add_argument("--username", default=env.get("ADT_USERNAME", "ADT"))
    sub.add_argument("--password", default=env.get("ADT_PASSWORD", ""))
    sub.add_argument("--timeout", type=int, default=110,
                     help="per-request timeout (s); chunks are CloudFront-safe")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="command", required=True)
    exp = subs.add_parser("export", help="export the entire graph to JSON")
    _add_remote_args(exp)
    exp.add_argument("--out", required=True)
    imp = subs.add_parser("import",
                          help="DESTRUCTIVE: wipe graph + restore from file")
    _add_remote_args(imp)
    imp.add_argument("--file", required=True)
    imp.add_argument("--yes", action="store_true",
                     help="confirm the wipe-and-restore")
    imp.add_argument("--repair", action="store_true",
                     help="after a successful import, re-provision any "
                          "husk systems (reconcile --repair)")
    rec = subs.add_parser("reconcile",
                          help="husk check: graph-onboarded systems vs live "
                               "runtimes/targets; --repair re-provisions")
    _add_remote_args(rec)
    rec.add_argument("--repair", action="store_true",
                     help="re-provision unhealthy systems (background job)")
    dif = subs.add_parser("diff", help="offline semantic diff of two exports")
    dif.add_argument("file_a")
    dif.add_argument("file_b")
    dif.add_argument("--report", default="", help="optional markdown report path")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("export", "import", "reconcile") and (
            not args.base_url or not args.password):
        print("ERROR: --base-url and --password "
              "(or ADT_BASE_URL/ADT_PASSWORD) required")
        return 2
    return {"export": cmd_export, "import": cmd_import,
            "diff": cmd_diff, "reconcile": cmd_reconcile}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
