# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SFC freshness watchdog Lambda (``lambda-sfc-watchdog/handler.py``), offline.

Fake SiteWise, ECS and SSM clients stand in for boto3. These pin what the
handler reads, that it acts through exactly one ``UpdateService`` call with
``forceNewDeployment`` on the SFC service, that it records its restarts for
the hourly cap, and that every run logs one structured line.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.unit import _sfc_watchdog_modules

handler = _sfc_watchdog_modules.handler()

NOW = 1_790_000_000.0
UTC = dt.timezone.utc
SFC_CLUSTER = "arn:aws:ecs:us-east-1:111122223333:cluster/sfc"
SERVER_CLUSTER = "arn:aws:ecs:us-east-1:111122223333:cluster/servers"
SFC_SERVICE = "sfc-service"
STATE = "/watchdog/restarts"
ANCHOR_PARAMETER = "/story/anchor"


class ClientError(Exception):
    """The shape of botocore's ClientError the handler reads."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


def _at(seconds_ago: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(NOW - seconds_ago, UTC)


class FakeSitewise:
    def __init__(self, ages: dict, *, fail_batch: str | None = None, describe_errors=(),
                 entry_errors=()):
        self.ages = ages                       # external id -> seconds since the newest value (None: no value)
        self.fail_batch = fail_batch
        self.describe_errors = set(describe_errors)
        self.entry_errors = set(entry_errors)
        self.describe_calls: list[str] = []
        self.batches: list[list] = []

    def describe_asset(self, assetId, **_kw):
        ext = assetId.removeprefix("externalId:")
        self.describe_calls.append(ext)
        if ext in self.describe_errors:
            raise ClientError("ResourceNotFoundException")
        return {"assetId": f"id-{ext}", "assetProperties": [
            {"name": "Other", "id": f"other-{ext}"}, {"name": "Signal", "id": f"prop-{ext}"}]}

    def batch_get_asset_property_value(self, entries, **_kw):
        if self.fail_batch:
            raise ClientError(self.fail_batch)
        self.batches.append(entries)
        ok, errors = [], []
        for entry in entries:
            ext = entry["assetId"].removeprefix("id-")
            assert entry["propertyId"] == f"prop-{ext}"
            if ext in self.entry_errors:
                errors.append({"entryId": entry["entryId"], "errorCode": "AccessDeniedException",
                               "errorMessage": "denied"})
                continue
            age = self.ages[ext]
            item = {"entryId": entry["entryId"]}
            if age is not None:
                stamp = NOW - age
                item["assetPropertyValue"] = {
                    "value": {"doubleValue": 1.0}, "quality": "GOOD",
                    "timestamp": {"timeInSeconds": int(stamp), "offsetInNanos": int((stamp % 1) * 1e9)}}
            ok.append(item)
        return {"successEntries": ok, "errorEntries": errors, "skippedEntries": []}


class FakeEcs:
    def __init__(self, *, server_ages: dict, sfc_task_age=6 * 3600, sfc_deployed=6 * 3600,
                 in_progress=False, fail_update=None, fail_describe_services=None):
        self.server_ages = server_ages        # service name -> task age seconds (None: no task)
        self.sfc_task_age = sfc_task_age
        self.sfc_deployed = sfc_deployed
        self.in_progress = in_progress
        self.fail_update = fail_update
        self.fail_describe_services = fail_describe_services
        self.updates: list[dict] = []

    def describe_services(self, cluster, services):
        assert cluster == SFC_CLUSTER and services == [SFC_SERVICE]
        if self.fail_describe_services:
            raise ClientError(self.fail_describe_services)
        state = "IN_PROGRESS" if self.in_progress else "COMPLETED"
        return {"services": [{"serviceArn": SFC_SERVICE, "runningCount": 1, "deployments": [
            {"status": "PRIMARY", "rolloutState": state, "createdAt": _at(self.sfc_deployed)}]}],
            "failures": []}

    def list_tasks(self, cluster, serviceName, desiredStatus):
        assert desiredStatus == "RUNNING"
        if cluster == SFC_CLUSTER:
            return {"taskArns": [] if self.sfc_task_age is None else ["sfc-task"]}
        assert cluster == SERVER_CLUSTER
        age = self.server_ages[serviceName]
        return {"taskArns": [] if age is None else [f"{serviceName}-old", f"{serviceName}-task"]}

    def describe_tasks(self, cluster, tasks):
        out = []
        for arn in tasks:
            if arn == "sfc-task":
                out.append({"taskArn": arn, "lastStatus": "RUNNING", "startedAt": _at(self.sfc_task_age)})
            elif arn.endswith("-old"):
                out.append({"taskArn": arn, "lastStatus": "RUNNING", "startedAt": _at(30 * 86400)})
            else:
                age = self.server_ages[arn.removesuffix("-task")]
                out.append({"taskArn": arn, "lastStatus": "RUNNING", "startedAt": _at(age)})
        return {"tasks": out, "failures": []}

    def update_service(self, **kwargs):
        if self.fail_update:
            raise ClientError(self.fail_update)
        self.updates.append(kwargs)
        return {"service": {}}


class FakeSsm:
    """SSM parameters with versions (each put adds one, as SSM does).
    ``stale`` makes the next reads of a name return the value and version it
    had then: a run that read before a concurrent run wrote."""

    def __init__(self, values: dict, *, fail=(), fail_put: str | None = None):
        self.values = dict(values)
        self.versions = {name: 1 for name in values}
        self.fail = set(fail)
        self.fail_put = fail_put
        self.stale: dict[str, list] = {}
        self.puts: list[dict] = []

    def get_parameter(self, Name):
        if Name in self.fail:
            raise ClientError("AccessDeniedException")
        if self.stale.get(Name):
            value, version = self.stale[Name].pop(0)
            return {"Parameter": {"Name": Name, "Value": value, "Version": version}}
        if Name not in self.values:
            raise ClientError("ParameterNotFound")
        return {"Parameter": {"Name": Name, "Value": self.values[Name], "Version": self.versions[Name]}}

    def put_parameter(self, **kwargs):
        if self.fail_put:
            raise ClientError(self.fail_put)
        self.puts.append(kwargs)
        name = kwargs["Name"]
        self.values[name] = kwargs["Value"]
        self.versions[name] = self.versions.get(name, 0) + 1
        return {"Version": self.versions[name], "Tier": "Standard"}


def _config(story: bool = False) -> dict:
    sources = []
    for i, name in enumerate(("site-a", "site-b", "site-c", "site-d")):
        src = {"source": name, "asset_external_id": f"asset-{i}", "property": "Signal",
               "server": {"cluster": SERVER_CLUSTER, "service": f"server-{i}"}}
        if story and name == "site-d":
            src["story_clock"] = {"parameter": ANCHOR_PARAMETER, "loop_seconds": 28800,
                                  "rows": 5760, "interval_seconds": 5}
        sources.append(src)
    return {"sfc": {"cluster": SFC_CLUSTER, "service": SFC_SERVICE}, "state_parameter": STATE,
            "sources": sources}


def _world(ages=None, server_ages=None, ssm=None, **ecs_kw):
    ages = ages or {f"asset-{i}": 30 for i in range(4)}
    server_ages = server_ages or {f"server-{i}": 3600 for i in range(4)}
    return handler.Clients(sitewise=FakeSitewise(ages), ecs=FakeEcs(server_ages=server_ages, **ecs_kw),
                           ssm=FakeSsm(ssm if ssm is not None else {STATE: "[]"}))


@pytest.fixture(autouse=True)
def _fresh_cache():
    handler._PROPERTY_IDS.clear()
    yield
    handler._PROPERTY_IDS.clear()


def _run(clients, config=None, now=NOW):
    lines: list[str] = []
    record = handler.run(config or _config(), clients, now, log=lines.append)
    assert len(lines) == 1, "one structured log line per run"
    assert json.loads(lines[0]) == record
    return record


def test_all_fresh_logs_one_line_and_changes_nothing():
    clients = _world()
    record = _run(clients)
    assert record["event"] == "sfc-freshness-watchdog"
    assert record["action"] == "none"
    assert [s["decision"] for s in record["sources"]] == ["fresh"] * 4
    assert clients.ecs.updates == [] and clients.ssm.puts == []


def test_a_stale_source_forces_a_new_sfc_deployment_and_records_it():
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30})
    record = _run(clients)
    assert record["action"] == "restart"
    assert clients.ecs.updates == [{"cluster": SFC_CLUSTER, "service": SFC_SERVICE,
                                    "forceNewDeployment": True}]
    (put,) = clients.ssm.puts
    assert put["Name"] == STATE and put["Overwrite"] is True and put["Type"] == "String"
    assert json.loads(put["Value"]) == {"restarts": [NOW]}
    assert record["sources"][1] == {"source": "site-b", "age_seconds": 1500, "decision": "stale",
                                    "reason": "no new value for 1500 s"}


def test_the_newest_running_server_task_is_the_one_judged():
    """After a replacement the newest task counts, not the one being drained."""
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30},
                     server_ages={"server-0": 3600, "server-1": 120, "server-2": 3600, "server-3": 3600})
    record = _run(clients)
    assert record["action"] == "none"
    assert record["sources"][1]["decision"] == "skip"


def test_the_hourly_cap_is_read_from_the_restart_record():
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30},
                     ssm={STATE: json.dumps([NOW - 1800, NOW - 1200])})
    record = _run(clients)
    assert record["action"] == "none"
    assert "2 restarts in the last hour" in record["reason"]
    assert clients.ecs.updates == []


def test_the_record_keeps_a_day_of_restarts():
    """Older than an hour no longer counts toward the cap but stays in the
    record for a day: it is how a replay's one recovery is remembered."""
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30},
                     ssm={STATE: json.dumps([NOW - 90_000, NOW - 7200, NOW - 1800])})
    record = _run(clients)
    assert record["restarts_last_hour"] == 1
    assert json.loads(clients.ssm.puts[0]["Value"]) == {"restarts": [NOW - 7200, NOW - 1800, NOW]}


