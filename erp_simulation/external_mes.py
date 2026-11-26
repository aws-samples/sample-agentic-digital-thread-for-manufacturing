# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run ownership for an external MES backend at a simulated site.

A site's MES backend is the ``simulation_config`` row ``mes_backend:<SITE>``:
``builtin`` (the default; absent, blank or unrecognised also mean builtin) or
``external``. The backend only says who may TAKE new work. Who OWNS a run is
recorded per run, in the ``external_mes_runs`` claims table, so a later flip of
the switch never reinterprets a run that is already in flight:

* ``claim_runs`` takes RELEASED runs at a site whose backend is ``external``
  and that have no built-in ``mes_work_orders`` row, records the claim and
  sets them IN_PROGRESS, in ONE statement (two integrators claiming at once
  never get the same run).
* ``complete_run`` sets a claimed run COMPLETED with its ``actual_quantity``
  (the good units; production_runs has no good/reject split) and stamps the
  claim's ``completed_at``. ``ProductionSimulator.complete_production`` then
  deposits the finished goods exactly as it does for a built-in run.
* ``release_claim`` hands a claimed, unfinished run back: RELEASED again, so
  the site's current backend takes it next.
* ``record_external_ref`` stores the vendor's record id on the claim.

The built-in simulator never creates a work order for a claimed run and its
stale-run cleanup never completes one. Flipping the site back to ``builtin``
leaves claimed runs with their integrator until it completes or releases them.

The claims table exists only where the optional plant's custom resource
created it. ``claims_table_present`` is the
probe the simulator runs once per cycle step: without the table, the simulator
runs exactly the SQL it always did. This module imports nothing from the
simulator, so the simulator can import it.
"""
from __future__ import annotations

#: The claims table (created by the external-MES site's own setup, whose DDL a test
#: keeps in step with the statements here).
EXTERNAL_MES_RUNS_TABLE = 'external_mes_runs'

MES_BACKEND_CONFIG_PREFIX = 'mes_backend:'
MES_BACKEND_BUILTIN = 'builtin'
MES_BACKEND_EXTERNAL = 'external'


def mes_backend_config_key(site_code: str) -> str:
    """The simulation_config key carrying ``site_code``'s MES backend."""
    return f"{MES_BACKEND_CONFIG_PREFIX}{site_code}"


#: The simulation_config key where an external sender host records which
#: target (a hash of base URL and workspace) took the site's work, or the
#: handoff marker (the external sender's control).
MES_BACKEND_TARGET_PREFIX = 'mes_backend_target:'


def mes_backend_target_key(site_code: str) -> str:
    """The simulation_config key carrying ``site_code``'s external MES target."""
    return f"{MES_BACKEND_TARGET_PREFIX}{site_code}"


#: The target key's value after the sender's handoff (the control resource, or
#: the documented hand recovery): a sender cycle that still starts holds on it.
MES_BACKEND_HANDED_OFF = 'handed-off'

#: Run statuses that are finished: an open claim on such a run blocks nothing
#: and the handoff may delete it. Any other open claim stops the handoff.
TERMINAL_RUN_STATUSES = ('CANCELLED', 'COMPLETED')

#: The application_name of the ERP dashboard's Reset All lock session. The
#: db-init full reset and the cleanup leave that one session alive (it holds
#: only the sender lock), so the lock is held through the whole reset.
RESET_ALL_LOCK_APPLICATION = 'erp-dashboard-reset-all'


_READ_SWITCH_SQL = """
    SELECT config_key, config_value FROM simulation_config WHERE config_key = ANY(%(keys)s)
"""

_SET_EXTERNAL_SQL = """
    INSERT INTO simulation_config (config_key, config_value, data_type, category, description, default_value)
    VALUES (%(key)s, 'external', 'string', 'mes', %(description)s, 'builtin')
    ON CONFLICT (config_key)
    DO UPDATE SET config_value = 'external'
"""

_SET_BUILTIN_SQL = """
    UPDATE simulation_config
       SET config_value = 'builtin'
     WHERE config_key = %(key)s
"""

_SET_TARGET_SQL = """
    INSERT INTO simulation_config (config_key, config_value, data_type, category, description, default_value)
    VALUES (%(key)s, %(value)s, 'string', 'mes', %(description)s, '')
    ON CONFLICT (config_key)
    DO UPDATE SET config_value = EXCLUDED.config_value
"""

_OPEN_CLAIMS_SQL = """
    SELECT run_id FROM external_mes_runs
     WHERE site_code = %(site)s AND owner = %(owner)s AND completed_at IS NULL
     ORDER BY claimed_at, run_id
"""


def read_switch(cursor, site_code: str) -> tuple[str, str | None]:
    """(the site's resolved MES backend, its stored target or None when blank)."""
    keys = [mes_backend_config_key(site_code), mes_backend_target_key(site_code)]
    cursor.execute(_READ_SWITCH_SQL, {"keys": keys})
    values = dict(cursor.fetchall())
    stored = str(values.get(keys[1]) or "").strip() or None
    return resolve_mes_backend(values.get(keys[0])), stored


