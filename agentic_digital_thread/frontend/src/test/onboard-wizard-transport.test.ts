// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Task 1.3 transport contract for the deterministic onboarding wizard. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => {
  const ob = {
    active: null as string | null,
    abort: null as AbortController | null,
    currentStep: null as {
      step: string;
      systemId: string;
      data: Record<string, unknown>;
      history: unknown[];
    } | null,
    isAdmin: false,
    adminChecked: false,
    waitingOnRetry: false,
    provisioned: false,
  };
  return {
    ob,
    autoRerun: false,
    heldCheckpointTitle: null as string | null,
    checkpoints: [] as Array<{ title: string; onConfirm: (id: number) => void; onRerun?: (id: number) => Promise<void> }>,
    setBusy: vi.fn(),
    markOnboarded: vi.fn(),
    obLoadCatalog: vi.fn(async () => {}),
    obSystemIsOnboarded: vi.fn(async (_sysId: string, _signal?: AbortSignal) => false),
    refreshSuggestions: vi.fn(async () => {}),
    invalidateOnboardingComplete: vi.fn(async () => {}),
    obAppendCheckpoint: vi.fn(),
    obAppendLoading: vi.fn(() => 1),
    obAppendMsg: vi.fn(),
    obCollapseStep: vi.fn(),
    obRemoveLoading: vi.fn(),
    obUpdateLoading: vi.fn(),
    appendConnections: vi.fn(),
    appendCta: vi.fn(),
    appendDiscovery: vi.fn(),
    // Pre-map probe entry (T-H): continue immediately — the transport contract
    // under test is the discover→map→resolve→provision→verify request path,
    // and the probe is skippable/failure-open by design.
    appendProbe: vi.fn(
      (_systemId: string, _tables: unknown[], onContinue: () => void) => {
        onContinue();
        return 99;
      },
    ),
    patchProgress: vi.fn(),
    removeEntry: vi.fn(),
    setCheckpointContent: vi.fn(),
    showReconcileBanner: vi.fn(),
    startWizard: vi.fn(),
    appendRetryError: vi.fn(() => new Promise<void>(() => {})),
  };
});

vi.mock('../views/onboard/catalog-store', () => ({
  getConnString: () => 'postgresql://example.test/db',
  markOnboarded: h.markOnboarded,
  setBusy: h.setBusy,
}));

vi.mock('../views/onboard/catalog', () => ({
  obLoadCatalog: h.obLoadCatalog,
  obSystemIsOnboarded: h.obSystemIsOnboarded,
}));

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(() => ({ id: 'test-query-client' })),
  invalidateOnboardingComplete: h.invalidateOnboardingComplete,
}));

vi.mock('../views/onboard/chat', () => ({ refreshSuggestions: h.refreshSuggestions }));

vi.mock('../views/onboard/flow', () => ({
  obAppendCheckpoint: h.obAppendCheckpoint,
  obAppendLoading: h.obAppendLoading,
  obAppendMsg: h.obAppendMsg,
  obCollapseStep: h.obCollapseStep,
  obRemoveLoading: h.obRemoveLoading,
  obUpdateLoading: h.obUpdateLoading,
}));

vi.mock('../views/onboard/flow-store', () => ({
  appendConnections: h.appendConnections,
  appendCta: h.appendCta,
  appendDiscovery: h.appendDiscovery,
  appendProbe: h.appendProbe,
  appendRetryError: h.appendRetryError,
  confirmCheckpoint: vi.fn(),
  getEntry: vi.fn(() => undefined),
  patchProgress: h.patchProgress,
  removeEntry: h.removeEntry,
  setCheckpointContent: h.setCheckpointContent,
  showReconcileBanner: h.showReconcileBanner,
  startWizard: h.startWizard,
}));

vi.mock('../views/onboard/render', () => ({ basisFor: vi.fn(() => undefined) }));
vi.mock('../views/onboard/state', () => ({
  ob: h.ob,
  getObConcepts: vi.fn(() => ['unknown']),
  setObConcepts: vi.fn(),
}));

import type { CatalogSystem } from '../api/types/onboard';
import {
  delayWithAbort,
  obStart,
  provisionErrorText,
  provisionLoadingLabel,
  RESOLVE_DEADLINE_MS,
} from '../views/onboard/wizard';

const system: CatalogSystem = {
  id: 'cmms',
  name: 'CMMS',
  connection_type: 'PostgreSQL',
};

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn(async () => body),
  } as unknown as Response;
}

function abortablePending(signal: AbortSignal | null | undefined): Promise<Response> {
  return new Promise<Response>((_resolve, reject) => {
    const rejectAbort = (): void => {
      reject(new DOMException('The operation was aborted', 'AbortError'));
    };
    if (signal?.aborted) rejectAbort();
    else signal?.addEventListener('abort', rejectAbort, { once: true });
  });
}

type FetchMode =
  | 'success'
  | 'initial-resolve-pending'
  | 'rerun-resolve-pending'
  | 'provision-probe'
  | 'provision-busy'
  | 'provision-start-busy'
  | 'provision-error'
  | 'withheld-rules'
  | 'verify-error'
  | 'preflight-blocked';

/** The 409 detail when another provision of the system runs (agents/onboard._PROVISION_BUSY_DETAIL). */
const PROVISION_RUNNING_DETAIL =
  'This system is already being provisioned (from the Onboard view or the onboarding chat), so nothing was started. Retry when that provision has finished.';

