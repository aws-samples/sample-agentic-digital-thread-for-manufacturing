<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0091: The attendee places the plant clock; the loop length is packaged

## Status

**Proposed: 2026-09-30.** Verified offline only; not yet verified in a
deployed account.

Builds on [ADR-0089](0089-placeable-and-packaged-story-clock.md), which made
the clock placeable and the anchor packageable, and on
[ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md), the
one story clock. It does not change ADR-0089's Lambda `place` contract.

## Context

Workshop Studio gives each attendee their own AWS account. The facilitator
cannot sign in to those accounts, and event operators cannot set
CloudFormation parameters at event time. ADR-0089 still assumed that a
steward places the clock (`scripts/place_story_clock.py`) or that the
package pins it (`--story-anchor`). Neither covers an event whose start time
is known only on the day.

So the ATTENDEE places the clock from the app UI. The packaged anchor stays
as the fallback for an attendee who never places it. The 8 h loop is too
long for a half-day event, so the loop length also becomes a package-time
parameter. Its default must leave every template and constant unchanged.

## Decisions

### D1. One wire name: `dry_run`

`POST /api/admin/workshop/story/clock` keeps `dry_run` as its only preview
field (`extra="forbid"` holds). The UI button is labelled "Preview" and sends
`dry_run: true`.

### D2. The dry run stays Lambda-free

This is ADR-0089's rule, and it still holds. The dry run returns the resolved
`alarm_at`, the new anchor, `loops_shifted` and a fixed `effect` text list:
"rewrites the three-day history and every run number" and "resets the sensor
backfill, about 10 minutes". It has no `impact` counts and there is no
`preview` Lambda action (an earlier draft had one; it was dropped).

### D3. The attendee places the clock in module 1

The UI asks for "alarm in about 90 minutes". The app turns
`alarm_in_minutes` into an absolute `alarm_at` BEFORE it invokes the Lambda,
so the Lambda `place` contract does not change. A placement rewrites the
three-day history, so every run number changes and module 2's answers depend
on it. Placing the clock after module 2 would invalidate those answers.
Module 6 only READS the clock, through the chip.

### D4. The admin Workshop tab is inserted as JSX only

`AdminPage.tsx` is one function of more than 50 lines. The tab is added with
about 4 inserted lines. The new components gate themselves, and AdminPage
gains no new state or hooks.

### D5. Only the loop length is packaged; everything else is derived

The alarm offset is NOT an independent setting. `BPFO_ALARM_STEP` follows
from `BEARING_FAIL_STEP` through the BPFO growth law, and making it separate
would break "the trigger is from the vibration data" (ADR-0073). There is one
parameter, with three spellings:

| Layer | Name | Owner |
|---|---|---|
| process env | `STORY_LOOP_SECONDS` | `erp_simulation/condition_monitor.py` |
| CDK context | `story_loop_seconds` | `stacks/_workshop_story.resolve_loop_seconds` / `loop_from_node` |
| package flag | `--story-loop-minutes M` (live only) | the Workshop Studio packager, manifest `story_loop_seconds` |

- **Validation.** Both readers apply the same rules. The value must be a
  base-10 integer, a multiple of 300 s, and within [3600, 28800] inclusive (longer loops let the failed batch overlap a normal line-3 run; 42600 to 51300 s and 75600 s and up were measured).
  Anything else raises `ValueError`. `condition_monitor` fails CLOSED at
  import, and the synth refuses the value first.
- **Default.** Unset or blank means 28800 s. The CDK side also treats an
  explicit 28800 as unset (`None`), so a default package emits no key and no
  property.
- **Where it is set.** Only on a live synth. It goes on the
  simulation-orchestrator Lambda, the story-history Lambda (env) and the app
  container (`adt_compute_stack._env_story_clock`), and it appears as the
  history CR property `LoopSeconds`. A loop set on a frozen or non-workshop
  synth raises.
