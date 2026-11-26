# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S0.2 cloud integration harness — reproducibility orchestrator + safety guards.

This package makes the real cloud deployment reproducible and safe to operate
against. Every destructive operation in the harness MUST first call the guards
in :mod:`tests.harness._guard` so it can only ever touch the known dev account
(111122223333 / us-east-1) and a real onboarded graph, and only when the
operator has explicitly opted in.
"""
