// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboarding flow lifecycle + message actions (jsx-migration W5.1). The
 * former imperative DOM helpers (obAppendMsg/obAppendLoading/obAppendError/
 * obAppendCheckpoint/obCollapseStep) are now thin wrappers over the flow
 * store — the wizard's imports stay stable while <ObFlow/> renders the
 * entries. Confirmation modals (cancel / reset-all / disconnect) are
 * store-driven components (<ObModals/>); the lifecycle actions here open
 * them and carry the fetch logic they invoke. */
import { ApiError, authFetch, requestJson } from '../../api/client';
import { queryKeys } from '../../api/query-keys';
import type { CatalogSystem } from '../../api/types/onboard';
import { obLoadCatalog } from './catalog';
import { obWelcomeChat, welcomeSubmit } from './chat';
import {
  appendCheckpoint,
  appendError,
  appendLoading,
  appendMsg,
  closeDetailKeepFlow,
  closeModal,
  collapseStep,
  flowStore,
  openModal,
  patchProgress,
  removeEntry,
  resetToWelcome,
  updateLoadingLabel,
  type CheckpointSpec,
} from './flow-store';
import {
  clearRemoval,
  clearRemovalNotice,
  recordRemovalError,
  removingStore,
  setRemovalNotice,
} from './removal-store';
import { ob } from './state';
import { sysId } from './catalog-store';
import {
  fetchCatalog,
  getOnboardQueryClient,
  invalidateOnboardingReset,
} from './onboard-queries';
import { obStart } from './wizard';

// ── Confirmation modals (store-driven; rendered by <ObModals/>) ──────────────

export function obConfirmCancel(thenFn?: () => void): void {
  openModal({ kind: 'cancel', then: thenFn });
}

export function obCancelActive(): void {
  if (ob.abort) ob.abort.abort();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  ob.waitingOnRetry = false;
  ob.provisioned = false;
  patchProgress({ cancelHidden: true });
  void obReset();
}

/** Whether the wizard's run is still working, so starting another onboarding
 * would cancel something. A run is not, once it waits on a failed step's
 * Retry (the step's request or job has ended) or its provision has finished
 * (the system is onboarded; Cancel would stop only the read-only verify). */
export function obRunInProgress(): boolean {
  return !!ob.active && !ob.waitingOnRetry && !ob.provisioned;
}

export function obGuardStart(sys: CatalogSystem): void {
  // Starting an onboarding: an earlier "Remove again" notice of this system
  // no longer applies.
  clearRemovalNotice(sysId(sys));
  if (obRunInProgress()) {
    obConfirmCancel(() => void obStart(sys));
    return;
  }
  // A wizard left on a Retry, or past its provision, has nothing running to
  // ask about: put it away the way Cancel does, then start.
  if (ob.active) obCancelActive();
  void obStart(sys);
}

/** Whether this system's onboarding is running: its card is busy (the whole
 * run after discover) or it owns the active run (also while that run waits on
 * a failed step's Retry). */
export function obIsOnboarding(systemId: string, busy: boolean): boolean {
  return busy || ob.active === systemId;
}

/** "Onboarding…" on the card of the system being onboarded: bring its running
 * wizard back into view. From another system's detail screen that closes the
 * detail and shows the LIVE flow; the snapshot taken when the detail opened is
 * dropped, not restored, because the run kept updating the flow meanwhile (a
 * finished step, a new checkpoint). On the flow screen the wizard is already
 * showing. Never starts, restarts or cancels a run, and never resets the flow. */
export function obShowActiveOnboarding(): void {
  closeDetailKeepFlow();
}

// NOTE: onboard.js also defined toggleTheme() + a theme-restore block here.
// Theme persistence is owned by the app shell (useTheme) — deliberately NOT
// duplicated in this port.

// ── Message actions (store wrappers — ids instead of HTMLElements) ───────────

