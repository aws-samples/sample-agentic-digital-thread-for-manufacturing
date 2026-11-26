// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Answer times: the model writes every time in UTC, the browser shows it in
 * the viewer's zone. These pin the pure helpers in lib/answer-times.ts: which
 * text counts as a UTC time, and how it reads in a given zone. The zone and
 * locale are injected so the results do not depend on the test host. */
import { afterEach, describe, expect, it } from 'vitest';

import {
  findUtcStamps,
  formatLocalStamp,
  localTimeOptions,
  setDisplayTimeZone,
} from '../lib/answer-times';

const DENVER = { timeZone: 'America/Denver', locale: 'en-US' };
const KOLKATA = { timeZone: 'Asia/Kolkata', locale: 'en-US' };

function localOf(text: string, opts = DENVER): string | null {
  const [stamp] = findUtcStamps(text);
  return stamp ? formatLocalStamp(stamp.iso, stamp.seconds, opts) : null;
}

afterEach(() => setDisplayTimeZone(null));

describe('findUtcStamps', () => {
  it('finds the answer form and keeps the source text and an ISO instant', () => {
    const stamps = findUtcStamps('Window end 2026-10-15 14:17 UTC, then more.');
    expect(stamps).toHaveLength(1);
    expect(stamps[0]).toMatchObject({
      text: '2026-10-15 14:17 UTC',
      iso: '2026-10-15T14:17Z',
      seconds: false,
    });
    expect('Window end 2026-10-15 14:17 UTC, then more.'.slice(stamps[0].start, stamps[0].end)).toBe(
      '2026-10-15 14:17 UTC',
    );
  });

  it('finds the answer form with seconds', () => {
    const [stamp] = findUtcStamps('at 2026-10-15 14:17:05 UTC');
    expect(stamp).toMatchObject({ iso: '2026-10-15T14:17:05Z', seconds: true });
  });

  it('finds ISO instants with Z, with or without seconds and fractions', () => {
    const stamps = findUtcStamps('from 2026-10-15T13:17Z to 2026-10-15T14:17:13.250Z.');
    expect(stamps.map((s) => s.text)).toEqual(['2026-10-15T13:17Z', '2026-10-15T14:17:13.250Z']);
    expect(stamps.map((s) => s.seconds)).toEqual([false, true]);
    expect(stamps[1].iso).toBe('2026-10-15T14:17:13.250Z');
  });

  it('leaves a time with no date, a date with no time, and a non-UTC offset alone', () => {
    expect(findUtcStamps('the alarm fired at 14:17 UTC')).toEqual([]);
    expect(findUtcStamps('opened 2026-10-15, closed 2026-10-16')).toEqual([]);
    expect(findUtcStamps('2026-10-15 UTC')).toEqual([]);
    expect(findUtcStamps('2026-10-15 14:17 UTC+5 and 2026-10-15T14:17+05:00')).toEqual([]);
    expect(findUtcStamps('2026-10-15 14:17 MDT')).toEqual([]);
  });

  it('leaves a UTC time followed by a spaced offset alone', () => {
    for (const offset of [' +5', ' + 05:30', ' -5', ' \u22125', '\t+5', '\u00a0+5', '\u202f+05:30', '\u2009-5', ' +0530']) {
      expect(findUtcStamps(`2026-10-15 14:17 UTC${offset}`)).toEqual([]);
    }
    // A dash that starts the next clause is not an offset.
    expect(findUtcStamps('2026-10-15 14:17 UTC - all three axes rose')).toHaveLength(1);
    expect(findUtcStamps('2026-10-15 14:17 UTC\n-5 units')).toHaveLength(1);
    expect(findUtcStamps('2026-10-15 14:17 UTC\u2028-5 units')).toHaveLength(1);
  });

  it('converts both ends of a range written in full, whatever the separator', () => {
    for (const sep of [' - ', '-', ' \u2013 ', '\u2013', ' \u2014 ', ' \u2192 ', ' to ']) {
      const stamps = findUtcStamps(`2026-10-16 02:19 UTC${sep}2026-10-16 03:18 UTC`);
      expect(stamps.map((s) => s.iso)).toEqual(['2026-10-16T02:19Z', '2026-10-16T03:18Z']);
    }
  });

  it('leaves impossible dates and times alone', () => {
    expect(findUtcStamps('2026-02-30 10:00 UTC')).toEqual([]);
    expect(findUtcStamps('2026-13-01 10:00 UTC')).toEqual([]);
    expect(findUtcStamps('2026-10-15 24:00 UTC')).toEqual([]);
    expect(findUtcStamps('2026-10-15T10:60Z')).toEqual([]);
  });

  it('skips a UTC audit stamp in parentheses right after a zoned local time', () => {
    expect(findUtcStamps('2026-10-15 08:17 MDT (2026-10-15 14:17 UTC)')).toEqual([]);
    expect(findUtcStamps('8:17 a.m. CDT ( 2026-10-15T13:17Z )')).toEqual([]);
    expect(findUtcStamps('19:47 +05:30 (2026-10-15 14:17 UTC)')).toEqual([]);
    expect(findUtcStamps('14:17 UTC (2026-10-15 14:17 UTC)')).toHaveLength(1);
    expect(findUtcStamps('Plant MDT (2026-10-15 14:17 UTC)')).toHaveLength(1);
  });

  it('does not match inside a longer token', () => {
    expect(findUtcStamps('id12026-10-15 14:17 UTC')).toEqual([]);
    expect(findUtcStamps('2026-10-15T14:17Zulu')).toEqual([]);
  });
});

