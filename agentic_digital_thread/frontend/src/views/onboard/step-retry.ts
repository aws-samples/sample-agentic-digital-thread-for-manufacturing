// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Wizard step-error surfacing with Retry (port of onboard.js obStepErrorText /
 * obRetryStep). Every step failure (non-2xx, {error} body, network throw, or a
 * body that previously hung the flow) renders visibly through the flow store's
 * retryable error entry instead of dead-ending the pipeline. */
import { appendRetryError } from './flow-store';

export type StepAttempt<T> = { ok: true; data: T } | { ok: false; error: string };

/**
 * Extract the user-facing error text for a wizard step response, or '' on
 * success. A step failed when the HTTP response is non-2xx OR the JSON body
 * carries {error}. When the payload carries reason='assume_role_denied'
 * (cross-account role probe), its `detail` is written to be user-actionable —
 * surface it VERBATIM.
 */
export function obStepErrorText(
  resp: { ok: boolean; status: number } | null,
  data: unknown,
): string {
  const d = (data || {}) as Record<string, unknown>;
  if (d.reason === 'assume_role_denied' && d.detail) return String(d.detail);
  if (d.error) return String(d.error);
  if (d.detail && String(d.detail).indexOf('assume_role_denied') !== -1)
    return String(d.detail);
  if (resp && !resp.ok)
    return String(d.detail || d.message || 'Request failed (HTTP ' + resp.status + ')');
  return '';
}

/** The server's code for a Map/Provision it cannot rejoin to its Discover
 * schema (expired, another system's, or a mismatched request: the server
 * answers all of them with this one code). Retrying sends the same schema
 * revision again, so only a fresh Discover recovers. */
const SCHEMA_GONE_CODE = 'schema_provenance_invalid';
const SCHEMA_GONE_HINT =
  "The server can no longer validate the schema this onboarding discovered, so Retry can't fix it. " +
  'Click Cancel, then Onboard on the system card to start again.';

/** The error text a step shows: the step's own text, plus how to recover when
 * Retry cannot help. The original text (with its code) is kept for support. */
export function obRecoverableErrorText(text: string): string {
  if (!text.includes(SCHEMA_GONE_CODE)) return text;
  return `${text}. ${SCHEMA_GONE_HINT}`;
}

/**
 * Run a wizard step attempt, surfacing every failure with a visible error +
 * Retry button instead of hanging or dead-ending the flow. `attempt` resolves
 * to {ok:true, data} on success or {ok:false, error} on failure; a thrown
 * exception is treated as a failure too. Loops until an attempt succeeds
 * (each retry re-invokes the same call). `onWaiting` hears true while the
 * Retry card waits on the user and false once Retry is clicked.
 */
export async function obRetryStep<T>(
  attempt: () => Promise<StepAttempt<T>>,
  onWaiting?: (waiting: boolean) => void,
): Promise<T> {
  for (;;) {
    let res: StepAttempt<T>;
    try {
      res = await attempt();
    } catch (e) {
      res = {
        ok: false,
        error: e instanceof Error && e.message ? e.message : String(e),
      };
    }
    if (res && res.ok) return res.data;
    onWaiting?.(true);
    await appendRetryError(
      obRecoverableErrorText((res && res.error) || 'Step failed (no error detail).'),
    );
    onWaiting?.(false);
  }
}
