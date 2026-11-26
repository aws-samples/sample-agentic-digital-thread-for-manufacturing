# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Asset-model ROLE classification (pure — no graph, no network).

The walkers label every asset with a ``level``. Only four exact model names
(Enterprise / Site / Area / Line) say what an asset IS; every other asset is
labelled ``equipment`` or ``component`` by how deep it sits. That label then
reads as a kind of asset, and it is wrong the moment a source adds a tier: a
station that holds machines pushes those machines to ``component`` while the
station itself becomes ``equipment``.

This module classifies each asset MODEL of one system (never a single
instance — every instance of a model gets the same role at any depth) into a
role from the ISA-95 equipment hierarchy:

    enterprise, site, area, work_center, work_unit, equipment,
    equipment_part, data_stream, unclassified

Evidence, strongest first; a model is decided by the first rule that applies
and is never guessed:

1. steward override (a confirmed role for this system + model);
2. the model's exact structural name (enterprise / site / area / line) — the
   rule the walkers already apply, kept as is;
3. cross-system value evidence: accepted value mappings whose equipment-id
   value lands on this model's instances. Only the TOPMOST evidenced model on
   a branch becomes equipment; an evidenced model below it is a part or data
   stream of that equipment;
4. structure of the model tree:
   * a model whose child models are all leaves, at most one instance of each
     per parent, is equipment when one of them is a data stream (events,
     alarms, logs); with several such kinds and no stream it is most likely
     equipment and flagged for review; with a single non-stream kind (a
     station holding one machine) the tree can't tell which one is the
     equipment, so both stay unclassified;
   * a leaf model that repeats under one parent (several presses in a cell)
     is equipment, and so is a leaf held only by containers or a model that
     stands alone with no parent and no children. When, in every parent
     asset holding it, an equipment identifier from another system lands on
     a sibling leaf and not on it, it is still equipment but flagged for
     review with source ``identifier_gap`` (it may be the container's own
     data, or the other system may not track that kind of machine); evidence
     is judged per parent asset, so a match under one line never touches a
     model on another line. When only a sibling repeats and it does not, it
     is equipment flagged for review;
   * a model with equipment below it is a container. Under a known tier (a
     named one, or a container already placed) its role follows from what it
     holds (sites, areas, work centers) or else from where it sits (inside a
     work center or work unit it is a work unit, otherwise a work center; a
     model in both positions stays unclassified for review). With no known
     tier above it, it is placed from the equipment up — the nearest
     container is the work center — and flagged for a steward;
   * anything below equipment is part of that equipment;
5. a small name lexicon (events, alarms, logs, streams...) to tell a data
   stream from a physical part below equipment, and to recognise a holder of
   data streams as equipment.

A model whose instances nest inside each other (a source that gives every
asset the same generic type) cannot be classified from structure and stays
``unclassified``; so does anything no rule reaches. Unclassified assets keep
the label the walker gave them.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

ENTERPRISE, SITE, AREA = "enterprise", "site", "area"
WORK_CENTER, WORK_UNIT = "work_center", "work_unit"
EQUIPMENT, EQUIPMENT_PART, DATA_STREAM = "equipment", "equipment_part", "data_stream"
UNCLASSIFIED = "unclassified"

#: The ``source`` of an equipment role flagged because, in every parent asset
#: holding the model, a sibling is matched by another system's equipment
#: identifier and it is not: it may be its container's own data.
IDENTIFIER_GAP = "identifier_gap"
ROLES = (ENTERPRISE, SITE, AREA, WORK_CENTER, WORK_UNIT,
         EQUIPMENT, EQUIPMENT_PART, DATA_STREAM, UNCLASSIFIED)

#: Roles that hold other assets.
CONTAINER_ROLES = frozenset({ENTERPRISE, SITE, AREA, WORK_CENTER, WORK_UNIT})
#: Roles at or below a piece of equipment.
EQUIPMENT_ROLES = frozenset({EQUIPMENT, EQUIPMENT_PART, DATA_STREAM})

