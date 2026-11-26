// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — pure Workshop-tab logic (ADR-0091) and the shared plant-clock
 * module (features/workshop/clock): the clock gate (hides only on no data or
 * 403/404; keeps the last good body on a transient error), the ONE phase
 * list, phase-bar proportions, the loop-derived minutes bound, preview
 * matching and expiry, the effect/warning code mapping, countdown
 * subtraction and the clock-route error mapping (409 in_flight /
 * stale_anchor, 422, 502). */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import type { PhaseTimeline, PlaceClockPreview, WorkshopStory } from '../api/types/workshop';
import {
  clockGate,
  fill,
  formatCountdown,
  isHidingError,
  isStoryPhase,
  phaseLabel,
  phaseLabelKey,
  refreshFailed,
  remainingSeconds,
  STORY_PHASES,
  workshopMode,
} from '../features/workshop/clock';
import { workshopStoryQueryOptions, workshopStoryRetry } from '../features/workshop/queries';
import {
  anchorSourceLabel,
  backfillKey,
  effectLines,
  errorMessage,
  FALLBACK_MAX_ALARM_MINUTES,
  maxAlarmMinutes,
  phaseBarAriaLabel,
  phaseBarModel,
  previewExpired,
  previewMatches,
  PREVIEW_TTL_MS,
  previewSummary,
  validateMinutes,
  warningLines,
} from '../pages/admin/tabs/workshop/workshop-logic';

const t = (key: string) => key;

/** The shipped I18N literal, evaluated (the i18n-chrome.test.tsx helper). */
const I18N_LANGS = ['en', 'ja', 'de', 'fr', 'ko', 'zh'] as const;
const DICTS = (() => {
  const src = readFileSync(path.resolve(process.cwd(), 'src/lib/i18n.ts'), 'utf-8');
  const decl = 'const I18N: Record<string, Record<string, string>> = {';
  const start = src.indexOf(decl);
  const end = src.indexOf('\n};', start) + 3;
  const objText = src.slice(src.indexOf('{', start + decl.length - 1), end - 1);
  return new Function(`return ${objText}`)() as Record<string, Record<string, string>>;
})();
const translate = (lang: string, key: string): string => DICTS[lang]?.[key] ?? key;

/** The 8 h walk-2 geometry: alarm step 3898, fail 5184, repair 240 steps (5 s). */
const TIMELINE: PhaseTimeline = {
  loop_started_at: '2026-09-30T02:30:10+00:00',
  loop_ends_at: '2026-09-30T10:30:10+00:00',
  phases: [
    { phase: 'degrading', start_at: 'a', end_at: 'b', start_offset_s: 0, end_offset_s: 19490 },
    { phase: 'bpfo_alarm', start_at: 'b', end_at: 'c', start_offset_s: 19490, end_offset_s: 25920 },
    { phase: 'repair', start_at: 'c', end_at: 'd', start_offset_s: 25920, end_offset_s: 27120 },
    { phase: 'healthy', start_at: 'd', end_at: 'e', start_offset_s: 27120, end_offset_s: 28800 },
  ],
  current: 'degrading',
  seconds_into_phase: 18013,
  seconds_to_next_phase: 1477,
  next_phase: 'bpfo_alarm',
  next_phase_at: '2026-09-30T08:35:00+00:00',
  next_alarm_at: '2026-09-30T08:35:00+00:00',
  seconds_to_alarm: 1477,
  alarm_active: false,
  loop_fraction: 0.625451,
};

const LIVE: WorkshopStory = {
  story: 'live',
  generated_at: '2026-09-30T08:10:23+00:00',
  clock: {
    phase: 'degrading',
    anchor_source: 'packaged',
    anchor_epoch: 1790737810,
    anchor_at: '2026-09-30T02:30:10+00:00',
    bpfo_alarm_at: '2026-09-30T08:35:00+00:00',
    telemetry: { anchor: 1790737810, cursor_epoch: 1790756700, backfill_complete: true },
    phase_timeline: TIMELINE,
  },
  warnings: [],
};

