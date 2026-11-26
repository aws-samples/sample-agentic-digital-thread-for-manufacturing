# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared credential-SOURCE layer: a connection may take its credentials either
inline (the key the user pastes) or from a Secrets Manager secret the user names
by ARN. This module owns the two rules that keep the ARN source safe, so any
connection type can opt in (``connection_types.SECRET_ARN_CREDENTIAL_TYPES``);
Tulip is wired. The allowlist prefix is set in deploy config.

Rule 1 — the secret is PINNED to where it names. A secret may carry the
non-secret fields that decide where its key is sent (:func:`pinned_fields`, per
connection type: Tulip's ``base_url`` / ``workspace_id`` / ``key_scope`` /
``table_label_prefix``) beside its
credentials. When it does, those WIN: the matching request field is optional,
and if supplied it must equal the secret's value, else the request is refused
(:func:`refuse_pin_conflict`). The URL is not itself secret, but it decides
where the key is sent, so it has to travel with the key — no path may pair a key
read from a secret with a URL or workspace the caller supplied.

Rule 2 — only an ALLOWLISTED ARN is accepted (:func:`check_arn_allowed`):

  * a per-deploy PREFIX allowlist (``ADT_SECRET_ARN_ALLOWLIST``, comma or
    whitespace separated secret-NAME prefixes). EMPTY by default, which hides
    AND refuses the ARN option — a deploy opts in by setting it (the workshop
    sets ``adt/workshop/``);
  * the connection types it covers (``ADT_SECRET_ARN_SOURCE_TYPES``, keys
    ``tulip``; Tulip alone when unset), so a deploy offers and accepts
    the source only for types whose read grants it built (:func:`source_types`);
  * ADT's OWN secrets are refused even when a prefix would allow them
    (:data:`FORBIDDEN_SECRET_PREFIXES` / :data:`FORBIDDEN_SECRET_NAMES` —
    ``adt/conn/``, ``adt/systems/``, ``adt/agent-config/``, ``adt-demo-password``
    and the database reader/owner secrets), so a caller can never point a system
    at a secret the app role reads for its own purposes and have it sent out;
  * same account and Region as the app only — a full ARN in another account is
    refused by :func:`connection_resolver.refuse_foreign_account_secret` at read
    time, and the Region is checked here.

