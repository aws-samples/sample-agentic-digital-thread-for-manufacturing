# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL client for this sample: the DB-API calls the code makes, over pg8000.

pg8000 (BSD-3-Clause, pure Python) is the wire driver. This module gives it the
calling conventions the sample's database code is written to:

* ``connect(dsn, **kwargs)`` takes a libpq connection string or URI plus libpq
  keywords (``host``, ``hostaddr``, ``port``, ``dbname``/``database``, ``user``,
  ``password``, ``connect_timeout``, ``options``, ``application_name``,
  ``sslmode``, ``sslrootcert``, ``keepalives*``, ``tcp_user_timeout``); keywords
  win over the string.
  An unknown keyword is refused. ``hostaddr`` is the address dialled, ``host``
  the name TLS checks. ``connect_timeout`` (else ``PGCONNECT_TIMEOUT``, as in
  libpq) bounds connecting and logging in only.
* TLS follows libpq. ``sslmode`` and ``sslrootcert`` come from the keywords,
  else ``PGSSLMODE`` / ``PGSSLROOTCERT``; the root certificate file defaults
  to ``~/.postgresql/root.crt``, and ``sslrootcert=system`` uses the system's
  trusted roots (verify-full only). ``verify-full`` checks the issuer and the
  host name, ``verify-ca`` the issuer. ``require``, ``prefer`` (the default)
  and ``allow`` check the issuer when the root certificate file exists, else
  they encrypt without checking. ``prefer`` falls back to plain text when the
  TLS attempt fails or the server answers it with an error; ``allow`` tries
  plain text first and TLS after a server error. A mode that does not check
  the server certificate is open to a man-in-the-middle and is not for
  production use: set ``verify-full`` with a root certificate file (for
  Amazon RDS, the RDS CA bundle) to check it.
* Parameters are ``%s`` / ``%(name)s`` and always reach the server as bound
  parameters, never as SQL text. With parameters, ``%%`` is a literal ``%``. A
  placeholder inside a quoted literal, quoted identifier or comment is refused
  (the server cannot bind it there): bind the whole value instead. With no
  parameters (``None``) the text is sent unchanged as one simple query.
* A bound number, bool or date/time value carries its type (``int4``/``int8``/
  ``numeric``, ``bool``, ``timestamptz``...); a string, a list and ``None`` are
  typed by the server from where they are used.
* ``with conn:`` commits on success and rolls back on an error, and leaves the
  connection open; ``with conn.cursor() as cur:`` closes the cursor. Outside
  autocommit the first statement opens a transaction.
* Errors are the DB-API classes below, chosen by SQLSTATE class, with
  ``pgcode`` (the SQLSTATE) and ``pgerror``. A failure to connect, log in or
  keep the connection is an ``OperationalError``.
* Rows are tuples (dicts with ``cursor_factory=RealDictCursor``) and are read
  whole. ``uuid``, ``inet`` and ``cidr`` come back as ``str``, ``interval`` as
  ``timedelta`` (a month is 30 days, a year 365).
* ``sql`` composes statements that need identifiers. ``Identifier`` is always
  double-quoted. ``Literal`` renders on the client as PostgreSQL's own
  ``quote_literal`` does: a string with a backslash becomes ``E'...'`` with the
  backslash doubled, so its meaning never depends on the session's
  ``standard_conforming_strings``; NUL is refused.
* One connection may be shared by threads: its calls are serialized.

