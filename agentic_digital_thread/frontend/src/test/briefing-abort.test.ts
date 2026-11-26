// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* An in-flight auto-briefing must be ABORTABLE (P0-A).
 *
 * Incident evidence (2026-08-24 Management full-e2e): a single app task
 * (cpu=512 / memory=1024, one Uvicorn process) pinned ~99.9-100% CPU for ~45
 * minutes with ALB averages of 3-6 s. `stopBriefingLoop` cleared its three
 * timers but had no hold on the STREAM: a briefing whose /api/ask turn was
 * already streaming kept the orchestrator turn (and the server-side producer
 * thread) alive after the dashboard went away, and its failure path armed a
 * fresh 30 s retry that re-entered the whole generation.
 *
 * Contract pinned here:
 * - a briefing passes an AbortSignal to the shared SSE reader;
 * - `stopBriefingLoop` aborts that signal;
 * - an AbortError is EXPECTED CANCELLATION: no retry timer, no 'unavailable'
 *   phase, and the single-flight guard is released;
 * - a genuine (non-abort) failure keeps the existing 30 s retry;
 * - an explicit user refresh (force) still works after a stop. */
// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import {
  _resetBriefingCache,
  requestOpsBriefing,
  startBriefingLoop,
  stopBriefingLoop,
} from '../views/dashboard/briefing';
import { dashboardStore, patchBriefing } from '../views/dashboard/store';

vi.mock('../api/sse', () => ({
  postSse: vi.fn(),
  openEventSource: vi.fn(),
}));

import { postSse } from '../api/sse';

const postSseMock = vi.mocked(postSse);

const BRIEFING_TEXT =
  '- Line status: All lines running.\n' +
  '- Quality: Pass rate steady.\n' +
  '- Maintenance: One work order overdue.\n' +
  '- Order risk: One order at risk.\n' +
  '- Action: Inspect the line today.';

function abortError(): Error {
  const err = new Error('The operation was aborted.');
  err.name = 'AbortError';
  return err;
}

beforeEach(() => {
  vi.useFakeTimers();
  postSseMock.mockReset();
  _resetBriefingCache();
  patchBriefing({ phase: 'init', bullets: [], blockMd: '', analyzing: false, timestamp: '' });
  (window as unknown as { isStreaming?: boolean }).isStreaming = false;
  vi.stubGlobal('localStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
});

afterEach(() => {
  stopBriefingLoop();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

  it('passes an AbortSignal to the SSE reader', async () => {
    postSseMock.mockImplementation(async (_url, _body, onEvent) => {
      onEvent('done', { text: BRIEFING_TEXT });
    });

    await requestOpsBriefing();

    const options = postSseMock.mock.calls[0][3];
    expect(options?.signal).toBeInstanceOf(AbortSignal);
    expect(options?.signal?.aborted).toBe(false);
  });

  it('stopBriefingLoop aborts the in-flight signal', async () => {
    let seen: AbortSignal | undefined;
    let release: (() => void) | undefined;
    postSseMock.mockImplementation(async (_url, _body, _onEvent, options) => {
      seen = options?.signal;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
    });

    const inFlight = requestOpsBriefing();
    await Promise.resolve();
    expect(seen).toBeDefined();
    expect(seen?.aborted).toBe(false);

    stopBriefingLoop();

    expect(seen?.aborted).toBe(true);
    release?.();
    await inFlight;
  });

  it('an AbortError schedules NO retry and is not an error state', async () => {
    postSseMock.mockRejectedValue(abortError());

    await requestOpsBriefing();

    // No retry timer was armed by the cancellation.
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(postSseMock).toHaveBeenCalledTimes(1);
    expect(dashboardStore.get().briefing.phase).not.toBe('unavailable');
    expect(dashboardStore.get().briefing.analyzing).toBe(false);
  });

  it('a genuine failure still arms the 30s retry', async () => {
    postSseMock.mockRejectedValue(new Error('IncompleteRead'));

    await requestOpsBriefing();
    expect(postSseMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(30_000);

    expect(postSseMock.mock.calls.length).toBeGreaterThan(1);
  });

  it('a second retry REPLACES the first — stop leaves no stale timer', async () => {
    postSseMock.mockRejectedValue(new Error('IncompleteRead'));

    // Two genuine failures inside the 30s window. The retry watchdog is a
    // SINGLE-OWNER module slot: the second must replace the first, not accrete
    // an untracked timer that `stopBriefingLoop` can no longer clear.
    await requestOpsBriefing();
    expect(vi.getTimerCount()).toBe(1);
    await requestOpsBriefing();
    expect(vi.getTimerCount()).toBe(1);

    stopBriefingLoop();
    expect(vi.getTimerCount()).toBe(0);

    // An abandoned dashboard must not pay for a third expensive generation.
    await vi.advanceTimersByTimeAsync(120_000);
    expect(postSseMock).toHaveBeenCalledTimes(2);
  });

  it('a stop after an abort releases the single-flight guard', async () => {
    postSseMock.mockRejectedValueOnce(abortError());
    await requestOpsBriefing();

    postSseMock.mockImplementation(async (_url, _body, onEvent) => {
      onEvent('done', { text: BRIEFING_TEXT });
    });
    await requestOpsBriefing({ force: true });

    expect(postSseMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().briefing.phase).toBe('bullets');
  });

  it('stopBriefingLoop after startBriefingLoop cancels the boot generation', async () => {
    postSseMock.mockImplementation(async (_url, _body, onEvent) => {
      onEvent('done', { text: BRIEFING_TEXT });
    });

    startBriefingLoop();
    stopBriefingLoop();
    await vi.advanceTimersByTimeAsync(10_000);

    expect(postSseMock).not.toHaveBeenCalled();
  });

// --------------------------------------------------------------------------
// Hidden tab (workshop event 24): an abandoned dashboard tab kept a full
// orchestrator turn running every 5 to 10 minutes for hours. Interval ticks on
// a hidden tab are skipped; a forced refresh still runs; the tab catches up
// once when it becomes visible again.
// --------------------------------------------------------------------------
function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

it('skips an automatic briefing while the tab is hidden', async () => {
  postSseMock.mockImplementation(async (_url, _body, onEvent) => {
    onEvent('done', { text: BRIEFING_TEXT });
  });
  setVisibility('hidden');
  try {
    await requestOpsBriefing();
    expect(postSseMock).not.toHaveBeenCalled();
    await requestOpsBriefing({ force: true });
    expect(postSseMock).toHaveBeenCalledTimes(1);
  } finally {
    setVisibility('visible');
  }
});

it('a hidden tab catches up once when it becomes visible again', async () => {
  postSseMock.mockImplementation(async (_url, _body, onEvent) => {
    onEvent('done', { text: BRIEFING_TEXT });
  });
  setVisibility('hidden');
  try {
    startBriefingLoop();
    await vi.advanceTimersByTimeAsync(4000); // boot tick: hidden, skipped
    await vi.advanceTimersByTimeAsync(5 * 60 * 1000); // interval tick: skipped
    expect(postSseMock).not.toHaveBeenCalled();
    setVisibility('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(postSseMock).toHaveBeenCalledTimes(1);
  } finally {
    setVisibility('visible');
    stopBriefingLoop();
  }
});
