// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Conversation Session (ADR-0005) — the client session-id logic.
 *
 * A stable session id is minted once per page load and reused across every
 * turn; it is sent as `session_id` on /api/ask so the server keys memory
 * (Events/Summaries) on the caller-provided Conversation Session. Do NOT
 * regenerate per send. `+ New` mints a fresh id (resetSessionId).
 *
 * The value is mirrored on `window.currentSessionId` because other ported
 * modules (memory rehydration, thread follow-ups) and the e2e suite read the
 * global directly. */

declare global {
  interface Window {
    currentSessionId?: string;
  }
}

/** 'session-<ts>-<rand>' — the stable id format. */
export function mintSessionId(): string {
  return 'session-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
}

/** Current session id; mints and mirrors to window on first access. */
export function getSessionId(): string {
  if (!window.currentSessionId) {
    window.currentSessionId = mintSessionId();
  }
  return window.currentSessionId;
}

/** '+ New' semantics — mint a FRESH conversation session (new memory context). */
export function resetSessionId(): string {
  window.currentSessionId = mintSessionId();
  return window.currentSessionId;
}

/** Session rehydration (memory panel) adopts an existing server-side session. */
export function adoptSessionId(sessionId: string): void {
  window.currentSessionId = sessionId;
}