export function obAppendMsg(text: string): number {
  return appendMsg(text || '');
}

/** `strong` renders bold after the label (the old
 * 'Connecting to <strong>name</strong>' HTML-string interpolation). */
export function obAppendLoading(text: string, strong?: string): number {
  return appendLoading(text, strong);
}

export function obUpdateLoading(id: number, text: string): void {
  updateLoadingLabel(id, text);
}

export function obRemoveLoading(id: number | null): void {
  if (id != null) removeEntry(id);
}

export function obCollapseStep(label: string): void {
  collapseStep(label);
}

export function obAppendError(text: string): void {
  appendError(text);
}

/** Deterministic-pipeline checkpoint card — STRUCTURED content (the former
 * HTML-string signature is gone; see flow-store CheckpointSpec). The
 * component renders the Re-run button, the "Ask about this step" chat
 * section, and gates onConfirm behind the confirm click. */
export function obAppendCheckpoint(spec: CheckpointSpec): number {
  return appendCheckpoint(spec);
}

// ── Reset / disconnect lifecycle ──────────────────────────────────────────────

export async function obReset(): Promise<void> {
  resetToWelcome();
  obWelcomeChat();
  void obLoadCatalog();
}

export async function obResetAndConnect(): Promise<void> {
  await obReset();
  // The welcome shortcut for "Connect a new system" opens the Connect New
  // System form (chat.ts welcomeSubmit) — same as typing it into the input.
  welcomeSubmit('Connect a new system');
}

export function obResetAll(): void {
  openModal({ kind: 'resetAll' });
}

/** Checked mutation for endpoints whose successful contract is 204/empty.
 * A failure throws an ApiError whose payload is the parsed JSON body (or
 * null), so a caller can show the server's public `error` text. */
async function requestEmptyMutation(url: string, label: string): Promise<void> {
  await requestMutation(url, label);
}

/** POST a mutation and return its parsed JSON body (null when empty or not
 * JSON). A failure throws an ApiError whose payload is that body. */
async function requestMutation(
  url: string,
  label: string,
): Promise<Record<string, unknown> | null> {
  const response = await authFetch(url, { method: 'POST' });
  if (!response) throw new ApiError(401, url, `${label} failed (authentication required)`);

  const text = (await response.text()).trim();
  let body: Record<string, unknown> | null = null;
  if (text) {
    try {
      body = JSON.parse(text) as Record<string, unknown>;
    } catch {
      // Empty/plain-text success is a valid non-JSON mutation contract.
    }
  }
  if (response.ok && !body?.error && body?.status !== 'error') return body;

  const detail = String(
    body?.detail ||
      body?.error ||
      body?.message ||
      text ||
      response.statusText ||
      `HTTP ${response.status}`,
  );
  throw new ApiError(response.status, url, `${label} failed (${detail})`, body);
}

/** The synchronous Reset All route every app release answers (the modal
 * labels its errors by this url). */
const RESET_ALL_URL = '/api/onboard/reset';
/** The background Reset All job (start) and its status (+ job id). A task of
 * the previous release has neither: it answers the start 405 (or 404). */
const RESET_ALL_JOB_URL = '/api/onboard/reset-all';
const RESET_ALL_POLL_MS = 3000;
/** Consecutive failed polls after which the reset's progress counts as unknown. */
const RESET_ALL_POLL_FAILURES = 20;

type ResetAllStatus = { state?: string; reason?: string; error?: string };

const pause = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** The same failure, reported against the reset's url so the modal labels it. */
function asResetError(err: unknown): unknown {
  if (!(err instanceof ApiError) || err.url === RESET_ALL_URL) return err;
  return new ApiError(err.status, RESET_ALL_URL, err.detail, err.payload);
}

