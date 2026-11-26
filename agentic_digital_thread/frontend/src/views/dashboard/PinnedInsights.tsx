// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pinned prompt cards — React half of the old pinned.ts (jsx-migration W2.1).
 * Same classes/structure as the old DOM builders (.pinned-card[data-card-id],
 * .pinned-card-refresh, .pinned-card-body, .pinned-card-error); titles/errors
 * are JSX text and result markdown renders through <Md/> (the old
 * marked+DOMPurify sanitize). Container #pinned-cards-container stays hidden
 * until cards exist. */
import { useState } from 'react';

import type { PinnedCard } from '../../api/types/dashboard';
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import { Md } from '../../shared/ui/Md';
import { relativeTime } from './lib';
import { refreshCard } from './pinned';
import { dashboardStore } from './store';

function PinnedCardItem({ card, refreshError }: { card: PinnedCard; refreshError?: string }) {
  const [refreshing, setRefreshing] = useState(false);

  // Click-to-run: switch to chat + ask the prompt (globals owned by the chat
  // port; guarded like the original's typeof askQuestion === 'function').
  const promptText = (card.prompt_text || '').replace(/\n/g, ' ');
  const onCardClick = promptText
    ? () => {
        const w = window as unknown as {
          askQuestion?: (q: string) => void;
          switchView?: (v: string) => void;
        };
        if (typeof w.askQuestion === 'function') {
          w.switchView?.('chat');
          w.askQuestion(promptText);
        }
      }
    : undefined;

  return (
    <div
      className="pinned-card"
      data-card-id={card.card_id || ''}
      style={promptText ? { cursor: 'pointer' } : undefined}
      onClick={onCardClick}
    >
      <div className="pinned-card-header">
        <span className="pinned-card-title">{card.title || 'Untitled'}</span>
        <span className="pinned-card-time">
          {card.last_executed ? relativeTime(card.last_executed) : ''}
        </span>
        <button
          className={'pinned-card-refresh' + (refreshing ? ' loading' : '')}
          onClick={(event) => {
            event.stopPropagation();
            setRefreshing(true);
            void refreshCard(card.card_id || '').finally(() => setRefreshing(false));
          }}
        >
          {/* static refresh glyph (old REFRESH_SVG) */}
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="23 4 23 10 17 10" />
            <path d="M20.49 15a9 9 0 11-2.12-9.36L23 10" />
          </svg>
        </button>
      </div>
      <div className="pinned-card-body">
        {refreshError ? (
          <div className="pinned-card-error">{refreshError}</div>
        ) : card.status === 'running' ? (
          <div className="pinned-card-loading">Running…</div>
        ) : card.result_video_url ? (
          <video controls preload="metadata" src={card.result_video_url} />
        ) : card.result_markdown ? (
          <Md className="stream-md" text={card.result_markdown} />
        ) : (
          <div style={{ color: 'var(--muted)', fontSize: '0.75rem' }}>Choose to run</div>
        )}
      </div>
      {card.error ? <div className="pinned-card-error">{card.error}</div> : null}
    </div>
  );
}

export function PinnedInsights() {
  const { t } = useI18n();
  const pinned = useStore(dashboardStore, (s) => s.pinned);
  const pinnedErrors = useStore(dashboardStore, (s) => s.pinnedErrors);
  const visible = !!(pinned && pinned.length);
  return (
    <div
      className="pinned-cards-section"
      id="pinned-cards-container"
      style={{ display: visible ? '' : 'none' }}
    >
      <div className="pinned-cards-header">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M12 2L2 7l10 5 10-5-10-5z" />
          <path d="M2 17l10 5 10-5" />
          <path d="M2 12l10 5 10-5" />
        </svg>
        <span>{t('dash.pinnedInsights')}</span>
      </div>
      <div className="pinned-cards-grid">
        {(pinned || []).map((c, i) => (
          <PinnedCardItem
            key={c.card_id || i}
            card={c}
            refreshError={pinnedErrors[c.card_id || '']}
          />
        ))}
      </div>
    </div>
  );
}
