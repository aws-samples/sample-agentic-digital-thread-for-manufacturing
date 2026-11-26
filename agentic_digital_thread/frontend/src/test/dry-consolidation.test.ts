// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { requestJson } from '../api/client';
import { formatRelativeTime } from '../lib/relative-time';
import { parseToolIdentity } from '../lib/tool-identity';
import {
  dimensionErrorEnvelope,
  makeDimensionLoader,
} from '../views/dashboard/dimension-loader';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

const requestJsonMock = vi.mocked(requestJson);

beforeEach(() => {
  requestJsonMock.mockReset();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('formatRelativeTime', () => {
  it('supports the dashboard day ceiling and SessionMenu week ceiling', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-16T12:00:00Z'));
    const eightDaysAgo = new Date('2026-07-08T12:00:00Z');

    expect(formatRelativeTime(eightDaysAgo, { maxUnit: 'day' })).toBe('8d ago');
    expect(formatRelativeTime(eightDaysAgo.getTime(), { maxUnit: 'week' })).toBe('1w ago');
    expect(formatRelativeTime(null, { empty: 'never' })).toBe('never');
  });
});

describe('parseToolIdentity', () => {
  it('parses gateway, local query, and plain shapes without choosing presentation', () => {
    expect(parseToolIdentity('mes-mil___query_mes_mil')).toMatchObject({
      shape: 'gateway',
      systemId: 'mes-mil',
      operation: 'query_mes_mil',
      family: 'mes',
      qualifier: 'mil',
      separator: '-',
    });
    expect(parseToolIdentity('query_mes_cedar_rapids')).toMatchObject({
      shape: 'query',
      systemId: 'mes_cedar_rapids',
      family: 'mes',
      qualifier: 'cedar_rapids',
      separator: '_',
    });
    expect(parseToolIdentity('explore_graph')).toMatchObject({
      shape: 'plain',
      systemId: null,
      operation: null,
    });
  });

  it("retains the callers' first-operation-segment contract", () => {
    expect(parseToolIdentity('erp___query_erp___ignored').operation).toBe('query_erp');
  });
});

describe('makeDimensionLoader', () => {
  it('uses the canonical body and preserves caller augmentation', async () => {
    requestJsonMock.mockResolvedValue({ status: 'ok', rows: [['Line 1', 12]] });
    const stored: unknown[] = [];
    const loader = makeDimensionLoader({
      name: 'test-dimension',
      metricId: 'actual-quantity-windowed',
      dimensionConcept: 'production-line',
      setPayload: (payload) => stored.push(payload),
      transform: (payload) => ({ ...payload, kind: 'augmented' }),
    });

    await loader.load();

    expect(requestJsonMock).toHaveBeenCalledWith('/api/metrics/resolve-by-dimension', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: expect.any(AbortSignal),
      body: JSON.stringify({
        metric_id: 'actual-quantity-windowed',
        dimension_concept: 'production-line',
      }),
    });
    expect(stored).toEqual([
      { status: 'ok', rows: [['Line 1', 12]], kind: 'augmented' },
    ]);
  });

  it('uses the established friendly envelope on a first failure by default', async () => {
    requestJsonMock.mockRejectedValue(new Error('resolver offline'));
    const stored: unknown[] = [];
    const loader = makeDimensionLoader({
      name: 'test-dimension-error',
      metricId: 'quality-yield-rate',
      dimensionConcept: 'production-line',
      setPayload: (payload) => stored.push(payload),
    });

    await loader.load();

    expect(stored).toEqual([{ status: 'error', error: 'resolver offline' }]);
    expect(dimensionErrorEnvelope('plain failure')).toEqual({
      status: 'error',
      error: 'plain failure',
    });
  });
});