function conflict(detail: string): Response {
  return {
    ok: false,
    status: 409,
    statusText: 'Conflict',
    text: vi.fn(async () => JSON.stringify({ detail })),
  } as unknown as Response;
}

/** The server's retryable busy message (agents/onboard._CONNECTION_BUSY_ERROR). */
const PROVISION_BUSY_DETAIL =
  "Another change to this system's connection is in progress, so it was not provisioned. Retry in a moment.";

// Generic C5 vocab_fit fixtures (no dataset literals) — the map status carries
// the initial block; the re-run map response carries a distinguishable one.
const MAP_VOCAB_FIT = {
  total_columns: 10,
  rows_emitted: 10,
  mapped_confident: 7,
  needs_review: 3,
  reconciled: 1,
  distinct_concepts: 5,
  vocabulary_size: 40,
  coverage_pct: 70,
};
const RERUN_VOCAB_FIT = {
  ...MAP_VOCAB_FIT,
  mapped_confident: 8,
  needs_review: 2,
  coverage_pct: 80,
};

function installPipelineFetch(mode: FetchMode = 'success'): ReturnType<typeof vi.fn> {
  const counts = new Map<string, number>();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const count = (counts.get(url) || 0) + 1;
    counts.set(url, count);

    // Step 0 (E1): preflight job — a ready verdict lets the pipeline continue.
    if (url === '/api/onboard/preflight') {
      return response({ job_id: 'preflight-job' });
    }
    if (url === '/api/onboard/preflight/preflight-job') {
      return response({
        status: 'done',
        result: {
          system_id: 'cmms',
          verdict: mode === 'preflight-blocked' ? 'blocked' : 'ready',
          connection: { status: 'pass', latency_ms: 5 },
          tables: [{ name: 'work_orders', sample_read: 'ok', detail: '' }],
          completeness: {
            tables_visible: 1,
            tables_sample_read: 1,
            columns_discovered: 2,
            physical_columns: 'n/a',
          },
          warnings: [],
        },
      });
    }
    if (url === '/api/onboard/discover') {
      return response({ tables: [{ name: 'work_orders', row_count: 1 }] });
    }
    if (url === '/api/onboard/map') {
      // /map only starts a job (the server never returns mappings here); the
      // checkpoint Re-run gets its own job id and must poll it too.
      return response({ job_id: count === 1 ? 'map-job' : 'rerun-map-job', status: 'running' });
    }
    if (url === '/api/onboard/map/status/map-job') {
      return response({
        status: 'done',
        mappings: [{ field: 'work_orders.id', concept: 'work-order' }],
        vocab_fit: MAP_VOCAB_FIT,
      });
    }
    if (url === '/api/onboard/map/status/rerun-map-job') {
      return response({
        status: 'done',
        mappings: [{ field: 'work_orders.id', concept: 'work-order' }],
        vocab_fit: RERUN_VOCAB_FIT,
      });
    }
    if (url === '/api/onboard/resolve') {
      return response({ job_id: count === 1 ? 'resolve-job' : 'rerun-job' });
    }
    if (url === '/api/onboard/resolve/status/resolve-job') {
      if (mode === 'withheld-rules') {
        return response({
          status: 'done',
          rules: [],
          rejected_rules: [
            { source_field: 'a', rejected_reason: 'only 1 shared value(s) (need >= 3)' },
            { source_field: 'b', rejected_reason: 'only 2 shared value(s) (need >= 3)' },
          ],
        });
      }
      return response(
        mode === 'initial-resolve-pending'
          ? { status: 'running' }
          : { status: 'done', rules: [], resolve_job_id: 'resolve-job' },
      );
    }
    if (url === '/api/onboard/resolve/status/rerun-job') {
      return response(
        mode === 'rerun-resolve-pending'
          ? { status: 'running' }
          : { status: 'done', rules: [], resolve_job_id: 'rerun-job' },
      );
    }
    if (url === '/api/onboard/provision') {
      if (mode === 'provision-start-busy' && count === 1) return conflict(PROVISION_RUNNING_DETAIL);
      return response({ job_id: 'provision-job' });
    }
    if (url === '/api/onboard/provision/status/provision-job') {
      if (mode === 'provision-busy') {
        return response({
          status: 'error',
          error: 'provision_failed',
          reason: 'busy',
          detail: PROVISION_BUSY_DETAIL,
          retryable: true,
        });
      }
      if (mode === 'provision-error') {
        return response({ status: 'error', error: 'provision_failed' });
      }
      if (mode === 'withheld-rules') {
        return response({
          status: 'done',
          system_id: 'cmms',
          neptune_nodes_created: 5,
          identity_rules_created: 0,
          identity_rules_rejected: 0,
          mapped_fields: 2,
          join_paths: [],
          value_mappings: [],
          tool_name: 'query_cmms',
        });
      }
      return response(
        mode === 'provision-probe'
          ? { status: 'running', error: 'Unknown job_id: provision-job' }
          : {
              status: 'done',
              neptune_nodes_created: 1,
              identity_rules_created: 1,
              tool_name: 'query_cmms',
              provision_run_id: 'run-abc123',
            },
      );
    }
    if (url === '/api/onboard/reconcile') return response({ suggestions: [] });
    if (url === '/api/onboard/verify') {
      if (mode === 'verify-error') return response({ error: 'verification query failed' });
      return response({ system_id: 'cmms', tables_accessible: 1, total_rows: 1 });
    }
    if (url === '/api/onboard/verify/agent-test') {
      return response({ job_id: 'agent-test-job', status: 'running', deadline_s: 180 });
    }
    if (url === '/api/onboard/verify/agent-test/agent-test-job') {
      return response({
        status: 'done',
        result: {
          outcome: 'passed',
          step: null,
          tool: 'cmms___query_cmms',
          table: 'work_orders',
          rows: 3,
          elapsed_s: 2,
        },
      });
    }
    throw new Error(`Unexpected request: ${url} (${String(init?.method || 'GET')})`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function advanceUntilSettled<T>(promise: Promise<T>, ms: number): Promise<T> {
  await vi.advanceTimersByTimeAsync(ms);
  return promise;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(0));
  vi.clearAllMocks();
  h.obSystemIsOnboarded.mockResolvedValue(false);
  h.ob.active = null;
  h.ob.abort = null;
  h.ob.currentStep = null;
  h.ob.waitingOnRetry = false;
  h.ob.provisioned = false;
  h.autoRerun = false;
  h.heldCheckpointTitle = null;
  h.checkpoints.length = 0;
  h.appendRetryError.mockImplementation(() => new Promise<void>(() => {}));
  h.obAppendCheckpoint.mockImplementation((spec: {
    title: string;
    onConfirm: (id: number) => void;
    onRerun?: (id: number) => Promise<void>;
  }) => {
    const id = h.checkpoints.push(spec);
    void Promise.resolve().then(async () => {
      if (h.autoRerun && spec.onRerun) await spec.onRerun(id);
      if (!h.heldCheckpointTitle || !spec.title.includes(h.heldCheckpointTitle)) {
        spec.onConfirm(id);
      }
    });
    return id;
  });
});