The module has one identical copy in each separately packaged component (app,
ERP simulator, database-init Lambda, Lambda layer); a repository test checks
that the copies match.
"""

import base64
import datetime as _dt
import decimal
import hashlib
import hmac
import os
import re
import secrets
import socket
import ssl
import string
import struct
import threading
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

import pg8000.core as _core
import pg8000.dbapi as _dbapi
import pg8000.exceptions as _pgexc
from pg8000.converters import string_array_in as _string_array_in

apilevel = "2.0"
threadsafety = 2
paramstyle = "pyformat"


# ------------------------------------------------------------------ errors ---

class Error(Exception):
    """Base class of every error this module raises."""

    def __init__(self, *args, pgcode: Optional[str] = None, pgerror: Optional[str] = None):
        super().__init__(*args)
        self.pgcode = pgcode
        self.pgerror = pgerror


class InterfaceError(Error):
    """The client was misused (a closed connection or cursor)."""


class DatabaseError(Error):
    """An error the server reported."""


class DataError(DatabaseError):
    """SQLSTATE class 22."""


class OperationalError(DatabaseError):
    """Connecting, logging in or the session failed (classes 08, 28, 53-58...)."""


class IntegrityError(DatabaseError):
    """SQLSTATE class 23."""


class InternalError(DatabaseError):
    """SQLSTATE classes 24-2F, 38-3B, XX..."""


class ProgrammingError(DatabaseError):
    """SQLSTATE classes 42, 3D, 3F... and bad statements or parameters."""


class NotSupportedError(DatabaseError):
    """SQLSTATE class 0A, and features this module leaves out."""


class QueryCanceledError(OperationalError):
    """SQLSTATE 57014 (``statement_timeout``, a cancel)."""


class TransactionRollbackError(OperationalError):
    """SQLSTATE class 40 (serialization failure, deadlock)."""


class PoolError(Error):
    """The connection pool is exhausted or closed."""


#: SQLSTATE class (its first two characters) -> error class, the usual
#: mapping of libpq-based Python drivers.
_CLASS_BY_SQLSTATE_CLASS = {
    "08": OperationalError, "0A": NotSupportedError,
    "20": ProgrammingError, "21": ProgrammingError, "22": DataError, "23": IntegrityError,
    "24": InternalError, "25": InternalError, "26": OperationalError, "27": OperationalError,
    "28": OperationalError, "2B": InternalError, "2D": InternalError, "2F": InternalError,
    "34": OperationalError, "38": InternalError, "39": InternalError, "3B": InternalError,
    "3D": ProgrammingError, "3F": ProgrammingError, "40": TransactionRollbackError,
    "42": ProgrammingError, "44": ProgrammingError,
    "53": OperationalError, "54": OperationalError, "55": OperationalError,
    "57": OperationalError, "58": OperationalError,
    "F0": InternalError, "HV": OperationalError, "P0": InternalError, "XX": InternalError,
}


#: SQLSTATE -> PostgreSQL condition name (PostgreSQL "Appendix A. Error
#: Codes"), for the conditions this sample can meet. Each gets a class named in
#: CamelCase (``undefined_table`` -> ``UndefinedTable``) under its SQLSTATE
#: class's DB-API class, as libpq-based drivers name them: callers report and branch on
#: ``type(exc).__name__``. Other SQLSTATEs raise the DB-API class itself.
_CONDITIONS = """
08000 connection_exception          08003 connection_does_not_exist
08006 connection_failure            08001 sqlclient_unable_to_establish_sqlconnection
08004 sqlserver_rejected_establishment_of_sqlconnection
08P01 protocol_violation            0A000 feature_not_supported
21000 cardinality_violation         22000 data_exception
22001 string_data_right_truncation  22003 numeric_value_out_of_range
22004 null_value_not_allowed        22007 invalid_datetime_format
22008 datetime_field_overflow       22012 division_by_zero
22015 interval_field_overflow       22018 invalid_character_value_for_cast
22021 character_not_in_repertoire   22023 invalid_parameter_value
2201B invalid_regular_expression    2202E array_subscript_error
22P02 invalid_text_representation   22P03 invalid_binary_representation
22P05 untranslatable_character      22032 invalid_json_text
23000 integrity_constraint_violation 23502 not_null_violation
23503 foreign_key_violation         23505 unique_violation
23514 check_violation               23P01 exclusion_violation
25000 invalid_transaction_state     25001 active_sql_transaction
25006 read_only_sql_transaction     25P01 no_active_sql_transaction
25P02 in_failed_sql_transaction     25P03 idle_in_transaction_session_timeout
28000 invalid_authorization_specification 28P01 invalid_password
2BP01 dependent_objects_still_exist 2D000 invalid_transaction_termination
34000 invalid_cursor_name           3D000 invalid_catalog_name
3F000 invalid_schema_name           40000 transaction_rollback
40001 serialization_failure         40P01 deadlock_detected
42000 syntax_error_or_access_rule_violation 42501 insufficient_privilege
42601 syntax_error                  42602 invalid_name
42622 name_too_long                 42701 duplicate_column
42702 ambiguous_column              42703 undefined_column
42704 undefined_object              42710 duplicate_object
42712 duplicate_alias               42723 duplicate_function
42725 ambiguous_function            42803 grouping_error
42804 datatype_mismatch             42809 wrong_object_type
42830 invalid_foreign_key           42846 cannot_coerce
42883 undefined_function            42939 reserved_name
42P01 undefined_table               42P02 undefined_parameter
42P04 duplicate_database            42P06 duplicate_schema
42P07 duplicate_table               42P08 ambiguous_parameter
42P09 ambiguous_alias               42P10 invalid_column_reference
42P18 indeterminate_datatype        42P20 windowing_error
53000 insufficient_resources        53100 disk_full
53200 out_of_memory                 53300 too_many_connections
54000 program_limit_exceeded        54001 statement_too_complex
55000 object_not_in_prerequisite_state 55006 object_in_use
55P03 lock_not_available            57000 operator_intervention
57014 query_canceled                57P01 admin_shutdown
57P02 crash_shutdown                57P03 cannot_connect_now
58000 system_error                  P0001 raise_exception
P0002 no_data_found                 P0003 too_many_rows
XX000 internal_error
"""


def _condition_classes() -> Dict[str, type]:
    words = _CONDITIONS.split()
    classes = {}
    for code, name in zip(words[::2], words[1::2]):
        base = QueryCanceledError if code == "57014" else \
            _CLASS_BY_SQLSTATE_CLASS.get(code[:2], DatabaseError)
        cls_name = "".join(part.capitalize() for part in name.split("_"))
        classes[code] = type(cls_name, (base,), {"__module__": __name__, "sqlstate": code})
    return classes


_CLASS_BY_SQLSTATE = _condition_classes()


class _ErrorsNamespace:
    """``pgdriver.errors``: the condition classes by name (``errors.UniqueViolation``)."""


errors = _ErrorsNamespace()
for _cls in _CLASS_BY_SQLSTATE.values():
    setattr(errors, _cls.__name__, _cls)
del _cls


def _error_line(query: str, position) -> str:
    """libpq's ``LINE n:`` excerpt with a caret under the 1-based *position*."""
    try:
        offset = int(position) - 1
    except (TypeError, ValueError):
        return ""
    if not query or offset < 0 or offset > len(query):
        return ""
    start = query.rfind("\n", 0, offset) + 1
    end = query.find("\n", offset)
    end = len(query) if end < 0 else end
    prefix = f"LINE {query.count(chr(10), 0, start) + 1}: "
    return f"{prefix}{query[start:end]}\n{' ' * (len(prefix) + offset - start)}^\n"


def _server_error(fields: Mapping, query: Optional[str], cls=None) -> DatabaseError:
    code = fields.get("C") or ""
    if cls is None:
        cls = _CLASS_BY_SQLSTATE.get(code) or _CLASS_BY_SQLSTATE_CLASS.get(code[:2], DatabaseError)
    text = (fields.get("M") or "") + "\n" + _error_line(query or "", fields.get("P"))
    for key, label in (("D", "DETAIL"), ("H", "HINT"), ("W", "CONTEXT")):
        if fields.get(key):
            text += f"{label}:  {fields[key]}\n"
    severity = fields.get("V") or fields.get("S") or "ERROR"
    return cls(text, pgcode=code or None, pgerror=f"{severity}:  {text}")


def _translate(exc: BaseException, query: Optional[str] = None) -> BaseException:
    """This module's error for a pg8000 or socket error (others unchanged)."""
    if isinstance(exc, Error):
        return exc
    if isinstance(exc, _pgexc.DatabaseError) and exc.args and isinstance(exc.args[0], Mapping):
        return _server_error(exc.args[0], query)
    if isinstance(exc, _pgexc.InterfaceError) and exc.args == ("in failed transaction block",):
        return InternalError("current transaction is aborted, commands ignored until end of "
                             "transaction block\n", pgcode="25P02")
    if isinstance(exc, (_pgexc.InterfaceError, OSError, EOFError)):
        return OperationalError("server closed the connection unexpectedly "
                                f"({type(exc).__name__})\n")
    return exc


