// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Workshop story wire types (ADR-0085/0089).
 *
 * GET  /api/admin/workshop/story        → agents/demo/story_lookup.py::lookup
 * POST /api/admin/workshop/story/clock  → agents/demo/story_clock.py::place
 *
 * Only the fields the UI reads are typed strictly; the answer key carries
 * more (failed_batch, part, maintenance, customers …) and stays `unknown`. */

import type { STORY_PHASES } from '../../features/workshop/clock';

/** A live-story phase — derived from the ONE loop-order list
 * (features/workshop/clock.STORY_PHASES = story_lookup.PHASE_ORDER). */
export type StoryPhase = (typeof STORY_PHASES)[number];

export interface PhaseSpan {
  phase: StoryPhase;
  start_at: string;
  end_at: string;
  start_offset_s: number;
  end_offset_s: number;
}

/** story_lookup.phase_timeline — every countdown is server-computed. */
export interface PhaseTimeline {
  loop_started_at: string;
  loop_ends_at: string;
  phases: PhaseSpan[];
  current: StoryPhase;
  seconds_into_phase: number;
  seconds_to_next_phase: number;
  next_phase: StoryPhase;
  next_phase_at: string;
  next_alarm_at: string;
  seconds_to_alarm: number;
  alarm_active: boolean;
  loop_fraction: number;
}

export interface StoryTelemetry {
  anchor: number | null;
  cursor_epoch: number | null;
  /** null = the minute step has not written a cursor yet. */
  backfill_complete: boolean | null;
}

export type AnchorSource = 'deploy' | 'packaged' | 'placed';

/** Design C: the packaged "start the clock on first sign-in" switch. */
export interface StoryAutoPlace {
  enabled: boolean;
  /** "Bearing alarm in N minutes" the first sign-in places. */
  minutes: number;
}

export interface StoryClock {
  phase: StoryPhase;
  anchor_source: AnchorSource | string | null;
  anchor_epoch: number;
  anchor_at: string;
  bpfo_alarm_at: string;
  telemetry: StoryTelemetry;
  phase_timeline: PhaseTimeline;
  /** The second clock (ADR-0095): the anchor the phase, the alarm and the
   * chip follow. Equal to `anchor_epoch` (one clock) unless the alarm was
   * moved without rewriting the history, when `live_anchor_source` is
   * `placed-live`. Absent on an older server ⇒ one clock. */
  live_anchor_epoch?: number;
  live_anchor_at?: string;
  live_anchor_source?: 'placed-live' | null;
  /** Optional (Design C): absent on an older server ⇒ no auto-place. */
  auto_place?: StoryAutoPlace | null;
  /** The packaged/deploy anchor a reset restores; null/absent ⇒ no reset. */
  default_anchor_epoch?: number | null;
  default_anchor_iso?: string | null;
  default_source?: 'packaged' | 'deploy' | null;
}

export interface WorkshopStory {
  story: 'live' | 'frozen' | string;
  generated_at: string;
  /** null on the frozen story (no clock exists). */
  clock: StoryClock | null;
  warnings: string[];
  [key: string]: unknown;
}

/** Preview: the relative form, resolved by the server from ONE `now`
 * (D1: `dry_run` is the ONE wire name). */
export interface PlaceClockPreviewBody {
  alarm_in_minutes: number;
  expected_anchor_epoch: number;
  dry_run: true;
  /** ADR-0095: move only the live clock; `expected_anchor_epoch` is then the LIVE anchor. */
  keep_history?: true;
}

/** Place: the ABSOLUTE alarm the preview resolved (`alarm_at_iso`), so the
 * applied alarm is exactly the previewed one — never re-resolved. */
export interface PlaceClockApplyBody {
  alarm_at: string;
  expected_anchor_epoch: number;
  dry_run: false;
  /** ADR-0095: move only the live clock; `expected_anchor_epoch` is then the LIVE anchor. */
  keep_history?: true;
}

/** Auto-place (Design C, first sign-in): the relative form applied directly
 * (`dry_run` omitted = the server default `false`). */
export interface PlaceClockAutoBody {
  alarm_in_minutes: number;
  expected_anchor_epoch: number;
  dry_run?: false;
}

/** Reset to the packaged default (Design C) — the fourth exclusive form. */
export interface ResetClockBody {
  reset: true;
  expected_anchor_epoch: number;
  dry_run: boolean;
  force?: boolean;
}

export type PlaceClockBody = PlaceClockPreviewBody | PlaceClockApplyBody | PlaceClockAutoBody | ResetClockBody;

/** The reset dry run: effect lines (`restores_default` …) plus the default
 * anchor. Every field beyond `effect` is optional so the UI degrades. */
export interface ResetClockPreview {
  dry_run: true;
  reset?: true;
  expected_anchor?: number | null;
  new_anchor?: number;
  new_anchor_iso?: string;
  alarm_at?: number;
  alarm_at_iso?: string;
  default_anchor_epoch?: number | null;
  default_anchor_iso?: string | null;
  default_source?: 'packaged' | 'deploy' | null;
  effect: string[];
  effect_codes?: string[];
  warnings?: string[];
  warning_codes?: string[];
}

export interface PlaceClockRequested {
  alarm_in_minutes: number;
  alarm_at: number;
}

export interface PlaceClockPreview {
  dry_run: true;
  /** Present (true) only on a keep-history preview (ADR-0095). */
  keep_history?: true;
  expected_anchor: number | null;
  new_anchor: number;
  new_anchor_iso: string;
  alarm_at: number;
  alarm_at_iso: string;
  loops_shifted: number;
  /** English display text, parallel to `effect_codes`. */
  effect: string[];
  /** English display text, parallel to `warning_codes`. */
  warnings: string[];
  /** Stable codes (`rewrites_history`, `resets_backfill`; keep-history:
   * `keeps_history`, `moves_live`) the UI localizes. */
  effect_codes?: string[];
  /** Stable codes (`loops_shifted`) the UI localizes. */
  warning_codes?: string[];
  requested?: PlaceClockRequested;
}

/** The story-history Lambda `place` report, passed through plus `alarm_at_iso`. */
export interface PlaceClockReport {
  alarm_at?: number;
  alarm_at_iso: string | null;
  requested?: PlaceClockRequested;
  [key: string]: unknown;
}

/** The `detail` object of a 409/422/502 from the clock route. */
export interface PlaceClockErrorDetail {
  error: 'in_flight' | 'stale_anchor' | 'invalid' | 'internal' | string;
  message: string;
}
