# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SFC freshness watchdog: restart SFC when a source silently stops delivering.

Runs on a schedule. Each run reads, for every SFC source in its configuration:

* the newest stored SiteWise value of the source's sentinel channel
  (``BatchGetAssetPropertyValue``, the property id resolved once per
  container with ``DescribeAsset`` on the asset's external id);
* when the source's OPC UA server task started (``ListTasks`` and
  ``DescribeTasks`` on its service);
* for a server that follows a story clock, the SSM parameter that server
  polls, read with the server's own rule (``watchdog_clock``);

plus the SFC service's deployments and task (``DescribeServices``,
``ListTasks``, ``DescribeTasks``) and the watchdog's own restart record (an
SSM parameter). On the live story with the history replay it also reads the
replay's window parameter (``replay`` below). ``watchdog_decision.decide``
makes the call. A restart is first RESERVED in the record (the new version
must be the one right after the version this run read, so of two runs that
read the same record only one restarts), and only then one ``UpdateService``
with ``forceNewDeployment`` on the SFC service; no reservation, no update.
Every run logs exactly one JSON line.

The configuration (env ``WATCHDOG_CONFIG``) is written by the stack at synth:

    {"sfc": {"cluster": ..., "service": ...},
     "state_parameter": ...,
     "limits": {...},                                   # optional, watchdog_decision.Limits
     "sources": [{"source": ..., "asset_external_id": ..., "property": ...,
                  "server": {"cluster": ..., "service": ...},
                  "story_clock": {"parameter": ..., "loop_seconds": ...,
                                  "rows": ..., "interval_seconds": ...}}],  # story_clock optional
     "replay": {"source": ..., "parameter": ..., "server": {...}}}         # optional
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import watchdog_clock as story_clock
import watchdog_decision as decision

CONFIG_ENV = "WATCHDOG_CONFIG"

#: external id -> (asset id, {property name: property id}); kept across warm invocations.
_PROPERTY_IDS: dict[str, tuple[str, dict[str, str]]] = {}


def _log(line: str) -> None:
    print(line, flush=True)


@dataclass(frozen=True)
class Clients:
    sitewise: Any
    ecs: Any
    ssm: Any


def _aws_clients() -> Clients:
    import boto3
    from botocore.config import Config

    config = Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=15)
    return Clients(sitewise=boto3.client("iotsitewise", config=config),
                   ecs=boto3.client("ecs", config=config),
                   ssm=boto3.client("ssm", config=config))


def _code(exc: Exception) -> str:
    """The AWS error code of ``exc`` (or its type), never its message text."""
    error = getattr(exc, "response", {}).get("Error", {}) if hasattr(exc, "response") else {}
    return str(error.get("Code") or type(exc).__name__)


def _epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    return value.timestamp() if hasattr(value, "timestamp") else float(value)


# --------------------------------------------------------------------------- SiteWise


def _property_ids(sitewise, external_id: str) -> tuple[str, dict[str, str]]:
    if external_id not in _PROPERTY_IDS:
        asset = sitewise.describe_asset(assetId=f"externalId:{external_id}")
        _PROPERTY_IDS[external_id] = (
            asset["assetId"], {p["name"]: p["id"] for p in asset.get("assetProperties", [])})
    return _PROPERTY_IDS[external_id]