afterEach(() => {
  h.ob.abort?.abort();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('wizard request cancellation', () => {
  it('forwards one active signal to every start, poll, reconcile, verify, and re-run request', async () => {
    h.autoRerun = true;
    const fetchMock = installPipelineFetch();

    const run = obStart({ ...system });
    const signal = h.ob.abort!.signal;
    await advanceUntilSettled(run, 60_000);

    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
    const scopedCalls = fetchMock.mock.calls.filter(([input]) =>
      /^\/api\/onboard\/(preflight|discover|map|resolve|provision|reconcile|verify)/.test(String(input)),
    );
    expect(scopedCalls.length).toBeGreaterThan(10);
    for (const [, init] of scopedCalls) {
      expect(init?.signal).toBe(signal);
    }
    expect(scopedCalls.filter(([input]) => String(input) === '/api/onboard/discover')).toHaveLength(2);
    expect(scopedCalls.filter(([input]) => String(input) === '/api/onboard/map')).toHaveLength(2);
    expect(scopedCalls.filter(([input]) => String(input) === '/api/onboard/resolve')).toHaveLength(2);
    expect(
      scopedCalls.filter(([input]) => String(input) === '/api/onboard/verify/agent-test'),
    ).toHaveLength(1);
  });

  it('sends back the id of the Resolve job whose rules the checkpoint showed', async () => {
    const fetchMock = installPipelineFetch();
    await advanceUntilSettled(obStart({ ...system }), 60_000);
    const start = fetchMock.mock.calls.find(([input]) => String(input) === '/api/onboard/provision');
    expect(JSON.parse(String(start?.[1]?.body)).resolve_job_id).toBe('resolve-job');
  });

  it('sends back the Re-run job id after a Re-run replaced the rules', async () => {
    h.autoRerun = true;
    const fetchMock = installPipelineFetch();
    await advanceUntilSettled(obStart({ ...system }), 60_000);
    const start = fetchMock.mock.calls.find(([input]) => String(input) === '/api/onboard/provision');
    expect(JSON.parse(String(start?.[1]?.body)).resolve_job_id).toBe('rerun-job');
  });

  it('runs the agent test after verification and shows its result', async () => {
    const fetchMock = installPipelineFetch();

    await advanceUntilSettled(obStart({ ...system }), 60_000);

    const start = fetchMock.mock.calls.find(
      ([input]) => String(input) === '/api/onboard/verify/agent-test',
    );
    expect(start).toBeDefined();
    expect(JSON.parse(String(start?.[1]?.body))).toEqual({ system_id: 'cmms' });
    expect(h.obAppendLoading).toHaveBeenCalledWith('Running agent test');
    expect(h.obAppendMsg).toHaveBeenCalledWith('**✓ Verification passed** — 1 table, 1 row accessible.');
    expect(h.obAppendMsg).toHaveBeenCalledWith(
      'Agent test passed: query_cmms returned 3 rows from work_orders in 2 s',
    );
    expect(
      h.obAppendMsg.mock.calls.some(([message]) => String(message).includes('Agent test: ')),
    ).toBe(false);
    // The test result is reported before onboarding is marked complete.
    const testMsgOrder = h.obAppendMsg.mock.invocationCallOrder.at(-1) ?? 0;
    expect(h.invalidateOnboardingComplete.mock.invocationCallOrder[0]).toBeGreaterThan(testMsgOrder);
  });

  it('asks the reconcile check about the system it just provisioned', async () => {
    const fetchMock = installPipelineFetch();

    await advanceUntilSettled(obStart({ ...system }), 60_000);

    const reconcile = fetchMock.mock.calls.filter(
      ([input]) => String(input) === '/api/onboard/reconcile',
    );
    expect(reconcile).toHaveLength(1);
    const init = reconcile[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      system_id: system.id,
      run_id: 'run-abc123',
    });
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json');
  });

  it('pauses on the pre-map probe with the discovered tables BEFORE any /map request', async () => {
    // Pins the T-H wiring itself: deleting the pause block in wizard.ts (the
    // integration, not the helper or the component) must fail THIS test —
    // every other suite exercises the pieces in isolation. Kill list:
    // remove `await runProbeStep(...)` → appendProbe never called;
    // move the pause after the map POST → mapCallsWhenProbed !== 0;
    // stop forwarding discover output → tables argument mismatch.
    const fetchMock = installPipelineFetch();
    let mapCallsWhenProbed = -1;
    h.appendProbe.mockImplementation(
      (_systemId: string, _tables: unknown[], onContinue: () => void) => {
        mapCallsWhenProbed = fetchMock.mock.calls.filter(
          ([input]) => String(input) === '/api/onboard/map',
        ).length;
        onContinue();
        return 99;
      },
    );

    await advanceUntilSettled(obStart({ ...system }), 60_000);

    expect(h.appendProbe).toHaveBeenCalledOnce();
    expect(h.appendProbe).toHaveBeenCalledWith(
      system.id,
      [{ name: 'work_orders', row_count: 1 }],
      expect.any(Function),
    );
    expect(mapCallsWhenProbed).toBe(0);
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input) === '/api/onboard/map').length,
    ).toBeGreaterThan(0);
  });

  it('rejects an aborted polling wait and clears its timer', async () => {
    const controller = new AbortController();
    const waiting = delayWithAbort(3000, controller.signal);

    controller.abort();

    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' });
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([
    ['Confirm Schema', 3_000],
    ['Confirm Mappings', 6_000],
    ['Identity Rules', 9_000],
  ])('cancels and clears busy while waiting at the %s checkpoint', async (title, elapsedMs) => {
    h.heldCheckpointTitle = title;
    installPipelineFetch();

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(elapsedMs);
    expect(h.checkpoints.some((checkpoint) => checkpoint.title.includes(title))).toBe(true);

    h.ob.abort!.abort();
    await run;

    expect(h.setBusy).toHaveBeenLastCalledWith('cmms', false);
    expect(h.appendRetryError).not.toHaveBeenCalled();
  });

  it('clears the captured system busy flag when onboarding fails', async () => {
    installPipelineFetch();
    h.invalidateOnboardingComplete.mockRejectedValueOnce(new Error('invalidation failed'));

    const run = obStart({ ...system });
    const rejected = expect(run).rejects.toThrow('invalidation failed');
    await vi.advanceTimersByTimeAsync(60_000);
    await rejected;

    expect(h.setBusy).toHaveBeenLastCalledWith('cmms', false);
  });

  it('cancels an in-flight start request without post-cancel writes or a retry card', async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      abortablePending(init?.signal),
    );
    vi.stubGlobal('fetch', fetchMock);
    const run = obStart({ ...system });
    await Promise.resolve();
    const writesAtCancel = h.startWizard.mock.calls.length + h.obAppendLoading.mock.calls.length;

    h.ob.abort!.abort();
    await run;
    await Promise.resolve();

    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
    expect(h.startWizard.mock.calls.length + h.obAppendLoading.mock.calls.length).toBe(writesAtCancel);
    expect(h.obRemoveLoading).not.toHaveBeenCalled();
    expect(h.appendRetryError).not.toHaveBeenCalled();
    expect(h.setCheckpointContent).not.toHaveBeenCalled();
  });

  it('cancels the polling interval before another status request or error card', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/onboard/preflight') return response({ job_id: 'preflight-job' });
      if (url === '/api/onboard/preflight/preflight-job') {
        return response({
          status: 'done',
          result: { verdict: 'ready', connection: { status: 'pass' }, tables: [] },
        });
      }
      if (url === '/api/onboard/discover') {
        return response({ tables: [{ name: 'work_orders' }] });
      }
      if (url === '/api/onboard/map') return response({ job_id: 'map-job' });
      throw new Error(`Unexpected request after cancel: ${url} (${String(init?.method || 'GET')})`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetchMock).toHaveBeenCalledWith('/api/onboard/map', expect.objectContaining({ signal: h.ob.abort!.signal }));

    h.ob.abort!.abort();
    await run;
    await vi.advanceTimersByTimeAsync(6000);

    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/map/status/'))).toBe(false);
    expect(h.appendRetryError).not.toHaveBeenCalled();
    expect(h.obUpdateLoading).not.toHaveBeenCalled();
  });

  it('cancels the provisioning fallback probe with the wizard signal', async () => {
    installPipelineFetch('provision-probe');
    h.obSystemIsOnboarded.mockImplementation(
      (_sysId: string, signal?: AbortSignal) =>
        new Promise<boolean>((_resolve, reject) => {
          const rejectAbort = () =>
            reject(new DOMException('The operation was aborted', 'AbortError'));
          if (signal?.aborted) rejectAbort();
          else signal?.addEventListener('abort', rejectAbort, { once: true });
        }),
    );

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(20_000);

    expect(h.obSystemIsOnboarded).toHaveBeenCalledWith('cmms', h.ob.abort!.signal);
    h.ob.abort!.abort();
    await run;

    expect(h.appendRetryError).not.toHaveBeenCalled();
  });

  it('reports unavailable statistics when the catalog confirms fallback completion', async () => {
    installPipelineFetch('provision-probe');
    h.obSystemIsOnboarded.mockResolvedValue(true);

    const run = obStart({ ...system });
    await advanceUntilSettled(run, 60_000);

    expect(h.obSystemIsOnboarded).toHaveBeenCalledWith('cmms', expect.any(AbortSignal));
    expect(h.obAppendMsg).toHaveBeenCalledWith(
      '✓ **Provisioned.** Completion confirmed, but statistics could not be recovered. Tool `query_cmms` ready.',
    );
    expect(
      h.obAppendMsg.mock.calls.some(([message]) => String(message).includes('0 graph nodes')),
    ).toBe(false);
  });

  it('says why no identity rule was written and always offers the connections card', async () => {
    installPipelineFetch('withheld-rules');

    const run = obStart({ ...system });
    await advanceUntilSettled(run, 60_000);

    const summary = h.obAppendMsg.mock.calls
      .map(([message]) => String(message))
      .find((m) => m.startsWith('✓ **Provisioned.**'));
    expect(summary).toContain('- 2 fields mapped to concepts');
    expect(summary).toContain(
      'The Resolve step held back 2 candidates that did not meet the evidence bar',
    );
    // Shown even with no join path written: the card carries the detail-page button.
    expect(h.appendConnections).toHaveBeenCalledWith(
      expect.objectContaining({ system_id: 'cmms', join_paths: [] }),
      'cmms',
    );
  });

  it('cancels an in-flight status poll without post-cancel writes or a retry card', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/onboard/preflight') {
        return Promise.resolve(response({ job_id: 'preflight-job' }));
      }
      if (url === '/api/onboard/preflight/preflight-job') {
        return Promise.resolve(
          response({
            status: 'done',
            result: { verdict: 'ready', connection: { status: 'pass' }, tables: [] },
          }),
        );
      }
      if (url === '/api/onboard/discover') {
        return Promise.resolve(response({ tables: [{ name: 'work_orders' }] }));
      }
      if (url === '/api/onboard/map') {
        return Promise.resolve(response({ job_id: 'map-job' }));
      }
      if (url === '/api/onboard/map/status/map-job') {
        return abortablePending(init?.signal);
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    vi.stubGlobal('fetch', fetchMock);

    const run = obStart({ ...system });
    await vi.advanceTimersByTimeAsync(6000);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/map/status/map-job',
      expect.objectContaining({ signal: h.ob.abort!.signal }),
    );
    const writesAtCancel =
      h.obRemoveLoading.mock.calls.length +
      h.obUpdateLoading.mock.calls.length +
      h.setCheckpointContent.mock.calls.length;

    h.ob.abort!.abort();
    await run;
    await Promise.resolve();

    expect(
      h.obRemoveLoading.mock.calls.length +
        h.obUpdateLoading.mock.calls.length +
        h.setCheckpointContent.mock.calls.length,
    ).toBe(writesAtCancel);
    expect(h.appendRetryError).not.toHaveBeenCalled();
  });
});

