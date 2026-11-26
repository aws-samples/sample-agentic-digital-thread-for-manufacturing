# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Tulip table map and the saved discovery follow the CURRENT seat.

A seat-backed (ARN-sourced) Tulip system takes its URL, workspace, key scope,
table scope and key from the seat secret at use time, and the seat can switch
(mock to live and back, or gain a ``table_label_prefix``). Two things cached
what an earlier seat answered:

* the resolve-time name -> id map is cached for 600 s, and its key was the
  provision-time URL/workspace, an empty prefix and an empty credential
  fingerprint (provision leaves the token and prefix out of an ARN system's
  config, and a seat carries a raw key pair, not a ``token``), so after a seat
  or scope switch within 600 s the previous seat's map came back;
* the provision seed preferred the saved onboarding session, so a session
  saved before the scope put other sites' table names, ids and columns into
  the agent's prompt.

The map's identity now comes from one validated read of the seat (URL,
workspace, key scope, prefix and a salted fingerprint of the key, never the
key itself), and under a table scope the saved discovery is kept only where
the fresh scoped list names the same table with the same id. Unscoped systems
are unchanged and send no extra request.

Offline: fake HTTP, fake Secrets Manager, fake clocks.
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

os.environ.setdefault("POSTGRES_PASSWORD", "test-only-not-a-real-password")  # nosec B105

import agents.connection_resolver as cr  # noqa: E402
import agents.onboard as onboard  # noqa: E402
import agents.resolve_evidence as rev  # noqa: E402
import agents.system_agent as sa  # noqa: E402

SID = "tulip-mes"
SEAT_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:customer/tulip/tulip-connection-AbCdEf"
SCOPE = "Plant B - "
LIVE_HOST, MOCK_HOST = "live.tulip.co", "mock.tulip.co"

#: The live workspace: two sites' tables, plus one that only contains the prefix.
LIVE_TABLES = [
    {"id": "aSt1", "label": "Plant A - Stations"},
    {"id": "aWo1", "label": "Plant A - Work Orders"},
    {"id": "bSt1", "label": f"{SCOPE}Stations"},
    {"id": "bWo1", "label": f"{SCOPE}Work Orders"},
    {"id": "xMid", "label": f"Copy of {SCOPE}Stations"},
]
#: The mock: unprefixed labels.
MOCK_TABLES = [{"id": "mSt1", "label": "Stations"}, {"id": "mWo1", "label": "Work Orders"}]
TABLES_BY_HOST = {LIVE_HOST: LIVE_TABLES, MOCK_HOST: MOCK_TABLES}

LIVE_SEAT = {"schema": 1, "mode": "live", "base_url": f"https://{LIVE_HOST}",
             "workspace_id": "wsLIVE", "key_scope": "instance",
             "table_label_prefix": SCOPE,
             "api_key": "LIVE-KEY-1", "api_secret": "LIVE-SECRET-1"}  # nosec B105
MOCK_SEAT = {"schema": 1, "mode": "mock", "base_url": f"https://{MOCK_HOST}",
             "workspace_id": "wsMOCK", "key_scope": "workspace",
             "api_key": "MOCK-KEY", "api_secret": "MOCK-SECRET"}  # nosec B105
UNSCOPED_LIVE_SEAT = {k: v for k, v in LIVE_SEAT.items() if k != "table_label_prefix"}

#: The config provision hands an ARN system's agent and seed: the provision-time
#: URL snapshot, the seat pointer and the marker; no token and no prefix.
ARN_CFG = {"base_url": "https://snapshot.tulip.co", "auth_scheme": "basic",
           "credential_source": "secret_arn", "secret_arn": SEAT_ARN}

#: Past the seat secret's 45 s cache, inside the table map's 600 s cache.
STEP = 100.0


