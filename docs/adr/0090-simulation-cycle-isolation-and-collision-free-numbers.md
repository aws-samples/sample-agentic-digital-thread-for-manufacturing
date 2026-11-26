<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0090: Simulation cycle isolation and collision-free business numbers

## Status

**Proposed: 2026-09-30.** Implemented (offline unit tests plus
real-PostgreSQL tests; not yet deployed). Amends
[ADR-0085](0085-live-story-clock-history-and-lookup.md) and
[ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md): the
story step and the telemetry step now run BEFORE the simulator steps, not
after the cycle.

## Context

On the live account the ERP simulation Lambda `erp-simulation-orchestrator`
(`rate(1 minute)`, 300 s timeout) timed out on almost every invocation from
2026-09-29 ~19:30Z.

- `utils.generate_invoice_number` drew 5 random digits per day
  (`INV-YYYYMMDD-XXXXX`, 100,000 values). Once a day held thousands of
  invoices, inserts hit `invoices_invoice_number_key` (birthday collisions).
- The whole cycle ran in ONE transaction. The failed statement aborted it,
  every later statement failed, and the cycle's shipments rolled back.
  "Orders ready to ship" grew from 15 to 12,596.
- Every cycle re-read the whole backlog. Cycles passed 60 s, overlapped, and
  then all timed out.
- The story step and the telemetry step ran AFTER the cycle, so a timed-out
  invocation never ran the workshop's clock.

The frozen account showed the same class on `purchase_orders_po_number_key`.

## Decision

1. **Business numbers come from a PostgreSQL sequence, not from randomness.**
   `erp_simulation/utils.py::next_business_number(cursor, prefix)` returns
   `PREFIX-YYYYMMDD-N...N`. The digits are `nextval('seq_<kind>_number')`
   modulo the prefix's cap. There is one sequence per prefix, created with
   `CREATE SEQUENCE IF NOT EXISTS` on first use. The orchestrator also
   ensures every sequence at the start of each cycle, in its own commit. The
   formats are unchanged. Numbers are unique within a day as long as that day
   issues fewer numbers than its **daily cap**:

   | Prefix | Sequence | Digits | Daily cap | Why the cap |
   |---|---|---|---|---|
   | `INV`, `PAY`, `PO` | `seq_invoice_number`, `seq_payment_number`, `seq_po_number` | 5 | 100,000 | the full 5-digit space |
   | `RUN`, `WO` | `seq_run_number`, `seq_work_order_number` | 5 | 40,000 each (RUN 0-39,999, WO 40,000-79,999) | `story_history` owns 9xxxx, `story_live` owns 8xxxx; disjoint halves so an unrelated run and work order never share digits (amendment 2026-11-06) |
   | `ORD`, `SHIP` | `seq_order_number`, `seq_shipment_number` | 8 | 40,000,000 each (ORD 0-39,999,999, SHIP 40,000,000-79,999,999) | `story_history` owns 9xxxxxxx; disjoint halves as for RUN/WO (amendment 2026-11-06) |

   The sequence is global rather than per day, so any window of consecutive
   values shorter than the cap is distinct. Past the cap, numbers wrap and
   rely on (2). Tracking, payment reference and MES consumption lot numbers
   have no UNIQUE constraint and stay random. Receipt lots
   (`LOT-<po_number>-NN`) take their uniqueness from the PO number.

2. **A UNIQUE insert is savepoint-isolated: retry once, then skip.**
   `utils.insert_numbered(cursor, prefix, sql, params_for)` runs the INSERT
   under `SAVEPOINT adt_numbered_insert`. On SQLSTATE 23505 it rolls back to
   the savepoint and retries once with the next number. On a second conflict
   it skips that row with a WARNING and returns `None`, and the transaction
   stays usable. A skipped row is still pending (no invoice for the shipment,
   the order still `READY_TO_SHIP`), so the next cycle picks it up. Conflicts
   come from numbers issued before this ADR (random ones on the same day) or
   from a daily wrap. The invoice, payment, PO, planned-run and backorder-run
   loops also run each row under its own savepoint, so a failure on one row
   never aborts the rows after it.

3. **Each simulator step is its own transaction.**
   `SimulationOrchestrator._run_simulation_cycle` walks the table-driven
   `_SIMULATION_STEPS`, in the same order as before. `_run_step` commits each
   step alone. When a step fails, the orchestrator rolls back only that step,
   logs `Cycle step <name> failed and was rolled back alone`, and counts it in
   `errors`. A step that swallowed an error inside itself and left the
   transaction aborted (`TRANSACTION_STATUS_INERROR`) is also detected and
   rolled back. Before this, `COMMIT` would have rolled it back silently.
   Metrics count only committed work.