The resolver's existing ``write_connection`` guard that refuses a
client-supplied ``secret_arn`` stays in force for every connection type that has
NOT opted in; the ARN source is the ONE sanctioned way a request names a secret,
and only for an opted-in type whose ARN clears :func:`check_arn_allowed`.
"""

from __future__ import annotations

import os
import re

from .connection_types import TULIP as _TULIP

#: Non-secret fields a credentials secret may carry that DECIDE where the key is
#: sent. When the secret holds one, its value wins and a mismatched request value
#: is refused. ``key_scope`` is included because it selects instance- vs
#: workspace-scoped auth, which changes how the key is used;
#: ``table_label_prefix`` because it decides which of the workspace's tables the
#: key reads. This is Tulip's set, and the set for every type with no entry in
#: :data:`PINNED_FIELDS_BY_TYPE`.
PINNED_SECRET_FIELDS = ("base_url", "workspace_id", "key_scope", "table_label_prefix")


#: Connection type -> its pinned fields, for a type whose fields differ from
#: :data:`PINNED_SECRET_FIELDS`.
PINNED_FIELDS_BY_TYPE: dict = {}


def pinned_fields(connection_type=None) -> tuple:
    """The fields a credentials secret pins for ``connection_type``:
    :data:`PINNED_FIELDS_BY_TYPE` when the type has its own set, else
    :data:`PINNED_SECRET_FIELDS` (Tulip's, and every other type's). Pure."""
    return PINNED_FIELDS_BY_TYPE.get(str(connection_type or ""), PINNED_SECRET_FIELDS)

#: Secret NAME prefixes ADT writes/reads for its OWN purposes. Never acceptable
#: as a request-named ARN source, even inside an allowlisted prefix.
FORBIDDEN_SECRET_PREFIXES = (
    "adt/conn/",          # per-system connection secrets (write_connection)
    "adt/systems/",       # legacy per-system secrets
    "adt/agent-config/",  # the per-system agent runtime config secrets
)

#: Exact secret NAMES ADT owns (the demo password).
FORBIDDEN_SECRET_NAMES = (
    "adt-demo-password",
)

#: The database credential secrets are CDK-generated (random-suffixed) with
#: these name stems; refused by stem so none can be named as an ARN source.
FORBIDDEN_SECRET_NAME_PATTERNS = (
    re.compile(r"(?:^|/)Reader[A-Za-z0-9]"),       # _db_readers.py reader logins
    re.compile(r"(?:^|/)AdtDbSecret", re.I),       # the sample DB owner secret
    re.compile(r"(?:^|/)DatabaseSecret", re.I),    # Aurora-generated owner secrets
)

_ALLOWLIST_ENV = "ADT_SECRET_ARN_ALLOWLIST"
#: Which connection types may take a named secret, by key (:func:`source_types`).
_SOURCE_TYPES_ENV = "ADT_SECRET_ARN_SOURCE_TYPES"
#: ``ADT_SECRET_ARN_SOURCE_TYPES`` key -> connection type, in reporting order.
SOURCE_TYPE_KEYS = {"tulip": _TULIP}


class SecretArnNotAllowedError(ValueError):
    """A request named a Secrets Manager ARN that the credential-source rules
    refuse: the ARN is malformed, in another Region, an ADT-owned secret, or not
    covered by this deploy's allowlist (including the empty-allowlist default,
    which refuses every ARN). The message names the reason, never a value."""


class PinnedFieldConflictError(ValueError):
    """A request supplied a pinned field (:func:`pinned_fields`, such as
    ``base_url``) that disagrees with the value the credentials secret names.
    The secret wins, so the request is refused rather than sending the key where
    the caller asked. The message names the field, never a secret value."""


def allowlist_prefixes(environ=None) -> tuple[str, ...]:
    """The deploy's allowlisted secret-NAME prefixes (``ADT_SECRET_ARN_ALLOWLIST``,
    comma/whitespace separated), or ``()`` when unset/blank — the default, which
    hides and refuses the ARN option. Pure; reads ``environ`` (``os.environ``)."""
    env = os.environ if environ is None else environ
    raw = str(env.get(_ALLOWLIST_ENV, "") or "")
    return tuple(p for p in re.split(r"[,\s]+", raw.strip()) if p)


def arn_source_enabled(environ=None) -> bool:
    """True when this deploy accepts request-named secret ARNs for at least one
    connection type (:func:`source_types` is not empty). The UI hides the ARN
    option when this is False."""
    return bool(source_types(environ))


def source_types(environ=None) -> tuple[str, ...]:
    """The connection types this deploy lets take a named secret, in the order
    of :data:`SOURCE_TYPE_KEYS`. ``()`` while the allowlist is empty. Otherwise
    the types ``ADT_SECRET_ARN_SOURCE_TYPES`` names by key (comma/whitespace
    separated, case-insensitive; an unknown key is ignored), or Tulip alone when
    that variable is unset or blank, the source's original meaning. The
    deployment sets the list where its read grants reach.
    Pure; reads ``environ`` (``os.environ``)."""
    env = os.environ if environ is None else environ
    if not allowlist_prefixes(env):
        return ()
    raw = str(env.get(_SOURCE_TYPES_ENV, "") or "").strip()
    if not raw:
        return (_TULIP,)
    keys = {k.lower() for k in re.split(r"[,\s]+", raw) if k}
    return tuple(ctype for key, ctype in SOURCE_TYPE_KEYS.items() if key in keys)


def source_type_enabled(connection_type, environ=None) -> bool:
    """True when this deploy lets ``connection_type`` take a named secret
    (:func:`source_types`)."""
    return str(connection_type or "") in source_types(environ)


def _secret_name_from_arn(secret_arn: str) -> str:
    """The resource NAME of a Secrets Manager ARN (the part after ``:secret:``),
    with Secrets Manager's trailing 6-char ``-XXXXXX`` suffix removed when
    present so a prefix match reads the name the deploy configured. ``""`` for a
    non-ARN."""
    text = str(secret_arn or "")
    marker = ":secret:"
    idx = text.find(marker)
    if idx == -1:
        return ""
    name = text[idx + len(marker):]
    return re.sub(r"-[A-Za-z0-9]{6}$", "", name)


def _arn_region(secret_arn: str) -> str:
    parts = str(secret_arn or "").split(":", 6)
    return parts[3] if len(parts) == 7 and parts[0] == "arn" else ""


def is_forbidden_secret_name(name: str) -> bool:
    """True when a secret NAME is one ADT owns for its own purposes (never an
    acceptable request-named source), by prefix, exact name, or DB-secret stem."""
    name = str(name or "")
    if not name:
        return True
    if name.startswith(FORBIDDEN_SECRET_PREFIXES) or name in FORBIDDEN_SECRET_NAMES:
        return True
    return any(pat.search(name) for pat in FORBIDDEN_SECRET_NAME_PATTERNS)


def _deployment_account(environ) -> str:
    """The deployment's 12-digit AWS account: ``AWS_ACCOUNT_ID`` (set on the
    runtimes), else ``connection_resolver.deployment_account_id`` (STS). ``""``
    when it cannot be learned the ARN's account then cannot be checked, so
    the caller fails closed. Lazy import (connection_resolver imports this
    module)."""
    env_account = str((environ or {}).get("AWS_ACCOUNT_ID", "") or "").strip()
    if re.fullmatch(r"\d{12}", env_account):
        return env_account
    try:
        from . import connection_resolver as _cr
        return _cr.deployment_account_id()
    except Exception:  # noqa: BLE001 - unknown account: fail closed in the caller
        return ""


def _shown_region(region: str) -> str:
    """The ARN's region field for a refusal message: the field is submitted
    text, so only a well-formed region name is echoed."""
    if not region:
        return "(none)"
    return region if re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}", region) else "(not a region name)"


def check_arn_allowed(secret_arn: str, *, environ=None) -> None:
    """Raise :class:`SecretArnNotAllowedError` unless ``secret_arn`` is a full,
    well-formed Secrets Manager ARN of THIS deployment (supported partition,
    this Region, this account, resource type ``secret``, a valid Secrets Manager
    name), whose secret NAME starts with one of the deploy's allowlisted
    prefixes and is not one of ADT's own secrets.

    The ARN's IDENTITY (partition / account / Region / resource shape / name)
    is validated here, in full, before a registration is claimed: a
    foreign-account or malformed full-looking ARN is refused up front, not
    deferred to a later read. The deployment account is read from
    ``AWS_ACCOUNT_ID`` (else STS); when it cannot be determined the ARN's
    account cannot be checked, so the ARN is refused (fail closed). Every read
    still re-checks the account (:func:`connection_resolver.refuse_foreign_account_secret`).

    Empty allowlist ⇒ every ARN is refused (the default): the ARN source is off
    unless the deploy opts in.
    """
    env = os.environ if environ is None else environ
    # Empty allowlist ⇒ the feature is OFF: refuse BEFORE any account lookup, so
    # a disabled deployment never makes an STS call.
    prefixes = allowlist_prefixes(env)
    if not prefixes:
        raise SecretArnNotAllowedError(
            "naming a Secrets Manager secret is not enabled for this deployment")
    # Split off exactly the 6 ARN fields plus the resource remainder.
    parts = str(secret_arn or "").split(":", 6)
    if len(parts) != 7 or parts[0] != "arn" or parts[2] != "secretsmanager":
        raise SecretArnNotAllowedError("not a Secrets Manager ARN")
    _arn_word, partition, _service, region, account, resource_type, resource_name = parts
    _check_partition_and_region(partition, region, env.get("AWS_REGION", "us-east-1"))
    if not re.fullmatch(r"\d{12}", account):
        raise SecretArnNotAllowedError("secret ARN does not name a 12-digit AWS account")
    # Resource type must be ``secret`` and the remainder must be a FULL Secrets
    # Manager ARN: the name plus the service's trailing ``-XXXXXX`` suffix (a
    # bare name without the suffix is a partial ARN, refused).
    if resource_type != "secret" or not _is_full_secret_arn_name(resource_name):
        raise SecretArnNotAllowedError("secret ARN is not a full Secrets Manager secret ARN")
    own_account = _deployment_account(env)
    if not own_account:
        raise SecretArnNotAllowedError(
            "this deployment's AWS account could not be determined, so the secret ARN "
            "could not be verified; retry in a moment")
    if account != own_account:
        raise SecretArnNotAllowedError("secret ARN is in another AWS account")
    _check_secret_name(_secret_name_from_arn(secret_arn), prefixes)


def _check_partition_and_region(partition: str, region: str, own_region: str) -> None:
    # Partition: a supported AWS partition (aws, aws-us-gov, aws-cn), never a
    # look-alike like 'notaws', AND it must match the deployment Region's
    # partition (a us-gov/cn Region's secret cannot carry the commercial 'aws').
    if partition not in ("aws", "aws-us-gov", "aws-cn"):
        raise SecretArnNotAllowedError("secret ARN names an unsupported AWS partition")
    if partition != _partition_for_region(own_region):
        raise SecretArnNotAllowedError(
            "secret ARN's partition does not match this deployment's Region")
    if region != own_region:
        raise SecretArnNotAllowedError(
            f"secret is in Region {_shown_region(region)}, not this deployment's {own_region}")


def _check_secret_name(name: str, prefixes: tuple) -> None:
    if is_forbidden_secret_name(name):
        raise SecretArnNotAllowedError(
            "that secret belongs to the application and cannot be used as a connection source")
    if not name.startswith(prefixes):
        raise SecretArnNotAllowedError(
            "that secret is not in this deployment's allowed set of connection secrets")


def _partition_for_region(region: str) -> str:
    """The AWS partition a Region belongs to: ``aws-us-gov`` for us-gov-*,
    ``aws-cn`` for cn-*, else ``aws``."""
    r = str(region or "").strip().lower()
    if r.startswith("us-gov-"):
        return "aws-us-gov"
    if r.startswith("cn-"):
        return "aws-cn"
    return "aws"


#: The trailing ``-XXXXXX`` suffix Secrets Manager appends to a secret's full
#: ARN (6 alphanumerics). A full ARN carries it; a bare name does not.
_SECRET_ARN_SUFFIX_RE = re.compile(r"-[A-Za-z0-9]{6}$")


def _is_full_secret_arn_name(resource_name: str) -> bool:
    """True when the ARN's resource remainder is a full Secrets Manager name:
    a valid name AND the trailing ``-XXXXXX`` suffix (the whole-ARN form, not a
    partial name-only ARN)."""
    text = str(resource_name or "")
    return bool(_valid_secret_resource_name(text)) and bool(_SECRET_ARN_SUFFIX_RE.search(text))


#: A Secrets Manager secret NAME is 1-512 chars of ``[A-Za-z0-9/_+=.@-]`` (the
#: characters the service accepts); the full-ARN resource part is that name,
#: optionally with Secrets Manager's trailing ``-XXXXXX`` suffix (covered by the
#: name charset). Used to reject a malformed/empty resource remainder.
_SECRET_RESOURCE_NAME_RE = re.compile(r"[A-Za-z0-9/_+=.@-]{1,512}")


def _valid_secret_resource_name(resource_name: str) -> bool:
    """True when the ARN's resource remainder (after ``secret:``) is a
    well-formed, non-empty Secrets Manager name."""
    return bool(_SECRET_RESOURCE_NAME_RE.fullmatch(str(resource_name or "")))


def pinned_values(secret: dict | None, *, fields: tuple = PINNED_SECRET_FIELDS) -> dict:
    """The pinned ``fields`` (default :data:`PINNED_SECRET_FIELDS`; a type's own
    set from :func:`pinned_fields`) a secret actually carries (non-empty), as a
    plain dict. Pure."""
    secret = secret or {}
    return {k: secret[k] for k in fields
            if str(secret.get(k, "") or "").strip()}


def first_pin_conflict(secret: dict | None, submitted: dict | None, *,
                       fields: tuple = PINNED_SECRET_FIELDS) -> str:
    """The NAME of the first pinned field (``fields``, default
    :data:`PINNED_SECRET_FIELDS`) the request disagrees with the secret on, or
    ``""`` when there is none. A field the request omits or leaves blank is fine
    (the secret's value is used). ``base_url`` compares as an endpoint identity
    (scheme/host/port/path), so a trailing-slash or case difference is not a
    spurious conflict; the others compare as exact text. Pure except for the
    endpoint helper; carries no value."""
    pinned = pinned_values(secret, fields=fields)
    submitted = submitted or {}
    for field, pinned_value in pinned.items():
        supplied = submitted.get(field)
        if supplied in (None, ""):
            continue
        if field == "base_url":
            if not _same_base_url(supplied, pinned_value, fields):
                return field
        elif str(supplied) != str(pinned_value):
            return field
    return ""


def _same_base_url(a, b, fields: tuple) -> bool:
    """Whether two base URLs name the same endpoint for a pin check (the
    exact path is compared)."""
    del fields
    return _same_endpoint(a, b)


def refuse_pin_conflict(secret: dict | None, submitted: dict | None, *,
                        fields: tuple = PINNED_SECRET_FIELDS) -> None:
    """Raise :class:`PinnedFieldConflictError` when :func:`first_pin_conflict`
    finds a request field that disagrees with the secret's value. The message
    names the field, never a value."""
    field = first_pin_conflict(secret, submitted, fields=fields)
    if field:
        raise PinnedFieldConflictError(
            f"{field} is fixed by the named credentials secret and cannot be changed "
            "here; leave it blank to use the secret's value")


def apply_pins(secret: dict | None, non_secret: dict, *,
               fields: tuple = PINNED_SECRET_FIELDS) -> dict:
    """Return ``non_secret`` with the secret's pinned values forced in (the
    secret wins). Used after :func:`refuse_pin_conflict` so a stored node config
    reflects the pinned url/workspace even if the request omitted them. Pure
    (returns a new dict)."""
    out = dict(non_secret or {})
    out.update(pinned_values(secret, fields=fields))
    return out


def _same_endpoint(a, b) -> bool:
    """Endpoint-identity compare for two URLs (DRY on the resolver's helper),
    falling back to stripped-text equality if it is unavailable."""
    try:
        from .connection_resolver import same_endpoint
    except Exception:  # noqa: BLE001 - fall back to text compare
        return str(a or "").strip() == str(b or "").strip()
    return same_endpoint(a, b)
