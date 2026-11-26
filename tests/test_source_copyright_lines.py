# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guard: every tracked source file carries the copyright line.

This repository is published as a sample, so each source file opens with the
two-line notice: the copyright line, then the SPDX identifier. A file that
carries only the SPDX line, or neither, ships without its copyright notice.

A file passes when one of its first 15 lines is a comment, in that file type's
own comment syntax, that starts with the full copyright line. The 15 lines
leave room for a shebang or a doctype above the notice. Mentioning the company
in prose or in a string does not count, and neither does a truncated notice.
An i3X Explorer file (``i3x-explorer/``, adapted from CESMII's project) passes
with CESMII's exact notice followed by the ``Modifications Copyright`` line.

Data, configuration, vendored, generated and empty files are out of scope. A
package ``__init__.py`` is in scope once it holds any code; an empty one
passes as an empty file.

Run:
  python3 -m pytest tests/test_source_copyright_lines.py -q
"""
from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed internal argv list (git ls-files), no shell
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

COPYRIGHT_LINE = "Amazon.com, Inc. or its affiliates"
NOTICE = "Copyright " + COPYRIGHT_LINE + ". All Rights Reserved."
MODIFICATIONS_NOTICE = "Modifications Copyright " + COPYRIGHT_LINE + "."
#: The i3X Explorer is adapted from CESMII's project; its files carry this
#: upstream notice and then MODIFICATIONS_NOTICE (see NOTICE).
I3X_UPSTREAM_NOTICE = "Copyright (c) 2024 CESMII - The Smart Manufacturing Institute"
I3X_PREFIX = "i3x-explorer/"
SPDX = "SPDX-License-Identifier: MIT-0"
HEAD_LINES = 15

#: Comment syntax per file type: line-comment markers, block-comment pairs.
_HASH = (("#",), ())
_C_LIKE = (("//",), (("/*", "*/"),))
_STYLES = {
    ".py": _HASH, ".sh": _HASH,
    ".ts": _C_LIKE, ".tsx": _C_LIKE, ".js": _C_LIKE, ".mjs": _C_LIKE,
    ".cjs": _C_LIKE, ".java": _C_LIKE, ".go": _C_LIKE,
    ".sql": (("--",), (("/*", "*/"),)),
    ".css": ((), (("/*", "*/"),)),
    ".html": ((), (("<!--", "-->"),)),
}
SOURCE_SUFFIXES = tuple(_STYLES)
#: Vendored, generated and build output, minified bundles and type declarations.
_OUT_OF_SCOPE = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|cdk\.out|\.venv)/"
    r"|\.min\.(js|css)$|\.d\.ts$"
)


def _is_dockerfile(rel: str) -> bool:
    return rel.rsplit("/", 1)[-1].startswith("Dockerfile")


def in_scope(rel: str) -> bool:
    if _OUT_OF_SCOPE.search(rel):
        return False
    return rel.endswith(SOURCE_SUFFIXES) or _is_dockerfile(rel)


def _style(rel: str):
    if _is_dockerfile(rel):
        return _HASH
    return _STYLES.get(Path(rel).suffix, _HASH)


def _comment_texts(text: str, rel: str) -> list[str]:
    """The text of each comment line among the first lines, markers removed."""
    line_markers, blocks = _style(rel)
    texts: list[str] = []
    open_block = None  # closing marker of the block comment we are inside
    for raw in text.splitlines()[:HEAD_LINES]:
        line = raw.strip()
        if open_block is not None:
            body = line
            if open_block in body:
                body = body.split(open_block, 1)[0]
                open_block = None
            texts.append(body.lstrip("*").strip())
            continue
        marker = next((m for m in line_markers if line.startswith(m)), None)
        if marker is not None:
            texts.append(line[len(marker):].strip())
            continue
        for start, end in blocks:
            if line.startswith(start):
                body = line[len(start):]
                if end in body:
                    body = body.split(end, 1)[0]
                else:
                    open_block = end
                texts.append(body.lstrip("*").strip())
                break
    return texts


def lacks_copyright_line(text: str, rel: str = "module.py") -> bool:
    """True when a non-empty file has no copyright comment in its first lines."""
    if not text.strip():
        return False
    texts = _comment_texts(text, rel)
    if any(t.startswith(NOTICE) for t in texts):
        return False
    if rel.startswith(I3X_PREFIX):
        for i, t in enumerate(texts):
            upstream = any(u.startswith(I3X_UPSTREAM_NOTICE) for u in texts[:i])
            if t.startswith(MODIFICATIONS_NOTICE) and upstream:
                return False
    return True


def suggested_header(rel: str) -> str:
    """The two notice lines in the file type's own comment syntax."""
    line_markers, blocks = _style(rel)
    if line_markers:
        m = line_markers[0]
        return f"{m} {NOTICE}\n{m} {SPDX}"
    start, end = blocks[0]
    if start == "<!--":
        return f"{start} {NOTICE} {SPDX} {end}"
    return f"{start} {NOTICE}\n   {SPDX} {end}"


def _tracked_files() -> list[str]:
    out = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout
    return [raw.decode() for raw in out.split(b"\0") if raw]


def test_every_source_file_carries_the_copyright_line():
    missing = []
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if not in_scope(rel) or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if lacks_copyright_line(text, rel):
            missing.append(rel)
    assert missing == [], (
        "These source files lack the copyright line. Add the two notice lines "
        "at the top (after any shebang or doctype):\n" + "\n".join(
            f"  {rel}\n" + "\n".join("      " + ln for ln in suggested_header(rel).splitlines())
            for rel in missing)
    )


def test_scope_and_check_rules():
    """Self-check of the scope filter and the head check."""
    assert in_scope("agentic_digital_thread/agents/x.py")
    assert in_scope("highbyte/components/opc-server/clock.mjs")
    assert in_scope("agentic_digital_thread/Dockerfile.ecs")
    assert not in_scope("agentic_digital_thread/frontend/node_modules/a/b.js")
    assert not in_scope("agentic_digital_thread/frontend/src/vite-env.d.ts")
    assert not in_scope("static/i3x-explorer/app.min.js")
    assert not in_scope("cdk.json")

    notice = "# " + NOTICE + "\n"
    spdx = "# " + SPDX + "\n"
    assert not lacks_copyright_line(notice + spdx + '"""Doc."""\n')
    assert not lacks_copyright_line("#!/usr/bin/env python3\n" + notice + spdx)
    assert lacks_copyright_line(spdx + '"""Doc."""\n')
    assert lacks_copyright_line('"""Doc."""\nimport os\n')
    assert lacks_copyright_line("\n" * HEAD_LINES + notice)
    assert not lacks_copyright_line("")
    assert not lacks_copyright_line("\n\n")


def test_package_initializers_are_in_scope():
    """An empty package marker passes; an initializer with code needs the line."""
    assert in_scope("agentic_digital_thread/agents/auth/__init__.py")
    assert not lacks_copyright_line("", "agentic_digital_thread/agents/auth/__init__.py")
    assert lacks_copyright_line(
        "from .middleware import AuthMiddleware\n", "agentic_digital_thread/agents/auth/__init__.py")


def test_only_a_real_notice_line_counts():
    """Prose, a string constant or a truncated notice is not the copyright line."""
    py = "agentic_digital_thread/agents/x.py"
    full = "Copyright " + COPYRIGHT_LINE + ". All Rights Reserved."
    assert not lacks_copyright_line("# " + full + "\n", py)
    assert not lacks_copyright_line("# " + full + " SPDX-License-Identifier: MIT-0\n", py)
    assert lacks_copyright_line('NOTICE = "' + full + '"\n', py)
    assert lacks_copyright_line('"""\n' + full + '\n"""\n', py)
    assert lacks_copyright_line("# This module is " + full + "\n", py)
    assert lacks_copyright_line("# Copyright " + COPYRIGHT_LINE + "\n", py)
    assert lacks_copyright_line("// " + full + "\n", py)
    # Each file type's own comment syntax.
    assert not lacks_copyright_line("// " + full + "\n", "frontend/src/a.tsx")
    assert lacks_copyright_line("# " + full + "\n", "frontend/src/a.tsx")
    assert not lacks_copyright_line("-- " + full + "\n", "erp_simulation/db/001.sql")
    assert not lacks_copyright_line("/* " + full + "\n   SPDX-License-Identifier: MIT-0 */\n", "a/b.css")
    assert not lacks_copyright_line("/*\n * " + full + "\n */\n", "a/b.css")
    assert lacks_copyright_line("a { content: '" + full + "'; }\n", "a/b.css")
    assert not lacks_copyright_line(
        "<!DOCTYPE html>\n<!-- " + full + " SPDX-License-Identifier: MIT-0 -->\n", "a/index.html")
    assert not lacks_copyright_line("#!/bin/bash\n# " + full + "\n", "scripts/run.sh")
    assert not lacks_copyright_line("# " + full + "\n", "agentic_digital_thread/Dockerfile.ecs")


def test_a_modified_third_party_file_carries_the_modifications_line():
    """An i3X Explorer file names the upstream holder, then our modifications."""
    upstream = "// " + I3X_UPSTREAM_NOTICE + "\n"
    mods = "// Modifications Copyright " + COPYRIGHT_LINE + ".\n"
    assert not lacks_copyright_line(upstream + mods, "i3x-explorer/src/App.tsx")
    assert lacks_copyright_line(mods, "i3x-explorer/src/App.tsx")
    assert not lacks_copyright_line(
        "/* " + I3X_UPSTREAM_NOTICE + "\n   Modifications Copyright "
        + COPYRIGHT_LINE + ".\n   SPDX-License-Identifier: MIT */\n", "i3x-explorer/src/index.css")


def test_the_modifications_line_needs_the_known_upstream_holder():
    """A bare or generic Copyright line is not an upstream holder, and the
    modifications form is accepted only for the i3X Explorer."""
    mods = "// Modifications Copyright " + COPYRIGHT_LINE + ".\n"
    app = "i3x-explorer/src/App.tsx"
    assert lacks_copyright_line("// Copyright\n" + mods, app)
    assert lacks_copyright_line("// Copyright information follows\n" + mods, app)
    assert lacks_copyright_line("// Copyright (c) 2024 Someone Else\n" + mods, app)
    assert lacks_copyright_line(mods + "// " + I3X_UPSTREAM_NOTICE + "\n", app)
    # The same two lines outside the i3X Explorer still need our own notice.
    assert lacks_copyright_line(
        "// " + I3X_UPSTREAM_NOTICE + "\n" + mods, "agentic_digital_thread/frontend/src/a.tsx")


def test_the_failure_message_uses_each_file_types_comment_syntax():
    full = "Copyright " + COPYRIGHT_LINE + ". All Rights Reserved."
    assert suggested_header("a/x.py").startswith("# " + full)
    assert suggested_header("a/x.ts").startswith("// " + full)
    assert suggested_header("a/x.sql").startswith("-- " + full)
    assert suggested_header("a/x.css").startswith("/* " + full)
    assert suggested_header("a/x.html").startswith("<!-- " + full)
    assert suggested_header("a/Dockerfile.ecs").startswith("# " + full)
    # Every suggested header passes the check it is suggested for.
    for rel in ("a/x.py", "a/x.ts", "a/x.sql", "a/x.css", "a/x.html", "a/Dockerfile.ecs", "a/x.sh"):
        assert not lacks_copyright_line(suggested_header(rel) + "\n", rel), rel