- **Read once.** `condition_monitor` reads the env ONCE, at import, in
  module-level code. This is the module's one exception to "no I/O". Every
  story module derives its constants from `cm` at import, so the value
  propagates with no call-site edits.
- **Byte-identical default.** With the env unset, every constant is
  unchanged: `TOTAL_SECONDS 28800`, `NUM_STEPS 5760`, `BEARING_FAIL_STEP
  5184`, `BPFO_ALARM_STEP 3898`, `BEARING_REPAIR_STEPS 240`,
  `ALARM_OFFSET_SECONDS 19490`, `ANCHOR_RESIDUE 10`. So are the plan golden
  and every synth. `tests/unit/test_condition_monitor.py` pins these as
  literals, recomputes them from first principles, and checks both with a
  fresh-interpreter import.

### D6. The facilitator Cognito group (A10) is deferred

Attendees now place the clock and read the chip through the same
steward-gated GET. A facilitator-only gate on that GET would hide both from
them. If A10 is pursued later, split the endpoint in two: a cheap clock-only
GET for any authenticated user, and a facilitator-only answer key.

### D7. One shared query key with a 60 s poll

The header chip and the Workshop tab share one query key. That is one
request per minute per attendee, which suits one attendee per account.
Attendees can see the answer key in the network tab. ADR-0089 already
accepts this ("shared login is Admin").

## Derived geometry (computed from the code)

The values below come from `condition_monitor` itself, run in a fresh
interpreter under each value of `STORY_LOOP_SECONDS`. The 3 h column
confirms the plan's D5 numbers.

| Quantity | Formula | 8 h (default) | 3 h (`10800`) |
|---|---|---|---|
| steps per loop | `TOTAL_SECONDS // 5` | 5760 | 2160 |
| fail step | `int(0.9 · N)` | 5184 | 1944 |
| BPFO alarm step | `ceil(sqrt((0.70-0.05)/(1.20-0.05)) · fail)` = `ceil(0.75181 · fail)` | 3898 | 1462 |
| alarm offset | `alarm · 5` | 19490 s | 7310 s |
| anchor residue | `(-offset) % 300` | 10 | 190 |
| alarm window | `(fail - alarm) · 5` | 6430 s (107 min) | 2410 s (40 min) |
| checks per window | `ceil(window / 900)` | 8 | 3 (alarm, +15 min, +30 min) |
| story run planned qty | `floor(window / 60)` | 107 | 40 |
| repair | `min(240, N // 24)` steps | 240 (20 min) | 90 (7.5 min) |
| healthy after repair | `N - fail - repair` | 336 steps (28 min) | 126 steps (10.5 min) |
| loops in the 72 h history | `259200 / loop` | 9 | 24 |

- **How the growth law scales.** The BPFO band grows with the square of the
  degradation fraction `step / fail_step`. It crosses 0.70 mm/s at the fixed
  fraction 0.7518 of the fail step, which is 67.7 % of any loop. Only the
  ceiling rounding moves the step. So the alarm, the reject-fraction climb
  (0.15 to 0.40 across the window) and every spectral value at a given
  FRACTION of the loop are the same for every loop length. The absolute
  times compress.
- **Grid.** A loop that is a multiple of 300 s is a whole number of grid
  slots. With a congruent anchor (`% 300 == ANCHOR_RESIDUE`), every loop's
  alarm instant is therefore a grid point. The fail and repair instants are
  NOT grid points, for 8 h or for 3 h (8 h: `anchor + 25920`, residue 130;
  3 h: `anchor + 9720`, residue 10). This is unchanged from ADR-0088, which
  aligns only the alarm.

### Spike finding 1: the fixed repair overran a 3 h loop

The shipped 20 min repair (240 steps) does not fit after a 3 h loop's
failure. 1944 + 240 = 2184 > 2160 steps, so it overran by 120 s. The loop
would wrap mid-repair, the healthy phase would vanish, and the history's MES
downtime and CMMS repair orders would end inside the next loop. With a fixed
repair, only loops of 12300 s or more fit.