class _Resp:
    def __init__(self, body, status=200):
        self._body, self._status = body, status

    def raise_for_status(self):
        if self._status >= 400:
            err = RuntimeError(f"HTTP {self._status}")
            err.response = types.SimpleNamespace(status_code=self._status)
            raise err

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Hosts:
    """A fake Tulip per host (live or mock); records every URL it was sent."""

    def __init__(self):
        self.urls: list[str] = []

    def client(self, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, headers=None, params=None):
        self.urls.append(url)
        parts = urlsplit(url)
        tables = TABLES_BY_HOST.get(parts.netloc, [])
        segs = parts.path.split("/")
        if len(segs) > 4 and segs[3] == "w":
            segs = segs[:3] + segs[5:]
        rest = segs[3:]
        by_id = {t["id"]: t for t in tables}
        if rest == ["tables"]:
            return _Resp(json.dumps(tables))
        if len(rest) >= 2 and rest[1] in by_id:
            if len(rest) == 2:
                return _Resp(json.dumps({**by_id[rest[1]], "columns": []}))
            return _Resp(json.dumps([{"id": "r1"}]))
        return _Resp(json.dumps({"errorCode": "NoSuchDatabaseEntry"}), 404)

    def lists(self):
        return [u for u in self.urls if u.endswith("/tables")]


@pytest.fixture
def hosts(monkeypatch):
    fake = _Hosts()
    import agents.pinned_http as pinned_http
    monkeypatch.setattr(pinned_http, "client", fake.client)
    monkeypatch.setattr("agents.response_integrity_query.read_http_body_capped",
                        lambda response, limit: (response._body, False))
    return fake


@pytest.fixture
def clock(monkeypatch):
    """One fake clock for both caches: the seat secret's (45 s) and the table
    map's (600 s)."""
    now = {"t": 1000.0}
    monkeypatch.setattr(cr, "_monotonic", lambda: now["t"])
    monkeypatch.setattr(rev, "_memo_clock", lambda: now["t"])
    return now


@pytest.fixture
def seat(monkeypatch):
    """The seat secret's current value (a dispenser or operator switches it)."""
    state = {"value": dict(LIVE_SEAT)}
    monkeypatch.setattr(sa, "_fetch_credentials", lambda arn: dict(state["value"]))
    return state


@pytest.fixture(autouse=True)
def _fresh_memo():
    rev._reset_tulip_table_id_memo()
    yield
    rev._reset_tulip_table_id_memo()


def _store(monkeypatch, tables, seat=None):
    """A saved onboarding session holding ``tables``; ``seat`` (a seat secret
    value) records the seat the discovery read through, as discover saves it."""
    from agents.onboarding_session import SessionNotFoundError

    class _Store:
        def get_session(self, system_id):
            if tables is None:
                raise SessionNotFoundError(system_id)
            session = {"discovered_tables": tables}
            if seat is not None:
                session["discovery_seat"] = _identity(seat)
            return session

    monkeypatch.setattr("agents.onboard_agent._store", lambda: _Store())


def _identity(seat_value):
    import agents.tulip_seat_contract as tsc
    return tsc.request_identity(tsc.request_tuple(seat_value))


def _ctx(cm_items=None):
    return types.SimpleNamespace(system_id=SID, system_name="Tulip MES", schema="",
                                 cm_items=cm_items or [])


def _saved(tables):
    """A saved onboarding session's discovered_tables for ``tables``."""
    return [{"name": t["label"], "table_id": t["id"], "row_count": 0,
             "columns": [{"name": f"c_{t['id']}", "data_type": "string"}]}
            for t in tables]


# --------------------------------------------------------------------------- #
# The resolve-time table map follows the seat
# --------------------------------------------------------------------------- #

def test_the_table_map_follows_a_scope_added_to_the_seat(hosts, clock, seat):
    seat["value"] = dict(UNSCOPED_LIVE_SEAT)
    assert set(rev._tulip_name_id_map_get(SID, dict(ARN_CFG)).values()) == {
        t["id"] for t in LIVE_TABLES}
    seat["value"] = dict(LIVE_SEAT)                 # the seat gains the scope
    clock["t"] += STEP
    assert rev._tulip_name_id_map_get(SID, dict(ARN_CFG)) == {
        f"{SCOPE}Stations": "bSt1", f"{SCOPE}Work Orders": "bWo1"}


