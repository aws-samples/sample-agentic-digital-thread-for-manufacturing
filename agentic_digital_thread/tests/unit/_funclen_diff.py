# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identity-aware function-length check over a branch diff.

The maintainability regressions flag any non-test production function longer
than 50 lines that a change added or rewrote. "A change" means the diff
between a base commit and the working tree: committed branch commits plus any
uncommitted and untracked edits. Comparing only against the working tree (an
empty diff on a clean checkout) checked nothing in CI and nothing locally once
the work was committed; comparing against a base makes the check see the whole
branch.

Base selection order (first that resolves):
  1. ``ADT_FUNCLEN_BASE`` environment override (any git ref).
  2. ``CI_MERGE_REQUEST_DIFF_BASE_SHA`` (GitLab merge-request pipelines).
  3. the merge base with ``origin/<CI_MERGE_REQUEST_TARGET_BRANCH_NAME>``,
     or with ``origin/feat/v2`` or ``origin/main`` when those refs exist.
  4. no base: the working-tree-only behaviour (uncommitted and untracked
     edits only), so a checkout with no reachable base still runs.

Identity-aware: a function is flagged only when the change actually altered
its body. An unchanged long function that merely sits in a touched file is
not flagged, so pre-existing length is never charged to an unrelated change.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

#: Non-test production functions longer than this many lines are flagged.
MAX_FUNCTION_LINES = 50
#: A newly added, untracked policy file over this many lines is flagged whole.
MAX_NEW_FILE_LINES = 800
#: Env override for the comparison base (any git ref).
BASE_ENV = "ADT_FUNCLEN_BASE"
#: Fallback target refs, in order, when no MR base is in the environment.
FALLBACK_BASE_REFS = ("origin/feat/v2", "origin/main")
#: Environment variables each CI provider sets to "true" on every job.
CI_ENV_VARS = ("GITLAB_CI", "GITHUB_ACTIONS")


class NoComparisonBaseError(RuntimeError):
    """Raised in CI when the function-length check cannot resolve a base.

    Locally the check falls back to the working tree, but in CI a clean
    checkout has no working-tree diff, so a missing base would silently check
    nothing. Failing here keeps a fetch failure or an unavailable base ref
    from reopening the original blind spot.
    """


def in_ci() -> bool:
    """Whether a known CI provider set its environment flag."""
    return any(os.environ.get(var) for var in CI_ENV_VARS)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 B607 - fixed argv, no shell, internal inputs; git resolved from PATH by design
        ["git", *args], cwd=root, check=check, capture_output=True, text=True,
    )


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                check=False).returncode == 0


def merge_base(root: Path, ref: str) -> str | None:
    """The merge base of ``HEAD`` and *ref*, or ``None`` when *ref* is absent
    or the two share no history. The commit to diff a branch against when the
    branch is to merge into *ref*.
    """
    if not _ref_exists(root, ref):
        return None
    result = _git(root, "merge-base", "HEAD", ref, check=False)
    base = result.stdout.strip()
    return base if result.returncode == 0 and base else None


def resolve_base(root: Path) -> str | None:
    """The commit to diff against, or ``None`` for working-tree-only.

    Follows the documented order. When a chosen ref is not present in the
    checkout (a shallow CI clone can lack the merge-base commit), the step
    that runs the check is responsible for fetching it; here a missing ref
    simply falls through to the next candidate so the check still runs.
    """
    override = os.environ.get(BASE_ENV)
    if override and _ref_exists(root, override):
        return _git(root, "rev-parse", override).stdout.strip()

    mr_base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    if mr_base and _ref_exists(root, mr_base):
        return mr_base

    candidates = []
    target = os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
    if target:
        candidates.append(f"origin/{target}")
    candidates.extend(FALLBACK_BASE_REFS)
    for ref in candidates:
        base = merge_base(root, ref)
        if base is not None:
            return base
    return None


def require_base(root: Path) -> str | None:
    """The comparison base, failing closed in CI when none resolves.

    Locally returns ``resolve_base`` as-is (``None`` means working-tree-only).
    In CI (``in_ci``) a ``None`` base means the fetch did not bring the base
    into the shallow checkout, so the check would see nothing on a clean
    tree; raise ``NoComparisonBaseError`` rather than pass blindly.
    """
    base = resolve_base(root)
    if base is None and in_ci():
        raise NoComparisonBaseError(
            "the function-length check found no comparison base in CI "
            f"(tried {BASE_ENV}, CI_MERGE_REQUEST_DIFF_BASE_SHA, the merge base "
            f"with origin/<target> and {', '.join(FALLBACK_BASE_REFS)}); the CI "
            "job must fetch the base branch so the check does not silently pass "
            f"on a shallow clone. Set {BASE_ENV} to a reachable ref to override.")
    return base


