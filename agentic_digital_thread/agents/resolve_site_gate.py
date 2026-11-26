# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Site scope for proposed identity rules (ADR-0051 P8, every system class).

A system that serves one plant writes that plant's local names: its logins,
crews, lines and reason codes. Plants built from the same templates write the
same names, so a value one plant's system shares with ANOTHER plant's system
is not evidence that the two fields name the same thing. A maintenance log
author at one plant shares its four technician logins with the downtime
``resolved_by`` of every plant's MES; only the same plant's MES names the same
people.

ADR-0051 P8 first refused such links only between two systems of the SAME
class (the exact-agreement pass, ``onboard._exact_rule_refusal``, and
self-learning proposals; the self-learning gate,
``self_learning._gate_cross_site_namespace``, now refuses every class too).
The resolve step's model rules and the cross-class pairs (a site CMMS and
another site's MES) were not covered. :func:`gate_cross_site_rules`
withholds a proposed identity rule whenever the systems it names serve
DIFFERENT plants, of any class. A plant is ``metric_multisite.site_of`` (the
P8 site notion): an unsited or All Sites system has no plant, so its rules to
any plant pass. The P8 lever still applies: a steward-confirmed All Sites
authority for the rule's concept (``self_learning._authority_vouch_verdict``)
says the concept's values are one namespace across plants, and the rule
passes. An unconfirmed authority does not vouch (machine claims never vouch
for machine claims, ADR-0052 B4). Only an authority whose owner is a KNOWN
unsited system vouches: an unknown owner, an empty system inventory or a
failed read vouches for nothing.

