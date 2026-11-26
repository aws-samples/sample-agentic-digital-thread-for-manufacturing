# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The read operations a REST-family connector offers, from its own toolset.

A REST vendor's runtime toolset is built by its registry hook
(``rest_vendor_registry.RestVendorAdapter.runtime_tools``), and every API tool
declares itself a data or discovery read where it is defined
(``tool_result_capture.declare_read``, pinned by
``tests/unit/test_api_read_evidence.py``). Discovery's schema summary reads the
operations from there instead of asking a model to infer endpoints from entity
and column names: on a read-only API the model listed POST, PUT and DELETE
endpoints for every entity.

Building the toolset makes no request; only the non-secret selectors that pick
a transport's toolset (``SELECTOR_KEYS``) are passed, never a credential.

Three states, kept apart so the summary never invites a guess:
``None`` (no vendor toolset of its own: the generic REST API, read from its
OpenAPI spec, and every non-REST type), a list of read operations, and
:data:`UNAVAILABLE` (a known vendor whose toolset could not be built or whose
tools declare no read): its operations are reported as not listed, never
inferred.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .rest_vendor_registry import RestVendorUnsupported, resolve_vendor
from .tool_result_capture import declared_read_kinds

logger = logging.getLogger(__name__)

#: Connection-config keys that choose which tools a vendor builds (a transport
#: or site selector, Tulip's workspace scope). Never a credential.
SELECTOR_KEYS = ("api_variant", "tenant", "target", "workspace_id", "key_scope")
_PURPOSE_MAX = 160
_SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)\.(?=\s|$)")


@dataclass(frozen=True)
class ReadOperations:
    """``reads``: ``(tool name, first sentence of its description)`` for each
    tool that declares a read. ``only_reads``: every tool the connector
    registers declares one, so it offers no other operation. ``available``:
    the list could be read (false for :data:`UNAVAILABLE`)."""

    reads: tuple
    only_reads: bool
    available: bool = True


#: A known vendor whose operation list could not be read.
UNAVAILABLE = ReadOperations(reads=(), only_reads=False, available=False)


def _purpose(tool) -> str:
    spec = getattr(tool, "tool_spec", None) or {}
    text = " ".join(str(spec.get("description") or "").split())
    end = _SENTENCE_END.search(text)
    sentence = text[:end.end()] if end else text
    return sentence[:_PURPOSE_MAX]


def read_operations(connection_type: str, connection_config: dict | None = None):
    """The :class:`ReadOperations` of the connector for ``connection_type``;
    ``None`` when that type has no vendor toolset of its own (the generic REST
    API, whose toolset raises :class:`RestVendorUnsupported`, and every
    non-REST type); :data:`UNAVAILABLE` when a known vendor's toolset cannot
    be built or none of its tools declares a read."""
    adapter = resolve_vendor(connection_type)
    if adapter is None:
        return None
    cfg = {k: v for k, v in (connection_config or {}).items() if k in SELECTOR_KEYS and v}
    try:
        tools = list(adapter.runtime_tools("", "", "", "", cfg) or [])
    except RestVendorUnsupported:
        return None
    except Exception as exc:  # noqa: BLE001 - a known vendor: reported as unlisted
        logger.warning("read operations: %s toolset could not be built (%s)",
                       connection_type, type(exc).__name__)
        return UNAVAILABLE
    kinds = declared_read_kinds(tools)
    reads = tuple((t.tool_name, _purpose(t)) for t in tools
                  if getattr(t, "tool_name", None) in kinds)
    if not reads:
        return UNAVAILABLE
    return ReadOperations(reads=reads, only_reads=len(reads) == len(tools))


def operations_text(ops: ReadOperations) -> str:
    """The operations as summary text: one line per read tool, headed by
    whether the connector offers nothing else; for :data:`UNAVAILABLE`, one
    sentence saying they could not be listed."""
    if not ops.available:
        return "This connector's operations could not be listed."
    head = ("The connector offers only these read operations (no create, update or delete):"
            if ops.only_reads else "Read operations this connector offers:")
    return "\n".join([head, *(f"- {name}: {purpose}" if purpose else f"- {name}"
                              for name, purpose in ops.reads)])
