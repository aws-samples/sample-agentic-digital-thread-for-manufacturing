# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline guard for the SiteWise (Direct) connection-host fix (BUG #2).

The connection URL must use the REAL, resolvable AWS service endpoint host
``iotsitewise.<region>.amazonaws.com`` — NOT the bare ``<region>.amazonaws.com``,
which does not resolve in DNS and made the SSRF/scheme guard reject the target
(``host did not resolve``). This is the same class of fix already applied to the
Athena probe (``athena.<region>.amazonaws.com``).

Pure string assembly — no AWS, no network — so it runs in the default offline
lane. Synthetic regions only.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from agents.connection_probes import sitewise_connection_url


def test_host_is_service_endpoint_not_bare_region():
    url = sitewise_connection_url("eu-west-1")
    parts = urlsplit(url)
    assert parts.scheme == "iotsitewise"
    # The host is the real service endpoint, NOT the bare region host.
    assert parts.hostname == "iotsitewise.eu-west-1.amazonaws.com"
    assert parts.hostname != "eu-west-1.amazonaws.com"


def test_host_has_at_least_three_dotted_labels_before_amazonaws():
    """A resolvable AWS service endpoint carries a service label ahead of the
    region (service.region.amazonaws.com) — the bare-region bug had only the
    region label."""
    host = urlsplit(sitewise_connection_url("ap-southeast-2")).hostname
    assert host.startswith("iotsitewise.")
    assert host.endswith(".amazonaws.com")
    # service + region + amazonaws + com = 4 labels
    assert len(host.split(".")) == 4


def test_empty_region_falls_back_to_us_east_1():
    assert sitewise_connection_url("") == "iotsitewise://iotsitewise.us-east-1.amazonaws.com"
    assert sitewise_connection_url(None) == "iotsitewise://iotsitewise.us-east-1.amazonaws.com"


def test_region_is_stripped():
    assert sitewise_connection_url("  us-west-2  ") == "iotsitewise://iotsitewise.us-west-2.amazonaws.com"
