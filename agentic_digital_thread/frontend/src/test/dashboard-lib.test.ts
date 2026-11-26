// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests for the pure dashboard logic (src/views/dashboard/lib.ts) —
 * the DOM-free dashboard helpers. The Line State
 * rollup (incl. the all-sites headline fallback) and the Operations Summary
 * derivation are the prime targets. */
import { describe, expect, it } from 'vitest';

import type { DashboardCard, LineStatePayload } from '../api/types/dashboard';
import {
  briefingBulletLines,
  briefingNoteLines,
  bucketMachineState,
  cardHasNoDataYet,
  cleanBriefingText,
  computeLineState,
  computeOperationsSummary,
  extractBriefingRaw,
  fmtCardValue,
  friendlyMetricError,
  gaugeColor,
  gaugePercent,
  gaugeStatusClass,
  gaugeSubLabel,
  getToolLabel,
  hasNonDegenerateRows,
  heatmapCellText,
  heatmapSeverityClass,
  issueSeverityColor,
  LINE_STATE_EXPAND_CAP,
  oeeComponentsPercent,
  oeeDisclosureText,
  panelErrorSummary,
  parseBriefingBullet,
  prettyConcept,
  relativeTime,
  siteChipLabels,
  splitBriefing,
  thresholdColor,
  toPercent,
} from '../views/dashboard/lib';

describe('fmtCardValue', () => {
  it('renders integers without decimals', () => {
    expect(fmtCardValue(113)).toBe('113');
  });
  it('rounds floats to at most 2 decimals', () => {
    expect(fmtCardValue(73.65025401069519)).toBe('73.65');
  });
  it('drops decimals that round to an integer', () => {
    expect(fmtCardValue(99.999)).toBe('100');
  });
  it('passes non-numeric values through as strings', () => {
    expect(fmtCardValue('n/a')).toBe('n/a');
    expect(fmtCardValue(NaN)).toBe('NaN');
  });
});

describe('thresholdColor', () => {
  const bands = [
    { value: 50, color: 'red' },
    { value: 80, color: 'amber' },
    { value: null, color: 'green' },
  ];
  it('picks the FIRST band whose cutoff the value is <=', () => {
    expect(thresholdColor(bands, 42)).toBe('red');
    expect(thresholdColor(bands, 50)).toBe('red');
    expect(thresholdColor(bands, 75)).toBe('amber');
  });
  it('null cutoff is the catch-all final band', () => {
    expect(thresholdColor(bands, 99)).toBe('green');
  });
  it('returns null for no bands / non-numeric values', () => {
    expect(thresholdColor([], 10)).toBeNull();
    expect(thresholdColor(undefined, 10)).toBeNull();
    expect(thresholdColor(bands, 'x')).toBeNull();
  });
});

describe('bucketMachineState', () => {
  it.each([
    ['RUNNING', 'running'],
    ['run', 'running'],
    ['Producing parts', 'running'],
    ['active', 'running'],
    ['DOWN', 'down'],
    ['faulted', 'down'],
    ['e-stop', 'down'],
    ['alarm', 'down'],
    ['offline', 'down'],
    ['idle', 'idle'],
    ['changeover', 'idle'],
    ['maintenance', 'idle'],
    ['standby', 'idle'],
  ])('%s → %s', (state, bucket) => {
    expect(bucketMachineState(state)).toBe(bucket);
  });
  it('defaults unknown states to idle', () => {
    expect(bucketMachineState('mystery')).toBe('idle');
    expect(bucketMachineState(undefined)).toBe('idle');
  });
});

