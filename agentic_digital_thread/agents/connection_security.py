# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SSRF/scheme guarding and canonical secret masking for connection management.

Extracted from ``connection_resolver`` (behavior-preserving split). Owns the
scheme allowlist + cloud-metadata deny constants, the DNS-rebinding-aware
:func:`validate_connection_target` guard (and its :func:`resolve_and_validate`
sibling), and the canonical masking helpers (:func:`mask_connection`,
:func:`_mask_url`, :func:`_strip_url_credentials`, :func:`_safe_detail`). These
names are re-exported by ``connection_resolver`` so existing import sites keep
working unchanged. See ADR 0008.
"""

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .response_integrity_security import redact_credential_values

SCHEME_ALLOWLIST = {"https", "opc.tcp", "postgres", "postgresql", "iotsitewise", "athena"}
# Cloud / link-local metadata endpoints that must never be probed (SSRF guard).
_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "[fd00:ec2::254]"}
# Keys whose values are secrets; redacted from any masked/audited copy.
_REDACT_KEYS = {"token", "password", "secret", "api_key", "api_secret", "key",
                "client_id", "client_secret"}  # OAuth + Tulip credential keys
# Fields carrying a connection URL whose inline credentials should be masked.
# ``base_url`` is included so a token/userinfo carried in a REST/i3X base URL is
# masked too (req6b).
_URL_KEYS = {"url", "connection_string", "base_url"}
# Secret-bearing query-string parameter names redacted from masked URLs.
_REDACT_QUERY_PARAMS = {"api_key", "token", "key", "password", "secret", "access_token"}
_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")
# Address ranges/IPs a connection target must never resolve to (SSRF guard).
# Link-local covers the IMDS endpoints, but the explicit IPs are kept for clarity.
# Loopback and RFC1918 are intentionally NOT denied (local probes / in-VPC targets).
_DENIED_NETWORKS = (_LINK_LOCAL_V4, _LINK_LOCAL_V6)
_DENIED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure IMDS (IPv4)
    ipaddress.ip_address("fd00:ec2::254"),     # AWS IMDS (IPv6)
})
# RFC1918 private ranges that ARE allowed (in-VPC targets). Kept explicit so the
# deny logic can distinguish "private but expected" from "non-global, reject".
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class ConnectionTargetError(Exception):
    """Raised when a connection target fails the SSRF/scheme guard (non-secret msg)."""


#: The refusal for a connection URL that does not parse cleanly. It names no
#: part of the URL: the part that does not parse is usually the password.
MALFORMED_URL_MESSAGE = (
    "The connection URL could not be read. If the user name or password contains "
    "@, /, ?, #, a space or another reserved character, percent-encode it (for "
    "example @ as %40 and / as %2F) and try again. Otherwise check that the port "
    "is a number from 0 to 65535.")

# What RFC 3986 allows in userinfo: unreserved and sub-delims characters, ':',
# and percent-encoded octets.
_USERINFO_RE = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=:]|%[0-9A-Fa-f]{2})*")
# The raw authority after '://', up to the first '/', '?' or '#' (whitespace
# and control characters included, as typed).
_RAW_AUTHORITY_RE = re.compile(r"[^/?#]*")
# Schemes where an '@' after the host is never a database name in practice and
# libpq decodes a percent-encoded one (see credential_url_error).
_PG_SCHEMES = frozenset({"postgres", "postgresql"})


def _parses_cleanly(url: str) -> bool:
    """True when ``url`` splits and its port (if any) is a number in range.

    A raw '/', '?' or '#' in a password ends the parsed authority early, so
    what is left (``user:pa``) reads as a host and a port that is not a number.
    """
    try:
        urlsplit(url).port
    except ValueError:
        return False
    return True


def credential_url_error(url) -> str:
    """``''`` when a connection URL is well formed, else the plain refusal
    (:data:`MALFORMED_URL_MESSAGE`). Refused: a URL that does not parse or
    whose port is not a number in range, and userinfo holding a character that
    must be percent-encoded (a raw '@', '/', '?', '#', whitespace, a control
    character or a broken escape). Whitespace and control characters are read
    from the raw string: ``urlsplit`` silently drops a tab, CR or LF, which
    would let ``user:pa<TAB>ss`` through as ``user:pass``. For the postgres
    and postgresql schemes a raw '@' anywhere after the host is refused too: a
    password whose part before a raw '/', '?' or '#' is all digits (user
    ``svc``, password ``12/34``, host ``db``) otherwise parses as a host, a
    port and a path, and libpq decodes ``%40``, so the message is a fix the
    user can apply. Other schemes keep it (``https://host:8443/users/@me`` is a valid
    base URL). A value with no ``://`` is not a URL and passes."""
    text = str(url or "").strip()
    if "://" not in text:
        return ""
    begin = text.find("://") + 3
    authority = _RAW_AUTHORITY_RE.match(text, begin).group()
    if "@" in authority and any(
            ch.isspace() or not ch.isprintable() for ch in authority[:authority.rfind("@")]):
        return MALFORMED_URL_MESSAGE
    if text[:begin - 3].lower() in _PG_SCHEMES and "@" in text[begin + len(authority):]:
        return MALFORMED_URL_MESSAGE
    if not _parses_cleanly(text):
        return MALFORMED_URL_MESSAGE
    netloc = urlsplit(text).netloc
    if "@" in netloc and not _USERINFO_RE.fullmatch(netloc.rsplit("@", 1)[0]):
        return MALFORMED_URL_MESSAGE
    return ""


