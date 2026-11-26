// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure review-queue logic — DOM-free so it is unit-testable under the node
 * vitest environment (the evals-logic.ts / admin-logic.ts convention).
 *
 * Queue text (question, answer excerpt, steward comment) is UNTRUSTED
 * customer data: these helpers only ever produce strings for literal text
 * nodes. Nothing here builds markup.
 *
 * Two boundaries are owned here (ADR-0062):
 *   - D4 — a machine pre-score is TRIAGE ONLY: `prescoreText` emits the
 *     literal "triage-only" label plus the server's stable signal ids and
 *     CANNOT emit a verdict, score or confidence (there is no such input);
 *   - D5 — a promoted draft is transient customer-derived data: the download
 *     filename is derived from the OPAQUE sha-16 ref only, never from the
 *     question text. */

import { ApiError } from '../../../../api/client';
import type {
  EvalMachinePrescore,
  EvalQueueDecision,
  EvalQueueItem,
  EvalQueueResponse,
  EvalQueueVerdict,
  GoldenDraftBundle,
} from '../../../../api/types/admin';

/** The steward's three verdicts, in the order the panel offers them. The
 * server pins the same enum (anything else is a 422). */
export const QUEUE_VERDICTS: readonly EvalQueueVerdict[] = [
  'correct',
  'incorrect',
  'needs_context',
] as const;

/** Button copy for one verdict (the wire value stays the wire value). */
export function verdictLabel(verdict: EvalQueueVerdict): string {
  return verdict === 'needs_context' ? 'Needs context' : verdict === 'correct' ? 'Correct' : 'Incorrect';
}

/** The literal machine-triage label (agents/eval_queue.py PRESCORE_LABEL).
 * Used as the fallback so the label is present even if the block is absent —
 * a pre-score can never render as a verdict. */
export const PRESCORE_LABEL = 'triage-only';

/** Server-note bound, mirroring eval_queue.NOTE_MAX_CHARS. */
export const NOTE_MAX_CHARS = 500;

/** Accessible pre-score text: the literal label, then the stable signal ids.
 * NO verdict, NO score, NO confidence — none is an input. */
export function prescoreText(prescore: EvalMachinePrescore | undefined): string {
  const label = prescore?.label || PRESCORE_LABEL;
  const signals = (prescore?.signals ?? []).filter((signal) => typeof signal === 'string');
  return signals.length ? `${label} · ${signals.join(', ')}` : `${label} · no signals`;
}

/** Source badge text — "feedback" (a user flagged the turn) or
 * "golden-failure" (a golden question failed in a recorded run). */
export function sourceBadgeText(item: EvalQueueItem): string {
  return item.source || 'unknown';
}

/** The steward decision as one line, or '' when undecided. */
export function decisionText(decision: EvalQueueDecision | null | undefined): string {
  if (!decision?.verdict) return '';
  const tags = (decision.taxonomy_tags ?? []).join(', ');
  const parts = [String(decision.verdict)];
  if (tags) parts.push(tags);
  if (decision.decided_by) parts.push(decision.decided_by);
  return parts.join(' · ');
}

/** Counts line — the honest computed totals from the server envelope. */
export function queueCountsText(counts: EvalQueueResponse['counts']): string {
  if (!counts) return '—';
  return [
    `${counts.total ?? 0} in window`,
    `${counts.decided ?? 0} decided`,
    `${counts.undecided ?? 0} undecided`,
    `${counts.source_expired ?? 0} source expired`,
  ].join(' · ');
}

/** A draft is only offered for a DECIDED item whose source still resolves —
 * the server 409s otherwise (not_decided | source_expired | no_question_text)
 * and a fabricated draft is never an option. */
export function isDraftable(item: EvalQueueItem): boolean {
  return !!item.decision?.verdict && !item.source_expired;
}

/** The pinned server rejection envelope: {reason, detail}. */
export interface QueueRejection {
  reason: string;
  detail: string;
}

/** Parse a route rejection into its pinned (reason, detail) pair.
 *
 * The server's 400/409 detail is an object; `ApiError.detail` carries it as
 * JSON text. Neither field ever contains the steward note — a rejected note is
 * never echoed — so this is safe to render verbatim.
 */
export function rejectionOf(error: unknown): QueueRejection {
  const raw = error instanceof ApiError
    ? error.detail
    : error instanceof Error ? error.message : String(error);
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const fields = parsed as Record<string, unknown>;
      const reason = typeof fields.reason === 'string' ? fields.reason : '';
      const detail = typeof fields.detail === 'string' ? fields.detail : '';
      if (reason || detail) return { reason, detail: detail || reason };
    }
  } catch {
    // A plain-text detail (403, transport failure) is already the message.
  }
  return { reason: '', detail: raw };
}

/** One display line for a rejection: the reason id plus the server's detail.
 * The rejected input itself is never part of either field. */
export function rejectionText(error: unknown): string {
  const { reason, detail } = rejectionOf(error);
  return reason && reason !== detail ? `${reason}: ${detail}` : detail;
}

const SHA16 = /^[0-9a-f]{16}$/;

/** The download filename — OPAQUE sha-16 only (mirrors
 * eval_promote.draft_filename), never question text. Falls back to the item's
 * ref_sha, then to a fixed name, so a download always has a safe filename. */
export function draftFilename(bundle: GoldenDraftBundle, fallbackSha?: string): string {
  const fromDraft = bundle.questions?.[0]?.provenance?.source_ref_sha;
  const sha = [fromDraft, fallbackSha].find((value) => typeof value === 'string' && SHA16.test(value));
  return sha ? `golden-draft-${sha}.json` : 'golden-draft.json';
}
