# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which of a connector's own read tools reads which of its entities.

An API connector's schema entries are entities (a Tulip
table, a SiteWise asset model) that its tools read under other names
(``tulip_query_records``, ``sitewise_list_assets``).
Each connector declares the mapping next to the tool list it owns
(``system_agent.entity_reads_for``). The onboarding agent test reads it to
tell, from the investigation trace of its own call, whether a read tool of
the tested entity read the source (``onboard_verify_agent``).

A rule names the tools that read an entity. An entity the source names
itself (a Tulip table, a SiteWise asset model, an i3X object type) is read
only by a call whose ``id_arg`` argument holds that entity's id in the
source. A connector whose source can name a table after one of its fixed
entities declares that a name does not tell the two apart
(``EntityReads.names_tell_origin``), and no trace proves its reads.

The proof is the tool call and what the tool parsed from the source's
response: a traced step of a mapped tool whose status is ``success`` and
that carries a ``records_read`` count (what the tool itself reported for a
response it parsed, ``tool_result_capture.report_read_count``). Zero is a
read: the source answered and the entity holds no records. A missing count
is not zero: a tool reports nothing for a read that failed, so such a step
proves nothing. Never the answer text, never a field name sampled at
onboarding.

Stdlib only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EntityRead:
    """The tools that read one entity, and the argument that must carry the
    entity's source id (``""``: any call of the tools reads it)."""

    tools: tuple
    id_arg: str = ""


@dataclass(frozen=True)
class EntityReads:
    """One connector's rules: ``named`` entities by their fixed names, and
    ``source_named`` for every other entity, named by the source.

    ``names_tell_origin`` says whether a table's name alone tells which rule
    reads it. It is False for a connector whose discovery can return a
    source-named table under one of its fixed names: no table origin is kept,
    so a fixed rule could be applied to a source-named table, with no id
    check. No trace proves a read of such a connector's tables."""

    named: dict = field(default_factory=dict)
    source_named: "EntityRead | None" = None
    names_tell_origin: bool = True

    def rule(self, entity: str) -> "EntityRead | None":
        """The rule for ``entity``, or None when no tool reads it."""
        return self.named.get(entity) or self.source_named


def _records_read(step: dict) -> "int | None":
    """The step's count, or None when it carries no whole non-negative one."""
    count = step.get("records_read")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return None


def _call_input(step: dict) -> dict:
    """The traced call's input, parsed; ``{}`` when it does not parse (a cut
    input, foreign text), so an id it may have held is never read."""
    try:
        value = json.loads(step.get("tool_input") or "")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def traced_reads(steps, rule: EntityRead) -> list:
    """The inputs of the traced calls of ``rule``'s tools whose tool parsed
    the source's response: status ``success`` and a ``records_read`` count,
    zero included."""
    if not isinstance(steps, list):
        return []
    return [_call_input(step) for step in steps
            if isinstance(step, dict) and step.get("tool_name") in rule.tools
            and step.get("status") == "success" and _records_read(step) is not None]


def proves_read(calls: list, rule: EntityRead, entity_id: str = "") -> bool:
    """Whether ``calls`` (:func:`traced_reads`) read the entity: any one of
    them for a rule with no id argument, else one whose id argument is the
    string ``entity_id`` exactly (never when the id is unknown)."""
    if not calls:
        return False
    if not rule.id_arg:
        return True
    return bool(entity_id) and any(
        isinstance(call.get(rule.id_arg), str) and call[rule.id_arg] == entity_id
        for call in calls)