# ------------------------------------------------------------- composition ---

class Composable:
    """A piece of a statement; ``as_string(context)`` renders it (the context,
    a connection or cursor, is accepted and not needed)."""

    def as_string(self, context=None) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _key(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def __add__(self, other):
        if isinstance(other, Composed):
            return Composed([self, *other.seq])
        if isinstance(other, Composable):
            return Composed([self, other])
        return NotImplemented

    def __eq__(self, other):
        return type(self) is type(other) and self._key() == other._key()

    def __hash__(self):
        return hash((type(self), self._key()))


class Composed(Composable):
    """:class:`Composable` pieces rendered one after another."""

    def __init__(self, seq):
        self.seq = list(seq)
        for item in self.seq:
            if not isinstance(item, Composable):
                raise TypeError(f"Composed elements must be Composable, got {item!r}")

    def as_string(self, context=None) -> str:
        return "".join(item.as_string(context) for item in self.seq)

    def __add__(self, other):
        if isinstance(other, Composed):
            return Composed(self.seq + other.seq)
        if isinstance(other, Composable):
            return Composed(self.seq + [other])
        return NotImplemented

    def join(self, joiner) -> "Composed":
        return (SQL(joiner) if isinstance(joiner, str) else joiner).join(self.seq)

    def _key(self):
        return tuple(self.seq)

    def __repr__(self):
        return f"Composed({self.seq!r})"


class SQL(Composable):
    """Statement text written in code. Never wrap a value from input."""

    def __init__(self, string_: str):
        if not isinstance(string_, str):
            raise TypeError("SQL values must be strings")
        self.string = string_

    def as_string(self, context=None) -> str:
        return self.string

    def format(self, *args, **kwargs) -> Composed:
        """``{}``, ``{0}`` and ``{name}`` replaced by Composable arguments
        (``{{``/``}}`` are braces); no format specs or conversions."""
        parts = []
        auto = 0
        for literal, field, spec, conversion in string.Formatter().parse(self.string):
            if spec or conversion:
                raise ValueError("no format specification or conversion allowed")
            if literal:
                parts.append(SQL(literal))
            if field is None:
                continue
            if field == "":
                value = args[auto]
                auto += 1
            elif field.isdigit():
                value = args[int(field)]
            else:
                value = kwargs[field]
            if not isinstance(value, Composable):
                raise TypeError(f"format() arguments must be Composable, got {value!r}")
            parts.append(value)
        return Composed(parts)

    def join(self, seq) -> Composed:
        parts = []
        for i, item in enumerate(seq):
            if i:
                parts.append(self)
            parts.append(item)
        return Composed(parts)

    def _key(self):
        return self.string

    def __repr__(self):
        return f"SQL({self.string!r})"


def quote_ident(name: str) -> str:
    """*name* as a double-quoted identifier (``"`` doubled; empty or NUL refused)."""
    if not isinstance(name, str):
        raise TypeError("SQL identifier parts must be strings")
    if not name or "\x00" in name:
        raise ValueError("an SQL identifier cannot be empty or contain NUL")
    return '"' + name.replace('"', '""') + '"'


class Identifier(Composable):
    """A quoted identifier; several parts are joined with dots."""

    def __init__(self, *strings):
        if not strings:
            raise TypeError("Identifier cannot be empty")
        for part in strings:
            quote_ident(part)
        self.strings = strings

    @property
    def string(self):
        if len(self.strings) != 1:
            raise AttributeError("the Identifier has more than one part")
        return self.strings[0]

    def as_string(self, context=None) -> str:
        return ".".join(quote_ident(part) for part in self.strings)

    def _key(self):
        return self.strings

    def __repr__(self):
        return f"Identifier({', '.join(map(repr, self.strings))})"


def _finite(value) -> bool:
    if isinstance(value, decimal.Decimal):
        return value.is_finite()
    return value == value and value not in (float("inf"), float("-inf"))


def quote_literal(value) -> str:
    """*value* as an SQL literal, the way PostgreSQL's ``quote_literal`` writes it.

    ``None`` is ``NULL``; a bool, an int and a finite float or Decimal are
    bare. A string has ``'`` doubled, and when it holds a backslash it is an
    ``E'...'`` string with the backslash doubled, which reads the same whatever
    ``standard_conforming_strings`` is. NUL and other types are refused."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, (float, decimal.Decimal)):
        if not _finite(value):
            raise ProgrammingError("a non-finite number has no literal here; bind it instead")
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("an SQL literal cannot contain NUL")
        quoted = value.replace("'", "''")
        if "\\" in quoted:
            return "E'" + quoted.replace("\\", "\\\\") + "'"
        return "'" + quoted + "'"
    raise ProgrammingError(f"can't render a {type(value).__name__} as an SQL literal")


class Literal(Composable):
    """A value written into the statement text, for DDL, which cannot bind."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    def as_string(self, context=None) -> str:
        return quote_literal(self.wrapped)

    def _key(self):
        return (type(self.wrapped), self.wrapped)

    def __repr__(self):
        return f"Literal({self.wrapped!r})"


class _SqlNamespace:
    """``pgdriver.sql``: the composition classes under one name."""

    Composable = Composable
    Composed = Composed
    SQL = SQL
    Identifier = Identifier
    Literal = Literal


sql = _SqlNamespace()


# --------------------------------------------------------------- parameters ---

_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$\u0080-\uffff]")
_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\uffff][A-Za-z0-9_\u0080-\uffff]*)?\$")
_NATIVE_PARAM = re.compile(r"\$[0-9]")