describe('clockGate', () => {
  it('is live whenever the query holds a clock', () => {
    expect(clockGate(LIVE)).toBe('live');
    expect(refreshFailed(LIVE)).toBe(false);
  });
  it('hides the frozen story (clock: null), no data, and a 403/404', () => {
    expect(clockGate({ ...LIVE, story: 'frozen', clock: null })).toBe('hidden');
    expect(clockGate(undefined)).toBe('hidden');
    expect(clockGate(undefined, new ApiError(404, '/x', 'Workshop mode is not enabled'))).toBe('hidden');
    expect(clockGate(undefined, new ApiError(403, '/x', 'forbidden'))).toBe('hidden');
    expect(clockGate(undefined, new ApiError(500, '/x', 'boom'))).toBe('hidden');
    // A 403/404 hides even over a cached live body: the clock is gone for this viewer.
    expect(clockGate(LIVE, new ApiError(404, '/x', 'nf'))).toBe('hidden');
    expect(clockGate(LIVE, new ApiError(403, '/x', 'no'))).toBe('hidden');
  });
  it('keeps the last good clock on a transient error and reports the failed refresh', () => {
    for (const error of [new ApiError(500, '/x', 'boom'), new ApiError(502, '/x', 'bad'), new Error('network')]) {
      expect(clockGate(LIVE, error)).toBe('live');
      expect(refreshFailed(LIVE, error)).toBe(true);
    }
    expect(refreshFailed(undefined, new Error('network'))).toBe(false);
    expect(refreshFailed(LIVE, new ApiError(404, '/x', 'nf'))).toBe(false);
    expect(isHidingError(new ApiError(404, '/x', 'nf'))).toBe(true);
    expect(isHidingError(new ApiError(409, '/x', 'c'))).toBe(false);
    expect(isHidingError(new Error('x'))).toBe(false);
  });
});

describe('workshopMode (the Onboard Reset All gate)', () => {
  it('is on for any story body, whatever its clock', () => {
    expect(workshopMode(LIVE)).toBe('on');
    expect(workshopMode({ ...LIVE, story: 'frozen', clock: null })).toBe('on');
    // A live account whose clock section failed is still a workshop.
    expect(workshopMode({ ...LIVE, clock: null, warnings: ['clock: config miss'] })).toBe('on');
  });
  it('is off on a 403/404, even over a cached body', () => {
    expect(workshopMode(undefined, new ApiError(404, '/x', 'Workshop mode is not enabled'))).toBe('off');
    expect(workshopMode(undefined, new ApiError(403, '/x', 'forbidden'))).toBe('off');
    expect(workshopMode(LIVE, new ApiError(404, '/x', 'nf'))).toBe('off');
  });
  it('is unknown with no body yet, and keeps the last body on a transient error', () => {
    expect(workshopMode(undefined)).toBe('unknown');
    for (const error of [new ApiError(500, '/x', 'boom'), new ApiError(502, '/x', 'bad'), new Error('network')]) {
      expect(workshopMode(undefined, error)).toBe('unknown');
      expect(workshopMode(LIVE, error)).toBe('on');
    }
  });
});

describe('STORY_PHASES (the ONE phase list)', () => {
  it('is the server loop order and drives every phase key', () => {
    expect(STORY_PHASES).toEqual(['degrading', 'bpfo_alarm', 'repair', 'healthy']);
    expect(STORY_PHASES.map(phaseLabelKey)).toEqual([
      'workshop.phase.degrading',
      'workshop.phase.bpfo_alarm',
      'workshop.phase.repair',
      'workshop.phase.healthy',
    ]);
    expect(isStoryPhase('repair')).toBe(true);
    expect(isStoryPhase('mystery')).toBe(false);
    expect(phaseLabelKey('mystery')).toBe('mystery');
  });
  it('has a phase label and a chip text in every locale', () => {
    for (const lang of I18N_LANGS) {
      for (const phase of STORY_PHASES) {
        for (const key of [`workshop.phase.${phase}`, `plantClock.chip.${phase}`, `plantClock.chip.now.${phase}`]) {
          expect(translate(lang, key), `${lang} ${key}`).not.toBe(key);
        }
      }
    }
  });
});

describe('workshop story query policy (D7)', () => {
  it('shares one key and polls every 60 s', () => {
    const options = workshopStoryQueryOptions(true);
    expect(options.queryKey).toEqual(['admin', 'workshop', 'story']);
    expect(options.refetchInterval).toBe(60_000);
    expect(workshopStoryQueryOptions(false).enabled).toBe(false);
  });
  it('never retries a 403/404 and retries a transient failure twice', () => {
    expect(workshopStoryRetry(0, new ApiError(404, '/x', 'nf'))).toBe(false);
    expect(workshopStoryRetry(0, new ApiError(403, '/x', 'no'))).toBe(false);
    expect(workshopStoryRetry(0, new ApiError(502, '/x', 'bad'))).toBe(true);
    expect(workshopStoryRetry(1, new Error('network'))).toBe(true);
    expect(workshopStoryRetry(2, new Error('network'))).toBe(false);
  });
});

