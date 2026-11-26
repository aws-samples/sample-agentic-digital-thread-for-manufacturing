# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# ── Stage 1: build the Vite SPA (frontend/dist) ──────────────────────────────
# --platform is deliberate: the image targets ECS Fargate on Graviton (arm64)
# and must build arm64 even from amd64 hosts. Digest-pinned for reproducibility.
# nosemgrep: avoid-platform-with-from
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: python app ───────────────────────────────────────────────────────
# --platform is deliberate (Fargate Graviton/arm64); digest-pinned for reproducibility
# (the index the 3.12-slim tag served on 2026-10-12, same digest as Dockerfile.runtime).
# nosemgrep: avoid-platform-with-from
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# IMAGE_TYPE: adt-app  ← ADT dashboard/API app. Push ONLY to the ADT app ECS image, NEVER to the system-agent runtime ECR repo (adt-system-agent).
LABEL image.type="adt-app"

WORKDIR /app

COPY pyproject.toml .
COPY requirements/app-py312-linux-arm64.lock /tmp/app-requirements.lock
COPY branding.json .
COPY agents/ agents/
COPY static/ static/
COPY --from=frontend /fe/dist frontend/dist

# Install curl for HEALTHCHECK and create non-root user (CKV_DOCKER_3).
# apt-get upgrade pulls OS security patches (e.g. openssl, util-linux CVEs)
# that postdate the digest-pinned base image.
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    python -m pip install --no-cache-dir --require-hashes \
        -r /tmp/app-requirements.lock && \
    python -m pip install --no-cache-dir --no-deps . && \
    useradd -m -u 1000 adtapp

# Native "S3 Tables" connector (DuckDB): onboarding discover runs in THIS
# image and opens the connector, which never downloads extension code at
# runtime. Bake the same extensions the system-agent runtime image bakes.
ENV S3T_DUCKDB_EXTENSION_DIR=/opt/duckdb/extensions
RUN mkdir -p /opt/duckdb/extensions \
    && python -c "import duckdb; c = duckdb.connect(config={'extension_directory': '/opt/duckdb/extensions'}); [c.execute(f'INSTALL {e}') for e in ('aws', 'httpfs', 'avro', 'iceberg')]" \
    && chmod -R a+rX /opt/duckdb

COPY data/ data/

RUN chown -R adtapp:adtapp /app
USER adtapp

# App version (git-derived). deploy.sh computes it and adt_compute_stack
# delivers it as a CONTAINER ENV VAR at task start (not a build-arg: a build-arg
# is part of the CDK asset hash, so the daily date rebuilt and pushed the image
# on every deploy). The ARG stays for local `docker build --build-arg` use;
# agents/server.py serves the value on /health + /api/version (the UI version
# badge); builds without either get "dev".
ARG APP_VERSION
ENV APP_VERSION=${APP_VERSION:-dev}

EXPOSE 8000

# Health check — verify uvicorn is serving (CKV_DOCKER_2). JSON notation so
# signals pass through; the shell fallback chain runs under an explicit sh -c.
# /health is the app's public, trivially non-blocking liveness route
# (agents/server.py; PUBLIC_ROUTES in agents/auth/middleware.py) — the same
# path the ALB target group probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["sh", "-c", "curl -fsS http://localhost:8000/health || exit 1"]

# Keep-alive MUST outlive the ALB idle timeout (stacks/adt_compute_stack.py,
# 120 s): with uvicorn's 5 s default the ALB reused connections the app had
# just closed and answered 502 (intermittent, live 2026-09-27). Pinned by
# tests/test_uvicorn_keepalive.py.
CMD ["uvicorn", "agents.server:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "125"]
