# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guard: no corporate single-sign-on overlay leaks into this public sample.

The external OIDC identity-provider option (ADR-0072) is deliberately
VENDOR-NEUTRAL: it is configured with an issuer URL, a client id, a secret ARN
and a display name, and the repo's examples use ``https://login.example.com``
/ ``CorporateIdP``. Any deployment can plug in its own corporate SSO — but the
name of a specific corporate SSO system, internal hostnames, or an internal
operational switch must never be committed here: git history is permanent and
this repository is published.

The scan walks every git-tracked text file. The needles are assembled from
parts / base64 at runtime so this file cannot trip itself and a plain grep of
the checkout for the assembled words finds nothing but the offenders.

Run:
  python3 -m pytest tests/test_no_internal_identity_strings.py -q
"""
from __future__ import annotations

import base64
import re
import subprocess  # nosec B404 - fixed internal argv list (git ls-files), no shell
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()

#: Case-insensitive plain substrings, assembled so the literal never appears
#: in the source tree.
_SUBSTRING_NEEDLES = (
    "mid" + "way",
    "kill" + "switch",
    base64.b64decode("YTJ6LmNvbQ==").decode(),  # <company-short>.com internal domain
)
#: Internal hostnames under the corporate domain: ``<label>.<company>.com``.
#: The public docs host (``…aws.<company>.com``) and bare ``@<company>.com``
#: contact addresses are legitimate and excluded.
_COMPANY = base64.b64decode("YW1hem9u").decode()
_HOSTNAME_RE = re.compile(
    rf"(?<![\w@.-])(?!aws\.)([a-z0-9-]+\.)+{_COMPANY}\.com\b", re.IGNORECASE
)
_PUBLIC_HOST_RE = re.compile(rf"\baws\.{_COMPANY}\.com\b", re.IGNORECASE)

#: Per-file frozen exceptions. Intentionally EMPTY: the last pre-existing
#: mentions (ADR-0070 and its detail doc) were reworded in the 2026-09 public
#: share review. Do not add entries for new content — reword instead.
_PREEXISTING_ALLOWLIST: dict[str, set[str]] = {}

_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".gz", ".zip",
    ".woff", ".woff2", ".ttf", ".eot", ".lock", ".parquet", ".db", ".sqlite",
}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout
    files = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        path = REPO_ROOT / raw.decode()
        if path.suffix.lower() in _SKIP_SUFFIXES or not path.is_file():
            continue
        if path.resolve() == THIS_FILE:
            continue
        files.append(path)
    return files


def _scan(text: str) -> set[str]:
    lowered = text.lower()
    hits = {needle for needle in _SUBSTRING_NEEDLES if needle in lowered}
    for match in _HOSTNAME_RE.finditer(text):
        host = match.group(0)
        if not _PUBLIC_HOST_RE.search(host):
            hits.add(host.lower())
    return hits


def test_no_internal_identity_strings_in_tracked_files():
    offenders: list[str] = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary — nothing to scan
        hits = _scan(text)
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits -= _PREEXISTING_ALLOWLIST.get(rel, set())
        if hits:
            offenders.append(f"{rel}: {sorted(hits)}")
    assert offenders == [], (
        "Internal corporate-SSO identifiers must not appear in this public sample "
        "(use vendor-neutral examples such as https://login.example.com / "
        "CorporateIdP):\n  " + "\n  ".join(offenders)
    )


def test_allowlisted_files_still_carry_only_the_allowed_needle():
    """The allowlist is a frozen exception, not a licence to grow."""
    for rel, allowed in _PREEXISTING_ALLOWLIST.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        hits = _scan(text)
        assert hits <= allowed, (rel, hits - allowed)


def test_scanner_detects_each_needle_class():
    """Self-check: the assembled needles and the hostname pattern do fire."""
    assert _scan("x " + "Mid" + "Way" + " y") == {"mid" + "way"}
    assert _scan("KILL" + "SWITCH") == {"kill" + "switch"}
    assert _scan("https://portal." + _COMPANY + ".com/x") == {"portal." + _COMPANY + ".com"}
    # Public docs host and contact addresses are fine.
    assert _scan("see https://docs.aws." + _COMPANY + ".com/cognito") == set()
    assert _scan("mail opensource-team@" + _COMPANY + ".com") == set()
    assert _scan("Copyright " + _COMPANY.title() + ".com, Inc.") == set()
