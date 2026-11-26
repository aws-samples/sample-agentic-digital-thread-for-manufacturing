# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The PostgreSQL client module has one copy per separately packaged
component, and every copy is the same file as the app's."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "agentic_digital_thread" / "agents" / "pgdriver.py"
COPIES = (
    ROOT / "erp_simulation" / "pgdriver.py",
    ROOT / "lambda-db-init" / "pgdriver.py",
    ROOT / "lambda-layers" / "pg8000" / "pgdriver.py",
)


def test_every_copy_matches_the_app_module():
    canonical = CANONICAL.read_bytes()
    for copy in COPIES:
        assert copy.read_bytes() == canonical, f"{copy.relative_to(ROOT)} differs from {CANONICAL.relative_to(ROOT)}"


def test_no_other_copy_exists():
    found = {p for p in ROOT.rglob("pgdriver.py")
             if not any(part in {".git", "node_modules", "cdk.out", ".venv"} for part in p.parts)}
    assert found == {CANONICAL, *COPIES}
