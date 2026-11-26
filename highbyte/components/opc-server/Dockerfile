# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Platform is selected by the CDK image asset (linux/arm64); FROM stays arch-neutral
# Digest-pinned (manifest list) for reproducible builds.
#
# BUILD CONTEXT is highbyte/ (not this directory): the CDK stack passes
# file="components/opc-server/Dockerfile" so the generated datasets in
# highbyte/datasets/ are reachable WITHOUT the stack copying them into the
# source tree at synth (the previous 383 MB copytree). highbyte/.dockerignore
# allowlists exactly what this file COPYs.
FROM public.ecr.aws/docker/library/node:alpine@sha256:233761595746769ebfdb6090f44fc7cdf818ae0ce62d2b37e0367723b9823e36

# Pull Alpine security patches (e.g. openssl CVEs) newer than the pinned base digest.
RUN apk upgrade --no-cache

WORKDIR /home/node/opcua-server
EXPOSE 26543

COPY components/opc-server/ /home/node/opcua-server/
# Per-site datasets, gzip-compressed by highbyte/generate_datasets.py and
# renamed here to the SITE_NAME the container receives (server.sh gunzips the
# selected one at start).
COPY datasets/site1_milwaukee_bottling.csv.gz /app/datasets/Milwaukee.csv.gz
COPY datasets/site2_detroit_automotive.csv.gz /app/datasets/Detroit.csv.gz
COPY datasets/site3_cedarrapids_food.csv.gz   /app/datasets/CedarRapids.csv.gz
COPY datasets/site4_indianapolis_machining.csv.gz /app/datasets/Indianapolis.csv.gz

RUN npm ci --omit=dev

RUN chown -R node:node /home/node/opcua-server
RUN chmod +x ./server.sh
USER node

# Health check — verify OPC UA server is listening on the configured port.
# JSON notation so signals pass through (prefer-json-notation).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["sh", "-c", "nc -z localhost 26543 || exit 1"]

ENTRYPOINT ["./server.sh"]
