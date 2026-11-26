# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline Athena + Glue doubles for the exact-total split tests.

``FakeLake`` keeps one table in an in-memory SQLite database, split into data
FILES the way an Iceberg table is, and answers the boto3 calls the connector
makes:

* rows go to files partitioned by the day of ``part_col`` (a NULL value goes
  to the NULL partition), ``rows_per_file`` rows each; rows for which
  ``unpartitioned(row)`` is true go to files with no partition directory (an
  older unpartitioned spec), sorted by ``unpart_sort`` (narrow bounds on that
  column), grouped one file per ``unpart_key(row)``, or dealt round-robin
  (wide bounds);
* every file has per-column sizes, value bounds (none on ``part_col`` unless
  ``part_bounds``; ``widen`` replaces a column's bounds on partitioned files)
  and NULL counts; ``delete_files`` adds delete files to partitions;
* a data query reads the files the engine could not prune (partition values
  and bounds against the query's comparisons on temporal columns, NULLs
  included; with ``footer_stats`` also each file's own row values, as the
  engine prunes by Parquet row-group statistics the file list may not carry)
  and is FAILED with the engine's scan-limit reason when the
  columns it references there are more than ``cutoff`` bytes; otherwise it
  runs in SQLite;
* ``"<table>$files"`` is a real SQLite table with JSON maps, so the planning
  query the connector writes is EXECUTED (``element_at``, ``regexp_extract``,
  ``regexp_like``, ``greatest``, ``least`` are registered); the path-field
  coverage query and ``"<table>$snapshots"`` are answered directly;
* ``get_table`` (Glue) returns the columns with Iceberg field ids;
* ``clock`` (a callable returning a UTC datetime, called once per data
  query) is the engine's current time: ``current_timestamp``, ``now()``,
  ``localtimestamp`` and ``current_date`` outside string literals read it, so
  pieces run at different instants see different values (unset: SQLite's).

``RunsUntilStopped`` wraps a ``FakeLake`` so its pieces run until they are
stopped, for tests where the split's own deadline, not a piece's poll
window, must end the run.

Synthetic names only. No network.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta

_SCAN_LIMIT = "Bytes scanned limit was exceeded"
_TEMPORAL_LIT = r"(?:TIMESTAMP|DATE)\s*'([^']+)'"


class FakeGlue:
    def __init__(self, lake: "FakeLake"):
        self.lake = lake
        self.calls = 0

    def get_table(self, **kwargs):
        self.calls += 1
        if self.lake.glue_fails:
            raise RuntimeError("AccessDenied")
        lake = self.lake
        columns = [{"Name": name, "Type": dtype,
                    "Parameters": {"iceberg.field.id": str(i + 1)}}
                   for i, (name, dtype) in enumerate(lake.columns.items())]
        params = {"table_type": "ICEBERG"} if lake.iceberg else {"classification": "parquet"}
        return {"Table": {"Name": lake.table, "Parameters": params,
                          "StorageDescriptor": {"Columns": columns}}}


def _moment(text: str) -> datetime:
    text = str(text).replace(" UTC", "").replace("T", " ").rstrip("Z")
    return datetime.strptime(text[:19] if len(text) > 10 else text,
                             "%Y-%m-%d %H:%M:%S" if len(text) > 10 else "%Y-%m-%d")


class _Range:
    """Allowed values of one column: ``[lo, hi)`` style bounds plus NULLs."""

    def __init__(self):
        self.lo, self.lo_incl, self.hi, self.hi_incl, self.nulls = None, True, None, False, True

    def add(self, op: str, value: datetime) -> None:
        self.nulls = False
        if op in (">", ">=", "=") and (self.lo is None or value > self.lo
                                        or (value == self.lo and op == ">")):
            self.lo, self.lo_incl = value, op != ">"
        if op in ("<", "<=", "=") and (self.hi is None or value < self.hi
                                        or (value == self.hi and op == "<")):
            self.hi, self.hi_incl = value, op != "<"

    def hits(self, low: datetime, high: datetime) -> bool:
        above = self.lo is None or high > self.lo or (self.lo_incl and high == self.lo)
        below = self.hi is None or low < self.hi or (self.hi_incl and low == self.hi)
        return above and below


class FakeLake:
    """One table, as an Athena client (and ``.glue`` as a Glue client)."""

    def __init__(self, *, columns: dict[str, str], rows: list[tuple], part_col: str,
                 cutoff: int, bytes_per_row: int = 70_000, table: str = "events",
                 snapshot: int = 4242, iceberg: bool = True, estimate_factor: float = 0.6,
                 rows_per_file: int = 6, unpartitioned=None, unpart_sort: str | None = None,
                 unpart_rows_per_file: int = 6, part_bounds: bool = False, widen=None,
                 unpart_key=None,
                 delete_files=(), delete_bytes: int = 20_000, newest: str | None = None,
                 footer_stats: bool = False):
        self.footer_stats = footer_stats
        self.columns, self.part_col, self.table = dict(columns), part_col, table
        self.cutoff, self.snapshot, self.iceberg = cutoff, snapshot, iceberg
        self.col_bytes = bytes_per_row // len(columns)
        self.estimate_factor, self.newest = estimate_factor, newest
        self.files_fail = self.glue_fails = False
        self.fail_piece_containing: str | None = None
        #: Fail only a query containing EVERY one of these substrings (so a
        #: single branch's pieces can be failed without failing the whole
        #: multi-branch query, which contains some of them but not the piece's
        #: own ``FOR VERSION AS OF``).
        self.fail_piece_all_of: tuple[str, ...] = ()
        self.slow_piece_containing: str | None = None
        #: The first this-many pieces (and plain data queries) stay QUEUED
        #: until stopped (a stalled poll).
        self.stall_first_pieces = 0
        self.stall_first_plain = 0
        #: Bytes a stalled query reports scanned while it stays unfinished.
        self.stall_bytes = 0
        #: The engine's clock, read once per data query (see the module doc).
        self.clock = None
        self._stalled: set = set()
        self.lock = threading.RLock()
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.scanned: dict[str, int] = {}
        self._queries: dict[str, str] = {}
        self._outcomes: dict[str, dict] = {}
        self.rows = list(rows)
        self._made = 0
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        _register_functions(self.db)
        names = ", ".join(f'"{c}"' for c in columns)
        self.db.execute(f'CREATE TABLE "{table}" ({names})')
        self.db.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(columns))})', rows)
        self.unpart_key = unpart_key
        self.files = self._build_files(rows_per_file, unpartitioned, unpart_sort,
                                       unpart_rows_per_file, part_bounds, widen or {})
        self.files += [self._delete_file(day, delete_bytes, i) for i, day in enumerate(delete_files)]
        self._write_files_table()
        self.glue = FakeGlue(self)

    # ------------------------------------------------------------- the files
    def _index(self, column: str) -> int:
        return list(self.columns).index(column)

    def _build_files(self, per_file, unpartitioned, unpart_sort, unpart_per_file,
                     part_bounds, widen) -> list[dict]:
        by_day: dict = {}
        loose: list[int] = []
        pi = self._index(self.part_col)
        for n, row in enumerate(self.rows):
            if unpartitioned is not None and unpartitioned(row):
                loose.append(n)
                continue
            value = row[pi]
            by_day.setdefault("null" if value is None else str(value)[:10], []).append(n)
        files = []
        for day in sorted(by_day):
            members = by_day[day]
            for start in range(0, len(members), per_file):
                files.append(self._data_file(members[start:start + per_file], day,
                                             part_bounds, widen))
        if loose:
            if self.unpart_key is not None:
                keyed: dict = {}
                for n in loose:
                    keyed.setdefault(self.unpart_key(self.rows[n]), []).append(n)
                chunks = [keyed[k] for k in sorted(keyed)]
            elif unpart_sort:
                si = self._index(unpart_sort)
                loose.sort(key=lambda n: (self.rows[n][si] is None, str(self.rows[n][si])))
                chunks = [loose[i:i + unpart_per_file] for i in range(0, len(loose), unpart_per_file)]
            else:
                count = max(1, len(loose) // unpart_per_file)
                chunks = [loose[i::count] for i in range(count)]
            files += [self._data_file(chunk, None, part_bounds, {}) for chunk in chunks]
        return files

    def _data_file(self, members, day, part_bounds, widen) -> dict:
        self._made += 1
        path = (f"s3://amzn-s3-demo-lake/{self.table}/data/{self.part_col}_day={day}/region=r1/f-{self._made}.parquet"
                if day is not None else f"s3://amzn-s3-demo-lake/{self.table}/data/f-{self._made}.parquet")
        bounds, nulls = {}, {}
        for column, dtype in self.columns.items():
            ci = self._index(column)
            values = [self.rows[m][ci] for m in members]
            nulls[column] = sum(v is None for v in values)
            present = [v for v in values if v is not None]
            temporal = dtype.startswith(("timestamp", "date"))
            if not temporal or not present or (column == self.part_col and not part_bounds):
                continue
            bounds[column] = (min(present), max(present))
            if day is not None and column in widen:
                bounds[column] = widen[column]
        return {"path": path, "rows": list(members), "partition": day, "content": 0,
                "size": len(members) * self.col_bytes * len(self.columns),
                "bounds": bounds, "nulls": nulls}

    def _delete_file(self, day, size, i) -> dict:
        path = (f"s3://amzn-s3-demo-lake/{self.table}/data/{self.part_col}_day={day}/region=r1/d-{i}.parquet"
                if day is not None else f"s3://amzn-s3-demo-lake/{self.table}/data/d-{i}.parquet")
        return {"path": path, "rows": [], "partition": day, "content": 1, "size": size,
                "bounds": {}, "nulls": {}}

    def _write_files_table(self) -> None:
        self.db.execute("ATTACH DATABASE ':memory:' AS lake_db")
        self.db.execute(f'CREATE TABLE lake_db."{self.table}$files" (file_path TEXT, content INTEGER, '
                        "record_count INTEGER, file_size_in_bytes INTEGER, column_sizes TEXT, "
                        "lower_bounds TEXT, upper_bounds TEXT, null_value_counts TEXT)")
        fids = {c: i + 1 for i, c in enumerate(self.columns)}
        for f in self.files:
            sizes = {fids[c]: int(len(f["rows"]) * self.col_bytes * self.estimate_factor)
                     for c in self.columns} if f["content"] == 0 else {}
            lower = {fids[c]: _iso(lo) for c, (lo, _hi) in f["bounds"].items()}
            upper = {fids[c]: _iso(hi) for c, (_lo, hi) in f["bounds"].items()}
            self.db.execute(f'INSERT INTO lake_db."{self.table}$files" VALUES (?,?,?,?,?,?,?,?)', (
                f["path"], f["content"], len(f["rows"]), f["size"], json.dumps(sizes),
                json.dumps(lower), json.dumps(upper),
                json.dumps({fids[c]: k for c, k in f["nulls"].items()})))

    # ---------------------------------------------------------- pruning model
    def _constraints(self, sql: str) -> dict:
        text, found = sql, {}
        or_null = re.compile(r'\(\s*"(\w+)"\s*<\s*' + _TEMPORAL_LIT + r'\s+OR\s+"\1"\s+IS\s+NULL\s*\)')
        for column, value in or_null.findall(text):
            rng = found.setdefault(column.lower(), _Range())
            nulls = rng.nulls
            rng.add("<", _moment(value))
            rng.nulls = nulls
        text = or_null.sub(" ", text)
        for column, op, value in re.findall(r'"?(\w+)"?\s*(>=|<=|<|>|=)\s*' + _TEMPORAL_LIT, text):
            if column in self.columns:
                found.setdefault(column.lower(), _Range()).add(op, _moment(value))
        for column, negate in re.findall(r'"?(\w+)"?\s+IS\s+(NOT\s+)?NULL', text):
            if column in self.columns:
                rng = found.setdefault(column.lower(), _Range())
                if negate:
                    rng.nulls = False
                else:
                    rng.lo, rng.hi, rng.lo_incl, rng.hi_incl = datetime.max, datetime.min, True, True
        return found

    def _may_read(self, f: dict, cons: dict) -> bool:
        for column, rng in cons.items():
            if column == self.part_col.lower() and f["partition"] is not None:
                if f["partition"] == "null":
                    if not rng.nulls:
                        return False
                    continue
                day = datetime.strptime(f["partition"], "%Y-%m-%d")
                if not rng.hits(day, day + timedelta(days=1) - timedelta(microseconds=1)):
                    return False
            if column in f["bounds"]:
                lo, hi = f["bounds"][column]
                if not ((rng.nulls and f["nulls"].get(column)) or rng.hits(_moment(lo), _moment(hi))):
                    return False
            if self.footer_stats and not self._footer_hits(f, column, rng):
                return False
        return True

    def _footer_hits(self, f: dict, column: str, rng) -> bool:
        """Whether the file's own rows (its footer statistics) can match."""
        if column not in {c.lower() for c in self.columns} or not f["rows"]:
            return True
        ci = [c.lower() for c in self.columns].index(column)
        values = [self.rows[m][ci] for m in f["rows"]]
        present = [_moment(v) for v in values if v is not None]
        if rng.nulls and len(present) < len(values):
            return True
        return bool(present) and rng.hits(min(present), max(present))

    def scan_bytes(self, sql: str) -> int:
        """Bytes the engine reads for ``sql``: the referenced columns of every
        file it cannot prune, plus the delete files of their partitions."""
        cons = self._constraints(sql)
        words = {w.lower() for w in re.findall(r"\w+", sql)}
        width = sum(1 for c in self.columns if c.lower() in words) or 1
        read = [f for f in self.files if f["content"] == 0 and self._may_read(f, cons)]
        parts = {f["partition"] for f in read}
        deletes = [f for f in self.files if f["content"] != 0
                   and read and (f["partition"] is None or f["partition"] in parts)]
        return sum(len(f["rows"]) * self.col_bytes * width for f in read) + sum(f["size"] for f in deletes)

    # ------------------------------------------------------------------ data
    def reference(self, sql: str, at=None) -> list[list]:
        """``sql`` run whole in SQLite, cells as Athena prints them; its
        current time is ``at`` when given."""
        with self.lock:
            return [[_cell(v) for v in row] for row in self.db.execute(_to_sqlite(sql, at))]

    def _run_data(self, sql: str) -> dict:
        if self.fail_piece_containing and self.fail_piece_containing in sql:
            return {"state": "FAILED", "reason": "INTERNAL_ERROR: worker lost", "scanned": 10}
        if self.fail_piece_all_of and all(part in sql for part in self.fail_piece_all_of):
            return {"state": "FAILED", "reason": "INTERNAL_ERROR: worker lost", "scanned": 10}
        if self.slow_piece_containing and self.slow_piece_containing in sql:
            return {"state": "RUNNING", "scanned": 10}
        # A query over no table (the digest merge's FROM (VALUES ...)) scans
        # nothing: run it in SQLite with a zero scan.
        if not re.search(rf"\bFROM\s+(?:\w+\.)*{re.escape(self.table)}\b", sql, re.IGNORECASE):
            now = self.clock() if self.clock else None
            with self.lock:
                cursor = self.db.execute(_to_sqlite(sql, now))
                names = [d[0] for d in cursor.description]
                rows = [[_cell(v) for v in row] for row in cursor.fetchall()]
            return {"state": "SUCCEEDED", "scanned": 0, "columns": names,
                    "types": _result_types(sql, self.columns), "rows": rows}
        scanned = self.scan_bytes(sql)
        if scanned > self.cutoff:
            return {"state": "FAILED", "reason": f"Query cancelled: {_SCAN_LIMIT}",
                    "scanned": self.cutoff + 1}
        now = self.clock() if self.clock else None
        with self.lock:
            cursor = self.db.execute(_to_sqlite(sql, now))
            names = [d[0] for d in cursor.description]
            rows = [[_cell(v) for v in row] for row in cursor.fetchall()]
        return {"state": "SUCCEEDED", "scanned": scanned, "columns": names,
                "types": _result_types(sql, self.columns), "rows": rows}

    def coverage(self) -> list[list]:
        totals: dict = {}
        for f in self.files:
            for field in re.findall(r"/([A-Za-z_][A-Za-z0-9_]*)=[^/]*", f["path"]):
                totals[field] = totals.get(field, 0) + f["size"]
        return [[k, str(v)] for k, v in sorted(totals.items())]

    def _run_meta(self, sql: str) -> dict:
        if "$snapshots" in sql:
            return _ok(["snapshot_id"], ["bigint"], [[str(self.snapshot)]])
        if self.files_fail:
            return {"state": "FAILED", "reason": "ACCESS_DENIED: no access", "scanned": 0}
        if "UNNEST" in sql:
            return _ok(["field", "bytes"], ["varchar", "bigint"], self.coverage())
        with self.lock:
            cursor = self.db.execute(_to_sqlite(sql))
            names = [d[0] for d in cursor.description]
            rows = [[_cell(v) for v in row] for row in cursor.fetchall()]
        if self.newest is not None:
            rows = [r[:len(names) - sum(n.startswith("n") and n[1:].isdigit() for n in names)]
                    + [self.newest] * sum(n.startswith("n") and n[1:].isdigit() for n in names)
                    for r in rows]
        return _ok(names, ["varchar"] * len(names), rows)

    # --------------------------------------------------------------- boto3 api
    def start_query_execution(self, QueryString, **_kwargs):
        with self.lock:
            qid = f"q{len(self._queries) + 1}"
            self._queries[qid] = QueryString
            self.started.append(QueryString)
        return {"QueryExecutionId": qid}

    def _outcome(self, qid: str) -> dict:
        with self.lock:
            if qid in self.stopped:
                return {"state": "CANCELLED", "reason": "Query cancelled by user", "scanned": 10}
            sql = self._queries[qid]
            if qid not in self._outcomes and self._stalls(qid, sql):
                return {"state": "RUNNING" if self.stall_bytes else "QUEUED",
                        "scanned": self.stall_bytes}
            if qid not in self._outcomes:
                if "information_schema" in sql:
                    outcome = _ok(["column_name", "data_type", "extra_info"], ["varchar"] * 3,
                                  [[c, t, ""] for c, t in self.columns.items()])
                elif _is_meta(sql):
                    outcome = self._run_meta(sql)
                else:
                    outcome = self._run_data(sql)
                if outcome["state"] == "RUNNING":
                    return outcome
                self._outcomes[qid] = outcome
                self.scanned[qid] = outcome.get("scanned", 0)
            return self._outcomes[qid]

    def _stalls(self, qid: str, sql: str) -> bool:
        """Whether this query's poll stays QUEUED (the first pieces, or the
        first plain data queries, until stopped)."""
        if qid in self._stalled:
            return True
        if _is_meta(sql) or "information_schema" in sql:
            return False
        piece = "FOR VERSION AS OF" in sql
        if piece and self.stall_first_pieces > 0:
            self.stall_first_pieces -= 1
        elif not piece and self.stall_first_plain > 0:
            self.stall_first_plain -= 1
        else:
            return False
        self._stalled.add(qid)
        return True

    def get_query_execution(self, QueryExecutionId):
        outcome = self._outcome(QueryExecutionId)
        status = {"State": outcome["state"]}
        if outcome.get("reason"):
            status["StateChangeReason"] = outcome["reason"]
        return {"QueryExecution": {"Status": status,
                                   "Statistics": {"DataScannedInBytes": outcome.get("scanned", 1000)}}}

    def get_query_results(self, QueryExecutionId, **_kwargs):
        outcome = self._outcome(QueryExecutionId)
        info = [{"Name": n, "Type": t} for n, t in zip(outcome["columns"], outcome["types"])]
        header = {"Data": [{"VarCharValue": n} for n in outcome["columns"]]}
        body = [{"Data": [{} if v is None else {"VarCharValue": v} for v in row]}
                for row in outcome["rows"]]
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": info}, "Rows": [header] + body}}

    def stop_query_execution(self, QueryExecutionId=None, **_kwargs):
        with self.lock:
            if QueryExecutionId and QueryExecutionId not in self.stopped:
                self.stopped.append(QueryExecutionId)
        return None

    def get_work_group(self, WorkGroup):
        return {"WorkGroup": {"Configuration": {"BytesScannedCutoffPerQuery": self.cutoff}}}

    def data_queries(self) -> list[str]:
        return [q for q in self.started if not _is_meta(q) and "information_schema" not in q]


class RunsUntilStopped:
    """``lake`` whose pieces keep running until they are stopped: a poll of a
    piece waits (at most ``seconds``) for its stop call before it is
    answered, so a piece ends only once the split has stopped it, never on
    its own poll window."""

    def __init__(self, lake: FakeLake, seconds: float = 10.0):
        self.lake, self.seconds = lake, seconds
        self._stops: dict = {}
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.lake, name)

    def _stop(self, query_id: str) -> threading.Event:
        with self._lock:
            return self._stops.setdefault(query_id, threading.Event())

    def get_query_execution(self, QueryExecutionId):  # noqa: N803
        if "FOR VERSION AS OF" in self.lake._queries.get(QueryExecutionId, ""):
            self._stop(QueryExecutionId).wait(self.seconds)
        return self.lake.get_query_execution(QueryExecutionId=QueryExecutionId)

    def stop_query_execution(self, QueryExecutionId=None, **kwargs):  # noqa: N803
        out = self.lake.stop_query_execution(QueryExecutionId=QueryExecutionId, **kwargs)
        if QueryExecutionId:
            self._stop(QueryExecutionId).set()
        return out


