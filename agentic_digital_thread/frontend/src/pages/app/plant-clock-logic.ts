// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure header plant-clock chip logic (ADR-0091) — only what the shared
 * features/workshop/clock does not already provide (the phase list, fill,
 * clockGate, remainingSeconds, formatClockTime and phaseLabel are REUSED from
 * there). Every number is a server countdown from phase_timeline; the client
 * only subtracts elapsed seconds and rounds to whole minutes — never
 * clock-geometry math. */
import type { PhaseTimeline, StoryClock } from '../../api/types/workshop';
import type { AutoPlaceStatus } from '../../features/workshop/auto-place';
import {
  anchorSourceLabel,
  effectiveAnchorSource,
  fill,
  formatClockTime,
  isDefaultAnchor,
  isStoryPhase,
  phaseLabel,
  type Translate,
} from '../../features/workshop/clock';

/** The server countdown the chip shows for the current phase: the alarm
 * while degrading, otherwise the next phase change (failure, healthy,
 * degrading). */
export function chipServerSeconds(timeline: PhaseTimeline): number {
  return timeline.current === 'degrading' ? timeline.seconds_to_alarm : timeline.seconds_to_next_phase;
}

/** Whole minutes, rounded UP so "alarm in 1 min" stays until the alarm fires
 * (0 only when nothing is left; negative/NaN → 0). */
export function chipMinutes(seconds: number): number {
  if (!Number.isFinite(seconds) || seconds <= 0) return 0;
  return Math.ceil(seconds / 60);
}

/** The chip text; at 0 remaining it says "now" instead of "in 0 min". An
 * unknown phase falls back to its raw label with no countdown. */
export function chipText(phase: string, remaining: number, t: Translate, isDefault = false): string {
  if (!isStoryPhase(phase)) return phaseLabel(phase, t);
  const minutes = chipMinutes(remaining);
  const prefix = isDefault ? 'plantClock.chip.default' : 'plantClock.chip';
  if (minutes === 0) return t(`${prefix}.now.${phase}`);
  return fill(t(`${prefix}.${phase}`), { minutes });
}

/** The chip's provenance state (Design C):
 * - placed   — someone placed the clock (the server says so, or a live-only
 *              move is stored: `effectiveAnchorSource`; always wins);
 * - starting — this page's auto-place POST is in flight;
 * - failed   — this page's auto-place failed (not a 409);
 * - default  — the packaged/deploy anchor (auto-place off, pending, or 409);
 * - unknown  — any other/absent source: the plain phase text, no claim. */
export type ChipState = 'placed' | 'default' | 'starting' | 'failed' | 'unknown';

export function chipState(
  clock: Pick<StoryClock, 'anchor_source' | 'live_anchor_source'>,
  autoPlace: AutoPlaceStatus,
): ChipState {
  const source = effectiveAnchorSource(clock);
  if (source === 'placed') return 'placed';
  if (autoPlace === 'starting') return 'starting';
  if (autoPlace === 'failed') return 'failed';
  return isDefaultAnchor(source) ? 'default' : 'unknown';
}

/** The chip tooltip with the anchor source: "Next bearing alarm: … · Clock
 * source: packaged default". */
export function chipSourceTitle(
  timeline: PhaseTimeline,
  source: string | null | undefined,
  t: Translate,
  fmtTime: (iso: string) => string = formatClockTime,
): string {
  return fill(t('plantClock.chip.titleWithSource'), {
    title: chipTitle(timeline, t, fmtTime),
    source: anchorSourceLabel(source, t),
  });
}

/** The chip tooltip: the next bearing alarm as local wall-clock time. */
export function chipTitle(
  timeline: PhaseTimeline,
  t: Translate,
  fmtTime: (iso: string) => string = formatClockTime,
): string {
  return fill(t('plantClock.chip.title'), { time: fmtTime(timeline.next_alarm_at) });
}

/** The chip's screen-reader announcement: the phase only, so a polite live
 * region speaks on a phase change and never on the minute tick. */
export function chipAnnouncement(phase: string, t: Translate): string {
  return fill(t('plantClock.chip.phaseAnnounce'), { phase: phaseLabel(phase, t) });
}