def _tracked(root: Path, relative: Path) -> bool:
    return _git(root, "ls-files", "--error-unmatch", str(relative),
                check=False).returncode == 0


def changed_python_paths(root: Path, base: str | None) -> set[Path]:
    """Every ``*.py`` the change touched: committed since *base* (when given),
    plus uncommitted working-tree edits, plus untracked files.

    A deleted file still appears in a name-only diff but has no working-tree
    source (hence no functions); such paths are dropped.
    """
    commands = [
        ["git", "diff", "--name-only", "--", "*.py"],  # unstaged working tree
        ["git", "diff", "--name-only", "--cached", "--", "*.py"],  # staged
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],  # untracked
    ]
    if base is not None:
        commands.append(["git", "diff", "--name-only", f"{base}...HEAD", "--", "*.py"])
    paths: set[Path] = set()
    for command in commands:
        output = _git(root, *command[1:]).stdout
        paths.update(root / line for line in output.splitlines())
    return {path for path in paths if path.is_file()}


def changed_line_numbers(root: Path, relative: Path, base: str | None) -> set[int]:
    """Line numbers the change added or modified in *relative*: the union of
    the unstaged and staged working-tree diffs and, when *base* is given, the
    ``base...HEAD`` diff. An untracked file (no baseline in git) counts every
    line as changed, whether or not a base is given.
    """
    if not _tracked(root, relative):
        source = (root / relative).read_text(encoding="utf-8")
        return set(range(1, len(source.splitlines()) + 1))
    diffs = [
        ["git", "diff", "--unified=0", "--", str(relative)],            # unstaged
        ["git", "diff", "--unified=0", "--cached", "--", str(relative)],  # staged
    ]
    if base is not None:
        diffs.append(["git", "diff", "--unified=0", f"{base}...HEAD", "--", str(relative)])
    lines: set[int] = set()
    for diff in diffs:
        output = _git(root, *diff[1:]).stdout
        for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", output, re.M):
            start, count = int(match.group(1)), int(match.group(2) or "1")
            lines.update(range(start, start + max(1, count)))
    return lines


def function_records(source: str) -> dict[tuple[str, str], tuple[str, int, int]]:
    """``{(qualname, signature): (body_fingerprint, start_line, end_line)}``
    for every function and method in *source*. The signature and body
    fingerprint make the record identity-aware: the same function keeps its
    identity across edits that do not change its body, so an unchanged long
    function is recognised even when its file moved around it.
    """
    tree = ast.parse(source)
    records: dict[tuple[str, str], tuple[str, int, int]] = {}

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                signature = ast.dump(node.args, include_attributes=False)
                fingerprint = ast.dump(
                    ast.Module(body=node.body, type_ignores=[]), include_attributes=False)
                records[(qualname, signature)] = (
                    fingerprint, node.lineno, int(node.end_lineno or node.lineno))
                visit(node.body, qualname)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}.{node.name}" if prefix else node.name)

    visit(tree.body)
    return records


def _baseline_records(root: Path, relative: Path, base: str | None,
                      tracked: bool) -> dict[tuple[str, str], tuple[str, int, int]]:
    """The function records of *relative* at the comparison baseline: the
    file at *base* (or HEAD when there is no base) for a tracked file, empty
    for an untracked one.
    """
    if not tracked:
        return {}
    ref = base if base is not None else "HEAD"
    shown = _git(root, "show", f"{ref}:{relative}", check=False)
    return function_records(shown.stdout) if shown.returncode == 0 else {}


def maintainability_failures(root: Path, base: str | None) -> list[str]:
    """``"<relative>:<name>:<length>"`` for every production function the
    change added or rewrote that exceeds ``MAX_FUNCTION_LINES``, plus any
    untracked policy file over ``MAX_NEW_FILE_LINES``. Empty when clean.
    """
    failures: list[str] = []
    for path in sorted(changed_python_paths(root, base)):
        relative = path.relative_to(root)
        if "tests" in relative.parts or relative.parts[0] == ".kiro":
            continue
        source = path.read_text(encoding="utf-8")
        tracked = _tracked(root, relative)
        changed = changed_line_numbers(root, relative, base)
        baseline = _baseline_records(root, relative, base, tracked)
        for identity, (fingerprint, start, end) in function_records(source).items():
            if not set(range(start, end + 1)) & changed:
                continue
            unchanged = identity in baseline and baseline[identity][0] == fingerprint
            if end - start + 1 > MAX_FUNCTION_LINES and not unchanged:
                failures.append(f"{relative}:{identity[0]}:{end - start + 1}")
        if not tracked and len(source.splitlines()) > MAX_NEW_FILE_LINES:
            failures.append(f"{relative}:new-policy-file:{len(source.splitlines())}")
    return failures
