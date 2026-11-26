// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Async onboarding jobs (map, provision) answer their start POST with only
 * {job_id, status: 'running'}; the result lives behind GET .../status/{id}.
 * This module is the ONE map runner (start + poll) shared by the wizard's map
 * step, its Confirm Mappings Re-run and the catalog Re-map screen, plus the
 * status poll loop the Re-map Save reuses for its provision job. Reading the
 * start reply as the result is the bug this replaces: it carries no mappings. */
import { ApiError, requestJson } from '../../api/client';
import type { JobStartResponse, MapStatusResponse } from '../../api/types/onboard';
import { buildMapBody } from './payloads';
import { obStepErrorText, type StepAttempt } from './step-retry';

export const POLL_DELAY_MS = 3000;
export const MAP_DEADLINE_MS = 15 * 60 * 1000;
export const PROVISION_DEADLINE_MS = 30 * 60 * 1000;
/** How long a job's status may stay unreadable (the connection dropped)
 * before a poll gives up reading it. */
export const STATUS_READ_GRACE_MS = 30 * 1000;

/** Answers from a load balancer that could not reach the app. */
const GATEWAY_UNAVAILABLE = new Set([502, 503, 504]);

export function abortError(): DOMException {
  return new DOMException('Onboarding canceled', 'AbortError');
}

export function isAbortError(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof Error && error.name === 'AbortError');
}

export function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw abortError();
}

/** Polling delay that rejects immediately when the active onboarding is canceled. */
export function delayWithAbort(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      signal.removeEventListener('abort', onAbort);
      reject(abortError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Whether a request failed on the way, so its answer says nothing about the
 * server's state: fetch rejects with a TypeError when the network fails
 * ("Failed to fetch", e.g. net::ERR_NETWORK_CHANGED), and a load balancer
 * answers 502/503/504 when it cannot reach the app. */
export function isTransportError(error: unknown): boolean {
  if (error instanceof ApiError) return GATEWAY_UNAVAILABLE.has(error.status);
  return error instanceof TypeError;
}

/** A transport failure's short text ("Failed to fetch", "HTTP 503"): a
 * gateway's error body is an HTML page, not a message. */
export function transportErrorText(error: unknown): string {
  if (error instanceof ApiError) return `HTTP ${error.status}`;
  return error instanceof Error && error.message ? error.message : String(error);
}

/**
 * GET a job's status, riding out a dropped connection: a read that fails on
 * the way (isTransportError) is made again on the poll cadence until one
 * answers or STATUS_READ_GRACE_MS has passed since the first failure, when
 * that failure is thrown. Any other failure is thrown at once; cancellation
 * rejects with AbortError.
 */
export async function readJobStatus<T>(url: string, signal: AbortSignal): Promise<T> {
  let failingSince: number | null = null;
  for (;;) {
    try {
      return await requestJson<T>(url, { signal });
    } catch (error) {
      if (isAbortError(error, signal) || !isTransportError(error)) throw error;
      const now = Date.now();
      if (failingSince === null) failingSince = now;
      if (now - failingSince >= STATUS_READ_GRACE_MS) throw error;
    }
    await delayWithAbort(POLL_DELAY_MS, signal);
  }
}

export interface JobPollMessages {
  /** A `status: 'error'` reply that carries no error text. */
  failed: string;
  /** The server no longer knows the job id (container restarted). */
  lost: string;
  /** The deadline passed while the job was still running. */
  timedOut: string;
}

export interface PollJobOptions<T> {
  deadlineMs: number;
  messages: JobPollMessages;
  /** A `done` reply that still carries `error` is a failure (provision). */
  doneErrorFails?: boolean;
  /** Called with every still-running reply (batch progress, elapsed time). */
  onRunning?: (poll: T) => void;
}

/**
 * Poll an async job's status URL until it is done, failed, lost or past its
 * deadline. Resolves {ok:false, error} for every failure the user should see;
 * rejects only on cancellation (AbortError) or a request failure (a dropped
 * connection only once readJobStatus gives up), which the caller turns into
 * its own message.
 */
export async function pollJob<T extends { status?: string; error?: unknown }>(
  statusUrl: string,
  signal: AbortSignal,
  opts: PollJobOptions<T>,
): Promise<StepAttempt<T>> {
  const deadline = Date.now() + opts.deadlineMs;
  while (true) {
    await delayWithAbort(POLL_DELAY_MS, signal);
    const poll = await readJobStatus<T>(statusUrl, signal);
    throwIfAborted(signal);
    if (poll.status === 'done') {
      if (opts.doneErrorFails && poll.error) return { ok: false, error: String(poll.error) };
      return { ok: true, data: poll };
    }
    if (poll.status === 'error') {
      return { ok: false, error: poll.error ? String(poll.error) : opts.messages.failed };
    }
    if (typeof poll.error === 'string' && poll.error.includes('Unknown job_id')) {
      return { ok: false, error: opts.messages.lost };
    }
    opts.onRunning?.(poll);
    if (Date.now() > deadline) return { ok: false, error: opts.messages.timedOut };
  }
}

/** The batch progress line for a running map job, or null before batches start. */
export function mapProgressText(poll: MapStatusResponse): string | null {
  if (!poll.batch || !poll.total_batches) return null;
  return `Mapping fields — batch ${poll.batch}/${poll.total_batches} (${poll.mappings_so_far || 0} mappings so far)`;
}

/**
 * Start the map job for `tables` and poll it to completion. Start errors,
 * job errors, a lost job, the 15-minute deadline and network failures all
 * resolve as {ok:false, error}; cancellation rejects with AbortError.
 */
export async function runMapJob(
  systemId: string,
  tables: unknown[],
  signal: AbortSignal,
  onProgress?: (text: string) => void,
): Promise<StepAttempt<MapStatusResponse>> {
  try {
    const start = await requestJson<JobStartResponse>('/api/onboard/map', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildMapBody(systemId, tables)),
      signal,
    });
    throwIfAborted(signal);
    const startErr = obStepErrorText(null, start);
    if (startErr) return { ok: false, error: startErr };
    if (!start.job_id) return { ok: false, error: 'Mapping failed: the server returned no job id.' };
    return await pollJob<MapStatusResponse>('/api/onboard/map/status/' + start.job_id, signal, {
      deadlineMs: MAP_DEADLINE_MS,
      messages: {
        failed: 'Mapping failed',
        lost: 'Mapping job lost (server restarted). Retry to restart mapping.',
        timedOut: 'Mapping timed out after 15 minutes.',
      },
      onRunning: (poll) => {
        const text = mapProgressText(poll);
        if (text && onProgress) onProgress(text);
      },
    });
  } catch (e) {
    if (isAbortError(e, signal)) throw e;
    return { ok: false, error: 'Mapping failed: ' + (e as Error).message };
  }
}