@pytest.mark.parametrize("value", ["not json", json.dumps({"restarts": "x"}), json.dumps({"replay": "k"}),
                                   json.dumps({"restarts": [True]}), json.dumps("text")])
def test_a_malformed_restart_record_takes_no_action(value):
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30}, ssm={STATE: value})
    record = _run(clients)
    assert record["action"] == "none" and "restart record" in record["reason"]
    assert clients.ecs.updates == []


# --- restart admission: reserve first, never two updates for one stall


def _stale_b(**kw):
    return _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30}, **kw)


def test_two_duplicate_runs_on_stale_ecs_state_update_sfc_once():
    """The second run still sees the old SFC deployment (ECS has not shown
    the new one); the first run's reservation in the record stops it."""
    clients = _stale_b()
    first, second = _run(clients), _run(clients, now=NOW + 20)
    assert (first["action"], second["action"]) == ("restart", "none")
    assert len(clients.ecs.updates) == 1
    assert "the watchdog restarted SFC 20 s ago" in second["reason"]


def test_a_run_that_read_the_record_before_another_run_reserved_does_not_update():
    """Two concurrent runs read the same record version; only the run whose
    reservation is the next version updates SFC."""
    clients = _stale_b()
    first = _run(clients)                           # read version 1, reserved version 2
    clients.ssm.stale[STATE] = [("[]", 1)]          # the second run's read was taken before that write
    second = _run(clients, now=NOW + 1)
    assert first["action"] == "restart"
    assert second["action"] == "reserve_lost"
    assert "another run" in second["reason"]
    assert len(clients.ecs.updates) == 1


