// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the Operations Summary on a first Dashboard load whose stream
 * errors before the first snapshot. A single early stream error is often
 * transient (a cold first snapshot; EventSource retries on its own), so the
 * summary keeps "Loading operations summary", fires ONE GET /api/dashboard
 * fallback, and shows "Unable to load the operations summary." only when no
 * snapshot has arrived by the end of the grace window or the stream errors a
 * second time in a row. A snapshot (stream or GET) inside the window means
 * the error text never renders. The Retry button stays the immediate path. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { requestJson } from '../api/client';
import { _resetLineStateLoader } from '../views/dashboard/lineState';
import { OpsSummaryContent } from '../views/dashboard/OpsSummary';
import { _resetPanelLoaders } from '../views/dashboard/Panels';
import { _resetSectionsLoader } from '../views/dashboard/sections';
import { dashboardStore, upsertCards } from '../views/dashboard/store';
import {
  _resetPanelStagger,
  _resetSnapshotGrace,
  connectDashboardSSE,
  disconnectDashboardSSE,
  SNAPSHOT_GRACE_MS,
} from '../views/dashboard/stream';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

// The periodic systems refresh is not under test here.
vi.mock('../views/dashboard/systems', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../views/dashboard/systems')>();
  return { ...actual, loadSystems: vi.fn(async () => {}) };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const requestJsonMock = vi.mocked(requestJson);

const ERROR_TEXT = 'Unable to load the operations summary.';
const LOADING_TEXT = 'Loading operations summary';

/** Minimal EventSource stand-in: records instances, lets a test fire a named
 * event or the error handler the way the browser would. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onerror: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  closed = false;
  private listeners: Record<string, Array<(e: MessageEvent) => void>> = {};

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, fn: (e: MessageEvent) => void): void {
    (this.listeners[type] ||= []).push(fn);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: unknown): void {
    const ev = { data: JSON.stringify(data) } as MessageEvent;
    for (const fn of this.listeners[type] || []) fn(ev);
  }

  fail(): void {
    this.onerror?.(new Event('error'));
  }
}

let container: HTMLDivElement;
let root: Root;
/** Every snapshotFailed value the store took, so "never rendered" is checked
 * across the whole run, not only at the assertion points. */
let failedSeen: boolean[];
let unsubscribe: () => void;

function dashboardCalls(): number {
  return requestJsonMock.mock.calls.filter((c) => String(c[0]) === '/api/dashboard').length;
}

/** requestJson stub: GET /api/dashboard per `dashboard`, every lazy panel
 * endpoint answers an empty body. */
function stubRequests(dashboard: 'fail' | 'hang'): void {
  requestJsonMock.mockImplementation(async (url: string) => {
    if (String(url) === '/api/dashboard') {
      if (dashboard === 'fail') throw new Error('upstream not ready');
      return new Promise(() => {});
    }
    return {};
  });
}

function stream(): FakeEventSource {
  const es = FakeEventSource.instances[FakeEventSource.instances.length - 1];
  if (!es) throw new Error('no EventSource opened');
  return es;
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  requestJsonMock.mockReset();
  _resetPanelLoaders();
  _resetLineStateLoader();
  _resetSectionsLoader();
  _resetPanelStagger();
  _resetSnapshotGrace();
  upsertCards([]);
  dashboardStore.update((s) => ({ ...s, dashboardData: null, snapshotFailed: false }));
  failedSeen = [];
  unsubscribe = dashboardStore.subscribe(() => failedSeen.push(dashboardStore.get().snapshotFailed));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<OpsSummaryContent />));
});