4. **The clock steps run first; the simulator steps run under a time budget.**
   `_run_cycle` order:
   1. load the story clock;
   2. the live-story step (`_run_story_step`);
   3. the MES condition monitor (`MESSimulator.run_condition_monitor`; the
      downtime step reuses the result for the same cycle id);
   4. the SiteWise telemetry step (`_run_story_telemetry`);
   5. ensure the number sequences and count the backlog;
   6. the simulator steps.

   Each clock step runs on its own best-effort connection and never raises.
   When the cycle has used more than `CYCLE_TIME_BUDGET_SECONDS = 150`
   (half the Lambda timeout), the orchestrator skips the remaining simulator
   steps and lists them in the summary. They run next cycle. The simulation
   event row and the CloudWatch metrics are always written.

5. **The bulk steps read a bounded, oldest-first batch.** `create_shipments`,
   `generate_invoices` and `process_payments` each read at most
   `utils.MAX_ROWS_PER_CYCLE = 200` rows (`ORDER BY` age, `LIMIT`,
   `FOR UPDATE SKIP LOCKED`). An overlapping invocation therefore takes a
   disjoint batch instead of blocking on, or double-invoicing, the same rows.

6. **Backlog recovery is the bounded batch working.** There is no one-off
   migration. Each cycle counts `READY_TO_SHIP` orders into the summary. Above
   `BACKLOG_WARN_THRESHOLD = 2000` it logs `Shipping backlog: N orders ready
   to ship (> 2000); draining at most 200 per cycle (~M cycles)`. The live
   account's 12,596 orders drain in about 63 cycles, net of new orders.

7. **One INFO summary line per cycle**, logged even when the cycle raises:
   `Cycle <n> summary: total=<s> budget=150s skipped=<steps|-> errors=<n>
   ready_to_ship=<n> orders_created=<n> ... | clock=<s> story=<s>
   monitor=<s> telemetry=<s> sequences=<s> backlog=<s> orders=<s> ...
   events=<s>`. It replaces the multi-line cycle summary and the per-step
   INFO lines.

## Consequences

- One number conflict costs at most one row for one cycle. It can no longer
  abort a cycle or roll back sibling steps.
- The story step now runs before the MES steps. In a story cycle it can start
  the LINE3 story run before the MES simulator schedules that line. ADR-0085
  already makes any RUNNING order on the line wait for the other.
- The shipping, invoice and payment throughput ceiling is 200 per minute each.
  The simulator's steady-state inflow is far below that.
- Characterization goldens that pin SQL traces filter the new savepoint
  wrappers (`erp_simulation/tests/_numbering.py`), so they still pin the
  business SQL byte for byte. The shipping golden records the bounded read
  explicitly.
- Verified by `erp_simulation/tests/test_business_numbers.py` and
  `erp_simulation/tests/test_cycle_isolation.py`. The PostgreSQL tests use real
  23505 conflicts on `invoices` and `purchase_orders`, a swallowed conflict
  that rolls back only its step, and a 450-order backlog drained as
  200 + 200 + 50.

## Amendment 2026-09-30: hot-path indexes, required steps, double shipping batch

The live account then logged `total=245.06s budget=150s ready_to_ship=14107
... shipments=238.55s` with every other step under 0.2 s. One bounded batch
of 200 orders took about 1.2 s per order.

- **Cause (EXPLAIN ANALYZE).** A PostgreSQL fixture had 14,000 READY orders
  with 3 lines each and 1.5M COMPLETED runs with lot usage. On it,
  `ShippingSimulator._traceable_batch_id` ran once per line item. Each call
  did a bitmap heap scan of every run of the product (44k rows, 31,868
  buffers) and then sorted them to keep one: 240 ms per line. The only index
  it could use was `production_runs(product_id)`. So the cost grew with the
  size of `production_runs`, not with the batch.
- **Fix.** `erp_simulation/hot_path_indexes.py` adds two indexes.
  `idx_production_runs_traceable (product_id, status, actual_end_time DESC
  NULLS LAST, updated_at DESC NULLS LAST)` matches the lookup's filter and
  its ORDER BY, so the lookup becomes an index walk that stops at the first
  traceable run (9 buffers, 0.03 ms). `idx_customer_orders_status_order
  (status, order_id)` serves the READY batch read and the backlog count.
  The batch also memoises the lookup per product. db-init creates both
  indexes (`HOT_PATH_INDEXES_SQL`, mirrored in `schema/03` and `schema/05`,
  held equal by a parity test). An existing account gets them from the
  orchestrator: every cycle it runs `ensure_hot_path_indexes`, which is one
  catalog read when they exist. When they don't, it runs a CREATE under a
  10 s `lock_timeout`, once. On the fixture, 200 orders went from 32.15 s to
  1.34 s.