def _is_meta(sql: str) -> bool:
    """A read of a table's metadata relation (``"<table>$files"`` and the
    like), never a data query (whose JSON paths may hold ``$``)."""
    return re.search(r"\$(?:files|snapshots|partitions)\b", sql) is not None


def _iso(value) -> str:
    text = str(value)
    return text.replace(" ", "T") if len(text) > 10 else text


def _ok(columns, types, rows) -> dict:
    return {"state": "SUCCEEDED", "scanned": 1000, "columns": columns, "types": types, "rows": rows}


def _cell(value):
    if value is None:
        return None
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _map_get(text, key):
    try:
        return json.loads(text).get(str(key)) if text else None
    except (TypeError, ValueError):
        return None


def _regexp_extract(text, pattern, group=0):
    if text is None:
        return None
    found = re.search(pattern, text)
    return found.group(group) if found else None


def _extreme(pick):
    def run(*args):
        return None if any(a is None for a in args) else pick(args)
    return run


def _json_scalar(text, path):
    """``json_extract_scalar`` for a ``$.key`` path on one JSON object."""
    try:
        value = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    key = str(path)[2:] if str(path).startswith("$.") else None
    found = value.get(key) if isinstance(value, dict) and key else None
    return None if isinstance(found, (dict, list)) else found


