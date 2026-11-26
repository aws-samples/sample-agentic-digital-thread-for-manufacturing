# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tripwire: edge ACCESS-token validity is pinned EQUAL to the ID-token
validity (constants in ``stacks/_edge_auth.py``).

The SAR cloudfront-authorization-at-edge ``CheckAuth`` handler verifies ONLY
the id token and redirects to ``/refreshauth`` ONLY on id-token expiry — it
never inspects the access token. The origin validates the ACCESS token
(``agents/auth/middleware.py`` -> ``cognito.py::validate_token``). If access
validity were shorter than id validity, the edge would keep admitting the
browser while the origin 401s every ``/api/*`` call, dropping the SPA onto
``/login.html`` — a dead end for external-IdP-only (``oidc_only``) users.

This complements the synth-time assertions in ``tests/test_edge_external_oidc.py``
(which skip when the CDK CLI is unavailable) with an import-only check that
always runs.
"""
from __future__ import annotations

from stacks._edge_auth import (
    EDGE_ACCESS_TOKEN_VALIDITY,
    EDGE_ID_TOKEN_VALIDITY,
    EDGE_REFRESH_TOKEN_VALIDITY,
)


def test_access_token_validity_equals_id_token_validity():
    """CheckAuth refreshes on id-token expiry only; the origin checks access."""
    assert (
        EDGE_ACCESS_TOKEN_VALIDITY.to_minutes() == EDGE_ID_TOKEN_VALIDITY.to_minutes()
    )


def test_edge_lifetime_values_pinned():
    """60-minute access/id tokens; 10-hour refresh token (one working day)."""
    assert EDGE_ACCESS_TOKEN_VALIDITY.to_minutes() == 60
    assert EDGE_ID_TOKEN_VALIDITY.to_minutes() == 60
    assert EDGE_REFRESH_TOKEN_VALIDITY.to_hours() == 10
