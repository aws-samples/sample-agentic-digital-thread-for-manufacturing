#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
simulate_local.py — generate transactional data in the local Postgres.

`scripts/seed_local_db.py` (lambda-db-init) creates the schema + reference data +
simulation config, but the transactional rows (orders, work orders, quality
checks, shipments, invoices) are produced at runtime by the `erp_simulation`
engine — which in a deployment runs continuously as an ECS task. This driver
runs that same engine for a fixed number of cycles against the local DB so
cross-system chat questions have real data to answer.

Run AFTER `scripts/local_dev.sh up` (which seeds schema + reference data).

Usage (env: POSTGRES_HOST/PORT/DB/USER/PASSWORD, as in .env.local):
    .venv-local/bin/python scripts/simulate_local.py --cycles 30
"""
import argparse
import os
import sys
import time

# The erp_simulation engine writes timestamps with naive datetime.now() and gates
# state transitions on Postgres NOW(). Postgres runs in UTC (and the columns are
# `timestamp without time zone`), so on a non-UTC host the two clocks diverge by
# the local offset — making every just-set planned_end_time look like it's already
# in the past, which completes work orders instantly (they're never observed
# RUNNING, so MES quality checks/downtime never generate). Deployment doesn't hit
# this because the ECS task runs in UTC. Align this process to UTC to match.
os.environ["TZ"] = "UTC"
time.tzset()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _connect():
    from erp_simulation import pgdriver
    return pgdriver.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "adt_demo"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=10,
    )


def _fast_forward(delta: str) -> None:
    """Age every public timestamp column by `delta`, advancing the simulated clock.

    The simulation gates state transitions on real wall-clock intervals
    (`NOW() - INTERVAL '...'`), and orders take hours, shipping days, payment
    weeks. Running cycles back-to-back never lets records age, so the pipeline
    stalls at PENDING. Shifting all timestamps back uniformly preserves relative
    ordering while making records "old enough" for the next cycle to advance —
    so the full lifecycle (orders -> production -> shipping -> invoicing) flows
    in a bounded number of cycles instead of real days.
    """
    from erp_simulation.utils import compose_sql
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%'"
            )
            cols = cur.fetchall()
            for table, col in cols:  # compose_sql quotes table and column as identifiers; delta is bound
                cur.execute(compose_sql("UPDATE public.{t} SET {c} = {c} - %s::interval WHERE {c} IS NOT NULL",
                                        t=table, c=col), (delta,))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    from erp_simulation.scenarios import list_scenarios, apply_scenario

    ap = argparse.ArgumentParser(description="Run erp_simulation cycles against local Postgres")
    ap.add_argument("--cycles", type=int, default=int(os.environ.get("SIM_CYCLES", "40")),
                    help="number of simulation cycles to run (default 40)")
    ap.add_argument("--time-step", default=os.environ.get("SIM_TIME_STEP", "1 day"),
                    help="simulated time to fast-forward after each cycle (default '1 day'; "
                         "set '0' to disable and run in real time)")
    ap.add_argument("--scenario", choices=list_scenarios(),
                    help="apply a named scenario (writes simulation_config) before running cycles, "
                         "e.g. equipment_down, quality_issues, supply_crisis")
    ap.add_argument("--loop", action="store_true",
                    help="run continuously — one cycle every --interval seconds (local stand-in for "
                         "the deployed rate(1 minute) simulation Lambda); Ctrl-C to stop")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("SIM_INTERVAL", "60")),
                    help="seconds between cycles in --loop mode (default 60)")
    args = ap.parse_args()

    if not os.environ.get("POSTGRES_PASSWORD"):
        print("POSTGRES_PASSWORD not set — source .env.local first.", file=sys.stderr)
        return 2

    fast_forward = args.time_step.strip() not in ("", "0")

    # Point the simulation engine at local Postgres; no Secrets Manager, no CloudWatch.
    os.environ["USE_SECRETS_MANAGER"] = "false"
    os.environ["CLOUDWATCH_ENABLED"] = "false"
    os.environ["DB_HOST"] = os.environ.get("POSTGRES_HOST", "localhost")
    os.environ["DB_PORT"] = os.environ.get("POSTGRES_PORT", "5432")
    os.environ["DB_NAME"] = os.environ.get("POSTGRES_DB", "adt_demo")
    os.environ["DB_USER"] = os.environ.get("POSTGRES_USER", "postgres")
    os.environ["DB_PASSWORD"] = os.environ["POSTGRES_PASSWORD"]
    os.environ.pop("DB_SECRET_NAME", None)

    # Pre-seed the cached config from env so the orchestrator doesn't attempt a
    # Secrets Manager round-trip on construction.
    from erp_simulation import config as sim_config
    sim_config.get_config(use_secrets_manager=False)

    # Apply a named scenario (downtime, quality issues, supply crisis, …) before
    # running cycles. Shared definitions live in erp_simulation/scenarios.py, so
    # the CLI and the Streamlit dashboard buttons stay in sync.
    if args.scenario:
        conn = _connect()
        try:
            scenario = apply_scenario(conn, args.scenario)
            conn.commit()
            print(f"applied scenario: {args.scenario} ({scenario['name']})")
        finally:
            conn.close()

    from erp_simulation.simulation_orchestrator import SimulationOrchestrator

    orch = SimulationOrchestrator()

    if args.loop:
        print(f"sim-loop: one cycle every {args.interval:g}s "
              f"(time-step={args.time_step}); Ctrl-C to stop")
        n = ok = 0
        try:
            while True:
                n += 1
                res = orch.run_single_cycle()
                if res.get("success"):
                    ok += 1
                    print(f"  cycle {n}: ok")
                else:
                    print(f"  cycle {n}: FAILED: {res.get('error')}", file=sys.stderr)
                if fast_forward:
                    _fast_forward(args.time_step)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\nstopped after {n} cycles ({ok} ok)")
            return 0

    ok = 0
    for i in range(args.cycles):
        res = orch.run_single_cycle()
        if res.get("success"):
            ok += 1
        else:
            print(f"  cycle {i + 1}/{args.cycles} FAILED: {res.get('error')}", file=sys.stderr)
        if fast_forward:
            _fast_forward(args.time_step)
    print(f"ran {ok}/{args.cycles} cycles successfully (time-step={args.time_step})")
    return 0 if ok == args.cycles else 1


if __name__ == "__main__":
    sys.exit(main())