class _Scan:
    """``%s``/``%(name)s`` -> ``$n`` over one statement (see :func:`_scan`)."""

    def __init__(self, query: str, args):
        self.query, self.args = query, args
        self.named = isinstance(args, Mapping)
        self.names: Dict[str, int] = {}
        self.values: list = []
        self.used = 0

    def placeholder(self, i: int, quoted: bool) -> "tuple[str, int]":
        query, nxt = self.query, self.query[i + 1:i + 2]
        if nxt == "%":
            return "%", i + 2
        if nxt not in ("s", "("):
            raise ValueError(f"unsupported format character {nxt!r} at index {i}")
        if quoted:
            raise ProgrammingError(
                "a query parameter cannot sit inside a quoted literal, quoted identifier "
                "or comment; bind the whole value instead")
        # None is written as NULL, as SQL-text drivers write it: a bound NULL has no
        # type, and "%s IS NULL" could not be planned.
        if nxt == "s":
            if self.named:
                raise ProgrammingError("argument formats can't be mixed")
            if self.used >= len(self.args):
                raise IndexError("tuple index out of range")
            value = self.args[self.used]
            self.used += 1
            if value is None:
                return "NULL", i + 2
            self.values.append(value)
            return f"${len(self.values)}", i + 2
        end = query.find(")", i)
        if end < 0 or query[end + 1:end + 2] != "s":
            raise ValueError("incomplete placeholder: '%(' without ')s'")
        if not self.named:
            raise TypeError("format requires a mapping")
        name = query[i + 2:end]
        if self.args[name] is None:
            return "NULL", end + 2
        if name not in self.names:
            self.values.append(self.args[name])
            self.names[name] = len(self.values)
        return f"${self.names[name]}", end + 2


def _scan(query: str, args, backslash_quotes: bool = False):
    """``(text, values)``: *query* with each ``%s``/``%(name)s`` as ``$n`` and
    ``%%`` as ``%``, and the values in ``$n`` order. Quoted literals
    (``'..'``, ``E'..'``), quoted identifiers, dollar quotes and comments are
    tracked, and a placeholder inside one is refused. *backslash_quotes* is
    the session's ``standard_conforming_strings = off``: a backslash then
    escapes in every ``'..'`` literal, as PostgreSQL reads it."""
    scan = _Scan(query, args)
    out = []
    i, n = 0, len(query)
    lexer = (None, "", 0)  # (state, dollar tag, comment depth)
    while i < n:
        if query[i] == "%":
            text, i = scan.placeholder(i, lexer[0] is not None)
            out.append(text)
            continue
        lexer, step = _lex(query, i, lexer, backslash_quotes)
        out.append(query[i:i + step])
        i += step
    if not scan.named and scan.used < len(args):
        raise TypeError("not all arguments converted during string formatting")
    return "".join(out), scan.values


def _lex(query: str, i: int, lexer: tuple, backslash_quotes: bool = False) -> tuple:
    """``(lexer, step)`` after the character at *i*. The state is ``None``
    (plain SQL), ``"'"`` (a literal), ``"E"`` (a literal in which a backslash
    escapes: ``E''``, or any literal when *backslash_quotes*), ``'"'`` (a
    quoted identifier), ``"$"`` (a dollar quote), ``"--"`` or ``"/*"``."""
    state, tag, depth = lexer
    c = query[i]
    prev = query[i - 1] if i else ""
    if state is None:
        if c == "'":
            escape = backslash_quotes or (
                prev in ("e", "E") and (i < 2 or not _IDENT_CHAR.match(query[i - 2])))
            return ("E" if escape else "'", tag, depth), 1
        if c == '"':
            return ('"', tag, depth), 1
        if query.startswith("--", i):
            return ("--", tag, depth), 1
        if query.startswith("/*", i):
            return ("/*", tag, 1), 2
        if c == "$" and not (prev and _IDENT_CHAR.match(prev)):
            if _NATIVE_PARAM.match(query, i):
                raise ProgrammingError("use %s placeholders, not $n, with this driver")
            m = _TAG.match(query, i)
            if m:
                return ("$", m.group(0), depth), len(m.group(0))
        return lexer, 1
    if state in ("'", '"'):
        return ((None if c == state else state), tag, depth), 1
    if state == "E":
        if c == "\\":
            return lexer, 2
        return ((None if c == "'" else state), tag, depth), 1
    if state == "--":
        return ((None if c == "\n" else state), tag, depth), 1
    if state == "/*":
        if query.startswith("/*", i):
            return (state, tag, depth + 1), 2
        if query.startswith("*/", i):
            return ((None if depth == 1 else state), tag, depth - 1), 2
        return lexer, 1
    if query.startswith(tag, i):  # state == "$"
        return (None, tag, depth), len(tag)
    return lexer, 1


_INT4 = range(-2 ** 31, 2 ** 31)
_INT8 = range(-2 ** 63, 2 ** 63)


def _oid(value) -> int:
    """The parameter type OID for *value*: the type a SQL-text driver's literal gave
    the same value (0, "unknown", lets the server type it from context)."""
    if value is None or isinstance(value, (str, list)):
        return 0
    if isinstance(value, bool):
        return 16
    if isinstance(value, int):
        return 23 if value in _INT4 else 20 if value in _INT8 else 1700
    if isinstance(value, float):
        return 1700 if _finite(value) else 701
    if isinstance(value, decimal.Decimal):
        return 1700
    if isinstance(value, _dt.datetime):
        return 1114 if value.tzinfo is None else 1184
    if isinstance(value, _dt.date):
        return 1082
    if isinstance(value, _dt.time):
        return 1083 if value.tzinfo is None else 1266
    if isinstance(value, _dt.timedelta):
        return 1186
    if isinstance(value, (bytes, bytearray, memoryview)):
        return 17
    if isinstance(value, uuid.UUID):
        return 2950
    raise ProgrammingError(f"can't adapt type '{type(value).__name__}'")


# ------------------------------------------------------------- row values ---

_INTERVAL_PART = re.compile(r"([+-]?\d+) (year|mon|day)s?")
_INTERVAL_TIME = re.compile(r"([+-])?(\d+):(\d+):(\d+(?:\.\d+)?)")


def _interval_in(text: str) -> _dt.timedelta:
    """An ``interval`` (IntervalStyle ``postgres``) as a timedelta; a year is
    365 days and a month 30."""
    days = sum(int(amount) * {"year": 365, "mon": 30, "day": 1}[unit]
               for amount, unit in _INTERVAL_PART.findall(text))
    seconds = 0.0
    m = _INTERVAL_TIME.search(text)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        seconds = sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4)))
    return _dt.timedelta(days=days, seconds=seconds)


