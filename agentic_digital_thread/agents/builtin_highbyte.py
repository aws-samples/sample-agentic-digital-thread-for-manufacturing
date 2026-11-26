# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The built-in HighByte Intelligence Hub system (system id ``highbyte``, ADR-0031).

Two rules every onboarding and query path applies to that one system id:

* **Its connection comes from the deployment, never a request.** A deploy that
  ships the hub sets ``/adt/highbyte-url`` (the app task reads it as
  ``HIGHBYTE_URL``) and the hub's setup Lambda mints the MCP key into
  ``adt/conn/highbyte``. :func:`bootstrap_source` is that pair, with the
  ``Bearer`` Authorization form the setup Lambda verifies and the HighByte guide
  documents ("a valid Bearer Token is required"). A local 4.4.2 hub also takes
  the raw key; Bearer is used because it is the documented form. Preflight,
  both discover paths and provision use it until the system's graph node holds
  its own connection, so a registration with no connection of its own (the
  workshop's register-only entry) onboards in one click.
* **Only its read tools reach a model.** The hub's Configuration Tools
  (``config_*``) create and edit pipelines with the API key's user's rights,
  and that key belongs to the administrator. :func:`read_only_tools` keeps only
  :data:`READ_TOOLS` for this system id, in discovery and in the provisioned
  runtime, whatever the hub's own Configuration Tools setting says (a hub
  deployed before that setting was turned off keeps its first-boot settings
  file). Every other MCP system keeps every tool it lists.
"""
from __future__ import annotations

from typing import Callable, Iterable, TypeVar

#: The built-in HighByte system id (ADR-0031).
BUILTIN_ID = "highbyte"

#: The Authorization form the built-in HighByte connection uses.
AUTH_SCHEME = "bearer"

#: The hub tools the built-in system may expose: the API-triggered pipeline that
#: reads the Unified Namespace (``config/highbyte/intelligencehub-configuration.json``).
#: A 4.4.2 hub lists only this tool with Configuration Tools off.
READ_TOOLS = frozenset({"NameSpace_MCP"})

T = TypeVar("T")


def is_builtin(system_id: object) -> bool:
    return system_id == BUILTIN_ID


def bootstrap_source() -> "dict | None":
    """``{url, token, auth_scheme, secret_arn}`` for the deployment's hub, or
    ``None`` when the deployment has no hub URL.

    ``url`` is the hub URL + ``/mcp``. The key is the canonical
    ``adt/conn/highbyte`` secret's whenever it holds one, with that secret's ARN
    as ``secret_arn``, so provision points the node at the secret instead of
    rewriting it; else the task env's ``HIGHBYTE_I3X_TOKEN`` with ``""``
    (``catalog._get_highbyte_token_source``)."""
    from .catalog import _get_highbyte_token_source, _get_highbyte_url
    base = _get_highbyte_url()
    if not base:
        return None
    token, secret_arn = _get_highbyte_token_source()
    return {"url": base + "/mcp", "token": token, "auth_scheme": AUTH_SCHEME,
            "secret_arn": secret_arn}


def read_only_tools(system_id: object, tools: Iterable[T],
                    name: Callable[[T], object]) -> "list[T]":
    """``tools`` without any tool outside :data:`READ_TOOLS` when ``system_id``
    is the built-in HighByte system; every tool otherwise."""
    tools = list(tools)
    if not is_builtin(system_id):
        return tools
    return [tool for tool in tools if name(tool) in READ_TOOLS]