Decision: the repair is `min(240, NUM_STEPS // 24)` steps, which is 1/24 of
the loop capped at the shipped 20 min. The result is exactly 240 for 8 h and
anything longer, so the default is byte-identical. Every loop in
[3600, 28800] then keeps a positive healthy phase. The validator rules stay
identical to `resolve_loop_seconds`, and no repair-length rule is
needed.

### Spike finding 2: the history's failed batch had 8 h-only offsets

`story_history` placed the failed batch 600 s into the alarm window, with
checks at +1800, +3600 and +5100 s and a 5400 s run. All of these assume the
6430 s window. Under 3 h, two of the three checks would land after the
bearing stops (0 fails, a nonsense "BPFO 0.05") and the run would span the
failure.

Decision: at module level, `FAILED_OFFSET`, `FAILED_CHECK_OFFSETS` and the
new `FAILED_RUN_SECONDS` scale with `ALARM_WINDOW_SECONDS /
SHIPPED_ALARM_WINDOW_SECONDS`, floored to whole minutes. With the env unset
they are the literals 600, (1800, 3600, 5100) and 5400, so the plan golden is
unchanged. The one use-site change is in `_failed_run` (15 lines, not a
grandfathered function), which now uses `FAILED_RUN_SECONDS`. The 3 h values
are offset 180, checks (660, 1320, 1860) and run 1980 s. The checks fail
5/6/7 of 20 and the batch rejects 18 of 60 (30 %), the same shape as 8 h.

No edit fell inside a function of more than 50 lines, so there is no blocker.

### Story-history impacts at 3 h

- `story_history.plan(anchor, 3)` still writes 18 normal runs and ONE failed
  batch, 19 unique run numbers. The history loop is still the one containing
  `anchor - 1 d`. Because 86400 is a multiple of 10800, it starts exactly
  one day back, and its alarm is about 2 h into that day.
  `HISTORY_LOOP_OFFSET_SECONDS` (86400) and `story_telemetry_step`'s
  `HISTORY_SECONDS` (3 d) are day-based, not loop-based, and do not change.
- Pre-anchor telemetry is healthy except in the one history loop. From the
  anchor on, the bearing alarms every 3 h instead of every 8 h. That means
  8 condition orders (`VIB-CMX-<loop>`) and 8 story runs per day instead of
  3, and `era_alarms` / `story_run_numbers` retire correspondingly more on
  a placement.
- Module 6 narrative: "about a day ago it climbs" still describes the
  HISTORY loop. The live trend now shows a full degrade, alarm, repair cycle
  every 3 h, so the workshop content must say "the next alarm is in N minutes"
  rather than relying on one climb per session.
- `_write_run`'s planned rate still divides by `RUN_SECONDS` (90 min). For
  the shortened failed batch that is a nominal rate, not the batch's actual
  pace. This is cosmetic, and the function is left unchanged.

### Update semantics when `LoopSeconds` changes

A CR Update whose `LoopSeconds` was set, changed or unset re-plans the
STORED anchor under the new geometry, keeping the anchor's source
(`lambda_story_history._replan_stored`). `story_replan.replan`
normalises it (`snap_anchor(..., "nearest")`) to the NEW congruence, so the
anchor moves by less than 150 s plus whole loops. With no stored anchor, the
Update is the first write. A changed `Anchor` takes precedence over a changed
`LoopSeconds` (packaged re-plan). The CDK side refuses a loop on a non-live
synth, so a frozen deploy never sees the property.

The alternative, refusing a loop change, was rejected. A re-package with a
new loop would then need a stack delete, which destroys the Aurora data.

### Out of scope: the OPC UA / HighByte dataset

`highbyte/generate_datasets.py` imports `condition_monitor` with the env
unset (the generator runs in `deploy.sh --highbyte`, never with the story
env). Its dataset stays 8 h. It is not on the live-story path, and
`test_condition_monitor.py::test_generator_trend_is_the_shared_trend` still
pins it.

