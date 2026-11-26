# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The function-length maintainability check must see a committed branch diff.

The original helpers found changed functions with ``git diff`` against the
working tree and ``git ls-files --others``. On a clean CI checkout both are
empty, so the 50-line rule checked nothing in CI, and nothing locally once the
work was committed. These tests build a real git repo, commit an over-long
function on a branch, and prove:

  * the base-aware check FLAGS it against the branch base;
  * the SAME committed scenario run through the old working-tree-only logic
    reports nothing (the defect reproduced against the old callable);
  * an unchanged long function in a touched file is NOT flagged (identity);
  * staged-only edits are seen;
  * CI fails closed when no comparison base resolves.
"""
from __future__ import annotations

import re
import subprocess  # nosec B404 - subprocess used with fixed internal argv lists only
from pathlib import Path

import pytest

from tests.unit import _funclen_diff as fd


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603 B607 - fixed argv, no shell, internal inputs; git from PATH by design
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(repo: Path) -> None:
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "Test")
    _run(repo, "config", "commit.gpgsign", "false")


def _long_function(name: str, lines: int) -> str:
    body = "\n".join(f"    x{i} = {i}" for i in range(lines))
    return f"def {name}(items):\n{body}\n    return items\n"


def _short_function(name: str) -> str:
    return f"def {name}(items):\n    return [i for i in items if i]\n"


# --- A faithful copy of the OLD working-tree-only logic (the code at
# da90a9c1), kept so the defect can be reproduced against the old callable in
# the same test run: the old callable reports nothing for a committed long
# function, while the base-aware check reports it. ---

def _old_changed_paths(root: Path) -> set[Path]:
    paths: set[Path] = set()
    for command in (["diff", "--name-only", "--", "*.py"],
                    ["ls-files", "--others", "--exclude-standard", "--", "*.py"]):
        output = subprocess.run(  # nosec B603 B607 - fixed argv, no shell, internal inputs
            ["git", *command], cwd=root, check=True, capture_output=True, text=True,
        ).stdout
        paths.update(root / line for line in output.splitlines())
    return {p for p in paths if p.is_file()}


def _old_failures(root: Path) -> list[str]:
    """The pre-fix check at da90a9c1: working-tree diff + untracked only, no
    base. Reproduced verbatim so a committed long function can be shown to go
    unreported by the old callable."""
    failures: list[str] = []
    for path in sorted(_old_changed_paths(root)):
        relative = path.relative_to(root)
        if "tests" in relative.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tracked = subprocess.run(  # nosec B603 B607 - fixed argv, no shell, internal inputs
            ["git", "ls-files", "--error-unmatch", str(relative)], cwd=root,
            capture_output=True,
        ).returncode == 0
        # The old callable diffed only the working tree; a clean committed
        # tree has no changed lines, which is the whole defect.
        changed: set[int] = set()
        if tracked:
            output = subprocess.run(  # nosec B603 B607 - fixed argv, no shell, internal inputs
                ["git", "diff", "--unified=0", "--", str(relative)], cwd=root,
                check=True, capture_output=True, text=True).stdout
            for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", output, re.M):
                start, count = int(match.group(1)), int(match.group(2) or "1")
                changed.update(range(start, start + max(1, count)))
        else:
            changed = set(range(1, len(source.splitlines()) + 1))
        for identity, (fingerprint, start, end) in fd.function_records(source).items():
            if set(range(start, end + 1)) & changed and end - start + 1 > 50:
                failures.append(f"{relative}:{identity[0]}:{end - start + 1}")
    return failures


@pytest.fixture()
def branch_repo(tmp_path: Path) -> Path:
    """A repo whose ``main`` holds a short function and whose ``work`` branch
    commits a 60-line function, with the work committed (clean tree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    module = repo / "mod.py"
    module.write_text(_short_function("helper"), encoding="utf-8")
    _run(repo, "add", "mod.py")
    _run(repo, "commit", "-qm", "base")
    _run(repo, "checkout", "-q", "-b", "work")
    module.write_text(_short_function("helper") + "\n\n" + _long_function("bloated", 60),
                      encoding="utf-8")
    _run(repo, "add", "mod.py")
    _run(repo, "commit", "-qm", "add bloated function")
    return repo


def test_reproduction_old_callable_misses_what_the_base_aware_check_finds(branch_repo: Path):
    # Failing-first reproduction against the OLD callable: the base-aware check
    # reports the committed 60-line function, while the old working-tree-only
    # callable reports nothing for the exact same repo. Run against the code at
    # da90a9c1 the base-aware implementation does not exist and the old helpers
    # report nothing, so this scenario is the defect's behavioural proof.
    base = fd.merge_base(branch_repo, "main")
    new_failures = fd.maintainability_failures(branch_repo, base)
    assert any(f.startswith("mod.py:bloated:") for f in new_failures)
    # Guard (passes by design): the old callable sees the clean committed tree
    # as empty and finds nothing, which is exactly what the fix corrects.
    assert _old_failures(branch_repo) == []


def test_committed_long_function_length_is_reported(branch_repo: Path):
    base = fd.merge_base(branch_repo, "main")
    failures = fd.maintainability_failures(branch_repo, base)
    length = int(re.search(r"mod\.py:bloated:(\d+)", " ".join(failures)).group(1))
    assert length > 50


def test_unchanged_long_function_in_a_touched_file_is_not_flagged(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    module = repo / "mod.py"
    module.write_text(_long_function("legacy", 60), encoding="utf-8")
    _run(repo, "add", "mod.py")
    _run(repo, "commit", "-qm", "base with pre-existing long function")
    _run(repo, "checkout", "-q", "-b", "work")
    # Touch the file around the long function without changing its body.
    module.write_text("# header added by the branch\n" + _long_function("legacy", 60)
                      + "\n\n" + _short_function("added"), encoding="utf-8")
    _run(repo, "add", "mod.py")
    _run(repo, "commit", "-qm", "touch file, do not change the long function")
    base = fd.merge_base(repo, "main")
    failures = fd.maintainability_failures(repo, base)
    assert not any("legacy" in f for f in failures)


def test_uncommitted_and_untracked_changes_are_still_included(branch_repo: Path):
    # A base-aware check must not stop seeing local edits. Add an uncommitted
    # long function and an untracked long module; both must be flagged.
    (branch_repo / "mod.py").write_text(
        _short_function("helper") + "\n\n" + _long_function("bloated", 60)
        + "\n\n" + _long_function("uncommitted", 55), encoding="utf-8")
    (branch_repo / "extra.py").write_text(_long_function("untracked_long", 70), encoding="utf-8")
    base = fd.merge_base(branch_repo, "main")
    failures = fd.maintainability_failures(branch_repo, base)
    assert any(f.startswith("mod.py:uncommitted:") for f in failures)
    assert any(f.startswith("extra.py:untracked_long:") for f in failures)


def test_no_base_falls_back_to_working_tree_behaviour(branch_repo: Path):
    # With no resolvable base and a clean tree, the check flags nothing (the
    # documented fallback), exactly the old behaviour.
    assert fd.maintainability_failures(branch_repo, None) == []


def test_fully_staged_long_function_is_flagged(tmp_path: Path):
    # A staged-only edit (git add, no commit) must still be seen: the changed
    # line numbers union the staged diff, not just the unstaged one.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    module = repo / "mod.py"
    module.write_text(_short_function("helper"), encoding="utf-8")
    _run(repo, "add", "mod.py")
    _run(repo, "commit", "-qm", "base")
    # Stage a 62-line replacement; do NOT commit it.
    module.write_text(_short_function("helper") + "\n\n" + _long_function("staged", 62),
                      encoding="utf-8")
    _run(repo, "add", "mod.py")
    # No base: the staged diff alone must carry the changed lines.
    failures = fd.maintainability_failures(repo, None)
    assert any(f.startswith("mod.py:staged:") for f in failures)


def test_ci_without_a_base_fails_closed(monkeypatch, branch_repo: Path):
    # In CI, a None base means the fetch did not bring the base into the
    # shallow checkout. require_base must raise rather than silently pass.
    for var in fd.CI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.delenv("ADT_FUNCLEN_BASE", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    # A repo with no origin/feat/v2 or origin/main ref: no base resolves.
    with pytest.raises(fd.NoComparisonBaseError):
        fd.require_base(branch_repo)


def test_fetch_failure_in_ci_is_a_failure_not_an_empty_result(monkeypatch, branch_repo: Path):
    # Simulate the MED scenario: CI is set and the base fetch failed, so no
    # base ref is present. The maintainability check path (require_base) must
    # surface a failure, not return [] as if the tree were clean.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITLAB_CI", raising=False)
    monkeypatch.delenv("ADT_FUNCLEN_BASE", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    with pytest.raises(fd.NoComparisonBaseError):
        fd.require_base(branch_repo)


def test_local_run_without_a_base_still_falls_back(monkeypatch, branch_repo: Path):
    # Outside CI, require_base returns None (working-tree-only), not a raise.
    for var in fd.CI_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ADT_FUNCLEN_BASE", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_DIFF_BASE_SHA", raising=False)
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    assert fd.require_base(branch_repo) is None
