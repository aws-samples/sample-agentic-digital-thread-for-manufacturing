# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudFormation provider handler for ``Custom::ADTStoryHistory`` (live story).

Create/Update: connect to the ERP database (``DB_SECRET_NAME``) and the seed
database (``SEED_DB_SECRET_NAME``), then write the deterministic story history
(``erp_simulation.story_history``). The first anchor is the packaged CR
``Anchor`` property (epoch string, normalised by
``story_replan.normalize_anchor``; source ``packaged``) or, when unset, the
deploy anchor ``condition_monitor.snap_anchor(now, "down")`` (source
``deploy``). The writer keeps a stored anchor, so an Update never moves the
story clock, EXCEPT an Update whose ``Anchor`` is set and differs from the old
one: that re-plans through ``story_replan.replan(source="packaged")`` (ADR-0089 D14),
and an Update whose ``LoopSeconds`` (packaged loop length) was set,
changed or unset: that re-plans the STORED anchor, its source kept, under the
geometry ``condition_monitor`` read at import (no stored anchor: the first
write). ``story_replan`` sees the stored geometry differ and retires the
story number namespace, not ``plan(old)``. That re-plan is CAS-guarded on the
pre-read anchor: a lost CAS re-reads and retries ONCE, then gives up and
reports the stored clock (logged; the stack does not fail).
``Data`` carries ``AnchorEpoch``, ``AnchorSource`` and ``FailedRunNumber``.
Errors RAISE so the stack fails visibly.

Direct invoke (RequestResponse, not CloudFormation; ADR-0089, D13):
``{"action": "place", "alarm_at": <epoch> | "phase": str, "elapsed_minutes":
n, "expected_anchor": <epoch|null>}`` re-plans with source ``placed`` and
returns the ``replan`` report. It NEVER raises (the ``story_replan`` import
is inside the guard): a CAS mismatch returns ``{"error": "stale_anchor",
"message"}``, bad INPUT ``{"error": "invalid", "message"}`` and any other
failure - a server-side ValueError inside the re-plan included -
``{"error": "internal", "message": <exception class name>}`` (the detail is
logged here, never returned).

``{"action": "place_live", "alarm_at": <epoch>, "expected_live_anchor":
<epoch|null>}`` (ADR-0095) moves ONLY the live clock through
``story_replan.replan_live``: the sensor feed, the condition orders and the
story runs follow the new alarm while the history, its failed batch and every
run number stay as written. The CAS is on the LIVE anchor (the lookup's
``live_anchor_epoch``). A move the model refuses (no clock, drifted geometry,
an alarm inside the history) answers ``invalid``.

``{"action": "reset", "expected_anchor": <epoch|null>}`` (ADR-0091 Design C,
the reset failsafe) re-plans onto the DEFAULT anchor through
``story_replan.reset`` - the same CAS-guarded, locked ``replan`` - with the
default's own source; the report adds ``reset: true``, ``default_anchor`` and
``default_source``. No stored default -> ``{"error": "no_default_anchor",
"message"}``. The default (``story_default_anchor`` / ``story_default_source``)
is recorded, best effort, on Create and on every Update that re-plans (a
changed ``Anchor`` or ``LoopSeconds``): the anchor planned onto when its source
is packaged | deploy (a stored ``placed`` clock records the packaged
``Anchor``, or on Create the deploy anchor). EVERY Update - the unchanged one
included - then back-fills the default when none (well-formed) is stored, so
an account upgraded in place from before Design C gets a Reset: the packaged
``Anchor`` when set, else the stored anchor when its source is packaged |
deploy (source kept), else (a ``placed`` clock) the deploy anchor ``now``
snapped down (source ``deploy``). A stored default is never overwritten by it.

Delete (live -> frozen), two independent best-effort steps:

1. ERP database: remove every ``story_*`` simulation_config key
   (``story_history.CONFIG_KEYS``), so the simulator's live clock and defect
   switch off. The ERP history rows stay (ordinary demo data).
2. Seed database: ``story_history.restore_frozen_seed`` reopens the frozen-era
   orders the live story superseded (each to its recorded status), removes
   the supersede / prior-era description marks and deletes the history-owned
   CMMS rows, so the frozen story sees exactly its own seed.

