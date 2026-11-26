# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run under ``opentelemetry-instrument python`` in the runtime environment.

Prints one JSON line describing what auto-instrumentation set up, after
importing the runtime entry module and the orchestrator module the way the
image's CMD would. Not a test module (no ``test_`` prefix); driven by
``test_instrumented_boot.py`` and runnable by hand inside the built image.
"""

import gc
import json
from importlib.metadata import entry_points, version


def _instrumentor_report() -> dict:
    """For every registered auto-instrumentor: is it active, and does the
    library it instruments sit at a version it does not support? A conflict
    whose library is simply not installed (django, flask, ...) is expected."""
    from opentelemetry.instrumentation.dependencies import get_dependency_conflicts

    from opentelemetry.instrumentation.dependencies import get_dist_dependency_conflicts

    active, version_conflicts, load_errors = [], [], []
    for ep in entry_points(group="opentelemetry_instrumentor"):
        # The same pre-check opentelemetry-instrument makes before loading.
        conflict = get_dist_dependency_conflicts(ep.dist) if ep.dist else None
        if conflict is not None:
            if conflict.found or conflict.found_any:
                version_conflicts.append(f"{ep.name}: {conflict}")
            continue  # library not installed: the loader skips it too
        try:
            instrumentor = ep.load()()
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            load_errors.append(f"{ep.name}: {type(exc).__name__}: {exc}")
            continue
        if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
            active.append(ep.name)
        conflict = get_dependency_conflicts(instrumentor.instrumentation_dependencies())
        if conflict is not None and (conflict.found or conflict.found_any):
            version_conflicts.append(f"{ep.name}: {conflict}")
    return {"active": sorted(active), "version_conflicts": version_conflicts,
            "load_errors": load_errors}


def main() -> None:
    from opentelemetry import trace

    import agents.runtime_app  # noqa: F401 - the image CMD's module
    import agents.orchestrator  # noqa: F401 - what orchestrator mode builds

    from amazon.opentelemetry.distro.exporter.otlp.aws.common.aws_auth_session import AwsAuthSession
    from amazon.opentelemetry.distro.exporter.otlp.aws.traces.otlp_aws_span_exporter import (
        OTLPAwsSpanExporter,
    )

    span_exporters = [o for o in gc.get_objects() if isinstance(o, OTLPAwsSpanExporter)]
    auth_sessions = [o for o in gc.get_objects() if isinstance(o, AwsAuthSession)]
    print(json.dumps({
        "adot": version("aws-opentelemetry-distro"),
        "otel_sdk": version("opentelemetry-sdk"),
        "tracer_provider": type(trace.get_tracer_provider()).__name__,
        "aws_span_exporters": [getattr(e, "_endpoint", "") for e in span_exporters],
        "auth_session_services": sorted({getattr(s, "_service", "") for s in auth_sessions}),
        "instrumentors": _instrumentor_report(),
    }))


if __name__ == "__main__":
    main()