describe('phaseBarModel (phase-bar math)', () => {
  it('sizes the four segments by their offset spans over the loop, in loop order', () => {
    const model = phaseBarModel(TIMELINE);
    expect(model.segments.map((s) => s.phase)).toEqual(['degrading', 'bpfo_alarm', 'repair', 'healthy']);
    const [deg, alarm, repair, healthy] = model.segments;
    expect(deg.start).toBe(0);
    expect(deg.width).toBeCloseTo(19490 / 28800, 10);
    expect(alarm.start).toBeCloseTo(19490 / 28800, 10);
    expect(alarm.width).toBeCloseTo(6430 / 28800, 10);
    expect(repair.width).toBeCloseTo(1200 / 28800, 10);
    expect(healthy.start + healthy.width).toBeCloseTo(1, 10);
  });
  it('tiles with no gaps and widths summing to 1', () => {
    const { segments } = phaseBarModel(TIMELINE);
    for (let i = 1; i < segments.length; i++) {
      expect(segments[i].start).toBeCloseTo(segments[i - 1].start + segments[i - 1].width, 10);
    }
    expect(segments.reduce((sum, s) => sum + s.width, 0)).toBeCloseTo(1, 10);
  });
  it('marks only the current phase and puts the marker at loop_fraction', () => {
    const model = phaseBarModel({ ...TIMELINE, current: 'repair', loop_fraction: 0.95 });
    expect(model.segments.filter((s) => s.current).map((s) => s.phase)).toEqual(['repair']);
    expect(model.marker).toBe(0.95);
  });
  it('clamps the marker and survives an empty or zero-length timeline', () => {
    expect(phaseBarModel({ ...TIMELINE, loop_fraction: 1.7 }).marker).toBe(1);
    expect(phaseBarModel({ ...TIMELINE, loop_fraction: Number.NaN }).marker).toBe(0);
    expect(phaseBarModel({ ...TIMELINE, phases: [] }).segments).toEqual([]);
  });
  it('carries a text accessible name (order, current phase, percent)', () => {
    const label = phaseBarAriaLabel(TIMELINE, (k) =>
      k === 'workshop.phaseBarAria' ? '{order} | {current} | {percent}' : k,
    );
    expect(label).toBe(
      'workshop.phase.degrading, workshop.phase.bpfo_alarm, workshop.phase.repair, workshop.phase.healthy | workshop.phase.degrading | 63',
    );
  });
  it('labels an unknown phase with its raw name', () => {
    expect(phaseLabel('bpfo_alarm', t)).toBe('workshop.phase.bpfo_alarm');
    expect(phaseLabel('mystery', t)).toBe('mystery');
  });
});

describe('maxAlarmMinutes (one loop, from phase_timeline)', () => {
  it('is the last phase end offset in whole minutes', () => {
    expect(maxAlarmMinutes(TIMELINE)).toBe(480);
    const threeHours = { ...TIMELINE, phases: TIMELINE.phases.map((p, i) => (i === 3 ? { ...p, end_offset_s: 10800 } : p)) };
    expect(maxAlarmMinutes(threeHours)).toBe(180);
    const odd = { ...TIMELINE, phases: TIMELINE.phases.map((p, i) => (i === 3 ? { ...p, end_offset_s: 10859 } : p)) };
    expect(maxAlarmMinutes(odd)).toBe(180);
  });
  it('falls back only for a malformed timeline with no phases', () => {
    expect(maxAlarmMinutes({ ...TIMELINE, phases: [] })).toBe(FALLBACK_MAX_ALARM_MINUTES);
    expect(maxAlarmMinutes(undefined)).toBe(FALLBACK_MAX_ALARM_MINUTES);
  });
});