describe('computeLineState', () => {
  it('error payload → error model with default badge and reason', () => {
    const m = computeLineState({ status: 'error', error: 'boom' });
    expect(m.kind).toBe('error');
    expect(m.badge).toBe('machine-state');
    expect(m.diag).toContain('boom');
  });

  it('missing sites → error model with the unmapped-concept reason', () => {
    const m = computeLineState({} as LineStatePayload);
    expect(m.kind).toBe('error');
    expect(m.diag).toContain('machine-state concept is not mapped');
  });

  it('empty sites → zeros', () => {
    const m = computeLineState({ sites: [], source_system_types: ['MES'] });
    expect(m.kind).toBe('empty');
    expect(m.badge).toBe('MES');
    if (m.kind === 'empty') expect(m.headline).toEqual({ running: 0, idle: 0, down: 0 });
  });

  it('badge joins contributing system types', () => {
    const m = computeLineState({
      sites: [{ site: 'A', states: [{ state: 'RUNNING', count: 1 }] }],
      source_system_types: ['CMMS', 'MES'],
    });
    expect(m.badge).toBe('CMMS + MES');
  });

  it('small (line-grained) site: one chip per line, feeds the headline', () => {
    const m = computeLineState({
      sites: [
        {
          site: 'Detroit',
          states: [
            { state: 'RUNNING', count: 2 },
            { state: 'DOWN', count: 1 },
          ],
        },
      ],
    });
    expect(m.kind).toBe('data');
    if (m.kind !== 'data') return;
    expect(m.headline).toEqual({ running: 2, idle: 0, down: 1 });
    // Expanded: 2 RUNNING chips + 1 DOWN chip, each labeled with the raw state.
    expect(m.groups[0].chips).toHaveLength(3);
    expect(m.groups[0].chips[0]).toEqual({ text: 'RUNNING', bucket: 'running' });
    expect(m.groups[0].chips[2]).toEqual({ text: 'DOWN', bucket: 'down' });
  });

  it(`large inventory site (> ${LINE_STATE_EXPAND_CAP} lines): compact chips, does NOT feed the headline when a line-grained site exists`, () => {
    const m = computeLineState({
      sites: [
        { site: 'MES-line3', states: [{ state: 'RUNNING', count: 3 }] },
        {
          site: 'SiteWise',
          states: [
            { state: 'RUNNING', count: 40 },
            { state: 'DOWN', count: 5 },
          ],
        },
      ],
    });
    expect(m.kind).toBe('data');
    if (m.kind !== 'data') return;
    // Headline counts PRODUCTION LINES — only the line-grained site.
    expect(m.headline).toEqual({ running: 3, idle: 0, down: 0 });
    // Compact "STATE · N" chips for the big site.
    expect(m.groups[1].chips).toEqual([
      { text: 'RUNNING \u00b7 40', bucket: 'running' },
      { text: 'DOWN \u00b7 5', bucket: 'down' },
    ]);
  });

  it('ALL-SITES FALLBACK: when no line-grained site contributes, the headline falls back to all sites instead of showing 0', () => {
    const m = computeLineState({
      sites: [
        {
          site: 'SiteWise',
          states: [
            { state: 'RUNNING', count: 40 },
            { state: 'IDLE', count: 10 },
            { state: 'DOWN', count: 5 },
          ],
        },
      ],
    });
    expect(m.kind).toBe('data');
    if (m.kind !== 'data') return;
    expect(m.headline).toEqual({ running: 40, idle: 10, down: 5 });
  });

  it('boundary: a site with exactly the cap total still expands + feeds the headline', () => {
    const m = computeLineState({
      sites: [{ site: 'S', states: [{ state: 'RUNNING', count: LINE_STATE_EXPAND_CAP }] }],
    });
    expect(m.kind).toBe('data');
    if (m.kind !== 'data') return;
    expect(m.headline.running).toBe(LINE_STATE_EXPAND_CAP);
    expect(m.groups[0].chips).toHaveLength(LINE_STATE_EXPAND_CAP);
  });

  it('diag surfaces the raw state → bucket mapping', () => {
    const m = computeLineState({
      sites: [{ site: 'A', states: [{ state: 'FAULT', count: 2 }] }],
    });
    expect(m.diag).toContain('FAULT\u2192down\u00d72');
  });
});

describe('computeOperationsSummary', () => {
  const okCard = (over: Partial<DashboardCard> = {}): DashboardCard => ({
    card_id: 'c1',
    title: 'Throughput',
    viz_type: 'kpi',
    metrics: [{ metric_id: 'm1', status: 'ok', value: 90, unit: 'units' }],
    ...over,
  });

  it('no cards → empty model', () => {
    expect(computeOperationsSummary([], null)).toEqual({ kind: 'empty' });
    expect(computeOperationsSummary(undefined, null)).toEqual({ kind: 'empty' });
  });

  it('all clear → ok with metric count', () => {
    expect(computeOperationsSummary([okCard()], null)).toEqual({ kind: 'ok', count: 1 });
  });

  it('red threshold band → critical insight', () => {
    const model = computeOperationsSummary(
      [okCard({ thresholds: [{ value: 100, color: 'red' }] })],
      null,
    );
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights[0].level).toBe('critical');
    expect(model.insights[0].text).toContain('Throughput 90 units — critical');
  });

  it('below numeric target → warning insight', () => {
    const model = computeOperationsSummary([okCard({ target: 95 })], null);
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights[0]).toEqual({
      level: 'warning',
      text: 'Throughput 90 units — below target 95',
    });
  });

  it('uncovered cards produce NO insight row — the awaiting-data strip owns that story', () => {
    const model = computeOperationsSummary(
      [okCard(), { card_id: 'u1', status: 'uncovered' }, { card_id: 'u2', status: 'uncovered' }],
      null,
    );
    // Nothing else is exceptional → the summary reads all-clear; the
    // uncovered metrics surface ONLY in the awaiting-data strip.
    expect(model).toEqual({ kind: 'ok', count: 3 });
  });

  it('unresolved cards roll up to an unavailable warning', () => {
    const model = computeOperationsSummary(
      [okCard(), { card_id: 'b1', metrics: [{ status: 'error', error: 'x' }] }],
      null,
    );
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights).toContainEqual({
      level: 'warning',
      text: '1 metric unavailable',
    });
  });

  it('machines down (line-state summary) → critical, with idle detail', () => {
    const model = computeOperationsSummary([okCard()], { running: 3, idle: 2, down: 2 });
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights[0]).toEqual({ level: 'critical', text: '2 machines down, 2 idle' });
  });

  it('sorts critical → warning → info', () => {
    const model = computeOperationsSummary(
      [okCard({ target: 95 }), { card_id: 'nd1', status: 'no-data' }],
      { running: 0, idle: 0, down: 1 },
    );
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights.map((i) => i.level)).toEqual(['critical', 'warning', 'info']);
  });

  it("no-data cards roll up to a 'mapped, no rows yet' INFO line — never an unavailable warning (Task 2B)", () => {
    const model = computeOperationsSummary(
      [
        okCard(),
        { card_id: 'nd1', status: 'no-data' },
        { card_id: 'nd2', metrics: [{ metric_id: 'm', status: 'no-data' }] },
      ],
      null,
    );
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights).toContainEqual({
      level: 'info',
      text: '2 metrics need data (mapped, no rows yet)',
    });
    expect(model.insights.some((i) => i.text.includes('unavailable'))).toBe(false);
    expect(model.insights.some((i) => i.level === 'warning')).toBe(false);
  });

  it('no-data keeps its info line while uncovered stays out of the summary', () => {
    const model = computeOperationsSummary(
      [okCard(), { card_id: 'u1', status: 'uncovered' }, { card_id: 'nd1', status: 'no-data' }],
      null,
    );
    expect(model.kind).toBe('insights');
    if (model.kind !== 'insights') return;
    expect(model.insights).toContainEqual({
      level: 'info',
      text: '1 metric need data (mapped, no rows yet)',
    });
    // The uncovered story belongs to the awaiting-data strip — no summary row.
    expect(model.insights.some((i) => i.text.includes('not yet mapped'))).toBe(false);
  });
});