Delete NEVER raises: each failing step is logged (exception class and message
only; no credentials are ever logged) and the resource is reported deleted so
a stack rollback or teardown is never blocked.

Every connection carries a server-side ``statement_timeout``
(:data:`STATEMENT_TIMEOUT_MS`), so a blocked statement cannot hold the
function (and the stack operation) until the Lambda timeout.
"""
import logging
import math
import os
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PHYSICAL_ID = "adt-story-history"
STATEMENT_TIMEOUT_MS = 15000


def _connect(secret_env: str):
    """A pgdriver connection from the Secrets Manager secret named by ``secret_env``."""
    from erp_simulation import pgdriver
    from erp_simulation.config import DatabaseConfig

    secret = os.environ.get(secret_env, "")
    if not secret:
        raise RuntimeError(f"{secret_env} is not set")
    cfg = DatabaseConfig.from_secrets_manager(
        secret, os.environ.get("AWS_REGION", os.environ.get("REGION", "us-east-1")))
    return pgdriver.connect(host=cfg.host, port=cfg.port, dbname=cfg.database,
                            user=cfg.user, password=cfg.password,
                            connect_timeout=cfg.connection_timeout,
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


def _now() -> int:
    """This invocation's wall clock (the deploy anchor and the re-plan ``now``)."""
    return int(time.time())


def _days(event) -> int:
    return int((event.get("ResourceProperties") or {}).get("Days", 3))


def _anchor_prop(event, key: str) -> str:
    """The CR ``Anchor`` property of ``event[key]`` (``""`` when absent or unset)."""
    return str((event.get(key) or {}).get("Anchor") or "").strip()


def _anchor_changed(event) -> bool:
    """ADR-0089 D14: an Update re-plans ONLY for a set ``Anchor`` that differs from the
    old one. Unset (missing or ``""``) never moves the stored clock."""
    new = _anchor_prop(event, "ResourceProperties")
    return bool(new) and new != _anchor_prop(event, "OldResourceProperties")


def _first_anchor(event, now: int):
    """``(anchor, source)`` for a first write: the normalised packaged
    ``Anchor``, else the deploy anchor (``now`` snapped down onto the grid)."""
    from erp_simulation import condition_monitor, story_replan

    raw = _anchor_prop(event, "ResourceProperties")
    if raw:
        return story_replan.normalize_anchor(int(raw), now), "packaged"
    return condition_monitor.snap_anchor(now, "down"), "deploy"


def _with_connections(fn):
    """``fn(erp_conn, seed_conn)`` with both connections closed afterwards."""
    erp_conn = _connect("DB_SECRET_NAME")
    try:
        seed_conn = _connect("SEED_DB_SECRET_NAME")
        try:
            return fn(erp_conn, seed_conn)
        finally:
            seed_conn.close()
    finally:
        erp_conn.close()


def _stored_value(erp_conn, key: str, fallback=None):
    """The stored ``simulation_config`` value of ``key`` (``fallback`` when absent)."""
    with erp_conn.cursor() as cur:
        cur.execute("SELECT config_value FROM simulation_config WHERE config_key = %s", (key,))
        row = cur.fetchone()
    return str(row[0]) if row else fallback


def _stored_source(erp_conn, fallback: str) -> str:
    """The stored ``story_anchor_source`` (the writer keeps a stored anchor,
    so the plan's own source is only right on a first write)."""
    return _stored_value(erp_conn, "story_anchor_source", fallback)


def _cfn_data(anchor, source: str, failed_run_number: str) -> dict:
    return {"AnchorEpoch": str(int(anchor)), "AnchorSource": source,
            "FailedRunNumber": failed_run_number}


def _write_history(event) -> dict:
    """Create / unchanged Update: the idempotent writer (a stored anchor wins)."""
    from erp_simulation import story_history

    now = _now()
    anchor, source = _first_anchor(event, now)
    story_plan = story_history.plan(anchor, _days(event), source=source, now=now)

    def write(erp_conn, seed_conn):
        result = story_history.write(erp_conn, seed_conn, story_plan)
        logger.info("story history written: %s", result)
        return _cfn_data(result["anchor_epoch"], _stored_source(erp_conn, source),
                         result["failed_run_number"])
    return _with_connections(write)


