# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Log format strings never put a secret-like word before a ``%s``.

Semgrep's ``python-logger-credential-disclosure`` rule flags a logger call
whose format string matches ``(?i).*(api.key|secret|credential|token|password).*%s.*``:
a word such as "secret" followed by a placeholder reads like the value of that
secret is about to be logged. None of these lines logs a value (they log a
system id, a secret name or an exception class), so the messages are worded
with the placeholder first and the noun after it. This test keeps it that way
for every shipped Python file, reading the whole format string (semgrep checks
only the first literal piece of an implicitly concatenated string, so this is
stricter than the scanner).

``ALLOWED`` holds the one call kept with a reasoned ``nosemgrep`` comment:
the ``REGISTRATION_REMOVED`` event line, a ``key=value`` record whose
``secret_scheduled`` key a test and log readers match on.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_OBJ = re.compile(r"(?i)(_logger|logger|self.logger|log)")
_CALL = re.compile(r"(debug|info|warn|warning|error|exception|critical)")
_FMT = re.compile(r"(?i).*(api.key|secret|credential|token|password).*\%s.*")
ALLOWED = {
    ("agentic_digital_thread/agents/onboard.py", "REGISTRATION_REMOVED system=%s"),
}


def _shipped_python() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout.split()
    return [f for f in out
            if "/tests/" not in f and not f.startswith("tests/")
            and not f.rsplit("/", 1)[-1].startswith("test_")
            and "node_modules/" not in f]


def _matches(path: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
            continue
        if not _OBJ.match(ast.unparse(node.func.value)) or not _CALL.fullmatch(node.func.attr):
            continue
        fmt = node.args[0]
        if isinstance(fmt, ast.Constant) and isinstance(fmt.value, str) and _FMT.match(fmt.value):
            hits.append((node.lineno, fmt.value))
    return hits


def test_no_log_format_string_puts_a_secret_word_before_a_placeholder():
    found = []
    for path in _shipped_python():
        for line, fmt in _matches(path):
            if any(path == p and fmt.startswith(prefix) for p, prefix in ALLOWED):
                continue
            found.append(f"{path}:{line}: {fmt!r}")
    assert not found, "reword so the placeholder comes before the noun:\n" + "\n".join(found)


def test_every_kept_line_is_still_there():
    """Each allow-list entry is real, so a later rewrite of that line drops it here too."""
    for path, prefix in ALLOWED:
        assert any(fmt.startswith(prefix) for _, fmt in _matches(path)), (path, prefix)
