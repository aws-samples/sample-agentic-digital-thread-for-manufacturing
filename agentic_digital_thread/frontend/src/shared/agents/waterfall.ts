// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure math + formatting helpers for the SessionDrilldown waterfall timeline.
 * No React, no DOM — every export is deterministically unit-testable.
 *
 * Data discipline: the Wave O2 backend contract is FROZEN
 * (agents/agents_trace_routes.py). Sessions-list rows carry
 * {session_id, start_ms, end_ms, last_seen_ms, span_count}; spans carry
 * {name, start_ms, duration_ms, status, tokens_in, tokens_out, kind} with
 * kind normalized to "tool" | "model" | "internal". Richer per-session
 * fields (tokens_in/tokens_out/errors, first_ms/last_ms) and the "llm" /
 * "other" kind aliases are read DEFENSIVELY so the UI upgrades in place if
 * the backend starts emitting them — and renders honestly (no bar, em-dash)
 * when they are absent. Never fabricate. */

import type { TraceSessionDetail, TraceSessionSummary, TraceSpan } from '../../api/types/agents';

/* ---- waterfall geometry ------------------------------------------------- */

export interface WaterfallWindow {
  /** Epoch/relative ms of the earliest span start. */
  min: number;
  /** Wall-clock extent in ms (>= 1 so percentage math never divides by 0). */
  range: number;
}

/** Compute the [min, max] wall-clock window across placeable spans (those
 * with a numeric start_ms). Returns null when nothing is placeable. */
export function spanWindow(spans: TraceSpan[]): WaterfallWindow | null {
  let min = Infinity;
  let max = -Infinity;
  for (const span of spans) {
    if (typeof span.start_ms !== 'number') continue;
    const start = span.start_ms;
    const end = start + (typeof span.duration_ms === 'number' ? Math.max(span.duration_ms, 0) : 0);
    if (start < min) min = start;
    if (end > max) max = end;
  }
  if (!Number.isFinite(min)) return null;
  return { min, range: Math.max(max - min, 1) };
}

/** Sub-1% spans still deserve a visible sliver. */
export const MIN_BAR_PCT = 0.75;

export interface BarGeometry {
  /** Percentage offset of the bar's left edge within the track. */
  leftPct: number;
  /** Percentage width of the bar (>= MIN_BAR_PCT, clamped to the track). */
  widthPct: number;
}

/** Proportional placement: left = (start - min) / range, width = duration /
 * range — clamped into [0, 100] and floored at MIN_BAR_PCT so short spans
 * stay visible. Spans without a numeric start_ms are honest no-shows
 * (null → the row renders its label/duration but no positioned bar). */
export function barGeometry(span: TraceSpan, window: WaterfallWindow): BarGeometry | null {
  if (typeof span.start_ms !== 'number') return null;
  const leftPct = Math.min(Math.max(((span.start_ms - window.min) / window.range) * 100, 0), 100);
  const rawWidth =
    typeof span.duration_ms === 'number' ? (Math.max(span.duration_ms, 0) / window.range) * 100 : 0;
  const widthPct = Math.min(Math.max(rawWidth, MIN_BAR_PCT), Math.max(100 - leftPct, MIN_BAR_PCT));
  return { leftPct, widthPct };
}

/** Vertical grid ticks at 25/50/75% of the window, labelled in wall-clock
 * offset from the window start. */
export function timeTicks(window: WaterfallWindow): { pct: number; label: string }[] {
  return [25, 50, 75].map((pct) => ({
    pct,
    label: fmtDuration((window.range * pct) / 100),
  }));
}

/* ---- classification ----------------------------------------------------- */

/** Map the span kind onto a waterfall color class. The deployed backend
 * emits "model" for LLM spans; "llm" is accepted as an alias per the design
 * schema. Anything unknown lands in the slate "other" bucket. */
export function kindClass(kind: string | undefined): 'sdd-kind-llm' | 'sdd-kind-tool' | 'sdd-kind-other' {
  const normalized = (kind ?? '').toLowerCase();
  if (normalized === 'llm' || normalized === 'model') return 'sdd-kind-llm';
  if (normalized === 'tool') return 'sdd-kind-tool';
  return 'sdd-kind-other';
}

/** Case-insensitive error detection — the backend normalizes OTEL
 * status.code, but fixtures/older payloads carry "ERROR"/"error". */
export function isErrorStatus(status: string | undefined): boolean {
  return /error/i.test(status ?? '');
}

/** True when the span carries any token attribution (drives the sub-bar). */
export function hasTokens(span: TraceSpan): boolean {
  return typeof span.tokens_in === 'number' || typeof span.tokens_out === 'number';
}

/** Split token usage into in/out percentages of the span's total — for the
 * thin stacked attribution bar under LLM span bars. Null when no tokens. */
