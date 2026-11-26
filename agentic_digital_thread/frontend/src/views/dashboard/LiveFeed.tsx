// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Live Factory Events panel (S5.4) — React half of the old feed.ts
 * (jsx-migration W2.1). Renders #live-feed-panel / #live-feed (CONTRACTUAL
 * ids) from the store's feed slice. Scroll behavior preserved: a seed scrolls
 * to the bottom (the old append-then-scrollTop=scrollHeight), an SSE prepend
 * pins the top only when the user is already near it (scrollTop < 10).
 *
 * Task 1C (live events lifecycle truth): the panel reads the store's iotStats
 * slice (seed fetch + SSE 'stats' event) and, when the feed is empty, renders
 * an HONEST empty state via liveFeedEmptyState — "not configured" vs "started,
 * can't connect" (last_error tooltip) vs "connected, no publishers". The old
 * hardcoded "Historian" src-badge is replaced by a presence-driven "IoT Core"
 * badge shown only when messages have actually been received; the stats line
 * (#mqtt-stats) renders from the store instead of a direct DOM write.
 *
 * Empty-env gate: the whole panel is HIDDEN (display:none — NOT unmounted;
 * the contractual nodes must stay attached for the e2e layout contract and
 * the Task 1C unit tests) until event data is actually flowing: it shows
 * once feed lines exist OR iotStats reports total_messages > 0. A feed that
 * started then went quiet (messages seen, current window empty) stays
 * visible — started-then-quiet is still "flowing". */
import { useEffect, useRef } from 'react';

import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import { liveFeedEmptyState, toggleFeedPause } from './feed';
import { dashboardStore } from './store';

export function LiveFeed() {
  const { t } = useI18n();
  const feed = useStore(dashboardStore, (s) => s.feed);
  const iotStats = useStore(dashboardStore, (s) => s.iotStats);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !feed.seq) return;
    if (feed.lastAction === 'seed') {
      el.scrollTop = el.scrollHeight;
    } else if (feed.lastAction === 'append' && el.scrollTop < 10) {
      el.scrollTop = 0;
    }
  }, [feed.seq, feed.lastAction]);

  const emptyState = feed.lines.length === 0 ? liveFeedEmptyState(iotStats) : null;
  const hasMessages = (iotStats?.total_messages || 0) > 0;
  // Empty-env gate: hide (never unmount — contractual ids) until data flows.
  const hidden = feed.lines.length === 0 && !hasMessages;

  return (
    <div
      className="live-feed-panel"
      id="live-feed-panel"
      style={hidden ? { display: 'none' } : undefined}
    >
      <div
        className="panel-title"
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
      >
        <span>
          <span
            className="live-dot"
            id="feed-live-dot"
            style={{ animationPlayState: feed.paused ? 'paused' : 'running' }}
          />
          {t('dash.liveEvents')}{' '}
          {hasMessages ? <span className="src-badge">IoT Core</span> : null}
          {iotStats ? (
            <span
              id="mqtt-stats"
              style={{ color: 'var(--text2)', fontSize: '0.65rem', marginLeft: '0.5rem' }}
            >
              {(iotStats.total_messages || 0).toLocaleString() +
                ' messages · ' +
                (iotStats.unique_topics || 0) +
                ' topics'}
            </span>
          ) : null}
        </span>
        <button
          id="feed-pause-btn"
          onClick={() => toggleFeedPause()}
          style={{
            background: 'none',
            border: '1px solid var(--border)',
            color: 'var(--text2)',
            fontSize: '0.65rem',
            cursor: 'pointer',
            padding: '0.15rem 0.5rem',
            borderRadius: 4,
          }}
        >
          {feed.paused ? '▶ Resume' : '⏸ Pause'}
        </button>
      </div>
      <div className="live-feed-scroll" id="live-feed" ref={scrollRef}>
        {emptyState ? (
          <div className="feed-empty" title={emptyState.tooltip}>
            {emptyState.text}
          </div>
        ) : (
          feed.lines.map((l) => (
            <div key={l.id} className="feed-line">
              <span className="feed-ts">{'[' + l.ts + ']'}</span>{' '}
              <span className="feed-topic">{l.topic}</span>
              {' ' + l.val}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
