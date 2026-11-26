# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Exact persisted identity of ADR-0058 machine-miner proposals."""

MINER_METHOD = "cross-system-miner"
MINER_PROVENANCE = "adr-0058-machine-miner"
MINER_SOURCE = "agent"
MINER_PROPOSED_TYPES = frozenset({"join_path", "crosswalk"})


def is_miner_artifact(row: dict) -> bool:
    """True only for rows emitted by the canonical ADR-0058 miner."""
    return (
        str(row.get("method") or "") == MINER_METHOD
        and str(row.get("provenance") or "") == MINER_PROVENANCE
        and str(row.get("source") or "") == MINER_SOURCE
        and str(row.get("proposed_type") or "") in MINER_PROPOSED_TYPES
    )
