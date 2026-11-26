// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Shared workshop plant-clock logic — DOM-free, imported by BOTH the admin
 * Workshop tab (pages/admin/tabs/workshop/*) and the app-shell header chip
 * (pages/app/PlantClockChip). Every clock value comes from the server's
 * phase_timeline; the only client arithmetic is subtraction (the local
 * countdown tick) — never clock geometry. Unit-tested under the node env
 * (src/test/workshop-clock.test.ts). */
import { ApiError } from '../../api/client';
import type { StoryClock, StoryPhase, WorkshopStory } from '../../api/types/workshop';

export type Translate = (key: string) => string;

/** The ONE list of live-story phases, in server loop order
 * (story_lookup.PHASE_ORDER). `StoryPhase` and every per-phase key map derive from it. */
export const STORY_PHASES = ['degrading', 'bpfo_alarm', 'repair', 'healthy'] as const;

export function isStoryPhase(phase: string): phase is StoryPhase {
  return (STORY_PHASES as readonly string[]).includes(phase);
}

/** Fill `{name}` placeholders in an i18n template. */
export function fill(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole,
  );
}

/** i18n key for a phase; an unknown phase falls back to its raw name. */
export function phaseLabelKey(phase: string): string {
  return isStoryPhase(phase) ? `workshop.phase.${phase}` : phase;
}

export function phaseLabel(phase: string, t: Translate): string {
  return isStoryPhase(phase) ? t(phaseLabelKey(phase)) : phase;
}

/** A 403 (not a steward) or 404 (workshop mode off): the clock does not exist
 * for this viewer, so every surface hides even over cached data. */
export function isHidingError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404);
}

/** 'live' when the query holds a clock (the last good body survives a
 * transient refetch failure); 'hidden' with no data, the frozen story
 * (`clock: null`), or a 403/404. */
export function clockGate(data: WorkshopStory | undefined, error?: unknown): 'live' | 'hidden' {
  if (isHidingError(error)) return 'hidden';
  return data?.clock ? 'live' : 'hidden';
}

/** true when a live clock is being shown from the last good body because the
 * latest refetch failed transiently. */
export function refreshFailed(data: WorkshopStory | undefined, error?: unknown): boolean {
  return !!error && clockGate(data, error) === 'live';
}

/** The gate as seen before the first response: 'pending' = not decided yet. */
export type WorkshopGate = 'live' | 'hidden' | 'pending';

/** Whether this account runs a workshop story, as the shared story GET
 * answers it for this viewer. */
export type WorkshopMode = 'on' | 'off' | 'unknown';

/** 'on' for any story body (either story: the frozen story's `clock: null`
 * counts, and so does a live account whose clock section failed; the last
 * good body survives a transient refetch failure), 'off' on a 403/404
 * (workshop mode off, or a viewer who cannot see the workshop), 'unknown'
 * with no body yet (in flight, or a transient failure). Unlike clockGate it
 * never reads the clock. */
export function workshopMode(data: WorkshopStory | undefined, error?: unknown): WorkshopMode {
  if (isHidingError(error)) return 'off';
  return data ? 'on' : 'unknown';
}

/** Seconds as `H:MM:SS` (negative/NaN → 0:00:00). */
export function formatCountdown(seconds: number): string {
  const total = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** A server countdown minus the whole seconds elapsed since it was fetched
 * (subtraction only; re-synced on every refetch through `fetchedAtMs`). */
export function remainingSeconds(serverSeconds: number, fetchedAtMs: number, nowMs: number): number {
  const elapsed = Math.max(0, Math.floor((nowMs - fetchedAtMs) / 1000));
  return Math.max(0, serverSeconds - elapsed);
}

/** An ISO instant as local wall-clock date + time (the raw ISO when unparseable). */
export function formatClockTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });
}

/** The anchor-source label (deploy | packaged | placed; unknown → raw). */
export function anchorSourceLabel(source: string | null | undefined, t: Translate): string {
  if (source === 'deploy' || source === 'packaged' || source === 'placed') return t(`workshop.anchorSource.${source}`);
  return source ? source : t('workshop.anchorSource.unknown');
}

/** true for an anchor nobody placed yet: the packaged or deploy default. */
export function isDefaultAnchor(source: string | null | undefined): boolean {
  return source === 'packaged' || source === 'deploy';
}

/** The source the clock is on for "has anyone placed it?": `placed` while a
 * live-only move (ADR-0095, the keep-history card) is stored, whatever the
 * history's own source, else the stored `anchor_source`. The anchor mirror
 * reads the feed's source the same way (story_anchor_mirror.read_feed_state). */
export function effectiveAnchorSource(
  clock: Pick<StoryClock, 'anchor_source' | 'live_anchor_source'>,
): string | null | undefined {
  return clock.live_anchor_source === 'placed-live' ? 'placed' : clock.anchor_source;
}