describe('resolve polling deadlines', () => {
  it('surfaces a retryable error when the initial resolve reaches 15 minutes', async () => {
    const fetchMock = installPipelineFetch('initial-resolve-pending');
    void obStart({ ...system });

    await vi.advanceTimersByTimeAsync(RESOLVE_DEADLINE_MS + 30_000);

    expect(h.appendRetryError).toHaveBeenCalledWith(
      'Resolve timed out after 15 minutes. Retry to restart resolve.',
    );
    const resolvePolls = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes('/resolve/status/resolve-job'),
    );
    expect(resolvePolls.length).toBeGreaterThan(0);
    expect(resolvePolls.at(-1)?.[1]?.signal).toBe(h.ob.abort!.signal);
  });

  it('surfaces a retryable checkpoint error when resolve re-run reaches 15 minutes', async () => {
    h.autoRerun = true;
    h.heldCheckpointTitle = 'Identity Rules';
    const fetchMock = installPipelineFetch('rerun-resolve-pending');
    void obStart({ ...system });

    await vi.advanceTimersByTimeAsync(RESOLVE_DEADLINE_MS + 30_000);

    expect(h.setCheckpointContent).toHaveBeenCalledWith(
      expect.any(Number),
      {
        kind: 'error',
        text: 'Re-run timed out after 15 minutes. Retry to restart resolve.',
      },
    );
    const rerunPolls = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes('/resolve/status/rerun-job'),
    );
    expect(rerunPolls.length).toBeGreaterThan(0);
    expect(rerunPolls.at(-1)?.[1]?.signal).toBe(h.ob.abort!.signal);
  });
});