def test_a_failed_reservation_write_makes_no_update():
    clients = _stale_b()
    clients.ssm.fail_put = "ThrottlingException"
    record = _run(clients)
    assert record["action"] == "reserve_failed"
    assert record["error"] == "ThrottlingException"
    assert clients.ecs.updates == []


def test_a_reservation_whose_version_is_unknown_makes_no_update():
    clients = _stale_b()
    clients.ssm.stale[STATE] = [("[]", None)]
    record = _run(clients)
    assert record["action"] == "reserve_lost"
    assert clients.ecs.updates == []


def test_a_failed_update_keeps_its_reservation():
    """Counted as an attempt: the cap and cooldown still apply to it."""
    clients = _stale_b(fail_update="AccessDeniedException")
    record = _run(clients)
    assert record["action"] == "restart_failed"
    assert "AccessDeniedException" in record["error"]
    assert json.loads(clients.ssm.values[STATE]) == {"restarts": [NOW]}
    assert _run(clients, now=NOW + 300)["action"] == "none"


def test_an_unreadable_restart_record_takes_no_action():
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30}, ssm={})
    record = _run(clients)
    assert record["action"] == "none"
    assert "restart record" in record["reason"]


def test_a_failed_sitewise_read_takes_no_action():
    clients = _world()
    clients.sitewise.fail_batch = "ThrottlingException"
    record = _run(clients)
    assert record["action"] == "none"
    assert "ThrottlingException" in record["reason"]
    assert clients.ecs.updates == []