#: The ``level`` each role is stored as. ``line`` and ``component`` are the
#: values the walkers already write; ``work_unit`` is new and only appears in a
#: source that has a tier between its work centers and its equipment.
LEVEL_BY_ROLE = {
    ENTERPRISE: "enterprise", SITE: "site", AREA: "area",
    WORK_CENTER: "line", WORK_UNIT: "work_unit",
    EQUIPMENT: "equipment", EQUIPMENT_PART: "component", DATA_STREAM: "component",
}

#: Exact structural model names (normalised) — the walkers' existing rule.
_ROLE_BY_STRUCTURAL_NAME = {"enterprise": ENTERPRISE, "site": SITE,
                            "area": AREA, "line": WORK_CENTER}

#: Concepts whose values identify a piece of equipment (``equipment`` is the
#: legacy alias of ``equipment-id``).
EQUIPMENT_ID_CONCEPTS = frozenset({"equipment-id", "equipment"})

#: Name tokens that mark a data stream rather than a physical part. Used ONLY
#: below equipment, where the role is already known to be part-or-stream.
_STREAM_TOKENS = frozenset({
    "event", "events", "alarm", "alarms", "log", "logs", "stream", "streams",
    "history", "message", "messages", "record", "records", "signal", "signals",
    "telemetry", "trace", "traces",
})


@dataclass(frozen=True)
class AssetRow:
    """One asset as the graph holds it."""
    id: str
    name: str
    model: str
    parent_id: str | None = None


@dataclass(frozen=True)
class ModelRole:
    """The classified role of one asset model, with why."""
    model: str
    role: str
    source: str          # steward | name | value_evidence | structure | identifier_gap | unresolved
    evidence: str        # one plain sentence a steward can check
    needs_review: bool = False

    @property
    def level(self) -> str | None:
        """The level this role is stored as, or None for unclassified."""
        return LEVEL_BY_ROLE.get(self.role)


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    return {t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t}


class _ModelTree:
    """Model-level view of one system's asset tree."""

    def __init__(self, assets: Iterable[AssetRow]):
        rows = list(assets)
        by_id = {a.id: a for a in rows}
        self.instances: dict[str, list[str]] = defaultdict(list)
        self.children: dict[str, Counter] = defaultdict(Counter)
        self.parents: dict[str, Counter] = defaultdict(Counter)
        # (parent model, child model) -> max instances of child under ONE parent
        per_parent: dict[tuple, Counter] = defaultdict(Counter)
        self.root_count: Counter = Counter()
        # Instance view: the parent assets holding each model, and what each
        # parent asset holds directly (child asset id, child model).
        self.holders: dict[str, set[str]] = defaultdict(set)
        self.held: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for a in rows:
            self.instances[a.model].append(a.id)
            parent = by_id.get(a.parent_id) if a.parent_id else None
            if parent is None:
                self.root_count[a.model] += 1
                continue
            self.children[parent.model][a.model] += 1
            self.parents[a.model][parent.model] += 1
            per_parent[(parent.model, a.model)][parent.id] += 1
            self.holders[a.model].add(parent.id)
            self.held[parent.id].append((a.id, a.model))
        self.max_per_parent = {k: max(v.values()) for k, v in per_parent.items()}
        self.models = sorted(self.instances)

    def is_leaf(self, m: str) -> bool:
        return not self.children.get(m)

    def is_recursive(self, m: str) -> bool:
        return m in self.children.get(m, ())

    def ancestors(self, m: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.parents.get(m, ()))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            stack.extend(self.parents.get(p, ()))
        return seen

    def descendants(self, m: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.children.get(m, ()))
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            stack.extend(self.children.get(c, ()))
        return seen

    def top_down(self) -> list[str]:
        """Models ordered roots-first (shortest path from a root model)."""
        dist: dict[str, int] = {}
        frontier = [m for m in self.models if self.root_count.get(m)]
        for m in frontier:
            dist[m] = 0
        while frontier:
            nxt = []
            for m in frontier:
                for c in self.children.get(m, ()):
                    if c not in dist:
                        dist[c] = dist[m] + 1
                        nxt.append(c)
            frontier = nxt
        return sorted(self.models, key=lambda m: (dist.get(m, 10_000), m))