describe('validateMinutes', () => {
  it('accepts integers 5..max, any step (22 is valid)', () => {
    expect(validateMinutes('5', 480)).toEqual({ ok: true, minutes: 5 });
    expect(validateMinutes('22', 480)).toEqual({ ok: true, minutes: 22 });
    expect(validateMinutes('90', 480)).toEqual({ ok: true, minutes: 90 });
    expect(validateMinutes(480, 480)).toEqual({ ok: true, minutes: 480 });
    expect(validateMinutes(180, 180)).toEqual({ ok: true, minutes: 180 });
  });
  it('rejects 4, max + 1, 30.5, empty and non-numbers', () => {
    for (const bad of ['4', '481', '30.5', '', '  ', 'abc', 4, 481, 30.5, Number.NaN]) {
      expect(validateMinutes(bad, 480).ok).toBe(false);
    }
    expect(validateMinutes(181, 180).ok).toBe(false);
  });
  it('fills the loop-derived bound into the hint strings in every locale', () => {
    for (const lang of I18N_LANGS) {
      for (const key of ['workshop.minutesHint', 'workshop.minutesInvalid']) {
        const text = fill(translate(lang, key), { min: 5, max: 180 });
        expect(text, `${lang} ${key}`).toContain('180');
        expect(text, `${lang} ${key}`).toContain('5');
        expect(text, `${lang} ${key}`).not.toMatch(/\{|480/);
      }
    }
  });
});

const PREVIEW: PlaceClockPreview = {
  dry_run: true,
  expected_anchor: 1790737810,
  new_anchor: 1790737810,
  new_anchor_iso: '2026-09-30T02:30:10+00:00',
  alarm_at: 1790757300,
  alarm_at_iso: '2026-09-30T08:35:00+00:00',
  loops_shifted: 0,
  effect: ['rewrites the three-day history and every run number', 'moves the sensor feed onto the new clock by the next alarm (recorded sensor history stays)'],
  warnings: [],
};

describe('previewMatches / previewExpired / previewSummary', () => {
  const record = { minutes: 90, anchorEpoch: 1790737810, previewedAt: 1_000_000, preview: PREVIEW };
  it('matches only the same N and the same anchor', () => {
    expect(previewMatches(record, { ok: true, minutes: 90 }, 1790737810)).toBe(true);
    expect(previewMatches(record, { ok: true, minutes: 95 }, 1790737810)).toBe(false);
    expect(previewMatches(record, { ok: true, minutes: 90 }, 1790737815)).toBe(false);
    expect(previewMatches(record, { ok: false }, 1790737810)).toBe(false);
    expect(previewMatches(null, { ok: true, minutes: 90 }, 1790737810)).toBe(false);
  });
  it('expires a preview after 10 minutes', () => {
    expect(PREVIEW_TTL_MS).toBe(600_000);
    expect(previewExpired(record, 1_000_000)).toBe(false);
    expect(previewExpired(record, 1_000_000 + PREVIEW_TTL_MS - 1)).toBe(false);
    expect(previewExpired(record, 1_000_000 + PREVIEW_TTL_MS)).toBe(true);
    expect(previewExpired(null, 9e12)).toBe(false);
  });
  it('lists the alarm and anchor times, and loops shifted only when > 0', () => {
    const fmt = (iso: string) => `T(${iso})`;
    const tt = (k: string) => `${k}:{time}{n}`;
    expect(previewSummary(PREVIEW, tt, fmt)).toEqual([
      'workshop.previewAlarmAt:T(2026-09-30T08:35:00+00:00){n}',
      'workshop.previewAnchor:T(2026-09-30T02:30:10+00:00){n}',
    ]);
    expect(previewSummary({ ...PREVIEW, loops_shifted: 1 }, tt, fmt)).toHaveLength(3);
  });
});

describe('effectLines / warningLines (server codes → i18n)', () => {
  const coded: PlaceClockPreview = {
    ...PREVIEW,
    loops_shifted: 1,
    effect_codes: ['rewrites_history', 'resets_backfill'],
    warnings: ['an earlier alarm already exists'],
    warning_codes: ['loops_shifted'],
  };
  it('maps known codes to i18n keys', () => {
    expect(effectLines(coded, t)).toEqual(['workshop.effectCode.rewrites_history', 'workshop.effectCode.resets_backfill']);
    expect(warningLines(coded, t)).toEqual(['workshop.warningCode.loops_shifted']);
  });
  it('localizes the keep-history rewind warning (event 28: a move past the alarm)', () => {
    const rewound = { ...coded, warnings: ['x', 'the plant is already past'], warning_codes: ['loops_shifted', 'rewinds_live_phase'] };
    expect(warningLines(rewound, t)).toEqual(['workshop.warningCode.loops_shifted', 'workshop.warningCode.rewinds_live_phase']);
  });
  it('falls back to the server text for an unknown or absent code', () => {
    expect(effectLines({ ...coded, effect_codes: ['rewrites_history', 'brand_new'] }, t)).toEqual([
      'workshop.effectCode.rewrites_history',
      'moves the sensor feed onto the new clock by the next alarm (recorded sensor history stays)',
    ]);
    expect(effectLines(PREVIEW, t)).toEqual(PREVIEW.effect);
    expect(warningLines({ ...coded, warning_codes: undefined }, t)).toEqual(['an earlier alarm already exists']);
    // A code with no text and no mapping still shows (as the raw code).
    expect(warningLines({ ...PREVIEW, warnings: [], warning_codes: ['mystery'] }, t)).toEqual(['mystery']);
  });
  it('has every code translated in all six locales', () => {
    for (const lang of I18N_LANGS) {
      for (const key of [
        'workshop.effectCode.rewrites_history',
        'workshop.effectCode.resets_backfill',
        'workshop.warningCode.loops_shifted',
        'workshop.previewExpired',
        'workshop.refreshFailed',
        'plantClock.chip.phaseAnnounce',
      ]) {
        expect(translate(lang, key), `${lang} ${key}`).not.toBe(key);
      }
    }
  });
});

describe('countdown', () => {
  it('formats H:MM:SS and never goes negative', () => {
    expect(formatCountdown(1477)).toBe('0:24:37');
    expect(formatCountdown(3600 * 5 + 61)).toBe('5:01:01');
    expect(formatCountdown(-5)).toBe('0:00:00');
    expect(formatCountdown(Number.NaN)).toBe('0:00:00');
  });
  it('subtracts whole elapsed seconds since the fetch, floored at 0', () => {
    expect(remainingSeconds(1477, 1_000, 1_000)).toBe(1477);
    expect(remainingSeconds(1477, 1_000, 11_999)).toBe(1467);
    expect(remainingSeconds(10, 0, 60_000)).toBe(0);
    expect(remainingSeconds(10, 5_000, 0)).toBe(10);
  });
});

describe('errorMessage (clock route failures)', () => {
  const err = (status: number, detail: unknown) =>
    new ApiError(status, '/api/admin/workshop/story/clock', typeof detail === 'string' ? detail : 'x', { detail });
  it('maps 409 in_flight and stale_anchor to distinct messages with the server text', () => {
    const tt = (k: string) => `${k}|{detail}`;
    expect(errorMessage(err(409, { error: 'in_flight', message: 'already in flight' }), tt)).toBe(
      'workshop.errorInFlight|already in flight',
    );
    expect(errorMessage(err(409, { error: 'stale_anchor', message: 'anchor moved' }), tt)).toBe(
      'workshop.errorStaleAnchor|anchor moved',
    );
  });
  it('maps a string-detail 409 (not the live story), 422, 502 and others', () => {
    const tt = (k: string) => `${k}|{detail}`;
    expect(errorMessage(err(409, 'the story clock exists only in the live story'), tt)).toBe(
      'workshop.errorConflict|the story clock exists only in the live story',
    );
    expect(errorMessage(err(422, { error: 'invalid', message: 'bad alarm' }), tt)).toBe('workshop.errorInvalid|bad alarm');
    expect(errorMessage(err(502, { error: 'internal', message: 'invoke failed (N)' }), tt)).toBe(
      'workshop.errorUpstream|invoke failed (N)',
    );
    expect(errorMessage(err(500, 'boom'), tt)).toBe('workshop.errorOther|boom');
    expect(errorMessage(new Error('offline'), tt)).toBe('workshop.errorOther|offline');
  });
});

describe('small helpers', () => {
  it('fills named placeholders and leaves unknown ones', () => {
    expect(fill('in {time} ({x})', { time: '0:01:00' })).toBe('in 0:01:00 ({x})');
  });
  it('labels anchor sources and the backfill state', () => {
    expect(anchorSourceLabel('placed', t)).toBe('workshop.anchorSource.placed');
    expect(anchorSourceLabel(null, t)).toBe('workshop.anchorSource.unknown');
    expect(anchorSourceLabel('cli', t)).toBe('cli');
    expect(backfillKey(true)).toBe('workshop.backfillComplete');
    expect(backfillKey(false)).toBe('workshop.backfillRunning');
    expect(backfillKey(null)).toBe('workshop.backfillUnknown');
  });
});
