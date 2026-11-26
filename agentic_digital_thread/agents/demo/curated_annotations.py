# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Curated demo schema annotations (demo addon — ADR 0019).

CONTENT half of the curated-annotation seeding split: facts about the demo
dataset's schema quirks that the LLM otherwise re-learns through failed
queries on every fresh deploy. The MECHANISM half —
``self_learning.seed_preaccepted_annotations`` — lives in engine code and
takes this dict as a parameter, so engine files carry zero demo literals
(project constraint). The onboarding provision post-pass seeds these as
pre-accepted ``field_annotation`` records (idempotent, deterministic keys),
then the standard replay path applies them to Neptune — a full wipe +
redeploy + onboard reproduces the same query behavior with no manual steps.
"Clear All" in Self-Learning removes them for the learn-from-zero story; the
next onboard re-seeds.

Keyed by the canonical demo system ids (``agents/demo/systems.py``). This
module is pure data + the demo-enablement gate; it depends only on its own
package (one-way dependency: engine callers route through
``curated_annotations()``, never the reverse).
"""

CURATED_FIELD_ANNOTATIONS = {
    # system_id -> {"table.field": annotation}
    sys_id: {
        "work_orders.line": (
            "Line identifiers are UPPERCASE: LINE1, LINE2, LINE3. "
            "Filter with line = 'LINE1' (never 'Line1' or 'Line 1')."),
        "work_orders.planned_quantity": (
            "There are NO variance columns (variance_quantity / variance_percentage / "
            "item_id do NOT exist in work_orders). Compute variance as "
            "planned_quantity - actual_quantity."),
        "work_orders.actual_quantity": (
            "There are NO variance columns in this table. "
            "Variance = planned_quantity - actual_quantity."),
    }
    for sys_id in ("mes-mil", "mes-det", "mes-cr")
}


def curated_annotations() -> dict:
    """The curated annotation content, or ``{}`` when the demo addon is
    disabled — the engine call site (onboard provision post-pass) treats an
    empty dict as "nothing to seed", so non-demo deployments are untouched."""
    from . import is_enabled
    if not is_enabled():
        return {}
    return {sys_id: dict(fields) for sys_id, fields in CURATED_FIELD_ANNOTATIONS.items()}
