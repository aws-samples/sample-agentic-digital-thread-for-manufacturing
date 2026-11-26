# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Branding configuration — SSM overrides env vars, which override branding.json, which overrides defaults."""

import json
import os
from pathlib import Path

_DEFAULTS = {
    "company": "Agentic Digital Thread",
    "tagline": "Manufacturing Intelligence",
    "plants": {"indianapolis": "Indianapolis", "detroit": "Detroit"},
    "systems": {},
}

_config_path = Path(__file__).resolve().parent.parent / "branding.json"
_cfg = {}
if _config_path.exists():
    with open(_config_path, encoding="utf-8") as f:
        _cfg = json.load(f)

# Read SSM overrides (persisted from Admin tab, survive container redeployments)
_ssm_company = ""
_ssm_tagline = ""
try:
    import boto3 as _boto3
    _region = os.environ.get("AWS_REGION", "us-east-1")
    _ssm = _boto3.client("ssm", region_name=_region)
    try:
        _ssm_company = _ssm.get_parameter(Name="/adt/branding-company")["Parameter"]["Value"]
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
    try:
        _ssm_tagline = _ssm.get_parameter(Name="/adt/branding-tagline")["Parameter"]["Value"]
    except Exception:  # nosec B110 - intentional fallback, no recovery action needed
        pass
except Exception:  # nosec B110 - intentional fallback, no recovery action needed
    pass

# Priority: BRAND_* env var → SSM → branding.json → defaults
COMPANY = os.environ.get("BRAND_COMPANY") or _ssm_company or _cfg.get("company") or _DEFAULTS["company"]
TAGLINE = os.environ.get("BRAND_TAGLINE") or _ssm_tagline or _cfg.get("tagline") or _DEFAULTS["tagline"]
PLANTS = {**_DEFAULTS["plants"], **_cfg.get("plants", {})}
SYSTEMS = _cfg.get("systems", _DEFAULTS["systems"])


def as_dict() -> dict:
    """Return full branding config for the /api/branding endpoint."""
    return {"company": COMPANY, "tagline": TAGLINE, "plants": PLANTS, "systems": SYSTEMS}
