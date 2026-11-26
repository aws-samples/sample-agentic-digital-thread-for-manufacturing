# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Whether to restart SFC: the watchdog's decision, with no AWS call.

SFC reads each OPC UA server through its own source. A source can stay
"connected" in SFC while its subscription delivers nothing, and SFC's health
check stays green, so one source's SiteWise values stop while the others
continue. Only an SFC restart clears it. The watchdog reads, for each source,
the latest stored value of one channel that changes on every server tick (its
sentinel). Here it decides:

* a source is STALE when its sentinel has no value newer than
  ``stale_seconds`` and its server has been running, and publishing, for
  longer than ``server_min_age_seconds``;
* a source whose server publishes nothing by design (a story clock waiting
  or held, or one row served for the whole window), whose server has no
  running task, or whose reads failed is never stale;
* SFC is restarted when at least one source is stale, unless the SiteWise
  read failed as a whole, SFC is deploying, has no running task, was
  deployed less than ``deployment_cooldown_seconds`` ago or restarted by the
  watchdog that recently, has a task younger than
  ``sfc_min_task_age_seconds``, or was already restarted
  ``max_restarts_per_hour`` times in the last hour (or that record is
  unreadable);
* the story server's one-time history replay is judged like one more
  source: stale when it is overdue, at most once per replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

HOUR = 3600.0


@dataclass(frozen=True)
class Limits:
    stale_seconds: float = 300
    server_min_age_seconds: float = 300
    sfc_min_task_age_seconds: float = 300
    deployment_cooldown_seconds: float = 900
    max_restarts_per_hour: int = 2

    @classmethod
    def from_config(cls, raw: Optional[dict]) -> "Limits":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})


@dataclass(frozen=True)
class SourceState:
    """One source as read this run.

    ``value_time``: epoch seconds of the sentinel's newest stored value (None:
    none stored). ``server_since``: since when the source's server has been
    running and publishing, the later of its newest running task's start and
    the start of its current publishing (None: no running task)."""

    source: str
    value_time: Optional[float]
    server_since: Optional[float]
    read_error: Optional[str] = None
    server_error: Optional[str] = None
    publishing: bool = True
    publishing_reason: str = ""


@dataclass(frozen=True)
class SfcState:
    """The SFC service as read this run (``error`` set when it could not be read)."""

    error: Optional[str] = None
    deployment_in_progress: bool = False
    newest_deployment: Optional[float] = None
    task_started: Optional[float] = None
    running: int = 0


@dataclass(frozen=True)
class ReplayState:
    """The story server's one-time history replay, as read this run.

    ``status``: ``none`` (no window), ``done``, ``running`` (not due yet),
    ``overdue``, ``waiting`` (an early restart gave it a new reader; the
    server has not had time to stop it), ``skip`` (not judged: no story
    server task, or due longer ago than the restart record covers) or
    ``error`` (unreadable). ``acted``: a new SFC reader arrived after the
    server must have stopped the replay, which is the one revival a stopped
    replay gets."""

    source: str
    status: str
    key: Optional[str] = None
    reason: str = ""
    acted: bool = False


def judge_replay(replay: ReplayState) -> Verdict:
    if replay.status == "error":
        return Verdict(replay.source, None, "error", replay.reason or "cannot read the replay")
    if replay.status == "running":
        return Verdict(replay.source, None, "fresh", replay.reason)
    if replay.status != "overdue":
        return Verdict(replay.source, None, "skip", replay.reason or f"replay {replay.status}")
    if replay.acted:
        return Verdict(replay.source, None, "skip",
                       f"{replay.reason}; the watchdog already restarted SFC once for this replay "
                       "after the server stopped it")
    return Verdict(replay.source, None, "stale", replay.reason)


@dataclass(frozen=True)
class Verdict:
    source: str
    age: Optional[float]
    decision: str       # fresh | stale | skip | error
    reason: str

    def as_record(self) -> dict:
        age = None if self.age is None else int(round(self.age))
        return {"source": self.source, "age_seconds": age, "decision": self.decision, "reason": self.reason}


@dataclass(frozen=True)
class Decision:
    restart: bool
    reason: str
    sources: tuple[Verdict, ...]
    restarts_last_hour: Optional[int] = None
    extra: dict = field(default_factory=dict)
    #: The history replay this restart is for (None: not for the replay).
    replay_key: Optional[str] = None

    def as_record(self) -> dict:
        return {"event": "sfc-freshness-watchdog", "action": "restart" if self.restart else "none",
                "reason": self.reason, "restarts_last_hour": self.restarts_last_hour,
                "sources": [v.as_record() for v in self.sources], **self.extra}