def test_a_per_entry_error_marks_only_that_source():
    clients = _world(ages={"asset-0": 30, "asset-1": 30, "asset-2": 30, "asset-3": 30})
    clients.sitewise.entry_errors = {"asset-2"}
    record = _run(clients)
    assert record["action"] == "none"
    assert record["sources"][2]["decision"] == "error"
    assert "AccessDeniedException" in record["sources"][2]["reason"]


def test_an_asset_that_cannot_be_described_marks_only_that_source():
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30})
    clients.sitewise.describe_errors = {"asset-3"}
    record = _run(clients)
    assert record["sources"][3]["decision"] == "error"
    assert record["action"] == "restart"                       # site-b is still stale


def test_property_ids_are_resolved_once_per_container():
    clients = _world()
    _run(clients)
    _run(clients)
    assert sorted(clients.sitewise.describe_calls) == [f"asset-{i}" for i in range(4)]
    assert len(clients.sitewise.batches) == 2


def test_a_held_story_server_is_skipped_even_though_its_sentinel_is_old():
    clients = _world(ages={"asset-0": 30, "asset-1": 30, "asset-2": 30, "asset-3": None},
                     ssm={STATE: "[]", ANCHOR_PARAMETER: "1790000010;healthy"})
    record = _run(clients, _config(story=True))
    assert record["action"] == "none"
    assert record["sources"][3]["decision"] == "skip"
    assert "first placement" in record["sources"][3]["reason"]


def test_a_placed_story_server_gets_its_grace_from_the_placement():
    start = int(NOW - 120)
    clients = _world(ages={"asset-0": 30, "asset-1": 30, "asset-2": 30, "asset-3": None},
                     ssm={STATE: "[]", ANCHOR_PARAMETER: f"1789990000;0;{start};{start + 4800}"})
    record = _run(clients, _config(story=True))
    assert record["action"] == "none"
    assert record["sources"][3]["decision"] == "skip"


def test_a_running_story_server_that_stopped_delivering_restarts_sfc():
    clients = _world(ages={"asset-0": 30, "asset-1": 30, "asset-2": 30, "asset-3": 1800},
                     ssm={STATE: "[]", ANCHOR_PARAMETER: str(int(NOW - 7200))})
    record = _run(clients, _config(story=True))
    assert record["action"] == "restart"
    assert record["sources"][3]["decision"] == "stale"


