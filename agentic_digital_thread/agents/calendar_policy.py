# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The deployment's calendar policy: what a date in a question means.

A user who writes "on 8/20" or "yesterday" means a calendar span, and which
span depends on the deployment: the UTC calendar day, or the site's working
day (which can start at, say, 06:30 local and so cross UTC midnight). That
rule used to be a steward note the model followed about half the time. It is
now one structured graph record, read before the model runs
(:mod:`agents.time_windows` turns it into computed windows):

* ``bare_date``: what a date with no time means, ``utc_day`` or ``site_day``;
* ``relative_days``: what today, yesterday, this week, last week and the past
  N days or weeks mean, ``utc_day`` or ``site_day``;
* ``site_day_start``: the local ``HH:MM`` a working day starts (default
  ``00:00``);
* ``sites``: each site's IANA time zone, with an optional per-site day start.
  The graph holds no time zone for a plant, site or asset today, so the
  policy carries the map itself. A ``site_day`` rule needs at least one site.

No policy keeps today's behaviour exactly: nothing is resolved and every turn
is byte-identical. A stored record that no longer validates reads as no
policy (logged, never guessed).

Storage: one ``(:CalendarPolicy {id: 'calendar-policy'})`` node, written
validate, MERGE, exact readback, with ``updated_by`` / ``updated_at`` audit
stamps, only through :func:`write_policy` behind the steward-gated route
(``calendar_policy_routes``). The site list is a JSON string property (the
graph stores no list of maps). Reads go through :func:`load_policy`, a
per-process snapshot refreshed at most once per :data:`SNAPSHOT_TTL_SECONDS`,
so a change reaches the orchestrator runtime within that window.
"""
from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timezone

from .time_context import _resolve_zone

logger = logging.getLogger(__name__)

UTC_DAY = "utc_day"
SITE_DAY = "site_day"
RULES = (UTC_DAY, SITE_DAY)

#: Plain labels for the two rules, shared by the route and the admin page.
RULE_LABELS = {
    UTC_DAY: "the UTC calendar day",
    SITE_DAY: "the site's working day",
}

POLICY_NODE_ID = "calendar-policy"
MAX_SITES = 32
_DEFAULT_DAY_START = "00:00"
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: A site name as a question names it: letters, digits, space, ``_.-``.
_SITE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _.\-]{0,62}[A-Za-z0-9])?$")

SNAPSHOT_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class SiteZone:
    """One site's time zone. ``day_start`` ``""`` means the policy default."""

    site: str
    timezone: str
    day_start: str = ""


@dataclass(frozen=True)
class CalendarPolicy:
    """The validated policy (construct through :func:`parse_policy`)."""

    bare_date: str = UTC_DAY
    relative_days: str = UTC_DAY
    site_day_start: str = _DEFAULT_DAY_START
    sites: tuple = field(default_factory=tuple)
    updated_by: str = ""
    updated_at: str = ""

    def day_start(self, site: "SiteZone | None" = None) -> time:
        """The local time a working day starts at ``site`` (or the default)."""
        return _parse_hhmm((site.day_start if site and site.day_start else "")
                           or self.site_day_start)

    def with_audit(self, actor: str, at: str) -> "CalendarPolicy":
        return replace(self, updated_by=actor, updated_at=at)

    def to_dict(self) -> dict:
        """The public shape (route responses, the admin page)."""
        sites = []
        for site in self.sites:
            entry = {"site": site.site, "timezone": site.timezone}
            if site.day_start:
                entry["day_start"] = site.day_start
            sites.append(entry)
        return {"bare_date": self.bare_date, "relative_days": self.relative_days,
                "site_day_start": self.site_day_start, "sites": sites,
                "updated_by": self.updated_by, "updated_at": self.updated_at}


class PolicyError(ValueError):
    """A policy body that does not validate; ``errors`` names every problem."""

    def __init__(self, errors: list):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def _parse_hhmm(text: str) -> time:
    match = _HHMM_RE.match(text or "")
    if not match:
        return time(0, 0)
    return time(int(match.group(1)), int(match.group(2)))