def _extract_host_port(url_or_host: str) -> tuple[str, int | None]:
    """Return (host, port) from a URL or bare ``host``/``host:port`` string.

    Strips brackets from IPv6 literals. ``port`` is ``None`` when not present.
    """
    target = url_or_host
    port: int | None = None
    if "://" in target:
        # Fail closed: a port that is not a number is refused, not dropped
        # (the host urlsplit reports is then usually part of a password).
        if not _parses_cleanly(target):
            raise ConnectionTargetError(MALFORMED_URL_MESSAGE)
        parsed = urlsplit(target)
        host = parsed.hostname or ""
        port = parsed.port
    else:
        host = target
        if host.startswith("["):  # bracketed IPv6 literal, optionally with :port
            end = host.find("]")
            if end != -1:
                rest = host[end + 1:]
                host = host[1:end]
                if rest.startswith(":") and rest[1:].isdigit():
                    port = int(rest[1:])
            else:
                host = host[1:]
        elif host.count(":") == 1:
            host, _, maybe_port = host.rpartition(":")
            if maybe_port.isdigit():
                port = int(maybe_port)
    return host.strip("[]"), port


def _embedded_ipv4s(ip: "ipaddress._BaseAddress") -> list:
    """Return any IPv4 address embedded inside an IPv6 literal.

    Covers the encodings that can smuggle a link-local/IMDS IPv4 past an IPv6
    deny check: IPv4-mapped (``::ffff:a.b.c.d``), IPv4-compatible
    (``::a.b.c.d``), 6to4 (``2002::/16``) and Teredo (``2001::/32``). Returns an
    empty list for plain IPv4 or for IPv6 with no embedded IPv4.
    """
    if getattr(ip, "version", None) != 6:
        return []
    out: list = []
    mapped = ip.ipv4_mapped
    if mapped is not None:
        out.append(mapped)
    sixtofour = ip.sixtofour
    if sixtofour is not None:
        out.append(sixtofour)
    teredo = getattr(ip, "teredo", None)
    if teredo:
        out.extend(addr for addr in teredo if addr is not None)
    # IPv4-compatible (``::a.b.c.d``): upper 96 bits zero, low 32 are the IPv4
    # address. Exclude ``::`` (unspecified) and ``::1`` (loopback). Mapped/6to4/
    # Teredo all set high bits, so their integer value is > 0xFFFFFFFF and they
    # never fall into this branch.
    as_int = int(ip)
    if mapped is None and 1 < as_int <= 0xFFFFFFFF:
        out.append(ipaddress.ip_address(as_int))  # -> IPv4Address
    return out


