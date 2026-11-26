# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""App-layer interim onboarding group gate (Wave 2, 2.4R / ADR-0033 decision 5).

The /api/onboard/* router carries a router-level dependency
(``require_onboard_access``) that 403s any caller who is not in the `integrator`
or `Admin` Cognito group, the active control while no Cedar policy is attached
to the onboarding gateway. Because it is a *router-level*
dependency, FastAPI runs it BEFORE any route handler, so a non-privileged caller
is rejected before any onboarding tool/handler executes.

Offline: exercises the dependency in isolation with a fake request, asserts the
real router has it wired, and checks the CognitoAuth.is_integrator semantics.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

_ADT_ROOT = Path(__file__).resolve().parents[2]
if str(_ADT_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADT_ROOT))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from agents.auth.cognito import CognitoAuth  # noqa: E402
from agents.onboard import require_onboard_access, router as onboard_router  # noqa: E402


def _request(*, is_admin=False, is_integrator=False):
    """Minimal stand-in for a FastAPI Request with the state the middleware sets."""
    return SimpleNamespace(state=SimpleNamespace(is_admin=is_admin, is_integrator=is_integrator))


# --------------------------------------------------------------------------- #
# require_onboard_access dependency
# --------------------------------------------------------------------------- #
def test_integrator_is_allowed():
    # integrator passes (returns None, no raise) — onboarding permitted.
    assert require_onboard_access(_request(is_integrator=True)) is None


def test_admin_is_allowed():
    assert require_onboard_access(_request(is_admin=True)) is None


def test_analyst_is_denied():
    # analyst / no group → 403 before any route handler runs.
    with pytest.raises(HTTPException) as exc:
        require_onboard_access(_request(is_admin=False, is_integrator=False))
    assert exc.value.status_code == 403
    assert "integrator" in exc.value.detail


def test_missing_state_defaults_to_denied():
    # If neither flag is present on state, fail closed (403).
    req = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        require_onboard_access(req)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# Router wiring — the gate applies to EVERY /api/onboard/* route
# --------------------------------------------------------------------------- #
def test_router_has_onboard_access_dependency():
    deps = [d.dependency for d in (onboard_router.dependencies or [])]
    assert require_onboard_access in deps, (
        "the onboarding router must carry require_onboard_access as a router-level "
        "dependency so it gates every /api/onboard/* route before the handler"
    )


def test_every_onboard_route_inherits_the_gate():
    # FastAPI merges router-level dependencies onto each route. Confirm the gate
    # is present on each route's resolved dependant (defense against a route
    # being added with dependency_overrides that drop it).
    assert onboard_router.routes, "router should expose onboard routes"
    for route in _walk_routes(onboard_router):
        route_deps = [d.dependency for d in getattr(route, "dependencies", [])]
        assert require_onboard_access in route_deps, (
            f"route {getattr(route, 'path', route)!r} is missing the onboarding gate"
        )


def test_walker_detects_an_ungated_sub_router():
    """Negative control for the walker itself.

    Without this, a green gate test proves nothing: the walker could silently
    yield zero sub-routes and pass trivially. Note that removing the EXPLICIT
    dependency from the real include does NOT make the gate test fail, because
    the parent onboard router's own dependency is still inherited through the
    include context — which is correct, and is why the control has to build an
    ungated parent instead.
    """
    from fastapi import APIRouter

    child = APIRouter(prefix="/child")

    @child.get("/thing")
    async def _thing():  # pragma: no cover - never called
        return {}

    ungated_parent = APIRouter(prefix="/parent")
    ungated_parent.include_router(child)

    walked = list(_walk_routes(ungated_parent))
    assert walked, "walker must descend into included sub-routers"
    for route in walked:
        deps = [d.dependency for d in getattr(route, "dependencies", [])]
        assert require_onboard_access not in deps


def test_walker_reaches_the_guided_sub_routes():
    """The blind spot this walker exists to close: the guided endpoints are real
    routes behind a lazy include wrapper."""
    paths = {str(getattr(r, "path", "")) for r in _walk_routes(onboard_router)}
    assert any("/guided/" in p for p in paths), paths


def _walk_routes(router):
    """Yield every real route, descending through INCLUDED sub-routers.

    FastAPI resolves ``include_router`` lazily: the parent holds an opaque
    wrapper whose own ``dependencies`` list is empty, with the real gate carried
    on the include context. Iterating only the top level therefore skipped
    sub-routers entirely — a blind spot that would let a future sub-router be the
    one unguarded corner of the onboarding surface. Descend instead, and treat
    the include context's dependencies as applying to everything beneath it.
    """
    for route in getattr(router, "routes", []):
        ctx = getattr(route, "include_context", None)
        if ctx is None:
            yield route
            continue
        inherited = list(getattr(ctx, "dependencies", []) or [])
        included = getattr(ctx, "included_router", None)
        for sub in _walk_routes(included) if included is not None else []:
            yield _WithDeps(sub, inherited)


class _WithDeps:
    """A route view whose dependencies include those inherited from the include."""

    def __init__(self, route, inherited):
        self._route = route
        self.dependencies = list(getattr(route, "dependencies", []) or []) + inherited
        self.path = getattr(route, "path", route)


# --------------------------------------------------------------------------- #
# CognitoAuth.is_integrator semantics
# --------------------------------------------------------------------------- #
def test_is_integrator_semantics():
    assert CognitoAuth.is_integrator(["integrator"]) is True
    assert CognitoAuth.is_integrator(["Admin"]) is True  # Admin supersedes
    assert CognitoAuth.is_integrator(["analyst"]) is False
    assert CognitoAuth.is_integrator([]) is False


_TESTS = [
    test_integrator_is_allowed,
    test_admin_is_allowed,
    test_analyst_is_denied,
    test_missing_state_defaults_to_denied,
    test_router_has_onboard_access_dependency,
    test_every_onboard_route_inherits_the_gate,
    test_is_integrator_semantics,
]


if __name__ == "__main__":
    for t in _TESTS:
        t()
        print(f"  ok  {t.__name__}")
    print(f"RESULT: PASS — {len(_TESTS)} onboard route-gate tests passed")