#: Row converters that differ from pg8000's (type OID -> function).
_IN_ADAPTERS = {
    2950: str, 2951: _string_array_in,   # uuid, uuid[]
    869: str, 1041: _string_array_in,    # inet, inet[]
    650: str, 651: _string_array_in,     # cidr, cidr[]
    1186: _interval_in,                  # interval
    2249: str,                           # record
}


class _Wire(_dbapi.Connection):
    """pg8000's connection, keeping only the LAST statement's rows of a
    multi-statement simple query (as libpq does)."""

    def handle_ROW_DESCRIPTION(self, data, context):
        context.rows = None
        super().handle_ROW_DESCRIPTION(data, context)

    def close_unnamed_portal(self):
        """Close the portal a bound statement leaves open. Inside a
        transaction it would keep the statement's snapshot until the next
        statement, which holds back VACUUM and ``CREATE INDEX CONCURRENTLY``
        (a simple query leaves none). Uses pg8000's
        message helpers (pinned pg8000 1.31)."""
        self._send_message(_core.CLOSE, _core.PORTAL + _core.NULL_BYTE)
        _core._write(self._sock, _core.SYNC_MSG)
        _core._flush(self._sock)
        self.handle_messages(_core.Context(None))


# ------------------------------------------------------------- connections ---

TRANSACTION_STATUS_IDLE = 0
TRANSACTION_STATUS_ACTIVE = 1
TRANSACTION_STATUS_INTRANS = 2
TRANSACTION_STATUS_INERROR = 3
TRANSACTION_STATUS_UNKNOWN = 4