def _is_denied_address(addr: "ipaddress._BaseAddress") -> bool:
    """Return True if a single (already-normalized) address must be blocked.

    Allow-list semantics: permit loopback (127/8, ::1), RFC1918 in-VPC private
    (10/8, 172.16/12, 192.168/16) and globally-routable addresses. Reject
    link-local (169.254.0.0/16, fe80::/10), the IMDS IPs, and anything else that
    is NOT global (e.g. other reserved/non-global ranges) — closing the gap where
    a non-global address slips through because it isn't an explicit deny entry.
    """
    if addr in _DENIED_IPS:                       # IMDS (IPv4 + IPv6)
        return True
    if addr.is_link_local:                         # 169.254.0.0/16 and fe80::/10  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return True
    if _is_local_address(addr):                    # allowed: local dev probes, in-VPC private
        return False
    if addr.is_global:                             # allowed: globally routable  # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return False
    return True                                    # reject other non-global ranges


def _is_local_address(addr: "ipaddress._BaseAddress") -> bool:
    """The non-global addresses the guard allows: loopback (127/8, ::1) for
    local dev probes and RFC1918 (10/8, 172.16/12, 192.168/16) for in-VPC
    targets."""
    if addr.is_loopback:                           # attribute/property access, not a function call  # nosemgrep: is-function-without-parentheses
        return True
    return addr.version == 4 and any(addr in net for net in _RFC1918_NETWORKS)


def _ip_literal(host) -> "ipaddress._BaseAddress | None":
    """``host`` as an address when it is written as one, else ``None``: the
    IPv4 / IPv6 forms (brackets and a zone id ignored), and the other IPv4
    spellings a resolver reads as an address without DNS (``2130706433``,
    ``127.1``). Pure (no DNS)."""
    text = str(host or "").strip().strip("[]").split("%", 1)[0]
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(text))
    except (OSError, ValueError):
        return None


def literal_address_problem(host, *, allow_local: bool = True) -> str:
    """Why the guard refuses ``host`` when it is written as an IP address, or
    ``""``: the classification :func:`validated_addresses` applies to every
    address a target resolves to (metadata, link-local and other non-global
    addresses, embedded IPv4 included). ``allow_local=False`` also refuses the
    loopback and RFC1918 addresses the guard allows for local probes and in-VPC
    targets, for a caller that only ever names a public host. A host NAME is
    ``""`` here: what it resolves to is checked only when it is connected to.
    Pure (no DNS); the reason names no address."""
    ip = _ip_literal(host)
    if ip is None:
        return ""
    try:
        _reject_if_denied(ip)
    except ConnectionTargetError as exc:
        return str(exc)
    if not allow_local and any(_is_local_address(a) for a in (ip, *_embedded_ipv4s(ip))):
        return "connection target is a loopback or private address"
    return ""


def _reject_if_denied(ip: "ipaddress._BaseAddress") -> None:
    """Raise ConnectionTargetError if ``ip`` (or an embedded IPv4) is denied.

    Before the deny comparison, any IPv4 embedded in an IPv6 literal (IPv4-mapped,
    IPv4-compatible, 6to4, Teredo) is extracted and checked too, so a form like
    ``::ffff:169.254.169.254`` cannot bypass the IPv4 metadata/link-local block.
    """
    for addr in (ip, *_embedded_ipv4s(ip)):
        if addr in _DENIED_IPS:
            raise ConnectionTargetError("connection target is a cloud metadata endpoint")
        if _is_denied_address(addr):
            raise ConnectionTargetError("connection target resolves to a denied address")