def _register_functions(db) -> None:
    db.create_function("element_at", 2, _map_get)
    db.create_function("regexp_extract", 3, _regexp_extract)
    db.create_function("regexp_like", 2, lambda t, p: int(bool(re.search(p, t or ""))))
    db.create_function("regexp_replace", 3,
                       lambda t, p, r: None if t is None else re.sub(p, r, t))
    db.create_function("json_extract_scalar", 2, _json_scalar)
    db.create_function("greatest", 2, _extreme(max))
    db.create_function("least", 2, _extreme(min))
    db.create_function("to_hex", 1, _to_hex)
    db.create_function("from_hex", 1, _from_hex)
    db.create_function("date_diff", 3, _date_diff)
    db.create_function("value_at_quantile", 2, _value_at_quantile)
    db.create_function("values_at_quantiles", 2, _values_at_quantiles)
    db.create_aggregate("qdigest_agg", 3, _QDigestAgg)
    db.create_aggregate("merge", 1, _MergeAgg)
    db.create_aggregate("approx_percentile", 2, _ApproxPercentile)


# --- percentile / qdigest stand-ins -------------------------------------- #
# A "qdigest" is modelled as the exact JSON list of its values (small test
# data), so value_at_quantile on the merge of several equals approx_percentile
# over the same rows: the split result and the whole-query reference agree.
# Integer values are kept as integers (JSON preserves them), so a BIGINT digest
# does not collapse two values that a double could not tell apart. The third
# qdigest_agg argument (accuracy) is accepted and ignored (the model is exact).
import json as _json


