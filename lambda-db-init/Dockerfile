# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Database Init Lambda Container
# Installs the exact Linux ARM64 pg8000 wheel closure from a checked-in lock.
#
# CONSOLIDATED: All seed data is now inline in handler.py.
# No external SQL files needed — single code path for reliability.
# rebuild: 2026-05-10-fix-released-status
# rebuild: 2026-05-10-increase-lot-quantities
#
# checkov:skip=CKV_DOCKER_2: AWS Lambda manages container health checks internally
# checkov:skip=CKV_DOCKER_3: AWS Lambda runs containers as a managed sandbox user (sbx_user1051)

# Digest-pinned (manifest list) for reproducible builds: the list the 3.11 tag served on 2026-10-12.
FROM public.ecr.aws/lambda/python:3.11@sha256:67d572c2eb30da5fdaa720bcdc56460cb4d1a54ef8c980c70eafc5f799cb1e96

# Install the exact AL2 ARM64 wheel closure from the image context. The lock is
# generated for aarch64-manylinux2014; hashes and binary-only mode prevent an
# unreviewed source build or registry-selected transitive from entering the image.
COPY requirements-py311-linux-arm64.lock ${LAMBDA_TASK_ROOT}/requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: \
    -r ${LAMBDA_TASK_ROOT}/requirements.lock

# Copy Lambda handler (contains all schema + seed data inline) and the
# PostgreSQL client module it imports (pgdriver.py, over pg8000).
COPY handler.py pgdriver.py ${LAMBDA_TASK_ROOT}/

# Set the Lambda handler
# nosemgrep: missing-user - AWS Lambda base image manages user (sbx_user1051)
CMD ["handler.handler"]
