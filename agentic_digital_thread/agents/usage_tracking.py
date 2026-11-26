# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Usage extraction for accepted self-learning rules (item C — utility loop).

Pure, stdlib-only (import purity per ADR-0020/0021 — no boto3, no I/O).
Scans arbitrary tool-result text for the applied-node ids a saved rule
carries once accepted:

- ``subj-<16 hex>`` — the subject-keyed applied-node identity
  (``self_learning._subject_id``, ADR-0003 D);
- ``vm-learned-<8 hex>`` — the legacy value-mapping node id shape.

The impure recording half (atomic usage counter on the accepted DynamoDB
rows) lives in ``self_learning.record_rule_usage``; the post-run hook that
feeds this extractor lives in ``runtime_app``.
"""
from __future__ import annotations

import re

# Exact-length ids only: the trailing (?![0-9a-fA-F]) rejects a longer hex run
# (e.g. a full sha256 digest after "subj-", or a UUID segment) so we never
# count a truncated prefix of some other token as a rule id. \b on the left
# anchors the literal prefix at a word boundary.
_NODE_ID_RE = re.compile(
    r"\b(?:subj-[0-9a-f]{16}|vm-learned-[0-9a-f]{8})(?![0-9a-fA-F])"
)


def extract_rule_node_ids(texts: list[str]) -> set[str]:
    """Return every applied-rule node id mentioned across ``texts``.

    Deterministic, no I/O. Non-string entries are skipped defensively (tool
    results are free-form). Returns a (possibly empty) set of ids.
    """
    ids: set[str] = set()
    for text in texts or []:
        if not isinstance(text, str):
            continue
        ids.update(_NODE_ID_RE.findall(text))
    return ids
