# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Two Admin > Settings truths the UI now relies on.

* ``GET /api/admin/quick-links`` reports the HighByte URL. ECS resolves
  HIGHBYTE_URL once at task start, and a first ``--highbyte`` deploy starts the
  task while ``/adt/highbyte-url`` still holds deploy.sh's "placeholder" (the
  real URL is written after CDK, with no app restart). When, and only when,
  the task-start value is that sentinel, the route reads SSM live (short
  timeouts, one request for every such link, a minute's cache, a short
  failure cache) so the links and the HighByte token
  card work without a restart. The i3X HighByte link mints against that URL.
* The agent-model save asks for a re-onboard when a listed system has no
  runtime to switch (cloud ``skipped``), since that system stays on the old
  model; local mode skips every system by design and is not flagged.
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

from agents import admin_routes  # noqa: E402


class _SSM:
    """SSM fake: every name holds *value*. ``calls`` lists the names read,
    ``batches`` the GetParameters requests."""

    def __init__(self, value=None, error=None):
        self.value, self.error, self.calls, self.batches, self.config = value, error, [], [], None

    def get_parameters(self, Names):
        self.batches.append(list(Names))
        self.calls.extend(Names)
        if self.error:
            raise self.error
        return {"Parameters": [{"Name": n, "Value": self.value} for n in Names],
                "InvalidParameters": []}


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(admin_routes, "_QUICKLINK_CACHE", {})
    monkeypatch.setattr(admin_routes, "_QUICKLINK_SSM_CLIENT", None, raising=False)
    monkeypatch.setattr(admin_routes, "_QUICKLINK_READ_AT", {}, raising=False)


def _patched(monkeypatch, env_url, ssm):
    if env_url is None:
        monkeypatch.delenv("HIGHBYTE_URL", raising=False)
    else:
        monkeypatch.setenv("HIGHBYTE_URL", env_url)
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    def client(service, **kw):
        assert service == "ssm", service
        ssm.config = kw.get("config")
        return ssm

    return patch("boto3.client", client)


def _links(monkeypatch, env_url, ssm):
    with _patched(monkeypatch, env_url, ssm):
        return admin_routes.quick_links()


