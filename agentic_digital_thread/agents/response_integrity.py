# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Compatibility facade for deterministic Mode A response integrity.

Implementation is split by responsibility so contracts, trusted capture,
validation, and rendering can be audited independently. Existing imports remain
stable through this module.
"""
from __future__ import annotations

from .response_integrity_contracts import *  # noqa: F401,F403
from .response_integrity_capture import (  # noqa: F401
    attach_source_metadata,
    bounded_payloads,
    capture_private_source_evidence,
    capture_trusted_tool_result,
    initialize_integrity_state,
    utc_now,
)
from .response_integrity_validation import (  # noqa: F401
    capture_orchestrator_tool_result,
    final_draft_from_result,
    system_answer_from_result,
    validate_final_answer,
    validate_system_answer,
)
from .response_integrity_rendering import (  # noqa: F401
    gate_final_result,
    render_final_answer,
)
from .response_integrity_concepts import (  # noqa: F401
    concept_shadow_check,
    resolve_evidence_concepts,
)

# The memory-authority rule (historical context only; re-query the owning
# source) is standing text in the orchestrator prompt,
# ``orchestrator._MEMORY_AUTHORITY_RULE``. It was a per-turn note here until a
# live answer passed the note on to the reader (2026-10-27).