def _replan_packaged(event) -> dict:
    """Update with a changed ``Anchor``: re-plan onto it (source ``packaged``)."""
    from erp_simulation import story_replan

    anchor, now = int(_anchor_prop(event, "ResourceProperties")), _now()
    report = _with_connections(lambda erp_conn, seed_conn: story_replan.replan(
        erp_conn, seed_conn, anchor, now=now, source="packaged"))
    logger.info("story clock re-planned (packaged): %s", report)
    return _cfn_data(report["new_anchor"], report["source"], report["failed_run_number"])


def _loop_prop(event, key: str) -> str:
    """The CR ``LoopSeconds`` property of ``event[key]`` (``""`` when absent or unset)."""
    return str((event.get(key) or {}).get("LoopSeconds") or "").strip()


def _loop_changed(event) -> bool:
    """The packaged loop length (ADR-0091) was set, changed or unset. The geometry
    itself reaches this function only through ``condition_monitor`` (its
    import-time env read); the property is the re-run trigger."""
    return _loop_prop(event, "ResourceProperties") != _loop_prop(event, "OldResourceProperties")


LOOP_REPLAN_ATTEMPTS = 2  # the pre-read, then ONE re-read after a lost compare-and-swap


def _replan_stored_once(erp_conn, seed_conn, now: int):
    """Re-plan the stored anchor, CAS-guarded on it (None: nothing stored).
    The pre-reads run outside the re-plan's lock, so the CAS is what makes a
    placement that committed in between raise StaleAnchor instead of being
    silently overwritten."""
    from erp_simulation import story_history, story_replan

    stored = story_history.read_stored_anchor(erp_conn)
    if stored is None:
        return None
    return story_replan.replan(erp_conn, seed_conn, stored, now=now,
                               source=_stored_source(erp_conn, "deploy"), expected_anchor=stored)


def _stored_report(erp_conn) -> dict:
    """What a given-up loop re-plan reports: the clock as stored now."""
    from erp_simulation import story_history

    stored = story_history.read_stored_anchor(erp_conn)
    return {"gave_up": True, "new_anchor": stored, "source": _stored_source(erp_conn, "deploy"),
            "failed_run_number": _stored_value(erp_conn, "story_failed_run_number", "")}


def _replan_stored_cas(erp_conn, seed_conn, now: int):
    """:func:`_replan_stored_once`, re-read and retried once on StaleAnchor;
    a second loss gives up and reports the stored clock (a concurrent
    placement owns it and ran under the same, new, geometry)."""
    from erp_simulation import story_replan

    for attempt in range(1, LOOP_REPLAN_ATTEMPTS + 1):
        try:
            return _replan_stored_once(erp_conn, seed_conn, now)
        except story_replan.StaleAnchor as err:
            logger.warning("story clock loop re-plan: stale anchor, lost the CAS (attempt %d of %d): %s",
                           attempt, LOOP_REPLAN_ATTEMPTS, str(err)[:300])
    report = _stored_report(erp_conn)
    logger.warning("story clock loop re-plan gave up after %d stale attempts; reporting the stored "
                   "clock: %s", LOOP_REPLAN_ATTEMPTS, report)
    return report if report["new_anchor"] is not None else None


def _replan_stored(event) -> dict:
    """Update with a changed ``LoopSeconds``: re-plan the STORED anchor (its
    source kept, CAS on it) under the new geometry; no stored anchor -> the
    first write."""
    now = _now()
    report = _with_connections(lambda erp_conn, seed_conn: _replan_stored_cas(erp_conn, seed_conn, now))
    if report is None:
        return _write_history(event)
    logger.info("story clock re-planned (loop change): %s", report)
    return _cfn_data(report["new_anchor"], report["source"], report["failed_run_number"])


