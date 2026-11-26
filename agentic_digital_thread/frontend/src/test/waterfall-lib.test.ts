// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests for the pure waterfall geometry/format helpers backing
 * <SessionDrilldown/>. Fixtures follow the FROZEN Wave O2 span schema
 * (agents/agents_trace_routes.py): spans carry {name, start_ms, duration_ms,
 * status, tokens_in, tokens_out, kind}; the deployed backend emits kind
 * "tool"|"model"|"internal" ("llm" accepted as a design-schema alias). */
import { describe, expect, it } from 'vitest';

import type { TraceSpan } from '../api/types/agents';
import {
  barGeometry,
  fmtDuration,
  fmtK,
  isErrorStatus,
  kindClass,
  maxSessionTokens,
  MIN_BAR_PCT,
  relativeTime,
  sessionMeta,
  sessionStats,
  spanAriaLabel,
  spanWindow,
  timeTicks,
  tokenSplit,
} from '../shared/agents/waterfall';

const T0 = 1_760_000_000_000;

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return { name: 's', start_ms: T0, duration_ms: 100, status: 'ok', kind: 'internal', ...overrides };
}

describe('spanWindow', () => {
  it('spans the earliest start to the latest end (start + duration)', () => {
    const window = spanWindow([
      span({ start_ms: T0, duration_ms: 5000 }), // ends latest at T0+5000
      span({ start_ms: T0 + 1000, duration_ms: 640 }),
      span({ start_ms: T0 + 4000, duration_ms: 800 }),
    ]);
    expect(window).toEqual({ min: T0, range: 5000 });
  });

  it('ignores spans without a numeric start and never divides by zero', () => {
    expect(spanWindow([span({ start_ms: null })])).toBeNull();
    // Single zero-duration span → range floors at 1.
    expect(spanWindow([span({ duration_ms: 0 })])).toEqual({ min: T0, range: 1 });
  });
});

describe('barGeometry — proportional left/width', () => {
  const window = { min: T0, range: 10_000 };

  it('positions left by (start − min) / range and width by duration / range', () => {
    const geometry = barGeometry(span({ start_ms: T0 + 2500, duration_ms: 5000 }), window)!;
    expect(geometry.leftPct).toBeCloseTo(25);
    expect(geometry.widthPct).toBeCloseTo(50);
  });

  it('floors tiny spans at the minimum visible sliver', () => {
    const geometry = barGeometry(span({ start_ms: T0, duration_ms: 1 }), window)!;
    expect(geometry.widthPct).toBe(MIN_BAR_PCT);
  });

  it('clamps overshooting spans into the track', () => {
    const geometry = barGeometry(span({ start_ms: T0 + 9000, duration_ms: 50_000 }), window)!;
    expect(geometry.leftPct).toBeCloseTo(90);
    expect(geometry.widthPct).toBeCloseTo(10);
  });

  it('returns null for spans without a start (honest no-show, no fabricated bar)', () => {
    expect(barGeometry(span({ start_ms: null }), window)).toBeNull();
  });
});

describe('timeTicks', () => {
  it('emits 25/50/75% ticks labelled with wall-clock offsets', () => {
    expect(timeTicks({ min: T0, range: 10_000 })).toEqual([
      { pct: 25, label: '2.5s' },
      { pct: 50, label: '5.0s' },
      { pct: 75, label: '7.5s' },
    ]);
  });
});

describe('kindClass — kind → color class mapping', () => {
  it('maps llm AND the deployed backend alias "model" to the cyan class', () => {
    expect(kindClass('llm')).toBe('sdd-kind-llm');
    expect(kindClass('model')).toBe('sdd-kind-llm');
    expect(kindClass('LLM')).toBe('sdd-kind-llm');
  });
  it('maps tool to the violet class', () => {
    expect(kindClass('tool')).toBe('sdd-kind-tool');
  });
  it('maps internal/unknown/absent to the slate other class', () => {
    expect(kindClass('internal')).toBe('sdd-kind-other');
    expect(kindClass('other')).toBe('sdd-kind-other');
    expect(kindClass(undefined)).toBe('sdd-kind-other');
  });
});

describe('isErrorStatus', () => {
  it('detects error case-insensitively and treats ok/unset/absent as non-error', () => {
    expect(isErrorStatus('error')).toBe(true);
    expect(isErrorStatus('ERROR')).toBe(true);
    expect(isErrorStatus('ok')).toBe(false);
    expect(isErrorStatus('unset')).toBe(false);
    expect(isErrorStatus(undefined)).toBe(false);
  });
});

describe('tokenSplit', () => {
  it('splits in/out percentages of the span total', () => {
    expect(tokenSplit(span({ tokens_in: 1200, tokens_out: 400 }))).toEqual({
      inPct: 75,
      outPct: 25,
    });
  });
  it('is null without token data — no fabricated sub-bar', () => {
    expect(tokenSplit(span({ tokens_in: null, tokens_out: null }))).toBeNull();
    expect(tokenSplit(span())).toBeNull();
  });
});

