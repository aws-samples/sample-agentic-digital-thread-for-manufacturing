# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""HighByte documentation: guide links exist, and the first-deploy docs ask
for the explicit EULA acceptance.

HighByte's user guide lives under ``/release/``: the older
``guide.highbyte.com/setup/...`` paths return 404 while the ``/release/setup/...``
ones return 200 (checked with a GET, 2026-10-12).

The hub runs only with ``ACCEPT_EULA=Y``, which agrees to the HighByte EULA, so
the first HighByte deploy needs ``--accept-highbyte-eula`` and a raw CDK synth
needs ``-c highbyte_accept_eula=true``. The README, the deploy-mode table and
the ``app.py`` HighByte comment are where a first deploy starts, so they must
say that rather than describe the acceptance as implicit.

Offline: reads tracked text files only, no network.
"""
from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed argv (git ls-files), no shell
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_GUIDE_LINK = re.compile(r"https?://guide\.highbyte\.com/[^\s)\"'<>`\]]*")
_RELEASE_PREFIX = "https://guide.highbyte.com/release/"
_EULA_URL = "https://www.highbyte.com/hubfs/highbyte_intelligence_hub_eula.pdf"
_ACCEPT_FLAG = "--accept-highbyte-eula"
_FIRST_DEPLOY_DOCS = ("README.md", "docs/deployment.md")


def _tracked_text_files():
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,  # nosec B603 B607
                             capture_output=True, check=True).stdout
    for rel in listing.decode().split("\0"):
        if not rel:
            continue
        path = REPO_ROOT / rel
        try:
            yield rel, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue


def test_every_highbyte_guide_link_is_a_release_path():
    links = [(rel, match.group(0)) for rel, text in _tracked_text_files()
             for match in _GUIDE_LINK.finditer(text)]
    assert links, "expected at least one HighByte guide link (deployment docs, stack comments)"
    stale = [f"{rel}: {url}" for rel, url in links if not url.startswith(_RELEASE_PREFIX)]
    assert stale == [], "HighByte guide links outside /release/ return 404:\n" + "\n".join(stale)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _sentences(text: str) -> list[str]:
    """Whitespace-normalized sentences (split on '. ' and ';'), good enough
    to keep a claim and its condition together for the checks below."""
    flat = " ".join(text.split())
    return [s for s in re.split(r"(?<=\.)\s+|;\s*", flat) if s]


def test_the_readme_links_the_eula_and_names_the_first_deploy_flags():
    readme = " ".join(_read("README.md").split())
    assert _EULA_URL in readme
    assert f"--highbyte {_ACCEPT_FLAG}" in readme


def test_no_readme_sentence_about_accept_eula_leaves_out_the_acceptance_flag():
    """A README sentence that mentions ACCEPT_EULA without the acceptance flag
    describes the acceptance as something a plain --highbyte deploy does."""
    implicit = [s for s in _sentences(_read("README.md"))
                if "ACCEPT_EULA" in s and _ACCEPT_FLAG not in s]
    assert implicit == [], "README describes an implicit EULA acceptance:\n" + "\n".join(implicit)


def test_every_highbyte_deploy_command_in_the_first_deploy_docs_accepts_the_eula():
    commands = [(rel, line.strip()) for rel in _FIRST_DEPLOY_DOCS
                for line in _read(rel).splitlines()
                if "./deploy.sh" in line and "--highbyte" in line]
    assert commands, "expected HighByte deploy commands in the first-deploy docs"
    missing = [f"{rel}: {line}" for rel, line in commands if _ACCEPT_FLAG not in line]
    assert missing == [], f"HighByte deploy commands without {_ACCEPT_FLAG}:\n" + "\n".join(missing)


def test_the_deploy_mode_table_highbyte_row_accepts_the_eula():
    rows = [line for line in _read("docs/deployment.md").splitlines()
            if line.startswith("| **Full + HighByte**")]
    assert len(rows) == 1, rows
    command = rows[0].split("|")[2]
    assert "--highbyte" in command and _ACCEPT_FLAG in command, command


def _app_highbyte_comment() -> str:
    lines = _read("app.py").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# HighByte (OT/UNS layer)"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("# ----"))
    return " ".join(" ".join(lines[start:end]).replace("#", " ").split())


def test_the_app_highbyte_comment_names_both_cdk_context_values():
    comment = _app_highbyte_comment()
    assert "-c highbyte_enabled=true" in comment
    assert "-c highbyte_accept_eula=true" in comment
    assert _ACCEPT_FLAG in comment
    assert "accepted at runtime" not in comment
