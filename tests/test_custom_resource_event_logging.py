# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Custom-resource handlers never log the whole CloudFormation event.

The db-init handler and the shared demo-user handler used to log
``json.dumps(event)``. A CloudFormation custom-resource event carries its
ResourceProperties (the demo PasswordTrigger, the demo secret name), so the
full dump put those into CloudWatch. Both now log only control fields
(RequestType, action, username). These guards fail on the old full-event dump.
"""
from __future__ import annotations

from pathlib import Path

from stacks._edge_auth import _DEMO_USER_HANDLER

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_db_init_handler_does_not_dump_the_whole_event():
    source = (REPO_ROOT / "lambda-db-init" / "handler.py").read_text()
    assert "json.dumps(event)" not in source
    # The control-field log line is the intended replacement.
    assert "RequestType=%s" in source


def test_demo_user_handler_does_not_dump_the_whole_event():
    assert "json.dumps(event)" not in _DEMO_USER_HANDLER
    # It logs the request type and username only.
    assert "RequestType:" in _DEMO_USER_HANDLER
