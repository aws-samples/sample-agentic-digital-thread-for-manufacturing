# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read-only admin routes check admin access in the route itself.

``AuthMiddleware`` already rejects non-admins under ``/api/admin``. These
routes also call ``_check_admin`` so the rule does not depend on the
middleware alone. No AWS, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import agents.admin_routes as admin_routes

_ROUTES = [
    "/api/admin/quick-links",
    "/api/admin/llm-settings",
    "/api/admin/models",
    "/api/admin/llm-settings/memory-model/job",
    "/api/admin/llm-settings/memory-model/job/some-job",
]


class _Inject(BaseHTTPMiddleware):
    def __init__(self, app, is_admin: bool):
        super().__init__(app)
        self._is_admin = is_admin

    async def dispatch(self, request, call_next):
        request.state.is_admin = self._is_admin
        return await call_next(request)


@pytest.mark.parametrize("path", _ROUTES)
def test_non_admin_gets_403(path):
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.add_middleware(_Inject, is_admin=False)
    r = TestClient(app, raise_server_exceptions=False).get(path)
    assert r.status_code == 403
