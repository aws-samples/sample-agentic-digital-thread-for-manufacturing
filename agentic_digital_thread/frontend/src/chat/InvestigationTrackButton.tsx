// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Investigation tracking button — appears beside the chat header title
 * whenever the conversation session is ACTIVE and navigates to the
 * Architecture view's Investigation tab.
 *
 * "Session is active" (replaces the old streaming+30s-grace window): the
 * turn is streaming right now, OR a live conversation session exists — a
 * session id has been minted/adopted (lib/session.ts window.currentSessionId)
 * AND the thread holds at least one turn or rehydrated history message. A
 * pristine empty chat (no session activity) shows nothing; '+ New'
 * (ChatSidebar.startNewSession → chatThreadStore.clear()) empties the thread
 * and hides the button even though resetSessionId mints a fresh id.
 *
 * The stream lives in module-level chatThreadStore (chat/useAskStream.ts) and
 * the chat sidebar is shell-persistent (views lazy-mount; the sidebar does
 * not unmount on switchView), so navigating here CANNOT kill the stream —
 * goToInvestigation is a view switch only and must never touch the thread
 * store, the arch store, or the abort controller. */
import { useSyncExternalStore } from 'react';

import { useI18n } from '../lib/i18n';
import { requestArchTab } from '../views/architecture/arch-tab-bridge';
import { chatThreadStore } from './useAskStream';

/** Navigate to the Architecture view's Investigation tab. View-switch ONLY —
 * requestArchTab stashes the request if ArchitectureView is not mounted yet
 * (arch-tab-bridge lazy-mount seam, same pattern as StewardDigest). */
export function goToInvestigation(): void {
  window.switchView?.('architecture');
  requestArchTab('investigation');
}

/** Thread has conversational content — a live turn or rehydrated history.
 * Boolean snapshot (stable under Object.is) so the subscription re-renders
 * only when the emptiness actually flips. */
function threadHasContent(): boolean {
  const s = chatThreadStore.getState();
  return s.turns.length > 0 || s.history.length > 0;
}

/** The session-active visibility rule (see the file header). READ-ONLY over
 * the thread store — subscribing here never mutates it. */
export function useSessionActive(streaming: boolean): boolean {
  const hasThread = useSyncExternalStore(
    chatThreadStore.subscribe,
    threadHasContent,
    threadHasContent,
  );
  return streaming || (hasThread && !!window.currentSessionId);
}

/** Small node-graph glyph (stroke: currentColor). */
const GRAPH_SVG = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="5" cy="6" r="2.5" />
    <circle cx="19" cy="6" r="2.5" />
    <circle cx="12" cy="18" r="2.5" />
    <line x1="6.8" y1="8" x2="10.5" y2="16" />
    <line x1="17.2" y1="8" x2="13.5" y2="16" />
    <line x1="7.5" y1="6" x2="16.5" y2="6" />
  </svg>
);

export function InvestigationTrackButton({
  streaming,
  onNavigate,
}: {
  streaming: boolean;
  /** Runs after the view switch so the host can get out of the way of the
   * page it just navigated to (ChatSidebar closes the pop-out modal, and the
   * chat drawer on the mobile layout). Must never touch the stream. */
  onNavigate?: () => void;
}) {
  const { t } = useI18n();
  const visible = useSessionActive(streaming);
  if (!visible) return null;
  return (
    <button
      type="button"
      className={`inv-track-btn${streaming ? ' streaming' : ''}`}
      aria-label={t('chat.trackInvestigation')}
      title={t('chat.trackInvestigation')}
      onClick={() => {
        goToInvestigation();
        onNavigate?.();
      }}
    >
      {GRAPH_SVG}
    </button>
  );
}
