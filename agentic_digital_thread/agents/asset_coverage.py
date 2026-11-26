# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-site asset coverage of one system, counted from its asset hierarchy.

Pure: no graph, no network. The role pass (``asset_role_pass.classify_system``)
calls :func:`coverage_json` with the rows it already holds and stores the
result on the system's ``:System`` node as ``asset_coverage``; the route seed
reads it back and :func:`render_coverage` turns it into one compact line.

Anchors are the assets whose stored ``level`` is ``site``; a system with none
is anchored on its roots instead. For each anchor every descendant is counted
by its stored level (blank -> ``other``). Assets whose parent could not be
told (the pass's "unplaced" assets) are not counted, only disclosed, so the
summary never reads as complete when it is not.

The stored form is ONE compact JSON string (Neptune Analytics property values
are primitives), bounded by :data:`JSON_CAP`: when the cap bites, anchors are
dropped from the end and counted in ``more_sites``; the JSON is never cut.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Iterable, Mapping

from .asset_roles import LEVEL_BY_ROLE, AssetRow

#: Most anchors kept; the rest are counted in ``more_sites``. Same number as
#: the route seed's plant cap (``orchestrator._ROUTE_SEED_PLANT_COUNT_CAP``).
ANCHOR_CAP = 8
#: Longest stored anchor label (same as the route seed's plant field cap).
NAME_CAP = 96
#: Longest stored JSON string.
JSON_CAP = 1_500

SITE_LEVEL = "site"
OTHER_LEVEL = "other"
_ELLIPSIS = "…"

#: Render order: the hierarchy order of the stored levels, then any other
#: stored level alphabetically, then ``other``.
_LEVEL_ORDER = tuple(dict.fromkeys(LEVEL_BY_ROLE.values()))
#: Levels read as mass nouns (no plural form).
_UNCOUNTED = frozenset({"equipment", OTHER_LEVEL})


def _label(name: str) -> str:
    name = str(name or "").strip()
    return name if len(name) <= NAME_CAP else name[:NAME_CAP - 1].rstrip() + _ELLIPSIS


def _anchor_labels(anchors: list[AssetRow], all_names: list[str],
                   paths: Mapping[str, str]) -> list[str]:
    """The anchor's name; its path when another anchor shares the name; and,
    for any label that still collides (same-parent duplicates, or names equal
    after :data:`NAME_CAP`), `` [<shortest id tail, at least 8 chars, that
    tells the colliding anchors apart>]``. When no tail that fits the cap does
    (ids sharing a long tail), the suffix is ``[#N]``, N the anchor's position
    among those sharing the label in the deterministic anchor order. Every
    label stays within the cap."""
    labels = [_label(paths[a.id] if all_names.count(str(a.name or "")) > 1 else a.name)
              for a in anchors]
    groups: dict[str, list[int]] = defaultdict(list)
    for i, label in enumerate(labels):
        groups[label].append(i)
    out = list(labels)
    for label, members in groups.items():
        if len(members) < 2:
            continue
        ids = [str(anchors[i].id) for i in members]
        longest = NAME_CAP // 2
        width = next((n for n in range(8, longest + 1)
                      if len({x[-n:] for x in ids}) == len(ids)), None)
        for ordinal, (i, aid) in enumerate(zip(members, ids), start=1):
            suffix = f" [{aid[-width:]}]" if width else f" [#{ordinal}]"
            out[i] = label[:NAME_CAP - len(suffix)].rstrip() + suffix
    # A suffixed label can still equal another anchor's own name; the anchor's
    # position in the deterministic order is unique, so it settles any rest.
    seen: set[str] = set()
    for i, label in enumerate(out):
        if label in seen:
            suffix = f" [#{i + 1}]"
            out[i] = label[:NAME_CAP - len(suffix)].rstrip() + suffix
        seen.add(out[i])
    return out


def _paths(rows: list[AssetRow]) -> dict[str, str]:
    """Slash path of names from each asset's root, cycle-safe."""
    by_id = {a.id: a for a in rows}
    out: dict[str, str] = {}
    for a in rows:
        names, cur, seen = [], a, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            names.append(str(cur.name or cur.id))
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        out[a.id] = "/".join(reversed(names))
    return out


def _descendant_levels(anchor: str, children: Mapping[str, list[AssetRow]],
                       levels: Mapping[str, object]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    stack, seen = [anchor], {anchor}
    while stack:
        for child in children.get(stack.pop(), ()):
            if child.id in seen:
                continue
            seen.add(child.id)
            counts[str(levels.get(child.id) or "").strip() or OTHER_LEVEL] += 1
            stack.append(child.id)
    return {lvl: counts[lvl] for lvl in sorted(counts, key=_level_key)}


def _level_key(level: str) -> tuple:
    if level in _LEVEL_ORDER:
        return (0, _LEVEL_ORDER.index(level), "")
    return (2, 0, "") if level == OTHER_LEVEL else (1, 0, level)


def compute_coverage(placed: Iterable[AssetRow], levels: Mapping[str, object],
                     unplaced: int = 0) -> "dict | None":
    """The coverage summary, or ``None`` when the system holds no assets."""
    rows = list(placed)
    if not rows and not unplaced:
        return None
    known = {a.id for a in rows}
    children: dict[str, list[AssetRow]] = defaultdict(list)
    for a in rows:
        if a.parent_id in known:
            children[a.parent_id].append(a)
    anchors = [a for a in rows if str(levels.get(a.id) or "") == SITE_LEVEL]
    kind = SITE_LEVEL
    if not anchors:
        kind = "root"
        anchors = [a for a in rows if a.parent_id not in known]
    paths = _paths(rows)
    anchors.sort(key=lambda a: (str(a.name or ""), paths[a.id], a.id))
    kept = anchors[:ANCHOR_CAP]
    labels = _anchor_labels(kept, [str(a.name or "") for a in anchors], paths)
    sites = []
    for a, label in zip(kept, labels):
        counts = _descendant_levels(a.id, children, levels)
        sites.append({"name": label, "total": sum(counts.values()), "levels": counts})
    out: dict = {"anchor": kind, "sites": sites}
    if len(anchors) > ANCHOR_CAP:
        out["more_sites"] = len(anchors) - ANCHOR_CAP
    if unplaced:
        out["unplaced"] = int(unplaced)
    return out


def _dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def serialize_coverage(coverage: "dict | None") -> "str | None":
    """One compact JSON string no longer than :data:`JSON_CAP`."""
    if coverage is None:
        return None
    out = {**coverage, "sites": list(coverage.get("sites") or [])}
    raw = _dumps(out)
    while len(raw) > JSON_CAP and out["sites"]:
        out["sites"].pop()
        out["more_sites"] = int(out.get("more_sites") or 0) + 1
        raw = _dumps(out)
    return raw


def coverage_json(placed: Iterable[AssetRow], levels: Mapping[str, object],
                  unplaced: int = 0) -> "str | None":
    return serialize_coverage(compute_coverage(placed, levels, unplaced))


def _count(value) -> "int | None":
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_coverage(raw: object) -> "dict | None":
    """The stored summary, validated; ``None`` for anything malformed."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("sites"), list):
        return None
    for key in ("more_sites", "unplaced"):
        if key in value and _count(value[key]) is None:
            return None
    for site in value["sites"]:
        if not (isinstance(site, dict) and isinstance(site.get("name"), str)
                and _count(site.get("total")) is not None
                and isinstance(site.get("levels"), dict)
                and all(isinstance(k, str) and _count(v) is not None
                        for k, v in site["levels"].items())):
            return None
    if not value["sites"] and not value.get("more_sites") and not value.get("unplaced"):
        return None
    return value


def _plural(word: str) -> str:
    if word in _UNCOUNTED:
        return word
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def _noun(count: int, level: str) -> str:
    word = level.replace("_", " ")
    if count != 1:
        word = " ".join([*word.split(" ")[:-1], _plural(word.split(" ")[-1])])
    return f"{count} {word}"


def render_coverage(raw: object) -> "str | None":
    """``"Site A: 1 area, 3 lines, 12 equipment; ...; +2 more sites"``, or
    ``None`` when the stored value is missing or malformed."""
    value = parse_coverage(raw)
    if value is None:
        return None
    roots = value.get("anchor") == "root"
    parts = []
    for site in value["sites"]:
        levels = sorted(site["levels"].items(), key=lambda kv: _level_key(kv[0]))
        counts = ", ".join(_noun(n, lvl) for lvl, n in levels if n) or "none"
        parts.append(f"{site['name']}: {counts}")
    more = value.get("more_sites") or 0
    if more:
        parts.append(f"+{more} more " + ("top-level asset" if roots else "site")
                     + ("s" if more != 1 else ""))
    unplaced = value.get("unplaced") or 0
    if unplaced:
        parts.append(f"{_noun(unplaced, 'asset')} not placed in the hierarchy (not counted)")
    text = "; ".join(parts)
    return f"top-level assets (no site level): {text}" if roots and value["sites"] else text