Withheld rules move to ``rejected_rules`` with a ``rejected_reason``, so the
Identity Rules checkpoint still lists them and says why. A model-written
target is resolved to one catalog system (an exact id, the one entry with
that display name, or :func:`match_system_id`, the containment rule the
identity-rule writer also uses), and a kept rule's ``target_system`` is
rewritten to that id, so the writer, which matches ids only, writes the
system the gate judged. For a system that serves a plant, a target that names
no known system or more than one is withheld: its plant is unknown. Nothing
here names a dataset, plant or system.
"""
from __future__ import annotations

import logging
from typing import Callable

from .metric_multisite import site_of

logger = logging.getLogger(__name__)

#: ``vouch(concept) -> (verdict, authority rule id)``, verdicts as
#: ``self_learning._authority_vouch_verdict``: ``steward``, ``unverified``, ``none``.
Vouch = Callable[[str], "tuple[str, str]"]


def match_system_id(name, ids) -> "tuple[str, str]":
    """``(system id, "")`` for the ONE id in ``ids`` that ``name`` names, or
    ``("", reason)``: ``unknown`` or ``ambiguous``. ``name`` is an exact id,
    or names exactly one id by containment either way (a short ``mes`` for
    ``mes-mil``, a decorated ``mes-mil-db``), as the identity-rule writer has
    always read a model-written target. An empty name names nothing. The
    writer (``onboard._dp_rule_target_check``) and :func:`cross_site_refusal`
    resolve with this one rule, so the site gate judges the system the writer
    would write."""
    name = str(name or "").strip()
    ids = {str(i) for i in ids or () if i}
    if not name:
        return "", "unknown"
    if name in ids:
        return name, ""
    found = sorted(i for i in ids if name in i or i in name)
    if len(found) == 1:
        return found[0], ""
    return "", "ambiguous" if found else "unknown"


def _entry_for(name, catalog) -> "tuple[dict | None, str]":
    """``(catalog entry, "")`` for the system ``name`` names, else
    ``(None, reason)``. ``name`` is model-written: an exact id, the ONE
    entry whose display name it is, or a name :func:`match_system_id`
    resolves among the catalog's ids; else (a process whose catalog was never
    hydrated) the catalog's graph ``lookup`` by id. A read failure is
    ``unknown``: a rule naming a system the gate cannot place is withheld."""
    name = str(name or "").strip()
    if not name:
        return None, "unknown"
    try:
        entry = catalog.get(name)
        if entry:
            return entry, ""
        entries = list(catalog.get_all() or [])
        folded = name.casefold()
        named = [e for e in entries if str(e.get("name") or "").strip().casefold() == folded]
        if len(named) > 1:
            return None, "ambiguous"
        if named:
            return named[0], ""
        sid, reason = match_system_id(name, [e.get("id") for e in entries])
        if sid:
            return catalog.get(sid), ""
        if reason == "ambiguous":
            return None, reason
        lookup = getattr(catalog, "lookup", None)
        entry = lookup(name) if lookup else None
        return (entry, "") if entry else (None, "unknown")
    except Exception:  # noqa: BLE001 - a system the gate cannot read is unplaced
        return None, "unknown"


def _site_key(site: str) -> str:
    return str(site or "").strip().casefold()


def make_vouch() -> Vouch:
    """A per-resolve vouch: one authority read per concept, cached. Only an
    authority whose owner is a KNOWN unsited system vouches
    (``known_owner_only``); an empty system inventory or a failed read vouches
    for nothing (the rule stays visible as withheld)."""
    cache: dict = {}

    def vouch(concept: str) -> "tuple[str, str]":
        key = str(concept or "").strip().casefold()
        if not key:
            return ("none", "")
        if key not in cache:
            try:
                from . import self_learning
                entries = {str(s.get("id") or "").strip().casefold(): s
                           for s in self_learning.known_systems() if s.get("id")}
                cache[key] = self_learning._authority_vouch_verdict(
                    concept, entries, known_owner_only=True) if entries else ("none", "")
            except Exception as e:  # noqa: BLE001 - no vouch is the safe answer
                logger.info("[RESOLVE] All Sites authority read failed for '%s' (%s): "
                            "no vouch", concept, type(e).__name__)
                cache[key] = ("none", "")
        return cache[key]

    return vouch


def _sites(names, catalog) -> list:
    """The distinct plants of the systems ``names`` resolves to, in order."""
    sites: list = []
    for name in names:
        entry, _reason = _entry_for(name, catalog)
        site = site_of(entry) if entry else ""
        if site and _site_key(site) not in {_site_key(s) for s in sites}:
            sites.append(site)
    return sites


def cross_site_refusal(system_id: str, rule: dict, catalog, vouch: Vouch) -> str:
    """Why ``rule`` must be withheld because the systems it names serve
    different plants, or ``""``. The systems are the onboarding system, the
    rule's ``target_system`` and, when the gate can place it, its
    ``source_system`` (model-written; the writer always writes the onboarding
    system as the source, so a name the gate cannot place there is ignored).
    When the onboarding system serves a plant, a target the gate cannot place
    (empty, unknown, or naming more than one system) is withheld: its plant is
    unknown, and the writer would refuse it as well."""
    own = _sites([system_id], catalog)
    target = rule.get("target_system")
    entry, why = _entry_for(target, catalog)
    if own and entry is None:
        shown = str(target or "").strip()
        return (f"target system '{shown}' names more than one system, so its plant is unknown"
                if why == "ambiguous" and shown else
                f"target system '{shown}' isn't a known system, so its plant is unknown")
    sites = _sites([system_id, rule.get("source_system"), target], catalog)
    if len(sites) < 2:
        return ""
    concept = str(rule.get("concept") or "")
    verdict, ar_id = vouch(concept)
    if verdict == "steward":
        return ""
    reason = (f"different plants ({sites[0]}, {sites[1]}): a value both plants use "
              f"isn't evidence the two fields name the same thing")
    if verdict == "unverified":
        reason += f"; the All Sites authority for '{concept}' ({ar_id}) isn't confirmed"
    return reason


def _canonical_target(rule: dict, catalog) -> dict:
    """``rule`` with ``target_system`` written as the catalog id the gate
    resolved it to (a display name or short name the gate placed), so the
    identity-rule writer, which matches ids only, writes the system the gate
    judged. ``rule`` itself when the target is already that id or can't be
    placed (the writer then refuses or resolves it as before)."""
    entry, _why = _entry_for(rule.get("target_system"), catalog)
    sid = str((entry or {}).get("id") or "")
    if not sid or sid == rule.get("target_system"):
        return rule
    return {**rule, "target_system": sid}


def gate_cross_site_rules(system_id: str, result: dict, catalog,
                          vouch: Vouch | None = None) -> dict:
    """``result`` with every rule :func:`cross_site_refusal` refuses moved from
    ``rules`` to ``rejected_rules`` (after any already there, each with its
    ``rejected_reason``, one row per candidate: ``resolve_gates.merge_withheld``),
    and every kept rule's ``target_system`` written as the catalog id it names
    (:func:`_canonical_target`). Every
    other key passes unchanged; ``result`` itself when nothing changed."""
    rules = result.get("rules")
    if not isinstance(rules, list) or not rules:
        return result
    vouch = vouch or make_vouch()
    kept: list = []
    withheld: list = []
    for rule in rules:
        if not isinstance(rule, dict):
            kept.append(rule)
            continue
        reason = cross_site_refusal(system_id, rule, catalog, vouch)
        if not reason:
            kept.append(_canonical_target(rule, catalog))
            continue
        withheld.append({**rule, "rejected_reason": reason})
        logger.info("[RESOLVE] %s: identity rule withheld (%s): %s -> %s.%s", system_id, reason,
                    rule.get("source_field"), rule.get("target_system"), rule.get("target_field"))
    if not withheld and all(a is b for a, b in zip(kept, rules)):
        return result
    out = {**result, "rules": kept}
    if withheld:
        # One row per candidate: a pair the overlap gate already withheld and
        # the affinity pass re-added is listed once, with the first reason.
        from .resolve_gates import merge_withheld
        out["rejected_rules"] = merge_withheld(result.get("rejected_rules") or [], withheld)
    return out