### Every module that reads `cm.*` at import

| File | Edit? | Why |
|---|---|---|
| `erp_simulation/condition_monitor.py` | yes | the env read, the validator, the derived repair |
| `erp_simulation/story_history.py` | yes | module-level failed-batch scaling; `_failed_run` uses `FAILED_RUN_SECONDS` |
| `erp_simulation/story_telemetry.py` | docstring only | the 8 h history-loop wording |
| `erp_simulation/story_live.py` | no | `DEFECT_CHECKS_PER_WINDOW`, `STORY_PLANNED_QUANTITY` derived |
| `erp_simulation/story_replan.py` | no | `LOOP_SECONDS`, `PHASE_STEPS` derived |
| `erp_simulation/story_telemetry_step.py`, `story_sitewise.py` | no | day-based windows; grid from `cm` |
| `erp_simulation/mes_simulator.py`, `simulation_orchestrator.py` | no | call `cm` at runtime |
| `lambda_/lambda_story_history.py` | yes | `LoopSeconds` Update re-plan |
| `stacks/_workshop_story.py`, `stacks/erp_lambda_stack.py`, `stacks/adt_compute_stack.py` (`_env_story_clock` only) | yes | context, env on 3 targets, CR property |
| the Workshop Studio packager | yes | `--story-loop-minutes`, manifest, `check_story_loop` |
| `agentic_digital_thread/agents/demo/story_clock.py` | yes | hardcodes `LOOP_SECONDS=28800` and `3898/5184/240/5760`; must derive them, including the repair `min(240, N // 24)`, with the same validation. `ALARM_IN_MINUTES_RANGE (5, 480)` exceeds a 3 h loop; values above one loop shift whole loops (`loops_shifted`) |
| `agentic_digital_thread/agents/demo/story_lookup.py` | no | reads the STORED geometry |
| `highbyte/generate_datasets.py`, `highbyte/DeploySitewiseModels.py` | no | 8 h dataset; comment only |
| tests: `tests/unit/test_condition_monitor.py`, `erp_simulation/tests/test_story_{replan,telemetry,live,history}.py`, `tests/test_workshop_story_context.py`, `tests/test_erp_lambda_story_history.py`, `tests/test_adt_compute_env_wiring.py`, the Workshop Studio packager, `tests/unit/test_story_clock_demo.py`, `tests/unit/test_workshop_story_lookup.py` | yes | pins both ways |

## Consequences

- An attendee can put their own account into any phase from the UI, with no
  facilitator access and no event-time parameters. An account that is never
  placed runs on the packaged anchor.
- A 3 h package fits a half-day agenda. Placing the alarm about 90 min out in
  module 1 lands it in module 6. The loop then repeats every 3 h, so a late
  module 6 still sees an alarm within 3 h.
- With the env unset, the default package, every synth and the plan golden
  are byte-identical. The byte-identical guarantee is pinned by tests, not by
  convention.
- A bad `STORY_LOOP_SECONDS` in a runtime env fails the import. This is loud
  and deliberate, and synth validation makes it unreachable in practice.
- Short loops produce more story rows per day (8 condition orders and 8
  story runs at 3 h), and a placement retires more of them.
- The app mirror (`agents/demo/story_loop.py`, which `agents/demo/story_clock.py`
  binds from) must stay in parity with `condition_monitor` (a parity test pins
  it). Both compute the geometry with a pure `geometry_for(loop_seconds)`, so
  the test checks every valid loop length in one process; only the env read
  at import needs a child interpreter (amendment 2026-10-09).

## Alternatives considered

- **An event-time CFN parameter or a facilitator-run placement.** Workshop
  Studio offers neither at event time. Rejected.
- **A separate alarm-offset setting.** This would break the
  vibration-derived trigger (ADR-0073). Rejected (D5).