def _default_for(event, data: dict, first: bool):
    """The ``(anchor, source)`` the reset failsafe restores (ADR-0091 Design C):
    the anchor this CR planned onto when its source is packaged | deploy; for
    a stored ``placed`` clock the packaged ``Anchor`` or, on a first write, the
    deploy anchor (:func:`_first_anchor`); None keeps the stored default."""
    from erp_simulation import story_replan

    if data.get("AnchorSource") in story_replan.DEFAULT_SOURCES:
        return int(data["AnchorEpoch"]), data["AnchorSource"]
    if first or _anchor_prop(event, "ResourceProperties"):
        return _first_anchor(event, _now())
    return None


def _record_default(event, data: dict, first: bool) -> dict:
    """Record :func:`_default_for` (best effort: the story is already written,
    and a missing default only disables the reset, which the UI shows); returns ``data``."""
    try:
        from erp_simulation import story_replan

        default = _default_for(event, data, first)
        if default is not None:
            changed = _on_connection("DB_SECRET_NAME", lambda conn: story_replan.write_default_anchor(
                conn, *default))
            logger.info("story default anchor recorded: %s (%d rows changed)", default, changed)
    except Exception as err:  # best effort: never fail the stack over the failsafe
        logger.warning("story default anchor record FAILED (%s): %s", type(err).__name__, str(err)[:300])
    return data


def _backfill_default_for(event, erp_conn, now: int):
    """The ``(anchor, source)`` an Update records when NO default is stored (an
    account upgraded in place from before ADR-0091 Design C): the packaged
    ``Anchor`` when set; else the stored anchor when its source is packaged |
    deploy (source kept); else - a ``placed`` clock - the deploy anchor
    (:func:`_first_anchor`: ``now`` snapped down, already normalised)."""
    from erp_simulation import story_history, story_replan

    if not _anchor_prop(event, "ResourceProperties"):
        stored, source = story_history.read_stored_anchor(erp_conn), _stored_source(erp_conn, "")
        if stored is not None and source in story_replan.DEFAULT_SOURCES:
            return int(stored), source
    return _first_anchor(event, now)


def _backfill_on(event, erp_conn, now: int):
    """Write :func:`_backfill_default_for` when no default is stored; returns
    ``(default, rows changed)`` or None (a default is already stored)."""
    from erp_simulation import story_replan

    keys = (story_replan.DEFAULT_ANCHOR_KEY, story_replan.DEFAULT_SOURCE_KEY)
    if story_replan.parse_default({key: _stored_value(erp_conn, key) for key in keys}) is not None:
        return None
    default = _backfill_default_for(event, erp_conn, now)
    return default, story_replan.write_default_anchor(erp_conn, *default)


def _backfill_default(event, data: dict) -> dict:
    """Every Update: fill the reset default IF ABSENT (best effort, like
    :func:`_record_default`: never fails the stack); returns ``data``."""
    try:
        now = _now()
        done = _on_connection("DB_SECRET_NAME", lambda conn: _backfill_on(event, conn, now))
        if done is not None:
            logger.info("story default anchor recorded (backfill): %s (%d rows changed)", *done)
    except Exception as err:  # best effort: never fail the stack over the failsafe
        logger.warning("story default anchor backfill FAILED (%s): %s", type(err).__name__, str(err)[:300])
    return data


def _update(event) -> dict:
    """An Update's ``Data``: a changed ``Anchor`` wins (packaged re-plan), then
    a changed ``LoopSeconds`` (stored re-plan), else the idempotent writer.
    The re-plans record the default they planned onto."""
    if _anchor_changed(event):
        return _record_default(event, _replan_packaged(event), first=False)
    if _loop_changed(event):
        return _record_default(event, _replan_stored(event), first=False)
    return _write_history(event)


def _create_or_update(event, request: str) -> dict:
    """The CFN ``Data``. Create records the reset default; EVERY Update
    (the unchanged one included) then back-fills it when none is stored."""
    if request == "Create":
        return _record_default(event, _write_history(event), first=True)
    return _backfill_default(event, _update(event))


# ---------------------------------------------------------------------------
# Direct invoke (RequestResponse, not CloudFormation): {"action": "place" | "reset"}
# ---------------------------------------------------------------------------

PLACE_KEYS = frozenset({"action", "alarm_at", "phase", "elapsed_minutes", "expected_anchor"})
ALARM_WINDOW_SECONDS = (86400, 7 * 86400)  # alarm_at within [now - 1 d, now + 7 d]


