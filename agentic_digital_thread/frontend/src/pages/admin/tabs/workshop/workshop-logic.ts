// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure Workshop-tab logic — DOM-free, unit-tested under the node env
 * (src/test/workshop-logic.test.ts). The clock helpers shared with the
 * header chip (phase list, gate, countdown, phaseLabel) live in
 * features/workshop/clock. Every clock value comes from the server's
 * phase_timeline; the only client arithmetic is proportion (bar widths),
 * subtraction (the countdown tick) and the loop length in minutes. */
import { ApiError } from '../../../../api/client';
import type {
  PhaseTimeline,
  PlaceClockErrorDetail,
  PlaceClockPreview,
  ResetClockPreview,
  StoryPhase,
} from '../../../../api/types/workshop';
import { fill, formatClockTime, phaseLabel, type Translate } from '../../../../features/workshop/clock';

export const MIN_ALARM_MINUTES = 5;
export const DEFAULT_ALARM_MINUTES = 90;
/** Only for a timeline with no phases (a malformed body): the 8 h default loop. */
export const FALLBACK_MAX_ALARM_MINUTES = 480;
/** A preview older than this may no longer describe what Place would do. */
export const PREVIEW_TTL_MS = 10 * 60_000;

/** The largest "alarm in N minutes": one loop, i.e. the last phase's end
 * offset in whole minutes (server ALARM_IN_MINUTES_RANGE = 5..loop // 60). */
export function maxAlarmMinutes(timeline: PhaseTimeline | null | undefined): number {
  const phases = timeline?.phases ?? [];
  const end = phases.length ? phases[phases.length - 1].end_offset_s : Number.NaN;
  if (!Number.isFinite(end) || end <= 0) return FALLBACK_MAX_ALARM_MINUTES;
  return Math.max(MIN_ALARM_MINUTES, Math.floor(end / 60));
}

export interface PhaseSegment {
  phase: StoryPhase;
  /** Left edge as a fraction of the loop, 0..1. */
  start: number;
  /** Width as a fraction of the loop, 0..1. */
  width: number;
  current: boolean;
}

export interface PhaseBarModel {
  segments: PhaseSegment[];
  /** The current-position marker, a fraction 0..1 of the loop. */
  marker: number;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/** Segments in the server's loop order, each sized by its offset span over
 * the loop length (the last phase's end offset: the phases tile the loop). */
export function phaseBarModel(timeline: PhaseTimeline): PhaseBarModel {
  const phases = timeline.phases ?? [];
  const loop = phases.length ? phases[phases.length - 1].end_offset_s : 0;
  const segments = phases.map((span) => ({
    phase: span.phase,
    start: loop > 0 ? clamp01(span.start_offset_s / loop) : 0,
    width: loop > 0 ? clamp01((span.end_offset_s - span.start_offset_s) / loop) : 0,
    current: span.phase === timeline.current,
  }));
  return { segments, marker: clamp01(timeline.loop_fraction) };
}

/** The phase bar's accessible name (role=img): order, current phase, position. */
export function phaseBarAriaLabel(timeline: PhaseTimeline, t: Translate): string {
  const order = (timeline.phases ?? []).map((span) => phaseLabel(span.phase, t)).join(', ');
  return fill(t('workshop.phaseBarAria'), {
    order,
    current: phaseLabel(timeline.current, t),
    percent: Math.round(clamp01(timeline.loop_fraction) * 100),
  });
}

export type MinutesCheck = { ok: true; minutes: number } | { ok: false };

/** An integer MIN_ALARM_MINUTES..max; '', fractions, NaN and out-of-range are invalid. */
export function validateMinutes(value: string | number, max: number): MinutesCheck {
  if (typeof value === 'string' && value.trim() === '') return { ok: false };
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isInteger(n) || n < MIN_ALARM_MINUTES || n > max) return { ok: false };
  return { ok: true, minutes: n };
}