def test_the_table_map_follows_live_to_mock_to_live(hosts, clock, seat):
    live = {f"{SCOPE}Stations": "bSt1", f"{SCOPE}Work Orders": "bWo1"}
    assert rev._tulip_name_id_map_get(SID, dict(ARN_CFG)) == live
    seat["value"] = dict(MOCK_SEAT)
    clock["t"] += STEP
    assert rev._tulip_name_id_map_get(SID, dict(ARN_CFG)) == {
        "Stations": "mSt1", "Work Orders": "mWo1"}
    seat["value"] = dict(LIVE_SEAT)
    clock["t"] += STEP
    assert rev._tulip_name_id_map_get(SID, dict(ARN_CFG)) == live
    # Each list went to the host the seat named at that moment.
    assert [urlsplit(u).netloc for u in hosts.lists()][:2] == [LIVE_HOST, MOCK_HOST]


def test_a_seat_key_rotation_lists_again_and_the_key_never_reaches_the_cache_key(
        hosts, clock, seat):
    rev._tulip_name_id_map_get(SID, dict(ARN_CFG))
    rev._tulip_name_id_map_get(SID, dict(ARN_CFG))
    assert len(hosts.lists()) == 1                  # same seat: served from the cache
    seat["value"] = {**LIVE_SEAT, "api_key": "LIVE-KEY-2", "api_secret": "LIVE-SECRET-2"}
    clock["t"] += STEP
    rev._tulip_name_id_map_get(SID, dict(ARN_CFG))
    assert len(hosts.lists()) == 2                  # new key: listed again
    keys = repr(list(rev._tulip_table_id_memo.keys()))
    import base64
    for raw in ("LIVE-KEY-1", "LIVE-SECRET-1", "LIVE-KEY-2", "LIVE-SECRET-2",
                base64.b64encode(b"LIVE-KEY-2:LIVE-SECRET-2").decode()):
        assert raw not in keys, keys


# --------------------------------------------------------------------------- #
# The provision seed: a saved session never brings out-of-scope tables back
# --------------------------------------------------------------------------- #

def test_a_pre_scope_session_does_not_leak_out_of_scope_tables(hosts, clock, seat, monkeypatch):
    _store(monkeypatch, _saved(LIVE_TABLES))        # saved before the scope existed
    rows = onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    # No recorded seat: the session is not trusted, so the scoped list is read.
    assert sorted(rows) == [(f"{SCOPE}Stations", "bSt1", []),
                            (f"{SCOPE}Work Orders", "bWo1", [])]
    desc = onboard._tulip_known_tables_desc(_ctx(), dict(ARN_CFG))
    for leaked in ("Plant A", "aSt1", "aWo1", "xMid", "Copy of", "c_aSt1"):
        assert leaked not in desc, desc
    assert "(id=bSt1)" in desc and "(id=bWo1)" in desc


def test_a_session_saved_under_another_seat_is_replaced_by_the_fresh_list(
        hosts, clock, seat, monkeypatch):
    # Saved while the seat was the mock (same labels would not match: ids differ).
    _store(monkeypatch, _saved([{"id": "mSt1", "label": f"{SCOPE}Stations"}]))
    rows = onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    assert sorted(rows) == [(f"{SCOPE}Stations", "bSt1", []),
                            (f"{SCOPE}Work Orders", "bWo1", [])]


def test_an_inline_scoped_pre_scope_session_is_bound_too(hosts, monkeypatch):
    _store(monkeypatch, _saved(LIVE_TABLES))
    cfg = {"base_url": f"https://{LIVE_HOST}", "token": "dG9r", "auth_scheme": "basic",  # nosec B106
           "table_label_prefix": SCOPE}
    rows = onboard._tulip_tables_from_discovery(_ctx(), cfg)
    assert [tid for _n, tid, _c in rows] == ["bSt1", "bWo1"]


def test_the_name_only_fallback_keeps_to_the_scope(monkeypatch):
    """No session and the live list fails: the seed falls back to the concept
    mappings' table names, and under a scope only in-scope names are shown."""
    _store(monkeypatch, None)
    monkeypatch.setattr(sa._TulipClient, "get",
                        lambda self, path, params=None: (_ for _ in ()).throw(RuntimeError("down")))
    cfg = {"base_url": f"https://{LIVE_HOST}", "token": "dG9r", "auth_scheme": "basic",  # nosec B106
           "table_label_prefix": SCOPE}
    ctx = _ctx(cm_items=[{"field": "Plant A - Stations.station_id"},
                         {"field": f"{SCOPE}Stations.station_id"}])
    desc = onboard._tulip_known_tables_desc(ctx, cfg)
    assert f"{SCOPE}Stations" in desc
    assert "Plant A" not in desc, desc