describe('sessionMeta / maxSessionTokens — defensive sessions-list reads', () => {
  it('reads tokens, errors, and recency from whichever fields the row carries', () => {
    const meta = sessionMeta({
      session_id: 's1',
      tokens_in: 1500,
      tokens_out: 300,
      errors: 2,
      last_ms: T0,
    });
    expect(meta).toEqual({ tokensIn: 1500, tokensOut: 300, errors: 2, lastMs: T0 });
  });

  it('prefers last_seen_ms (the frozen backend field) for recency', () => {
    expect(sessionMeta({ session_id: 's1', last_seen_ms: T0, last_ms: T0 - 5 }).lastMs).toBe(T0);
    expect(sessionMeta({ session_id: 's1', end_ms: T0 }).lastMs).toBe(T0);
  });

  it('degrades honestly when the frozen contract fields are all that exist', () => {
    expect(sessionMeta({ session_id: 's1', span_count: 4 })).toEqual({
      tokensIn: null,
      tokensOut: null,
      errors: 0,
      lastMs: null,
    });
  });

  it('scales the token mini-bars across all sessions', () => {
    expect(
      maxSessionTokens([
        { session_id: 'a', tokens_in: 100, tokens_out: 900 },
        { session_id: 'b', tokens_in: 2000 },
        { session_id: 'c' },
      ]),
    ).toBe(2000);
    expect(maxSessionTokens([{ session_id: 'c' }])).toBe(0);
  });
});

describe('sessionStats — summary strip', () => {
  it('computes wall clock, span/tool/error counts, and prefers backend totals', () => {
    const stats = sessionStats({
      spans: [
        span({ start_ms: T0, duration_ms: 5000, kind: 'model', status: 'ok' }),
        span({ start_ms: T0 + 1000, duration_ms: 640, kind: 'tool', status: 'ok' }),
        span({ start_ms: T0 + 2000, duration_ms: 1400, kind: 'tool', status: 'error' }),
      ],
      totals: { tokens_in: 1500, tokens_out: 1430, tool_calls: 2 },
    });
    expect(stats).toEqual({
      wallClockMs: 5000,
      spanCount: 3,
      tokensIn: 1500,
      tokensOut: 1430,
      toolCalls: 2,
      errorCount: 1,
    });
  });

  it('falls back to counting tool spans when totals are absent, and stays honest on empty', () => {
    expect(sessionStats({ spans: [span({ kind: 'tool' })], totals: {} }).toolCalls).toBe(1);
    expect(sessionStats({ spans: [], totals: {} })).toEqual({
      wallClockMs: null,
      spanCount: 0,
      tokensIn: null,
      tokensOut: null,
      toolCalls: null,
      errorCount: 0,
    });
  });
});

describe('formatting', () => {
  it('fmtK formats thousands compactly and em-dashes the unknown', () => {
    expect(fmtK(840)).toBe('840');
    expect(fmtK(1500)).toBe('1.5k');
    expect(fmtK(1430)).toBe('1.4k');
    expect(fmtK(12_000)).toBe('12k');
    expect(fmtK(125_000)).toBe('125k');
    expect(fmtK(1_500_000)).toBe('1.5M');
    expect(fmtK(null)).toBe('\u2014');
    expect(fmtK(undefined)).toBe('\u2014');
  });

  it('fmtDuration covers ms / seconds / minutes and em-dashes the unknown', () => {
    expect(fmtDuration(412)).toBe('412 ms');
    expect(fmtDuration(5230)).toBe('5.2s');
    expect(fmtDuration(125_000)).toBe('2m 05s');
    expect(fmtDuration(null)).toBe('\u2014');
  });

  it('relativeTime renders compact recency and em-dashes unknown/future', () => {
    const now = T0;
    expect(relativeTime(now - 10_000, now)).toBe('just now');
    expect(relativeTime(now - 12 * 60_000, now)).toBe('12m ago');
    expect(relativeTime(now - 3 * 3_600_000, now)).toBe('3h ago');
    expect(relativeTime(now - 2 * 86_400_000, now)).toBe('2d ago');
    expect(relativeTime(null, now)).toBe('\u2014');
    expect(relativeTime(now + 5000, now)).toBe('\u2014');
  });

  it('relativeTime em-dashes sub-epoch values instead of fabricating "~19000d ago"', () => {
    const now = T0;
    // Epoch sanity guard: relative-ms offsets (small values) are NOT
    // wall-clock times — dash them rather than render a huge bogus recency.
    expect(relativeTime(5230, now)).toBe('\u2014');
    expect(relativeTime(0, now)).toBe('\u2014');
    expect(relativeTime(1e12 - 1, now)).toBe('\u2014');
    // Genuine epoch values just above the threshold still format.
    expect(relativeTime(now, now)).toBe('just now');
  });

  it('spanAriaLabel carries name + duration + status', () => {
    expect(spanAriaLabel(span({ name: 'query_erp', duration_ms: 640, status: 'ok' }))).toBe(
      'query_erp, 640 ms, ok',
    );
    expect(spanAriaLabel({})).toBe('span, \u2014, unset');
  });
});