def set_external(cursor, site_code: str) -> None:
    """The documented upsert of the switch to ``external``. The caller commits."""
    cursor.execute(_SET_EXTERNAL_SQL, {"key": mes_backend_config_key(site_code),
                                       "description": f"MES backend for {site_code} (builtin|external)"})


def set_builtin(cursor, site_code: str) -> bool:
    """The documented update of the switch to ``builtin``; True when a row
    changed. The caller commits."""
    cursor.execute(_SET_BUILTIN_SQL, {"key": mes_backend_config_key(site_code)})
    return cursor.rowcount > 0


def set_target(cursor, site_code: str, value: str) -> None:
    """Record the site's external MES target (or the handoff marker). The
    caller commits."""
    cursor.execute(_SET_TARGET_SQL, {"key": mes_backend_target_key(site_code), "value": value,
                                     "description": f"external MES target of {site_code} (sender host)"})


def open_claims(cursor, site_code: str, owner: str) -> list:
    """``owner``'s open claims at ``site_code`` (run ids, oldest first)."""
    cursor.execute(_OPEN_CLAIMS_SQL, {"site": site_code, "owner": _owner(owner)})
    return [row[0] for row in cursor.fetchall()]


def resolve_mes_backend(value) -> str:
    """``external`` only for that value (case and surrounding space ignored),
    otherwise ``builtin``."""
    return MES_BACKEND_EXTERNAL if str(value or '').strip().lower() == MES_BACKEND_EXTERNAL \
        else MES_BACKEND_BUILTIN


def claims_table_present(cursor) -> bool:
    """True when the database has the claims table (one catalog read)."""
    cursor.execute("SELECT to_regclass('public.external_mes_runs')")
    row = cursor.fetchone()
    return bool(row and row[0] is not None)


#: The PostgreSQL session advisory lock every external sender cycle holds: the
#: in-account external sender host, its control's handoff, the
#: vendor's own ``run_cycle`` (re-entrant on the host's session) and the ERP
#: dashboard's Reset All, which takes it before it destroys anything.
SENDER_LOCK_KEY = int.from_bytes(b"TULIPSND", "big", signed=True)


def try_sender_lock(cursor) -> bool:
    """Take the sender lock on this session if it is free (never waits)."""
    cursor.execute("SELECT pg_try_advisory_lock(%s)", (SENDER_LOCK_KEY,))
    row = cursor.fetchone()
    return bool(row and row[0])


def wait_for_sender_lock(conn, wait_seconds: float, *, poll_seconds: float = 1.0,
                         sleep=None, clock=None) -> bool:
    """Take the sender lock on ``conn``'s session, waiting up to
    ``wait_seconds`` for a running sender cycle to let it go. Each try is its
    own short transaction, so the session is never left idle inside one.
    Returns False (lock not held) when the time runs out."""
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max(0.0, float(wait_seconds))
    while True:
        with conn.cursor() as cur:
            held = try_sender_lock(cur)
        conn.commit()
        if held:
            return True
        left = deadline - clock()
        if left <= 0:
            return False
        sleep(min(poll_seconds, left))


#: Unclaimed runs only, in SQL the simulator appends to its own predicates
#: (``production_runs`` aliased ``pr`` or named in full).
UNCLAIMED_RUN_SQL = "NOT EXISTS (SELECT 1 FROM external_mes_runs emr WHERE emr.run_id = {run})"


def unclaimed(run_column: str) -> str:
    """``UNCLAIMED_RUN_SQL`` for ``run_column`` (a code-supplied column name)."""
    return UNCLAIMED_RUN_SQL.format(run=run_column)


_CLAIM_SQL = """
    WITH candidates AS (
        SELECT pr.run_id
        FROM production_runs pr
        WHERE pr.site = %(site)s
          AND pr.status = 'RELEASED'
          AND EXISTS (SELECT 1 FROM sites s WHERE s.site_code = pr.site AND s.is_active)
          AND EXISTS (SELECT 1 FROM simulation_config sc
                      WHERE sc.config_key = %(key)s
                        AND lower(btrim(sc.config_value)) = %(external)s)
          AND NOT EXISTS (SELECT 1 FROM mes_work_orders mwo WHERE mwo.production_run_id = pr.run_id)
          AND NOT EXISTS (SELECT 1 FROM external_mes_runs emr WHERE emr.run_id = pr.run_id)
        ORDER BY pr.created_at, pr.run_id
        LIMIT %(limit)s
        FOR UPDATE OF pr SKIP LOCKED
    ), claimed AS (
        INSERT INTO external_mes_runs (run_id, site_code, owner)
        SELECT run_id, %(site)s, %(owner)s FROM candidates
        ON CONFLICT (run_id) DO NOTHING
        RETURNING run_id
    )
    UPDATE production_runs pr
       SET status = 'IN_PROGRESS',
           actual_start_time = COALESCE(pr.actual_start_time, NOW()),
           updated_at = NOW()
      FROM claimed, products p
     WHERE pr.run_id = claimed.run_id
       AND p.product_id = pr.product_id
    RETURNING pr.run_id, pr.run_number, p.sku, pr.planned_quantity
"""