describe('formatLocalStamp', () => {
  it('shows a UTC time in America/Denver with its abbreviation', () => {
    expect(localOf('2026-10-15 14:17 UTC')).toBe('2026-10-15 08:17 MDT');
    expect(localOf('2026-10-15T14:17:13Z')).toBe('2026-10-15 08:17:13 MDT');
  });

  it('moves the date back when the local time is before midnight', () => {
    expect(localOf('2026-07-15 05:03 UTC')).toBe('2026-07-14 23:03 MDT');
  });

  it('handles a half-hour offset zone', () => {
    expect(localOf('2026-10-15 14:17 UTC', KOLKATA)).toBe('2026-10-15 19:47 GMT+5:30');
    expect(localOf('2026-10-15 20:45 UTC', KOLKATA)).toBe('2026-10-16 02:15 GMT+5:30');
  });

  it('follows the daylight saving change (America/Denver, 2026-11-01)', () => {
    expect(localOf('2026-11-01 07:59 UTC')).toBe('2026-11-01 01:59 MDT');
    expect(localOf('2026-11-01 08:00 UTC')).toBe('2026-11-01 01:00 MST');
    expect(localOf('2026-03-08 09:00 UTC')).toBe('2026-03-08 03:00 MDT');
  });

  it('shows midnight as 00, never 24', () => {
    expect(localOf('2026-10-15 06:00 UTC')).toBe('2026-10-15 00:00 MDT');
  });

  it('returns null for an unknown zone so the caller keeps the UTC text', () => {
    expect(formatLocalStamp('2026-10-15T14:17Z', false, { timeZone: 'Not/AZone' })).toBeNull();
  });
});

describe('display zone seam', () => {
  it('defaults to the browser zone and can be pinned', () => {
    expect(localTimeOptions()).toEqual({ timeZone: undefined, locale: undefined });
    setDisplayTimeZone('Asia/Kolkata', 'en-US');
    expect(localTimeOptions()).toEqual({ timeZone: 'Asia/Kolkata', locale: 'en-US' });
    setDisplayTimeZone(null);
    expect(localTimeOptions()).toEqual({ timeZone: undefined, locale: undefined });
  });
});
