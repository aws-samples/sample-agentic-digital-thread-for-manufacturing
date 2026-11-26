// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* End-of-turn answer-ready gating.
 *
 * The end-of-turn AUTO-OPEN is retired (2026-08-06): the pop-out chat modal
 * NEVER opens by itself when an answer settles. It opens only from explicit
 * user actions — the in-thread expand affordance (chat/ExpandAnswerButton.tsx),
 * the header pop-out button, or the "response ready" toast.
 *
 * What remains here is the CONTEXT gate the retired takeover used, now
 * deciding toast-vs-nothing: when the user is in a context where the settled
 * turn is already surfaced in-app —
 *   - the modal is already open (the answer streamed into it), or
 *   - the chat dock (sidebar) is expanded — the answer plus the expand
 *     affordance are visible in the thread, or
 *   - the active view is 'architecture' (any of its sub tabs — the sub tabs
 *     all live under the one `#architecture` hash view; the investigation
 *     surface itself settles visibly on done)
 * — no notification fires. Anywhere else (e.g. reading the dashboard or
 * mid-onboarding with the dock collapsed) the caller shows a "response
 * ready" toast, and clicking the toast opens the modal (explicit action). */

export interface AutoOpenContext {
  /** The pop-out modal is already open. */
  modalOpen: boolean;
  /** The chat sidebar (dock) is expanded (not collapsed). */
  dockOpen: boolean;
  /** Current hash view name ('dashboard' | 'agents' | 'architecture' | 'onboard'). */
  view: string | null;
}

/** True when the settled turn is already surfaced in-app (see header) — the
 * caller then skips the "response ready" toast. Same contexts the retired
 * takeover recognized; only the ACTION changed (nothing, instead of a
 * full-screen modal). */
export function answerAlreadySurfaced(ctx: AutoOpenContext): boolean {
  return ctx.modalOpen || ctx.dockOpen || ctx.view === 'architecture';
}

/** THE substantive-answer rule (shared by the end-of-turn notify in
 * chat/useAskStream.ts and the expand affordance) — a settled answer counts
 * only when it carries real content. One rule, two consumers: the affordance
 * must appear for exactly the answers the notify hook fires for. */
export function isSubstantiveAnswer(answerMd: string | null | undefined): boolean {
  return !!answerMd && answerMd.trim().length > 10;
}