def validate_connection_target(url_or_host: str, *, connection_type: str) -> str:
    """Validate a connection target before any network probe; return the pinned IP.

    Order of checks:
      1. Scheme allowlist (:data:`SCHEME_ALLOWLIST`) — FIRST, before any resolution.
      2. Literal cloud-metadata host string.
      3. DNS resolution via :func:`socket.getaddrinfo` of EVERY returned address;
         each resolved IP is normalized through :func:`ipaddress.ip_address` and
         rejected if it is link-local (169.254.0.0/16, fe80::/10), an IMDS IP
         (169.254.169.254, fd00:ec2::254), or any non-global range that is not
         loopback/RFC1918. Embedded-IPv4 IPv6 forms (IPv4-mapped/compat, 6to4,
         Teredo) are normalized first so e.g. ``::ffff:169.254.169.254`` cannot
         slip past the IPv4 deny check. Alternate IPv4 encodings (integer/hex)
         are also caught because getaddrinfo + ip_address normalize them.

    Loopback and RFC1918 (10/8, 172.16/12, 192.168/16) are intentionally allowed
    (local dev probes and in-VPC private targets are expected). If a host does not
    resolve, the target is rejected rather than silently allowed.

    Scope (TOCTOU / DNS-rebinding): this validates the host's CURRENTLY-resolved
    addresses at call time. It does NOT by itself close the rebinding window: a
    name re-resolving to a denied address AFTER validation but before the socket
    connects would still be reachable. Full protection requires pinning the
    validated IP to the actual connection. The PostgreSQL probe connects with
    ``hostaddr=<pinned>`` (:func:`_probe_postgres`); the httpx probes
    and every credentialed connector client (``agents/pinned_http.py``)
    connect to the returned IP with the original Host header and TLS SNI. To
    support pinning, this function returns the first safe resolved IP as a
    string.

    Returns:
        The first safe resolved IP (string), suitable for pinning to the socket.

    Raises :class:`ConnectionTargetError`.
    """
    return validated_addresses(url_or_host, connection_type=connection_type)[0]


def validated_addresses(url_or_host: str, *, connection_type: str) -> list[str]:
    """Every address ``url_or_host`` resolves to now, in resolver order without
    repeats, after the checks of :func:`validate_connection_target` (one denied
    address refuses the whole target). A pinned client tries them in this
    order, as a plain connect by name would.

    Raises :class:`ConnectionTargetError`.
    """
    if not url_or_host or not str(url_or_host).strip():
        raise ConnectionTargetError(f"empty connection target ({connection_type})")
    target = str(url_or_host).strip()

    # (1) Scheme allowlist FIRST — before any name resolution.
    if "://" in target:
        scheme = (urlsplit(target).scheme or "").lower()
        if scheme not in SCHEME_ALLOWLIST:
            raise ConnectionTargetError(f"scheme '{scheme}' is not allowed")

    host, port = _extract_host_port(target)
    host_norm = host.strip("[]")
    if not host_norm:
        raise ConnectionTargetError("connection target has no host")

    # (2) Literal metadata host (e.g. metadata.google.internal) — fast reject.
    if host_norm in _METADATA_HOSTS or f"[{host_norm}]" in _METADATA_HOSTS:
        raise ConnectionTargetError("connection target is a cloud metadata endpoint")

    # (3) Resolve ALL addresses and reject any denied one (DNS-rebinding guard).
    try:
        infos = socket.getaddrinfo(host_norm, port or None, proto=0)
    except (socket.gaierror, OSError, UnicodeError) as exc:
        raise ConnectionTargetError("host did not resolve") from exc
    if not infos:
        raise ConnectionTargetError("host did not resolve")
    safe_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if "%" in ip_str:  # strip IPv6 zone id (e.g. fe80::1%eth0)
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _reject_if_denied(ip)
        if ip_str not in safe_ips:
            safe_ips.append(ip_str)
    if not safe_ips:
        raise ConnectionTargetError("host did not resolve")
    return safe_ips


def resolve_and_validate(url_or_host: str, *, connection_type: str) -> str:
    """Validate a target and return the pinned (validated) IP as a string.

    Thin, explicit sibling of :func:`validate_connection_target` for callers that
    want to pin the validated IP to the connection (e.g. ``hostaddr`` for PG),
    closing the DNS-rebinding TOCTOU window. Raises :class:`ConnectionTargetError`
    on any SSRF/scheme violation.
    """
    return validate_connection_target(url_or_host, connection_type=connection_type)