def _rule_error(name: str, value) -> list:
    if value in RULES:
        return []
    return [f"{name} must be one of {', '.join(RULES)}"]


def _site_errors(index: int, raw) -> "tuple[SiteZone | None, list]":
    where = f"sites[{index}]"
    if not isinstance(raw, dict):
        return None, [f"{where} must be an object with site and timezone"]
    name = raw.get("site")
    zone = raw.get("timezone")
    start = raw.get("day_start") or ""
    errors = []
    if not isinstance(name, str) or not _SITE_RE.match(name):
        errors.append(f"{where}: a site name is 1 to 64 letters, digits, spaces or _.- "
                      "and starts and ends with a letter or digit")
    if not isinstance(zone, str) or _resolve_zone(zone) is None:
        errors.append(f"{where}: timezone must be an IANA time zone name, e.g. Europe/London")
    if not isinstance(start, str) or (start and not _HHMM_RE.match(start)):
        errors.append(f"{where}: a day start is a 24-hour HH:MM time, e.g. 06:30")
    if errors:
        return None, errors
    return SiteZone(site=name, timezone=_resolve_zone(zone).key, day_start=start), []


def parse_policy(body: dict) -> CalendarPolicy:
    """Validate a policy body; raise :class:`PolicyError` naming every problem."""
    if not isinstance(body, dict):
        raise PolicyError(["the policy must be an object"])
    bare = body.get("bare_date")
    relative = body.get("relative_days")
    start = body.get("site_day_start")
    start = _DEFAULT_DAY_START if start in (None, "") else start
    raw_sites = body.get("sites") or []
    errors = _rule_error("bare_date", bare) + _rule_error("relative_days", relative)
    if not isinstance(start, str) or not _HHMM_RE.match(start):
        errors.append("site_day_start must be a 24-hour HH:MM time, e.g. 06:30")
    sites: list = []
    if not isinstance(raw_sites, list):
        errors.append("sites must be a list")
        raw_sites = []
    if len(raw_sites) > MAX_SITES:
        errors.append(f"sites holds at most {MAX_SITES} entries")
        raw_sites = []
    seen: set = set()
    for index, raw in enumerate(raw_sites):
        site, site_errors = _site_errors(index, raw)
        errors.extend(site_errors)
        if site is None:
            continue
        if site.site.casefold() in seen:
            errors.append(f"sites[{index}]: site {site.site!r} is listed more than once")
            continue
        seen.add(site.site.casefold())
        sites.append(site)
    if SITE_DAY in (bare, relative) and not raw_sites:
        errors.append("a site working day needs at least one site time zone in sites")
    if errors:
        raise PolicyError(errors)
    return CalendarPolicy(bare_date=bare, relative_days=relative,
                          site_day_start=start, sites=tuple(sites))


# --------------------------------------------------------------------------- #
# Graph storage
# --------------------------------------------------------------------------- #
POLICY_READ_CYPHER = (
    "MATCH (p:CalendarPolicy {id: $id}) "
    "RETURN p.bare_date AS bare_date, p.relative_days AS relative_days, "
    "p.site_day_start AS site_day_start, p.sites_json AS sites_json, "
    "p.updated_by AS updated_by, p.updated_at AS updated_at LIMIT 1"
)
POLICY_WRITE_CYPHER = (
    "MERGE (p:CalendarPolicy {id: $id}) "
    "SET p.bare_date = $bare_date, p.relative_days = $relative_days, "
    "p.site_day_start = $site_day_start, p.sites_json = $sites_json, "
    "p.updated_by = $actor, p.updated_at = $at"
)
POLICY_CLEAR_CYPHER = "MATCH (p:CalendarPolicy {id: $id}) DETACH DELETE p"


def _graph_read(cypher: str, params=None) -> list:
    """Graph read seam: the checked read (raises, so a failure is not 'no policy')."""
    from .db import neptune_query_checked
    return neptune_query_checked(cypher, params)


def _graph_write(cypher: str, params=None) -> bool:
    """Graph write seam (the shared write helper)."""
    from .db import neptune_write
    return neptune_write(cypher, params)