def test_an_unreadable_story_parameter_is_skipped_not_guessed():
    clients = _world(ages={"asset-0": 30, "asset-1": 30, "asset-2": 30, "asset-3": 1800})
    clients.ssm.fail = {ANCHOR_PARAMETER}
    record = _run(clients, _config(story=True))
    assert record["action"] == "none"
    assert record["sources"][3]["decision"] == "skip"


def test_a_deploying_sfc_is_left_alone():
    clients = _world(ages={f"asset-{i}": 1500 for i in range(4)}, in_progress=True, sfc_deployed=120)
    record = _run(clients)
    assert record["action"] == "none"
    assert clients.ecs.updates == []


def test_an_unreadable_sfc_service_is_left_alone():
    clients = _world(ages={f"asset-{i}": 1500 for i in range(4)},
                     fail_describe_services="AccessDeniedException")
    record = _run(clients)
    assert record["action"] == "none"
    assert "AccessDeniedException" in record["reason"]


def test_a_failed_restart_is_logged_and_keeps_the_reservation():
    clients = _world(ages={"asset-0": 30, "asset-1": 1500, "asset-2": 30, "asset-3": 30},
                     fail_update="AccessDeniedException")
    record = _run(clients)
    assert record["action"] == "restart_failed"
    assert "AccessDeniedException" in record["error"]
    assert len(clients.ssm.puts) == 1


# --- the one-time history replay


BACKFILL = "/story/backfill"
REPLAY_ANCHOR = int(NOW - 2 * 86400)
REPLAY_WINDOW = f"{REPLAY_ANCHOR};{REPLAY_ANCHOR - 86400};{REPLAY_ANCHOR - 600};{int(NOW - 6 * 3600)}"


def _replay_config() -> dict:
    config = _config(story=True)
    config["replay"] = {"source": "site-d-history", "parameter": BACKFILL,
                        "server": {"cluster": SERVER_CLUSTER, "service": "server-3"}}
    return config


def _replay_world(value: str | None, *, record: str = "[]", sfc_task_age=6 * 3600, story_server_age=3600):
    ssm = {STATE: record, ANCHOR_PARAMETER: str(int(NOW - 86400))}
    if value is not None:
        ssm[BACKFILL] = value
    server_ages = {f"server-{i}": 3600 for i in range(3)} | {"server-3": story_server_age}
    return _world(ssm=ssm, sfc_task_age=sfc_task_age, server_ages=server_ages)


def _replay_verdict(record: dict) -> dict:
    (verdict,) = [s for s in record["sources"] if s["source"] == "site-d-history"]
    return verdict


# REPLAY_WINDOW has 286 points: one clean pass is (286 + 2) x 2 s = 576 s, so it
# is due 600 + 2 x 576 + 300 = 2,052 s after the later of its publication and
# the newest story-server and SFC task starts, and every pass must have failed
# (the server stopped it) 600 + 5 x 576 + 300 = 3,780 s after that.
REPLAY_DUE_SECONDS = 2052
REPLAY_STOPPED_SECONDS = 3780


def test_an_overdue_replay_restarts_sfc_and_logs_which_replay():
    clients = _replay_world(REPLAY_WINDOW)
    record = _run(clients, _replay_config())
    assert record["action"] == "restart"
    assert record["replay"] == REPLAY_WINDOW
    assert _replay_verdict(record)["decision"] == "stale"
    assert json.loads(clients.ssm.values[STATE]) == {"restarts": [NOW]}


def test_an_overdue_replay_with_no_story_server_task_restarts_nothing():
    """The story server is down or being replaced: SFC has nothing to replay
    from, and a restart now would spend the replay's one recovery."""
    clients = _replay_world(REPLAY_WINDOW, story_server_age=None)
    record = _run(clients, _replay_config())
    assert record["action"] == "none"
    assert clients.ecs.updates == [] and clients.ssm.puts == []
    verdict = _replay_verdict(record)
    assert verdict["decision"] == "skip" and verdict["reason"] == "no story server task is running"