describe('cardHasNoDataYet (Task 2B honest-data predicate)', () => {
  it('card-level no-data status', () => {
    expect(cardHasNoDataYet({ card_id: 'c', status: 'no-data' })).toBe(true);
  });
  it('every metric no-data (all-metrics-degenerate)', () => {
    expect(
      cardHasNoDataYet({
        card_id: 'c',
        metrics: [{ status: 'no-data' }, { status: 'no-data' }],
      }),
    ).toBe(true);
  });
  it('a mixed card (any ok/error metric) is NOT no-data', () => {
    expect(
      cardHasNoDataYet({
        card_id: 'c',
        metrics: [{ status: 'no-data' }, { status: 'ok', value: 1 }],
      }),
    ).toBe(false);
    expect(
      cardHasNoDataYet({
        card_id: 'c',
        metrics: [{ status: 'no-data' }, { status: 'error', error: 'x' }],
      }),
    ).toBe(false);
  });
  it('speed-vs-target ratio without a usable target is no-data, not an honest zero', () => {
    expect(
      cardHasNoDataYet({
        card_id: 'performance',
        viz_type: 'gauge',
        metrics: [
          { metric_id: 'performance-ratio', status: 'ok', value: 0, sample_count: 12 },
          { metric_id: 'machine-speed', status: 'ok', value: 0, sample_count: 12 },
          { metric_id: 'target-machine-speed', status: 'error', value: null },
        ],
      }),
    ).toBe(true);
  });
  it('speed-vs-target ratio keeps an honest sampled zero when target is usable', () => {
    expect(
      cardHasNoDataYet({
        card_id: 'performance',
        viz_type: 'gauge',
        metrics: [
          { metric_id: 'performance-ratio', status: 'ok', value: 0, sample_count: 12 },
          { metric_id: 'machine-speed', status: 'ok', value: 0, sample_count: 12 },
          { metric_id: 'target-machine-speed', status: 'ok', value: 60, sample_count: 12 },
        ],
      }),
    ).toBe(false);
  });
  it('no metrics / ok / uncovered / null → false', () => {
    expect(cardHasNoDataYet({ card_id: 'c' })).toBe(false);
    expect(cardHasNoDataYet({ card_id: 'c', metrics: [] })).toBe(false);
    expect(cardHasNoDataYet({ card_id: 'c', status: 'uncovered' })).toBe(false);
    expect(cardHasNoDataYet(null)).toBe(false);
    expect(cardHasNoDataYet(undefined)).toBe(false);
  });
});

describe('hasNonDegenerateRows (Task 2B degenerate-hide helper)', () => {
  it('empty / absent rows → false', () => {
    expect(hasNonDegenerateRows([])).toBe(false);
    expect(hasNonDegenerateRows(null)).toBe(false);
    expect(hasNonDegenerateRows(undefined)).toBe(false);
  });
  it('all-null values → false (degenerate)', () => {
    expect(
      hasNonDegenerateRows([
        ['L1', null],
        ['L2', undefined],
      ]),
    ).toBe(false);
  });
  it('zeros are honest data → true', () => {
    expect(hasNonDegenerateRows([['L1', 0]])).toBe(true);
  });
  it('any non-null value among nulls → true', () => {
    expect(
      hasNonDegenerateRows([
        ['L1', null],
        ['L2', 42],
      ]),
    ).toBe(true);
  });
  it('non-array rows are ignored', () => {
    expect(hasNonDegenerateRows(['garbage' as unknown as unknown[]])).toBe(false);
  });
});

