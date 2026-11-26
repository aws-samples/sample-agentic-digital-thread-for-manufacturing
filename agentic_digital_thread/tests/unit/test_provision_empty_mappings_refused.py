# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""POST /api/onboard/provision refuses an empty mapping list.

The catalog Re-map Save re-provisions an already-onboarded system. A client
that sent zero mappings (the map start reply read as the result) must be
refused before any job starts, so it can never replace a system's mappings
with nothing. ``_trusted_provision_body`` is the guard; this pins it at the
route boundary.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agents import onboard


def _onboard_app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def roles(request: Request, call_next):
        request.state.is_admin = True
        request.state.is_integrator = True
        return await call_next(request)

    app.include_router(onboard.router)
    return app


def test_provision_with_no_mappings_is_refused_before_any_job(monkeypatch):
    started = []

    def forbidden(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("an empty provision reached the job path")

    monkeypatch.setattr(onboard, "_onboard_provision_legacy", forbidden)
    monkeypatch.setattr(onboard, "_run_provision_job", forbidden)
    monkeypatch.setattr(onboard, "_reset_conflict_or_none", lambda system_id: None)

    response = TestClient(_onboard_app()).post(
        "/api/onboard/provision",
        json={
            "system_id": "system-a", "system_name": "System A",
            "connection_type": "PostgreSQL", "schema": "schema_a",
            "rules": [], "mappings": [], "table_entities": [], "table_notes": [],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "schema_provenance_invalid"
    assert started == []