class _Classifier:
    """One classification run; each rule is one short method, in order."""

    def __init__(self, assets: Iterable[AssetRow], equipment_hits: Mapping[str, int] | None,
                 overrides: Mapping[str, str] | None,
                 matched_assets: Iterable[str] | None = None):
        self.tree = _ModelTree(assets)
        self.hits = {m: n for m, n in (equipment_hits or {}).items()
                     if n and m in self.tree.instances}
        self.matched = {str(a) for a in (matched_assets or ())}
        self.overrides = dict(overrides or {})
        self.out: dict[str, ModelRole] = {}

    # ── helpers ──
    def decide(self, m: str, role: str, source: str, evidence: str, review: bool = False) -> None:
        self.out[m] = ModelRole(m, role, source, evidence, review)

    def role(self, m: str) -> str | None:
        r = self.out.get(m)
        return r.role if r else None

    def under_equipment(self, m: str) -> bool:
        return any(self.role(a) in EQUIPMENT_ROLES for a in self.tree.ancestors(m))

    def above_equipment(self, m: str) -> bool:
        return any(self.role(d) in EQUIPMENT_ROLES for d in self.tree.descendants(m))

    def held_by_containers(self, m: str) -> bool:
        parents = self.tree.parents.get(m, {})
        return bool(parents) and all(self.role(p) in CONTAINER_ROLES for p in parents)

    def outshone_by_siblings(self, m: str) -> str | None:
        """What sibling leaf assets have that ``m`` lacks: ``"identifier"``
        (``m`` may be its containers' own data, such as a line's OEE
        readings — see :meth:`outshone_by_identifiers`), ``"repeats"`` (a
        sibling model repeats under one parent, ``m`` never does — weaker,
        only worth a review), else ``None``."""
        if self.hits.get(m):
            return None
        if self.outshone_by_identifiers(m):
            return "identifier"
        tree, found = self.tree, None
        for p in tree.parents.get(m, ()):
            m_repeats = tree.max_per_parent.get((p, m), 0) >= 2
            for c in tree.children.get(p, ()):
                if c != m and tree.is_leaf(c) and not m_repeats \
                        and tree.max_per_parent.get((p, c), 0) >= 2:
                    found = "repeats"
        return found

    def outshone_by_identifiers(self, m: str) -> bool:
        """Local identifier evidence against ``m``: EVERY parent asset holding
        it also directly holds a sibling leaf asset that another system's
        equipment identifier lands on. Judged per parent asset, not per
        parent model, so a match under one line says nothing about the other
        lines. It only flags (see :meth:`decide_contained_leaf`)."""
        holders = self.tree.holders.get(m, set())
        return bool(holders) and all(self._holds_matched_sibling(p, m) for p in holders)

    def _holds_matched_sibling(self, parent_id: str, m: str) -> bool:
        return any(child_model != m and self.tree.is_leaf(child_model) and child in self.matched
                   for child, child_model in self.tree.held.get(parent_id, ()))

    def decide_contained_leaf(self, m: str) -> None:
        outshone = self.outshone_by_siblings(m)
        if outshone == "identifier":
            # Absence of an identifier is weak evidence: another system may
            # simply not track this kind of machine. Flag it, never demote it;
            # its own source lets a reader tell this flag from the others.
            self.decide(m, EQUIPMENT, IDENTIFIER_GAP,
                        "It is a leaf asset held directly by a container. Wherever it "
                        "appears, assets beside it are matched by equipment identifiers in "
                        "another system and it is not. Often that system just does not track "
                        "it, but it may be its container's own data, so a steward should "
                        "confirm it is equipment.", review=True)
        elif outshone == "repeats":
            self.decide(m, EQUIPMENT, "structure",
                        "It is a leaf asset held directly by a container; assets beside it "
                        "repeat and it does not, so a steward should confirm it.", review=True)
        else:
            self.decide(m, EQUIPMENT, "structure", "It is a leaf asset held directly by a container.")

    def run(self) -> dict[str, ModelRole]:
        for step in (self.stewards, self.structural_names, self.self_nesting,
                     self.value_evidence, self.structural_equipment, self.populations,
                     self.containers, self.contained_leaves, self.below_equipment,
                     self.unresolved):
            step()
        return self.out

    # ── 1-2. steward overrides, exact structural names ──
    def stewards(self) -> None:
        for m, role in self.overrides.items():
            if m in self.tree.instances and role in ROLES:
                self.decide(m, role, "steward", "A steward confirmed this role.")

    def structural_names(self) -> None:
        for m in self.tree.models:
            if m not in self.out and _norm(m) in _ROLE_BY_STRUCTURAL_NAME:
                self.decide(m, _ROLE_BY_STRUCTURAL_NAME[_norm(m)], "name",
                            f"The source names this model '{m}'.")

    def self_nesting(self) -> None:
        """A model that nests inside itself carries no structural signal."""
        for m in self.tree.models:
            if m not in self.out and (self.tree.is_recursive(m) or m in self.tree.descendants(m)):
                self.decide(m, UNCLASSIFIED, "unresolved",
                            "Instances of this model sit inside each other, so its "
                            "role can't be told from the tree.", review=True)

    # ── 3. cross-system value evidence: topmost evidenced model per branch ──
    def value_evidence(self) -> None:
        evidenced = {m for m in self.hits if m not in self.out}
        for m in sorted(evidenced):
            if self.tree.ancestors(m) & evidenced:
                continue
            stronger_below = any(self.hits.get(d, 0) > self.hits[m]
                                 for d in self.tree.descendants(m))
            self.decide(m, EQUIPMENT, "value_evidence",
                        f"{self.hits[m]} of its assets are matched by equipment "
                        "identifiers in another system.", review=stronger_below)

    # ── 4. structure ──
    def structural_equipment(self) -> None:
        """Holders of one-of-each leaf (parts/streams), lone assets, and leaves
        of named tiers are equipment; repeated until nothing changes."""
        changed = True
        while changed:
            changed = False
            for m in self.tree.models:
                if m in self.out or self.under_equipment(m) or self.above_equipment(m):
                    continue
                if self.tree.is_leaf(m) and self.held_by_containers(m):
                    self.decide_contained_leaf(m)
                    changed = True
                    continue
                why = self._equipment_reason(m)
                if why:
                    self.decide(m, EQUIPMENT, "structure", why[0], why[1])
                    changed = True
                elif self._lone_singleton_holder(m):
                    self.decide(m, UNCLASSIFIED, "unresolved",
                                "Each of its assets holds exactly one asset of a single other "
                                "kind; the tree can't tell whether it is the equipment or holds "
                                "it.", review=True)
                    changed = True

    def _aspect_kids(self, m: str) -> list[str] | None:
        """Its child models when they are all leaves at most once per parent."""
        tree = self.tree
        kids = list(tree.children.get(m, {}))
        if kids and all(tree.is_leaf(c) and tree.max_per_parent.get((m, c), 0) <= 1
                        for c in kids):
            return kids
        return None

    def _equipment_reason(self, m: str) -> tuple[str, bool] | None:
        """``(reason, needs_review)`` when structure makes ``m`` equipment."""
        tree = self.tree
        if tree.children.get(m):
            kids = self._aspect_kids(m)
            if kids is None:
                return None
            if any(_tokens(c) & _STREAM_TOKENS for c in kids):
                return ("Each of its assets holds at most one of each kind of leaf asset, "
                        "and they are its data streams.", False)
            if len(kids) >= 2:
                return ("Each of its assets holds at most one of each of several kinds of "
                        "leaf asset, so those are most likely its parts.", True)
            return None
        if not tree.parents.get(m):
            return "It stands alone, with no parent and no child assets.", False
        return None

    def _lone_singleton_holder(self, m: str) -> bool:
        kids = self._aspect_kids(m)
        return kids is not None and len(kids) == 1 and not (_tokens(kids[0]) & _STREAM_TOKENS)

    def populations(self) -> None:
        """A leaf model that repeats under one parent is that parent's equipment
        (per child model: a one-off sibling is not swept along)."""
        tree = self.tree
        for m in tree.models:
            if m in self.out or self.under_equipment(m):
                continue
            for c in tree.children.get(m, {}):
                if c not in self.out and tree.is_leaf(c) and not self.under_equipment(c) \
                        and tree.max_per_parent.get((m, c), 0) >= 2:
                    self.decide(c, EQUIPMENT, "structure",
                                "Several of its assets sit side by side under one parent.")

    def _by_contents(self, m: str) -> tuple[str, str, bool] | None:
        kid_roles = {self.role(c) for c in self.tree.children.get(m, ())}
        if SITE in kid_roles:
            return ENTERPRISE, "It holds sites.", False
        if AREA in kid_roles:
            return SITE, "It holds areas.", False
        if WORK_CENTER in kid_roles and not self.tree.parents.get(m):
            # Sources often skip the area tier; a top-level holder of work
            # centers is most likely the site. Not certain, so flagged.
            return SITE, "It is top-level and holds work centers.", True
        if WORK_CENTER in kid_roles:
            return AREA, "It holds work centers.", False
        if WORK_UNIT in kid_roles:
            return WORK_CENTER, "It holds work units.", False
        return None

    def _by_position(self, m: str) -> tuple[str, str, bool]:
        parent_roles = {self.role(p) for p in self.tree.parents.get(m, ())}
        inside = parent_roles & {WORK_CENTER, WORK_UNIT}
        if inside and parent_roles - {WORK_CENTER, WORK_UNIT}:
            return (UNCLASSIFIED, "Some of its assets sit inside a work center and others "
                    "directly under a site or area, so one role can't cover both.", True)
        if inside:
            return WORK_UNIT, "It holds equipment and sits inside a work center.", False
        return (WORK_CENTER, "It holds equipment and is the first such tier below its "
                "site or area.", False)

    def containers(self) -> None:
        """Place every model with equipment below it. Under a known tier its
        role follows from where it sits; with none known above it, from the
        equipment up (nearest container = work center), flagged for review."""
        pending = {m for m in self.tree.models
                   if m not in self.out and not self.tree.is_leaf(m)
                   and not self.under_equipment(m) and self.above_equipment(m)}
        order = self.tree.top_down()
        while pending:
            anchored = [m for m in order if m in pending and self.held_by_containers(m)]
            for m in anchored:                      # top-down; parents placed first
                if self.held_by_containers(m):
                    role, why, review = self._by_contents(m) or self._by_position(m)
                    self.decide(m, role, "unresolved" if role == UNCLASSIFIED else "structure",
                                why, review)
                    pending.discard(m)
            if anchored:
                continue
            m = next((m for m in reversed(order)     # deepest unanchored first
                      if m in pending and not (self.tree.descendants(m) & pending)),
                     None) or sorted(pending)[0]     # (a model-level cycle)
            role, why, _ = self._by_contents(m) or (
                WORK_CENTER, "It holds equipment and nothing above it says otherwise.", False)
            self.decide(m, role, "structure", why, True)
            pending.discard(m)

    def contained_leaves(self) -> None:
        for m in self.tree.models:
            if m not in self.out and self.tree.is_leaf(m) and \
                    not self.under_equipment(m) and self.held_by_containers(m):
                self.decide_contained_leaf(m)

    # ── 4-5. below equipment: parts or data streams ──
    def below_equipment(self) -> None:
        changed = True
        while changed:
            changed = False
            for m in self.tree.models:
                if m in self.out or not self.under_equipment(m):
                    continue
                if _tokens(m) & _STREAM_TOKENS:
                    self.decide(m, DATA_STREAM, "structure",
                                "It sits below equipment and its model name marks it "
                                "as a data stream.")
                else:
                    self.decide(m, EQUIPMENT_PART, "structure", "It sits below equipment.")
                changed = True

    def unresolved(self) -> None:
        for m in self.tree.models:
            if m not in self.out:
                self.decide(m, UNCLASSIFIED, "unresolved",
                            "No rule reached it: no equipment was found above or below it.",
                            review=True)


