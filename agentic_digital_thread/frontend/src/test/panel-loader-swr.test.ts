// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the SWR guard on the lazy panel loaders that every SSE dashboard
 * snapshot re-fires (stream.ts processDashboardData): a burst of snapshot
 * events produces exactly ONE request per panel loader (in-flight dedupe + 15s
 * freshness TTL), the TTL expiring re-fetches, a failed refresh KEEPS the
 * cached panel data (console.error only), and the Ops Summary snapshot-failure
 * flag flips on a failed GET fallback. Three loaders share the
 * /api/metrics/resolve-by-dimension endpoint (quality-by-line + the
 * count bars), so per-loader counts match on url + body metric_id. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../api/client';
import { dashboardStore, setDashboardData, setSnapshotFailed } from '../views/dashboard/store';
import { _resetLineStateLoader, loadLineState } from '../views/dashboard/lineState';
import {
  _resetPanelLoaders,
  loadActiveIssues,
  loadOutputByLine,
  loadQualityByLine,
  loadSensorHeatmap,
  loadWorkOrdersByStatus,
} from '../views/dashboard/Panels';
import { _resetSectionsLoader } from '../views/dashboard/sections';
import {
  _resetPanelStagger,
  loadDashboardData,
  PANEL_STAGGER_MS,
  processDashboardData,
} from '../views/dashboard/stream';
import {
  ERROR_BACKOFF_MS,
  ERROR_BACKOFF_MAX_MS,
  FETCH_TIMEOUT_MS,
  PANEL_TTL_MS,
} from '../views/dashboard/swr';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

const requestJsonMock = vi.mocked(requestJson);

/** URL-routed requestJson mock covering all panel endpoints. */
function okRequestStub() {
  requestJsonMock.mockImplementation(async (url: string) => {
    const body: Record<string, unknown> = {};
    if (String(url).includes('/api/dashboard/sections')) body.sections = [];
    if (String(url).includes('/api/dashboard/active-issues')) body.issues = [];
    if (String(url).includes('/api/dashboard/line-state')) body.rows = [];
    if (String(url).includes('/api/metrics/resolve')) body.rows = [];
    return body;
  });
  return requestJsonMock;
}

interface LoaderCall {
  url: string;
  /** Body fragment disambiguating loaders that share an endpoint. */
  body?: string;
}

function callsTo(requestMock: typeof requestJsonMock, m: LoaderCall): number {
  return requestMock.mock.calls.filter((c) => {
    if (!String(c[0]).includes(m.url)) return false;
    if (!m.body) return true;
    const init = c[1] as { body?: unknown } | undefined;
    return String(init && init.body).includes(m.body);
  }).length;
}

const PANEL_CALLS: LoaderCall[] = [
  { url: '/api/dashboard/line-state' },
  { url: '/api/metrics/resolve-by-dimension', body: '"metric_id":"quality-yield-rate"' },
  { url: '/api/metrics/resolve-by-dimension', body: '"metric_id":"work-order-count"' },
  { url: '/api/metrics/resolve-by-dimension', body: '"metric_id":"actual-quantity-windowed"' },
  { url: '/api/dashboard/active-issues' },
  { url: '/api/metrics/resolve-heatmap' },
];

function resetAllLoaders(): void {
  _resetPanelLoaders();
  _resetLineStateLoader();
  _resetSectionsLoader();
  _resetPanelStagger();
}