class TestQuickLinksHighByte:
    def test_unset_env_never_reads_ssm(self, monkeypatch):
        ssm = _SSM(value="https://hb.example.com")
        assert _links(monkeypatch, None, ssm)["highbyte"] == ""
        assert ssm.calls == []

    def test_real_task_start_url_is_used_as_is(self, monkeypatch):
        ssm = _SSM(value="https://other.example.com")
        assert _links(monkeypatch, "https://hb.example.com", ssm)["highbyte"] == "https://hb.example.com"
        assert ssm.calls == []

    def test_sentinel_at_task_start_reads_the_live_url(self, monkeypatch):
        ssm = _SSM(value="https://hb.example.com/")
        assert _links(monkeypatch, "placeholder", ssm)["highbyte"] == "https://hb.example.com"
        assert ssm.calls == ["/adt/highbyte-url"]

    def test_sentinel_still_in_ssm_means_no_highbyte(self, monkeypatch):
        # The workshop and every deploy without HighByte.
        ssm = _SSM(value="placeholder")
        assert _links(monkeypatch, "placeholder", ssm)["highbyte"] == ""

    def test_ssm_failure_means_no_highbyte(self, monkeypatch):
        ssm = _SSM(error=RuntimeError("AccessDenied"))
        assert _links(monkeypatch, "placeholder", ssm)["highbyte"] == ""

    @pytest.mark.parametrize("env", ["PLACEHOLDER", " Placeholder "])
    def test_sentinel_in_any_case_reads_the_live_url(self, monkeypatch, env):
        ssm = _SSM(value="https://hb.example.com")
        assert _links(monkeypatch, env, ssm)["highbyte"] == "https://hb.example.com"
        assert ssm.calls == ["/adt/highbyte-url"]

    def test_ssm_sentinel_or_dummy_in_any_case_is_no_highbyte(self, monkeypatch):
        for value in ("PLACEHOLDER", "Placeholder", "dummy-value-for-/adt/highbyte-url"):
            monkeypatch.setattr(admin_routes, "_QUICKLINK_CACHE", {})
            monkeypatch.setattr(admin_routes, "_QUICKLINK_SSM_CLIENT", None, raising=False)
            assert _links(monkeypatch, "placeholder", _SSM(value=value))["highbyte"] == "", value

    def test_live_read_is_short_and_single_attempt(self, monkeypatch):
        ssm = _SSM(value="https://hb.example.com")
        _links(monkeypatch, "placeholder", ssm)
        assert ssm.config.connect_timeout == 2
        assert ssm.config.read_timeout == 2
        assert ssm.config.retries == {"total_max_attempts": 1}

    def test_the_real_client_sends_exactly_one_request_on_a_timeout(self):
        """botocore's ``max_attempts=1`` means one RETRY (two requests);
        the quick-link client must send one. Counted on a real client whose
        send is replaced by a connect timeout (no network)."""
        import boto3
        import botocore.exceptions as be
        from botocore.config import Config
        sent = []

        def timeout(**_kw):
            sent.append(1)
            raise be.ConnectTimeoutError(endpoint_url="https://ssm.example.invalid")

        client = boto3.client("ssm", region_name="us-east-1", aws_access_key_id="x",
                              aws_secret_access_key="y",  # nosec B106 - fake, never sent
                              config=Config(**admin_routes._QUICKLINK_SSM_CONFIG))
        client.meta.events.register("before-send.ssm", timeout)
        with pytest.raises(be.ConnectTimeoutError):
            client.get_parameters(Names=["/adt/highbyte-url"])
        assert sent == [1]

    def test_an_ssm_outage_costs_one_request_per_window(self, monkeypatch):
        """Every sentinel link is read in ONE request, and a failure is cached
        briefly: an outage stalls a page load once by one bounded request,
        not once per link with retries on every reload."""
        import time as _time

        class _Slow(_SSM):
            """Each request waits, then fails (both read shapes, so a per-link
            reader pays the wait once per link)."""

            def get_parameters(self, Names):
                _time.sleep(0.3)  # one request's worth of waiting
                return super().get_parameters(Names)

            def get_parameter(self, Name):
                return self.get_parameters([Name])

        ssm = _Slow(error=RuntimeError("EndpointConnectionError"))
        env = {"HIGHBYTE_URL": "pending", "ERP_DASHBOARD_URL": "pending",
               "SITEWISE_I3X_URL": "placeholder", "QUICK_DESKTOP_URL": "placeholder"}
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        with patch("boto3.client", lambda service, **kw: ssm):
            started = _time.monotonic()
            links = admin_routes.quick_links()
            first = _time.monotonic() - started
            started = _time.monotonic()
            admin_routes.quick_links()
            second = _time.monotonic() - started
        assert links["highbyte"] == links["erp_dashboard"] == ""
        assert 0.3 <= first < 0.6, first
        assert second < 0.1, second
        assert len(ssm.batches) == 1 and sorted(ssm.batches[0]) == sorted(
            ["/adt/highbyte-url", "/adt/erp-dashboard-url", "/adt/sitewise-i3x-url",
             "/adt/quick-desktop-url"])

    def test_live_read_is_cached_for_a_minute(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("time.monotonic", lambda: clock[0])
        ssm = _SSM(value="placeholder")
        _links(monkeypatch, "placeholder", ssm)
        clock[0] += 59.0
        _links(monkeypatch, "placeholder", ssm)
        assert ssm.calls == ["/adt/highbyte-url"]
        assert admin_routes._QUICKLINK_CACHE == {"/adt/highbyte-url": (1060.0, "")}
        clock[0] += 2.0
        _links(monkeypatch, "placeholder", ssm)
        assert len(ssm.calls) == 2

    def test_a_failed_read_is_cached_only_briefly(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("time.monotonic", lambda: clock[0])
        ssm = _SSM(error=RuntimeError("Throttling"))
        assert _links(monkeypatch, "placeholder", ssm)["highbyte"] == ""
        ssm.error, ssm.value = None, "https://hb.example.com"
        clock[0] += 10.0
        assert _links(monkeypatch, "placeholder", ssm)["highbyte"] == ""
        assert len(ssm.batches) == 1
        clock[0] += 6.0
        assert _links(monkeypatch, "placeholder", ssm)["highbyte"] == "https://hb.example.com"
        assert len(ssm.batches) == 2

    def test_synth_dummy_is_not_a_url_and_reads_nothing(self, monkeypatch):
        ssm = _SSM(value="https://hb.example.com")
        assert _links(monkeypatch, "dummy-value-for-/adt/highbyte-url", ssm)["highbyte"] == ""
        assert ssm.calls == []

    def test_route_offloads_the_sync_body(self, monkeypatch):
        seen = []

        async def fake_offload(fn, *args, **kwargs):
            seen.append(fn)
            return fn(*args, **kwargs)

        monkeypatch.setattr(admin_routes, "offload", fake_offload)
        monkeypatch.delenv("HIGHBYTE_URL", raising=False)
        out = asyncio.run(admin_routes.quick_links_route(types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))))
        assert seen == [admin_routes.quick_links]
        assert out["highbyte"] == ""