- **A fixed 20 min repair with a validator that rejects loops under
  12300 s.** This was the first draft. It rules out the 3 h target
  package and would make the synth-side and import-side rules disagree.
  Rejected in favour of the capped proportional repair.
- **Scaling the repair proportionally without a cap.** This changes loops
  longer than 8 h for no benefit (a 24 h loop would repair for 1 h).
  Rejected.
- **Reloading modules in-process in tests.** This leaves already-imported
  dependents on mixed geometry for the rest of the lane. Rejected. Tests use
  a fresh interpreter (`erp_simulation/tests/_loop_env.py::run_under_loop`).
- **A dry run that invokes a read-only Lambda for impact counts.** Rejected
  (D2); ADR-0089's Lambda-free dry run stands.

## Amendment 2026-09-30: Design C (auto-place, provenance chip, reset failsafe)

The walk showed that attendees skip the module 1 placement, so their account
runs on the packaged (or deploy-time) anchor, and nothing brings a mis-placed
account back. Design C adds three things. None of them changes ADR-0089's
Lambda `place` contract, and the `place` payload of every existing form is
byte-identical (pinned by `test_existing_forms_send_byte_identical_payloads`).

### C1. Auto-place on first sign-in: the backend publishes a policy only

- The lookup's live `clock` gains `auto_place: {enabled, minutes}`. It is
  read from env `STORY_AUTO_PLACE_MINUTES` on the ADT compute task. Unset,
  blank or `0` means `{enabled: false, minutes: 0}`. A whole number from 5
  to `min(stored loop, env STORY_LOOP_SECONDS loop)` in minutes (the place
  route's own `alarm_in_minutes` cap) means enabled. Any other value means off,
  plus ONE `clock:` warning, and the answer key still renders.
- CDK context `story_auto_place_minutes` (live only, validated like the
  loop: 0 is off, otherwise 5 to loop/60) goes ONLY to the compute task env
  (`stacks/_workshop_story.py::auto_place_from_node`,
  `ADTComputeStack._env_story_clock`). Without the context, every template
  is byte-identical.
- `package.py --profile workshop --story live` sets 80 by default, capped at
  one loop. With `--story-anchor` and no policy flag the default is OFF, with
  a WARNING (the facilitator aligned the anchor deliberately).
  `--auto-place-minutes N` overrides it, and `--no-auto-place` (or
  `0`) turns it off. The value is recorded in `adt-story-anchor.json` and in
  `asset-manifest.json` as `story_auto_place_minutes`.
  `packaging_story.check_story_auto_place` (re-exported by `packaging_lib`) requires the env on exactly one
  compute container and on no Lambda.
- The frontend decides when to fire. Auto-place fires once per page load
  while the clock is still the packaged or deploy default; a reset
  suppresses the next load once and then re-arms auto-place; a
  facilitator's own admin visit also auto-places (turn the policy off with
  `--no-auto-place` for an account you reuse to practice). It then sends the
  ordinary relative placement, `{alarm_in_minutes: minutes,
  expected_anchor_epoch: clock.anchor_epoch}`. The CAS and the in-flight
  guard make a second tab or a second attendee harmless: the loser gets a
  409.

### C2. Provenance chip

The header chip shows `anchor_source` (`placed` | `packaged` | `deploy`)
next to the phase. The attendee can then see whether the clock is their own
placement or the default. The field already existed (ADR-0089), so there is
no backend change.

### C3. Reset failsafe