export function tokenSplit(span: TraceSpan): { inPct: number; outPct: number } | null {
  if (!hasTokens(span)) return null;
  const tokensIn = typeof span.tokens_in === 'number' ? Math.max(span.tokens_in, 0) : 0;
  const tokensOut = typeof span.tokens_out === 'number' ? Math.max(span.tokens_out, 0) : 0;
  const total = tokensIn + tokensOut;
  if (total <= 0) return { inPct: 0, outPct: 0 };
  return { inPct: (tokensIn / total) * 100, outPct: (tokensOut / total) * 100 };
}

/* ---- sessions-list metadata (defensive reads) --------------------------- */

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

export interface SessionMeta {
  /** Most recent activity, preferring last_seen_ms then last_ms then end_ms. */
  lastMs: number | null;
  tokensIn: number | null;
  tokensOut: number | null;
  /** Error count when the row carries one; 0 otherwise (chip renders only >0). */
  errors: number;
}

/** Defensive projection of one sessions-list row — the frozen contract
 * guarantees only session_id; everything else may be absent. */
export function sessionMeta(session: TraceSessionSummary): SessionMeta {
  const record = session as Record<string, unknown>;
  return {
    lastMs:
      numberOrNull(record.last_seen_ms) ?? numberOrNull(record.last_ms) ?? numberOrNull(record.end_ms),
    tokensIn: numberOrNull(record.tokens_in),
    tokensOut: numberOrNull(record.tokens_out),
    errors: numberOrNull(record.errors) ?? 0,
  };
}

/** Scale for the paired in/out token mini-bars across the visible sessions
 * list — bars are comparable between rows. Returns 0 when no row has data. */
export function maxSessionTokens(sessions: TraceSessionSummary[]): number {
  let max = 0;
  for (const session of sessions) {
    const meta = sessionMeta(session);
    max = Math.max(max, meta.tokensIn ?? 0, meta.tokensOut ?? 0);
  }
  return max;
}

/* ---- summary strip ------------------------------------------------------ */

export interface SessionStats {
  wallClockMs: number | null;
  spanCount: number;
  tokensIn: number | null;
  tokensOut: number | null;
  toolCalls: number | null;
  errorCount: number;
}

/** Stats for the summary strip above the waterfall. Totals come from the
 * backend `totals` object when present (it aggregates server-side); tool
 * calls fall back to counting kind === 'tool' spans; error count is always
 * derived from span statuses. */
export function sessionStats(detail: Pick<TraceSessionDetail, 'spans' | 'totals'>): SessionStats {
  const spans = detail.spans ?? [];
  const totals = detail.totals ?? {};
  const window = spanWindow(spans);
  const toolSpanCount = spans.filter((span) => kindClass(span.kind) === 'sdd-kind-tool').length;
  return {
    wallClockMs: window ? window.range : null,
    spanCount: spans.length,
    tokensIn: numberOrNull(totals.tokens_in),
    tokensOut: numberOrNull(totals.tokens_out),
    toolCalls: numberOrNull(totals.tool_calls) ?? (spans.length ? toolSpanCount : null),
    errorCount: spans.filter((span) => isErrorStatus(span.status)).length,
  };
}

/* ---- formatting --------------------------------------------------------- */

/** 840 → "840" · 1500 → "1.5k" · 12000 → "12k" · 1_500_000 → "1.5M" · null → "—". */
export function fmtK(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '\u2014';
  const abs = Math.abs(value);
  if (abs < 1000) return String(Math.round(value));
  const scale = (scaled: number, suffix: string) => {
    const rounded = scaled >= 100 ? Math.round(scaled) : Math.round(scaled * 10) / 10;
    return `${rounded}${suffix}`;
  };
  if (abs < 1_000_000) return scale(value / 1000, 'k');
  return scale(value / 1_000_000, 'M');
}

/** "412 ms" · "5.2s" · "2m 05s" · null → "—". */
export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '\u2014';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
}

/** Compact relative time for the sessions list ("just now", "12m ago",
 * "3h ago", "2d ago"). Unknown/future timestamps are an honest em-dash. */
export function relativeTime(ms: number | null | undefined, nowMs: number = Date.now()): string {
  if (ms == null || !Number.isFinite(ms)) return '\u2014';
  // Epoch sanity guard: the sessions API contract is epoch-ms, but a
  // relative-ms value slipping through would render as "~19000d ago".
  // Anything below the epoch threshold is not a wall-clock time — dash it.
  if (ms < 1e12) return '\u2014';
  const delta = nowMs - ms;
  if (delta < 0) return '\u2014';
  if (delta < 60_000) return 'just now';
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`;
  return `${Math.floor(delta / 86_400_000)}d ago`;
}

/** Waterfall row aria-label: name + duration + status in one utterance. */
export function spanAriaLabel(span: TraceSpan): string {
  const name = span.name ?? 'span';
  const duration = fmtDuration(span.duration_ms);
  const status = span.status ?? 'unset';
  return `${name}, ${duration}, ${status}`;
}