/** Reset All: start the server's background job (POST /api/onboard/reset-all),
 * then poll GET /api/onboard/reset-all/{job_id} until it ends. During a
 * rolling deploy the start can reach a task of the previous release, which
 * has no such route (405 or 404): then the synchronous POST /api/onboard/reset
 * runs the reset and answers when it has ended. Every failure throws an
 * ApiError whose url is the reset's, so the modal labels it: a refusal keeps
 * the server's own status (409 a removal or reset is running, 503 not
 * started), a job that ended retryable (reason reset_incomplete,
 * reset_lease_lost or reset_stopped) is a 503, and any other end (an error,
 * a job the server no longer knows, or polls that keep failing) is a 500. */
async function runResetAll(): Promise<void> {
  let started: Record<string, unknown> | null;
  try {
    started = await requestMutation(RESET_ALL_JOB_URL, 'Reset');
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
      await requestEmptyMutation(RESET_ALL_URL, 'Reset');
      return;
    }
    throw asResetError(err);
  }
  const jobId = typeof started?.job_id === 'string' ? started.job_id : '';
  if (!jobId) throw new ApiError(500, RESET_ALL_URL, 'Reset failed (no job was started)', started);
  await pollResetAll(jobId);
}

async function pollResetAll(jobId: string): Promise<void> {
  let failures = 0;
  for (;;) {
    let status: ResetAllStatus;
    try {
      status = await requestJson<ResetAllStatus>(
        RESET_ALL_JOB_URL + '/' + encodeURIComponent(jobId),
      );
      failures = 0;
    } catch {
      // A transient poll failure (or a poll a previous-release task answered
      // 404): the job runs on; ask again, a bounded number of times.
      failures += 1;
      if (failures >= RESET_ALL_POLL_FAILURES) {
        throw new ApiError(500, RESET_ALL_URL, 'Reset failed (its progress could not be read)');
      }
      await pause(RESET_ALL_POLL_MS);
      continue;
    }
    if (status.state === 'done') return;
    if (status.state === 'running') {
      await pause(RESET_ALL_POLL_MS);
      continue;
    }
    const retryable = ['reset_incomplete', 'reset_lease_lost', 'reset_stopped'].includes(
      status.reason || '',
    );
    const detail = status.error || 'the reset did not finish';
    throw new ApiError(retryable ? 503 : 500, RESET_ALL_URL, `Reset failed (${detail})`, status);
  }
}

/** Reset-all confirm (the modal's danger button): disconnect all systems,
 * clear the graph, reseed. Throws on failure (the modal renders 'Failed').
 * Once the reset itself succeeded, a failed reseed still refreshes the
 * systems, catalog, status and concepts and returns the wizard to welcome
 * (the systems and graph are already gone), then rethrows so the modal stays
 * open with the reseed notice and a retry. */
export async function obResetAllConfirmed(
  onPhase: (label: string) => void,
): Promise<void> {
  if (ob.abort) ob.abort.abort();
  ob.abort = null;
  ob.active = null;
  ob.currentStep = null;

  await runResetAll();

  onPhase('Reseeding DB...');
  try {
    await requestEmptyMutation('/api/onboard/reseed', 'Reseed');
  } catch (err) {
    try {
      await invalidateOnboardingReset(getOnboardQueryClient());
    } catch {
      /* cache invalidation is best-effort */
    }
    await obReset();
    throw err;
  }

  await invalidateOnboardingReset(getOnboardQueryClient());
  closeModal();
  await obReset();
}

/** Open the removal dialog. ``retryFull`` is the mode of a removal that did
 * not finish ("Remove again"): the dialog then finishes it the same way.
 * ``retry`` marks a "Remove again" dialog: it re-checks the system's current
 * state before it confirms (:func:`obRetryStillNeeded`). */
export function obDisconnectSystem(
  systemId: string,
  systemName?: string,
  registration = false,
  retryFull?: boolean,
  retry = false,
  userOrigin = false,
): void {
  // A new action on this system: an earlier "Remove again" notice no longer
  // applies.
  clearRemovalNotice(systemId);
  openModal({ kind: 'disconnect', systemId, systemName, registration, retryFull, retry, userOrigin });
}

