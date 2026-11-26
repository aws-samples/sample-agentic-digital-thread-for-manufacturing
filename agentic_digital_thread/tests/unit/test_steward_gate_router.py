# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ADR-0076 D2 — the router-importable steward gate fails CLOSED."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agents.steward_gate import check_steward


def _req(**state):
    return SimpleNamespace(state=SimpleNamespace(**state))


def test_admin_passes():
    assert check_steward(_req(is_admin=True)) is None


@pytest.mark.parametrize("state", [{"is_admin": False}, {}, {"is_admin": None}])
def test_non_admin_and_absent_verdict_are_denied(state):
    with pytest.raises(HTTPException) as e:
        check_steward(_req(**state))
    assert e.value.status_code == 403
    assert e.value.detail == "Steward (admin) role required"