describe('gauge helpers', () => {
  it('toPercent scales 0-1 ratios and passes 0-100 through (0 preserved)', () => {
    expect(toPercent(0.735)).toBeCloseTo(73.5);
    expect(toPercent(73.5)).toBe(73.5);
    expect(toPercent(0)).toBe(0);
    expect(toPercent('x')).toBeNull();
    expect(toPercent(Infinity)).toBeNull();
  });
  it('gaugePercent honors the per-metric fail-soft contract', () => {
    expect(gaugePercent({ status: 'ok', value: 0.9 })).toBeCloseTo(90);
    expect(gaugePercent({ status: 'error', value: 0.9 })).toBeNull();
    expect(gaugePercent(null)).toBeNull();
  });
  it('gaugeColor / gaugeStatusClass thresholds: >=85 green, >=60 amber, else red', () => {
    expect(gaugeColor(85)).toBe('var(--green)');
    expect(gaugeColor(60)).toBe('var(--amber)');
    expect(gaugeColor(59.9)).toBe('var(--red)');
    expect(gaugeColor(null)).toBe('var(--muted)');
    expect(gaugeStatusClass(85)).toBe('status-good');
    expect(gaugeStatusClass(60)).toBe('status-warning');
    expect(gaugeStatusClass(10)).toBe('status-error');
    expect(gaugeStatusClass(null)).toBe('status-warning');
  });
  it("gaugeSubLabel: 'quality-rate' → 'Quality'", () => {
    expect(gaugeSubLabel({ metric_id: 'quality-rate' })).toBe('Quality');
    expect(gaugeSubLabel({ metric_id: 'availability' })).toBe('Availability');
    expect(gaugeSubLabel({})).toBe('');
  });
  it('gaugeSubLabel prefers the backend label over the id-derived prettifier', () => {
    // The post-1A contract: backend supplies "Quality" for the oee-quality id.
    expect(gaugeSubLabel({ metric_id: 'oee-quality', label: 'Quality' })).toBe('Quality');
    expect(gaugeSubLabel({ metric_id: 'availability', label: 'Availability' })).toBe(
      'Availability',
    );
  });
  it("gaugeSubLabel strips a trailing 'rate' WORD case-insensitively", () => {
    // Legacy payload: label "Quality Rate" must render "Quality", not "OEE QUALITY".
    expect(gaugeSubLabel({ metric_id: 'oee-quality', label: 'Quality Rate' })).toBe('Quality');
    expect(gaugeSubLabel({ label: 'Quality RATE' })).toBe('Quality');
    // Word-boundary only: a label that merely ENDS in "rate" is untouched…
    expect(gaugeSubLabel({ label: 'Operate' })).toBe('Operate');
    // …and a label that IS just "Rate" doesn't strip to empty.
    expect(gaugeSubLabel({ label: 'Rate' })).toBe('Rate');
    // Id fallback still strips: 'first-pass-rate' → 'First Pass'.
    expect(gaugeSubLabel({ metric_id: 'first-pass-rate' })).toBe('First Pass');
  });
});

describe('oeeComponentsPercent — client-side A×P×Q reconciliation', () => {
  const ok = (id: string, value: number) => ({ metric_id: id, status: 'ok', value });
  it('computes A×P×Q as 0–100 from the sub-readings (metrics[1..3])', () => {
    // Mixed scales: 0–1 ratios and 0–100 percentages both normalize (toPercent).
    const metrics = [ok('oee', 76.07), ok('availability', 0.9), ok('performance', 0.85), ok('oee-quality', 93)];
    expect(oeeComponentsPercent(metrics)).toBeCloseTo(71.145, 2);
  });
  it('matches a consistent center (within rounding)', () => {
    const metrics = [ok('oee', 72.9), ok('a', 0.9), ok('p', 0.9), ok('q', 90)];
    expect(oeeComponentsPercent(metrics)).toBeCloseTo(72.9, 4);
  });
  it('returns null when any sub-reading is missing or unresolved', () => {
    // Only two sub-readings.
    expect(oeeComponentsPercent([ok('oee', 76), ok('a', 0.9), ok('p', 0.85)])).toBeNull();
    // Third sub-reading failed to resolve.
    expect(
      oeeComponentsPercent([
        ok('oee', 76),
        ok('a', 0.9),
        ok('p', 0.85),
        { metric_id: 'q', status: 'error', error: 'boom' },
      ]),
    ).toBeNull();
    // Non-finite value.
    expect(
      oeeComponentsPercent([ok('oee', 76), ok('a', 0.9), ok('p', 0.85), ok('q', NaN)]),
    ).toBeNull();
    expect(oeeComponentsPercent(null)).toBeNull();
    expect(oeeComponentsPercent([])).toBeNull();
  });
  it('ignores extra metrics beyond the fourth (same slice GaugeBody renders)', () => {
    const metrics = [ok('oee', 76), ok('a', 1), ok('p', 1), ok('q', 50), ok('x', 0.01)];
    expect(oeeComponentsPercent(metrics)).toBeCloseTo(50, 6);
  });
  it('uses formula copy only when displayed OEE agrees with A×P×Q', () => {
    expect(
      oeeDisclosureText([ok('oee', 72.9), ok('a', 0.9), ok('p', 0.9), ok('q', 90)]),
    ).toContain('OEE = Availability');
    expect(
      oeeDisclosureText([
        ok('oee', 70.97),
        ok('a', 85.94),
        ok('p', 89.96),
        ok('q', 61.42),
      ]),
    ).toBe('OEE as reported by the source system; contributing readings shown below');
  });
  it('does not claim the formula when components are unavailable', () => {
    expect(oeeDisclosureText([ok('oee', 70.97)])).toBe(
      'OEE as reported by the source system; contributing readings shown below',
    );
  });
});

