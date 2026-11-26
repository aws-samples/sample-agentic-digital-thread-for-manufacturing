// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The deterministic onboarding pipeline (obStart): discover → map (polled job)
 * → resolve (polled job) → provision (polled job) → verify, gated by
 * .ob-confirm-btn checkpoints. The control flow is unchanged from the
 * onboard.js port; the DOM writes became flow-store actions and the
 * checkpoints carry STRUCTURED content (jsx-migration W5.1) — the mapping
 * confirm resolution reads the checkpoint's React state rows, never the DOM.
 * Every step runs through obRetryStep (step-retry.ts): failures (non-2xx,
 * {error} body, network throw, lost/timed-out jobs) render visibly with a
 * Retry affordance instead of dead-ending the flow (merged legacy delta). */
import { requestJson } from '../../api/client';
import { agentTestMessage, agentTestRunningText, runAgentTest } from './agent-test';
import type {
  CatalogSystem,
  DiscoverResponse,
  FieldMapping,
  IdentityRule,
  JobStartResponse,
  MapStatusResponse,
  PreflightResult,
  PreflightStatusResponse,
  ProposedConcept,
  ProvisionStatusResponse,
  ReconcileResponse,
  RemapSuggestion,
  ResolveStatusResponse,
  VerifyResponse,
} from '../../api/types/onboard';
import { getConnString, setBusy } from './catalog-store';
import { obLoadCatalog, obSystemIsOnboarded } from './catalog';
import { refreshSuggestions } from './chat';
import {
  obAppendCheckpoint,
  obAppendLoading,
  obAppendMsg,
  obCollapseStep,
  obRemoveLoading,
  obUpdateLoading,
} from './flow';
import {
  appendConnections,
  appendCta,
  appendDiscovery,
  confirmCheckpoint,
  getEntry,
  patchProgress,
  removeEntry,
  setCheckpointContent,
  showReconcileBanner,
  startWizard,
  type ConceptTagVm,
  type MappingRowVm,
} from './flow-store';
import { runProbeStep } from './probe-step';
import { provisionSummaryMd } from './provision-summary';
import { verifySummaryLine } from './verify-summary';
import {
  buildDiscoverBody,
  buildPreflightBody,
  buildProvisionBody,
  buildReconcileBody,
  buildResolveBody,
  buildVerifyBody,
  confirmedMappingsFromRows,
} from './payloads';
import { basisFor } from './render';
import {
  getOnboardQueryClient,
  invalidateOnboardingComplete,
} from './onboard-queries';
import { getObConcepts, ob, setObConcepts } from './state';
import { obRetryStep, obStepErrorText, type StepAttempt } from './step-retry';
import {
  abortError,
  delayWithAbort,
  isAbortError,
  isTransportError,
  POLL_DELAY_MS,
  PROVISION_DEADLINE_MS,
  readJobStatus,
  runMapJob,
  throwIfAborted,
  transportErrorText,
} from './job-poll';

export { delayWithAbort };
export const RESOLVE_DEADLINE_MS = 15 * 60 * 1000;

const PROVISION_LOADING_TEXT = "Creating the system's agent and its gateway connection";

/** The provisioning loader line: what is being created, how long it has been
 * running, and the usual duration, so a multi-minute wait reads as progress. */