def recent_restarts(restarts: Sequence, now: float) -> list[float]:
    """The numeric restart times within the last hour, oldest first."""
    kept = [float(t) for t in restarts
            if isinstance(t, (int, float)) and not isinstance(t, bool) and now - float(t) < HOUR]
    return sorted(kept)


#: How long the restart record keeps a restart. The last hour counts toward the
#: cap; the whole day is how a history replay's one recovery is remembered
#: (a watchdog restart after the replay was due), and a replay due longer ago
#: than this is not acted on, because the record can no longer tell.
RECORD_RETENTION_SECONDS = 24 * HOUR


def kept_restarts(restarts: Sequence, now: float) -> list[float]:
    """The numeric restart times within :data:`RECORD_RETENTION_SECONDS`, oldest first."""
    kept = [float(t) for t in restarts if isinstance(t, (int, float)) and not isinstance(t, bool)
            and now - float(t) < RECORD_RETENTION_SECONDS]
    return sorted(kept)


def judge(now: float, src: SourceState, limits: Limits) -> Verdict:
    """One source's verdict."""
    age = None if src.value_time is None else now - src.value_time
    if src.read_error:
        return Verdict(src.source, age, "error", f"SiteWise read failed: {src.read_error}")
    if src.server_error:
        return Verdict(src.source, age, "error", f"cannot read the OPC UA server task: {src.server_error}")
    if not src.publishing:
        return Verdict(src.source, age, "skip", src.publishing_reason or "the server publishes no value")
    if age is not None and age <= limits.stale_seconds:
        return Verdict(src.source, age, "fresh", "")
    if src.server_since is None:
        return Verdict(src.source, age, "skip", "no OPC UA server task is running")
    server_age = now - src.server_since
    if server_age <= limits.server_min_age_seconds:
        return Verdict(src.source, age, "skip", f"the OPC UA server started {int(server_age)} s ago")
    reason = "no value stored yet" if age is None else f"no new value for {int(round(age))} s"
    return Verdict(src.source, age, "stale", reason)


def _sfc_blocker(now: float, sfc: SfcState, limits: Limits) -> Optional[str]:
    """Why SFC must not be restarted now, or None."""
    if sfc.error:
        return f"cannot read the SFC service: {sfc.error}"
    if sfc.deployment_in_progress:
        return "an SFC deployment is in progress"
    if sfc.running < 1 or sfc.task_started is None:
        return "no SFC task is running"
    if sfc.newest_deployment is not None:
        deployed = now - sfc.newest_deployment
        if deployed < limits.deployment_cooldown_seconds:
            return f"the SFC deployment is {int(deployed)} s old (cooldown {int(limits.deployment_cooldown_seconds)} s)"
    task_age = now - sfc.task_started
    if task_age < limits.sfc_min_task_age_seconds:
        return f"the SFC task started {int(task_age)} s ago"
    return None


def decide(now: float, sources: Sequence[SourceState], sfc: SfcState, *,
           restarts: Optional[Sequence], limits: Limits = Limits(),
           sitewise_error: Optional[str] = None, replay: Optional[ReplayState] = None) -> Decision:
    """The run's decision. ``restarts`` is the watchdog's own restart record
    (epoch seconds); None means it could not be read. Its newest entry is a
    cooldown like the SFC deployment's age: a duplicate run can still see the
    old deployment. ``replay`` is the story server's history replay, judged
    like one more source."""
    verdicts = tuple(judge(now, src, limits) for src in sources)
    if replay is not None:
        verdicts += (judge_replay(replay),)
    recent = None if restarts is None else recent_restarts(restarts, now)
    count = None if recent is None else len(recent)

    def no(reason: str) -> Decision:
        return Decision(False, reason, verdicts, count)

    if sitewise_error:
        return no(f"the SiteWise read failed: {sitewise_error}")
    stale = [v.source for v in verdicts if v.decision == "stale"]
    if not stale:
        return no("no source is stale")
    blocker = _sfc_blocker(now, sfc, limits)
    if blocker:
        return no(f"stale: {', '.join(stale)}; not restarting: {blocker}")
    if recent is None:
        return no(f"stale: {', '.join(stale)}; not restarting: the restart record is unreadable")
    if recent and now - recent[-1] < limits.deployment_cooldown_seconds:
        return no(f"stale: {', '.join(stale)}; not restarting: the watchdog restarted SFC "
                  f"{int(now - recent[-1])} s ago (cooldown {int(limits.deployment_cooldown_seconds)} s)")
    if count >= limits.max_restarts_per_hour:
        return no(f"stale: {', '.join(stale)}; not restarting: {count} restarts in the last hour")
    replay_key = replay.key if replay is not None and replay.source in stale else None
    return Decision(True, f"stale: {', '.join(stale)}", verdicts, count, replay_key=replay_key)