def _number(event, key: str) -> float:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number, got {value!r}")
    return value


def _alarm_reference(event, now: int) -> int:
    from erp_simulation import story_replan

    alarm = _number(event, "alarm_at")
    before, ahead = ALARM_WINDOW_SECONDS
    if not now - before <= alarm <= now + ahead:
        raise ValueError(f"alarm_at must be within [now - {before} s, now + {ahead} s], got {alarm!r}")
    return story_replan.alarm_to_anchor(alarm)


def _phase_reference(event, now: int) -> int:
    from erp_simulation import story_replan

    phase = event.get("phase")
    if not isinstance(phase, str):
        raise ValueError(f"phase must be a string, got {phase!r}")
    return story_replan.phase_offset_to_anchor(phase, _number(event, "elapsed_minutes"), now)


def _place_args(event, now: int):
    """``(reference_anchor, expected_anchor)`` of a valid place event; else ValueError."""
    unknown = sorted(set(event) - PLACE_KEYS)
    if unknown:
        raise ValueError(f"unknown place keys {unknown}")
    by_alarm = "alarm_at" in event
    if by_alarm == ("phase" in event or "elapsed_minutes" in event):
        raise ValueError("give exactly one of alarm_at or phase + elapsed_minutes")
    if "expected_anchor" not in event:
        raise ValueError("expected_anchor is required (null skips the compare-and-swap)")
    expected = None if event["expected_anchor"] is None else _number(event, "expected_anchor")
    reference = _alarm_reference(event, now) if by_alarm else _phase_reference(event, now)
    return reference, expected


def _place(event, story_replan) -> dict:
    """Validate (ValueError -> ``invalid``: the ONLY input-validation path),
    then ``story_replan.replan(source="placed")``; returns its report."""
    now = _now()
    try:
        reference, expected = _place_args(event, now)
    except ValueError as err:
        return {"error": "invalid", "message": str(err)}
    report = _with_connections(lambda erp_conn, seed_conn: story_replan.replan(
        erp_conn, seed_conn, reference, now=now, source="placed", expected_anchor=expected))
    logger.info("story clock placed: %s", report)
    return report


RESET_KEYS = frozenset({"action", "expected_anchor"})


def _reset_expected(event):
    """The ``expected_anchor`` of a valid reset event (None skips the CAS); else ValueError."""
    unknown = sorted(set(event) - RESET_KEYS)
    if unknown:
        raise ValueError(f"unknown reset keys {unknown}")
    if "expected_anchor" not in event:
        raise ValueError("expected_anchor is required (null skips the compare-and-swap)")
    return None if event["expected_anchor"] is None else _number(event, "expected_anchor")


def _reset(event, story_replan) -> dict:
    """Validate (ValueError -> ``invalid``), then ``story_replan.reset``: the
    re-plan onto the stored default anchor with its stored source (ADR-0091
    Design C); returns its report (``reset: true``)."""
    now = _now()
    try:
        expected = _reset_expected(event)
    except ValueError as err:
        return {"error": "invalid", "message": str(err)}
    report = _with_connections(lambda erp_conn, seed_conn: story_replan.reset(
        erp_conn, seed_conn, now=now, expected_anchor=expected))
    logger.info("story clock reset to its default: %s", report)
    return report


_ACTIONS = {"place": _place, "reset": _reset}


# ``{"action": "place_live", "alarm_at": <epoch>, "expected_live_anchor":
# <epoch|null>}`` (ADR-0095): move ONLY the live clock through
# ``story_replan.replan_live`` (the history, its failed batch and every run
# number stay). The CAS is on the LIVE anchor the lookup reports as
# ``live_anchor_epoch``. A refused move (no clock, drifted geometry, an alarm
# inside the history) answers ``invalid``.
PLACE_LIVE_KEYS = frozenset({"action", "alarm_at", "expected_live_anchor"})