/** A preview the Place button may act on: it was made for THIS N and THIS
 * anchor, at `previewedAt` (ms, for the PREVIEW_TTL_MS expiry). */
export interface PreviewRecord {
  minutes: number;
  anchorEpoch: number;
  previewedAt: number;
  preview: PlaceClockPreview;
}

/** true once a preview is PREVIEW_TTL_MS old: Place is refused until a re-preview. */
export function previewExpired(record: { previewedAt: number } | null, nowMs: number): boolean {
  return !!record && nowMs - record.previewedAt >= PREVIEW_TTL_MS;
}

export function previewMatches(
  record: PreviewRecord | null,
  minutes: MinutesCheck,
  anchorEpoch: number,
): boolean {
  return !!record && minutes.ok && record.minutes === minutes.minutes && record.anchorEpoch === anchorEpoch;
}

/** The preview's display lines: the alarm and anchor times, the loops shifted. */
export function previewSummary(
  preview: PlaceClockPreview,
  t: Translate,
  fmtTime: (iso: string) => string = formatClockTime,
): string[] {
  const lines = [
    fill(t('workshop.previewAlarmAt'), { time: fmtTime(preview.alarm_at_iso) }),
    fill(t('workshop.previewAnchor'), { time: fmtTime(preview.new_anchor_iso) }),
  ];
  if (preview.loops_shifted > 0) {
    lines.push(fill(t('workshop.previewLoopsShifted'), { n: preview.loops_shifted }));
  }
  return lines;
}

const EFFECT_CODES = ['rewrites_history', 'resets_backfill', 'restores_default', 'keeps_history', 'moves_live'] as const;
const WARNING_CODES = ['loops_shifted', 'rewinds_live_phase'] as const;

/** Localize parallel server lists: a known code → its i18n key, an unknown
 * (or absent) code → the server's English text at the same index. */
function localizedLines(
  texts: readonly string[] | undefined,
  codes: readonly string[] | undefined,
  known: readonly string[],
  prefix: string,
  t: Translate,
): string[] {
  const text = texts ?? [];
  const code = codes ?? [];
  return Array.from({ length: Math.max(text.length, code.length) }, (_, i) => {
    const c = code[i];
    if (c && known.includes(c)) return t(`${prefix}.${c}`);
    return text[i] ?? c ?? '';
  }).filter((line) => line !== '');
}

/** The effect/warning lists a placement or reset preview carries. */
type PreviewLines = Pick<PlaceClockPreview, 'effect' | 'effect_codes'> &
  Partial<Pick<PlaceClockPreview, 'warnings' | 'warning_codes'>>;

/** The preview's effect lines (`effect_codes` localized, else server text). */
export function effectLines(preview: PreviewLines, t: Translate): string[] {
  return localizedLines(preview.effect, preview.effect_codes, EFFECT_CODES, 'workshop.effectCode', t);
}

/** The preview's warning lines (`warning_codes` localized, else server text). */
export function warningLines(preview: PreviewLines, t: Translate): string[] {
  return localizedLines(preview.warnings, preview.warning_codes, WARNING_CODES, 'workshop.warningCode', t);
}

/** A reset preview the Reset button may act on: made for THIS anchor, at
 * `previewedAt` (ms, the same PREVIEW_TTL_MS expiry as a placement). */
export interface ResetPreviewRecord {
  anchorEpoch: number;
  previewedAt: number;
  preview: ResetClockPreview;
}

/** true when a default exists to reset to (`default_anchor_epoch` a number;
 * null or absent — an older server — disables the reset with a hint). */
export function resetAvailable(defaultAnchorEpoch: number | null | undefined): boolean {
  return typeof defaultAnchorEpoch === 'number' && Number.isFinite(defaultAnchorEpoch);
}

/** The i18n key saying why Reset is disabled (null = enabled, or only pending):
 * no default to reset to, an expired preview, or no preview for this anchor. */
