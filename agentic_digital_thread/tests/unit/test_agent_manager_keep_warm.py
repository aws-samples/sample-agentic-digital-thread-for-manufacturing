# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the runtime keep-warm job — ``AgentManager.keep_warm_onboarded``.

Background: AgentCore per-system runtimes scale to zero when idle, so the next user
query hits a cold container (multi-second cold start or a transient "no endpoint or
agent found"). ``keep_warm_onboarded`` sends the CHEAPEST liveness signal — an MCP
``initialize`` (session open, no tool call, no Bedrock call) — to each onboarded
system whose backing ``adt_<id>`` runtime is READY, on a periodic schedule.

These tests use NO real AWS. They stub the control-plane runtime listing, the M2M
token fetch, and the per-runtime ping, and assert:
  (i)   only onboarded systems whose runtime is READY are pinged (non-READY / absent
        runtimes are skipped);
  (ii)  a per-system ping failure is swallowed and does NOT stop the other pings
        (best-effort contract);
  (iii) local (non-cloud) mode is a no-op;
  (iv)  no onboarded systems yet is a no-op (guard before onboarding).

Runnable two ways:
  * pytest:      pytest agentic_digital_thread/tests/unit/test_agent_manager_keep_warm.py
  * standalone:  python agentic_digital_thread/tests/unit/test_agent_manager_keep_warm.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

# Make `agents...` importable (agentic_digital_thread/ is two levels up from here).
_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents.agent_manager import AgentInstance, AgentManager  # noqa: E402

_GATEWAY_URL = "https://gw.example.com/mcp"


def _cloud_mgr():
    return AgentManager(mode="cloud", gateway_id="gw-123", gateway_url=_GATEWAY_URL)


def _register(mgr, system_id):
    """Register an in-memory onboarded agent (mirrors the fast-path registration)."""
    mgr._agents[system_id] = AgentInstance(
        system_id=system_id, system_name=system_id, schema="",
        system_prompt="", tables_desc="", mcp_url=_GATEWAY_URL,
    )


def _runtimes(*specs):
    """Build the {name: {"arn","status"}} map _list_runtimes_by_name would return.

    Each spec is (system_id, status); the runtime name mirrors _runtime_name.
    """
    out = {}
    for system_id, status in specs:
        name = AgentManager._runtime_name(system_id)
        out[name] = {"arn": f"arn:aws:...:runtime/{name}", "status": status}
    return out


def test_pings_only_ready_onboarded_runtimes():
    """(i) READY runtimes are pinged; a not-READY runtime and an absent runtime are
    skipped."""
    mgr = _cloud_mgr()
    _register(mgr, "erp")           # READY -> pinged
    _register(mgr, "mes")           # CREATE_FAILED -> skipped
    _register(mgr, "cmms")          # no runtime at all -> skipped

    runtimes = _runtimes(("erp", "READY"), ("mes", "CREATE_FAILED"))  # cmms absent
    pinged_arns = []

    with patch("boto3.client", lambda *a, **k: object()), \
            patch.object(AgentManager, "_list_runtimes_by_name",
                         autospec=True, return_value=runtimes), \
            patch.object(AgentManager, "_get_m2m_token",
                         autospec=True, return_value="tok"), \
            patch.object(AgentManager, "_ping_runtime", autospec=True,
                         side_effect=lambda self, arn, token, region: pinged_arns.append(arn)):
        count = mgr.keep_warm_onboarded()

    assert count == 1, count
    assert pinged_arns == [f"arn:aws:...:runtime/{AgentManager._runtime_name('erp')}"], pinged_arns


def test_swallows_per_system_ping_failure():
    """(ii) A ping failure for one READY system is logged/swallowed and the OTHER
    READY systems are still pinged — the job never crashes."""
    mgr = _cloud_mgr()
    _register(mgr, "erp")   # ping raises
    _register(mgr, "mes")   # ping succeeds

    runtimes = _runtimes(("erp", "READY"), ("mes", "READY"))
    pinged_ok = []

    def _flaky_ping(self, arn, token, region):
        if AgentManager._runtime_name("erp") in arn:
            raise RuntimeError("boom — endpoint not responding")
        pinged_ok.append(arn)

    with patch("boto3.client", lambda *a, **k: object()), \
            patch.object(AgentManager, "_list_runtimes_by_name",
                         autospec=True, return_value=runtimes), \
            patch.object(AgentManager, "_get_m2m_token",
                         autospec=True, return_value="tok"), \
            patch.object(AgentManager, "_ping_runtime", autospec=True, side_effect=_flaky_ping):
        count = mgr.keep_warm_onboarded()  # must NOT raise

    # erp failed (swallowed), mes still pinged.
    assert count == 1, count
    assert pinged_ok == [f"arn:aws:...:runtime/{AgentManager._runtime_name('mes')}"], pinged_ok


def test_local_mode_is_noop():
    """(iii) In local mode keep-warm is a no-op — it never touches AWS."""
    mgr = AgentManager(mode="local")
    _register(mgr, "erp")

    with patch.object(AgentManager, "_list_runtimes_by_name", autospec=True) as list_rt:
        count = mgr.keep_warm_onboarded()

    assert count == 0
    assert not list_rt.called, "local mode must not list runtimes"


def test_no_onboarded_systems_is_noop():
    """(iv) Guard before onboarding: with no onboarded systems it is a no-op and does
    not create a control client."""
    mgr = _cloud_mgr()  # no _register calls

    with patch("boto3.client", autospec=True) as client:
        count = mgr.keep_warm_onboarded()

    assert count == 0
    assert not client.called, "must not create a control client when nothing is onboarded"


def test_no_token_skips_all_pings():
    """When the M2M token is unavailable, no runtime is pinged (degrades to no-op)."""
    mgr = _cloud_mgr()
    _register(mgr, "erp")
    runtimes = _runtimes(("erp", "READY"))

    with patch("boto3.client", lambda *a, **k: object()), \
            patch.object(AgentManager, "_list_runtimes_by_name",
                         autospec=True, return_value=runtimes), \
            patch.object(AgentManager, "_get_m2m_token", autospec=True, return_value=""), \
            patch.object(AgentManager, "_ping_runtime", autospec=True) as ping:
        count = mgr.keep_warm_onboarded()

    assert count == 0
    assert not ping.called, "no token -> must not attempt any ping"


_TESTS = [
    test_pings_only_ready_onboarded_runtimes,
    test_swallows_per_system_ping_failure,
    test_local_mode_is_noop,
    test_no_onboarded_systems_is_noop,
    test_no_token_skips_all_pings,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} keep-warm tests passed (no real AWS)")


def test_pings_catalog_onboarded_when_memory_empty():
    """Regression (observed live): right after an ECS task redeploy ``self._agents``
    is empty while the durable catalog still knows the onboarded systems — the job
    previously pinged only the in-memory subset (1 of 3 READY runtimes), leaving the
    rest to answer queries cold. The enumeration must union the catalog set."""
    import agents.catalog as _cat_mod

    mgr = _cloud_mgr()                      # NOTE: no _register calls — memory empty
    runtimes = _runtimes(("lake-athena", "READY"), ("telem-sitewise", "READY"))
    pings = []
    with patch.object(_cat_mod.catalog, "get_onboarded_ids",
                      return_value={"lake-athena", "telem-sitewise"}), \
         patch.object(AgentManager, "_list_runtimes_by_name", return_value=runtimes), \
         patch.object(AgentManager, "_get_m2m_token", return_value="tok"), \
         patch.object(AgentManager, "_ping_runtime",
                      side_effect=lambda arn, tok, region: pings.append(arn)), \
         patch("boto3.client"):
        pinged = mgr.keep_warm_onboarded()
    assert pinged == 2, f"expected both catalog-known runtimes pinged, got {pinged}"
    assert len(pings) == 2
