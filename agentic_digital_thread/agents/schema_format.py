# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared, import-light schema formatting — the SINGLE source of the schema-string
renderers used by BOTH consumers (Mode A prompt baking and the Mode B schema bundle).

Houses ``format_tables_desc`` and ``_format_samples`` — the one implementation;
``system_agent`` and ``onboard`` re-export them so their importers see the same
functions. Adds ``format_schema_with_samples`` (columns + types + capped/redacted
example values) and ``cap_redact_sample_list`` (the shared cap+redact primitive).

No strands / MCP / boto3 / DB imports — pure and offline-testable. The only optional
coupling is a lazy import of :mod:`.pii` for value/column redaction.
"""


import re
from contextlib import contextmanager
from contextvars import ContextVar

_RELATION_KINDS: ContextVar[dict[str, str]] = ContextVar("schema_relation_kinds", default={})


def _normalized_kind(value) -> str:
    kind = str(value or "unknown").strip().lower().replace(" ", "_")
    aliases = {"base": "base_table", "table": "base_table", "external_table": "base_table"}
    allowed = {"base_table", "view", "materialized_view", "computed", "foreign_table"}
    return aliases.get(kind, kind if kind in allowed else "unknown")


@contextmanager
def relation_kind_scope(mappings):
    """Apply connector-owned relation kinds only within one provisioning call."""
    observed: dict[str, set[str]] = {}
    for mapping in mappings or []:
        if isinstance(mapping, dict) and mapping.get("table"):
            observed.setdefault(str(mapping["table"]), set()).add(
                _normalized_kind(mapping.get("relation_kind"))
            )
    kinds = {table: next(iter(values)) if len(values) == 1 else "unknown"
             for table, values in observed.items()}
    token = _RELATION_KINDS.set(kinds)
    try:
        yield
    finally:
        _RELATION_KINDS.reset(token)


def _scoped_tables(table_columns: dict) -> dict:
    kinds = _RELATION_KINDS.get()
    if not kinds:
        return table_columns
    return {
        table: raw if isinstance(raw, dict) and "columns" in raw else {
            "relation_kind": kinds.get(str(table), "unknown"), "columns": raw,
        }
        for table, raw in (table_columns or {}).items()
    }


# ---------------------------------------------------------------------------
# Per-column rendering (shared by both renderers below)
# ---------------------------------------------------------------------------

def _column_name(col) -> str:
    """The bare column name for a bare-name or ``(name, data_type)`` column."""
    if isinstance(col, (tuple, list)):
        return str(col[0])
    return str(col)


def _render_column(col) -> str:
    """Render a single column as ``"name: type"`` (or the bare name when no type)."""
    if isinstance(col, (tuple, list)):
        name = str(col[0])
        dtype = str(col[1]) if len(col) > 1 and col[1] else ""
    else:
        name, dtype = str(col), ""
    return f"{name}: {dtype}" if dtype else name


# ---------------------------------------------------------------------------
# Partition-completeness marker — the AFFIRMATIVE "this table has no partition
# keys" signal
# ---------------------------------------------------------------------------
# A partition column renders with a ``"(partition key)"`` data-type suffix (the
# connector-owned marker; appended by the two writers that own physical Athena
# schema — onboard._dp_athena_tables_desc and server._sql_tables_desc_from_graph).
# Its ABSENCE is ambiguous by itself: it means "no partition keys" only when the
# renderer actually SAW a complete physical introspection. A names-only fallback
# desc, a schema baked without physical discovery, or a row-capped (truncated)
# column read all render exactly the same marker-free text.
#
# ``PARTITION_KEYS_ABSENT_MARKER`` closes that gap: it is appended to a table's
# rendered entry ONLY when the caller affirms the introspection completed AND the
# rendered columns carry zero partition-key suffixes. Readers (see
# ``system_agent._athena_partition_columns_from_desc``) may therefore treat it as
# proof of an unpartitioned table; a marker-free entry stays UNKNOWN and falls
# back to live discovery.

PARTITION_KEY_SUFFIX = "(partition key)"
PARTITION_KEYS_ABSENT_MARKER = "(no partition keys)"


def _cols_and_relation(raw) -> tuple:
    """Unpack a per-table value into ``(columns, rendered_relation_suffix)``.

    Shared by both renderers: the plain form is a bare column list, the enriched
    form is ``{"relation_kind": ..., "columns": [...]}`` (see :func:`_scoped_tables`).
    """
    if isinstance(raw, dict) and "columns" in raw:
        kind = str(raw.get("relation_kind") or "unknown").strip().lower().replace(" ", "_")
        return raw.get("columns") or [], f" [{kind}]"
    return raw, ""


def _partition_marker(table, rendered: list[str], unpartitioned_tables,
                      hidden_partitioning=None) -> str:
    """The ``" (no partition keys)"`` suffix for one table, or ``""``.

    Emitted only when BOTH hold: the caller listed ``table`` as completely
    introspected with zero partition keys, and the text we just rendered
    genuinely carries no ``"(partition key)"`` suffix. The second check keeps the
    rendered desc self-consistent — the two markers can never both appear, so a
    reader never has to arbitrate between them.

    A table in ``hidden_partitioning`` gets :func:`_hidden_marker` instead: its
    storage is partitioned by fields the catalog does not list as keys, so the
    absent-keys marker would be false for it.
    """
    if any(PARTITION_KEY_SUFFIX in text.lower() for text in rendered):
        return ""
    if hidden_partitioning and table in hidden_partitioning:
        return _hidden_marker(hidden_partitioning[table])
    if not unpartitioned_tables or table not in unpartitioned_tables:
        return ""
    return f" {PARTITION_KEYS_ABSENT_MARKER}"


# ---------------------------------------------------------------------------
# Hidden-partitioning marker: a table whose storage partitions by fields the
# catalog lists no key for (an Apache Iceberg table partitions by transforms
# of ordinary columns). Rendered ``(hidden partitioning by f1, f2)``, or
# ``(hidden partitioning)`` when the fields could not be read. Never paired
# with either partition-key marker. The agent-side parser
# (``system_agent._athena_partition_columns_from_desc``) reads it back as a
# :class:`HiddenPartitioning` value: known metadata, no partition COLUMN.
# ---------------------------------------------------------------------------
HIDDEN_PARTITIONING_MARKER = "(hidden partitioning"
_HIDDEN_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _hidden_marker(fields) -> str:
    names = [str(f) for f in (fields or ()) if _HIDDEN_FIELD_RE.match(str(f))]
    if not names:
        return f" {HIDDEN_PARTITIONING_MARKER})"
    return f" {HIDDEN_PARTITIONING_MARKER} by {', '.join(names)})"


def parse_hidden_marker(text: str):
    """``(fields, length)`` when ``text`` opens with a well-formed hidden marker.

    ``fields`` is a tuple of the field names (empty when the marker names
    none); ``length`` is how many characters the marker spans. ``None`` for
    anything malformed (unclosed, a field that is not one identifier), so a
    damaged marker is never read as metadata.
    """
    if not text.startswith(HIDDEN_PARTITIONING_MARKER):
        return None
    close = text.find(")")
    if close < 0:
        return None
    inner = text[len(HIDDEN_PARTITIONING_MARKER):close]
    if not inner:
        return (), close + 1
    if not inner.startswith(" by "):
        return None
    fields = tuple(part.strip() for part in inner[len(" by "):].split(","))
    if not fields or not all(_HIDDEN_FIELD_RE.match(f) for f in fields):
        return None
    return fields, close + 1


class HiddenPartitioning(tuple):
    """Baked partition metadata for a table with hidden partitioning.

    An EMPTY tuple (the table has no partition column a WHERE clause can name),
    with the storage's partition field names in :attr:`fields`. A value of this
    type in the baked partition map means "metadata known, not unpartitioned".
    """

    fields: tuple

    def __new__(cls, fields=()):
        obj = super().__new__(cls)
        obj.fields = tuple(fields)
        return obj

    def __repr__(self) -> str:
        return f"HiddenPartitioning(fields={self.fields!r})"


# ---------------------------------------------------------------------------
# format_tables_desc — MOVED from system_agent (kept verbatim; re-exported there)
# ---------------------------------------------------------------------------

def format_tables_desc(table_columns: dict, *, unpartitioned_tables=None,
                       hidden_partitioning=None) -> str:
    """Render a compact per-table schema string for a system-agent prompt.

    ``table_columns`` maps a table name to its columns. Each column may be a bare
    name (``str``) or a ``(name, data_type)`` pair; a blank/absent data type
    renders the name alone. Output shape::

        "tbl(c1: t1, c2: t2). tbl2(c1: t1)"

    This is the columns-inclusive shape the restore path (server.py) already used,
    extended with data types so a SQL agent can compose queries DIRECTLY from the
    prompt WITHOUT a runtime list/describe round-trip per sub-question. Pure /
    offline and generic across connection types — no dataset knowledge.

    ``unpartitioned_tables`` is the OPTIONAL affirmative-completeness set: tables
    the caller PROVED to be completely introspected with zero partition keys get
    a trailing :data:`PARTITION_KEYS_ABSENT_MARKER`. Omit it (the default) and the
    output is byte-identical to the pre-marker rendering.

    ``hidden_partitioning`` (``{table: [field, ...]}``, optional) marks the
    tables whose storage uses hidden partitioning (:func:`_hidden_marker`).
    """
    table_columns = _scoped_tables(table_columns)
    parts: list[str] = []
    for tbl, raw in (table_columns or {}).items():
        cols, relation = _cols_and_relation(raw)
        rendered: list[str] = [_render_column(col) for col in (cols or [])]
        marker = _partition_marker(tbl, rendered, unpartitioned_tables, hidden_partitioning)
        parts.append(f"{tbl}{relation}({', '.join(rendered)}){marker}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# _format_samples — MOVED from onboard (kept verbatim; re-exported there)
# ---------------------------------------------------------------------------

def _one_line(text: str) -> str:
    """``text`` on one line: line breaks, tabs and other control characters
    become spaces and runs of whitespace collapse. A source value is data in
    the prompt it is shown in, so it must never start a line of its own."""
    return " ".join(_LINE_BREAKS.sub(" ", text).split())


#: Every line break and control character a value could carry (C0 and DEL,
#: NEL, and the Unicode line and paragraph separators).
_LINE_BREAKS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


def _format_samples(raw, max_samples: int = 3, sample_maxlen: int = 40) -> str:
    """Render up to ``max_samples`` de-duplicated, length-capped sample values.

    Each value is stringified, put on one line (:func:`_one_line`), truncated
    to ``sample_maxlen`` chars (with a trailing ellipsis) and de-duplicated
    preserving order. Empty/None values are skipped. Returns "" when there is
    nothing to show. Pure — safe offline."""
    if not raw:
        return ""
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: list[str] = []
    for v in raw:
        if v is None:
            continue
        s = _one_line(str(v))
        if not s:
            continue
        if len(s) > sample_maxlen:
            s = s[: sample_maxlen - 1] + "…"
        if s not in out:
            out.append(s)
        if len(out) >= max_samples:
            break
    return ", ".join(out)


# ---------------------------------------------------------------------------
# cap_redact_sample_list — the shared cap + PII-redact primitive
# ---------------------------------------------------------------------------

def cap_redact_sample_list(raw, max_samples: int = 3, sample_maxlen: int = 40) -> list:
    """Return a capped, de-duplicated, PII-REDACTED list of sample strings.

    Same cap/truncate/dedup semantics as :func:`_format_samples` but value-level
    PII redaction (:func:`pii.redact_pii`) is applied to each value FIRST, so a
    structured identifier (email/SSN/phone/card) that leaked into a non-PII-named
    column is replaced with its ``[REDACTED_*]`` token before it is surfaced."""
    from .pii import redact_pii

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: list = []
    for v in raw:
        if v is None:
            continue
        s = _one_line(redact_pii(str(v)))
        if not s:
            continue
        if len(s) > sample_maxlen:
            s = s[: sample_maxlen - 1] + "…"
        if s not in out:
            out.append(s)
        if len(out) >= max_samples:
            break
    return out


# ---------------------------------------------------------------------------
# format_schema_with_samples — columns + types + capped/redacted example values
# ---------------------------------------------------------------------------

def _sample_column_strings(cols, tbl_samples: dict, max_samples: int,
                           sample_maxlen: int) -> list[str]:
    """Render one table's columns with capped/redacted example values.

    A PII-shaped column NAME renders ``name: type [redacted]`` with no values;
    every surfaced value is redacted by :func:`cap_redact_sample_list`.
    """
    from .pii import column_looks_pii

    out: list[str] = []
    for col in (cols or []):
        base = _render_column(col)
        name = _column_name(col)
        if column_looks_pii(name):
            out.append(f"{base} [redacted]")
            continue
        vals = cap_redact_sample_list(tbl_samples.get(name), max_samples, sample_maxlen)
        out.append(f"{base} e.g. {', '.join(vals)}" if vals else base)
    return out


def format_schema_with_samples(
    table_columns: dict,
    sample_values: dict | None,
    max_samples: int = 3,
    sample_maxlen: int = 40,
    *,
    unpartitioned_tables=None,
    hidden_partitioning=None,
) -> str:
    """Render ``"tbl(c1: t1 e.g. v1, v2; c2: t2)"`` — columns/types plus a few
    example values per column.

    Composes :func:`_render_column` (the same per-column shape as
    :func:`format_tables_desc`) with capped/redacted samples from ``sample_values``
    (``{table: {column: [values]}}``). Columns are separated by ``; `` (so the
    ``, ``-separated sample lists stay unambiguous). Per-column redaction rules
    live in :func:`_sample_column_strings`.

    ``unpartitioned_tables`` and ``hidden_partitioning`` behave exactly as in
    :func:`format_tables_desc` — the affirmative
    :data:`PARTITION_KEYS_ABSENT_MARKER` is appended only for the tables the
    caller proved complete-and-unpartitioned.

    Pure / offline; generic — no dataset knowledge.
    """
    sample_values = sample_values or {}
    table_columns = _scoped_tables(table_columns)
    parts: list[str] = []
    for tbl, raw in (table_columns or {}).items():
        cols, relation = _cols_and_relation(raw)
        col_strs = _sample_column_strings(
            cols, sample_values.get(tbl) or {}, max_samples, sample_maxlen)
        marker = _partition_marker(tbl, col_strs, unpartitioned_tables, hidden_partitioning)
        parts.append(f"{tbl}{relation}({'; '.join(col_strs)}){marker}")
    return ". ".join(parts)


# ---------------------------------------------------------------------------
# with_join_keys: the database's own join keys after the table entries
# ---------------------------------------------------------------------------

#: Opening of the join-keys text baked after the table entries. It holds
#: no parenthesis and no ``e.g.``, so the table parsers
#: (``response_integrity_sql.parse_physical_schema``, the sample reader in
#: ``context_placement``) read the entries before it exactly as they did.
#: The list is NOT exhaustive and says so: valid joins with no declared key
#: (a second column that refers to the same table, a number one system records
#: that another stores under a different name, a ``join_rule`` the
#: orchestrator puts in the question) must stay usable.
JOIN_KEYS_PREFIX = (
    "Declared join keys, read from the database's foreign keys at onboarding: "
    "each links a row to the one row it references in the other table, so use "
    "it when you need that referenced row. These are not the only valid joins: "
    "two tables can also be joined on another column that holds the same values, "
    "such as a second column that refers to the same table, a shared business "
    "number or code, or a join given in the question. Declared keys: ")


def with_join_keys(tables_desc: str, joins, tables) -> str:
    """``tables_desc`` followed by one sentence naming each join key as
    ``table.column = table.column``, FK side first; unchanged when there is
    no desc or no usable join.

    ``joins`` are ``{from_table, from_field, to_table, to_field}`` dicts;
    ``tables`` are the table names the desc renders. Only joins whose two
    tables are among them are named, each once, in the given order. A ``.``
    closes the last table entry first: the parsers end an entry at a ``)``
    followed by ``.`` or the end of the text.

    Pure / offline; generic, no dataset knowledge.
    """
    if not tables_desc or not joins:
        return tables_desc
    known = {str(t) for t in (tables or ())}
    rendered: list[str] = []
    for join in joins:
        if not isinstance(join, dict):
            continue
        ends = [str(join.get(k) or "") for k in
                ("from_table", "from_field", "to_table", "to_field")]
        if not all(ends) or any(c in "".join(ends) for c in " ,;()."):
            continue
        if ends[0] not in known or ends[2] not in known:
            continue
        text = f"{ends[0]}.{ends[1]} = {ends[2]}.{ends[3]}"
        if text not in rendered:
            rendered.append(text)
    if not rendered:
        return tables_desc
    return f"{tables_desc}. {JOIN_KEYS_PREFIX}{'; '.join(rendered)}."


#: One ``table.column = table.column`` entry as :func:`with_join_keys` writes
#: it (names never hold a space, comma, semicolon, parenthesis or dot).
_JOIN_KEY_ENTRY = re.compile(r"([^\s,;().=]+)\.([^\s,;().=]+) = ([^\s,;().=]+)\.([^\s,;().=]+)")


def declared_join_keys(tables_desc: str) -> list[tuple[str, str, str, str]]:
    """The join keys :func:`with_join_keys` baked into ``tables_desc``, as
    ``(from_table, from_field, to_table, to_field)`` in the baked order,
    lowercased; ``[]`` when the text carries none.

    Reads only the text after :data:`JOIN_KEYS_PREFIX`, up to the full stop
    that closes the list (a ``.`` followed by a space or the end: a name
    never holds one). An entry of any other shape is skipped. Pure.
    """
    text = tables_desc if isinstance(tables_desc, str) else ""
    _head, found, rest = text.partition(JOIN_KEYS_PREFIX)
    if not found:
        return []
    body = re.split(r"\.(?=\s|$)", rest, maxsplit=1)[0]
    keys: list[tuple[str, str, str, str]] = []
    for entry in body.split(";"):
        match = _JOIN_KEY_ENTRY.fullmatch(entry.strip())
        if match:
            keys.append(tuple(part.lower() for part in match.groups()))
    return keys


# ---------------------------------------------------------------------------
# with_distinct_identifiers: same-named columns measured to hold different ids
# ---------------------------------------------------------------------------

#: Opening of the text baked after the join keys for the pairs
#: ``distinct_identifiers.measure`` found. Like :data:`JOIN_KEYS_PREFIX` it
#: holds no parenthesis, bracket or ``e.g.``, so every table parser reads the
#: entries before it as it did. Generic: it names no dataset.
DISTINCT_IDENTIFIERS_PREFIX = (
    "Same-named columns that hold different identifiers, measured at onboarding: each pair "
    "below has the same column name and is mapped to the same concept, but the values read "
    "from the two columns have different shapes and none of them was found in the other "
    "column, so each column holds its own identifier. Never join the two columns to each "
    "other and never look up a value of one in the other: it finds no rows. Never rewrite a "
    "value into the other column's form yourself. Rows of the two tables belong to the same "
    "record only when another link between them says so, such as a declared join key. When a "
    "request needs both columns and gives you values of only one, answer what the column "
    "those values belong to holds, and for the other column say that it holds a different "
    "identifier for this concept and that you need that column's own identifiers, unless such "
    "a link gives them. Pairs: ")

#: Characters a rendered name may not hold (they would end a list entry or a sentence).
_DISTINCT_NAME_STOPS = set(" ,;:().[]")


#: A shape example as ``distinct_identifiers.shape_example`` writes it: no
#: space, list or sentence punctuation, and a letter or digit at each end.
_SHAPE_EXAMPLE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_/.:-]*[A-Za-z0-9])?")
#: Characters of one rendered name (the opposite of :data:`_DISTINCT_NAME_STOPS`).
_DISTINCT_NAME = r"[^\s,;:().\[\]]+"
#: One entry as :func:`with_distinct_identifiers` writes it with its shapes.
_DISTINCT_ENTRY = re.compile(
    rf"({_DISTINCT_NAME}): ({_DISTINCT_NAME})\.({_DISTINCT_NAME}) and "
    rf"({_DISTINCT_NAME})\.({_DISTINCT_NAME}), \d+ and \d+ values read, "
    rf"shaped like ({_SHAPE_EXAMPLE.pattern}) and ({_SHAPE_EXAMPLE.pattern})")


def _usable_shape(value) -> bool:
    return (isinstance(value, str) and bool(_SHAPE_EXAMPLE.fullmatch(value))
            and "e.g" not in value.lower())


def _shapes_text(pair: dict) -> str:
    shapes = (pair.get("shape_a"), pair.get("shape_b"))
    if not all(_usable_shape(s) for s in shapes):
        return ""
    return f", shaped like {shapes[0]} and {shapes[1]}"


def with_distinct_identifiers(tables_desc: str, distinct) -> str:
    """``tables_desc`` followed by one sentence naming each measured pair as
    ``concept: table.column and table.column, N and M values read``, plus
    ``, shaped like A and B`` when both measured shape examples are usable;
    unchanged when there is no desc or no usable pair.

    ``distinct`` are ``distinct_identifiers.measure`` records ``{concept,
    column, table_a, table_b, read_a, read_b}`` with optional ``shape_a`` /
    ``shape_b`` examples (``distinct_identifiers.shape_example``). A record
    with a missing or unusable name, or counts that are not whole numbers, is
    skipped; each pair is named once. A shape example is rendered only when
    both are present and :data:`_SHAPE_EXAMPLE` matches each, so
    :func:`distinct_identifier_pairs` reads them back exactly. The text after
    the desc holds no parenthesis or ``e.g.``, and a desc that ends without a
    full stop gets one first, so the table parsers and
    :func:`declared_join_keys` read the desc as before.

    Pure / offline; generic, no dataset knowledge.
    """
    if not tables_desc or not distinct:
        return tables_desc
    rendered: list[str] = []
    for pair in distinct:
        if not isinstance(pair, dict):
            continue
        names = [str(pair.get(k) or "") for k in ("concept", "column", "table_a", "table_b")]
        if not all(names) or _DISTINCT_NAME_STOPS & set("".join(names)):
            continue
        try:
            read_a, read_b = int(pair.get("read_a")), int(pair.get("read_b"))
        except (TypeError, ValueError):
            continue
        concept, column, table_a, table_b = names
        text = (f"{concept}: {table_a}.{column} and {table_b}.{column}, "
                f"{read_a} and {read_b} values read{_shapes_text(pair)}")
        if text not in rendered:
            rendered.append(text)
    if not rendered:
        return tables_desc
    joiner = " " if tables_desc.endswith(".") else ". "
    return f"{tables_desc}{joiner}{DISTINCT_IDENTIFIERS_PREFIX}{'; '.join(rendered)}."


def distinct_identifier_note(tables_desc: str) -> str:
    """The sentence :func:`with_distinct_identifiers` baked into
    ``tables_desc`` (from :data:`DISTINCT_IDENTIFIERS_PREFIX` through the full
    stop that closes its list, as :func:`distinct_identifier_pairs` reads
    it), or ``""`` when the text carries none. Pure."""
    text = tables_desc if isinstance(tables_desc, str) else ""
    _head, found, rest = text.partition(DISTINCT_IDENTIFIERS_PREFIX)
    if not found:
        return ""
    body = re.split(r"\.(?=\s|$)", rest, maxsplit=1)[0]
    return f"{DISTINCT_IDENTIFIERS_PREFIX}{body}." if body else ""


def with_distinct_identifier_note(tables_desc: str, note: str) -> str:
    """``tables_desc`` followed by a baked ``note``
    (:func:`distinct_identifier_note`), joined as
    :func:`with_distinct_identifiers` joins it; unchanged when either is
    empty. Pure."""
    if not tables_desc or not note:
        return tables_desc
    joiner = " " if tables_desc.endswith(".") else ". "
    return f"{tables_desc}{joiner}{note}"


def distinct_identifier_pairs(tables_desc: str) -> list[dict]:
    """The measured pairs :func:`with_distinct_identifiers` baked WITH their
    shapes into ``tables_desc``, as ``{concept, column, table_a, table_b,
    shape_a, shape_b}`` in the baked order; ``[]`` when the text carries none.

    Reads only the text after :data:`DISTINCT_IDENTIFIERS_PREFIX`, up to the
    full stop that closes the list (a ``.`` followed by a space or the end:
    neither a name nor a shape example holds one). An entry baked without
    shapes (a system provisioned before they were measured), or of any other
    form, is skipped, so such a system yields nothing. Pure.
    """
    text = tables_desc if isinstance(tables_desc, str) else ""
    _head, found, rest = text.partition(DISTINCT_IDENTIFIERS_PREFIX)
    if not found:
        return []
    body = re.split(r"\.(?=\s|$)", rest, maxsplit=1)[0]
    pairs: list[dict] = []
    for entry in body.split(";"):
        match = _DISTINCT_ENTRY.fullmatch(entry.strip())
        if not match or match.group(3) != match.group(5):
            continue
        concept, table_a, column, table_b, _column_b, shape_a, shape_b = match.groups()
        pairs.append({"concept": concept, "column": column, "table_a": table_a,
                      "table_b": table_b, "shape_a": shape_a, "shape_b": shape_b})
    return pairs


# ---------------------------------------------------------------------------
# Field descriptions: the meaning a connector records after a field's type
# ---------------------------------------------------------------------------
# A connector's discovery may record what a field holds after its type, in
# parentheses or after a spaced dash: ``text (when the ticket was logged)``,
# ``string \u2014 holds the line name``. A bare type (``integer``), a precision
# (``numeric(10,2)``), a nested engine type (``array(varchar)``, written with
# no space before its parenthesis) and the partition-key marker are not a
# description.

#: The spaced separators a description follows (a parenthesis, an em dash or
#: a hyphen, each with a space before it). A type text without one records no
#: description (the field-description graph read filters on them).
DESCRIPTION_STARTS = (" (", " \u2014 ", " - ")
#: A description says something in words: at least one run of three letters.
_DESCRIPTION_WORDS = re.compile(r"[A-Za-z]{3}")


def _unwrapped(text: str) -> str:
    """``text`` without one pair of parentheses that encloses all of it."""
    if not (text.startswith("(") and text.endswith(")")):
        return text
    depth = 0
    for i, char in enumerate(text):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and i < len(text) - 1:
            return text
    return text[1:-1]


def type_description(data_type) -> str:
    """What a field holds as its connector recorded it after the field's type,
    on one line; ``""`` when the type text records no description. Pure."""
    text = _one_line(str(data_type or ""))
    cuts = [i for i in (text.find(start) for start in DESCRIPTION_STARTS) if i > 0]
    if not cuts:
        return ""
    rest = _unwrapped(text[min(cuts):].strip())
    rest = rest.lstrip("\u2014- ").strip()
    if rest.lower() == PARTITION_KEY_SUFFIX.strip("()") or not _DESCRIPTION_WORDS.search(rest):
        return ""
    return rest


#: A field a description line names: a table and a column (or a dotted
#: path), each part letters, digits, ``_``, ``$`` or ``-``.
_FIELD_TOKEN = re.compile(r"[A-Za-z0-9_$-]{1,128}(?:\.[A-Za-z0-9_$-]{1,128}){0,4}")


def field_description_lines(items, cap: int) -> tuple[list[str], int]:
    """``table.column: description`` lines for the ``{field, description}``
    records in ``items`` (the provision record of described fields), each on
    one line and at most ``cap`` characters in all, and how many records that
    have a line were left out over the cap. A record that is not a mapping,
    names no single field token or has no description is skipped. Pure."""
    lines: list[str] = []
    used = left_out = 0
    for item in items or ():
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        text = _one_line(str(item.get("description") or ""))
        if not text or not _FIELD_TOKEN.fullmatch(field):
            continue
        line = f"{field}: {text}"
        if used + len(line) + 1 > cap:
            left_out += 1
            continue
        lines.append(line)
        used += len(line) + 1
    return lines, left_out