def classify_models(assets: Iterable[AssetRow],
                    equipment_hits: Mapping[str, int] | None = None,
                    overrides: Mapping[str, str] | None = None,
                    matched_assets: Iterable[str] | None = None) -> dict[str, ModelRole]:
    """Classify every asset model of ONE system.

    ``equipment_hits`` maps a model to how many of its instances an accepted
    equipment-id value mapping lands on (see :func:`equipment_hits_from_mappings`).
    ``matched_assets`` are the ids of those instances (see
    :func:`matched_assets_from_mappings`); the sibling rule reads them per
    parent asset, so without them a sibling is never set aside for lack of an
    identifier. ``overrides`` maps a model to a steward-confirmed role.
    """
    return _Classifier(assets, equipment_hits, overrides, matched_assets).run()


def asset_depths(assets: Iterable[AssetRow]) -> dict[str, int]:
    """Depth of every asset from its root (root = 0), cycle-safe."""
    rows = list(assets)
    parent = {a.id: a.parent_id for a in rows}
    known = set(parent)
    depths: dict[str, int] = {}
    for a in rows:
        chain, cur, seen = [], a.id, set()
        while cur is not None and cur in known and cur not in depths and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            nxt = parent.get(cur)
            cur = nxt if nxt in known else None
        base = depths.get(cur, -1) if cur is not None and cur in depths else -1
        for i, node in enumerate(reversed(chain)):
            depths[node] = base + 1 + i
    return depths


