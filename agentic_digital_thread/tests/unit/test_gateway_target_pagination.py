# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pagination of AgentCore gateway-target listings (>10 targets regression).

``list_gateway_targets`` returns at most 10 targets per call plus a
``nextToken``. A gateway with 12 targets puts two of them on page 2. The
per-system reset made ONE unpaginated call, found nothing for a page-2
system, deleted its runtime, and reported done while the gateway target
survived, pointing at the deleted runtime (every orchestrator turn then saw
a tool whose MCP initialize failed). These tests pin:

* the ONE sanctioned enumerator
  (:func:`runtime_provision_helpers.list_all_gateway_targets`);
* every caller under ``agents/`` finds a page-2 target (reset lookup + the
  absence wait, the global reset sweep, the sync route, the provisioner
  conflict-reuse path, and the four ``AgentManager`` paths);
* the absence wait never succeeds while the target is still on a later page;
* a source tripwire: no direct ``list_gateway_targets(`` call under
  ``agents/`` outside the helper.

All fixtures are synthetic; no AWS call is made.
"""
from __future__ import annotations

import ast
import asyncio
import functools
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import pytest  # noqa: E402

import agents.onboard as onboard  # noqa: E402
from agents.agent_manager import AgentManager, QUERY_BRIDGE_SYSTEM_ID  # noqa: E402
from agents.provisioner import (  # noqa: E402
    CreateTargetInput,
    create_target,
)

_GW = "gw-query-111"
_QUICK_GW = "gw-quick-222"

# The shape observed on a 12-target gateway: ten on page 1, two on page 2.
_PAGE_ONE = ["cmms", "cmms-rest", "cmms-ind", "digital-thread", "erp",
             "mes-cr", "mes-det", "mes-ind", "mes-mil", "sitewise-direct"]
_PAGE_TWO = ["cmms-rest-2", "plm"]
_TWELVE = _PAGE_ONE + _PAGE_TWO


class _ConflictException(Exception):
    pass


class _PagedGateway:
    """bedrock-agentcore-control stand-in that pages targets 10 at a time.

    A deleted target lingers (status ``DELETING``) for ``linger_polls`` FULL
    listings, then disappears; names in ``never_delete`` stay forever. A
    "full listing" starts with a call that carries no ``nextToken``.
    """

    PAGE = 10

    class exceptions:  # noqa: N801 - boto3 shape
        ConflictException = _ConflictException

    def __init__(self, names, *, linger_polls: int = 0, never_delete=(),
                 conflict: bool = False, key: str = "items", runtimes=(),
                 fail_page_two: bool = False):
        self.targets = [{"name": n, "targetId": f"tid-{n}", "status": "READY"}
                        for n in names]
        self.runtimes = [{"agentRuntimeName": n, "status": "READY",
                          "agentRuntimeArn": f"arn:aws:x:runtime/{n}-id"}
                         for n in runtimes]
        self.fail_page_two = fail_page_two
        self.deleted_runtimes: list[str] = []
        self.linger_polls = linger_polls
        self.never_delete = set(never_delete)
        self.conflict = conflict
        self.key = key
        self.list_calls: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.synced: list[list[str]] = []
        self.created: list[dict] = []
        self._lingering: dict[str, int] = {}

    # -- listing -------------------------------------------------------------
    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
        kwargs = {"gatewayIdentifier": gatewayIdentifier}
        if nextToken is not None:
            kwargs["nextToken"] = nextToken
        self.list_calls.append(kwargs)
        if nextToken is not None and self.fail_page_two:
            raise RuntimeError("ThrottlingException on page 2")
        if nextToken is None:
            self._advance_deletes()
        start = int(nextToken or 0)
        resp = {self.key: [dict(t) for t in self.targets[start:start + self.PAGE]]}
        if start + self.PAGE < len(self.targets):
            resp["nextToken"] = str(start + self.PAGE)
        return resp

    def _advance_deletes(self) -> None:
        for tid, remaining in list(self._lingering.items()):
            if remaining <= 0:
                self._remove(tid)
            else:
                self._lingering[tid] = remaining - 1

    def _remove(self, tid: str) -> None:
        self.targets = [t for t in self.targets if t["targetId"] != tid]
        self._lingering.pop(tid, None)

    # -- mutations -------------------------------------------------------------
    def delete_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        self.deleted.append((gatewayIdentifier, targetId))
        target = next((t for t in self.targets if t["targetId"] == targetId), None)
        if target is None or target["name"] in self.never_delete:
            return {}
        target["status"] = "DELETING"
        if self.linger_polls <= 0:
            self._remove(targetId)
        else:
            self._lingering[targetId] = self.linger_polls
        return {}

    def get_gateway_target(self, gatewayIdentifier, targetId):  # noqa: N803
        target = next((t for t in self.targets if t["targetId"] == targetId), None)
        if target is None:
            raise RuntimeError("ResourceNotFoundException")
        return dict(target)

    def create_gateway_target(self, **kwargs):
        self.created.append(kwargs)
        if self.conflict:
            raise _ConflictException("target exists")
        return {"targetId": f"tid-new-{kwargs['name']}"}

    def synchronize_gateway_targets(self, gatewayIdentifier, targetIdList):  # noqa: N803
        self.synced.append(list(targetIdList))
        return {}

    def list_agent_runtimes(self, **kwargs):
        return {"agentRuntimes": [dict(r) for r in self.runtimes]}

    def delete_agent_runtime(self, agentRuntimeId):  # noqa: N803
        self.deleted_runtimes.append(agentRuntimeId)
        self.runtimes = [r for r in self.runtimes
                         if not r["agentRuntimeArn"].endswith("/" + agentRuntimeId)]
        return {}

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
        raise RuntimeError("ResourceNotFoundException")


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        self.t += s


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #

def test_helper_collects_every_page_and_starts_with_gateway_id_only():
    from agents.runtime_provision_helpers import list_all_gateway_targets

    control = _PagedGateway(_TWELVE)
    got = [t["name"] for t in list_all_gateway_targets(control, _GW)]
    assert got == _TWELVE
    assert control.list_calls == [
        {"gatewayIdentifier": _GW},
        {"gatewayIdentifier": _GW, "nextToken": "10"},
    ]


def test_helper_single_page_works_with_a_gateway_id_only_fake():
    """Many fakes define ``list_gateway_targets(self, gatewayIdentifier)``; a
    one-page listing must never pass ``nextToken``."""
    from agents.runtime_provision_helpers import list_all_gateway_targets

    class _Strict:
        def list_gateway_targets(self, gatewayIdentifier):  # noqa: N803
            return {"items": [{"name": "erp", "targetId": "tid-erp"}]}

    assert [t["name"] for t in list_all_gateway_targets(_Strict(), _GW)] == ["erp"]


def test_helper_normalizes_the_targets_key():
    from agents.runtime_provision_helpers import list_all_gateway_targets

    control = _PagedGateway(_TWELVE, key="targets")
    assert [t["name"] for t in list_all_gateway_targets(control, _GW)] == _TWELVE


def test_helper_treats_a_non_string_token_as_the_last_page():
    """An unconfigured MagicMock response must not loop forever."""
    from agents.runtime_provision_helpers import list_all_gateway_targets

    control = MagicMock()
    assert list_all_gateway_targets(control, _GW) == []
    assert control.list_gateway_targets.call_count == 1


def test_helper_raises_on_a_repeated_token():
    """A token that repeats can never finish; a truncated list would read as
    a false absence, so the helper raises instead of returning it."""
    from agents.runtime_provision_helpers import list_all_gateway_targets

    class _Looping:
        def list_gateway_targets(self, gatewayIdentifier, nextToken=None):  # noqa: N803
            return {"items": [{"name": "erp"}], "nextToken": "same"}

    with pytest.raises(RuntimeError, match="repeated nextToken"):
        list_all_gateway_targets(_Looping(), _GW)


def test_helper_propagates_a_client_error():
    from agents.runtime_provision_helpers import list_all_gateway_targets

    control = MagicMock()
    control.list_gateway_targets.side_effect = RuntimeError("throttled")
    with pytest.raises(RuntimeError, match="throttled"):
        list_all_gateway_targets(control, _GW)


# --------------------------------------------------------------------------- #
# Reset: lookup + absence wait
# --------------------------------------------------------------------------- #

def _clocked_wait(monkeypatch, clock):
    """Route the reset's absence wait through an injected clock (the real
    wait logic, no real sleeping)."""
    monkeypatch.setattr(
        onboard, "_wait_targets_absent",
        functools.partial(onboard._wait_targets_absent,
                          sleep=clock.sleep, monotonic=clock.monotonic))


@pytest.mark.parametrize("sid", _PAGE_TWO)
def test_reset_deletes_a_page_two_target_and_waits_until_absent(monkeypatch, sid):
    control = _PagedGateway(_TWELVE, linger_polls=2)
    clock = _Clock()
    _clocked_wait(monkeypatch, clock)

    awaited, timed_out, delete_failed = onboard._reset_delete_gateway_target(
        control, _GW, sid, deadline_at=onboard.time.monotonic() + 900.0)

    assert awaited == [sid]
    assert timed_out is False
    assert delete_failed is False
    assert control.deleted == [(_GW, f"tid-{sid}")]
    # It lingered for two full listings, so the wait polled past them.
    assert clock.sleeps == 2
    assert sid not in {t["name"] for t in control.targets}
    # Every absence poll read page 2 as well as page 1.
    assert any(c.get("nextToken") for c in control.list_calls)


def test_reset_reports_not_done_while_a_page_two_target_survives(monkeypatch):
    """The reset contract: never report done while the target still
    exists. A page-2 target that will not go away must time the wait out."""
    control = _PagedGateway(_TWELVE, never_delete={"plm"})
    clock = _Clock()
    _clocked_wait(monkeypatch, clock)

    awaited, timed_out, delete_failed = onboard._reset_delete_gateway_target(
        control, _GW, "plm", deadline_at=onboard.time.monotonic() + 12.0)

    assert awaited == ["plm"]
    assert timed_out is True
    # The delete API accepted (returned {}), the target simply never left — this
    # is a surviving-after-accept case, NOT a refused delete, so delete_failed
    # stays False (the runtime delete is still gated only by the absence wait).
    assert delete_failed is False


def test_absence_wait_does_not_succeed_while_target_is_on_a_later_page():
    control = _PagedGateway(_TWELVE)
    clock = _Clock()
    gone = onboard._wait_targets_absent(
        control, _GW, ["cmms-rest-2"], deadline_s=9.0, poll_s=3.0,
        sleep=clock.sleep, monotonic=clock.monotonic)
    assert gone is False
    assert clock.t >= 9.0  # it kept polling to the deadline


def test_absence_wait_succeeds_once_the_page_two_target_is_gone():
    control = _PagedGateway(_TWELVE, linger_polls=1)
    control.delete_gateway_target(_GW, "tid-plm")
    clock = _Clock()
    gone = onboard._wait_targets_absent(
        control, _GW, ["plm"], deadline_s=30.0, poll_s=3.0,
        sleep=clock.sleep, monotonic=clock.monotonic)
    assert gone is True
    assert clock.sleeps == 1


def test_absence_wait_fails_safe_on_a_later_page_error():
    """Waiting for a PAGE-2 target while page 2 errors: absence is
    unverifiable, so False, never True. (The old one-page read never asked
    for page 2 and called the target absent.)"""
    control = _PagedGateway(_TWELVE, fail_page_two=True)
    clock = _Clock()
    assert onboard._wait_targets_absent(
        control, _GW, ["plm"], deadline_s=9.0, poll_s=3.0,
        sleep=clock.sleep, monotonic=clock.monotonic) is False
    assert {"gatewayIdentifier": _GW, "nextToken": "10"} in control.list_calls


# --------------------------------------------------------------------------- #
# Global reset sweep + sync route
# --------------------------------------------------------------------------- #

def test_global_delete_targets_reaches_page_two():
    control = _PagedGateway(_TWELVE)
    names, failed = onboard._global_delete_targets(
        control, _GW, deadline_at=onboard.time.monotonic() + 900.0)

    assert set(_PAGE_TWO) <= set(names)
    assert failed == []
    assert "digital-thread" not in names  # built-in preserved
    deleted_ids = {tid for _, tid in control.deleted}
    assert {f"tid-{n}" for n in _PAGE_TWO} <= deleted_ids
    assert "tid-digital-thread" not in deleted_ids


def test_sync_route_finds_a_page_two_target(monkeypatch):
    control = _PagedGateway(_TWELVE)
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", _GW)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("boto3.client", lambda *a, **k: control):
        result = onboard.onboard_sync_gateway("cmms_rest_2")
    assert result == {"status": "sync_triggered", "target_id": "tid-cmms-rest-2",
                      "system_id": "cmms_rest_2"}
    assert control.synced == [["tid-cmms-rest-2"]]


def test_sync_route_runs_its_body_off_the_event_loop(monkeypatch):
    """The async /sync route only awaits offload(onboard_sync_gateway, ...):
    the paged listing and the sync call run on a worker thread."""
    import threading

    loop_thread = []
    seen = []
    real = onboard.onboard_sync_gateway

    def body(system_id):
        seen.append(threading.get_ident())
        return real(system_id)

    control = _PagedGateway(_TWELVE)
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", _GW)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(onboard, "onboard_sync_gateway", body)

    async def run():
        loop_thread.append(threading.get_ident())
        return await onboard.onboard_sync_gateway_route("plm")

    with patch("boto3.client", lambda *a, **k: control):
        result = asyncio.run(run())
    assert result["target_id"] == "tid-plm"
    assert seen and seen[0] != loop_thread[0]


# --------------------------------------------------------------------------- #
# Provisioner conflict-reuse path
# --------------------------------------------------------------------------- #

def test_provisioner_create_target_reuses_a_page_two_target_on_conflict():
    control = _PagedGateway(_TWELVE, conflict=True)
    # The page-2 target already points at this runtime, so it is reused as is.
    plm = next(t for t in control.targets if t["name"] == "plm")
    plm["targetConfiguration"] = {"mcp": {"mcpServer": {"endpoint": "https://runtime/mcp"}}}
    inp = CreateTargetInput(system_id="plm", system_name="PLM", gateway_id=_GW,
                            runtime_mcp_url="https://runtime/mcp")
    res = create_target(inp, control=control)
    assert res.gateway_target_id == "tid-plm"


# --------------------------------------------------------------------------- #
# AgentManager paths
# --------------------------------------------------------------------------- #

def _bare_manager(gateway_id: str = _GW) -> AgentManager:
    mgr = AgentManager.__new__(AgentManager)  # no __init__ side effects needed
    mgr.gateway_id = gateway_id
    return mgr


def test_manager_fast_path_sees_a_ready_page_two_target():
    control = _PagedGateway(_TWELVE)
    mgr = _bare_manager()
    with patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "_runtime_is_ready", return_value=True):
        assert mgr._cloud_fast_path_ready("cmms_rest_2", _GW) is True


def test_manager_conflict_reuse_finds_a_page_two_target():
    control = _PagedGateway(_TWELVE, conflict=True)
    mgr = _bare_manager()
    tid, needs_sync = mgr._create_or_reuse_gateway_target(
        control, "plm", {"gatewayIdentifier": _GW, "name": "plm"})
    assert (tid, needs_sync) == ("tid-plm", False)


def test_manager_lookup_finds_a_page_two_target():
    control = _PagedGateway(_TWELVE)
    _runtime_id, target_id = AgentManager._lookup_cloud_agent(control, "cmms-rest-2", _GW)
    assert target_id == "tid-cmms-rest-2"


def test_manager_stale_bridge_sweep_finds_a_page_two_target():
    control = _PagedGateway(_TWELVE + [QUERY_BRIDGE_SYSTEM_ID])
    mgr = _bare_manager(gateway_id=_GW)
    removed = []
    with patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "_delete_gateway_target_and_wait",
                         staticmethod(lambda c, gw, tid, sid: removed.append((gw, tid, sid)))):
        mgr._remove_stale_bridge_target_from_shared_gateway(_QUICK_GW)
    assert removed == [(_GW, f"tid-{QUERY_BRIDGE_SYSTEM_ID}", QUERY_BRIDGE_SYSTEM_ID)]


# --------------------------------------------------------------------------- #
# Startup: one complete target listing per gateway per restore pass
# --------------------------------------------------------------------------- #

def _cloud_manager() -> AgentManager:
    mgr = _bare_manager()
    mgr.mode = "cloud"
    mgr.gateway_url = "https://gw.example.com/mcp"
    mgr._agents = {}
    return mgr


def _restore_systems(start_agent, sids) -> None:
    for sid in sids:
        start_agent(system_id=sid, system_name=sid, schema="", system_prompt="")


def test_restore_pass_lists_the_targets_once_per_gateway():
    """12 restored systems on one 12-target gateway: ONE complete listing (2
    pages), not one per system (24 calls)."""
    from agents.agent_manager import gateway_target_snapshot

    control = _PagedGateway(_TWELVE)
    mgr = _cloud_manager()
    with patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "_runtime_is_ready", return_value=True), \
            gateway_target_snapshot():
        _restore_systems(mgr.start_agent, _TWELVE)
    assert sorted(mgr._agents) == sorted(_TWELVE)       # every one took the fast path
    assert len(control.list_calls) == 2


def test_server_restore_wraps_the_pass_in_one_snapshot(monkeypatch):
    import agents.server as server

    control = _PagedGateway(_TWELVE)
    monkeypatch.setattr(server, "_agent_manager", _cloud_manager())
    monkeypatch.setattr(server, "_sql_tables_desc_from_graph", lambda _sid: "")
    monkeypatch.setattr("agents.onboard.neptune_query_checked", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_restore_legacy", lambda: _restore_systems(
        server._agent_manager.start_agent, _TWELVE))
    with patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "_runtime_is_ready", return_value=True):
        server._restore()
    assert len(server._agent_manager._agents) == 12
    assert len(control.list_calls) == 2


def test_fast_path_outside_a_restore_pass_lists_fresh_every_time():
    """No snapshot outside a pass: a target deleted between two checks is
    seen as gone (onboarding never reads a stale view)."""
    control = _PagedGateway(_TWELVE)
    mgr = _bare_manager()
    with patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "_runtime_is_ready", return_value=True):
        assert mgr._cloud_fast_path_ready("plm", _GW) is True
        control.delete_gateway_target(_GW, "tid-plm")
        assert mgr._cloud_fast_path_ready("plm", _GW) is False
    assert len(control.list_calls) == 4


def test_snapshot_does_not_cache_a_listing_error_and_ends_with_its_scope():
    from agents.agent_manager import _TARGET_SNAPSHOT, gateway_target_snapshot

    control = _PagedGateway(_TWELVE, fail_page_two=True)
    mgr = _bare_manager()
    with patch("boto3.client", lambda *a, **k: control), \
            patch.object(AgentManager, "_runtime_is_ready", return_value=True), \
            gateway_target_snapshot():
        assert mgr._cloud_fast_path_ready("erp", _GW) is False  # unverifiable -> slow path
        control.fail_page_two = False
        assert mgr._cloud_fast_path_ready("erp", _GW) is True   # retried, then cached
        assert mgr._cloud_fast_path_ready("plm", _GW) is True
    assert len(control.list_calls) == 4  # failed pair + one good listing
    assert _TARGET_SNAPSHOT.get() is None


# --------------------------------------------------------------------------- #
# A failed target LISTING is "unknown", never "absent": no runtime deletion
# --------------------------------------------------------------------------- #

def test_manager_lookup_raises_when_the_target_listing_fails():
    from agents.runtime_provision_helpers import GatewayTargetLookupError

    control = _PagedGateway(_TWELVE, fail_page_two=True, runtimes=["adt_plm"])
    with pytest.raises(GatewayTargetLookupError):
        AgentManager._lookup_cloud_agent(control, "plm", _GW)


def test_delete_cloud_agent_keeps_the_runtime_when_target_listing_fails():
    """A page-2 throttle while looking up the target must stop the delete
    before the runtime goes; otherwise the target is stranded pointing at a
    deleted runtime."""
    from agents.runtime_provision_helpers import GatewayTargetLookupError

    control = _PagedGateway(_TWELVE, fail_page_two=True, runtimes=["adt_plm"])
    mgr = _bare_manager()
    mgr._agents = {}
    with patch("boto3.client", lambda *a, **k: control), \
            patch("agents.agent_manager.time.sleep", lambda *_: None), \
            pytest.raises(GatewayTargetLookupError):
        mgr.delete_cloud_agent("plm")
    assert control.deleted_runtimes == []
    assert control.deleted == []
    assert any(c.get("nextToken") for c in control.list_calls)  # page 2 was tried


def test_delete_cloud_agent_still_deletes_target_then_runtime_on_page_two():
    control = _PagedGateway(_TWELVE, runtimes=["adt_plm"])
    mgr = _bare_manager()
    mgr._agents = {}
    with patch("boto3.client", lambda *a, **k: control), \
            patch("agents.agent_manager.time.sleep", lambda *_: None):
        mgr.delete_cloud_agent("plm")
    assert control.deleted == [(_GW, "tid-plm")]
    assert control.deleted_runtimes == ["adt_plm-id"]


def test_reset_cleanup_keeps_the_runtime_when_target_listing_fails(monkeypatch):
    """The authoritative per-system cleanup has the same rule: a target
    listing that fails is incomplete AND leaves the runtime in place."""
    control = _PagedGateway(_TWELVE, fail_page_two=True, runtimes=["adt_plm"])
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", _GW)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("boto3.client", lambda *a, **k: control):
        cleanup = onboard._reset_cleanup_agentcore_sync("plm", 5.0)
    assert cleanup["wait_timed_out"] is True
    assert control.deleted_runtimes == []
    assert control.deleted == []


def test_reset_cleanup_deletes_a_page_two_target_and_its_runtime(monkeypatch):
    control = _PagedGateway(_TWELVE, runtimes=["adt_plm"])
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", _GW)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("boto3.client", lambda *a, **k: control):
        cleanup = onboard._reset_cleanup_agentcore_sync("plm", 5.0)
    assert cleanup == {"runtimes_awaited": ["adt_plm"], "targets_awaited": ["plm"],
                       "wait_timed_out": False}
    assert control.deleted == [(_GW, "tid-plm")]
    assert control.deleted_runtimes == ["adt_plm-id"]


def test_head_start_failure_is_logged_not_left_unretrieved(caplog):
    import concurrent.futures
    from agents.runtime_provision_helpers import GatewayTargetLookupError

    fut = concurrent.futures.Future()
    fut.set_exception(GatewayTargetLookupError(_GW, "plm"))
    with caplog.at_level("WARNING", logger=onboard.logger.name):
        onboard._log_delete_head_start("plm", fut)
    assert "delete_cloud_agent stopped for plm" in caplog.text
    assert "GatewayTargetLookupError" in caplog.text


def test_reset_head_start_attaches_the_outcome_logger():
    """_run_reset_cascade (behind _run_reset_steps' removal marker) wires
    _log_delete_head_start onto the head-start future, so a stopped delete is
    reported, not silently dropped."""
    import inspect

    source = inspect.getsource(onboard._run_reset_cascade)
    assert "add_done_callback" in source and "_log_delete_head_start" in source


# --------------------------------------------------------------------------- #
# Source tripwire: each paginating helper is the ONE caller of its API
# --------------------------------------------------------------------------- #

_AGENTS = _ADT_ROOT / "agents"
_HELPER_FILE = _AGENTS / "runtime_provision_helpers.py"
# raw paginated API -> the ONE helper allowed to reference it
_PAGINATED = {
    "list_gateway_targets": "list_all_gateway_targets",
    "list_oauth2_credential_providers": "list_all_oauth2_credential_providers",
}


def _api_references(tree: ast.AST, api: str) -> list[ast.AST]:
    """Every node that names ``api``: an attribute (a call, OR a bound-method
    alias such as ``fn = control.list_gateway_targets`` / ``partial(...)``)
    and any string constant equal to it (``getattr(control, "...")``, a
    paginator name, or a module constant holding the name)."""
    return [n for n in ast.walk(tree)
            if (isinstance(n, ast.Attribute) and n.attr == api)
            or (isinstance(n, ast.Constant) and n.value == api)]


def _offenders(tree: ast.AST, api: str, allowed_fn: str | None) -> tuple[list[int], int]:
    allowed: set[int] = set()
    if allowed_fn:
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name == allowed_fn:
                allowed = {id(n) for n in _api_references(fn, api)}
    lines = [n.lineno for n in _api_references(tree, api) if id(n) not in allowed]
    return lines, len(allowed)


@pytest.mark.parametrize("api", sorted(_PAGINATED))
def test_no_direct_paginated_list_call_outside_its_helper(api):
    helper_fn = _PAGINATED[api]
    offenders = []
    helper_refs = 0
    for path in sorted(_AGENTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines, allowed = _offenders(tree, api, helper_fn if path == _HELPER_FILE else None)
        if path == _HELPER_FILE:
            helper_refs = allowed
        offenders += [f"{path.relative_to(_ADT_ROOT).as_posix()}:{ln}" for ln in lines]
    assert offenders == [], (
        f"{api} returns one page at a time; call "
        f"runtime_provision_helpers.{helper_fn} instead:\n  " + "\n  ".join(offenders))
    assert helper_refs >= 1, f"tripwire is vacuous: {helper_fn} no longer calls {api}"


@pytest.mark.parametrize("snippet", [
    "control.list_gateway_targets(gatewayIdentifier=g)",
    "fn = control.list_gateway_targets\nfn(gatewayIdentifier=g)",
    "fn = functools.partial(control.list_gateway_targets, gatewayIdentifier=g)",
    "getattr(control, 'list_gateway_targets')(gatewayIdentifier=g)",
    "control.get_paginator('list_gateway_targets')",
    "API = 'list_gateway_targets'\ngetattr(control, API)(gatewayIdentifier=g)",
])
def test_tripwire_catches_each_reference_form(snippet):
    lines, _ = _offenders(ast.parse(snippet), "list_gateway_targets", None)
    assert lines, snippet
