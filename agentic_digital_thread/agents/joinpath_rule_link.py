# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A JoinPath made from an IdentityRule follows that rule's lifecycle.

Onboarding writes one cross-system JoinPath per accepted IdentityRule
(``onboard._dp_write_rule_joinpath``). No edge joins the two nodes. The
JoinPath names its rule by ``jp.rule_id`` (written since the connection
evidence properties) and, on graphs written before that, by its id alone:
both ids are minted from one content digest, ``jp-<system>-<digest>`` and
``ir-<system>-<digest>``. Any other JoinPath (an internal, value-overlap or
same-row join, or an accepted proposal's ``subj-`` node) names no rule and
is unaffected.

Read side. A JoinPath whose linked rule is switched off
(:data:`INACTIVE_RULE_STATUSES`) is not a join: it is not served to an agent,
not used to plan or translate, and not shown as a connection. Every reader
does that in its own Cypher with :func:`linked_rule_match` and
:func:`rule_inactive` (or :func:`drop_if_rule_inactive`), so a JoinPath the
rule switched off is left out in the same statement that reads it; the route
seed collects it as null, so it never takes a capped slot. Observed live: a
deactivated rule's JoinPath stayed in the route seed and the planner sent one
system's line names to a column holding the other system's site codes.

Write side. Deactivating a rule marks each of its JoinPaths deactivated and
records the rule in :data:`RULE_MARKER`, so the data itself carries the
status a free-form reader filters on (the metamodel's JoinPath contract);
reactivating the rule restores only the JoinPaths carrying that mark
(:func:`cascade_rule_lifecycle`). A JoinPath already switched off (a
steward's decision, or ``retired``) is never marked, so the rule coming back
does not override that decision, and a direct JoinPath decision removes the
mark. Never a delete; both directions are idempotent, and deactivating an
already-deactivated rule again marks any JoinPath of it still unmarked.
"""
from __future__ import annotations

from .rule_status import (  # noqa: F401 (re-exported)
    INACTIVE_RULE_STATUSES, is_inactive_status, rule_active, rule_inactive)

#: JoinPath property naming the rule whose deactivation switched it off.
RULE_MARKER = "deactivated_with_rule"

_JOINPATH_PREFIX = "jp-"
_RULE_PREFIX = "ir-"


def linked_rule_id(jp_id: "str | None", rule_id: "str | None" = None) -> "str | None":
    """The id of the IdentityRule a JoinPath came from, or ``None``: its
    ``rule_id`` when set, else the rule id sharing its digest suffix. The
    Python twin of :func:`linked_rule_id_expr`."""
    if rule_id:
        return str(rule_id)
    jid = str(jp_id or "")
    if jid.startswith(_JOINPATH_PREFIX):
        return _RULE_PREFIX + jid[len(_JOINPATH_PREFIX):]
    return None


def linked_rule_id_expr(jp: str = "jp") -> str:
    """openCypher for :func:`linked_rule_id` on the JoinPath bound to ``jp``
    (null when it names no rule)."""
    return (f"CASE WHEN coalesce({jp}.rule_id, '') <> '' THEN {jp}.rule_id "
            f"WHEN {jp}.id STARTS WITH '{_JOINPATH_PREFIX}' "
            f"THEN '{_RULE_PREFIX}' + substring({jp}.id, {len(_JOINPATH_PREFIX)}) END")


def linked_rule_match(jp: str = "jp", rule: str = "jp_rule") -> str:
    """Bind ``rule`` to the JoinPath's IdentityRule (null when there is none)."""
    return (f"OPTIONAL MATCH ({rule}:IdentityRule) "
            f"WHERE {rule}.id = {linked_rule_id_expr(jp)} ")


def drop_if_rule_inactive(jp: str = "jp", rule: str = "jp_rule",
                          carry: "tuple[str, ...]" = ()) -> str:
    """Clauses that follow a ``MATCH (jp:JoinPath) WHERE ...`` and keep only
    the JoinPaths whose rule is active or absent. ``carry`` names any other
    variables the query still needs."""
    keep = ", ".join((*carry, jp, rule))
    return (linked_rule_match(jp, rule)
            + f"WITH {keep} WHERE NOT ({rule_inactive(rule)}) ")


def effective_status(jp: str = "jp", rule: str = "jp_rule") -> str:
    """The JoinPath's status as a reader should report it: ``deactivated``
    when its rule is switched off, else its own status."""
    return f"CASE WHEN {rule_inactive(rule)} THEN 'deactivated' ELSE {jp}.status END"


def _inactive_joinpaths_read(where: str, tail: str) -> str:
    """Switched-off JoinPaths (their own status, or their rule's) matching
    ``where``, with their endpoints and why."""
    return ("MATCH (jp:JoinPath) " + where
            + linked_rule_match()
            + f"WITH jp, jp_rule WHERE {rule_inactive('jp')} "
            f"OR {rule_inactive('jp_rule')} "
            "RETURN jp.id AS id, jp.from_system AS from_system, jp.from_table AS from_table, "
            "jp.from_field AS from_field, jp.to_system AS to_system, jp.to_table AS to_table, "
            "jp.to_field AS to_field, jp_rule.id AS rule_id, "
            f"CASE WHEN {rule_inactive('jp_rule')} THEN 'rule_inactive' ELSE 'deactivated' END "
            "AS reason ORDER BY jp.id" + tail)


#: The switched-off JoinPaths among the result's strings (``$values``), by
#: exact id only: what ``explore_graph`` withholds and names.
INACTIVE_JOINPATHS_SCOPED_CYPHER = _inactive_joinpaths_read("WHERE jp.id IN $values ", "")

#: Every switched-off JoinPath, bounded: the fallback when a result holds
#: too many distinct strings to scope the read.
INACTIVE_JOINPATHS_CYPHER = _inactive_joinpaths_read("", " LIMIT 1000")

# Both cascade writes end in an aggregate, so a write that ran answers one
# row (``changed`` may be 0) and a failed one (the fail-soft seam's ``[]``)
# answers none: the caller can tell them apart.
#
# Deactivation switches off every JoinPath made from the rule, and any the
# rule already marked, that is not already switched off. Running it again on
# a rule that is already deactivated therefore marks a JoinPath an earlier
# run missed (one written before the cascade existed) and changes nothing
# else: a JoinPath already off, by the rule or by a steward, is not touched.
_DEACTIVATE_WITH_RULE_CYPHER = (
    f"MATCH (jp:JoinPath) WHERE ({linked_rule_id_expr()} = $rid OR jp.{RULE_MARKER} = $rid) "
    f"AND {rule_active('jp')} "
    "SET jp.status = 'deactivated', jp.deactivated_by = $actor, "
    f"jp.deactivated_at = $now, jp.{RULE_MARKER} = $rid "
    "WITH count(jp) AS changed RETURN changed"
)

_RESTORE_WITH_RULE_CYPHER = (
    f"MATCH (jp:JoinPath) WHERE jp.{RULE_MARKER} = $rid "
    "SET jp.status = 'active', jp.reactivated_by = $actor, jp.reactivated_at = $now "
    f"REMOVE jp.{RULE_MARKER} "
    "WITH count(jp) AS changed RETURN changed"
)

#: Appended to a direct JoinPath lifecycle write: the steward's decision on
#: the JoinPath itself replaces any mark its rule left.
DIRECT_DECISION_CLAUSE = f"REMOVE jp.{RULE_MARKER} "

#: Ends a direct JoinPath lifecycle write: its id, plus the rule it came from
#: and whether that rule is switched off (then the JoinPath stays off for
#: every reader, whatever its own status).
DIRECT_DECISION_RETURN = (
    "WITH jp " + linked_rule_match()
    + f"RETURN jp.id AS id, jp_rule.id AS rule_id, {rule_inactive('jp_rule')} AS rule_inactive"
)


def cascade_rule_lifecycle(run, rule_id: str, actor: str, now: str,
                           *, deactivate: bool):
    """Switch off (``deactivate``) or restore the JoinPaths made from rule
    ``rule_id`` through the graph seam ``run(cypher, params)``, returning what
    ``run`` returns: one ``{"changed": n}`` row when the write ran."""
    cypher = _DEACTIVATE_WITH_RULE_CYPHER if deactivate else _RESTORE_WITH_RULE_CYPHER
    return run(cypher, {"rid": rule_id, "actor": actor, "now": now})