async function flush(): Promise<void> {
  // Drain the loaders' microtask chains (fetch stub resolves immediately).
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

/** Total span of the staggered convoy. */
const STAGGER_SPAN_MS = PANEL_STAGGER_MS * (PANEL_CALLS.length - 1);

/** Advance fake timers through the full stagger so every lazy loader has
 * fired (advanceTimersByTimeAsync drains microtasks between timers). */
async function runStagger(): Promise<void> {
  await vi.advanceTimersByTimeAsync(STAGGER_SPAN_MS);
  await flush();
}

beforeEach(() => {
  vi.useFakeTimers();
  requestJsonMock.mockReset();
  resetAllLoaders();
  setSnapshotFailed(false);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('panel loaders — SWR dedupe across snapshot bursts', () => {
  it('a burst of snapshot events fires exactly ONE request per panel endpoint', async () => {
    const requestMock = okRequestStub();

    // Three rapid SSE snapshots (the audit finding: each re-fired all five).
    processDashboardData({ cards: [] });
    processDashboardData({ cards: [] });
    processDashboardData({ cards: [] });
    await runStagger();

    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(1);
    }

    // A LATER snapshot inside the 15s freshness TTL still refetches nothing.
    vi.advanceTimersByTime(PANEL_TTL_MS - STAGGER_SPAN_MS - 5_000);
    processDashboardData({ cards: [] });
    await runStagger();
    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(1);
    }
  });

  it('after the TTL expires, a snapshot refetches each panel once', async () => {
    const requestMock = okRequestStub();

    processDashboardData({ cards: [] });
    await runStagger();
    vi.advanceTimersByTime(PANEL_TTL_MS + 1_000);

    processDashboardData({ cards: [] });
    await runStagger();

    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(2);
    }
  });

  it('concurrent direct calls share one in-flight request per loader', async () => {
    const requestMock = okRequestStub();

    await Promise.all([
      loadQualityByLine(),
      loadQualityByLine(),
      loadWorkOrdersByStatus(),
      loadWorkOrdersByStatus(),
      loadOutputByLine(),
      loadOutputByLine(),
      loadActiveIssues(),
      loadActiveIssues(),
      loadSensorHeatmap(),
      loadSensorHeatmap(),
      loadLineState(),
      loadLineState(),
    ]);

    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(1);
    }
  });

  it('a failed refresh KEEPS the cached panel data and logs (no silent hide)', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    okRequestStub();
    await loadQualityByLine();
    expect(dashboardStore.get().quality).not.toBeNull();

    vi.advanceTimersByTime(PANEL_TTL_MS + 1_000);
    requestJsonMock.mockRejectedValue(
      new ApiError(500, '/api/metrics/resolve-by-dimension', 'HTTP 500'),
    );
    await loadQualityByLine();

    expect(dashboardStore.get().quality).not.toBeNull(); // cached data kept
    expect(errSpy).toHaveBeenCalled(); // logged, never silent
  });

  it('a failed FIRST load runs the existing fail-safe (hidden panel) and logs', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    requestJsonMock.mockRejectedValue(
      new ApiError(503, '/api/metrics/resolve-by-dimension', 'HTTP 503'),
    );

    await loadQualityByLine();

    expect(dashboardStore.get().quality).toBeNull(); // hidden-on-failure kept
    expect(errSpy).toHaveBeenCalled();
  });

  it('preserves output-by-line lineage window augmentation', async () => {
    requestJsonMock.mockResolvedValue({
      status: 'ok',
      rows: [['Line 1', 12]],
      lineage: [{ query_plan: { window: '24h' } }],
    });

    await loadOutputByLine();

    expect(dashboardStore.get().outputByLine).toMatchObject({
      status: 'ok',
      rows: [['Line 1', 12]],
      window: '24h',
    });
  });
});

describe('entity sections — strictly on demand', () => {
  it('never requests /api/dashboard/sections during first paint or repeat snapshots', async () => {
    const requestMock = okRequestStub();

    processDashboardData({ cards: [] });
    processDashboardData({ cards: [] });
    await runStagger();
    vi.advanceTimersByTime(PANEL_TTL_MS + 1_000);
    processDashboardData({ cards: [] });
    await runStagger();

    expect(callsTo(requestMock, { url: '/api/dashboard/sections' })).toBe(0);
  });
});

describe('Ops Summary snapshot failure flag', () => {
  it('a failed GET /api/dashboard fallback sets snapshotFailed; a snapshot clears it', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    requestJsonMock.mockRejectedValue(new Error('network down'));

    await loadDashboardData();
    expect(dashboardStore.get().snapshotFailed).toBe(true);

    setDashboardData({ cards: [] });
    expect(dashboardStore.get().snapshotFailed).toBe(false);
  });
});

// ── Request discipline (slowload diagnosis) ───────────────────────────────────

describe('fetch timeout — hung requests abort and release the in-flight lock', () => {
  it('aborts a hung request at the timeout, fails soft, and does NOT dead-lock', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    errSpy.mockClear(); // the shared spy accumulates across specs
    // A request that never settles (the blocked-event-loop backend) — but the
    // guard's abort race must fail the attempt anyway.
    let capturedSignal: AbortSignal | undefined;
    requestJsonMock.mockImplementation((_url: string, init?: RequestInit) => {
      capturedSignal = init?.signal ?? undefined;
      return new Promise(() => {}); // hangs forever
    });

    const p = loadActiveIssues();
    await vi.advanceTimersByTimeAsync(FETCH_TIMEOUT_MS + 1);
    await p; // resolves via the failure path — never hangs

    expect(capturedSignal?.aborted).toBe(true); // typed request aborted
    expect(dashboardStore.get().activeIssues).toBeNull(); // first-load fail-safe ran
    expect(errSpy).toHaveBeenCalledTimes(1);

    // Lock released (no dead lock): after the backoff window, the next call
    // actually requests and recovers.
    vi.advanceTimersByTime(ERROR_BACKOFF_MS + 1_000);
    requestJsonMock.mockClear();
    const okMock = okRequestStub();
    await loadActiveIssues();
    expect(callsTo(okMock, { url: '/api/dashboard/active-issues' })).toBe(1);
    expect(dashboardStore.get().activeIssues).not.toBeNull();
  });
});