def _num(value):
    """``value`` as an int when it is integral, else a float (so a bigint
    digest keeps distinct large integers that a double would merge)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    f = float(value)
    return int(f) if f.is_integer() and abs(f) < 2 ** 63 else f


def _render_q(value):
    """Render one quantile value as Athena prints it: an int plainly, a float
    with repr."""
    return str(value) if isinstance(value, int) else repr(float(value))


def _quantile(values, q: float):
    """The value at quantile ``q`` of ``values`` (Trino's ``approx_percentile``
    method: the value whose rank is ceil(q * n), 1-based), exact on this small
    data. ``None`` for no value."""
    present = sorted(v for v in values if v is not None)
    if not present:
        return None
    import math
    n = len(present)
    rank = max(1, math.ceil(q * n))
    return present[min(rank, n) - 1]


def _digest_values(text) -> list:
    if text is None:
        return []
    try:
        data = _json.loads(text)
    except (TypeError, ValueError):
        return []
    return [_num(v) for v in data if v is not None]


def _to_hex(text):
    return None if text is None else str(text).encode("utf-8").hex().upper()


def _from_hex(hexed):
    return None if hexed is None else bytes.fromhex(str(hexed)).decode("utf-8")


def _date_diff(unit, a, b):
    if a is None or b is None:
        return None
    seconds = (_moment(b) - _moment(a)).total_seconds()
    per = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(str(unit).lower(), 1)
    return int(seconds // per)


def _value_at_quantile(digest, q):
    value = _quantile(_digest_values(digest), float(q))
    return None if value is None else _render_q(value)


def _values_at_quantiles(digest, quantiles):
    try:
        qs = _json.loads(quantiles) if isinstance(quantiles, str) else list(quantiles)
    except (TypeError, ValueError):
        return None
    values = _digest_values(digest)
    out = [_quantile(values, float(q)) for q in qs]
    return "[" + ", ".join("null" if v is None else _render_q(v) for v in out) + "]"


class _QDigestAgg:
    """``qdigest_agg(x, weight, accuracy)`` -> the JSON list of its non-NULL
    values (weight and accuracy accepted, ignored: the model is exact)."""

    def __init__(self):
        self.values = []

    def step(self, value, _weight=1, _accuracy=None):
        if value is not None:
            self.values.append(_num(value))

    def finalize(self):
        return _json.dumps(self.values)


class _MergeAgg:
    """``merge(d)`` -> the concatenation of the digests' value lists."""

    def __init__(self):
        self.values = []

    def step(self, digest):
        self.values.extend(_digest_values(digest))

    def finalize(self):
        return _json.dumps(self.values)