def _sites_json(policy: CalendarPolicy) -> str:
    return json.dumps(policy.to_dict()["sites"], sort_keys=True, separators=(",", ":"))


def policy_from_row(row) -> "CalendarPolicy | None":
    """The policy a stored row holds, or ``None`` when it does not validate."""
    if not isinstance(row, dict):
        return None
    try:
        sites = json.loads(row.get("sites_json") or "[]")
        policy = parse_policy({"bare_date": row.get("bare_date"),
                               "relative_days": row.get("relative_days"),
                               "site_day_start": row.get("site_day_start"),
                               "sites": sites})
    except (PolicyError, ValueError, TypeError) as exc:
        logger.warning("calendar policy: stored record does not validate (%s); "
                       "treated as no policy", type(exc).__name__)
        return None
    return policy.with_audit(str(row.get("updated_by") or ""),
                             str(row.get("updated_at") or ""))


def read_policy(query=None) -> "CalendarPolicy | None":
    """The stored policy, ``None`` when there is none (or it does not
    validate). Raises when the read itself fails."""
    rows = (query or _graph_read)(POLICY_READ_CYPHER, {"id": POLICY_NODE_ID}) or []
    return policy_from_row(rows[0]) if rows else None


def write_policy(policy: CalendarPolicy, actor: str, *, write=None, query=None) -> dict:
    """MERGE the policy with audit stamps, then read it back. ``status`` is
    ``ok`` only when the readback holds exactly what was written."""
    at = datetime.now(timezone.utc).isoformat()
    actor = actor or "unknown"
    params = {"id": POLICY_NODE_ID, "bare_date": policy.bare_date,
              "relative_days": policy.relative_days,
              "site_day_start": policy.site_day_start,
              "sites_json": _sites_json(policy), "actor": actor, "at": at}
    wrote = (write or _graph_write)(POLICY_WRITE_CYPHER, params)
    stored = read_policy(query=query) if wrote else None
    expected = policy.with_audit(actor, at)
    ok = stored == expected
    logger.info("CALENDAR_POLICY_SET actor=%s bare_date=%s relative_days=%s "
                "site_day_start=%s sites=%d readback=%s", actor, policy.bare_date,
                policy.relative_days, policy.site_day_start, len(policy.sites), ok)
    reset_snapshot()
    return {"status": "ok" if ok else "failed", "readback": ok,
            "policy": expected.to_dict()}


def clear_policy(actor: str, *, write=None, query=None) -> dict:
    """Remove the policy (back to today's behaviour), then confirm it is gone."""
    (write or _graph_write)(POLICY_CLEAR_CYPHER, {"id": POLICY_NODE_ID})
    gone = read_policy(query=query) is None
    logger.info("CALENDAR_POLICY_CLEARED actor=%s readback=%s", actor or "unknown", gone)
    reset_snapshot()
    return {"status": "cleared" if gone else "failed", "readback": gone}


# --------------------------------------------------------------------------- #
# The per-process snapshot
# --------------------------------------------------------------------------- #
_snapshot: "tuple | None" = None   # (expires at, policy or None)


def _monotonic() -> float:
    return _time.monotonic()


def reset_snapshot() -> None:
    """Drop the snapshot (after a write in this process, and between tests)."""
    global _snapshot
    _snapshot = None


def load_policy(query=None) -> "CalendarPolicy | None":
    """The policy, read at most once per :data:`SNAPSHOT_TTL_SECONDS` per
    process. A failed read keeps the previous snapshot (or gives none) and is
    retried on the next call. Never raises."""
    global _snapshot
    now = _monotonic()
    current = _snapshot
    if current is not None and current[0] > now:
        return current[1]
    try:
        policy = read_policy(query=query)
    except Exception as exc:  # noqa: BLE001 - the policy is additive; never break a turn
        logger.info("calendar policy: read failed category=%s", type(exc).__name__)
        return current[1] if current is not None else None
    _snapshot = (now + SNAPSHOT_TTL_SECONDS, policy)
    return policy