def mask_connection(cfg: dict) -> dict:
    """Return a NEW dict with secrets redacted (input never mutated).

    Redacts ``token``/``password``/``secret``/``api_key``/``key``/``client_id``/
    ``client_secret`` to ``"***"`` and rewrites inline ``user:pass@`` credentials
    in ``url``/``connection_string``.
    Recurses into nested dicts. Canonical masking helper.
    """
    if not isinstance(cfg, dict):
        return cfg
    masked: dict = {}
    for key, value in cfg.items():
        lkey = str(key).lower()
        if lkey in _REDACT_KEYS:
            masked[key] = "***"
        elif lkey in _URL_KEYS and isinstance(value, str):
            masked[key] = _mask_url(value)
        elif isinstance(value, dict):
            masked[key] = mask_connection(value)
        else:
            masked[key] = value
    return masked


# Display masking of userinfo: from '://' through the LAST '@' of the
# authority, which ends at the first '/', '?', '#' or whitespace, as URL
# parsers read it. When the authority holds no '@', the second alternative
# still masks through a later '@' before the first '/' (a password with a raw
# '?', '#' or space in it), so a malformed URL is masked at least as far as
# the first-'@' rule did. An '@' in the path stays.
_DISPLAY_USERINFO_RE = re.compile(r"://(?:[^/?#\s]*@|[^/]*@)")
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
_TRAILING_PUNCT = ".,;:!?)]}>'\""


def _mask_unparseable_urls(text: str) -> str:
    """*text* with every URL that does not parse cleanly replaced by
    ``scheme://***@``: its whole authority and the rest, since where its
    password ends is unknown. A URL is a whitespace-separated token's part from
    the scheme before its first ``://``; trailing punctuation is not part of it."""
    out: list[str] = []
    for part in re.split(r"(\s+)", text):
        sep = part.find("://")
        if sep > 0:
            begin = sep
            while begin > 0 and part[begin - 1] in _SCHEME_CHARS:
                begin -= 1
            core = part[begin:].rstrip(_TRAILING_PUNCT)
            if begin < sep and not _parses_cleanly(core):
                part = part[:sep] + "://***@" + part[begin + len(core):]
        out.append(part)
    return "".join(out)


def _mask_url(value: str) -> str:
    """Mask inline ``user:pass@`` credentials AND secret query-string params.

    The userinfo runs through the LAST ``@`` of the authority, as URL parsers
    read it, so a password with a raw ``@`` or ``:`` is masked whole; a
    malformed URL is masked at least as far as the first-``@`` rule did
    (:data:`_DISPLAY_USERINFO_RE`), and one that still does not parse (a raw
    '/', '?' or '#' in a password, or what the at-rest stripper kept of one) is
    masked from its authority on. Query params named in
    :data:`_REDACT_QUERY_PARAMS` (api_key/token/key/password/secret/
    access_token) have their values replaced with ``***`` so a token carried in
    the URL query string is not leaked into logs/audit output.
    """
    masked = _mask_unparseable_urls(_DISPLAY_USERINFO_RE.sub("://***@", value))
    if "?" not in masked:
        return masked
    try:
        parts = urlsplit(masked)
    except ValueError:
        return masked
    if not parts.query:
        return masked
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in _REDACT_QUERY_PARAMS for k, _ in pairs):
        return masked
    redacted = [(k, "***" if k.lower() in _REDACT_QUERY_PARAMS else v) for k, v in pairs]
    return urlunsplit(parts._replace(query=urlencode(redacted)))


def _strip_url_credentials(url: str) -> str:
    """Return ``url`` with inline credentials REMOVED (not masked) for at-rest storage.

    Unlike :func:`_mask_url` (which replaces secrets with ``***`` for display/audit),
    this DROPS them so nothing secret is persisted: the ``user:pass@`` userinfo is
    deleted from the netloc and any secret-bearing query param
    (:data:`_REDACT_QUERY_PARAMS`) is removed entirely. Used before the at-rest
    node MERGE and the catalog persist so a credential carried inline in a
    ``url``/``base_url`` is never stored in cleartext. Non-URL inputs are
    returned unchanged. A URL that does not parse cleanly (registration refuses
    one, :func:`credential_url_error`; an older entry may hold one) becomes
    ``scheme://***@``: where its password ends is unknown, so nothing of it is
    kept, and the masked marker tells a client it is not a usable URL.
    """
    if not url or "://" not in str(url):
        return url
    if not _parses_cleanly(str(url)):
        return str(url).split("://", 1)[0] + "://***@"
    parts = urlsplit(str(url))
    netloc = parts.netloc
    if "@" in netloc:  # drop any inline user:pass@ userinfo (through the last '@')
        netloc = netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                if k.lower() not in _REDACT_QUERY_PARAMS]
        query = urlencode(kept)
    return urlunsplit(parts._replace(netloc=netloc, query=query))


