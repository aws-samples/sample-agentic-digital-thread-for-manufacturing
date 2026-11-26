<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0095: Two story clocks: a live anchor beside the history anchor, so the bearing alarm can move without rewriting the history

## Status

**Proposed: 2026-10-04.** Amends [ADR-0088](0088-one-story-clock-telemetry-from-the-condition-model.md)
("one story clock": the live side may now follow a second, live-only anchor)
and [ADR-0089](0089-placeable-and-packaged-story-clock.md) (a second
placement form, `keep_history`, beside the retire-and-rewrite placement, which
is unchanged).

## Context

ADR-0085 and ADR-0089 derive the WHOLE plant story from one stored anchor,
`simulation_config.story_anchor_epoch`: the three-day history (the failed run
`RUN-<date>-90001`, its lot, the three customer orders and shipments, the
deferred PM and the repair orders) is `story_history.plan(anchor)`, and the
live side (the condition-monitor loop, `VIB-CMX-<loop>`, the RUNNING line
story run and its checks, the sensor feed through the anchor mirror, the phase
chip) reads the same anchor through `story_live.read_anchor`. Because the
numbers embed dates, moving the anchor is retire-and-rewrite
(`story_replan.replan`), so a placement after module 2 invalidates the batch
number an attendee wrote down; module 1 therefore tells attendees to leave the
clock alone.

The workshop needs the opposite: an attendee who is ahead of the 80-minute
pacing wants the alarm now, not in thirty minutes; a facilitator wants to
demonstrate the alarm on their own account after showing the earlier modules;
a room on a different schedule wants everyone to reach the alarm together.
Each of these happens AFTER the history has been read, so the history must not
move. With one anchor there is nothing to keep.

## Decisions

1. **A second config key, `story_live_anchor_epoch`
   (`story_history.LIVE_ANCHOR_KEY`), is the LIVE anchor.** Absent, the live
   side follows `story_anchor_epoch` exactly as before (one clock; every
   existing deployment is unchanged). Present, every live-side reader follows
   it and nothing on the history side does. It is not a plan row:
   `story_history.plan` never writes it, `clear_story_config` deletes it.

2. **Readers split by what they time.** HISTORY anchor (`story_anchor_epoch`,
   read by `story_history.read_stored_anchor` / `story_lookup._anchor`): the
   history plan, the answer key and the maintenance window
   (`story_lookup.lookup`, `live_window`), `retire_era`, the preflight, the
   CloudFormation resource, the packaged `--story-anchor`, the full
   placement's CAS. LIVE anchor (`story_live.read_anchor`, ONE statement that
   prefers the live key): the simulator's `_load_story_clock`, hence the MES
   condition monitor, the story step, the anchor mirror (and the OPC UA server
   behind it), the phase chip and the admin Workshop tab's phase, countdown
   and timeline (`story_lookup.story_clock`, which now reports
   `live_anchor_epoch` / `live_anchor_source` beside the unchanged
   `anchor_epoch`). `story_anchor_mirror.confirm` compare-and-sets on the
   live anchor, and `read_feed_state` reports the source as `placed` while a
   live key exists, so the feed hold ends and the catch-up climbs like after a
   full placement, whatever the history's own source.

3. **One invariant: the new live loop-0 alarm falls after the history ends**
   (`story_replan.live_alarm_allowed`: `live + ALARM_OFFSET > history`). It is
   what keeps a live condition order from landing among the history's own
   orders. The live anchor may be EARLIER than the history anchor (an alarm
   brought forward); the sensor ramp then overlaps the history's healthy tail,
   which the existing OPC UA catch-up (ADR-0092 amendment) already handles:
   recorded sensor values are never rewritten, the server climbs onto the new
   curve by the alarm.

4. **`story_replan.replan_live(erp, seed, new_anchor, now=, expected_live_anchor=)`**
   is the live-only move. ERP transaction first, under the same exclusive
   story-clock advisory lock: CAS on the LIVE anchor (`StaleAnchor`), the
   three refusals of `LiveClockRefused` (no clock stored, drifted loop
   geometry, an alarm inside the history), then retire ONLY the old live
   era's story runs (`_retire_live_runs`: the 8xxxx runs of the old live
   anchor's loops whose alarm has passed, with their MES rows), write the live
   key (or delete it when the new live anchor IS the history anchor: one clock
   again), the catch-up row from the anchor the feed was following, and
   `story_telemetry_anchor`. Then the seed transaction, under the seed-side
   exclusive lock and after confirming the committed live anchor is this
   move's: purge the story asset's `VIB-CMX-*` orders that are not this live
   era's and raise the ones whose alarm has passed. Nothing of the history is
   read for retirement or written: the history rows, the history config and
   `story_failed_run_number` are untouched (one exception: amendment
   2026-10-05, item 2), and the test pins the history
   snapshot byte-identical across a move.