def _place_live_args(event, now: int):
    """``(reference_anchor, expected_live_anchor)`` of a valid place_live event; else ValueError."""
    unknown = sorted(set(event) - PLACE_LIVE_KEYS)
    if unknown:
        raise ValueError(f"unknown place_live keys {unknown}")
    if "alarm_at" not in event:
        raise ValueError("alarm_at is required for place_live")
    if "expected_live_anchor" not in event:
        raise ValueError("expected_live_anchor is required (null skips the compare-and-swap)")
    expected = None if event["expected_live_anchor"] is None else _number(event, "expected_live_anchor")
    return _alarm_reference(event, now), expected


def _place_live(event, story_replan) -> dict:
    """Validate (ValueError -> ``invalid``), then ``story_replan.replan_live``;
    a :class:`story_replan.LiveClockRefused` is ``invalid`` too (the caller
    chose a move the model cannot make, not a server fault)."""
    now = _now()
    try:
        reference, expected = _place_live_args(event, now)
    except ValueError as err:
        return {"error": "invalid", "message": str(err)}
    try:
        report = _with_connections(lambda erp_conn, seed_conn: story_replan.replan_live(
            erp_conn, seed_conn, reference, now=now, expected_live_anchor=expected))
    except story_replan.LiveClockRefused as err:
        return {"error": "invalid", "message": str(err)}
    logger.info("live story clock moved, history kept: %s", report)
    return report


_ACTIONS["place_live"] = _place_live


def _mapped_error(err, story_replan):
    """The answer of a known clock error (None: any other failure)."""
    if story_replan is None:
        return None
    if isinstance(err, story_replan.StaleAnchor):
        return {"error": "stale_anchor", "message": str(err)}
    if isinstance(err, story_replan.NoDefaultAnchor):
        return {"error": "no_default_anchor", "message": str(err)}
    return None


def _handle_action(event) -> dict:
    """A direct invoke: never raises (the import too is inside the guard).
    StaleAnchor -> ``stale_anchor``; NoDefaultAnchor -> ``no_default_anchor``;
    invalid input -> ``invalid``; anything else - including a ValueError
    raised server-side inside the re-plan - -> ``internal`` whose message is
    the exception CLASS only (the detail, which may name a host or a
    statement, stays in this function's log)."""
    story_replan = None
    try:
        from erp_simulation import story_replan

        action = event.get("action")
        handler = _ACTIONS.get(action) if isinstance(action, str) else None
        if handler is None:
            return {"error": "invalid", "message": f"unsupported action {action!r}"}
        return handler(event, story_replan)
    except Exception as err:  # never raise out of a clock action
        mapped = _mapped_error(err, story_replan)
        if mapped is not None:
            return mapped
        logger.warning("story clock action FAILED (%s): %s", type(err).__name__, str(err)[:300])
        return {"error": "internal", "message": type(err).__name__}


def _on_connection(secret_env: str, fn):
    conn = _connect(secret_env)
    try:
        return fn(conn)
    finally:
        conn.close()


def _best_effort(label: str, secret_env: str, fn_name: str) -> None:
    """Run ``story_history.<fn_name>`` on a connection; log, never raise."""
    try:
        from erp_simulation import story_history

        result = _on_connection(secret_env, getattr(story_history, fn_name))
        logger.info("%s: %s", label, result)
    except Exception as err:  # best effort: Delete must never fail the stack
        logger.warning("%s FAILED (%s): %s", label, type(err).__name__, str(err)[:300])


def _delete_story() -> None:
    """Delete: clear the story_* keys, then restore the frozen seed; each step
    independent and best effort (never raises)."""
    _best_effort("story config clear", "DB_SECRET_NAME", "clear_story_config")
    _best_effort("frozen seed restore", "SEED_DB_SECRET_NAME", "restore_frozen_seed")


def lambda_handler(event, context):
    if "RequestType" not in event and "action" in event:
        logger.info("story history action %r", event.get("action"))
        return _handle_action(event)
    request = event.get("RequestType")
    logger.info("Custom::ADTStoryHistory %s", request)
    if request == "Delete":
        _delete_story()
        return {"PhysicalResourceId": event.get("PhysicalResourceId", PHYSICAL_ID)}
    if request not in ("Create", "Update"):
        raise ValueError(f"unsupported RequestType {request!r}")
    return {"PhysicalResourceId": PHYSICAL_ID, "Data": _create_or_update(event, request)}
