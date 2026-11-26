// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chart labels that are UTC instants show in the viewer's zone, like the
 * answer prose beside them. Tick labels are HH:MM (seconds when the label has
 * them), with the date when the series crosses local midnight; the tooltip
 * shows the full local date, time and zone. A title's UTC times convert the
 * same way. Labels that are not times pass through untouched. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { buildChartConfigFromAttrs, buildChartSrcdoc } from '../chat/charts';
import { localChartLabels, setDisplayTimeZone } from '../lib/answer-times';

const DENVER = { timeZone: 'America/Denver', locale: 'en-US' };

type Config = {
  data: { labels: unknown[] };
  tickLabels?: unknown[];
  options: { plugins: { title: { text: string } } };
};

function attrs(labels: unknown[], title = 'T'): string {
  return `type="line" title="${title}" labels='${JSON.stringify(labels)}' datasets='[{"label":"x","data":[1,2,3]}]'`;
}

beforeEach(() => setDisplayTimeZone('America/Denver', 'en-US'));
afterEach(() => setDisplayTimeZone(null));

describe('localChartLabels', () => {
  it('turns UTC minutes into local HH:MM ticks and full local tooltips', () => {
    const out = localChartLabels(['2026-10-15T14:00Z', '2026-10-15T14:05Z'], DENVER);
    expect(out).toEqual({
      ticks: ['08:00', '08:05'],
      full: ['2026-10-15 08:00 MDT', '2026-10-15 08:05 MDT'],
    });
  });

  it('adds the local date to ticks when the series crosses local midnight', () => {
    const out = localChartLabels(['2026-10-16T05:30Z', '2026-10-16T06:30Z'], DENVER);
    expect(out?.ticks).toEqual(['10-15 23:30', '10-16 00:30']);
    expect(out?.full).toEqual(['2026-10-15 23:30 MDT', '2026-10-16 00:30 MDT']);
  });

  it('adds the year when the series spans years', () => {
    const out = localChartLabels(['2027-01-01T06:30Z', '2027-01-01T07:30Z'], DENVER);
    expect(out?.ticks).toEqual(['2026-12-31 23:30', '2027-01-01 00:30']);
  });

  it('keeps seconds and fractions that keep readings apart', () => {
    const out = localChartLabels(['2026-01-05T11:00:05.250000Z', '2026-01-05T11:00:05.750000Z'], DENVER);
    expect(out?.ticks).toEqual(['04:00:05.250', '04:00:05.750']);
    expect(out?.full).toEqual(['2026-01-05 04:00:05.250 MST', '2026-01-05 04:00:05.750 MST']);
  });

  it('passes labels that are not UTC instants through untouched', () => {
    expect(localChartLabels(['Line 1', 'Line 2'], DENVER)).toBeNull();
    expect(localChartLabels(['07:00', '07:01'], DENVER)).toBeNull();
    const mixed = localChartLabels(['2026-10-15T14:00Z', 'total'], DENVER);
    expect(mixed).toEqual({ ticks: ['08:00', 'total'], full: ['2026-10-15 08:00 MDT', 'total'] });
  });
});

describe('buildChartConfigFromAttrs', () => {
  it('puts full local labels on the data and short ticks beside them', () => {
    const cfg = buildChartConfigFromAttrs(attrs(['2026-10-15T14:00Z', '2026-10-15T14:05Z'])) as Config;
    expect(cfg.data.labels).toEqual(['2026-10-15 08:00 MDT', '2026-10-15 08:05 MDT']);
    expect(cfg.tickLabels).toEqual(['08:00', '08:05']);
  });

  it('converts the UTC times in a chart title', () => {
    const title = 'Unit A: vib (mm/s), 2026-01-05 11:00 UTC to 2026-01-05 11:05 UTC';
    const cfg = buildChartConfigFromAttrs(attrs(['a', 'b'], title)) as Config;
    expect(cfg.options.plugins.title.text).toBe('Unit A: vib (mm/s), 2026-01-05 04:00 MST to 2026-01-05 04:05 MST');
  });

  it('leaves category labels and their config shape unchanged', () => {
    const cfg = buildChartConfigFromAttrs(attrs(['Line 1', 'Line 2'])) as Config;
    expect(cfg.data.labels).toEqual(['Line 1', 'Line 2']);
    expect(cfg.tickLabels).toBeUndefined();
  });

  it('the chart document draws the short ticks on the x axis', () => {
    const cfg = buildChartConfigFromAttrs(attrs(['2026-10-15T14:00Z', '2026-10-15T14:05Z']));
    const doc = buildChartSrcdoc(cfg);
    expect(doc).toContain('cfg.tickLabels');
    expect(doc).toContain('ticks.callback');
    expect(doc.match(/<\/script>/g)?.length).toBe(2);
  });
});