_LOG_AUTHORITY_RUN_RE = re.compile(r"[^/?#\s]*")


def _log_userinfo_end(text: str, sep: int, limit: int) -> int:
    """Where a log line's mask of the URL at *sep* ends (after an '@'), or -1.

    Through the LAST '@' on the URL's line before the next '://' (*limit*):
    whatever a password holds, raw whitespace included, cannot end the mask
    early. When the line holds no '@' and the URL's authority runs to the line
    end (a raw line break in the password), through the last '@' on the next
    line instead.
    """
    begin = sep + 3
    newline = text.find("\n", begin, limit)
    at = text.rfind("@", begin, limit if newline < 0 else newline)
    if at >= 0:
        return at + 1
    if newline < 0:
        return -1
    run_end = _LOG_AUTHORITY_RUN_RE.match(text, begin, newline).end()
    line_end = newline
    while line_end > run_end and text[line_end - 1] in " \t\r":
        line_end -= 1
    if run_end < line_end:
        return -1
    after = text.find("\n", newline + 1, limit)
    at = text.rfind("@", newline + 1, limit if after < 0 else after)
    return at + 1 if at >= 0 else -1


def mask_url_credentials_for_log(text) -> str:
    """*text* with every URL's credentials masked conservatively, for a log
    line or an error detail.

    From each ``://`` through the last ``@`` on its line before the next
    ``://`` (:func:`_log_userinfo_end`): a raw '/', '?', '#', '@', ':' or
    whitespace in a password cannot end the mask early, and one raw line break
    in it moves the mask on to the next line. An '@' in a path or query, or in
    prose later on the line, is masked too, which a log line can afford. Each
    URL token then gets :func:`_mask_url` (secret query parameters, a URL that
    does not parse).
    """
    text = str(text)
    out: list[str] = []
    pos = 0
    while True:
        sep = text.find("://", pos)
        if sep < 0:
            break
        limit = text.find("://", sep + 3)
        end = _log_userinfo_end(text, sep, len(text) if limit < 0 else limit)
        if end < 0:
            out.append(text[pos:sep + 3])
            pos = sep + 3
        else:
            out.append(text[pos:sep] + "://***@")
            pos = end
    out.append(text[pos:])
    return "".join(_mask_url(part) if "://" in part else part
                   for part in re.split(r"(\s+)", "".join(out)))


def mask_stored_url(value: str) -> str:
    """A stored connection URL as it is served or logged.

    Through the FIRST '@' after ``://`` (the rule the catalog always served,
    which also hides a password with a raw '/' in it), then :func:`_mask_url`
    (through the last '@' of the authority; a URL that does not parse cleanly
    is masked from its authority on). A masked value keeps the ``://***@``
    marker clients use to tell it is not a usable URL.
    """
    text = str(value)
    out: list[str] = []
    pos = 0
    while True:
        sep = text.find("://", pos)
        if sep < 0:
            break
        at = text.find("@", sep + 3)
        if at < 0:
            break
        if at == sep + 3:   # nothing between: no userinfo at this '://'
            out.append(text[pos:sep + 3])
            pos = sep + 3
            continue
        out.append(text[pos:sep] + "://***@")
        pos = at + 1
    out.append(text[pos:])
    return _mask_url("".join(out))


def _safe_detail(text, resolved: dict) -> str:
    """Scrub a probe/error string so no secret value can leak into ``detail``:
    URL credentials (:func:`mask_url_credentials_for_log`), Authorization and
    secret-key values (``response_integrity_security.redact_credential_values``)
    and the resolved config's own secret values."""
    out = redact_credential_values(mask_url_credentials_for_log(str(text)))
    for key, value in (resolved or {}).items():
        if str(key).lower() in _REDACT_KEYS and value and isinstance(value, str):
            out = out.replace(value, "***")
    return out
