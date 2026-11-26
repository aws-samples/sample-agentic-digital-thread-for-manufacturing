// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Task 2.3 (E1, ADR-0053) — the onboarding wizard's preflight stage: the
 * verdict gate (blocked never auto-starts discover; degraded proceeds only on
 * explicit confirm; ready auto-continues), the poll lifecycle (stops on
 * done/error), the honest job.error surface (wave-1 contract amendment), and
 * the <PreflightBody/> card (blocked banner naming the reason enum + docs
 * reference, per-table list collapsed beyond 8, completeness, warnings
 * verbatim). Integration-style over the REAL flow store — generic fixture
 * data only, no dataset literals. */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CatalogSystem, PreflightResult } from '../api/types/onboard';
import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  appendCheckpoint,
  confirmCheckpoint,
  flowStore,
  resetToWelcome,
  startWizard,
  type FlowEntry,
} from '../views/onboard/flow-store';
import { ob } from '../views/onboard/state';
import { obStart } from '../views/onboard/wizard';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const POLL_MS = 3000;

const system: CatalogSystem = {
  id: 'sys-a',
  name: 'System A',
  connection_type: 'PostgreSQL',
  connection_string: 'postgresql://example.test/db',
};

// ── Fixtures (generic; the pinned Task 1.2 result contract) ──────────────────

function readyResult(): PreflightResult {
  return {
    system_id: 'sys-a',
    verdict: 'ready',
    connection: { status: 'pass', latency_ms: 12 },
    tables: [{ name: 'records_a', sample_read: 'ok', detail: '' }],
    completeness: {
      tables_visible: 1,
      tables_sample_read: 1,
      columns_discovered: 4,
      physical_columns: 'n/a',
    },
    warnings: [],
    elapsed_seconds: 1.2,
  };
}

const BLOCKED_DETAIL =
  'The app could not assume the configured cross-account role. Verify the ' +
  'role trust policy and grant list. See docs/cross-account-onboarding.md';

function blockedResult(): PreflightResult {
  return {
    system_id: 'sys-a',
    verdict: 'blocked',
    connection: {
      status: 'fail',
      reason: 'assume_role_denied',
      detail: BLOCKED_DETAIL,
    },
    tables: [],
    completeness: {
      tables_visible: 0,
      tables_sample_read: 0,
      columns_discovered: 0,
      physical_columns: 'n/a',
    },
    warnings: [],
    elapsed_seconds: 0.4,
  };
}

const DEGRADED_WARNING =
  'Sample read failed for table_b (PermissionError) — rows from this table ' +
  'may be unavailable to the agent.';

function degradedResult(): PreflightResult {
  return {
    system_id: 'sys-a',
    verdict: 'degraded',
    connection: { status: 'pass', latency_ms: 20 },
    tables: [
      { name: 'table_a', sample_read: 'ok', detail: '' },
      { name: 'table_b', sample_read: 'failed', detail: 'PermissionError' },
    ],
    completeness: {
      tables_visible: 2,
      tables_sample_read: 1,
      columns_discovered: 9,
      physical_columns: 'skipped:AwsSessionError',
    },
    warnings: [DEGRADED_WARNING],
    elapsed_seconds: 8.5,
  };
}

// ── Harness ──────────────────────────────────────────────────────────────────

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

type PreflightRunSpec = PreflightResult | { jobError: string };

/** Fetch router: preflight POST N returns job pf-N; its GET serves the Nth
 * spec (a done result, or an error job whose honest line rides `error`).
 * The pipeline past preflight parks at the schema checkpoint. */