def _entries(sitewise, sources: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """BatchGet entries for the sources whose property resolves, and the
    per-source error for those that do not."""
    entries, errors = [], {}
    for index, src in enumerate(sources):
        try:
            asset_id, props = _property_ids(sitewise, src["asset_external_id"])
        except Exception as exc:  # noqa: BLE001 - one source's failure is reported, never raised
            errors[src["source"]] = _code(exc)
            continue
        if src["property"] not in props:
            errors[src["source"]] = f"property {src['property']} not on the asset"
            continue
        entries.append({"entryId": f"s{index}", "assetId": asset_id, "propertyId": props[src["property"]]})
    return entries, errors


def read_sentinels(sitewise, sources: list[dict]) -> tuple[dict, dict, Optional[str]]:
    """(source -> newest value epoch or None, source -> error, whole-read error)."""
    entries, errors = _entries(sitewise, sources)
    names = {f"s{i}": src["source"] for i, src in enumerate(sources)}
    times: dict[str, Optional[float]] = {}
    token = None
    try:
        while entries:
            kwargs = {"entries": entries, **({"nextToken": token} if token else {})}
            page = sitewise.batch_get_asset_property_value(**kwargs)
            for item in page.get("successEntries", []):
                stamp = (item.get("assetPropertyValue") or {}).get("timestamp")
                times[names[item["entryId"]]] = (
                    None if not stamp else stamp["timeInSeconds"] + stamp.get("offsetInNanos", 0) / 1e9)
            for item in page.get("errorEntries", []):
                errors[names[item["entryId"]]] = str(item.get("errorCode") or "error")
            for item in page.get("skippedEntries", []):
                info = item.get("errorInfo") or {}
                errors[names[item["entryId"]]] = str(info.get("errorCode") or item.get("completionStatus"))
            token = page.get("nextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 - the run reports it and takes no action
        return {}, errors, _code(exc)
    return times, errors, None


# --------------------------------------------------------------------------- ECS


def _newest_task_start(ecs, cluster: str, service: str) -> Optional[float]:
    arns = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING").get("taskArns", [])
    if not arns:
        return None
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns).get("tasks", [])
    starts = [_epoch(t.get("startedAt")) for t in tasks if t.get("lastStatus") == "RUNNING"]
    starts = [s for s in starts if s is not None]
    return max(starts) if starts else None


def read_sfc(ecs, sfc: dict) -> decision.SfcState:
    try:
        out = ecs.describe_services(cluster=sfc["cluster"], services=[sfc["service"]])
        (service,) = out.get("services", []) or [None]
        if service is None:
            return decision.SfcState(error="service not found")
        deployments = service.get("deployments", [])
        created = [_epoch(d.get("createdAt")) for d in deployments]
        created = [c for c in created if c is not None]
        return decision.SfcState(
            deployment_in_progress=(len(deployments) > 1
                                    or any(d.get("rolloutState") == "IN_PROGRESS" for d in deployments)),
            newest_deployment=max(created) if created else None,
            task_started=_newest_task_start(ecs, sfc["cluster"], sfc["service"]),
            running=int(service.get("runningCount", 0)))
    except Exception as exc:  # noqa: BLE001 - the run reports it and takes no action
        return decision.SfcState(error=_code(exc))


# --------------------------------------------------------------------------- per source


def _story_view(ssm, cfg: dict, now: float, window: float) -> story_clock.ServerView:
    try:
        value = ssm.get_parameter(Name=cfg["parameter"])["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 - never guess the server's clock
        return story_clock.unreadable(_code(exc))
    return story_clock.server_view(
        value, now, rows=int(cfg["rows"]), loop_seconds=int(cfg["loop_seconds"]), window_seconds=window,
        interval_seconds=float(cfg.get("interval_seconds", story_clock.MODEL_INTERVAL_SECONDS)))


def _source_state(clients: Clients, src: dict, now: float, limits: decision.Limits,
                  times: dict, errors: dict) -> decision.SourceState:
    name = src["source"]
    base = {"source": name, "value_time": times.get(name), "read_error": errors.get(name)}
    try:
        started = _newest_task_start(clients.ecs, src["server"]["cluster"], src["server"]["service"])
    except Exception as exc:  # noqa: BLE001 - reported on the source, never raised
        return decision.SourceState(server_since=None, server_error=_code(exc), **base)
    if "story_clock" not in src:
        return decision.SourceState(server_since=started, **base)
    view = _story_view(clients.ssm, src["story_clock"], now, limits.stale_seconds)
    since = started if started is None or view.since is None else max(started, view.since)
    return decision.SourceState(server_since=since, publishing=view.publishing,
                                publishing_reason=view.reason, **base)


@dataclass(frozen=True)
class RestartRecord:
    """The watchdog's own record (SSM): its restarts (epoch seconds, kept for
    ``watchdog_decision.RECORD_RETENTION_SECONDS``) and the parameter version
    read. Stored as ``{"restarts": [...]}``; a bare list (the first form) is
    the restarts alone, and any other key (an earlier form kept a ``replay``
    key) is ignored: which replay a restart recovered is derived from the
    restart times, which a losing concurrent write cannot erase."""

    restarts: list
    version: Optional[int]


def _read_record(ssm, name: str) -> Optional[RestartRecord]:
    """The record, or None when it cannot be read or is malformed (decide()
    then refuses to restart)."""
    try:
        parameter = ssm.get_parameter(Name=name)["Parameter"]
        value = json.loads(parameter["Value"])
    except Exception:  # noqa: BLE001 - unreadable record: no restart
        return None
    if isinstance(value, list):
        restarts = value
    elif isinstance(value, dict) and isinstance(value.get("restarts"), list):
        restarts = value["restarts"]
    else:
        return None
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in restarts):
        return None
    version = parameter.get("Version")
    return RestartRecord(list(restarts), version if isinstance(version, int) else None)


def _replay_state(clients: Clients, cfg: dict, now: float, sfc: decision.SfcState,
                  record: Optional[RestartRecord]) -> decision.ReplayState:
    """The story server's history replay: its window parameter and since when
    the server and SFC have run (either restarting replays from point 0).

    It is judged only while the story server has a running task, and only
    from the restart times (which a losing concurrent write cannot erase),
    counting those at or after the replay's first due time on this server
    task (from the later of the window's publication and the server task's
    start, not SFC's). A restart is a new reader only once an SFC task
    started after it. The first such reader after the due time is an early
    attempt: an ordinary pass, not the server's revival. The watchdog then
    waits until every pass must have failed and the server stopped the
    replay (``stopped_by`` from the current SFC task) and restarts once more;
    a reader at least that long after an earlier attempt is the replay's one
    revival (``acted``)."""
    source = cfg["source"]
    try:
        value = clients.ssm.get_parameter(Name=cfg["parameter"])["Parameter"]["Value"]
        server_started = _newest_task_start(clients.ecs, cfg["server"]["cluster"], cfg["server"]["service"])
    except Exception as exc:  # noqa: BLE001 - never guess the replay's state
        return decision.ReplayState(source, "error", reason=f"cannot read the replay ({_code(exc)})")
    if server_started is None:
        return decision.ReplayState(source, "skip", reason="no story server task is running")
    since = max(server_started, sfc.task_started or 0.0)
    view = story_clock.replay_view(value, now, since=since)
    if view.status != "overdue":
        return decision.ReplayState(source, view.status, key=view.key, reason=view.reason)
    first_due = story_clock.replay_view(value, now, since=server_started).due
    if now - first_due > decision.RECORD_RETENTION_SECONDS:
        return decision.ReplayState(source, "skip", key=view.key,
                                    reason="the replay was due more than a day ago, beyond the restart record")
    attempts = [t for t in (record.restarts if record is not None else []) if t >= first_due]
    # A restart counts as a new reader only once an SFC task started after it;
    # one whose UpdateService failed keeps its cooldown and cap, nothing more.
    readers = [t for t in attempts if sfc.task_started is not None and t < sfc.task_started]
    if any(b >= story_clock.replay_view(value, b, since=a).stopped_by for a in attempts for b in readers):
        return decision.ReplayState(source, "overdue", key=view.key, reason=view.reason, acted=True)
    if readers and now < view.stopped_by:
        return decision.ReplayState(
            source, "waiting", key=view.key,
            reason=(f"{view.reason}; an SFC restart gave it a new reader, waiting "
                    f"{round(view.stopped_by - now)} s for the server to stop it before its one revival"))
    return decision.ReplayState(source, "overdue", key=view.key, reason=view.reason)


def _reserve(clients: Clients, config: dict, now: float, prior: RestartRecord,
             result: decision.Decision) -> Optional[dict]:
    """Write this restart into the record BEFORE updating SFC. Returns the
    log fields of a reservation that did not hold (no update then), else None.
    The write must be the version right after the one this run read: if
    another run wrote in between, that run owns the restart."""
    value = {"restarts": decision.kept_restarts(prior.restarts, now) + [now]}
    try:
        out = clients.ssm.put_parameter(Name=config["state_parameter"], Value=json.dumps(value),
                                        Type="String", Overwrite=True)
    except Exception as exc:  # noqa: BLE001 - no reservation, no restart
        return {"action": "reserve_failed", "error": _code(exc)}
    written = out.get("Version") if isinstance(out, dict) else None
    if prior.version is None or written != prior.version + 1:
        return {"action": "reserve_lost",
                "reason": f"{result.reason}; not restarting: another run wrote the restart record "
                          f"(read version {prior.version}, wrote {written})"}
    return None


def _restart(clients: Clients, config: dict, now: float, prior: RestartRecord,
             result: decision.Decision, record: dict) -> None:
    lost = _reserve(clients, config, now, prior, result)
    if lost:
        record.update(lost)
        return
    sfc = config["sfc"]
    try:
        clients.ecs.update_service(cluster=sfc["cluster"], service=sfc["service"], forceNewDeployment=True)
    except Exception as exc:  # noqa: BLE001 - logged; the reservation stays, counted as an attempt
        record.update(action="restart_failed", error=_code(exc))


def run(config: dict, clients: Clients, now: float, log: Optional[Callable[[str], None]] = None) -> dict:
    """One watchdog run: read, decide, act at most once, log one line."""
    limits = decision.Limits.from_config(config.get("limits"))
    sources = config["sources"]
    times, errors, sitewise_error = read_sentinels(clients.sitewise, sources)
    states = [_source_state(clients, src, now, limits, times, errors) for src in sources]
    sfc = read_sfc(clients.ecs, config["sfc"])
    prior = _read_record(clients.ssm, config["state_parameter"])
    replay = _replay_state(clients, config["replay"], now, sfc, prior) if config.get("replay") else None
    result = decision.decide(now, states, sfc, restarts=None if prior is None else prior.restarts,
                             limits=limits, sitewise_error=sitewise_error, replay=replay)
    record = result.as_record()
    if result.replay_key:
        record["replay"] = result.replay_key
    if result.restart:
        _restart(clients, config, now, prior, result, record)
    (log or _log)(json.dumps(record, sort_keys=True))
    return record


def handler(event, context):  # noqa: ARG001 - scheduled invocation, no payload
    config = json.loads(os.environ[CONFIG_ENV])
    return run(config, _aws_clients(), time.time())
