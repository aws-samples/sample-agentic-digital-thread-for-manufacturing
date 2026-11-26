# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared exception types — stdlib-only at import time.

This module exists so exception classes can be shared across the one-way demo
seam (``agents/demo/*`` MUST NOT import ``agents.db`` / ``agents.catalog`` /
``agents.main``): both sides import from here instead.
"""


class MissingDbPasswordError(RuntimeError):
    """Fail-closed refusal: no explicit database password is configured.

    Raised at the credential refusal sites (``db.get_conn_params`` and
    ``demo.db_routing.demo_conn_params_for_schema``) instead of a well-known
    default password. Inherits ``RuntimeError`` for backward compatibility
    (existing ``except RuntimeError`` callers and ``pytest.raises(RuntimeError)``
    pins are unchanged) — but it lets discriminating callers (the records-first
    fallback in ``db._records_conn_params_for_schema``) re-raise ONLY this
    deliberate refusal while unrelated ``RuntimeError``s keep the fail-soft env
    fallback.
    """


class ConnectionSecretUnavailableError(MissingDbPasswordError):
    """Fail-closed refusal: a system's OWN connection secret (the user's
    ``adt/conn/<id>`` registration) cannot be read, is not bound to that system,
    or lacks a user and password.

    Raised instead of falling back to the deployment's own database credentials
    or the demo routing — either would send the deployment's credentials to the
    user's host, or repoint the user's system at the demo database. A subclass
    of :class:`MissingDbPasswordError` so the records-first fallback re-raises
    it, and so ``server._restore`` can skip exactly that one system. The
    message never carries a secret value.
    """


class SystemReaderLoginError(MissingDbPasswordError):
    """Fail-closed refusal: a system agent would read a database this
    deployment provisions, but its read-only login for that schema is not
    configured or its secret cannot be read.

    Raised instead of handing the agent the database owner's login
    (``demo.db_routing.system_agent_reader_login``). A subclass of
    :class:`MissingDbPasswordError` so the callers that already treat a
    missing credential as "do not start this system" do the same here. The
    message names the schema only, never a secret value.
    """