export function resetBlockedKey(available: boolean, matches: boolean, expired: boolean): string | null {
  if (!available) return 'workshop.resetUnavailable';
  if (expired) return 'workshop.previewExpired';
  return matches ? null : 'workshop.resetNeedsPreview';
}

/** The i18n key saying why Place is disabled (null = enabled, or only pending). */
export function placeBlockedKey(matches: boolean, expired: boolean): string | null {
  if (expired) return 'workshop.previewExpired';
  return matches ? null : 'workshop.placeNeedsPreview';
}

/** The anchor a successful reset placed — the one the next GET reports —
 * which the auto-place suppression flag is keyed to: the apply report's
 * `new_anchor`, else the preview's (the SAME normalization), else the
 * stored default. */
export function resetAppliedAnchor(
  result: unknown,
  preview: ResetClockPreview | null | undefined,
  defaultAnchorEpoch: number,
): number {
  const fromResult = result && typeof result === 'object' ? (result as { new_anchor?: unknown }).new_anchor : undefined;
  if (typeof fromResult === 'number' && Number.isFinite(fromResult)) return fromResult;
  const fromPreview = preview?.new_anchor;
  if (typeof fromPreview === 'number' && Number.isFinite(fromPreview)) return fromPreview;
  return defaultAnchorEpoch;
}

/** The reset preview's display lines: the default bearing alarm and the
 * anchor the reset places — the server's `new_anchor_iso` (the default
 * normalized onto the current loop), else its `default_anchor_iso`, else
 * the GET's `default_anchor_iso`. Nothing is computed client-side. */
export function resetPreviewSummary(
  preview: ResetClockPreview,
  fallbackAnchorIso: string | null | undefined,
  t: Translate,
  fmtTime: (iso: string) => string = formatClockTime,
): string[] {
  const lines: string[] = [];
  if (preview.alarm_at_iso) lines.push(fill(t('workshop.previewAlarmAt'), { time: fmtTime(preview.alarm_at_iso) }));
  const anchorIso = preview.new_anchor_iso ?? preview.default_anchor_iso ?? fallbackAnchorIso;
  if (anchorIso) lines.push(fill(t('workshop.resetPreviewAnchor'), { time: fmtTime(anchorIso) }));
  return lines;
}

function errorDetailOf(error: ApiError): PlaceClockErrorDetail | null {
  const payload = error.payload as { detail?: unknown } | undefined;
  const detail = payload?.detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    if (typeof d.error === 'string') {
      return { error: d.error, message: typeof d.message === 'string' ? d.message : '' };
    }
  }
  return null;
}

/** A clock-route failure as display text: 409 in_flight / stale_anchor, 422,
 * 502, with the server's (already digit-masked on 502) message appended. */
export function errorMessage(error: unknown, t: Translate): string {
  if (!(error instanceof ApiError)) {
    return fill(t('workshop.errorOther'), { detail: error instanceof Error ? error.message : String(error) });
  }
  const detail = errorDetailOf(error);
  const message = detail?.message || error.detail;
  if (error.status === 409 && detail?.error === 'in_flight') return fill(t('workshop.errorInFlight'), { detail: message });
  if (error.status === 409 && detail?.error === 'stale_anchor') return fill(t('workshop.errorStaleAnchor'), { detail: message });
  if (error.status === 409) return fill(t('workshop.errorConflict'), { detail: message });
  if (error.status === 422) return fill(t('workshop.errorInvalid'), { detail: message });
  if (error.status === 502) return fill(t('workshop.errorUpstream'), { detail: message });
  return fill(t('workshop.errorOther'), { detail: message });
}

/** Shared with the header chip — lives in features/workshop/clock. */
export { anchorSourceLabel } from '../../../../features/workshop/clock';

/** The backfill line key for `telemetry.backfill_complete` (null = no cursor yet). */
export function backfillKey(complete: boolean | null | undefined): string {
  if (complete === true) return 'workshop.backfillComplete';
  if (complete === false) return 'workshop.backfillRunning';
  return 'workshop.backfillUnknown';
}