def test_an_unscoped_inline_session_is_used_unchanged_with_no_request(hosts, monkeypatch):
    _store(monkeypatch, _saved(LIVE_TABLES))
    cfg = {"base_url": f"https://{LIVE_HOST}", "token": "dG9r", "auth_scheme": "basic"}  # nosec B106
    rows = onboard._tulip_tables_from_discovery(_ctx(), cfg)
    assert rows == [(t["label"], t["id"], [f"c_{t['id']}"]) for t in LIVE_TABLES]
    assert hosts.urls == []


def test_an_unscoped_seat_session_is_used_unchanged_with_no_request(hosts, clock, seat, monkeypatch):
    """A session the current (unscoped) seat produced is used as saved."""
    seat["value"] = dict(MOCK_SEAT)
    _store(monkeypatch, _saved(MOCK_TABLES), seat=MOCK_SEAT)
    rows = onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    assert rows == [(t["label"], t["id"], [f"c_{t['id']}"]) for t in MOCK_TABLES]
    assert hosts.urls == []


# --------------------------------------------------------------------------- #
# The sampler's table-id lookup under a scope
# --------------------------------------------------------------------------- #

def test_a_scoped_sampler_takes_ids_from_the_fresh_list_not_a_pre_scope_session(
        hosts, clock, seat, monkeypatch):
    _store(monkeypatch, _saved([{"id": "oldB", "label": f"{SCOPE}Stations"},
                                {"id": "aSt1", "label": "Plant A - Stations"}]))
    assert rev._tulip_table_id_for(SID, f"{SCOPE}Stations", dict(ARN_CFG)) == "bSt1"
    assert rev._tulip_table_id_for(SID, "Plant A - Stations", dict(ARN_CFG)) == ""


def test_an_unscoped_sampler_still_reads_the_session_first(hosts, clock, seat, monkeypatch):
    seat["value"] = dict(MOCK_SEAT)
    _store(monkeypatch, _saved([{"id": "sess1", "label": "Stations"}]), seat=MOCK_SEAT)
    assert rev._tulip_table_id_for(SID, "Stations", dict(ARN_CFG)) == "sess1"
    assert hosts.urls == []


# --------------------------------------------------------------------------- #
# An unavailable seat fails closed (the seat cannot be read, or is not a
# complete contract): it is never taken for an unscoped system.
# --------------------------------------------------------------------------- #

OWN_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:adt/conn/tulip-mes-AbCdEf"


@pytest.fixture
def seat_io(monkeypatch):
    """The seat secret behind Secrets Manager: its value (an exception makes
    the read fail) and how many reads reached it."""
    state = {"value": dict(LIVE_SEAT), "reads": 0}

    def _fetch(arn):
        state["reads"] += 1
        if isinstance(state["value"], Exception):
            raise state["value"]
        return dict(state["value"])

    monkeypatch.setattr(sa, "_fetch_credentials", _fetch)
    return state


def _mapped_ctx():
    return _ctx(cm_items=[{"field": "Plant A - Stations.station_id"},
                          {"field": f"{SCOPE}Stations.station_id"}])


@pytest.mark.parametrize("unavailable", [
    RuntimeError("AccessDeniedException"),
    {**LIVE_SEAT, "table_label_prefix": "Plant\nB - "},       # not a complete contract
    {k: v for k, v in LIVE_SEAT.items() if k != "workspace_id"},
], ids=["unreadable", "invalid-prefix", "missing-field"])
def test_an_unavailable_scoped_seat_seeds_nothing_from_a_pre_scope_session(
        hosts, clock, seat_io, monkeypatch, unavailable):
    seat_io["value"] = unavailable
    _store(monkeypatch, _saved(LIVE_TABLES))        # saved before the scope existed
    assert onboard._tulip_tables_from_discovery(_mapped_ctx(), dict(ARN_CFG)) == []
    assert onboard._tulip_known_tables_desc(_mapped_ctx(), dict(ARN_CFG)) == ""
    assert hosts.urls == []


