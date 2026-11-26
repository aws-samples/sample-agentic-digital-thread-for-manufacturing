# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# In-process SFC (ADR-0082): SFC Core + OPC UA adapter + SiteWise and S3 Tables
# targets in one JVM. SFC publishes no container image, only release bundles.
# Build context is a cdk.out dir staged by stacks/sfc_stack.py (this file plus
# the generated sfc-config.json). Platform (arm64) comes from the CDK asset.

# ── Stage 1: fetch the pinned bundles and check their published SHA-512 ─────
FROM public.ecr.aws/docker/library/alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8 AS bundles

ARG SFC_VERSION=v1.11.0
WORKDIR /dl
RUN set -eu; \
    base="https://github.com/awslabs/industrial-shopfloor-connect/releases/download/${SFC_VERSION}"; \
    for b in sfc-main opcua aws-sitewise-target aws-s3-tables-target; do \
      wget -q -O "$b.tar.gz" "$base/$b.tar.gz"; \
    done; \
    printf '%s  %s\n' \
      68c36221b046ab5c3275d7c2c8ee3e65ff657592acb78bc8c20325de8c2d7b4f34f2f97f3207ff6c9f404fc693f701bd7001b28edc58f3d87d1f03867589316c sfc-main.tar.gz \
      17e02104b80e5b31bbcb1204cf33f5b0e5935d62ffebd153788b2fda6ce58d688090decc427f1fad235916de06b753a3a67dce0233847e02b2295bc2ed7e490d opcua.tar.gz \
      f10c5a3f661f8341d497bd1147617969b84bc9b332cb254e07330a7c373d1d3dc5ad9c8aa58478d97d0759f811580804e10be0424dca022437cb71f39e4105ec aws-sitewise-target.tar.gz \
      e6b70fe5483c93821ce1e16614bc63759bfc2f115356aa8bae473948e61a586529a350a6fd27d967a486ebd44e29fe2ed1adbadc14739c6aa99c89be5cdf9163 aws-s3-tables-target.tar.gz \
      | sha512sum -c -; \
    mkdir /sfc; \
    for b in sfc-main opcua aws-sitewise-target aws-s3-tables-target; do \
      tar -xzf "$b.tar.gz" -C /sfc; \
    done

# ── Stage 2: runtime ────────────────────────────────────────────────────────
FROM public.ecr.aws/amazoncorretto/amazoncorretto:21@sha256:d776b0a66110ca8f06944945f18d6b30d9fe96b9ded8c74078d016d8bf332ab0

# Pull Amazon Linux 2023 security patches newer than the pinned base digest
# (e.g. curl, openssl, krb5 CVEs). --releasever=latest moves past the release
# the base was built on. stacks/_os_patch.py forces this to re-run.
RUN dnf -y upgrade --security --releasever=latest && dnf clean all && rm -rf /var/cache/dnf

ARG SFC_VERSION=v1.11.0
LABEL org.opencontainers.image.title="AWS Shop Floor Connectivity (in-process)" \
      org.opencontainers.image.source="https://github.com/awslabs/industrial-shopfloor-connect" \
      org.opencontainers.image.version="${SFC_VERSION}"

WORKDIR /sfc

# Numeric uid:gid: the Corretto base has no useradd.
COPY --from=bundles --chown=10001:10001 /sfc/ /sfc/

# Resolves ${MODULES_DIR}/<component>/lib in the config's JarFiles.
ENV MODULES_DIR=/sfc

# Must match SFC_CONFIG_PATH in stacks/sfc_stack.py.
COPY --chown=10001:10001 sfc-config.json /etc/sfc/sfc-config.json

USER 10001:10001

# No HEALTHCHECK: SFC opens no port. Credentials come from the ECS task role.
# The JVM is called directly because bin/sfc-main needs xargs, which this base
# image lacks.
CMD ["java", "-cp", "/sfc/sfc-main/lib/*", "com.amazonaws.sfc.MainController", \
     "-config", "/etc/sfc/sfc-config.json"]