describe('panelErrorSummary', () => {
  it('maps ambiguous joins to an operator summary', () => {
    expect(
      panelErrorSummary("ambiguous join: 2 JoinPaths connect 'a' and 'b' in 'erp'"),
    ).toBe("This chart's data mapping needs attention");
  });
  it('uses the supplied safe fallback for unclassified internals', () => {
    expect(panelErrorSummary('SQLSTATE XX000', 'Quality by line unavailable.')).toBe(
      'Quality by line unavailable.',
    );
  });
});

describe('friendlyMetricError', () => {
  it('maps authority/mapping internals to the operator-facing reason', () => {
    expect(
      friendlyMetricError(
        'no AuthorityRule for concept x and 0 systems map it ([]); call set_authority(...)',
      ),
    ).toBe('not mapped on any onboarded system yet');
  });
  it('maps NotImplemented', () => {
    expect(friendlyMetricError('NotImplementedError: nope')).toBe(
      'not supported for this source',
    );
  });
  it('keeps only the first human clause', () => {
    expect(friendlyMetricError('query timed out; retry later')).toBe('query timed out');
    expect(friendlyMetricError('bad input (code 7)')).toBe('bad input');
  });
  it('empty → generic', () => {
    expect(friendlyMetricError('')).toBe('could not be resolved');
  });
});

describe('labels + severity tokens', () => {
  it('prettyConcept', () => {
    expect(prettyConcept('production-line')).toBe('Production Line');
  });
  it('heatmap severity is a closed vocabulary (unknown → uncovered)', () => {
    expect(heatmapSeverityClass('critical')).toBe('severity-critical');
    expect(heatmapSeverityClass('warning')).toBe('severity-warning');
    expect(heatmapSeverityClass('ok')).toBe('severity-ok');
    expect(heatmapSeverityClass('<img>')).toBe('severity-uncovered');
  });
  it('heatmapCellText formats readings, em-dash for absent', () => {
    expect(heatmapCellText(73.6501)).toBe('73.65');
    expect(heatmapCellText(null)).toBe('\u2014');
    expect(heatmapCellText('')).toBe('\u2014');
  });
  it('issueSeverityColor', () => {
    expect(issueSeverityColor('critical')).toBe('var(--red)');
    expect(issueSeverityColor('warning')).toBe('var(--amber)');
    expect(issueSeverityColor('other')).toBe('var(--muted)');
  });
});

describe('getToolLabel', () => {
  it('known labels', () => {
    expect(getToolLabel('explore_graph')).toBe('Knowledge Graph');
  });
  it('gateway format system___tool', () => {
    expect(getToolLabel('mes-mil___query_mes_mil')).toBe('Query MES · Milwaukee');
    expect(getToolLabel('unknown-sys___q')).toBe('Query unknown-sys');
  });
  it('query_ prefix', () => {
    expect(getToolLabel('query_erp')).toBe('Query ERP');
    expect(getToolLabel('query_mes_cedar_rapids')).toBe('Query MES · Cedar Rapids');
    expect(getToolLabel('query_quality_system')).toBe('Query Quality System');
  });
  it('fallback passthrough', () => {
    expect(getToolLabel('something')).toBe('something');
  });
});

describe('relativeTime', () => {
  it('buckets seconds/minutes/hours/days and remains day-based after one week', () => {
    const now = Date.now();
    expect(relativeTime(new Date(now - 30_000).toISOString())).toBe('just now');
    expect(relativeTime(new Date(now - 120_000).toISOString())).toBe('2m ago');
    expect(relativeTime(new Date(now - 2 * 3_600_000).toISOString())).toBe('2h ago');
    expect(relativeTime(new Date(now - 3 * 86_400_000).toISOString())).toBe('3d ago');
    expect(relativeTime(new Date(now - 8 * 86_400_000).toISOString())).toBe('8d ago');
  });
});