function installFetch(preflight: PreflightRunSpec[]) {
  let preflightRun = 0;
  const counts = new Map<string, number>();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    counts.set(url, (counts.get(url) || 0) + 1);
    if (url === '/api/onboard/preflight' && init?.method === 'POST') {
      preflightRun += 1;
      return jsonResponse({ job_id: 'pf-' + preflightRun });
    }
    const poll = url.match(/^\/api\/onboard\/preflight\/pf-(\d+)$/);
    if (poll) {
      const spec = preflight[Number(poll[1]) - 1];
      if (spec && 'jobError' in spec) {
        // Wave-1 amendment: the honest sanitized line IS job.error
        // (error_detail also present on the wire).
        return jsonResponse({ status: 'error', error: spec.jobError, error_detail: spec.jobError });
      }
      return jsonResponse({ status: 'done', result: spec });
    }
    if (url === '/api/onboard/discover') {
      return jsonResponse({ tables: [{ name: 'records_a', row_count: 1 }] });
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, calls: (url: string) => counts.get(url) || 0 };
}

type CheckpointEntry = Extract<FlowEntry, { kind: 'checkpoint' }>;

function findCheckpoint(titlePart: string): CheckpointEntry | undefined {
  return flowStore
    .get()
    .entries.find(
      (e): e is CheckpointEntry => e.kind === 'checkpoint' && e.title.includes(titlePart),
    );
}

/** Mount <ObFlow/> against the live store, assert, unmount (keeps React
 * subscriptions out of the wizard's store writes). */
function withRenderedFlow(assertFn: (el: HTMLElement) => void): void {
  const el = document.createElement('div');
  document.body.appendChild(el);
  const root = createRoot(el);
  act(() => root.render(<ObFlow />));
  try {
    assertFn(el);
  } finally {
    act(() => root.unmount());
    el.remove();
  }
}