def equipment_hits_from_mappings(assets: Iterable[AssetRow], value_mappings: Iterable[Mapping],
                                 system_id: str) -> dict[str, int]:
    """Count, per model, the instances an equipment-id value mapping lands on
    (the assets :func:`matched_assets_from_mappings` returns)."""
    rows = list(assets)
    model_of = {str(a.id): a.model for a in rows}
    return dict(Counter(model_of[a] for a in matched_assets_from_mappings(
        rows, value_mappings, system_id)))


def matched_assets_from_mappings(assets: Iterable[AssetRow], value_mappings: Iterable[Mapping],
                                 system_id: str) -> set[str]:
    """The ids of this system's assets an equipment-id value mapping lands on.

    A mapping counts on this system's side (``to_*`` when ``to_system`` is the
    system, ``from_*`` when ``from_system`` is). A target-side
    ``to_element_id`` names the asset exactly and is used alone. Otherwise the
    value matches an asset's source id, or else its name when exactly one asset
    of the system carries that name; a value that is one asset's id and
    another's name, or a name several assets share, counts for nothing.
    """
    rows = list(assets)
    by_id = {str(a.id): a for a in rows}
    by_name: dict[str, list[AssetRow]] = defaultdict(list)
    for a in rows:
        if a.name:
            by_name[str(a.name)].append(a)
    matched: set[str] = set()
    for vm in value_mappings:
        if str(vm.get("concept") or "") not in EQUIPMENT_ID_CONCEPTS:
            continue
        for side in ("to", "from"):
            if vm.get(f"{side}_system") != system_id:
                continue
            asset = _mapped_asset(vm, side, by_id, by_name)
            if asset is not None:
                matched.add(str(asset.id))
    return matched


def _mapped_asset(vm: Mapping, side: str, by_id: dict, by_name: dict) -> AssetRow | None:
    element = str(vm.get("to_element_id") or "") if side == "to" else ""
    if element:
        return by_id.get(element)
    value = str(vm.get(f"{side}_value") or "")
    by_value_id = by_id.get(value)
    named = by_name.get(value, [])
    if by_value_id is not None:
        return None if any(n.id != by_value_id.id for n in named) else by_value_id
    return named[0] if len(named) == 1 else None