class _ApproxPercentile:
    """``approx_percentile(x, q|[...])`` over rows, the same method the digest
    path uses, so the reference and the split agree."""

    def __init__(self):
        self.values = []
        self.spec = None

    def step(self, value, spec):
        if value is not None:
            self.values.append(_num(value))
        self.spec = spec

    def finalize(self):
        spec = self.spec
        if isinstance(spec, str) and spec.strip().startswith("["):
            qs = _json.loads(spec)
            out = [_quantile(self.values, float(q)) for q in qs]
            return "[" + ", ".join("null" if v is None else _render_q(v) for v in out) + "]"
        value = _quantile(self.values, float(spec))
        return None if value is None else _render_q(value)


#: The one list expansion the fake reads: a JSON list column, one row per
#: element, written ``CROSS JOIN UNNEST(...)`` or ``, UNNEST(...)``.
_UNNEST_SQL = re.compile(r"(?:CROSS JOIN\s+|,\s*)UNNEST\(CAST\(json_parse\((\"?\w+\"?)\) AS ARRAY\(JSON\)\)\)"
                         r"\s+(?:AS\s+)?(\w+)\s*\((\w+)\)", re.IGNORECASE)


def _unnest_sql(text: str) -> str:
    """The list expansion of a JSON list as SQLite's ``json_each``: the
    element column reads the element (string literals untouched)."""
    found = _UNNEST_SQL.search(text)
    if not found:
        return text
    column, alias, element = found.groups()
    text = f"{text[:found.start()]} CROSS JOIN json_each({column}) AS {alias}{text[found.end():]}"
    word = re.compile(r"('(?:[^']|'')*')|\b" + re.escape(element) + r"\b")
    return word.sub(lambda m: m.group(1) or f"{alias}.value", text)