5. **Every full `replan` clears the live key** and catches the feed up from
   the anchor it was actually following (the live one), so the two clocks
   become one again on a full placement, a reset or a packaged re-plan.

6. **Surface.** The Lambda gains the direct action `place_live`
   (`alarm_at`, `expected_live_anchor`; a refused move answers `invalid`).
   The app route's body gains `keep_history: true` (with `alarm_at` or
   `alarm_in_minutes` only; the dry run reports the `keeps_history` /
   `moves_live` effect, the apply invokes `place_live`); the existing forms'
   payloads are byte-identical. `place_story_clock.py --keep-history`. The
   admin Workshop tab shows the keep-history card FIRST and marked
   recommended, the rewrite card after it with its existing warning, and names
   the live clock in the provenance line after a move. The attendee's plant
   clock chip is a plain link to that tab (`/admin.html#tab-workshop`), so the
   banner that shows the alarm is also the way to see what the clock is and to
   move it.

## Consequences

- An attendee or facilitator can move the alarm at any point of the workshop
  without invalidating anything written down; the module 1 warning becomes a
  pointer to the right card instead of "leave it alone".
- The old `VIB-CMX-<loop>` order and the RUNNING line story run of the old
  live era disappear on a move and reappear on the new alarm. That is the
  cost the card states; the history's repair orders and the deferred PM do not
  move.
- Two anchors can disagree by up to one loop; the chip, the countdown and the
  simulator all read the live one, the answer key the history one, so the
  attendee's "which batch" and "when is the alarm" stay consistent with what
  they see.

## Not yet verified in a deployed account

The OPC UA catch-up after a live-only move (decision 3) is the same mechanism
as after a full placement and is covered by the same mirror tests, but it has
not yet been watched on a deployed account after a `keep_history` move.

## Amendment 2026-10-05: a live move counts as a placement everywhere

Decision 2 made the feed treat a stored live key as a placement. The app and
one store path did not, so in a package with an auto-place policy:

- after a reset followed by a keep-history move, the next page load
  auto-placed over the history (the auto-place read only the history
  source, still `packaged`), and the header chip still read "(default)";
- a keep-history move back onto the history anchor deleted the live key, the
  only record of the placement, so the anchor mirror held the feed again and
  the next page load auto-placed over the history.

1. **The app reads a live move as a placement.** `effectiveAnchorSource`
   (frontend `features/workshop/clock.ts`) is `placed` while
   `live_anchor_source` is `placed-live`, else the stored `anchor_source`, the
   same rule as `story_anchor_mirror.read_feed_state`. The auto-place
   decision, its page-load guard and the chip's state and tooltip use it.
2. **A move back onto the history anchor records the placement.** When it
   deletes the live key and the history source is anything but `placed` (a
   default `deploy` or `packaged`, or an absent or unknown value, which a
   policy mirror would otherwise leave alone, so the feed would stay on the
   old live anchor), the same ERP transaction writes
   `story_anchor_source = placed`. That is the one history config row a live
   move may write; the history anchor, its rows and the failed run number
   stay untouched.
3. **A reset always leaves an unplaced clock.** A placement onto the stored
   anchor keeps the stored source (unchanged); `story_replan.reset` now writes
   the default source even then (`replan(..., restore_source=True)`), so a
   reset after item 2 does not leave `placed` behind.

## Amendment 2026-10-06: a keep-history move past the alarm says it rewinds

**Observed (a fresh workshop deployment).** A keep-history move made while the plant was
already in the alarm phase ("bearing alarm, failure in 68 min") put the live
side back into `degrading`, as designed: the new alarm lies ahead. The hour's
vibration trend then read as a drop of about 8% at the move and a
stabilisation, and the module 6 "trending up" answer honestly said *not
trending up*. Nothing was wrong; nothing had told the attendee.

**Decision.** The keep-history dry run computes the live side's phase at
`now` from its CAS anchor (the live anchor) and, when the live side is past
this loop's alarm and the requested alarm lies ahead, adds the warning
`rewinds_live_phase` (`story_clock.REWINDS_LIVE_WARNING`): the plant is
already past the alarm; moving it ahead rewinds the live sensor to the
degrading phase, so the last hour's trend reads as a drop; move before the
alarm fires, or demo the trend an hour after the new alarm. The admin
Workshop tab localises the code in all six locales
(`workshop.warningCode.rewinds_live_phase`); an older UI shows the server's
English text. A move to an alarm behind `now` and the full rewrite carry no
such note: the first does not rewind, the second already states that it moves
everything.
