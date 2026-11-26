# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SFC stack custom resource: the SiteWise models + asset tree SFC writes into.

ADR-0082 decision 9. SFC's task role cannot create anything, and the HighByte
pipeline that creates assets where it runs is off in the unattended workshop
profile (``highbyte_sitewise_pipeline=disabled``) and absent without the hub,
so this runs at deploy time as part of ``ADTSfcStack``, in every deployment
path, including a Workshop Studio account that never runs deploy.sh:

1. the 28 asset models (``DeploySitewiseModels.deploy_models``, the same code
   deploy.sh step 1a runs; idempotent), then
2. the Enterprise/Site/Area/Line/process tree (``sitewise_tree.ensure_tree``),
   whose specs the stack computes at synth and passes as the ``Specs``
   property, so this Lambda carries no tag map or generator.

Both files sit next to this handler (vendored by ``stacks/sfc_stack.py`` with
``stage_tree``). Create and Update build or reuse; Delete does nothing, because
the assets belong to the account's SiteWise estate (``cleanup.sh`` removes
them, as it does the models).
"""
from __future__ import annotations

import json

import boto3
from botocore.config import Config

import DeploySitewiseModels
import sitewise_tree

PHYSICAL_ID = "adt-sfc-sitewise-tree"


def handler(event, _context):
    request = event.get("RequestType")
    if request == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", PHYSICAL_ID)}
    specs = json.loads(event["ResourceProperties"]["Specs"])
    client = boto3.client(
        "iotsitewise", config=Config(retries={"mode": "adaptive", "max_attempts": 10}))
    DeploySitewiseModels.deploy_models(client)
    stats = sitewise_tree.ensure_tree(client, specs)
    print(f"SiteWise asset tree: {len(specs)} assets — {stats}")
    return {"PhysicalResourceId": PHYSICAL_ID,
            "Data": {k: str(v) for k, v in stats.items()}}
