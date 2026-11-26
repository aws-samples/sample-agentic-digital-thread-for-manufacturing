# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which rule statuses switch a rule off: the ONE predicate every reader uses.

A soft-disabled IdentityRule or JoinPath keeps its node for audit and stops
taking part in decisions. Two statuses mean that: ``deactivated`` (the
steward route and a requeue) and ``retired``. Each reader that decides with
an IdentityRule (the route seed and the identifier translations computed
from it, resolve, the /paths view, the system Connections section), each
reader that filters a JoinPath's own status, and the JoinPath link
(:mod:`joinpath_rule_link`) builds its filter here, so a status added to
:data:`INACTIVE_RULE_STATUSES` is honoured everywhere at once. The metamodel
and the ``explore_graph`` description state the same predicate to a
free-form reader (:func:`active_where`).
"""
from __future__ import annotations

#: Statuses that switch a rule off.
INACTIVE_RULE_STATUSES = ("deactivated", "retired")


def is_inactive_status(status: object) -> bool:
    """Python twin of :func:`rule_inactive`."""
    return isinstance(status, str) and status in INACTIVE_RULE_STATUSES


def _active_conditions(alias: str) -> str:
    return " AND ".join(f"coalesce({alias}.status, '') <> '{status}'"
                        for status in INACTIVE_RULE_STATUSES)


def rule_active(alias: str) -> str:
    """openCypher: the node bound to ``alias`` is not switched off. True for a
    node with no status, and for a null binding (an OPTIONAL MATCH miss).
    Parenthesized, so it can be joined with AND/OR as it is."""
    return "(" + _active_conditions(alias) + ")"


def active_where(alias: str) -> str:
    """The same predicate as a ``WHERE`` clause, the exact text the metamodel
    and the ``explore_graph`` description give a free-form reader."""
    return "WHERE " + _active_conditions(alias)


def rule_inactive(alias: str) -> str:
    """openCypher: the node bound to ``alias`` is switched off (false for a
    null binding)."""
    return f"NOT {rule_active(alias)}"