describe('map checkpoint vocab_fit wiring (C5)', () => {
  type MappingContent = { kind?: string; rows?: unknown[]; vocab_fit?: unknown };

  it('delivers the map result vocab_fit into the initial mappingTable checkpoint content', async () => {
    installPipelineFetch();

    const run = obStart({ ...system });
    await advanceUntilSettled(run, 60_000);

    const mapSpec = h.obAppendCheckpoint.mock.calls
      .map(([spec]) => spec as { title: string; content?: MappingContent })
      .find((spec) => spec.title.includes('Confirm Mappings'));
    expect(mapSpec).toBeDefined();
    expect(mapSpec?.content?.kind).toBe('mappingTable');
    expect(mapSpec?.content?.vocab_fit).toEqual(MAP_VOCAB_FIT);
  });

  it('carries the re-run result vocab_fit through setCheckpointContent', async () => {
    h.autoRerun = true;
    const fetchMock = installPipelineFetch();

    const run = obStart({ ...system });
    await advanceUntilSettled(run, 60_000);

    const rerunContents = h.setCheckpointContent.mock.calls
      .map(([, content]) => content as MappingContent)
      .filter((content) => content.kind === 'mappingTable');
    expect(rerunContents.length).toBeGreaterThan(0);
    expect(rerunContents.at(-1)?.vocab_fit).toEqual(RERUN_VOCAB_FIT);
    // The rows come from the re-run job's status poll, not the start reply.
    expect(rerunContents.at(-1)?.rows).toHaveLength(1);
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        String(input) === '/api/onboard/map/status/rerun-map-job',
      ),
    ).toHaveLength(1);
  });
});