def test_an_unavailable_seat_gives_the_sampler_no_session_id_and_no_cached_map(
        hosts, clock, seat_io, monkeypatch):
    rev._tulip_name_id_map_get(SID, dict(ARN_CFG))  # readable: one cached map
    assert len(rev._tulip_table_id_memo) == 1
    seat_io["value"] = RuntimeError("AccessDeniedException")
    clock["t"] += STEP                              # past the seat secret's cache
    _store(monkeypatch, _saved(LIVE_TABLES))
    assert rev._tulip_name_id_map_get(SID, dict(ARN_CFG)) == {}
    assert rev._tulip_table_id_for(SID, "Plant A - Stations", dict(ARN_CFG)) == ""
    assert rev._tulip_table_id_for(SID, f"{SCOPE}Stations", dict(ARN_CFG)) == ""
    assert len(rev._tulip_table_id_memo) == 1       # nothing new cached


def test_the_table_scope_tells_inline_seat_and_unavailable_apart(clock, seat_io):
    inline = {"base_url": f"https://{LIVE_HOST}", "table_label_prefix": SCOPE}
    assert rev.tulip_table_scope(SID, inline) == rev.TulipTableScope(prefix=SCOPE)
    assert rev.tulip_table_scope(SID, {"base_url": f"https://{LIVE_HOST}"}) == \
        rev.TulipTableScope(prefix="")
    assert rev.tulip_table_scope(SID, dict(ARN_CFG)) == rev.TulipTableScope(prefix=SCOPE)
    seat_io["value"] = RuntimeError("AccessDeniedException")
    clock["t"] += STEP
    scope = rev.tulip_table_scope(SID, dict(ARN_CFG))
    assert scope.seat_unavailable and scope.prefix == ""


def test_an_unavailable_seat_is_read_once_per_seat_ttl_and_the_miss_holds_no_data(
        hosts, clock, seat_io, monkeypatch):
    seat_io["value"] = RuntimeError("AccessDeniedException")
    _store(monkeypatch, _saved(LIVE_TABLES))
    rev._tulip_name_id_map_get(SID, dict(ARN_CFG))
    rev._tulip_table_id_for(SID, f"{SCOPE}Stations", dict(ARN_CFG))
    onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    assert seat_io["reads"] == 1
    assert all(isinstance(expires, float) for expires in rev._tulip_seat_miss.values())
    seat_io["value"] = dict(LIVE_SEAT)
    clock["t"] += cr._TTL + 1                       # the miss expires with the seat TTL
    assert rev.tulip_table_scope(SID, dict(ARN_CFG)) == rev.TulipTableScope(prefix=SCOPE)
    assert seat_io["reads"] == 2


def test_a_table_map_miss_reads_the_seat_once(hosts, clock, seat_io):
    assert rev._tulip_name_id_map_get(SID, dict(ARN_CFG)) == {
        f"{SCOPE}Stations": "bSt1", f"{SCOPE}Work Orders": "bWo1"}
    assert seat_io["reads"] == 1