async function finish(run: Promise<void>): Promise<void> {
  ob.abort?.abort();
  // jsdom's DOMException is cross-realm (not `instanceof Error` in module
  // scope), so obStart's AbortError swallow re-throws here — absorb it.
  await run.catch((e: unknown) => {
    if ((e as { name?: string })?.name !== 'AbortError') throw e;
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  resetToWelcome();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
});

afterEach(() => {
  ob.abort?.abort();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  resetToWelcome();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ── Verdict gate ─────────────────────────────────────────────────────────────

describe('preflight verdict gate (obStart step 0)', () => {
  it('test_blocked_prevents_discover_and_offers_retry', async () => {
    const { calls } = installFetch([blockedResult(), readyResult()]);

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(POLL_MS);

    const cp = findCheckpoint('Preflight Blocked');
    expect(cp).toBeDefined();
    expect(cp!.confirmLabel).toContain('Fix connection & retry');
    expect(calls('/api/onboard/discover')).toBe(0);

    // The prominent honest banner names the reason ENUM + the docs reference.
    withRenderedFlow((el) => {
      const banner = el.querySelector('.preflight-blocked-banner');
      expect(banner).not.toBeNull();
      expect(banner!.textContent).toContain('assume_role_denied');
      expect(banner!.textContent).toContain('docs/cross-account-onboarding.md');
      expect(el.querySelector('.preflight-verdict')?.getAttribute('data-verdict')).toBe(
        'blocked',
      );
    });

    // "Fix connection & retry" re-runs preflight; the fixed run is ready.
    confirmCheckpoint(cp!.id);
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(calls('/api/onboard/preflight')).toBe(2);
    expect(calls('/api/onboard/discover')).toBe(1);
    await finish(run);
  });

  it('test_degraded_warns_then_proceeds', async () => {
    const { calls } = installFetch([degradedResult()]);

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(POLL_MS);

    const cp = findCheckpoint('Preflight Warnings');
    expect(cp).toBeDefined();
    expect(cp!.confirmLabel).toContain('Proceed with warnings');
    expect(calls('/api/onboard/discover')).toBe(0);

    // Warning + per-check detail rows render verbatim before the gate.
    withRenderedFlow((el) => {
      expect(el.querySelector('.preflight-verdict')?.getAttribute('data-verdict')).toBe(
        'degraded',
      );
      expect(el.textContent).toContain(DEGRADED_WARNING);
      expect(el.textContent).toContain('physical columns: skipped:AwsSessionError');
      expect(el.textContent).toContain('2 tables visible · 1 sample-read · 9 columns discovered');
      const failed = el.querySelector('.preflight-table-row[data-sample-read="failed"]');
      expect(failed).not.toBeNull();
      expect(failed!.textContent).toContain('table_b');
      expect(failed!.textContent).toContain('PermissionError');
      expect(el.querySelector('.preflight-blocked-banner')).toBeNull();
    });

    confirmCheckpoint(cp!.id);
    await vi.advanceTimersByTimeAsync(0);

    expect(calls('/api/onboard/discover')).toBe(1);
    await finish(run);
  });

  it('test_ready_autoproceeds', async () => {
    const { calls } = installFetch([readyResult()]);

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(POLL_MS);

    // Discover started with NO manual confirm; the card renders confirmed.
    expect(calls('/api/onboard/discover')).toBe(1);
    const cp = findCheckpoint('Preflight Passed');
    expect(cp).toBeDefined();
    expect(cp!.confirmed).toBe(true);
    withRenderedFlow((el) => {
      expect(el.querySelector('.preflight-verdict')?.getAttribute('data-verdict')).toBe(
        'ready',
      );
    });
    await finish(run);
  });
});

// ── Poll lifecycle + honest error surface ────────────────────────────────────

describe('preflight poll lifecycle', () => {
  it('test_poll_stops_on_done_and_error', async () => {
    // done: exactly one status GET, then no further preflight polls.
    const done = installFetch([readyResult()]);
    const doneRun = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(done.calls('/api/onboard/preflight/pf-1')).toBe(1);
    await vi.advanceTimersByTimeAsync(10 * POLL_MS);
    expect(done.calls('/api/onboard/preflight/pf-1')).toBe(1);
    await finish(doneRun);
    resetToWelcome();

    // error: the poll loop stops on the error job too.
    const errored = installFetch([{ jobError: 'preflight timed out' }]);
    const errorRun = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(errored.calls('/api/onboard/preflight/pf-1')).toBe(1);
    await vi.advanceTimersByTimeAsync(10 * POLL_MS);
    expect(errored.calls('/api/onboard/preflight/pf-1')).toBe(1);
    expect(errored.calls('/api/onboard/discover')).toBe(0);
    await finish(errorRun);
  });

  it('test_error_job_renders_honest_error', async () => {
    const honest =
      'AssumeRole failed for the configured cross-account role — see ' +
      'docs/cross-account-onboarding.md';
    const { calls } = installFetch([{ jobError: honest }]);

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(POLL_MS);

    // The retry card carries job.error VERBATIM (never a generic message).
    const entry = flowStore
      .get()
      .entries.find((e): e is Extract<FlowEntry, { kind: 'error' }> => e.kind === 'error');
    expect(entry).toBeDefined();
    expect(entry!.text).toBe(honest);
    expect(typeof entry!.retry).toBe('function');
    expect(calls('/api/onboard/discover')).toBe(0);
    await finish(run);
  });
});

// ── Card rendering details ───────────────────────────────────────────────────

describe('preflight card rendering', () => {
  it('collapses the per-table sample-read list beyond 8 rows', () => {
    startWizard();
    const tables = Array.from({ length: 12 }, (_, i) => ({
      name: 'table_' + (i + 1),
      sample_read: 'ok' as const,
      detail: '',
    }));
    appendCheckpoint({
      title: 'CHECKPOINT — Preflight Passed',
      content: { kind: 'preflight', result: { ...readyResult(), tables } },
      confirmLabel: 'Continue →',
      stepContext: { step: 'preflight', systemId: 'sys-a', data: {}, history: [] },
      onConfirm: () => {},
    });

    withRenderedFlow((el) => {
      // All 12 rows exist; rows beyond 8 sit inside the <details> fold.
      expect(el.querySelectorAll('.preflight-table-row')).toHaveLength(12);
      const fold = el.querySelector('details.preflight-tables-fold');
      expect(fold).not.toBeNull();
      expect(fold!.querySelector('summary')!.textContent).toContain('4 more tables');
      expect(fold!.querySelectorAll('.preflight-table-row')).toHaveLength(4);
    });
  });
});