afterEach(() => {
  unsubscribe();
  disconnectDashboardSSE();
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Ops Summary — a stream error before the first snapshot', () => {
  it('a snapshot inside the grace window means the error text never renders', async () => {
    stubRequests('fail');
    connectDashboardSSE();

    act(() => stream().fail());
    await advance(0);
    expect(container.textContent).toContain(LOADING_TEXT);
    expect(container.textContent).not.toContain(ERROR_TEXT);
    // One GET fallback fires at once (it fails here, which alone shows nothing).
    expect(dashboardCalls()).toBe(1);

    await advance(SNAPSHOT_GRACE_MS / 2);
    expect(container.textContent).toContain(LOADING_TEXT);

    act(() => stream().emit('dashboard', { cards: [] }));
    await advance(SNAPSHOT_GRACE_MS);

    expect(dashboardStore.get().dashboardData).not.toBeNull();
    expect(container.textContent).not.toContain(ERROR_TEXT);
    expect(failedSeen).not.toContain(true);
  });

  it('no snapshot by the end of the window and a failing GET shows the error with Retry', async () => {
    stubRequests('fail');
    connectDashboardSSE();

    act(() => stream().fail());
    await advance(SNAPSHOT_GRACE_MS - 1_000);
    expect(container.textContent).toContain(LOADING_TEXT);
    expect(container.textContent).not.toContain(ERROR_TEXT);

    await advance(1_000);
    expect(container.textContent).toContain(ERROR_TEXT);
    const retry = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Retry'),
    );
    expect(retry).toBeTruthy();
    // Only the one fallback GET fired during the window.
    expect(dashboardCalls()).toBe(1);

    // Retry is the immediate path: it GETs again right away.
    await act(async () => {
      retry!.click();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(dashboardCalls()).toBe(2);
  });

  it('a second consecutive stream error with no snapshot shows the error at once', async () => {
    stubRequests('hang');
    connectDashboardSSE();

    act(() => stream().fail());
    await advance(3_000);
    expect(container.textContent).not.toContain(ERROR_TEXT);

    act(() => stream().fail());
    await advance(0);
    expect(container.textContent).toContain(ERROR_TEXT);
    // The second error fired no extra GET.
    expect(dashboardCalls()).toBe(1);
  });

  it('the GET fallback succeeding inside the window fills the summary, no error', async () => {
    requestJsonMock.mockImplementation(async () => ({ cards: [] }));
    connectDashboardSSE();

    act(() => stream().fail());
    await advance(SNAPSHOT_GRACE_MS + 1_000);

    expect(dashboardStore.get().dashboardData).not.toBeNull();
    expect(container.textContent).not.toContain(ERROR_TEXT);
    expect(failedSeen).not.toContain(true);
  });

  it('once a snapshot exists a stream error is a silent reconnect (no GET, no error)', async () => {
    stubRequests('fail');
    connectDashboardSSE();
    act(() => stream().emit('dashboard', { cards: [] }));
    await advance(0);
    const before = dashboardCalls();

    act(() => stream().fail());
    act(() => stream().fail());
    await advance(SNAPSHOT_GRACE_MS + 1_000);

    expect(dashboardCalls()).toBe(before);
    expect(failedSeen).not.toContain(true);
  });
});

describe('Ops Summary grace window — scoped to one stream connection', () => {
  it('a closed source erroring after unmount starts nothing', async () => {
    stubRequests('fail');
    connectDashboardSSE();
    const old = stream();
    disconnectDashboardSSE();

    act(() => old.fail());
    await advance(SNAPSHOT_GRACE_MS + 1_000);

    expect(dashboardCalls()).toBe(0);
    expect(failedSeen).not.toContain(true);
  });

  it('an old source erroring after a reconnect is not counted against the new connection', async () => {
    stubRequests('hang');
    connectDashboardSSE();
    const old = stream();
    connectDashboardSSE();
    const current = stream();
    expect(current).not.toBe(old);

    act(() => old.fail());
    act(() => current.fail());
    await advance(SNAPSHOT_GRACE_MS - 1_000);

    // The current source's first error opened its own window: no error yet,
    // and only that one error fired a GET.
    expect(container.textContent).toContain(LOADING_TEXT);
    expect(container.textContent).not.toContain(ERROR_TEXT);
    expect(dashboardCalls()).toBe(1);

    await advance(1_000);
    expect(container.textContent).toContain(ERROR_TEXT);
  });

  it('an old connection\'s GET resolving late does not close the new connection\'s window', async () => {
    const pending: Array<{ resolve: (v: unknown) => void; signal?: AbortSignal | null }> = [];
    requestJsonMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url) !== '/api/dashboard') return Promise.resolve({});
      return new Promise((resolve) => {
        pending.push({ resolve, signal: init?.signal });
      });
    });
    connectDashboardSSE();
    act(() => stream().fail());
    expect(pending).toHaveLength(1);

    connectDashboardSSE(); // e.g. a plant tab click
    act(() => stream().fail());
    expect(pending).toHaveLength(2);
    // The old connection's GET was abandoned when it was replaced.
    expect(pending[0].signal?.aborted).toBe(true);

    await act(async () => {
      pending[0].resolve({ cards: [] });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(dashboardStore.get().dashboardData).toBeNull();

    await advance(SNAPSHOT_GRACE_MS);
    expect(container.textContent).toContain(ERROR_TEXT);
  });
});