_CLOCK_SQL = re.compile(r"('(?:[^']|'')*')|\b(current_timestamp|localtimestamp|current_date|now\(\))",
                        re.IGNORECASE)


def _clock_sql(text: str, now) -> str:
    """The engine's clock words read as ``now`` (string literals untouched)."""
    def sub(match):
        if match.group(1):
            return match.group(1)
        if match.group(2).lower() == "current_date":
            return f"'{now:%Y-%m-%d}'"
        return f"'{now:%Y-%m-%d %H:%M:%S}'"
    return _CLOCK_SQL.sub(sub, text)


def _to_sqlite(sql: str, now=None) -> str:
    text = re.sub(r"\s+FOR VERSION AS OF \d+", "", sql)
    if now is not None:
        text = _clock_sql(text, now)
    text = re.sub(r"\bTIMESTAMP\s*'([^']*?)(?:\.0+)?(?: UTC)?'", r"'\1'", text)
    text = re.sub(r"\bDATE\s*'([^']*)'", r"'\1'", text)
    text = _rewrite_casts(text)
    text = _rewrite_arrays(text)
    text = _rewrite_values_alias(text)
    return _unnest_sql(re.sub(r"\bCAST\(NULL AS varchar\)", "NULL", text))


def _rewrite_values_alias(text: str) -> str:
    """Trino's ``(VALUES ...) AS t(c0, c1)`` -> a SQLite-compatible
    ``(SELECT column1 AS c0, column2 AS c1 FROM (VALUES ...)) AS t`` (SQLite
    names VALUES columns ``column1..N`` and rejects the column-alias list)."""
    m = re.search(r"\(VALUES\b", text, re.IGNORECASE)
    if not m:
        return text
    open_at = m.start()
    close = _matching(text, open_at)
    if close < 0:
        return text
    tail = re.match(r"\s+AS\s+(\w+)\s*\(([^)]*)\)", text[close + 1:], re.IGNORECASE)
    if not tail:
        return text
    alias = tail.group(1)
    cols = [c.strip() for c in tail.group(2).split(",")]
    projection = ", ".join(f"column{i + 1} AS {c}" for i, c in enumerate(cols))
    inner = text[open_at:close + 1]
    return (text[:open_at] + f"(SELECT {projection} FROM {inner}) AS {alias}"
            + text[close + 1 + tail.end():])