export function provisionLoadingLabel(elapsedMs: number): string {
  const seconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const elapsed =
    seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${PROVISION_LOADING_TEXT}, ${elapsed} elapsed (usually 2 to 5 minutes)`;
}

/** The text for a failed provision job: the server's own message when it
 * reports a retryable failure it explains (another change to the system's
 * connection was in progress; the graph write failed) or refused a
 * PostgreSQL login that can do more than read, else its error code. */
export function provisionErrorText(poll: ProvisionStatusResponse): string {
  const explained = poll.retryable === true || poll.reason === 'privileged_login';
  if (explained && typeof poll.detail === 'string' && poll.detail.trim()) {
    return poll.detail;
  }
  return poll.error || 'Provision failed';
}

/** Wait for a rendered checkpoint without leaking when cancellation removes its UI. */
function waitForCheckpoint<T>(
  signal: AbortSignal,
  render: (confirm: (value: T) => void) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const cleanup = (): void => signal.removeEventListener('abort', onAbort);
    const onAbort = (): void => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(abortError());
    };
    const confirm = (value: T): void => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };

    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener('abort', onAbort, { once: true });
    try {
      render(confirm);
    } catch (error) {
      settled = true;
      cleanup();
      reject(error);
    }
  });
}

function pendingCancellation<T>(): Promise<T> {
  return new Promise<T>(() => {});
}

/** Record whether the run owning `signal` waits on the user to retry a failed
 * step. A cancelled run (its wait never ends) leaves the next run's mark alone. */
function markWaitingOnRetry(signal: AbortSignal, waiting: boolean): void {
  if (ob.abort?.signal === signal) ob.waitingOnRetry = waiting;
}

/**
 * Race the existing retry UI against cancellation. obRetryStep intentionally
 * converts thrown errors into retry cards, so an AbortError attempt stays
 * pending while this outer promise resolves cancellation quietly.
 */
function retryStepWithAbort<T>(
  signal: AbortSignal,
  attempt: () => Promise<StepAttempt<T>>,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const cleanup = (): void => signal.removeEventListener('abort', onAbort);
    const onAbort = (): void => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(abortError());
    };
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener('abort', onAbort, { once: true });
    void obRetryStep<T>(
      async () => {
        try {
          return await attempt();
        } catch (error) {
          if (isAbortError(error, signal)) return pendingCancellation<StepAttempt<T>>();
          throw error;
        }
      },
      (waiting) => markWaitingOnRetry(signal, waiting),
    ).then(
      (data) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(data);
      },
      (error) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    );
  });
}

/** Mapping-checkpoint rows from the map response (the former buildRows HTML
 * builder, as view-models — <MappingTable/> renders them). */
export function buildMappingRows(maps: FieldMapping[]): MappingRowVm[] {
  return maps.map((m) => {
    const concepts = m.concepts || [
      { concept: m.concept, confidence: m.confidence || 0.5 },
    ];
    const tags: ConceptTagVm[] = concepts
      .filter((c) => c.concept && c.concept !== 'needs_review')
      .map((c) => ({
        concept: c.concept as string,
        confidence: c.confidence ?? 0,
        basis: basisFor(m, c.concept as string)?.basis,
      }));
    return { field: String(m.field || ''), tags, review: tags.length === 0 };
  });
}

/** Reconcile suggestions banner (store-driven <ReconcileBanner/>). */
export function showReconcileNotification(suggestions: RemapSuggestion[]): void {
  showReconcileBanner(suggestions);
}

// ── Preflight stage (E1, ADR-0053) ───────────────────────────────────────────

/** Poll budget for one preflight job. The server clamps a run at 300 s and
 * stores an honest timeout error; this outer budget only covers a lost job. */
export const PREFLIGHT_DEADLINE_MS = 6 * 60 * 1000;

/** Poll one preflight job GET until done/error/lost/deadline. Error jobs
 * surface the honest sanitized line AS `error` (wave-1 contract amendment)
 * — it is returned VERBATIM for the retry card. */
async function pollPreflightJob(
  jobId: string,
  signal: AbortSignal,
): Promise<StepAttempt<PreflightResult>> {
  const deadline = Date.now() + PREFLIGHT_DEADLINE_MS;
  while (true) {
    await delayWithAbort(POLL_DELAY_MS, signal);
    const poll = await readJobStatus<PreflightStatusResponse>(
      '/api/onboard/preflight/' + jobId,
      signal,
    );
    throwIfAborted(signal);
    if (poll.status === 'done' && poll.result) return { ok: true, data: poll.result };
    if (poll.status === 'error') {
      return { ok: false, error: poll.error || 'Preflight failed' };
    }
    if (poll.error && poll.error.includes('Unknown job_id')) {
      return {
        ok: false,
        error: 'Preflight job lost (server restarted). Retry to restart preflight.',
      };
    }
    if (Date.now() > deadline) {
      return { ok: false, error: 'Preflight timed out. Retry to restart preflight.' };
    }
  }
}

/** One preflight attempt: POST /api/onboard/preflight, then poll the job GET
 * until done/error (the map/resolve job idiom — plain fetch, never the SWR
 * loader). */
async function attemptPreflight(
  sysId: string,
  sysName: string,
  signal: AbortSignal,
): Promise<StepAttempt<PreflightResult>> {
  const loader = obAppendLoading('Running preflight checks for ', sysName);
  try {
    const startData = await requestJson<JobStartResponse>('/api/onboard/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPreflightBody(sysId)),
      signal,
    });
    throwIfAborted(signal);
    const startErr = obStepErrorText(null, startData);
    if (startErr) {
      obRemoveLoading(loader);
      return { ok: false, error: startErr };
    }
    const outcome = await pollPreflightJob(String(startData.job_id), signal);
    obRemoveLoading(loader);
    return outcome;
  } catch (e) {
    if (isAbortError(e, signal)) throw e;
    obRemoveLoading(loader);
    return { ok: false, error: 'Preflight failed: ' + (e as Error).message };
  }
}

function preflightStepContext(sysId: string, result: PreflightResult) {
  return {
    step: 'preflight',
    systemId: sysId,
    data: { preflight: result } as Record<string, unknown>,
    history: [],
  };
}

/** The preflight verdict gate: `blocked` never proceeds (the card's confirm
 * becomes "Fix connection & retry", which re-runs preflight — discover never
 * auto-starts); `degraded` proceeds only on an explicit confirm with the
 * warnings visible; `ready` auto-confirms the card and continues. */
async function runPreflightGate(
  sysId: string,
  sysName: string,
  signal: AbortSignal,
): Promise<void> {
  while (true) {
    const result = await retryStepWithAbort<PreflightResult>(signal, () =>
      attemptPreflight(sysId, sysName, signal),
    );
    throwIfAborted(signal);
    if (result.verdict === 'ready') {
      const cpId = obAppendCheckpoint({
        title: 'CHECKPOINT — Preflight Passed',
        content: { kind: 'preflight', result },
        confirmLabel: 'Continue →',
        stepContext: preflightStepContext(sysId, result),
        onConfirm: () => {},
      });
      confirmCheckpoint(cpId); // auto-proceed: card renders confirmed
      return;
    }
    const blocked = result.verdict === 'blocked';
    // Blocked: the preflight job has ended and its card's only action is to
    // run it again, like a failed step's Retry.
    if (blocked) markWaitingOnRetry(signal, true);
    const proceed = await waitForCheckpoint<boolean>(signal, (confirm) => {
      obAppendCheckpoint({
        title: blocked
          ? 'CHECKPOINT — Preflight Blocked'
          : 'CHECKPOINT — Preflight Warnings',
        content: { kind: 'preflight', result },
        confirmLabel: blocked ? '↻ Fix connection & retry' : 'Proceed with warnings →',
        stepContext: preflightStepContext(sysId, result),
        onConfirm: () => confirm(!blocked),
      });
    });
    throwIfAborted(signal);
    markWaitingOnRetry(signal, false);
    if (proceed) return; // degraded: explicit proceed past the warnings
    // blocked: the confirm was "Fix connection & retry" — loop re-runs preflight.
  }
}

/** The provision job one wizard run polls, kept across its Retry attempts. */
interface ProvisionJob {
  id: string;
  /** Date.now() when the wizard sent the provision request. */
  startedAt: number;
}

/** A finished provision, and how long the wizard waited for it. */
interface ProvisionOutcome {
  data: ProvisionStatusResponse;
  elapsedMs: number;
}

/** The result of a provision the catalog confirmed because its job's own
 * record was lost or could not be read: the counts are unknown. */
function catalogConfirmedProvision(sysId: string): ProvisionStatusResponse {
  return {
    status: 'done',
    stats_unavailable: true,
    tool_name: `query_${sysId.replace(/-/g, '_')}`,
  };
}

/**
 * One provision attempt: send the provision request (or, after an attempt
 * that lost contact with its job, read that job again), then poll the job
 * until it is done, failed, lost or past its deadline. A status that stays
 * unreadable (readJobStatus rides out a short drop) is checked against the
 * catalog before it counts as a failure, as a lost job and a lapsed deadline
 * are; that failure keeps the job, so Retry reads it again. A request that got
 * no reply keeps no job: Retry sends the request again.
 */
async function attemptProvision(
  sysId: string,
  provBody: string,
  signal: AbortSignal,
  job: { current: ProvisionJob | null },
): Promise<StepAttempt<ProvisionOutcome>> {
  const startedAt = job.current?.startedAt ?? Date.now();
  const elapsedMs = (): number => Date.now() - startedAt;
  const loader = obAppendLoading(provisionLoadingLabel(elapsedMs()));
  const finish = (
    data: ProvisionStatusResponse,
    waitedMs: number,
  ): StepAttempt<ProvisionOutcome> => {
    obRemoveLoading(loader);
    job.current = null;
    return { ok: true, data: { data, elapsedMs: waitedMs } };
  };
  const fail = (error: string, keepJob = false): StepAttempt<ProvisionOutcome> => {
    obRemoveLoading(loader);
    if (!keepJob) job.current = null;
    return { ok: false, error };
  };
  const catalogConfirms = async (): Promise<boolean> => {
    const onboarded = await obSystemIsOnboarded(sysId, signal);
    throwIfAborted(signal);
    return onboarded;
  };
  try {
    let current = job.current;
    if (!current) {
      let provStartData: JobStartResponse;
      try {
        provStartData = await requestJson<JobStartResponse>('/api/onboard/provision', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: provBody,
          signal,
        });
      } catch (e) {
        if (isAbortError(e, signal) || !isTransportError(e)) throw e;
        // No job id, so nothing to read again; the request may never have
        // reached the server.
        return fail(
          `Provision failed: no reply from the server (${transportErrorText(e)}). ` +
            'Retry sends the request again.',
        );
      }
      throwIfAborted(signal);
      const provStartErr = obStepErrorText(null, provStartData);
      if (provStartErr) return fail(provStartErr);
      current = { id: String(provStartData.job_id), startedAt };
      job.current = current;
    }
    const statusUrl = '/api/onboard/provision/status/' + current.id;
    // Poll for completion. A LARGE cross-account source legitimately takes
    // ~15-20 min to sample per-column, stand up its AgentCore runtime, and
    // write its schema graph — the budget must cover the slow case rather than
    // false-timing-out while provision is still working server-side.
    const provDeadline = startedAt + PROVISION_DEADLINE_MS;
    while (true) {
      await delayWithAbort(POLL_DELAY_MS, signal);
      let pollData: ProvisionStatusResponse;
      try {
        pollData = await readJobStatus<ProvisionStatusResponse>(statusUrl, signal);
      } catch (e) {
        if (isAbortError(e, signal) || !isTransportError(e)) throw e;
        // The job's status stayed unreadable: it may have finished, failed or
        // still be running. The catalog says whether the system was onboarded.
        if (await catalogConfirms()) return finish(catalogConfirmedProvision(sysId), elapsedMs());
        return fail(
          `Lost contact with the server while provisioning (${transportErrorText(e)}). ` +
            'The provision may still be running there: Retry checks on it again.',
          true,
        );
      }
      throwIfAborted(signal);
      const waitedMs = elapsedMs();
      obUpdateLoading(loader, provisionLoadingLabel(waitedMs));
      if (pollData.status === 'done') {
        if (pollData.error) return fail(String(pollData.error));
        return finish(pollData, waitedMs);
      }
      if (pollData.status === 'error') return fail(provisionErrorText(pollData));
      // Job ID lost (container restarted) — check if system was actually provisioned
      if (pollData.error && pollData.error.includes('Unknown job_id')) {
        // Provisioning completed before container restart — treat as success
        if (await catalogConfirms()) return finish(catalogConfirmedProvision(sysId), elapsedMs());
        return fail(
          'Provision job lost (server restarted). Check whether the system was onboarded, then Retry if needed.',
        );
      }
      if (Date.now() > provDeadline) {
        // Budget lapsed — but on a very large source the final graph write can
        // land just after; before failing, confirm against the catalog so a
        // server-side "onboarded" is treated as success, not a false timeout.
        if (await catalogConfirms()) return finish(catalogConfirmedProvision(sysId), elapsedMs());
        return fail('Provision timed out after 30 minutes.');
      }
    }
  } catch (e) {
    if (isAbortError(e, signal)) throw e;
    return fail('Provision failed: ' + (e as Error).message);
  }
}

export async function obStart(sys: CatalogSystem): Promise<void> {
  const systemId = (sys.id || sys.system_id) as string;
  const run = runOnboarding(sys);
  const runController = ob.abort;
  try {
    await run;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') return;
    throw error;
  } finally {
    // A cancel callback may already have started a replacement run for this
    // same system. Only the owning run (or a run with no replacement) clears it.
    if (ob.active !== systemId || ob.abort === runController) {
      setBusy(systemId, false);
    }
  }
}

async function runOnboarding(sys: CatalogSystem): Promise<void> {
  // (onboard.js has `const sysId = sys.id || sysId` — a latent TDZ self-ref;
  // the intent, matching the rest of the file, is the id-or-system_id fallback.)
  const sysId = (sys.id || sys.system_id) as string;
  sys.id = sysId; // normalize
  ob.active = sysId;
  ob.abort = new AbortController();
  ob.waitingOnRetry = false;
  ob.provisioned = false;
  const signal = ob.abort.signal;

  setBusy(sysId, true); // the old btn-<sid> disable
  const connStr = getConnString(sysId, sys.connection_string as string | undefined);
  // Structured, non-secret connection settings for typed connectors (e.g. Athena:
  // workgroup/database/catalog/region/output_location). Threaded into the
  // discover and provision calls below; {} for connection-string-based systems.
  const connCfg = (sys.connection_config || {}) as Record<string, unknown>;

  // Clear previous conversation, reset flow into wizard mode (progress bar +
  // persistent chat input render from the store).
  startWizard();

  const OB_STEPS = ['Discover', 'Map', 'Resolve', 'Provision', 'Verify'];
  const OB_STATUS = [
    'Connecting...',
    'Mapping concepts...',
    'Resolving identities...',
    'Provisioning...',
    'Verifying...',
  ];

  // Persistent chat input context (the flow input renders in wizard mode and
  // sends through obSendChat(ob.currentStep, msg, null)).
  ob.currentStep = {
    step: 'discover',
    systemId: sysId,
    data: { connection_string: connStr, connection_type: sys.connection_type },
    history: [],
  };

  function advanceStep(idx: number): void {
    patchProgress({
      activeIdx: idx,
      statusText:
        idx < OB_STATUS.length
          ? OB_STATUS[idx]
          : 'Onboarding complete',
      allDone: false,
    });
    if (ob.currentStep && idx < OB_STEPS.length)
      ob.currentStep.step = OB_STEPS[idx].toLowerCase();
  }

  // Step 0: Preflight (E1, ADR-0053) — the diagnostic gate BEFORE discover:
  // blocked never auto-starts discover (the card's confirm re-runs preflight);
  // degraded proceeds only on explicit confirm; ready auto-continues. Job
  // failures render with Retry like every other step.
  await runPreflightGate(sysId, sys.name, signal);
  throwIfAborted(signal);

  // Step 1: Discover — every failure (non-2xx, {error} body, network throw, or an
  // empty/detail-only body that previously hung the flow at 'Connecting…') renders
  // visibly with a Retry that re-invokes the same call.
  const discBody = JSON.stringify(
    buildDiscoverBody(sysId, connStr, sys.connection_type, connCfg),
  );
  let discData = await retryStepWithAbort<DiscoverResponse>(signal, async () => {
    const loader = obAppendLoading('Connecting to ', sys.name);
    try {
      const d = await requestJson<DiscoverResponse>('/api/onboard/discover', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: discBody,
        signal,
      });
      throwIfAborted(signal);
      obRemoveLoading(loader);
      const err = obStepErrorText(null, d);
      if (err) {
        setBusy(sysId, false); // the old btn re-enable on failure
        return { ok: false, error: err };
      }
      if (!Array.isArray(d.tables)) {
        setBusy(sysId, false);
        return {
          ok: false,
          error: 'Discovery returned no schema. Retry, or check the connection settings.',
        };
      }
      return { ok: true, data: d };
    } catch (e) {
      if (isAbortError(e, signal)) throw e;
      obRemoveLoading(loader);
      setBusy(sysId, false);
      return { ok: false, error: 'Connection failed: ' + (e as Error).message };
    }
  });
  throwIfAborted(signal);
  setBusy(sysId, true); // the old btn.disabled = true after a successful discover
  if (discData.ai_analysis) obAppendMsg(discData.ai_analysis);
  throwIfAborted(signal);
  if (!ob.currentStep) return;
  ob.currentStep.data = {
    tables: [...(discData.tables || [])],
    connection_string: connStr,
    connection_type: sys.connection_type,
  };

  // Checkpoint: schema
  await waitForCheckpoint<void>(signal, (confirm) => {
    const stepCtx = {
      step: 'discover',
      systemId: sysId,
      data: {
        tables: [...(discData.tables || [])],
        connection_string: connStr,
        connection_type: sys.connection_type,
      } as Record<string, unknown>,
      history: [],
    };
    // For MCP systems, show a more descriptive summary
    const isMcp = sys.connection_type === 'MCP Server';
    obAppendCheckpoint({
      title: 'CHECKPOINT — Confirm Schema',
      content: isMcp
        ? {
            kind: 'mcpSummary',
            name: discData.tables?.[0]?.name || 'MCP Tool',
            count: (discData.tables?.[0]?.row_count as number) || 0,
          }
        : { kind: 'schemaTable', tables: discData.tables || [] },
      confirmLabel: 'Confirm Schema →',
      stepContext: stepCtx,
      onConfirm: () => confirm(undefined),
      onRerun: async (id) => {
        if (signal.aborted) return;
        setCheckpointContent(id, { kind: 'loading', label: 'Re-discovering' });
        try {
          const d = await requestJson<DiscoverResponse>('/api/onboard/discover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(
              buildDiscoverBody(sysId, connStr, sys.connection_type, connCfg),
            ),
            signal,
          });
          throwIfAborted(signal);
          if (d.error) {
            setCheckpointContent(id, { kind: 'error', text: d.error });
            return;
          }
          discData = d;
          stepCtx.data.tables = [...(d.tables || [])];
          setCheckpointContent(id, { kind: 'schemaTable', tables: d.tables || [] });
        } catch (e) {
          if (isAbortError(e, signal)) return;
          setCheckpointContent(id, {
            kind: 'error',
            text: 'Re-run failed: ' + (e as Error).message,
          });
        }
      },
    });
  });
  throwIfAborted(signal);

  // Pre-map probe (T-H): pause BEFORE /map for per-system context questions.
  // Skippable and failure-open inside the component — a probe outage, a skip,
  // or nothing-to-ask all continue straight to map; /map itself is untouched.
  await runProbeStep(sysId, discData.tables || [], signal);
  throwIfAborted(signal);

  // Step 2: Map (async job + polling) — start/poll failures render with Retry.
  advanceStep(1);
  obCollapseStep('Discover — ' + sys.name);
  let mapData = await retryStepWithAbort<MapStatusResponse>(signal, async () => {
    const loader = obAppendLoading(
      'Analyzing field semantics and mapping to manufacturing concepts',
    );
    // Cancellation rejects out of runMapJob before the loader is touched.
    const result = await runMapJob(sysId, discData.tables || [], signal, (text) =>
      obUpdateLoading(loader, text),
    );
    obRemoveLoading(loader);
    return result;
  });
  throwIfAborted(signal);
  ob.currentStep!.data = {
    mappings: [...(mapData.mappings || [])],
    tables: discData.tables,
    connection_string: connStr,
    connection_type: sys.connection_type,
  };

  // Checkpoint: mappings with multi-concept tag UI (rows are React state —
  // the confirm resolution + payload derivation read the store, not the DOM)
  const confirmedMappings = await waitForCheckpoint<FieldMapping[]>(signal, (confirm) => {
    const stepCtx = {
      step: 'map',
      systemId: sysId,
      data: {
        mappings: [...(mapData.mappings || [])],
        tables: discData.tables,
      } as Record<string, unknown>,
      history: [],
    };
    const rows = buildMappingRows(mapData.mappings || []);
    const unmappedCount = rows.filter((r) => r.tags.length === 0).length;
    obAppendCheckpoint({
      title: 'CHECKPOINT — Confirm Mappings',
      // C5 (additive): carry the map result's vocab_fit so <ObFlow/> renders
      // the fit banner above the checkpoint.
      content: { kind: 'mappingTable', rows, vocab_fit: mapData.vocab_fit },
      confirmLabel: 'Confirm Mappings →',
      // Same gate as flow-store mappingConfirmBlocked: no rows ⇒ nothing to confirm.
      confirmDisabled: rows.length === 0 || unmappedCount > 0,
      showSkipAll: true,
      stepContext: stepCtx,
      onConfirm: (id) => {
        const entry = getEntry(id);
        const stateRows =
          entry && entry.kind === 'checkpoint' && entry.content.kind === 'mappingTable'
            ? entry.content.rows
            : null;
        confirm(confirmedMappingsFromRows(mapData.mappings || [], stateRows));
      },
      onRerun: async (id) => {
        if (signal.aborted) return;
        setCheckpointContent(id, { kind: 'loading', label: 'Re-mapping' });
        try {
          // /map only starts a job; the mappings come from its status poll.
          const result = await runMapJob(sysId, discData.tables || [], signal, (text) =>
            setCheckpointContent(id, { kind: 'loading', label: text }),
          );
          if (!result.ok) {
            setCheckpointContent(id, { kind: 'error', text: result.error });
            return;
          }
          const d = result.data;
          mapData = d;
          stepCtx.data.mappings = [...(d.mappings || [])];
          setCheckpointContent(id, {
            kind: 'mappingTable',
            rows: buildMappingRows(d.mappings || []),
            // C5: the re-run result carries its own vocab_fit (or none).
            vocab_fit: d.vocab_fit,
          });
        } catch (e) {
          if (isAbortError(e, signal)) return;
          setCheckpointContent(id, {
            kind: 'error',
            text: 'Re-run failed: ' + (e as Error).message,
          });
        }
      },
    });
  });
  throwIfAborted(signal);

  // Step 3: Resolve
  advanceStep(2);
  obCollapseStep('Map — Field Concepts');

  // Feature: concept discovery
  if (mapData.proposed_concepts && mapData.proposed_concepts.length > 0) {
    const w = window as unknown as { _acceptedProposals?: ProposedConcept[] };
    w._acceptedProposals = w._acceptedProposals || [];
    await new Promise<void>((resolve) => {
      const proposals = mapData.proposed_concepts || [];
      const entryId = appendDiscovery(
        proposals,
        (i) => {
          if (signal.aborted) return;
          const c = proposals[i];
          w._acceptedProposals!.push(c);
          setObConcepts([
            ...getObConcepts().filter((x) => x !== 'unknown'),
            (c.id || c.name) as string,
            'unknown',
          ]);
        },
        () => {
          if (signal.aborted) return;
          removeEntry(entryId);
          resolve();
        },
      );
    });
  }
  let resData = await retryStepWithAbort<ResolveStatusResponse>(signal, async () => {
    const loader = obAppendLoading('Identifying cross-system identity resolution rules');
    try {
      const resStartData = await requestJson<JobStartResponse>('/api/onboard/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildResolveBody(sysId, confirmedMappings)),
        signal,
      });
      throwIfAborted(signal);
      const resStartErr = obStepErrorText(null, resStartData);
      if (resStartErr) {
        obRemoveLoading(loader);
        return { ok: false, error: resStartErr };
      }
      const resJobId = resStartData.job_id;
      const resolveDeadline = Date.now() + RESOLVE_DEADLINE_MS;
      // Poll for completion
      while (true) {
        if (Date.now() >= resolveDeadline) {
          throwIfAborted(signal);
          obRemoveLoading(loader);
          return { ok: false, error: 'Resolve timed out after 15 minutes. Retry to restart resolve.' };
        }
        await delayWithAbort(POLL_DELAY_MS, signal);
        const resPollData = await readJobStatus<ResolveStatusResponse>(
          `/api/onboard/resolve/status/${resJobId}`,
          signal,
        );
        throwIfAborted(signal);
        if (resPollData.status === 'done') {
          obRemoveLoading(loader);
          if (resPollData.error) return { ok: false, error: String(resPollData.error) };
          return { ok: true, data: resPollData };
        }
        if (resPollData.status === 'error') {
          obRemoveLoading(loader);
          return {
            ok: false,
            error: 'Resolve failed: ' + (resPollData.error || 'unknown error'),
          };
        }
        if (resPollData.error && resPollData.error.includes('Unknown job_id')) {
          obRemoveLoading(loader);
          return {
            ok: false,
            error: 'Resolve job lost (server restarted). Retry to restart resolve.',
          };
        }
      }
    } catch (e) {
      if (isAbortError(e, signal)) throw e;
      obRemoveLoading(loader);
      return { ok: false, error: 'Resolve failed: ' + (e as Error).message };
    }
  });
  throwIfAborted(signal);
  ob.currentStep!.data = { rules: [...(resData.rules || [])], mappings: confirmedMappings };

  // Checkpoint: identity rules (use correct field names from API)
  const confirmedRules = await waitForCheckpoint<IdentityRule[]>(signal, (confirm) => {
    const rules = resData.rules || [];
    const stepCtx = {
      step: 'resolve',
      systemId: sysId,
      data: { rules: [...rules], mappings: confirmedMappings } as Record<string, unknown>,
      history: [],
    };
    obAppendCheckpoint({
      title: 'CHECKPOINT — Identity Rules',
      content: { kind: 'rules', rules, extras: resData },
      confirmLabel: 'Confirm & Provision →',
      stepContext: stepCtx,
      onConfirm: () => confirm(stepCtx.data.rules as IdentityRule[]),
      onRerun: async (id) => {
        if (signal.aborted) return;
        setCheckpointContent(id, { kind: 'loading', label: 'Re-resolving' });
        try {
          const rerunStartData = await requestJson<JobStartResponse>(
            '/api/onboard/resolve',
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(buildResolveBody(sysId, confirmedMappings)),
              signal,
            },
          );
          throwIfAborted(signal);
          if (rerunStartData.error) {
            setCheckpointContent(id, { kind: 'error', text: rerunStartData.error });
            return;
          }
          const rerunJobId = rerunStartData.job_id;
          const rerunDeadline = Date.now() + RESOLVE_DEADLINE_MS;
          while (true) {
            if (Date.now() >= rerunDeadline) {
              throwIfAborted(signal);
              setCheckpointContent(id, {
                kind: 'error',
                text: 'Re-run timed out after 15 minutes. Retry to restart resolve.',
              });
              break;
            }
            await delayWithAbort(POLL_DELAY_MS, signal);
            const d = await readJobStatus<ResolveStatusResponse>(
              `/api/onboard/resolve/status/${rerunJobId}`,
              signal,
            );
            throwIfAborted(signal);
            if (d.status === 'done') {
              resData = d;
              stepCtx.data.rules = [...(d.rules || [])];
              setCheckpointContent(id, {
                kind: 'rules',
                rules: d.rules || [],
                extras: d,
              });
              break;
            }
            if (d.status === 'error') {
              setCheckpointContent(id, {
                kind: 'error',
                text: 'Re-run failed: ' + d.error,
              });
              break;
            }
          }
        } catch (e) {
          if (isAbortError(e, signal)) return;
          setCheckpointContent(id, {
            kind: 'error',
            text: 'Re-run failed: ' + (e as Error).message,
          });
        }
      },
    });
  });
  throwIfAborted(signal);

  // Step 4: Provision (send all required data) — start/poll failures render with Retry.
  advanceStep(3);
  obCollapseStep('Resolve — Identity Rules');
  const provBody = JSON.stringify(
    buildProvisionBody({
      systemId: sysId,
      systemName: sys.name,
      plant: sys.plant,
      schema: discData.schema_name || sys.schema,
      rules: confirmedRules,
      mappings: confirmedMappings,
      connectionType: sys.connection_type,
      connectionString: connStr,
      connectionConfig: connCfg,
      // The job whose rules the checkpoint showed (a Re-run replaces resData).
      resolveJobId: resData.resolve_job_id,
    }),
  );
  // The job a Retry reads again after an attempt lost contact with it (it may
  // still be running, and the server refuses a second provision meanwhile).
  const provJob: { current: ProvisionJob | null } = { current: null };
  const provision = await retryStepWithAbort<ProvisionOutcome>(signal, () =>
    attemptProvision(sysId, provBody, signal, provJob),
  );
  throwIfAborted(signal);
  if (ob.abort?.signal === signal) ob.provisioned = true;
  const provData = provision.data;
  obAppendMsg(
    provisionSummaryMd(provData, {
      withheldRules: resData.rejected_rules?.length,
      proposedRules: resData.rules?.length,
      sentRules: confirmedRules.length,
      // The wizard's own clock, the one its loader line counted.
      elapsedSeconds: Math.floor(provision.elapsedMs / 1000),
    }),
  );

  // The connections touching the system, each with its evidence, and the
  // button to the system's detail page (shown even when none were written).
  appendConnections(provData, sysId);

  // Feature: concept suggestions. Fields in other systems that may belong to
  // a concept this system's onboarding added.
  try {
    const reconData = await requestJson<ReconcileResponse>('/api/onboard/reconcile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildReconcileBody(sysId, String(provData.provision_run_id || ''))),
      signal,
    });
    throwIfAborted(signal);
    if (reconData.suggestions && reconData.suggestions.length > 0) {
      showReconcileNotification(reconData.suggestions);
    }
  } catch (e) {
    if (isAbortError(e, signal)) throw e;
    obAppendMsg('⚠ Reconciliation check failed: ' + (e as Error).message);
  }
  throwIfAborted(signal);

  // Step 5: Verify — failures render with Retry.
  advanceStep(4);
  const verBody = JSON.stringify(
    buildVerifyBody(sysId, discData.schema_name || sys.schema),
  );
  const verData = await retryStepWithAbort<VerifyResponse>(signal, async () => {
    const loader = obAppendLoading('Running verification query');
    try {
      const d = await requestJson<VerifyResponse>('/api/onboard/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: verBody,
        signal,
      });
      throwIfAborted(signal);
      obRemoveLoading(loader);
      const err = obStepErrorText(null, d);
      if (err) return { ok: false, error: err };
      return { ok: true, data: d };
    } catch (e) {
      if (isAbortError(e, signal)) throw e;
      obRemoveLoading(loader);
      return { ok: false, error: 'Verify failed: ' + (e as Error).message };
    }
  });
  throwIfAborted(signal);
  obAppendMsg(verifySummaryLine(verData));

  // Agent test: the new system's own query tool, called through the gateway
  // (a polled job). Its result is reported, never a reason to stop.
  const testLoader = obAppendLoading(agentTestRunningText(0));
  const testStarted = Date.now();
  const test = await runAgentTest(sysId, signal, () =>
    obUpdateLoading(testLoader, agentTestRunningText(Date.now() - testStarted)),
  );
  throwIfAborted(signal);
  obRemoveLoading(testLoader);
  obAppendMsg(test.ok ? agentTestMessage(test.data) : `Agent test failed: ${test.error}`);

  // Done — mark all steps complete
  patchProgress({
    activeIdx: 5,
    allDone: true,
    statusText: '✓ Onboarding complete',
  });

  // Invalidate every completion-dependent server view only after verification.
  throwIfAborted(signal);
  await invalidateOnboardingComplete(getOnboardQueryClient());
  void refreshSuggestions();
  await obLoadCatalog();
  throwIfAborted(signal);
  ob.active = null;
  ob.abort = null;

  // Post-verify CTA
  appendCta(sys.name);
}