/** The system's entry in a catalog read from the server now (not this tab's
 * cache). A catalog read already in flight (the periodic refresh) may have
 * started before the state changed, so it is cancelled and a new read is
 * made rather than joined. The read also reconciles this tab's removal record
 * with it (removal-store.ts). Throws when the read fails. */
async function freshCatalogEntry(systemId: string): Promise<CatalogSystem | undefined> {
  const client = getOnboardQueryClient();
  await client.cancelQueries({ queryKey: queryKeys.catalog() });
  const systems = await fetchCatalog(client, true, new AbortController().signal);
  return systems.find((s) => sysId(s) === systemId);
}

/** "Remove again" on a half-removed card. This tab's view can be stale (the
 * catalog refreshes every minute, and another tab or Reset All may have
 * recovered the system), and its retry may be fixed to a full removal, so the
 * catalog is read from the server first. Opens the retry dialog only while
 * the system is still `removal_failed`. Otherwise it opens nothing, the card
 * re-renders from the fresh read, and a notice is announced
 * (removalNoticeStore): when the clicked button is gone (the state changed,
 * or the system left the catalog) focus moves to the card's current action;
 * when the read failed the button stays and keeps focus. */
export async function obRemoveAgain(systemId: string, systemName?: string): Promise<void> {
  clearRemovalNotice(systemId);
  let fresh: CatalogSystem | undefined;
  try {
    fresh = await freshCatalogEntry(systemId);
  } catch {
    setRemovalNotice(
      systemId,
      'Could not check its current state, so nothing was opened. Try again.',
      false,
      'unreadable',
    );
    return;
  }
  if (!fresh) {
    setRemovalNotice(
      systemId,
      `${systemName || systemId} is no longer in the catalog, so nothing was opened.`,
      true,
      'gone',
    );
    return;
  }
  if (fresh.status !== 'removal_failed') {
    setRemovalNotice(
      systemId,
      `Its state changed: it is ${fresh.status || 'not half-removed'} now, so nothing was opened.`,
      true,
      fresh.status || '',
    );
    return;
  }
  // Finish the removal the way it was asked: a registration-only removal (a
  // 'registered' user system) reopens in registration mode; any other
  // reopens with its full/default choice fixed, from the server's recorded
  // mode (it survives a restart) or this tab's job. Full wins: a full attempt
  // may already have scheduled the stored credentials for deletion.
  const mode = removingStore.get().modes?.[systemId];
  const registration = mode?.registration ?? false;
  const recorded = [
    fresh.removal_mode === 'full' ? true : fresh.removal_mode === 'default' ? false : undefined,
    mode?.full,
  ].filter((m): m is boolean => typeof m === 'boolean');
  const retryFull = recorded.length ? recorded.some(Boolean) : undefined;
  obDisconnectSystem(systemId, systemName, registration,
    registration ? undefined : retryFull, true, fresh.connection_origin === 'user');
}

/** Before a "Remove again" dialog confirms: whether the system is still
 * half-removed on the server (`yes`), is in another state now (`changed`,
 * with that state; `gone` when it is no longer in the catalog), or could not
 * be checked (`unknown`). */
export async function obRetryStillNeeded(
  systemId: string,
): Promise<{ check: 'yes' | 'changed' | 'unknown'; status?: string }> {
  try {
    const fresh = await freshCatalogEntry(systemId);
    if (fresh?.status === 'removal_failed') return { check: 'yes' };
    return { check: 'changed', status: fresh?.status || 'gone' };
  } catch {
    return { check: 'unknown' };
  }
}

/* ── System-removal job tracking ─────────────────────────────────────────────
 * The removal record (removingStore) and how a catalog read reconciles it
 * live in removal-store.ts; this module starts the background reset job
 * (?wait=false) and polls it. */