describe('Confirm Mappings re-run polls the map job', () => {
  type Content = { kind?: string; rows?: unknown[]; text?: string; label?: string };

  function rerunFetch(statuses: unknown[]): ReturnType<typeof vi.fn> {
    const base = installPipelineFetch() as unknown as typeof fetch;
    let rerunPolls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/onboard/map/status/rerun-map-job') {
        const body = statuses[Math.min(rerunPolls, statuses.length - 1)];
        rerunPolls += 1;
        return response(body);
      }
      return base(input, init);
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('keeps the checkpoint loading while the job runs, then shows the polled rows', async () => {
    h.heldCheckpointTitle = 'Confirm Mappings';
    rerunFetch([
      { status: 'running', batch: 1, total_batches: 3, mappings_so_far: 0 },
      {
        status: 'done',
        mappings: [
          { field: 'work_orders.id', concept: 'work-order' },
          { field: 'work_orders.state', concept: 'needs_review' },
        ],
      },
    ]);
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(9_000);
    const cp = h.checkpoints.find((c) => c.title.includes('Confirm Mappings'))!;
    const cpId = h.checkpoints.indexOf(cp) + 1;

    const rerun = cp.onRerun!(cpId);
    await vi.advanceTimersByTimeAsync(3000);
    const contents = () =>
      h.setCheckpointContent.mock.calls
        .filter(([id]) => id === cpId)
        .map(([, content]) => content as Content);
    expect(contents().at(-1)).toEqual({
      kind: 'loading',
      label: 'Mapping fields — batch 1/3 (0 mappings so far)',
    });

    await vi.advanceTimersByTimeAsync(3000);
    await rerun;
    expect(contents().at(-1)?.kind).toBe('mappingTable');
    expect(contents().at(-1)?.rows).toHaveLength(2);
  });

  it('shows the job error on the checkpoint when the re-run job fails', async () => {
    h.heldCheckpointTitle = 'Confirm Mappings';
    rerunFetch([{ status: 'error', error: 'mapper unavailable' }]);
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(9_000);
    const cp = h.checkpoints.find((c) => c.title.includes('Confirm Mappings'))!;
    const cpId = h.checkpoints.indexOf(cp) + 1;

    const rerun = cp.onRerun!(cpId);
    await vi.advanceTimersByTimeAsync(3000);
    await rerun;

    expect(h.setCheckpointContent).toHaveBeenLastCalledWith(cpId, {
      kind: 'error',
      text: 'mapper unavailable',
    });
  });
});

describe('Confirm Mappings with zero rows', () => {
  it('opens the checkpoint with Confirm disabled when the map job returns no mappings', async () => {
    h.heldCheckpointTitle = 'Confirm Mappings';
    const base = installPipelineFetch() as unknown as typeof fetch;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input) === '/api/onboard/map/status/map-job'
          ? response({ status: 'done', mappings: [] })
          : base(input, init),
      ),
    );
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(9_000);

    const spec = h.obAppendCheckpoint.mock.calls
      .map(([s]) => s as { title: string; confirmDisabled?: boolean; content?: { rows?: unknown[] } })
      .find((s) => s.title.includes('Confirm Mappings'));
    expect(spec?.content?.rows).toEqual([]);
    expect(spec?.confirmDisabled).toBe(true);
  });
});

describe('provisioning loader', () => {
  it('names what is created, the elapsed time and the usual duration', () => {
    expect(provisionLoadingLabel(0)).toBe(
      "Creating the system's agent and its gateway connection, 0s elapsed (usually 2 to 5 minutes)",
    );
    expect(provisionLoadingLabel(125_400)).toBe(
      "Creating the system's agent and its gateway connection, 2m 5s elapsed (usually 2 to 5 minutes)",
    );
  });

  it('updates the elapsed time on every provision poll', async () => {
    installPipelineFetch();
    await advanceUntilSettled(obStart({ ...system }), 60_000);

    expect(h.obAppendLoading).toHaveBeenCalledWith(provisionLoadingLabel(0));
    expect(
      h.obUpdateLoading.mock.calls.some(([, label]) =>
        /^Creating the system's agent and its gateway connection, \d+s elapsed/.test(String(label)),
      ),
    ).toBe(true);
  });
});

