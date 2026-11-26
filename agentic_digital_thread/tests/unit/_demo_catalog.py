# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Local, test-only demo-system fixtures.

Base mode ships an EMPTY live catalog (ADR-0030) and the former dormant demo-seed
machinery has been DELETED entirely from production ``catalog.py`` (ADR-0035,
query-ownership redesign). Several offline resolver/status tests still need a
handful of demo systems present in the shared catalog to exercise schema/authority
lookups BY NAME. Rather than reintroduce demo data into production ``catalog.py``,
those identities live here — in the test tree — as a minimal fixture
(id/name/plant/system_type/connection_type/schema/status; no query_guidance, which
the resolvers do not read).

DRY: the SIX demo SQL systems come from the REAL demo-seam manifest
(``agents.demo.systems.DEMO_SYSTEMS``) — the SAME single source the server startup
registration hook uses — so the fixture can never drift from the systems the deploy
actually registers. The test-only extras (a second synonym CMMS + the OT systems)
that resolver/status tests additionally rely on are appended here; they are NOT part
of the SQL registration manifest. Every entry carries ``status='registered'`` to
match the fixture's historical shape.

Kept intentionally small: only the fields the resolver/status tests consume.
"""
from __future__ import annotations

from agents.demo.systems import DEMO_SYSTEMS as _SQL_DEMO_SYSTEMS

# The six SQL systems from the real manifest, tagged with the fixture's
# ``status='registered'`` (the manifest itself is status-free — status is a live
# catalog concern, not a manifest identity).
_SQL_FIXTURE: list[dict] = [{**s, "status": "registered"} for s in _SQL_DEMO_SYSTEMS]

# Test-only extras NOT in the SQL registration manifest: a second CMMS (synonyms
# coverage) and the OT systems (SiteWise Direct + HighByte MCP) that some
# resolver/status tests look up by name.
_EXTRA_FIXTURE: list[dict] = [
    {"id": "cmms2", "name": "CMMS (Synonyms)", "plant": "All Sites", "system_type": "CMMS",
     "connection_type": "PostgreSQL", "schema": "cmms2_maintenance", "status": "registered"},
    {"id": "sitewise-direct", "name": "AWS IoT SiteWise (Direct)", "plant": "All Sites",
     "system_type": "Historian", "connection_type": "SiteWise (Direct)", "schema": "",
     "status": "registered"},
    {"id": "highbyte", "name": "HighByte Intelligence Hub", "plant": "All Sites",
     "system_type": "IIoT", "connection_type": "MCP Server", "schema": "", "status": "registered"},
]

DEMO_SYSTEMS: list[dict] = _SQL_FIXTURE + _EXTRA_FIXTURE