def test_a_restart_after_the_two_pass_due_is_only_the_early_attempt():
    """A watchdog restart after the replay was due gave the server a new
    reader, but that is an ordinary pass: the server revives a replay only for
    a reader after it STOPPED it. So the watchdog waits for the stopped bound
    on the new SFC task; it does not mark the replay recovered."""
    restarted = NOW - 2500                                   # due on this server at NOW - 8,748
    clients = _replay_world(REPLAY_WINDOW, record=json.dumps({"restarts": [restarted]}),
                            story_server_age=3 * 3600, sfc_task_age=NOW - restarted - 60)
    record = _run(clients, _replay_config())
    assert record["action"] == "none" and clients.ecs.updates == []
    verdict = _replay_verdict(record)
    assert verdict["decision"] == "skip"
    assert "waiting" in verdict["reason"] and "stop" in verdict["reason"]
    assert "already restarted" not in verdict["reason"]


def test_after_the_stopped_bound_one_more_restart_is_the_replays_revival():
    early = NOW - REPLAY_STOPPED_SECONDS - 200               # its SFC task started 60 s later
    clients = _replay_world(REPLAY_WINDOW, record=json.dumps({"restarts": [early]}),
                            story_server_age=3 * 3600, sfc_task_age=NOW - early - 60)
    record = _run(clients, _replay_config())
    assert record["action"] == "restart" and record["replay"] == REPLAY_WINDOW
    assert _replay_verdict(record)["decision"] == "stale"


def test_the_revival_restart_is_the_last_one_for_the_replay():
    early = NOW - 2 * REPLAY_STOPPED_SECONDS - 600
    revival = early + REPLAY_STOPPED_SECONDS + 300
    clients = _replay_world(REPLAY_WINDOW, record=json.dumps({"restarts": [early, revival]}),
                            story_server_age=4 * 3600, sfc_task_age=NOW - revival - 60)
    record = _run(clients, _replay_config())
    assert record["action"] == "none" and clients.ecs.updates == []
    verdict = _replay_verdict(record)
    assert verdict["decision"] == "skip" and "already restarted SFC once for this replay" in verdict["reason"]


@pytest.mark.parametrize("which", ["early", "revival"])
def test_a_restart_the_sfc_task_never_followed_does_not_count(which):
    """UpdateService failed (the reservation stays for the cooldown and the
    cap): no new reader, so it is neither the early attempt nor the revival,
    and the watchdog tries that step again once the cooldown passed."""
    if which == "early":
        failed = NOW - 1200
        restarts, task_start = [failed], NOW - 3 * 3600 + 30          # SFC task older than the attempt
    else:
        early = NOW - REPLAY_STOPPED_SECONDS - 2000
        failed = NOW - 1000
        restarts, task_start = [early, failed], early + 60
    clients = _replay_world(REPLAY_WINDOW, record=json.dumps({"restarts": restarts}),
                            story_server_age=3 * 3600, sfc_task_age=NOW - task_start)
    record = _run(clients, _replay_config())
    assert record["action"] == "restart" and record["replay"] == REPLAY_WINDOW
    assert len(clients.ecs.updates) == 1


def test_a_restart_before_the_replay_was_due_does_not_count():
    clients = _replay_world(REPLAY_WINDOW, record=json.dumps({"restarts": [NOW - 9000]}),
                            story_server_age=3 * 3600, sfc_task_age=8940)
    record = _run(clients, _replay_config())
    assert record["action"] == "restart"
    assert _replay_verdict(record)["decision"] == "stale"