_CONNECT_KEYS = frozenset({
    "host", "hostaddr", "port", "dbname", "user", "password", "connect_timeout",
    "options", "application_name", "sslmode", "sslrootcert",
    "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
    "tcp_user_timeout",
})
_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
_KEYWORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_keyword_dsn(dsn: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    i, n = 0, len(dsn)
    while True:
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n:
            return out
        m = _KEYWORD.match(dsn, i)
        if not m:
            raise ProgrammingError("invalid dsn: missing keyword")
        key, i = m.group(0), m.end()
        while i < n and dsn[i].isspace():
            i += 1
        if i >= n or dsn[i] != "=":
            raise ProgrammingError(f'invalid dsn: missing "=" after "{key}"')
        i += 1
        while i < n and dsn[i].isspace():
            i += 1
        quoted = i < n and dsn[i] == "'"
        i += 1 if quoted else 0
        value = []
        while True:
            if i >= n:
                if quoted:
                    raise ProgrammingError("invalid dsn: unterminated quoted string")
                break
            c = dsn[i]
            if c == "\\" and i + 1 < n:
                value.append(dsn[i + 1])
                i += 2
                continue
            if (quoted and c == "'") or (not quoted and c.isspace()):
                i += 1 if quoted else 0
                break
            value.append(c)
            i += 1
        out[key] = "".join(value)


def _parse_uri(dsn: str) -> Dict[str, str]:
    parts = urlsplit(dsn)
    out: Dict[str, str] = {}
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, netloc = netloc.rsplit("@", 1)
        user, has_password, password = userinfo.partition(":")
        if user:
            out["user"] = unquote(user)
        if has_password:
            out["password"] = unquote(password)
    if "," in netloc:
        raise ProgrammingError("invalid dsn: more than one host is not supported")
    if netloc.startswith("["):
        host, _, rest = netloc[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = netloc.partition(":")
    if host:
        out["host"] = unquote(host)
    if port:
        out["port"] = port
    if parts.path.lstrip("/"):
        out["dbname"] = unquote(parts.path.lstrip("/"))
    out.update(parse_qsl(parts.query, keep_blank_values=True))
    return out


def parse_dsn(dsn: str) -> Dict[str, str]:
    """A libpq connection string (``key=value ...``) or URI
    (``postgres[ql]://...``) as a dict of strings."""
    if not isinstance(dsn, str):
        raise TypeError("the dsn must be a string")
    if dsn.startswith(("postgresql://", "postgres://")):
        return _parse_uri(dsn)
    return _parse_keyword_dsn(dsn)


def _quote_dsn_value(value) -> str:
    text = str(value)
    if text and not re.search(r"[\s'\\]", text):
        return text
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def make_dsn(dsn: Optional[str] = None, **kwargs) -> str:
    """A ``key=value`` connection string: *dsn*'s parameters with *kwargs* on top."""
    params = _merge_params(dsn, kwargs)
    return " ".join(f"{k}={_quote_dsn_value(v)}" for k, v in params.items())


def _merge_params(dsn: Optional[str], kwargs: Mapping) -> Dict[str, Any]:
    params: Dict[str, Any] = parse_dsn(dsn) if dsn else {}
    extra = {k: v for k, v in kwargs.items() if v is not None}
    if "database" in extra:
        extra.setdefault("dbname", extra.pop("database"))
        extra.pop("database", None)
    params.update(extra)
    return params


def _tls_settings(params: Mapping) -> tuple:
    """``(sslmode, sslrootcert)`` as libpq reads them: the keyword, else
    ``PGSSLMODE`` / ``PGSSLROOTCERT``, else libpq's default (``prefer``;
    ``verify-full`` with ``sslrootcert=system``)."""
    # A keyword wins over the environment even when it is empty (libpq):
    # an empty sslmode is invalid, an empty sslrootcert means the default file.
    rootcert = params.get("sslrootcert", os.environ.get("PGSSLROOTCERT"))
    rootcert = str(rootcert or os.path.expanduser("~/.postgresql/root.crt"))
    mode = params.get("sslmode", os.environ.get("PGSSLMODE"))
    if mode is None:
        mode = "verify-full" if rootcert == "system" else "prefer"
    mode = str(mode)
    if mode not in _SSLMODES:
        raise ProgrammingError(f'invalid sslmode value: "{mode}"')
    if rootcert == "system" and mode != "verify-full":
        raise OperationalError(f'weak sslmode "{mode}" may not be used with '
                               'sslrootcert=system (use "verify-full")\n')
    return mode, rootcert


class _TLSError(OperationalError):
    """The TLS attempt failed (no TLS on the server, a refused certificate,
    an unreadable root file); ``prefer`` then retries in plain text."""


def _ssl_context(mode: str, rootcert: str):
    """pg8000's ``ssl_context`` for a libpq sslmode's TLS attempt: ``False``
    plain text, ``None`` TLS when the server offers it, unchecked, ``True``
    TLS without checking the server certificate, or a context that checks it."""
    if mode == "disable":
        return False
    if rootcert != "system" and not os.path.exists(rootcert):
        if mode == "prefer":
            # pg8000's opportunistic TLS: unchecked, and plain text on the
            # same socket when the server has no TLS (libpq).
            return None
        if mode in ("allow", "require"):
            # libpq: encrypted, server certificate not checked. Not for
            # production use; use verify-full with a root certificate.
            return True
        raise OperationalError(
            f'root certificate file "{rootcert}" does not exist\nEither provide the file, '
            "use the system's trusted roots with sslrootcert=system, or change sslmode "
            "to disable server certificate verification.\n")
    # verify-full checks the issuer and the host name. Every other mode checks
    # the issuer when a root certificate file exists, as libpq does.
    try:
        ctx = ssl.create_default_context(cafile=None if rootcert == "system" else rootcert)
    except (ssl.SSLError, OSError) as exc:
        raise _TLSError(f'could not read root certificate file "{rootcert}": '
                        f"{getattr(exc, 'reason', None) or exc}\n") from None
    ctx.check_hostname = mode == "verify-full"
    return ctx


def _open_socket(address: str, port: int, timeout: Optional[float], params: Mapping):
    sock = socket.create_connection((address, port), timeout)
    # libpq turns Nagle off on every TCP connection. With it on, the TLS
    # login's small writes wait for the server's delayed ACK, about 50 ms
    # per connection.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if int(params.get("keepalives", 1)):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for key, opt in (("keepalives_idle", "TCP_KEEPIDLE"),
                         ("keepalives_interval", "TCP_KEEPINTVL"),
                         ("keepalives_count", "TCP_KEEPCNT")):
            if key in params and hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), int(params[key]))
    if params.get("tcp_user_timeout") and hasattr(socket, "TCP_USER_TIMEOUT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, int(params["tcp_user_timeout"]))
    return sock


def _connect_params(dsn: Optional[str], kwargs: Mapping) -> tuple:
    """``(params, connect timeout in seconds or None)``, checked: unknown
    keywords, a Unix socket, a bad port or sslmode and a missing user are refused."""
    params = _merge_params(dsn, kwargs)
    unknown = sorted(set(params) - _CONNECT_KEYS)
    if unknown:
        raise ProgrammingError(f'invalid connection option "{unknown[0]}"')
    host = str(params.get("host") or params.get("hostaddr") or "localhost")
    if host.startswith("/"):
        raise NotSupportedError("Unix-domain socket connections are not supported")
    try:
        port = int(params.get("port") or 5432)
    except ValueError:
        raise ProgrammingError(f'invalid port number: "{params.get("port")}"') from None
    if not params.get("user"):
        raise ProgrammingError("no user name given")
    mode, rootcert = _tls_settings(params)
    seconds = int(params.get("connect_timeout") or os.environ.get("PGCONNECT_TIMEOUT") or 0)
    params.update(host=host, port=port, sslmode=mode, sslrootcert=rootcert)
    return params, (float(max(seconds, 2)) if seconds > 0 else None)  # libpq's 2 s floor


def connect(dsn: Optional[str] = None, *, cursor_factory=None, **kwargs) -> "Connection":
    """A connection from a libpq connection string or URI and/or keywords
    (keywords win). See the module docstring for the keywords taken."""
    params, timeout = _connect_params(dsn, kwargs)
    mode, rootcert = params["sslmode"], params["sslrootcert"]
    if mode == "prefer":
        # libpq's prefer: TLS first, then plain text when the TLS attempt
        # fails or the server answers it with an error.
        try:
            return _login(params, timeout, _ssl_context(mode, rootcert), cursor_factory)
        except OperationalError as exc:
            if not (isinstance(exc, _TLSError) or _other_encryption_may_help(exc)):
                raise
        return _login(params, timeout, False, cursor_factory)
    try:
        if mode == "allow":
            # libpq: plain text first, then TLS after an error from the server.
            try:
                return _login(params, timeout, False, cursor_factory)
            except OperationalError as exc:
                if not _other_encryption_may_help(exc):
                    raise
        return _login(params, timeout, _ssl_context(mode, rootcert), cursor_factory)
    except _TLSError as exc:  # only prefer uses the fallback signal
        raise OperationalError(str(exc)) from None


def _other_encryption_may_help(exc: OperationalError) -> bool:
    """libpq tries the other encryption after an error from the server,
    except 57P03 (the server cannot accept connections now)."""
    return exc.pgcode is not None and exc.pgcode != "57P03"


def _tls_error(exc: BaseException, host: str) -> Optional[str]:
    """libpq's wording for a refused TLS handshake, or ``None``."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        if exc.verify_code in (62, 64):  # X509_V_ERR_HOSTNAME_MISMATCH, _IP_ADDRESS_MISMATCH
            return f'server certificate does not match host name "{host}"'
        return f"SSL error: certificate verify failed ({exc.verify_message})"
    if isinstance(exc, ssl.SSLError):
        return f"SSL error: {exc.reason or exc}"
    if isinstance(exc, _pgexc.InterfaceError) and exc.args == ("Server refuses SSL",):
        return "server does not support SSL, but SSL was required"
    return None


def _login(params: Mapping, timeout: Optional[float], ssl_context, cursor_factory) -> "Connection":
    host, port = params["host"], params["port"]
    try:
        sock = _open_socket(str(params.get("hostaddr") or host), port, timeout, params)
    except socket.timeout:
        raise OperationalError(f'connection to server at "{host}", port {port} failed: '
                               "timeout expired\n") from None
    except OSError as exc:
        raise OperationalError(f'connection to server at "{host}", port {port} failed: '
                               f"{exc.strerror or exc}\n") from None
    try:
        raw = _Wire(params["user"], host=host, database=params.get("dbname"), port=port,
                    password=params.get("password"), sock=sock, ssl_context=ssl_context,
                    application_name=params.get("application_name"),
                    startup_params={"options": str(params["options"])} if params.get("options") else None)
    except BaseException as exc:
        sock.close()
        tls = _tls_error(exc, host)
        if tls:
            raise _TLSError(f'connection to server at "{host}", port {port} failed: '
                            f"{tls}\n") from None
        err = _translate(exc)
        if err is exc:
            raise
        if isinstance(err, Error) and not isinstance(err, OperationalError):
            err = OperationalError(str(err), pgcode=err.pgcode, pgerror=err.pgerror)
        if isinstance(exc, socket.timeout) or "timed out" in str(exc):
            err = OperationalError(f'connection to server at "{host}", port {port} failed: '
                                   "timeout expired\n")
        raise err from None
    # pg8000 keeps the socket it wrapped here (pinned pg8000 1.31); the
    # connect timeout must not also bound every later query.
    getattr(raw, "_usock", sock).settimeout(None)
    return Connection(raw, params, cursor_factory=cursor_factory)


class RealDictCursor:
    """``cursor_factory`` marker: rows come back as dicts keyed by column name."""


class Column(tuple):
    """A ``cursor.description`` entry: ``(name, type_code, None...)``."""

    __slots__ = ()

    def __new__(cls, name, type_code):
        return super().__new__(cls, (name, type_code, None, None, None, None, None))

    name = property(lambda self: self[0])
    type_code = property(lambda self: self[1])


class Cursor:
    """A DB-API cursor over a :class:`Connection`."""

    arraysize = 1

    def __init__(self, conn: "Connection", as_dict: bool = False):
        self.connection = conn
        self._as_dict = as_dict
        self._rows = None
        self._pos = 0
        self.description = None
        self.rowcount = -1
        self.closed = False
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True
        self._rows = None

    def _check(self):
        if self.closed:
            raise InterfaceError("cursor already closed")
        self.connection._check()

    def execute(self, query, vars=None):
        self._check()
        if not isinstance(query, str) and callable(getattr(query, "as_string", None)):
            query = query.as_string(self.connection)  # any copy's composed statement
        if not isinstance(query, str):
            raise TypeError("the query must be a string or a composed statement")
        if vars is not None and (isinstance(vars, (str, bytes))
                                 or not isinstance(vars, (Sequence, Mapping))):
            raise TypeError("query parameters must be a sequence or a mapping")
        self.query, context = self.connection._run(query, vars)
        self._pos = 0
        self.rowcount = context.row_count
        if context.columns is None:
            self.description, self._rows = None, None
            return
        names = [c["name"] for c in context.columns]
        self.description = [Column(c["name"], c["type_oid"]) for c in context.columns]
        rows = context.rows or []
        self._rows = [dict(zip(names, r)) for r in rows] if self._as_dict else [tuple(r) for r in rows]

    def executemany(self, query, vars_list):
        total = 0
        for vars in vars_list:
            self.execute(query, vars)
            total += max(self.rowcount, 0)
        self.rowcount = total

    def _fetch(self, count):
        self._check()
        if self._rows is None:
            raise ProgrammingError("no results to fetch")
        end = len(self._rows) if count is None else self._pos + max(count, 0)
        rows = self._rows[self._pos:end]
        self._pos += len(rows)
        return rows

    def fetchone(self):
        rows = self._fetch(1)
        return rows[0] if rows else None

    def fetchmany(self, size=None):
        return self._fetch(self.arraysize if size is None else size)

    def fetchall(self):
        return self._fetch(None)

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def setinputsizes(self, sizes):  # DB-API; nothing to do
        pass

    def setoutputsize(self, size, column=None):  # DB-API; nothing to do
        pass


class Connection:
    """A DB-API connection with libpq session semantics (see the module docstring)."""

    Error, InterfaceError, DatabaseError, OperationalError = (
        Error, InterfaceError, DatabaseError, OperationalError)
    ProgrammingError, IntegrityError, DataError, InternalError, NotSupportedError = (
        ProgrammingError, IntegrityError, DataError, InternalError, NotSupportedError)

    def __init__(self, raw, params: Dict[str, Any], cursor_factory=None):
        self._raw = raw
        self._params = params
        self._address = str(params.get("hostaddr") or params["host"])
        self._lock = threading.RLock()
        self._autocommit = False
        self.cursor_factory = cursor_factory
        self.closed = 0  # 0 open, 1 closed, 2 broken
        for oid, func in _IN_ADAPTERS.items():
            raw.register_in_adapter(oid, func)

    def _check(self):
        if self.closed:
            raise InterfaceError("connection already closed")

    def _backslash_quotes(self) -> bool:
        """Whether the session reads a backslash in ``'..'`` as an escape: the
        server reports ``standard_conforming_strings`` on every change."""
        return self._raw.parameter_statuses.get("standard_conforming_strings") == "off"

    def _in_transaction(self) -> bool:
        # pg8000 keeps the last ReadyForQuery status here (pinned pg8000 1.31).
        status = getattr(self._raw, "_transaction_status", None)
        return status in (_core.IN_TRANSACTION, _core.IN_FAILED_TRANSACTION)

    def _call(self, func, *args, query=None, **kwargs):
        try:
            return func(*args, **kwargs)
        except BaseException as exc:
            err = _translate(exc, query)
            if err is exc:
                raise
            if isinstance(err, OperationalError) and not isinstance(exc, _pgexc.DatabaseError):
                self.closed = 2
            raise err from None

    def _simple(self, text: str):
        return self._call(self._raw.execute_simple, text, query=text)

    def _run(self, query: str, args):
        """``(sent text, pg8000 context)`` for *query* with *args* (None:
        the text as given). The scan reads the session's string mode under the
        same lock as the execution, so no other thread can change it between."""
        with self._lock:
            self._check()
            text, values = (query, None) if args is None else \
                _scan(query, args, self._backslash_quotes())
            if not self._autocommit and not self._in_transaction():
                self._simple("BEGIN")
            if not values:
                return text, self._simple(text)
            oids = tuple(_oid(v) for v in values)
            vals = tuple(v.tobytes() if isinstance(v, memoryview) else v for v in values)
            context = self._call(self._raw.execute_unnamed, text, vals=vals, oids=oids, query=text)
            if self._in_transaction():
                self._call(self._raw.close_unnamed_portal, query=text)
            return text, context

    def cancel(self):
        """Ask the server to cancel the statement this connection is running
        (a separate cancel request, as libpq sends; safe from another thread).
        Nothing happens when no statement is running."""
        key = getattr(self._raw, "_backend_key_data", None)  # pid + secret, pinned pg8000 1.31
        if self.closed or not key:
            return
        try:
            with socket.create_connection((self._address, self._params["port"]), 10) as sock:
                sock.sendall(struct.pack("!ii", 16, 80877102) + bytes(key))
        except OSError as exc:
            raise OperationalError(f"could not send the cancel request: {exc}\n") from None

    def cursor(self, name=None, cursor_factory=None) -> Cursor:
        self._check()
        if name is not None:
            raise NotSupportedError("named (server-side) cursors are not supported")
        return Cursor(self, as_dict=(cursor_factory or self.cursor_factory) is RealDictCursor)

    def commit(self):
        with self._lock:
            self._check()
            if self._in_transaction():
                self._simple("COMMIT")

    def rollback(self):
        with self._lock:
            self._check()
            if self._in_transaction():
                self._simple("ROLLBACK")

    def close(self):
        with self._lock:
            already = self.closed
            self.closed = 1
            if already:
                return
            try:
                self._raw.close()
            except Exception:  # nosec B110 - the socket is gone either way
                pass

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.closed:
            return
        if exc_type is None:
            self.commit()
            return
        try:
            self.rollback()
        except Error:  # nosec B110 - the error leaving the block is the one to raise
            pass

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool):
        self.set_session(autocommit=value)

    def set_session(self, isolation_level=None, readonly=None, deferrable=None, autocommit=None):
        """``readonly`` sets the session's ``default_transaction_read_only``;
        ``autocommit`` as the property. ``isolation_level`` and ``deferrable``
        are not used by this sample and are refused."""
        if isolation_level is not None or deferrable is not None:
            raise NotSupportedError("isolation_level and deferrable are not supported")
        with self._lock:
            self._check()
            if self._in_transaction():
                raise ProgrammingError("set_session cannot be used inside a transaction")
            if readonly is not None:
                self._simple(f"SET default_transaction_read_only TO {'on' if readonly else 'off'}")
            if autocommit is not None:
                self._autocommit = bool(autocommit)

    def get_transaction_status(self) -> int:
        if self.closed:
            return TRANSACTION_STATUS_UNKNOWN
        status = getattr(self._raw, "_transaction_status", None)
        return {_core.IN_TRANSACTION: TRANSACTION_STATUS_INTRANS,
                _core.IN_FAILED_TRANSACTION: TRANSACTION_STATUS_INERROR}.get(
                    status, TRANSACTION_STATUS_IDLE)

    def get_dsn_parameters(self) -> Dict[str, str]:
        """The parameters the connection was opened with, password left out."""
        return {k: str(v) for k, v in self._params.items() if k != "password"}


# ---------------------------------------------------------------- passwords ---

def encrypt_password(password: str, user: str, scope=None, algorithm: Optional[str] = None) -> str:
    """A SCRAM-SHA-256 verifier for ``ALTER ROLE ... PASSWORD``, in
    PostgreSQL's format (4096 iterations, a 16-byte random salt), so the
    statement never carries the password. *user* and *scope* are accepted for
    the libpq signature; SCRAM does not use them."""
    if (algorithm or "scram-sha-256").lower() != "scram-sha-256":
        raise NotSupportedError(f"password encryption {algorithm!r} is not supported")
    try:
        from scramp.utils import saslprep
        prepared = saslprep(password).encode("utf-8")
    except Exception:
        prepared = password.encode("utf-8")  # PostgreSQL's fallback when SASLprep refuses
    salt = secrets.token_bytes(16)
    iterations = 4096
    salted = hashlib.pbkdf2_hmac("sha256", prepared, salt, iterations)
    stored_key = hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw):
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


# ------------------------------------------------------------------- pool ---

class SimpleConnectionPool:
    """Keeps up to *minconn* idle connections and opens at most *maxconn*; the
    other arguments go to :func:`connect`. For one thread at a time."""

    def __init__(self, minconn: int, maxconn: int, *args, **kwargs):
        self.minconn, self.maxconn = int(minconn), int(maxconn)
        self._args, self._kwargs = args, kwargs
        self._idle: list = []
        self._used: Dict[Any, Connection] = {}
        self._keys: Dict[int, Any] = {}
        self._next = 0
        self.closed = False
        for _ in range(self.minconn):
            self._idle.append(connect(*args, **kwargs))

    def getconn(self, key=None) -> Connection:
        if self.closed:
            raise PoolError("connection pool is closed")
        if key is None:
            self._next += 1
            key = self._next
        if key in self._used:
            return self._used[key]
        if self._idle:
            conn = self._idle.pop()
        elif len(self._used) >= self.maxconn:
            raise PoolError("connection pool exhausted")
        else:
            conn = connect(*self._args, **self._kwargs)
        self._used[key] = conn
        self._keys[id(conn)] = key
        return conn

    def putconn(self, conn: Connection, key=None, close: bool = False) -> None:
        if self.closed:
            raise PoolError("connection pool is closed")
        key = self._keys.get(id(conn)) if key is None else key
        if key is None or self._used.get(key) is not conn:
            raise PoolError("trying to put an unkeyed connection")
        del self._used[key]
        del self._keys[id(conn)]
        if not (close or conn.closed or len(self._idle) >= self.minconn):
            try:
                conn.rollback()
                self._idle.append(conn)
                return
            except Error:
                pass
        conn.close()

    def closeall(self) -> None:
        if self.closed:
            raise PoolError("connection pool is closed")
        for conn in self._idle + list(self._used.values()):
            conn.close()
        self._idle, self._used, self._keys = [], {}, {}
        self.closed = True