_COMPLETE_SQL = """
    WITH done AS (
        UPDATE external_mes_runs emr
           SET completed_at = NOW(),
               external_ref = COALESCE(%(ref)s, emr.external_ref)
         WHERE emr.run_id = %(run_id)s
           AND emr.completed_at IS NULL
           AND (%(owner)s::text IS NULL OR emr.owner = %(owner)s)
           AND EXISTS (SELECT 1 FROM production_runs pr
                       WHERE pr.run_id = emr.run_id AND pr.status = 'IN_PROGRESS')
        RETURNING emr.run_id
    )
    UPDATE production_runs pr
       SET status = 'COMPLETED',
           actual_quantity = %(quantity)s,
           actual_end_time = NOW(),
           updated_at = NOW()
      FROM done
     WHERE pr.run_id = done.run_id
    RETURNING pr.run_id
"""

_RELEASE_SQL = """
    WITH released AS (
        DELETE FROM external_mes_runs emr
         WHERE emr.run_id = %(run_id)s
           AND emr.completed_at IS NULL
           AND (%(owner)s::text IS NULL OR emr.owner = %(owner)s)
           AND EXISTS (SELECT 1 FROM production_runs pr
                       WHERE pr.run_id = emr.run_id AND pr.status = 'IN_PROGRESS')
        RETURNING emr.run_id
    )
    UPDATE production_runs pr
       SET status = 'RELEASED',
           actual_start_time = NULL,
           updated_at = NOW()
      FROM released
     WHERE pr.run_id = released.run_id
    RETURNING pr.run_id
"""

_EXTERNAL_REF_SQL = """
    UPDATE external_mes_runs
       SET external_ref = %(ref)s
     WHERE run_id = %(run_id)s
       AND completed_at IS NULL
       AND (%(owner)s::text IS NULL OR owner = %(owner)s)
"""


def _owner(owner) -> str:
    owner = str(owner or '').strip()
    if not owner or len(owner) > 100:
        raise ValueError("owner must be a non-empty name of at most 100 characters")
    return owner


def claim_runs(cursor, site_code: str, owner: str, limit: int = 10) -> list[dict]:
    """Claim up to ``limit`` RELEASED runs at ``site_code`` for ``owner``.

    Only when the site is active and its backend is ``external``, and only
    runs with no built-in work order and no claim yet. Each claimed run is set
    IN_PROGRESS in the same statement. Returns
    ``[{run_id, run_number, sku, planned_quantity}, ...]`` (empty when nothing
    was claimable). The caller commits."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    params = {
        'site': site_code, 'key': mes_backend_config_key(site_code),
        'external': MES_BACKEND_EXTERNAL, 'owner': _owner(owner), 'limit': limit,
    }
    cursor.execute(_CLAIM_SQL, params)
    return [{'run_id': run_id, 'run_number': run_number, 'sku': sku,
             'planned_quantity': planned_quantity}
            for run_id, run_number, sku, planned_quantity in cursor.fetchall()]


def complete_run(cursor, run_id, quantity: int, owner: str | None = None,
                 external_ref: str | None = None) -> bool:
    """Complete a claimed IN_PROGRESS run with ``quantity`` good units.

    ``owner``, when given, must be the claim's owner. Returns False (nothing
    changed) for a run that is not an open claim. The caller commits;
    ``complete_production`` deposits the finished goods on the next cycle."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise ValueError("quantity must be a non-negative integer")
    params = {
        'run_id': str(run_id), 'quantity': quantity,
        'owner': None if owner is None else _owner(owner), 'ref': external_ref,
    }
    cursor.execute(_COMPLETE_SQL, params)
    return cursor.fetchone() is not None


def release_claim(cursor, run_id, owner: str | None = None) -> bool:
    """Hand an unfinished claimed run back (RELEASED, claim removed), so the
    site's current backend takes it next. Returns False when it was not an
    open claim. The caller commits."""
    params = {'run_id': str(run_id), 'owner': None if owner is None else _owner(owner)}
    cursor.execute(_RELEASE_SQL, params)
    return cursor.fetchone() is not None


def record_external_ref(cursor, run_id, external_ref: str, owner: str | None = None) -> bool:
    """Store the vendor's record id on an open claim. Returns False when the
    run is not an open claim. The caller commits."""
    if not str(external_ref or '').strip() or len(str(external_ref)) > 255:
        raise ValueError("external_ref must be a non-empty id of at most 255 characters")
    params = {'run_id': str(run_id), 'ref': str(external_ref),
              'owner': None if owner is None else _owner(owner)}
    cursor.execute(_EXTERNAL_REF_SQL, params)
    return cursor.rowcount == 1