describe('provision failures', () => {
  it("shows the server's retryable message when another change holds the system", async () => {
    installPipelineFetch('provision-busy');
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(h.appendRetryError).toHaveBeenCalledWith(PROVISION_BUSY_DETAIL);
  });

  it('shows the 409 detail when another provision of the system is running, and Retry starts it again', async () => {
    const fetchMock = installPipelineFetch('provision-start-busy');
    h.appendRetryError.mockImplementationOnce(async () => {});
    await advanceUntilSettled(obStart({ ...system }), 60_000);

    expect(h.appendRetryError).toHaveBeenCalledTimes(1);
    expect(h.appendRetryError).toHaveBeenCalledWith(`Provision failed: ${PROVISION_RUNNING_DETAIL}`);
    const starts = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/onboard/provision');
    expect(starts).toHaveLength(2);
    expect(starts[1][1]?.body).toBe(starts[0][1]?.body);
    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
  });

  it('keeps the bare error code for a failure the server does not explain', async () => {
    installPipelineFetch('provision-error');
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(h.appendRetryError).toHaveBeenCalledWith('provision_failed');
  });

  it('never shows a detail the server did not mark retryable', () => {
    expect(provisionErrorText({ status: 'error', error: 'provision_failed', detail: 'x' })).toBe(
      'provision_failed',
    );
    expect(provisionErrorText({ status: 'error' })).toBe('Provision failed');
  });
});

// ── Walk findings 2026-10-19 (app-fixes C) ───────────────────────────────────

const PROVISION_STATUS_URL = '/api/onboard/provision/status/provision-job';

/** The pipeline fetch, with `override` answering (or failing) chosen requests;
 * null falls through to the pipeline. */
function wrapFetch(
  mode: FetchMode,
  override: (url: string) => Promise<Response> | null,
): ReturnType<typeof vi.fn> {
  const base = installPipelineFetch(mode) as unknown as typeof fetch;
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => override(String(input)) ?? base(input, init),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function failedResponse(status: number, text: string): Response {
  return {
    ok: false,
    status,
    statusText: '',
    text: vi.fn(async () => text),
  } as unknown as Response;
}

const networkDrop = (): Promise<Response> => Promise.reject(new TypeError('Failed to fetch'));

function callsTo(fetchMock: ReturnType<typeof vi.fn>, url: string): unknown[] {
  return fetchMock.mock.calls.filter(([input]) => String(input) === url);
}

function provisionedSummary(): string | undefined {
  return h.obAppendMsg.mock.calls
    .map(([message]) => String(message))
    .find((message) => message.startsWith('✓ **Provisioned.**'));
}

describe('what a wizard run is waiting on', () => {
  it("marks a run parked on a failed step's Retry, and clears the mark when Retry is clicked", async () => {
    installPipelineFetch('provision-error');
    let clickRetry: () => void = () => {};
    h.appendRetryError.mockImplementationOnce(
      () => new Promise<void>((resolve) => { clickRetry = resolve; }),
    );
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(h.appendRetryError).toHaveBeenCalledWith('provision_failed');
    expect(h.ob.waitingOnRetry).toBe(true);
    expect(h.ob.provisioned).toBe(false);

    clickRetry();
    await vi.advanceTimersByTimeAsync(1);
    expect(h.ob.waitingOnRetry).toBe(false); // the retried provision is running

    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.appendRetryError).toHaveBeenCalledTimes(2);
    expect(h.ob.waitingOnRetry).toBe(true);
  });

  it('marks the run provisioned once its provision finished, so a Verify error leaves nothing running', async () => {
    installPipelineFetch('verify-error');
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(h.appendRetryError).toHaveBeenCalledWith('verification query failed');
    expect(h.ob.provisioned).toBe(true);
    expect(h.ob.waitingOnRetry).toBe(true);
  });

  it("marks a blocked preflight as waiting on its retry", async () => {
    h.heldCheckpointTitle = 'Preflight Blocked';
    installPipelineFetch('preflight-blocked');
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(10_000);

    expect(h.checkpoints.some((c) => c.title.includes('Preflight Blocked'))).toBe(true);
    expect(h.ob.waitingOnRetry).toBe(true);
    expect(h.ob.provisioned).toBe(false);
  });

  it('counts a run waiting at a checkpoint as working, and a new run drops the marks of an old one', async () => {
    h.ob.waitingOnRetry = true;
    h.ob.provisioned = true;
    h.heldCheckpointTitle = 'Identity Rules';
    installPipelineFetch();
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(10_000);

    expect(h.checkpoints.some((c) => c.title.includes('Identity Rules'))).toBe(true);
    expect(h.ob.waitingOnRetry).toBe(false);
    expect(h.ob.provisioned).toBe(false);
  });
});

