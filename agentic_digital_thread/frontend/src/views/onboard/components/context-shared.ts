// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* context-shared — pure helpers, types, and URL constants for the guided
 * business-context capture surfaces.
 *
 * Extracted from the retired standalone guided-context panel (removed
 * 2026-09-11; rollback is `git revert`). These are the pieces with more than
 * one consumer:
 * `PreMapProbe` (the wizard-path probe) imports `turnResultLine` today, and the
 * conversational Context Interview card rides the same guided endpoints. The
 * module is deliberately component-free — no React, no fetch, no state — so any
 * card can import it without dragging a panel along.
 *
 * The honesty rules the helpers encode are contractual:
 *  - a rejection surfaces the server's pinned reason verbatim;
 *  - a conflict is a REFUSAL to create ambiguous authority, not an error;
 *  - a partial write must NEVER read as success (a steward told "saved" over a
 *    dropped write would never retry);
 *  - the confirm cost states the real affected-system count, never a
 *    hardcoded one. */

const MUTED = 'var(--muted)';
const RED = 'var(--red, #f87171)';
const GREEN = 'var(--green, #4ade80)';
const AMBER = 'var(--amber, #fbbf24)';

export const GUIDED_STATE_URL = '/api/onboard/guided/state';
export const GUIDED_QUESTIONS_URL = '/api/onboard/guided/questions';
export const GUIDED_TURN_URL = '/api/onboard/guided/turn';
export const GUIDED_DISMISS_URL = '/api/onboard/guided/dismiss';
export const GUIDED_CONFIRM_URL = '/api/onboard/guided/confirm-context';
export const GUIDED_CLARIFY_URL = '/api/onboard/guided/clarify';
export const GUIDED_MANIFEST_URL = '/api/onboard/guided/manifest';

export type GuidedState = {
  first_launch: boolean;
  dismissed: boolean;
  answered_topics: string[];
  draft_business_context: string;
};

export type GuidedQuestion = {
  id: string;
  prompt: string;
  treatment_phase: string;
  examples?: string[];
};

export type TurnResult = {
  outcome: 'proposed' | 'draft' | 'conflict' | 'rejected' | 'skipped';
  question_id: string;
  landing?: string;
  reason?: string;
  detail?: string;
  conflict_with?: string;
  proposal_ids?: string[];
  draft_business_context?: string;
};

/** Honest per-answer feedback. A rejection is actionable (the server returns the
 * pinned reason); a conflict is a REFUSAL to create ambiguous authority, which is
 * a correct outcome the steward must resolve deliberately, not an error to retry. */
export function turnResultLine(r: TurnResult): { color: string; text: string } {
  switch (r.outcome) {
    case 'draft':
      return { color: GREEN, text: 'Captured — added to the context draft below.' };
    case 'proposed':
      return { color: GREEN, text: 'Captured — created a proposal for review.' };
    case 'skipped':
      return { color: MUTED, text: 'Skipped.' };
    case 'conflict':
      return {
        color: AMBER,
        text:
          `Not applied — another system (${r.conflict_with ?? 'unknown'}) is already ` +
          'the authority for this concept. Deactivate that rule first if you want to change it.',
      };
    default:
      return { color: RED, text: r.detail || r.reason || 'Not accepted.' };
  }
}

export type ConfirmResponse = {
  status: 'ok' | 'partial';
  systems: Record<string, { written?: boolean; reason?: string; roll?: string }>;
};

/** Honest confirm reporting. A partial write MUST NOT read as success: the
 * durable write is what makes context real, and a steward told "saved" over a
 * dropped write would never retry. Names the systems that failed and why. */
export function confirmResultLine(res: ConfirmResponse): { color: string; text: string } {
  const entries = Object.entries(res.systems ?? {});
  const failed = entries.filter(([, v]) => !v.written);
  if (res.status === 'ok' && failed.length === 0) {
    const unverified = entries.filter(([, v]) => v.roll && v.roll !== 'ok');
    if (unverified.length > 0) {
      return {
        color: AMBER,
        text:
          'Context saved, but the agent restart could not be confirmed for ' +
          `${unverified.map(([k]) => k).join(', ')} — it will be picked up on the ` +
          'next restart.',
      };
    }
    return { color: GREEN, text: 'Context saved.' };
  }
  return {
    color: RED,
    text:
      'Not saved for ' +
      failed.map(([k, v]) => `${k} (${v.reason ?? 'failed'})`).join(', ') +
      '. Your draft has been kept — retry or fix the system first.',
  };
}

/** The write cost, stated before the button (a confirm batches N systems into
 * one serial roll each — never a hardcoded count). */
export function confirmWarningText(systems: string[]): string {
  const n = systems.length;
  if (n === 0) return 'Connect a system first — context is saved per system.';
  return (
    `Saving will restart ${n} system agent${n === 1 ? '' : 's'} (~2 min each); ` +
    `answers may briefly degrade while ${n === 1 ? 'it restarts' : 'they restart'}.`
  );
}

/** Human labels for the landing destinations — where each answer goes. */
export function landingLabel(landing: string): string {
  switch (landing) {
    case 'business_context_draft':
      return 'Business context (saved on confirm below)';
    case 'concept_overlay':
    case 'concept_retire':
      return 'Vocabulary proposal (pending steward review)';
    case 'identifier_semantics':
      return 'Identifier mapping proposal (pending steward review)';
    case 'authority_intent':
      return 'Authority preference (pending steward review)';
    case 'query_guidance':
    case 'domain_note':
      return 'Query guidance proposal (pending steward review)';
    default:
      return landing;
  }
}