describe('briefing text pipeline', () => {
  it('prefers doneText with the intro line stripped', () => {
    const raw = extractBriefingRaw(
      'Here is your briefing:\n• **Line status:** all running\n• **Quality:** ok',
      'ignored tokens',
    );
    expect(raw).toBe('• **Line status:** all running\n• **Quality:** ok');
  });
  it('falls back to the last briefing block in the token stream', () => {
    const tokens =
      'I will check things now\n---\nHere is your briefing\n• bullet one\n• bullet two';
    expect(extractBriefingRaw('', tokens)).toBe('• bullet one\n• bullet two');
  });
  it('last resort: text after the last ---', () => {
    expect(extractBriefingRaw('', 'preamble --- final text')).toBe('final text');
  });
  it('splits the follow-ups and proposal blocks off before choosing the block', () => {
    // The rehearsal stream shape (2026-10-15): one text run, five bullets, a rule
    // and a closing note, two proposal blocks, then the follow-ups. done.text
    // is empty. The '---' section split used to match the FOLLOWUPS marker
    // and keep only the follow-up Q/A.
    const bullets = [
      '- Line status: no line is running production; most are in changeover [1][2].',
      '- Quality: one milling center shows a bore-diameter drift on two work orders [3].',
      '- Maintenance: one pump station had an emergency shutdown an hour ago [5].',
      '- Maintenance: the same milling center has a bearing PM 9 days overdue [6].',
      '- Order risk: 35 open orders worth $363,291.84 have no shipments yet [7].',
    ].join('\n');
    const tokens =
      bullets +
      '\n\n---\n*The overdue PM and the drift point to the same cause.*\n\n' +
      '---CORRELATION-PROPOSAL---\nPattern: deferred PM matches drift\nConfidence: medium\n' +
      '---END-PROPOSAL---\n\n---VALUE-MAPPING-PROPOSAL---\nfrom_system: a\nto_system: b\n' +
      '---END-PROPOSAL---\n\n\n---FOLLOWUPS---\nQ1: Check the vibration trend?\n' +
      'Q2: Which batches map to the failing runs?\nA1: Expedite the overdue PM.\n' +
      'A2: Dispatch maintenance to the pump station.';
    const clean = cleanBriefingText(extractBriefingRaw('', tokens));
    const lines = briefingBulletLines(clean);
    expect(lines).toHaveLength(5);
    expect(clean).not.toMatch(/Q1:|A1:|Pattern:|from_system/);
    expect(lines[0]).toContain('no line is running production');
  });
  it('keeps the last block when no block has bullets', () => {
    expect(extractBriefingRaw('', 'a plan\n---\nAll lines are running.\n---FOLLOWUPS---\nQ1: x?')).toBe(
      'All lines are running.',
    );
  });
  it('keeps every bullet-bearing section when a rule sits between bullets', () => {
    // Review probe: five bullets with a `---` after the second came back as
    // the last three.
    const tokens = '- a one\n- b two\n---\n- c three\n- d four\n- e five';
    const clean = cleanBriefingText(extractBriefingRaw('', tokens));
    expect(briefingBulletLines(clean)).toEqual([
      '- a one', '- b two', '- c three', '- d four', '- e five',
    ]);
  });
  it('keeps bullets across several rules, before the follow-ups; the closing note is a note', () => {
    // The closing italic line after the cards is a note under them, never a card.
    const tokens =
      '- a one\n---\n- b two\n- c three\n\n---\n- d four\n---\n- e five\n\n---\n' +
      '*A closing note.*\n\n---FOLLOWUPS---\nQ1: x?\nA1: y.';
    const clean = cleanBriefingText(extractBriefingRaw('', tokens));
    expect(briefingBulletLines(clean)).toHaveLength(5);
    expect(briefingNoteLines(clean)).toEqual(['*A closing note.*']);
    expect(clean).not.toMatch(/Q1:|A1:/);
  });
  it('a `---` inside a bullet line does not cut the bullet', () => {
    const tokens = '- Line 2 --- stopped for a changeover\n- b two\n- c three';
    const lines = briefingBulletLines(cleanBriefingText(extractBriefingRaw('', tokens)));
    expect(lines).toHaveLength(3);
    expect(lines[0]).toContain('stopped for a changeover');
  });
  it('cleanBriefingText strips housekeeping, followups and citations', () => {
    const clean = cleanBriefingText(
      "I'll check the lines\n• **Line status:** OK [1][2]\n---FOLLOWUPS---\nQ1?",
    );
    expect(clean).toBe('• **Line status:** OK');
  });
  it('cleanBriefingText strips value-mapping proposal blocks and bare rules', () => {
    const clean = cleanBriefingText(
      '• **Order Risk** — none today\n---\n• **Citations** — ERP\n' +
        '---VALUE-MAPPING-PROPOSAL---\nfrom_system: erp\n---END-PROPOSAL---\n',
    );
    expect(clean).toBe('• **Order Risk** — none today\n• **Citations** — ERP');
    // An OPENED block (stream cut before the terminator) is cut too.
    expect(cleanBriefingText('• **A** — b\n---VALUE-MAPPING-PROPOSAL---\nfrom_system: erp')).toBe(
      '• **A** — b',
    );
  });
  it('cleanBriefingText drops an orphan END-PROPOSAL sentinel and dash-count variants', () => {
    expect(cleanBriefingText('• **A** — b\n---END-PROPOSAL---\n• **C** — d')).toBe(
      '• **A** — b\n• **C** — d',
    );
    expect(
      cleanBriefingText('• **A** — b\n--VALUE-MAPPING-PROPOSAL--\nfrom_system: erp\n--END-PROPOSAL--\n• **C** — d'),
    ).toBe('• **A** — b\n\n• **C** — d');
  });
  it('parseBriefingBullet: bold-prefix title + keyword color', () => {
    const b = parseBriefingBullet('• **Quality:** 2 checks failing');
    expect(b.title).toBe('Quality');
    expect(b.detail).toBe('2 checks failing');
    expect(b.color).toBe('#f0883e');
  });
  it('parseBriefingBullet: dash split fallback + default color', () => {
    const b = parseBriefingBullet('- Shipment — on schedule');
    expect(b.title).toBe('Shipment');
    expect(b.detail).toBe('on schedule');
    expect(b.color).toBe('#58a6ff');
  });
  it('parseBriefingBullet keeps a markdown bold opener (not a list marker)', () => {
    const b = parseBriefingBullet('**Maintenance:** PM due');
    expect(b.title).toBe('Maintenance');
    expect(b.detail).toBe('PM due');
    expect(b.color).toBe('#d29922');
  });
  it('briefingBulletLines filters bullet-marker lines', () => {
    const lines = briefingBulletLines('intro\n• one\n- two\n* three\nplain');
    expect(lines).toEqual(['• one', '- two', '* three']);
  });
});