- The story-history CR records the anchor it planned onto as
  `story_default_anchor` (epoch) and `story_default_source` (`packaged` |
  `deploy`). It records them on Create and on every Update that re-plans (a
  changed `Anchor` or `LoopSeconds`). Every Update, the unchanged one
  included, then back-fills them when none is stored (an account upgraded in
  place from before Design C): the packaged `Anchor` when set (`packaged`),
  else the stored anchor when its source is `deploy` | `packaged` (source
  kept), else the deploy anchor `now` snapped down (`deploy`), logged as
  `story default anchor recorded (backfill)`; a stored default is never
  overwritten by it. They are NOT plan rows, so a placement never moves them. Delete
  clears them with the other `story_*` keys. The write is best effort: a
  failure is logged and never fails the stack, and a missing default only
  disables the reset. A stored `placed` clock records the packaged `Anchor`
  instead, or on Create the deploy anchor. A loop re-plan of a `placed`
  clock with no packaged `Anchor` keeps the stored default (none stored: the
  back-fill records the deploy anchor).
- The new direct-invoke action `{"action": "reset", "expected_anchor":
  <epoch|null>}` calls `story_replan.reset`. That function re-plans onto
  `normalize_anchor(default, now)` with the default's own source, through
  the SAME `replan` (advisory lock, CAS, era guard). The report adds
  `reset: true`, `default_anchor` and `default_source`. When no default is
  stored, the action returns `no_default_anchor`, and the app maps that to
  409.
- App: `PlaceClockRequest` gains a fourth exclusive form, `reset: true` (a
  strict bool). It combines only with `expected_anchor_epoch`, `force` and
  `dry_run`. The dry run stays Lambda-free (D2). It reads the stored default
  through `story_lookup.stored_default` and reports the default, the
  normalised new anchor and the effect code `restores_default`. The lookup
  `clock` exposes `default_anchor_epoch`, `default_anchor_iso` and
  `default_source` (all `null` when absent), so the UI can show "Reset to
  default" disabled. CLI: `scripts/place_story_clock.py --reset`.

### Why not Design B (server-side auto-place)

Design B has the backend place the clock itself, for example on the first
authenticated request or at Create time. C was chosen instead for four
reasons:

- A server-side trigger turns a GET, or the sign-in, into a history rewrite.
  That breaks the rule that GET handlers are read-only and graph or history
  mutation is an explicit steward POST.
- The backend cannot tell a real attendee's first sign-in from a
  facilitator check, a health probe or the preflight script.
- A Create-time placement has no event start time to aim at. The packaged
  anchor already covers that case.
- Under C, the one placement write path (the steward POST, CAS and in-flight
  guard) stays the ONLY write path, and the policy stays a synth-time fact
  that the manifest records and the packaging gate checks.

## Amendment 2026-10-09: the geometry is a function of the loop length

The derived constants did not change; how they are computed did.
`erp_simulation.condition_monitor.geometry_for(total_seconds)` and the new
pure, stdlib-only `agents/demo/story_loop.geometry_for(loop_seconds)` compute
every step boundary for one loop length; each module's constants are
`geometry_for` of the value it reads from `STORY_LOOP_SECONDS` at import.
`story_clock` no longer reads the env or computes geometry: it binds
`story_loop.GEOMETRY`, so the place route's FastAPI imports are not needed
to load the loop math. Every module-level constant of `condition_monitor`,
`story_replan`, `story_history`, `story_clock` and `story_lookup` was
compared before and after at every valid loop length and unset: identical.

## Verification (offline)

- `python -m pytest erp_simulation/tests -q`
- `cd agentic_digital_thread && PYTHONPATH=. python -m pytest tests/unit/test_condition_monitor.py tests/unit/test_response_integrity_iteration5.py tests/unit/test_response_integrity_iteration6.py -q --disable-socket --allow-unix-socket`

For 10800, the fresh-interpreter tests check the following:

- The ADR table above.
- A congruent residue of 190.
- Every loop's alarm is on the 300 s grid, and the fail is 2410 s later.
- The repair lasts 450 s and ends inside the loop.
- The history's failed batch and its checks sit inside the window, with 18
  of 60 rejected.
- The telemetry at the history alarm reads `BPFO_Alarm` true, and one slot
  earlier false.
- Each check's noted BPFO equals the published `Spectrum_BPFO` at its time.
- There are three live check slots on the grid.
- `normalize_anchor` stays congruent.
