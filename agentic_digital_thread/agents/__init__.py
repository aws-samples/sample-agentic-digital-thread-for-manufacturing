# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Before any module of the package creates an AWS client: every botocore
# session runs the lease write check (agents/lease_guard.py).
from .lease_guard import install as _install_lease_guard

_install_lease_guard()