describe('error backoff — failed loaders are not refired by every snapshot', () => {
  it('snapshot refires within the backoff window are no-ops; the window allows a retry', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const requestMock = requestJsonMock.mockImplementation(async (url: string) => {
      throw new ApiError(500, url, 'HTTP 500');
    });

    // First snapshot: every loader attempts once and fails.
    processDashboardData({ cards: [] });
    await runStagger();
    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(1);
    }

    // Repeat snapshots WITHIN the backoff window: zero new fetches (the old
    // behavior refired every loader into a fresh error on every snapshot).
    processDashboardData({ cards: [] });
    await runStagger();
    vi.advanceTimersByTime(ERROR_BACKOFF_MS / 2);
    processDashboardData({ cards: [] });
    await runStagger();
    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(1);
    }

    // After the window expires, a snapshot retries each loader once.
    vi.advanceTimersByTime(ERROR_BACKOFF_MS + 2_000);
    processDashboardData({ cards: [] });
    await runStagger();
    for (const m of PANEL_CALLS) {
      expect(callsTo(requestMock, m), m.url + (m.body || '')).toBe(2);
    }
  });

  it('backoff doubles while failures persist (capped) and resets on success', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let failing = true;
    const requestMock = requestJsonMock.mockImplementation(async (url: string) => {
      if (failing) throw new ApiError(500, url, 'HTTP 500');
      return { issues: [] };
    });

    await loadActiveIssues(); // fail #1 → backoff 60s
    expect(requestMock).toHaveBeenCalledTimes(1);
    await loadActiveIssues(); // in window → no-op
    expect(requestMock).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(ERROR_BACKOFF_MS + 1_000);
    await loadActiveIssues(); // fail #2 → backoff doubles to 120s
    expect(requestMock).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(ERROR_BACKOFF_MS + 1_000); // 61s < 120s → still blocked
    await loadActiveIssues();
    expect(requestMock).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(ERROR_BACKOFF_MS * 2); // past the doubled window
    failing = false;
    await loadActiveIssues(); // succeeds → streak + backoff reset
    expect(requestMock).toHaveBeenCalledTimes(3);

    // Post-success: the next failure starts a FRESH 60s window (not 120s+).
    vi.advanceTimersByTime(PANEL_TTL_MS + 1_000);
    failing = true;
    await loadActiveIssues(); // fail → new streak, 60s window
    expect(requestMock).toHaveBeenCalledTimes(4);
    vi.advanceTimersByTime(ERROR_BACKOFF_MS + 1_000);
    await loadActiveIssues(); // 60s window elapsed → retries
    expect(requestMock).toHaveBeenCalledTimes(5);
    // Sanity: doubling never exceeds the cap constant.
    expect(ERROR_BACKOFF_MAX_MS).toBeGreaterThanOrEqual(ERROR_BACKOFF_MS);
  });
});

describe('error-once surfacing — one console.error per failure streak', () => {
  it('logs the first failure, stays silent on streak retries, logs again after recovery', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    errSpy.mockClear(); // the shared spy accumulates across specs
    let failing = true;
    requestJsonMock.mockImplementation(async (url: string) => {
      if (failing) throw new ApiError(503, url, 'HTTP 503');
      return { issues: [] };
    });

    await loadActiveIssues(); // fail #1 → logged
    expect(errSpy).toHaveBeenCalledTimes(1);
    // Format string is constant (semgrep unsafe-formatstring); the panel name
    // is passed as a %s argument — assert across the joined call args.
    expect(String(errSpy.mock.calls[0].join(' '))).toContain('active-issues');

    vi.advanceTimersByTime(ERROR_BACKOFF_MS + 1_000);
    await loadActiveIssues(); // fail #2 (same streak) → SILENT
    expect(errSpy).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(ERROR_BACKOFF_MS * 2 + 1_000);
    failing = false;
    await loadActiveIssues(); // success → streak resets
    expect(errSpy).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(PANEL_TTL_MS + 1_000);
    failing = true;
    await loadActiveIssues(); // NEW streak → logged again
    expect(errSpy).toHaveBeenCalledTimes(2);
  });
});

describe('first-paint stagger — the convoy does not hit the origin in one burst', () => {
  it('fires the lazy loaders in order at 250ms increments', async () => {
    const requestMock = okRequestStub();

    processDashboardData({ cards: [] });

    // PANEL_CALLS is declared in convoy fire order (stream.ts
    // LAZY_PANEL_LOADERS): loader i fires at i × 250ms, not before.
    for (let i = 0; i < PANEL_CALLS.length; i++) {
      await vi.advanceTimersByTimeAsync(i === 0 ? 0 : PANEL_STAGGER_MS);
      await flush();
      for (let j = 0; j < PANEL_CALLS.length; j++) {
        const m = PANEL_CALLS[j];
        expect(callsTo(requestMock, m), 'step ' + i + ': ' + m.url + (m.body || '')).toBe(
          j <= i ? 1 : 0,
        );
      }
    }
  });
});
