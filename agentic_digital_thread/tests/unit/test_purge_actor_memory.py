# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/purge_actor_memory.py — the workshop-teardown purge of ONE actor's
facts + preferences long-term-memory namespaces.

Offline: a stubbed ``bedrock-agentcore`` client; no AWS call.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "purge_actor_memory.py"
_spec = importlib.util.spec_from_file_location("purge_actor_memory_cli", _SCRIPT)
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)

_MEMORY = "adt_memory-abc123"
_ACTOR = "workshop-actor"


class _StubClient:
    """Pages list_memory_records (2 per page) and records deletes."""

    def __init__(self, records: dict[str, list[dict]], *, fail_delete=frozenset(),
                 ignore_delete=frozenset()):
        self.records = {ns: list(rs) for ns, rs in records.items()}
        self.fail_delete, self.ignore_delete = set(fail_delete), set(ignore_delete)
        self.list_calls: list[dict] = []
        self.deleted: list[str] = []

    def list_memory_records(self, **kw):
        self.list_calls.append(kw)
        rows = [r for ns, rs in self.records.items() if ns.startswith(kw["namespace"]) for r in rs]
        start = int(kw.get("nextToken") or 0)
        page = rows[start:start + 2]
        out = {"memoryRecordSummaries": page}
        if start + 2 < len(rows):
            out["nextToken"] = str(start + 2)
        return out

    def delete_memory_record(self, *, memoryId, memoryRecordId):
        assert memoryId == _MEMORY
        if memoryRecordId in self.fail_delete:
            raise RuntimeError("AccessDenied")
        self.deleted.append(memoryRecordId)
        if memoryRecordId in self.ignore_delete:
            return {}
        for rs in self.records.values():
            rs[:] = [r for r in rs if r["memoryRecordId"] != memoryRecordId]
        return {}


def _rec(rid: str, ns: str) -> dict:
    return {"memoryRecordId": rid, "namespaces": [ns], "content": {"text": "secret fact"}}


def _graph(actor=_ACTOR) -> dict:
    facts, prefs = f"/facts/{actor}/", f"/preferences/{actor}/"
    return {
        facts: [_rec("f1", facts), _rec("f2", facts), _rec("f3", facts)],
        prefs: [_rec("p1", prefs)],
        f"/summaries/{actor}/s-1/": [_rec("s1", f"/summaries/{actor}/s-1/")],
        "/facts/other-actor/": [_rec("x1", "/facts/other-actor/")],
    }


def _run(client, *extra):
    argv = ["--memory-id", _MEMORY, "--actor-id", _ACTOR, "--region", "us-west-2", *extra]
    return purge.main(argv, client_factory=lambda region: client)


def test_namespaces_are_the_actor_facts_and_preferences_only():
    assert purge.actor_namespaces(_ACTOR) == (f"/facts/{_ACTOR}/", f"/preferences/{_ACTOR}/")


def test_dry_run_lists_every_page_and_deletes_nothing(capsys):
    client = _StubClient(_graph())
    assert _run(client, "--dry-run") == 0
    assert client.deleted == []
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["namespaces"][f"/facts/{_ACTOR}/"]["found"] == 3   # paged: 2 + 1
    assert report["namespaces"][f"/preferences/{_ACTOR}/"]["found"] == 1
    assert all(c["memoryId"] == _MEMORY for c in client.list_calls)
    assert "secret fact" not in json.dumps(report)                   # never echoes content


def test_purge_deletes_facts_and_preferences_and_verifies(capsys):
    client = _StubClient(_graph())
    assert _run(client) == 0
    assert sorted(client.deleted) == ["f1", "f2", "f3", "p1"]        # never s1 / x1
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is True
    assert report["namespaces"][f"/facts/{_ACTOR}/"] == {
        "found": 3, "deleted": 3, "failed": 0, "remaining": 0}


def test_a_record_outside_the_actor_namespace_is_never_deleted():
    """Defence in depth: a listed record whose namespaces are not all inside
    the actor's namespace is skipped (and counted as remaining)."""
    graph = _graph()
    graph[f"/facts/{_ACTOR}/"].append(
        {"memoryRecordId": "odd", "namespaces": [f"/facts/{_ACTOR}/", "/facts/other-actor/"]})
    client = _StubClient(graph)
    assert _run(client) == 1
    assert "odd" not in client.deleted


def test_failed_delete_exits_1(capsys):
    client = _StubClient(_graph(), fail_delete={"f2"})
    assert _run(client) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is False
    assert report["namespaces"][f"/facts/{_ACTOR}/"]["failed"] == 1


def test_records_still_listed_after_delete_exit_1(capsys):
    client = _StubClient(_graph(), ignore_delete={"p1"})
    assert _run(client) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["namespaces"][f"/preferences/{_ACTOR}/"]["remaining"] == 1


def test_list_failure_exits_1(capsys):
    class _Down(_StubClient):
        def list_memory_records(self, **kw):
            raise RuntimeError("throttled")
    assert _run(_Down({})) == 1
    assert "throttled" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("actor", ["", " ", "a/b", "../x", "*"])
def test_unsafe_actor_id_is_a_usage_error(actor):
    client = _StubClient(_graph())
    argv = ["--memory-id", _MEMORY, "--actor-id", actor, "--region", "us-west-2"]
    assert purge.main(argv, client_factory=lambda region: client) == 2
    assert client.list_calls == [] and client.deleted == []


def test_missing_required_args_exit_2():
    with pytest.raises(SystemExit) as exc:
        purge.main(["--memory-id", _MEMORY], client_factory=lambda r: None)
    assert exc.value.code == 2


def test_region_reaches_the_client_factory():
    seen = []
    client = _StubClient({})

    def factory(region):
        seen.append(region)
        return client
    purge.main(["--memory-id", _MEMORY, "--actor-id", _ACTOR, "--region", "eu-west-1",
                "--dry-run"], client_factory=factory)
    assert seen == ["eu-west-1"]