describe('briefing cards and notes: one rule on both paths', () => {
  // The rule (lib.ts splitBriefing): a CARD is a bullet line (a list item or a
  // bold-title line with text after its marker) that is not wholly italic and
  // does not open with a note label; at most the first five are cards. Every
  // other non-empty line after the first card is a NOTE, in order, verbatim,
  // without rule lines or marker-only lines. Text before the first card is
  // dropped. The final answer (done text) and the token stream go through the
  // same split, so every case runs on both.
  const FIVE = ['- a one', '- b two', '- c three', '- d four', '- e five'];
  const FIVE_TEXT = FIVE.join('\n');
  const read = (raw: string) => {
    const out = [extractBriefingRaw(raw, ''), extractBriefingRaw('', raw)].map((r) => {
      const clean = cleanBriefingText(r);
      return { clean, cards: briefingBulletLines(clean), notes: briefingNoteLines(clean) };
    });
    // Both paths must agree.
    expect(out[0].cards).toEqual(out[1].cards);
    expect(out[0].notes).toEqual(out[1].notes);
    return out[1];
  };

  it('five cards and a trailing italic caveat: the caveat is a note, verbatim', () => {
    const note = '*Note: the ERP system flagged its total order value as unverified, so it is left out.*';
    const r = read(`${FIVE_TEXT}\n\n${note}`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([note]);
  });

  it.each([
    '**Note:** the order value total was omitted.',
    '**Caveat**: the order value total was omitted.',
    '- **Note:** the order value total was omitted.',
    '* *Note:* the order value total was omitted.',
    '__Note:__ the order value total was omitted.',
  ])('five cards and a note-label line %j: the line is a note, not a sixth card', (line) => {
    for (const sep of ['\n', '\n\n']) {
      const r = read(`${FIVE_TEXT}${sep}${line}`);
      expect(r.cards).toEqual(FIVE);
      expect(r.notes).toEqual([line]);
    }
  });

  it('a caveat between bullet 3 and 4: still five cards, the caveat is a note', () => {
    const r = read('- a one\n- b two\n- c three\n*The order total was capped at 500 rows.*\n- d four\n- e five');
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(['*The order total was capped at 500 rows.*']);
  });

  it('six real bullets: the first five are cards, the sixth is a note', () => {
    const r = read(`${FIVE_TEXT}\n- f six\n- g seven`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(['- f six', '- g seven']);
  });

  it('a note keeps its indentation, so a child item stays under its parent', () => {
    const r = read(`${FIVE_TEXT}\n- f six\n  - its detail\n  continued text`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(['- f six', '  - its detail', '  continued text']);
  });

  it('indentation shared by every note line is removed', () => {
    const r = read(`${FIVE_TEXT}\n\n   *The order total was capped at 500 rows.*\n   - capped at row 500`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(['*The order total was capped at 500 rows.*', '- capped at row 500']);
  });

  it('a two-line recovery footnote after a rule: both lines are notes, in order', () => {
    const one = "'A-17' isn't a CMMS identifier; matched it to 'B-17' via the shared serial number.";
    const two = 'The mapping was proposed for review, so once it is accepted future questions translate it.';
    const r = read(`${FIVE_TEXT}\n\n---\n${one}\n${two}`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([one, two]);
  });

  it('a multi-line labelled note after a rule: every line is kept', () => {
    const lines = [
      "*Note:* 'A-17' isn't a CMMS identifier; matched it to 'B-17'.",
      'The mapping was proposed for review, so once accepted future questions translate automatically.',
      'Until then the briefing reads B-17.',
    ];
    const r = read(`${FIVE_TEXT}\n\n---\n${lines.join('\n')}`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(lines);
  });

  it('a multi-line italic paragraph after the cards: every line is kept', () => {
    const r = read(`${FIVE_TEXT}\n\n*First sentence of the caveat.\nSecond sentence of the caveat.*`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(['*First sentence of the caveat.', 'Second sentence of the caveat.*']);
  });

  it('a closing sentence after the cards is a note (shown under the cards)', () => {
    for (const sep of ['\n\n', '\n\n---\n']) {
      const r = read(`${FIVE_TEXT}${sep}That is all for this shift.`);
      expect(r.cards).toEqual(FIVE);
      expect(r.notes).toEqual(['That is all for this shift.']);
    }
  });

  it('text before the first card is dropped', () => {
    const r = read(`Overview of the shift.\nAll four plants reported.\n\n${FIVE_TEXT}`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([]);
  });

  it.each(['* *All lines are normal.*', '- *All lines are normal.*', '• _All lines are normal._'])(
    'a wholly italic list item %j stays a card', (line) => {
      const r = read(`- a one\n- b two\n- c three\n- d four\n${line}`);
      expect(r.cards).toHaveLength(5);
      expect(r.cards[4]).toBe(line);
      expect(r.notes).toEqual([]);
    });

  it('a wholly italic line is a note; a line opening with an italic word is a card', () => {
    const r = read('- a one\n- b two\n- c three\n- d four\n*Line 2* is down\n*Line 3 is down too.*');
    expect(r.cards).toEqual(['- a one', '- b two', '- c three', '- d four', '*Line 2* is down']);
    expect(r.notes).toEqual(['*Line 3 is down too.*']);
  });

  it.each(['-', '- ', '*', '•', '-   ', '---'])('marker-only line %j is neither a card nor a note', (line) => {
    const r = read(`- a one\n- b two\n${line}\n- c three\n- d four\n- e five\n${line}`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([]);
  });

  it('a rule between bullets does not cut them', () => {
    const r = read('- a one\n- b two\n---\n- c three\n\n---\n- d four\n- e five');
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([]);
  });

  it.each([
    ['*item', '', 'item'],
    ['*Line 2* is down', '', '*Line 2* is down'],
    ['* item', '', 'item'],
    ['**Bold** text', 'Bold', 'text'],
    ['***x***', '', '***x***'],
    ['* *italic* rest', '', '*italic* rest'],
    ['• item', '', 'item'],
    ['**Line 2** is down', 'Line 2', 'is down'],
    ['**Notes on line 2:** belt slipping', 'Notes on line 2', 'belt slipping'],
    ['- **Order risk:** 22 orders late', 'Order risk', '22 orders late'],
  ])('%j is a card with title %j', (line, title, detail) => {
    const r = read(`- a one\n- b two\n- c three\n- d four\n${line}`);
    expect(r.cards).toHaveLength(5);
    expect(r.notes).toEqual([]);
    const card = parseBriefingBullet(r.cards[4]);
    expect([card.title, card.detail]).toEqual([title, detail]);
  });

  it('bold-title cards with no list marker, then a closing italic note after a rule', () => {
    const r = read('**Line status:** all idle\n**Quality:** one drift\n**Maintenance:** two open\n' +
      '**Order risk:** none\n**Pattern:** none\n\n---\n*Closing note.*');
    expect(r.cards).toHaveLength(5);
    expect(r.notes).toEqual(['*Closing note.*']);
  });

  it.each([
    '*additional_candidate_pairs_observed: 0*',
    '_additional_candidate_pairs_observed: 3_',
    'additional_candidate_pairs_observed: 2',
  ])('the internal counter %j is never a card or a note', (counter) => {
    const r = read(`${FIVE_TEXT}\n\n---\n${counter}`);
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([]);
    expect(r.clean).not.toContain('additional_candidate_pairs_observed');
  });

  it('follow-ups and proposal blocks never reach the notes', () => {
    const r = read(`${FIVE_TEXT}\n\n---VALUE-MAPPING-PROPOSAL---\nfrom_system: a\n---END-PROPOSAL---\n` +
      '\n---FOLLOWUPS---\nQ1: Check the trend?\nA1: Expedite the PM.');
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual([]);
  });

  it('a CRLF answer gives the same cards and notes, with no carriage return left', () => {
    const r = read(`${FIVE_TEXT}\n\n*Note: the total was capped.*`.replace(/\n/g, '\r\n'));
    expect(r.cards).toEqual(FIVE);
    expect(r.notes).toEqual(['*Note: the total was capped.*']);
    expect(r.clean).not.toContain('\r');
  });

  it('splitBriefing returns the same cards and notes as the two helpers', () => {
    const clean = cleanBriefingText(`${FIVE_TEXT}\n- f six\n\n*A caveat.*`);
    expect(splitBriefing(clean)).toEqual({
      cards: briefingBulletLines(clean),
      notes: briefingNoteLines(clean),
    });
  });
});

describe('siteChipLabels', () => {
  it('uses the site, falling back to the system name for unsited entries', () => {
    expect(
      siteChipLabels([
        { site: '', system_id: 'erp', system_name: 'ERP' },
        { site: 'Plant A', system_id: 'a', system_name: 'A MES' },
        { system_id: 'bare' },
      ]),
    ).toEqual(['ERP', 'Plant A', 'bare']);
  });
  it('names the system when two entries share one site label', () => {
    expect(
      siteChipLabels([
        { site: 'Plant C', system_id: 'hist', system_name: 'Historian C' },
        { site: 'Plant C', system_id: 'mes-c' },
        { site: 'Plant D', system_id: 'mes-d', system_name: 'D MES' },
      ]),
    ).toEqual(['Plant C \u00b7 Historian C', 'Plant C \u00b7 mes-c', 'Plant D']);
  });
});