- **Required steps first.** `shipments`, `invoices` and `payments` are now
  the first simulator steps, right after the clock steps. They are
  `required` (point 4's budget skips only the OPTIONAL steps), so each gets
  one bounded batch every cycle.
- **Double shipping batch.** While `ready_to_ship > BACKLOG_WARN_THRESHOLD`,
  the shipping step may take up to `BACKLOG_SHIPMENT_BATCHES = 2` batches per
  cycle. Each batch is its own transaction and is timed as `shipments_2`. A
  second batch runs only if the first made progress and a batch of the same
  length still fits in the budget. The warning now reads `draining up to
  2 x 200 per cycle while above 2000 (~M cycles)`: a 14k backlog drains in
  about 36 cycles. Verified by `erp_simulation/tests/test_shipping_backlog_drain.py`.

## Amendment 2026-10-02: the story history on an account with older numbers

Before this ADR the simulator numbered runs and MES work orders at random, so
an account deployed before it can hold a run or a work-order number inside
the live story's `9xxxx` history range (seen on a long-lived deployment: a
Detroit run held `WO-20260929-93004`, and every placement that day failed
with a NotNullViolation). The story history writer
(`erp_simulation/story_history.py::_runs_held_elsewhere`) now reads, before
it writes, which planned runs another run already holds by number or by
work-order number. Those runs are left out, with the order that ships them,
and the report counts them as `skipped_runs`. The older rows are never
changed. The failed batch is never left out: when its numbers are held, the
writer raises `NumberCollision` and writes nothing. A fresh account matches
nothing, so its history is unchanged.

## Amendment 2026-10-23: hot-path indexes are built concurrently on an existing account

The 2026-09-30 amendment's CREATE under a 10 s `lock_timeout` bounded only the
wait for the table lock, not the build. Once it had the lock, a plain
`CREATE INDEX` blocked every writer of the table for the whole build, with no
bound on how long that took. On a large table it could run past the Lambda's
300 s timeout, roll back and start again the next cycle. Now
`ensure_hot_path_indexes` builds every missing hot-path index with
`CREATE INDEX CONCURRENTLY IF NOT EXISTS` on the step connection, switched to
autocommit for the pass. Each statement runs under a `statement_timeout` set to
what is left of `INDEX_BUILD_BUDGET_SECONDS` (60 s for the whole pass). A
build that fails or runs out of budget is logged and skipped for that cycle,
and the cycle's steps still run. A failed concurrent build leaves an INVALID
index (`pg_index.indisvalid`), and the next pass drops it with
`DROP INDEX CONCURRENTLY` and builds it again. Only one pass runs at a time,
because the orchestrator holds the simulator lock. The lock's own session holds
no table lock and no snapshot between its heartbeats, so the build does not wait
for it. db-init still creates the indexes with plain `CREATE INDEX` on its
empty tables. Verified by `erp_simulation/tests/test_hot_path_index_build.py`.

## Amendment 2026-11-06: paired prefixes draw from disjoint ranges

The RUN and WO sequences were independent but both ran modulo 80,000 into the
same 0-79,999 range, and the same held for ORD and SHIP (0-79,999,999). An
unrelated run and work order issued on the same day therefore often carried
the same digits. The story gives a run and its own work order, and an order
and its own shipment, the SAME digits on purpose (`story_history._numbers`,
`_orders`, `_run_order`; `story_live.story_work_order`), all at or above
80,000 / 80,000,000. On a live workshop seat the ERP agent matched Tulip run
`RUN-20261106-00256` to an unrelated Detroit work order `WO-20261106-00256` by
its digits and reported Detroit as the run's site.

`BUSINESS_NUMBER_SPACE` now gives each prefix a suffix `range`, and
`next_business_number` returns `range.start + nextval % len(range)`. RUN takes
0-39,999 and WO 40,000-79,999; ORD takes 0-39,999,999 and SHIP
40,000,000-79,999,999. PO, INV and PAY keep the full 0-99,999. The formats,
digit widths and sequences are unchanged, and the story ranges are untouched.
Within the simulator's own numbers, two numbers of a pair now share digits
only when they are the story's deliberate pair. The daily cap for each of
these four prefixes is now 40,000 (RUN, WO) and 40,000,000 (ORD, SHIP).

On an account deployed before this amendment the sequences keep their values,
so the next WO and SHIP numbers jump into the upper half. No same-day unique
violation follows from the switch: an old number `N' % 80,000` and a new one
`40,000 + N % 40,000` (or `N % 40,000` for RUN) are equal only when `N - N'`
is a multiple of 40,000, which needs 40,000 numbers of that prefix in one day
(40,000,000 for ORD and SHIP). Rows numbered before the deploy keep their
numbers, so a pair that already shared digits by coincidence still does.
Verified by
`erp_simulation/tests/test_business_numbers.py::test_paired_prefixes_never_share_digits_and_stay_out_of_the_story_range`.
