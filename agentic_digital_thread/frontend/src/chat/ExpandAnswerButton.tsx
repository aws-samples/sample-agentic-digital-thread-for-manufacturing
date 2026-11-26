// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Expand-answer affordance — the EXPLICIT replacement for the retired
 * end-of-turn modal auto-open (chat/auto-open.ts header, 2026-08-06).
 *
 * Renders at the bottom of the sidebar thread (inside the #messages scroll
 * flow — it never shifts or overlaps the composer) once the ACTIVE turn has
 * settled with a substantive answer, and opens the SAME pop-out modal the
 * header pop-out button opens (ChatSidebar's openChatModal → <ChatModal/>,
 * which renders the ONE chatThreadStore and continues the SAME session on
 * follow-up). Visibility reuses THE substantive-answer rule
 * (auto-open.ts isSubstantiveAnswer) — never a second threshold. */
import { useSyncExternalStore } from 'react';

import { useI18n } from '../lib/i18n';
import { isSubstantiveAnswer } from './auto-open';
import { chatThreadStore } from './useAskStream';

/** Expand glyph (stroke: currentColor) — arrows out to corners. */
const EXPAND_SVG = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="15 3 21 3 21 9" />
    <polyline points="9 21 3 21 3 15" />
    <line x1="21" y1="3" x2="14" y2="10" />
    <line x1="3" y1="21" x2="10" y2="14" />
  </svg>
);

/** The active (last) turn has SETTLED with a substantive answer. answerMd is
 * assigned ONLY by the `done` reducer (thread-store onDone — the same
 * reducer that flips streaming off), so its presence IS the settled signal:
 * a streaming follow-up turn starts with an empty answerMd and hides the
 * affordance again; error/abort turns never set it. Boolean snapshot
 * (stable under Object.is): re-renders only when this flips. */
function settledAnswerExists(): boolean {
  const { turns } = chatThreadStore.getState();
  if (turns.length === 0) return false;
  return isSubstantiveAnswer(turns[turns.length - 1].assistant.answerMd);
}

export function ExpandAnswerButton({ onOpen }: { onOpen: () => void }) {
  const { t } = useI18n();
  const visible = useSyncExternalStore(
    chatThreadStore.subscribe,
    settledAnswerExists,
    settledAnswerExists,
  );
  if (!visible) return null;
  return (
    <div className="expand-answer-row">
      <button
        type="button"
        className="expand-answer-btn"
        aria-label={t('chat.expandAnswer')}
        title={t('chat.expandAnswer')}
        onClick={onOpen}
      >
        {EXPAND_SVG}
        <span>{t('chat.expandAnswer')}</span>
      </button>
    </div>
  );
}