def test_an_inline_own_secret_seed_uses_the_session_and_reads_no_secret(
        hosts, seat_io, monkeypatch):
    """An inline system's seed config names its own adt/conn/<id> secret and
    carries no system id: it is inline (never a seat), so the saved session is
    used exactly as before and no secret is read for a scope."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    seat_io["value"] = {"token": "dG9r", "auth_scheme": "basic"}  # nosec B105
    _store(monkeypatch, _saved(LIVE_TABLES))
    cfg = {"base_url": f"https://{LIVE_HOST}", "auth_scheme": "basic", "secret_arn": OWN_ARN}
    rows = onboard._tulip_tables_from_discovery(_ctx(), cfg)
    assert rows == [(t["label"], t["id"], [f"c_{t['id']}"]) for t in LIVE_TABLES]
    assert seat_io["reads"] == 0 and hosts.urls == []


# --------------------------------------------------------------------------- #
# A saved discovery is used only for the seat that produced it, scoped or not.
# Discover saves the seat's non-secret identity; provision and the sampler
# compare it with the seat read now.
# --------------------------------------------------------------------------- #

MOCK_MAP_ROWS = [("Stations", "mSt1", []), ("Work Orders", "mWo1", [])]


def test_a_live_scoped_session_seeds_nothing_into_a_mock_seat(hosts, clock, seat, monkeypatch):
    seat["value"] = dict(MOCK_SEAT)                 # switched to the mock before provision
    live_in_scope = [t for t in LIVE_TABLES if t["label"].startswith(SCOPE)]
    _store(monkeypatch, _saved(live_in_scope), seat=LIVE_SEAT)
    ctx = _ctx(cm_items=[{"field": f"{SCOPE}Stations.station_id"}])
    rows = onboard._tulip_tables_from_discovery(ctx, dict(ARN_CFG))
    assert sorted(rows) == MOCK_MAP_ROWS            # the mock's own tables, listed now
    desc = onboard._tulip_known_tables_desc(ctx, dict(ARN_CFG))
    for leaked in (SCOPE, "bSt1", "bWo1", "c_bSt1"):
        assert leaked not in desc, desc


def test_a_mock_session_seeds_nothing_into_a_live_scoped_seat(hosts, clock, seat, monkeypatch):
    # The mock session names a table the live scoped list also names with the
    # same id; its columns are the mock's and must not seed the live agent.
    _store(monkeypatch, [{"name": f"{SCOPE}Stations", "table_id": "bSt1",
                          "columns": [{"name": "c_mock_only"}]}], seat=MOCK_SEAT)
    rows = onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    assert sorted(rows) == [(f"{SCOPE}Stations", "bSt1", []),
                            (f"{SCOPE}Work Orders", "bWo1", [])]


def test_a_same_seat_scoped_session_keeps_its_columns(hosts, clock, seat, monkeypatch):
    live_in_scope = [t for t in LIVE_TABLES if t["label"].startswith(SCOPE)]
    _store(monkeypatch, _saved(live_in_scope), seat=LIVE_SEAT)
    rows = onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    assert rows == [(t["label"], t["id"], [f"c_{t['id']}"]) for t in live_in_scope]


def test_a_session_saved_before_seats_were_recorded_is_not_trusted(
        hosts, clock, seat, monkeypatch):
    seat["value"] = dict(MOCK_SEAT)
    _store(monkeypatch, _saved(MOCK_TABLES))        # no discovery_seat: saved before this change
    rows = onboard._tulip_tables_from_discovery(_ctx(), dict(ARN_CFG))
    assert sorted(rows) == MOCK_MAP_ROWS            # listed afresh: no saved columns
    assert len(hosts.lists()) == 1


def test_a_mapping_name_from_another_seat_is_not_seeded(monkeypatch, clock, seat):
    """The mock's list fails and the session came from the live seat: the
    concept mappings name the live seat's tables, so nothing is named."""
    seat["value"] = dict(MOCK_SEAT)
    _store(monkeypatch, _saved(LIVE_TABLES[2:4]), seat=LIVE_SEAT)
    monkeypatch.setattr(sa._TulipClient, "get",
                        lambda self, path, params=None: (_ for _ in ()).throw(RuntimeError("down")))
    ctx = _ctx(cm_items=[{"field": f"{SCOPE}Stations.station_id"}])
    assert onboard._tulip_known_tables_desc(ctx, dict(ARN_CFG)) == ""


def test_the_sampler_ignores_a_session_another_seat_produced(hosts, clock, seat, monkeypatch):
    seat["value"] = dict(MOCK_SEAT)
    _store(monkeypatch, _saved([{"id": "liveSt", "label": "Stations"}]), seat=LIVE_SEAT)
    assert rev._tulip_table_id_for(SID, "Stations", dict(ARN_CFG)) == "mSt1"


def test_the_sampler_ignores_a_seat_session_with_no_recorded_seat(hosts, clock, seat, monkeypatch):
    seat["value"] = dict(MOCK_SEAT)
    _store(monkeypatch, _saved([{"id": "oldSt", "label": "Stations"}]))
    assert rev._tulip_table_id_for(SID, "Stations", dict(ARN_CFG)) == "mSt1"