def test_a_story_server_restart_gives_the_replay_its_recovery_again():
    """A new server task replays from point 0 with its own bound and its own
    one revival; a restart before that task counts for nothing, and the
    record's replay field from an earlier version is ignored."""
    old = json.dumps({"restarts": [NOW - 3000], "replay": REPLAY_WINDOW})
    clients = _replay_world(REPLAY_WINDOW, record=old, story_server_age=2400, sfc_task_age=2940)
    record = _run(clients, _replay_config())
    assert record["action"] == "restart"
    assert _replay_verdict(record)["decision"] == "stale"


def test_the_replay_stays_acted_on_when_a_losing_run_overwrites_the_record():
    """Two runs read record version 1. The winner reserves version 2 for the
    overdue replay and restarts SFC; a concurrent run (a live stall, its
    replay read failed) writes version 3 without the winner's entry and loses.
    Once the replay is overdue again on the restarted SFC, the surviving
    timestamp still marks the early attempt: the watchdog waits for the
    server to stop the replay instead of restarting again."""
    clients = _replay_world(REPLAY_WINDOW)
    winner = _run(clients, _replay_config())
    assert (winner["action"], winner["replay"]) == ("restart", REPLAY_WINDOW)

    clients.sitewise.ages["asset-1"] = 1500
    clients.ssm.fail = {BACKFILL}
    clients.ssm.stale[STATE] = [("[]", 1)]
    loser = _run(clients, _replay_config(), now=NOW + 1)
    assert loser["action"] == "reserve_lost"
    assert json.loads(clients.ssm.values[STATE]) == {"restarts": [NOW + 1]}

    later = NOW + 60 + REPLAY_DUE_SECONDS + 300                # overdue again on the new SFC task
    clients.sitewise.ages = {f"asset-{i}": -(later - NOW - 10) for i in range(4)}
    clients.ssm.fail = set()
    clients.ecs.sfc_deployed, clients.ecs.sfc_task_age = 0, -60
    record = _run(clients, _replay_config(), now=later)
    assert record["action"] == "none"
    assert _replay_verdict(record)["decision"] == "skip"
    assert len(clients.ecs.updates) == 1


def test_a_replay_due_more_than_a_day_ago_is_not_acted_on():
    """The record keeps a day of restarts, so it cannot tell whether an older
    replay was already recovered; its points are past SiteWise's window by then."""
    stale_window = REPLAY_WINDOW.rsplit(";", 1)[0] + f";{int(NOW - 30 * 3600)}"
    clients = _replay_world(stale_window, story_server_age=31 * 3600)
    record = _run(clients, _replay_config())
    assert record["action"] == "none" and clients.ecs.updates == []
    verdict = _replay_verdict(record)
    assert verdict["decision"] == "skip" and "more than a day ago" in verdict["reason"]


def test_a_replay_counts_from_the_newest_sfc_task():
    """SFC restarted 10 min ago for another reason: its new reader replays
    from point 0, so the replay is not overdue yet."""
    clients = _replay_world(REPLAY_WINDOW, sfc_task_age=600)
    record = _run(clients, _replay_config())
    assert _replay_verdict(record)["decision"] == "fresh"
    assert record["action"] == "none"


@pytest.mark.parametrize("value,decided", [("pending", "skip"), (REPLAY_WINDOW + ";done", "skip"),
                                           (None, "error")])
def test_a_pending_done_or_unreadable_replay_restarts_nothing(value, decided):
    clients = _replay_world(value)
    record = _run(clients, _replay_config())
    assert record["action"] == "none" and clients.ecs.updates == []
    assert _replay_verdict(record)["decision"] == decided


def test_the_entrypoint_reads_its_configuration_from_the_environment(monkeypatch):
    clients = _world()
    monkeypatch.setenv(handler.CONFIG_ENV, json.dumps(_config()))
    monkeypatch.setattr(handler, "_aws_clients", lambda: clients)
    monkeypatch.setattr(handler.time, "time", lambda: NOW)
    printed: list[str] = []
    monkeypatch.setattr(handler, "_log", printed.append)
    record = handler.handler({}, None)
    assert record["action"] == "none"
    assert len(printed) == 1