def _matching(text: str, open_at: int) -> int:
    """Index of the ``)`` matching ``text[open_at] == '('``, else -1."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _rewrite_casts(text: str) -> str:
    """Make the qdigest-path casts SQLite-friendly: ``CAST(x AS varbinary)``
    and ``CAST(x AS qdigest(<type>))`` keep ``x`` (the digest is modelled as
    its text), ``CAST(x AS bigint)`` becomes ``CAST(x AS INTEGER)`` (integers
    stay exact, so bigints beyond 2**53 do not collapse), and
    ``CAST(x AS double|real)`` becomes ``CAST(x AS REAL)``."""
    for _ in range(50):
        m = re.search(r"\bCAST\(", text)
        if not m:
            break
        open_at = m.end() - 1
        close = _matching(text, open_at)
        if close < 0:
            break
        inside = text[open_at + 1:close]
        low = inside.lower()
        qdigest = re.search(r"\s+as\s+qdigest\((?:double|real|bigint)\)$", low)
        if low.endswith(" as varbinary"):
            repl = inside[:low.rfind(" as varbinary")]
        elif qdigest:
            repl = inside[:qdigest.start()]
        elif low.endswith(" as bigint"):
            repl = f"CAST({inside[:low.rfind(' as bigint')]} AS INTEGER)"
        elif low.endswith((" as double", " as real")):
            cut = low.rfind(" as double") if low.endswith(" as double") else low.rfind(" as real")
            repl = f"CAST({inside[:cut]} AS REAL)"
        else:
            # leave an ordinary CAST alone: mark it seen by a placeholder token
            text = text[:m.start()] + "C\x00AST(" + text[open_at + 1:]
            continue
        text = text[:m.start()] + repl + text[close + 1:]
    return text.replace("C\x00AST(", "CAST(")


def _rewrite_arrays(text: str) -> str:
    """``ARRAY[a, b]`` -> the JSON string ``'[a, b]'`` so a stand-in function
    receives a parseable quantile list."""
    return re.sub(r"\bARRAY\[([^\]]*)\]", lambda m: "'[" + m.group(1) + "]'", text, flags=re.IGNORECASE)


def _select_items(sql: str) -> list[str]:
    body = re.search(r"(?is)^\s*SELECT\s+(.*?)\s+FROM\s", sql).group(1)
    items, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            items.append(body[start:i])
            start = i + 1
    return [i.strip() for i in items + [body[start:]]]


def _result_types(sql: str, columns: dict[str, str]) -> list[str]:
    """Engine column types from the SELECT text alone (identical for every piece)."""
    def column_type(name: str) -> str | None:
        return columns.get(name.strip().strip('"').split(".")[-1])
    out = []
    for item in _select_items(sql):
        aliased = (re.match(r'(?is)^(.*\))\s+(?:AS\s+)?"?\w+"?$', item)
                   or re.match(r'(?is)^(\S+)\s+AS\s+"?\w+"?$', item))
        expr = aliased.group(1) if aliased else item
        found = re.match(r"(?is)(count|sum|min|max)\((.*)\)$", expr.strip())
        if found and found.group(1).lower() == "count":
            out.append("bigint")
        elif found:
            base = column_type(found.group(2))
            if base is None:
                base = "bigint" if found.group(1).lower() == "sum" else "varchar"
            out.append("bigint" if found.group(1).lower() == "sum" and base in {"integer", "int"} else base)
        else:
            out.append(column_type(expr) or "varchar")
    return out