class _NamedSSM:
    """SSM fake with one value per parameter name."""

    def __init__(self, values):
        self.values, self.calls, self.config = dict(values), [], None

    def get_parameters(self, Names):
        self.calls.extend(Names)
        self.batches = getattr(self, "batches", 0) + 1
        return {"Parameters": [{"Name": n, "Value": self.values[n]} for n in Names
                               if n in self.values],
                "InvalidParameters": [n for n in Names if n not in self.values]}


class TestDeferredQuickLinks:
    """The workshop root starts the app before the HighByte and ERP dashboard
    stacks finish: their links start as "pending" and the root writes the real
    URLs later, with no app restart (workshop_infra/quicklink_writer_root.py)."""

    _ENV = {"HIGHBYTE_URL": "pending", "ERP_DASHBOARD_URL": "pending",
            "SITEWISE_I3X_URL": "placeholder", "QUICK_DESKTOP_URL": "placeholder"}

    def _links(self, monkeypatch, env, ssm):
        for name in self._ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        with patch("boto3.client", lambda service, **kw: ssm):
            return admin_routes.quick_links()

    def test_pending_links_are_hidden_until_written(self, monkeypatch):
        ssm = _NamedSSM({"/adt/highbyte-url": "pending", "/adt/erp-dashboard-url": "pending",
                         "/adt/sitewise-i3x-url": "placeholder",
                         "/adt/quick-desktop-url": "placeholder"})
        links = self._links(monkeypatch, self._ENV, ssm)
        assert links["highbyte"] == "" and links["erp_dashboard"] == ""
        assert links["sitewise_i3x_url"] == "" and links["quick_desktop"] == ""

    def test_written_links_appear_without_a_restart(self, monkeypatch):
        ssm = _NamedSSM({"/adt/highbyte-url": "https://hb.example.com/",
                         "/adt/erp-dashboard-url": "https://erp.example.com",
                         "/adt/sitewise-i3x-url": "placeholder",
                         "/adt/quick-desktop-url": "placeholder"})
        links = self._links(monkeypatch, self._ENV, ssm)
        assert links["highbyte"] == "https://hb.example.com"
        assert links["erp_dashboard"] == "https://erp.example.com"
        assert sorted(ssm.calls) == sorted(ssm.values)

    def test_pending_in_any_case_is_a_sentinel(self, monkeypatch):
        ssm = _NamedSSM({"/adt/erp-dashboard-url": "https://erp.example.com"})
        links = self._links(monkeypatch, {"ERP_DASHBOARD_URL": " Pending "}, ssm)
        assert links["erp_dashboard"] == "https://erp.example.com"

    def test_real_task_start_urls_read_nothing(self, monkeypatch):
        ssm = _NamedSSM({})
        links = self._links(monkeypatch, {"HIGHBYTE_URL": "https://hb.example.com",
                                          "ERP_DASHBOARD_URL": "https://erp.example.com"}, ssm)
        assert links["erp_dashboard"] == "https://erp.example.com"
        assert ssm.calls == []

    def test_each_link_has_its_own_cache(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("time.monotonic", lambda: clock[0])
        ssm = _NamedSSM({"/adt/highbyte-url": "pending",
                         "/adt/erp-dashboard-url": "https://erp.example.com"})
        env = {"HIGHBYTE_URL": "pending", "ERP_DASHBOARD_URL": "pending"}
        self._links(monkeypatch, env, ssm)
        self._links(monkeypatch, env, ssm)
        assert sorted(ssm.calls) == ["/adt/erp-dashboard-url", "/adt/highbyte-url"]
        assert ssm.batches == 1
        assert admin_routes._QUICKLINK_CACHE == {
            "/adt/highbyte-url": (1060.0, ""),
            "/adt/erp-dashboard-url": (1060.0, "https://erp.example.com")}


class TestConcurrentQuickLinkReads:
    """FastAPI runs the sync quick-links body on worker threads, so several
    page loads can find the same expired entry at once. They must share one
    client and one GetParameters request, and all get the same answer."""

    _ENV = {"HIGHBYTE_URL": "pending", "ERP_DASHBOARD_URL": "pending",
            "SITEWISE_I3X_URL": "placeholder", "QUICK_DESKTOP_URL": "placeholder"}

    def _run(self, monkeypatch, ssm, threads=8):
        import threading
        for name, value in self._ENV.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        created = []

        def client(service, **_kw):
            created.append(service)
            return ssm

        barrier = threading.Barrier(threads)
        results, errors = [None] * threads, []

        def page_load(i):
            try:
                barrier.wait(timeout=5)
                results[i] = admin_routes.quick_links()
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        with patch("boto3.client", client):
            workers = [threading.Thread(target=page_load, args=(i,)) for i in range(threads)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
        assert errors == []
        return results, created

    def test_one_client_one_request_same_answer(self, monkeypatch):
        import time as _time

        class _Slow(_SSM):
            def get_parameters(self, Names):
                _time.sleep(0.2)  # every thread arrives while the read is in flight
                return super().get_parameters(Names)

        ssm = _Slow(value="https://hb.example.com")
        results, created = self._run(monkeypatch, ssm)
        assert created == ["ssm"]
        assert len(ssm.batches) == 1
        assert all(r == results[0] for r in results)
        assert results[0]["highbyte"] == "https://hb.example.com"

    def test_an_outage_is_read_once_for_every_caller(self, monkeypatch):
        import time as _time

        class _SlowDown(_SSM):
            def get_parameters(self, Names):
                _time.sleep(0.2)
                return super().get_parameters(Names)

        ssm = _SlowDown(error=RuntimeError("EndpointConnectionError"))
        results, created = self._run(monkeypatch, ssm)
        assert created == ["ssm"] and len(ssm.batches) == 1
        assert all(r == results[0] for r in results)
        assert results[0]["highbyte"] == results[0]["erp_dashboard"] == ""


def test_concurrent_first_use_builds_one_client(monkeypatch):
    """Client creation is slow enough (endpoint and credential resolution)
    that racing threads would each build one without the init lock."""
    import threading
    import time as _time
    created = []

    def slow_client(service, **_kw):
        created.append(service)
        _time.sleep(0.1)
        return object()

    barrier = threading.Barrier(6)
    got = []

    def first_use():
        barrier.wait(timeout=5)
        got.append(admin_routes._quicklink_ssm())

    with patch("boto3.client", slow_client):
        workers = [threading.Thread(target=first_use) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
    assert created == ["ssm"]
    assert len(got) == 6 and all(c is got[0] for c in got)


class TestQuickLinkCacheOrdering:
    def test_an_older_read_never_overwrites_a_newer_one(self, monkeypatch):
        """A read that STARTED earlier must not replace the result of one that
        started later, whatever order they finish in."""
        clock = [1000.0]
        monkeypatch.setattr("time.monotonic", lambda: clock[0])
        name = "/adt/erp-dashboard-url"
        admin_routes._quicklink_store(name, "https://erp.example.com", 60.0, read_at=995.0)
        clock[0] = 1002.0
        admin_routes._quicklink_store(name, "", 15.0, read_at=990.0)  # older failure lands late
        assert admin_routes._QUICKLINK_CACHE[name] == (1060.0, "https://erp.example.com")
        admin_routes._quicklink_store(name, "https://new.example.com", 60.0, read_at=1001.0)
        assert admin_routes._QUICKLINK_CACHE[name] == (1062.0, "https://new.example.com")


def _admin_request():
    return types.SimpleNamespace(state=types.SimpleNamespace(is_admin=True))


class TestI3xHighByteLink:
    """The i3X HighByte header link appears once quick-links has a URL and a
    token, so the link it mints must use the same (live) URL."""

    def _mint(self, monkeypatch, env_url, ssm):
        from agents import connection_resolver as cr
        monkeypatch.setattr(cr, "resolve_connection", lambda sid: {"token": "tok"})
        with _patched(monkeypatch, env_url, ssm):
            return admin_routes.i3x_explorer_link("highbyte", _admin_request())

    def test_sentinel_at_task_start_mints_against_the_live_url(self, monkeypatch):
        res = self._mint(monkeypatch, "placeholder", _SSM(value="https://hb.example.com"))
        assert res["status"] == "ok", res
        assert res["href"].startswith(
            "/i3x-explorer/?url=" + quote("https://hb.example.com/i3x", safe="")), res
        assert "placeholder" not in res["href"]

    def test_no_highbyte_reports_unconfigured_not_a_placeholder_link(self, monkeypatch):
        res = self._mint(monkeypatch, "placeholder", _SSM(value="placeholder"))
        assert res == {"status": "error", "message": "HighByte URL not configured"}

    def test_route_offloads_the_sync_body(self, monkeypatch):
        seen = []

        async def fake_offload(fn, *args, **kwargs):
            seen.append(fn)
            return {"status": "ok"}

        monkeypatch.setattr(admin_routes, "offload", fake_offload)
        asyncio.run(admin_routes.i3x_explorer_link_route("highbyte", _admin_request()))
        assert seen == [admin_routes.i3x_explorer_link]

    @pytest.mark.parametrize("built", [False, True])
    def test_quick_links_report_whether_the_explorer_build_exists(
            self, monkeypatch, tmp_path, built):
        index = tmp_path / "index.html"
        if built:
            index.write_text("<!doctype html>\n")
        monkeypatch.setattr(admin_routes, "_I3X_EXPLORER_INDEX", str(index))
        assert _links(monkeypatch, None, _SSM())["i3x_explorer"] is built


class _Manager:
    def __init__(self, mode, outcomes):
        self.mode, self.outcomes = mode, outcomes

    def list_agents(self):
        return [{"system_id": sid} for sid in self.outcomes]

    def refresh_agent_model(self, system_id, _model):
        return self.outcomes[system_id]


@pytest.mark.parametrize(
    ("mode", "outcomes", "flagged"),
    [
        ("cloud", {"erp": "ok", "mes": "ok"}, False),
        ("cloud", {"erp": "ok", "mes": "unchanged"}, False),
        ("cloud", {"erp": "ok", "mes": "skipped"}, True),   # listed, no runtime
        ("cloud", {"erp": "ok", "mes": "failed"}, True),
        ("local", {"erp": "skipped", "mes": "skipped"}, False),
    ],
)
def test_fleet_refresh_reonboard_flag(monkeypatch, mode, outcomes, flagged):
    monkeypatch.setattr(admin_routes, "_get_agent_manager", lambda: _Manager(mode, outcomes))
    tally = admin_routes._refresh_agent_model_fleet("anthropic.claude-haiku-4-5")
    assert bool(tally.get("reonboard_required")) is flagged, tally


def test_fleet_refresh_manager_error_is_flagged(monkeypatch):
    def boom():
        raise RuntimeError("no manager")

    monkeypatch.setattr(admin_routes, "_get_agent_manager", boom)
    tally = admin_routes._refresh_agent_model_fleet("anthropic.claude-haiku-4-5")
    assert tally["reonboard_required"] is True
    assert tally["error"] == "RuntimeError"


def test_llm_result_carries_the_skipped_flag(monkeypatch):
    monkeypatch.setattr(admin_routes, "_get_agent_manager",
                        lambda: _Manager("cloud", {"erp": "skipped"}))
    res = admin_routes._llm_settings_result("", "anthropic.claude-haiku-4-5", False, True)
    assert res["reonboard_required"] is True
    assert res["agent_runtime_refresh"]["skipped"] == ["erp"]