def test_an_inline_session_is_used_whatever_seat_it_records(hosts, monkeypatch):
    _store(monkeypatch, _saved(LIVE_TABLES), seat=MOCK_SEAT)
    cfg = {"base_url": f"https://{LIVE_HOST}", "token": "dG9r", "auth_scheme": "basic"}  # nosec B106
    rows = onboard._tulip_tables_from_discovery(_ctx(), cfg)
    assert rows == [(t["label"], t["id"], [f"c_{t['id']}"]) for t in LIVE_TABLES]
    assert rev._tulip_table_id_for(SID, "Plant A - Stations", cfg) == "aSt1"
    assert hosts.urls == []


# --------------------------------------------------------------------------- #
# Discover records the seat (non-secret identity only) for a seat system
# --------------------------------------------------------------------------- #

def _seat_resolved(seat_value):
    """What the resolver hands discover for a seat-backed Tulip system."""
    import agents.tulip_seat_contract as tsc
    req = tsc.request_tuple(seat_value)
    out = {"connection_type": "Tulip (Tables API)", "base_url": req.base_url,
           "workspace_id": req.workspace_id, "key_scope": req.key_scope,
           "token": req.token, "auth_scheme": req.auth_scheme,
           "credential_source": "secret_arn", "secret_arn": SEAT_ARN,
           "_secret_unresolved": False}
    if req.table_label_prefix:
        out["table_label_prefix"] = req.table_label_prefix
    return out


def test_discover_records_the_seat_identity_and_no_credential():
    import agents.onboard_agent as oa
    from agents.connection_types import TULIP
    seat_id = oa._discovery_seat(TULIP, _seat_resolved(LIVE_SEAT))
    assert seat_id == _identity(LIVE_SEAT) == {
        "base_url": f"https://{LIVE_HOST}", "workspace_id": "wsLIVE",
        "key_scope": "instance", "table_label_prefix": SCOPE}
    assert oa._discovery_seat(TULIP, _seat_resolved(MOCK_SEAT))["table_label_prefix"] == ""
    blob = json.dumps(seat_id)
    for raw in ("LIVE-KEY-1", "LIVE-SECRET-1", _seat_resolved(LIVE_SEAT)["token"]):
        assert raw not in blob


def test_discover_records_no_seat_for_an_inline_or_unresolved_system():
    import agents.onboard_agent as oa
    from agents.connection_types import TULIP
    inline = {"base_url": f"https://{LIVE_HOST}", "token": "dG9r", "auth_scheme": "basic"}  # nosec B106
    assert oa._discovery_seat(TULIP, inline) is None
    assert oa._discovery_seat(TULIP, {**_seat_resolved(LIVE_SEAT), "_secret_unresolved": True}) is None
    assert oa._discovery_seat("PostgreSQL", _seat_resolved(LIVE_SEAT)) is None


class _CapturingStore:
    def __init__(self):
        self.advanced = []

    def create_session(self, system_id, **kw):
        return {}

    def advance_status(self, system_id, from_status, to_status, **fields):
        self.advanced.append(fields)


def test_the_discover_session_saves_the_seat_beside_the_tables(monkeypatch):
    import agents.onboard_agent as oa
    from agents.connection_types import TULIP
    store = _CapturingStore()
    monkeypatch.setattr(oa, "_store", lambda: store)
    seat_id = _identity(LIVE_SEAT)
    assert oa._persist_discover_session(SID, TULIP, "", [], discovery_seat=seat_id) is None
    assert store.advanced[-1]["discovery_seat"] == seat_id
    # An inline system's session is written exactly as before: no seat field.
    assert oa._persist_discover_session(SID, TULIP, "", []) is None
    assert "discovery_seat" not in store.advanced[-1]


def test_the_session_store_keeps_the_seat_as_json():
    from agents.onboarding_session import JSON_FIELDS, _decode_item, _encode_fields
    assert "discovery_seat" in JSON_FIELDS
    seat_id = _identity(LIVE_SEAT)
    assert _decode_item(_encode_fields({"discovery_seat": seat_id}))["discovery_seat"] == seat_id
