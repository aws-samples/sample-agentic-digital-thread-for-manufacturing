#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""purge_actor_memory.py — delete ONE actor's actor-wide long-term memory
(the ``/facts/{actorId}/`` and ``/preferences/{actorId}/`` namespaces) from an
AgentCore Memory. The workshop teardown step (docs/workshop-mode.md).

Why: the workshop's ``MEMORY_LTM_SCOPE=session`` only stops actor-wide
facts/preferences being RETRIEVED into a fresh chat's model context; they are
still WRITTEN for the shared login's actor. A redeploy without ``--workshop``
returns to actor scope, and every chat of that login would then be answered
with the whole workshop's facts. Purge them before handing the account on.

Chat summaries (``/summaries/{actorId}/{sessionId}/``) are session-scoped and
are NOT touched. Only records whose every namespace lies inside the actor's
namespace are deleted; record content is never printed.

Usage
-----
  python3 scripts/purge_actor_memory.py --memory-id <id> --actor-id <actorId> \\
      --region <region> [--dry-run]

Find the ids with ``aws bedrock-agentcore-control list-memories`` and
``aws bedrock-agentcore list-actors --memory-id <id>``. Credentials come from
the usual AWS chain (``AWS_PROFILE`` etc.).

Exit: 0 every listed record deleted and a re-list finds none (or, with
``--dry-run``, the listing succeeded); 1 a list/delete failed or records
remain; 2 usage error (incl. an actor id that is not a plain namespace
segment).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Callable

# One namespace segment: no "/", no wildcard, no dot-only path segment.
_ACTOR_ID_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9._@+=,:-]+$")
_PAGE_SIZE = 100


def actor_namespaces(actor_id: str) -> tuple[str, ...]:
    """The actor-wide namespaces, byte-identical to the CfnMemory strategy
    templates (``agents/memory_client.py`` ``get_semantic``)."""
    return (f"/facts/{actor_id}/", f"/preferences/{actor_id}/")


def _default_client(region: str):
    import boto3
    return boto3.client("bedrock-agentcore", region_name=region)


def _list(client, memory_id: str, namespace: str) -> list[dict]:
    records, token = [], None
    while True:
        kwargs = {"memoryId": memory_id, "namespace": namespace, "maxResults": _PAGE_SIZE}
        if token:
            kwargs["nextToken"] = token
        resp = client.list_memory_records(**kwargs) or {}
        records.extend(r for r in resp.get("memoryRecordSummaries") or [] if isinstance(r, dict))
        token = resp.get("nextToken")
        if not token:
            return records


def _owned(record: dict, namespace: str) -> bool:
    spaces = record.get("namespaces") or [namespace]
    return bool(record.get("memoryRecordId")) and all(
        isinstance(ns, str) and ns.startswith(namespace) for ns in spaces)


def _purge_namespace(client, memory_id: str, namespace: str, dry_run: bool) -> dict:
    records = _list(client, memory_id, namespace)
    counts = {"found": len(records)}
    if dry_run:
        return counts
    deleted = failed = 0
    for record in records:
        if not _owned(record, namespace):
            continue
        try:
            client.delete_memory_record(memoryId=memory_id,
                                        memoryRecordId=record["memoryRecordId"])
            deleted += 1
        except Exception as exc:  # reported, never re-raised: the rest still purge
            failed += 1
            print(f"delete failed for a record in {namespace}: {type(exc).__name__}",
                  file=sys.stderr)
    remaining = len(_list(client, memory_id, namespace))
    return {**counts, "deleted": deleted, "failed": failed, "remaining": remaining}


def purge(client, memory_id: str, actor_id: str, *, dry_run: bool) -> tuple[int, dict]:
    """Purge (or, under ``dry_run``, count) the actor's namespaces."""
    report = {"memory_id": memory_id, "actor_id": actor_id, "dry_run": dry_run,
              "namespaces": {}}
    try:
        for namespace in actor_namespaces(actor_id):
            report["namespaces"][namespace] = _purge_namespace(
                client, memory_id, namespace, dry_run)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return 1, report
    if dry_run:
        return 0, report
    clean = all(n["failed"] == 0 and n["remaining"] == 0 for n in report["namespaces"].values())
    report["verified"] = clean
    return (0 if clean else 1), report


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Purge one actor's facts + preferences memory")
    ap.add_argument("--memory-id", required=True)
    ap.add_argument("--actor-id", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="list and count only — delete nothing")
    return ap


def main(argv=None, *, client_factory: Callable = _default_client) -> int:
    args = _build_arg_parser().parse_args(argv)
    if not _ACTOR_ID_RE.match(args.actor_id or ""):
        print("--actor-id must be one plain namespace segment (no '/', no wildcard)",
              file=sys.stderr)
        return 2
    code, report = purge(client_factory(args.region), args.memory_id, args.actor_id,
                         dry_run=args.dry_run)
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