describe('a dropped connection during provision', () => {
  it.each([
    ['a network drop', networkDrop],
    ['a gateway 503', () => Promise.resolve(failedResponse(503, '<html>503 Service Temporarily Unavailable</html>'))],
  ])('reads the job again after %s on a status poll and reports its full result', async (_label, fail) => {
    let failed = 0;
    const fetchMock = wrapFetch('success', (url) => {
      if (url !== PROVISION_STATUS_URL || failed > 0) return null;
      failed += 1;
      return fail();
    });
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(failed).toBe(1);
    expect(h.appendRetryError).not.toHaveBeenCalled();
    expect(h.obSystemIsOnboarded).not.toHaveBeenCalled();
    expect(provisionedSummary()).toMatch(/^✓ \*\*Provisioned\.\*\* Tool `query_cmms` ready\./);
    expect(provisionedSummary()).not.toContain('statistics could not be recovered');
    expect(callsTo(fetchMock, '/api/onboard/provision')).toHaveLength(1);
    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
  });

  it('reads the catalog once the status stays unreadable, and reports the completion it confirms', async () => {
    const fetchMock = wrapFetch('success', (url) =>
      url === PROVISION_STATUS_URL ? networkDrop() : null,
    );
    h.obSystemIsOnboarded.mockResolvedValue(true);
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(120_000);

    // The job's status was read again before the catalog was asked.
    expect(callsTo(fetchMock, PROVISION_STATUS_URL).length).toBeGreaterThan(2);
    expect(h.obSystemIsOnboarded).toHaveBeenCalledWith('cmms', expect.any(AbortSignal));
    expect(h.appendRetryError).not.toHaveBeenCalled();
    expect(h.obAppendMsg).toHaveBeenCalledWith(
      '✓ **Provisioned.** Completion confirmed, but statistics could not be recovered. Tool `query_cmms` ready.',
    );
    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
  });

  it('when neither the job nor the catalog answers, Retry reads the same job again instead of starting another', async () => {
    let networkDown = true;
    const fetchMock = wrapFetch('success', (url) =>
      url === PROVISION_STATUS_URL && networkDown ? networkDrop() : null,
    );
    h.appendRetryError.mockImplementationOnce(async () => {
      networkDown = false; // the user clicks Retry once the network is back
    });
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(120_000);

    expect(h.appendRetryError).toHaveBeenCalledOnce();
    expect(h.appendRetryError).toHaveBeenCalledWith(
      'Lost contact with the server while provisioning (Failed to fetch). ' +
        'The provision may still be running there: Retry checks on it again.',
    );
    expect(callsTo(fetchMock, '/api/onboard/provision')).toHaveLength(1);
    expect(provisionedSummary()).not.toContain('statistics could not be recovered');
    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
  });

  it('Retry sends again a provision request that got no reply, without reading the catalog', async () => {
    let posts = 0;
    const fetchMock = wrapFetch('success', (url) => {
      if (url !== '/api/onboard/provision') return null;
      posts += 1;
      return posts === 1 ? networkDrop() : null;
    });
    h.appendRetryError.mockImplementationOnce(async () => {});
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(h.appendRetryError).toHaveBeenCalledOnce();
    expect(h.appendRetryError).toHaveBeenCalledWith(
      'Provision failed: no reply from the server (Failed to fetch). Retry sends the request again.',
    );
    expect(h.obSystemIsOnboarded).not.toHaveBeenCalled();
    expect(callsTo(fetchMock, '/api/onboard/provision')).toHaveLength(2);
    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
  });

  it('still stops at once on a status read the server answered with an error', async () => {
    const fetchMock = wrapFetch('success', (url) =>
      url === PROVISION_STATUS_URL
        ? Promise.resolve(failedResponse(500, JSON.stringify({ detail: 'status read failed' })))
        : null,
    );
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(callsTo(fetchMock, PROVISION_STATUS_URL)).toHaveLength(1);
    expect(h.appendRetryError).toHaveBeenCalledWith('Provision failed: status read failed');
    expect(h.obSystemIsOnboarded).not.toHaveBeenCalled();
  });
});

describe('a dropped status poll of the other onboarding jobs', () => {
  it.each([
    ['preflight', '/api/onboard/preflight/preflight-job', '/api/onboard/preflight'],
    ['map', '/api/onboard/map/status/map-job', '/api/onboard/map'],
    ['resolve', '/api/onboard/resolve/status/resolve-job', '/api/onboard/resolve'],
    ['agent test', '/api/onboard/verify/agent-test/agent-test-job', '/api/onboard/verify/agent-test'],
  ])('reads the %s job again instead of failing the step', async (_label, statusUrl, startUrl) => {
    let dropped = 0;
    const fetchMock = wrapFetch('success', (url) => {
      if (url !== statusUrl || dropped > 0) return null;
      dropped += 1;
      return networkDrop();
    });
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    expect(dropped).toBe(1);
    expect(h.appendRetryError).not.toHaveBeenCalled();
    expect(callsTo(fetchMock, startUrl)).toHaveLength(1);
    expect(h.obAppendMsg).toHaveBeenCalledWith(
      'Agent test passed: query_cmms returned 3 rows from work_orders in 2 s',
    );
    expect(h.invalidateOnboardingComplete).toHaveBeenCalledOnce();
  });
});

describe('provision duration', () => {
  it('reports the time the wizard counted, not a shorter server-side span', async () => {
    let polls = 0;
    wrapFetch('success', (url) => {
      if (url !== PROVISION_STATUS_URL) return null;
      polls += 1;
      return Promise.resolve(
        response(
          polls < 4
            ? { status: 'running' }
            : {
                status: 'done',
                neptune_nodes_created: 1,
                identity_rules_created: 1,
                tool_name: 'query_cmms',
                // The server times only part of the job (agents/onboard.py).
                provision_seconds: 2,
              },
        ),
      );
    });
    void obStart({ ...system });
    await vi.advanceTimersByTimeAsync(60_000);

    const labels = h.obUpdateLoading.mock.calls
      .map(([, label]) => String(label))
      .filter((label) => label.startsWith("Creating the system's agent"));
    const counted = /, (\S+) elapsed/.exec(labels.at(-1) || '')?.[1];
    expect(counted).toBe('12s');
    expect(provisionedSummary()).toContain(`Took ${counted}.`);
    expect(provisionedSummary()).not.toContain('Took 2s.');
  });
});