export { removingStore } from './removal-store';
export type { RemovingMode, RemovingState } from './removal-store';

const REMOVAL_POLL_MS = 3000;

function removalDone(systemId: string): void {
  clearRemoval([systemId]);
}

async function pollRemoval(systemId: string): Promise<void> {
  for (;;) {
    await new Promise((r) => setTimeout(r, REMOVAL_POLL_MS));
    let status: { state?: string; error?: string; full?: boolean };
    try {
      status = await requestJson<{ state?: string; error?: string; full?: boolean }>(
        '/api/onboard/reset/' + systemId + '/status',
      );
    } catch {
      continue; // transient poll failure — keep the gate closed and retry
    }
    if (status.state === 'running') continue;
    if (status.state === 'error') {
      // Keep the sticky mode; `full` from the status record.
      recordRemovalError(systemId, status.error || 'Removal failed', status.full);
      // The server now reports the system as removal_failed: reload so every
      // view shows the half-removed system as a removal to retry.
      try {
        await obLoadCatalog();
      } catch {
        /* catalog reload is best-effort */
      }
      return;
    }
    // done — or "none" (app restarted mid-job; cleanup is idempotent and the
    // catalog reload below is the source of truth for what still exists).
    break;
  }
  removalDone(systemId);
  try {
    await invalidateOnboardingReset(getOnboardQueryClient());
  } catch {
    /* cache invalidation is best-effort */
  }
  try {
    await obLoadCatalog();
  } catch {
    /* catalog reload is best-effort */
  }
}

/** Removal confirm: start the background reset job and poll it; returns an
 * error string to render on the button, or null once the job is started
 * (modal closes; the card shows the removing state until the poll completes). */
export async function obDisconnectConfirmed(
  systemId: string,
  full = false,
  registration = false,
  retry = false,
): Promise<string | null> {
  let data: { error?: string; status?: string };
  try {
    // ``retry`` ("Remove again" only): the server refuses it (409, nothing
    // removed) unless the system is still half-removed when it claims it.
    data = await requestJson<{ error?: string; status?: string }>(
      '/api/onboard/reset/' + systemId + '?wait=false' + (full ? '&full=true' : '') +
        (retry ? '&retry=true' : ''),
      { method: 'POST' },
    );
  } catch (error) {
    return (error as Error).message;
  }
  if (data.error) return data.error;
  removingStore.update((s) => {
    const errorSeq = { ...s.errorSeq };
    delete errorSeq[systemId];
    return {
      ...s,
      ids: { ...s.ids, [systemId]: 'running' as const },
      errors: { ...s.errors, [systemId]: '' },
      // Retain the removal intent so a later "Remove again" reopens in the same
      // mode and never downgrades a full removal to a default one.
      modes: { ...s.modes, [systemId]: { full, registration } },
      errorSeq,
    };
  });
  clearRemovalNotice(systemId);
  closeModal();
  void pollRemoval(systemId);
  return null;
}

/** Open the chat sidebar (chat module's global) and ask about a system's
 * onboarding connections. */
export function obAskAbout(sysName: string): void {
  const w = window as unknown as {
    toggleChatSidebar?: () => void;
    askQuestion?: (q: string) => void;
  };
  const sidebar = document.getElementById('chat-sidebar');
  if (sidebar?.classList.contains('collapsed') && typeof w.toggleChatSidebar === 'function') {
    w.toggleChatSidebar();
  }
  if (typeof w.askQuestion === 'function') {
    w.askQuestion('What connections were made when onboarding ' + sysName + '?');
  }
}

/** Cancel-modal confirm path (component callback). */
export function obCancelConfirmed(then?: () => void): void {
  closeModal();
  obCancelActive();
  if (then) then();
}

/** Snapshot check used by the flow-input visibility (wizard mode only). */
export function isWizardActive(): boolean {
  return flowStore.get().mode === 'wizard';
}
