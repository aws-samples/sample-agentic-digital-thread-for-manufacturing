// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Memory sidebar.
 *
 * Tabs: Sessions (the user's prior conversations — loads even with no active
 * session) / Facts / Summaries / Prefs (session-scoped semantic memory).
 * Collapse state persists under the STABLE localStorage key
 * (MEMORY_COLLAPSED_KEY = 'adt-memory-collapsed'). Fetches go through the
 * stale-while-revalidate cache in memory-cache.ts (30s TTL, in-flight dedupe)
 * — a 401 is deliberately treated as an empty state (no login redirect).
 *
 * Session entries are real <button>s: clicking one rehydrates the session AND
 * opens the chat panel if it is collapsed — routed through
 * window.toggleChatSidebar exactly like the AppShell auto-expand, so
 * ChatSidebar's React state stays the single source of truth.
 *
 * Semantic records (facts/summaries/prefs) carry a small trash affordance —
 * DELETE /api/memory/record after askConfirm(), then cache invalidation + a
 * forced reload of the active tab. */
import { useCallback, useEffect, useRef, useState } from 'react';

import type {
  MemorySession,
  SemanticItem,
  SemanticType,
} from '../api/types/memory';
import { EmptyState, ErrorState, LoadingState } from '../shared/ui/AsyncStates';
import { askConfirm } from '../shared/ui/confirm-store';
import { useI18n } from '../lib/i18n';
import { collapsedDrawerAttrs } from './drawer-attrs';
import {
  deleteMemoryRecord,
  getMemorySemantic,
  getMemorySessions,
} from './memory-cache';

export const MEMORY_COLLAPSED_KEY = 'adt-memory-collapsed';

type MemoryTab = 'sessions' | SemanticType;

/** Tab labels are i18n keys — rendered via t() so they follow setLang. */
const TABS: Array<{ tab: MemoryTab; labelKey: string }> = [
  { tab: 'sessions', labelKey: 'memory.sessions' },
  { tab: 'facts', labelKey: 'memory.facts' },
  { tab: 'summaries', labelKey: 'memory.summaries' },
  { tab: 'preferences', labelKey: 'memory.prefs' },
];

/** Open the chat panel when it is collapsed — the SAME mechanism a user (and
 * the AppShell auto-expand) drives: window.toggleChatSidebar, keyed off the
 * #chat-sidebar collapsed class, so ChatSidebar's React state stays the
 * single source of truth. Exported for tests. */
export function openChatPanelIfCollapsed(): void {
  const sidebar = document.getElementById('chat-sidebar');
  if (sidebar?.classList.contains('collapsed')) window.toggleChatSidebar?.();
}

function fmtTime(ts: string | number | undefined): string {
  if (ts == null) return '';
  try {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

/** Trash affordance for one semantic record (facts/summaries/prefs). */
function DeleteBtn({ onDelete }: { onDelete: () => void }) {
  return (
    <button
      type="button"
      className="memory-del-btn"
      title="Remove this memory record"
      aria-label="Remove this memory record"
      onClick={(e) => {
        e.stopPropagation();
        onDelete();
      }}
    >
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="3 6 5 6 21 6" />
        <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
      </svg>
    </button>
  );
}

/** Facts (semantic strategy): a clean bulleted list. */
function Fact({ item, onDelete }: { item: SemanticItem; onDelete?: () => void }) {
  const text = item.content || item.text || '';
  if (!text) return null;
  const ts = fmtTime(item.timestamp || item.createdAt);
  return (
    <div className="memory-card memory-fact">
      <span className="memory-fact-bullet">•</span>
      <span className="memory-card-text">{text}</span>
      {ts ? <span className="memory-ts">{ts}</span> : null}
      {onDelete ? <DeleteBtn onDelete={onDelete} /> : null}
    </div>
  );
}

/** Preferences (userPreference strategy): statement + category chips. */
function Preference({ item, onDelete }: { item: SemanticItem; onDelete?: () => void }) {
  const s = item.structured || {};
  const pref = s.preference || item.content || '';
  if (!pref) return null;
  const cats = Array.isArray(s.categories) ? s.categories : [];
  return (
    <div className="memory-card memory-pref">
      <span className="memory-card-text">{pref}</span>
      {s.context ? <span className="memory-pref-context">{s.context}</span> : null}
      {cats.length ? (
        <div className="memory-chips">
          {cats.map((c) => (
            <span key={c} className="memory-chip">
              {c}
            </span>
          ))}
        </div>
      ) : null}
      {onDelete ? <DeleteBtn onDelete={onDelete} /> : null}
    </div>
  );
}

/** Render a flattened topic body (newline-delimited; '•' bullets and 'Name:'
 * sub-headings) into readable lines — port of _fmtBody. */
function TopicBody({ body }: { body: string }) {
  return (
    <>
      {String(body)
        .split('\n')
        .map((line, i) => {
          const t = line.trim();
          if (!t) return null;
          if (t.startsWith('•')) {
            return (
              <div key={i} className="memory-body-item">
                {t}
              </div>
            );
          }
          if (/^[^:]{1,60}:$/.test(t)) {
            return (
              <div key={i} className="memory-body-head">
                {t}
              </div>
            );
          }
          return (
            <div key={i} className="memory-body-line">
              {t}
            </div>
          );
        })}
    </>
  );
}

/** Summaries (summary strategy): overview lead line + collapsible topics. */
function Summary({ item, onDelete }: { item: SemanticItem; onDelete?: () => void }) {
  const s = item.structured || {};
  const overview = s.overview || '';
  const topics = Array.isArray(s.topics) ? s.topics : [];
  const ts = fmtTime(item.createdAt);
  return (
    <div className="memory-summary">
      {overview ? <p className="memory-summary-overview">{overview}</p> : null}
      {topics.map((t, i) => {
        const name = (t && t.name) || '';
        const body = (t && t.body) || '';
        if (!name && !body) return null;
        if (!name) {
          return (
            <div key={i} className="memory-summary-plain">
              <TopicBody body={body} />
            </div>
          );
        }
        return (
          <details key={i} className="memory-topic" open={i === 0}>
            <summary className="memory-topic-name">{name}</summary>
            <div className="memory-topic-body">
              <TopicBody body={body} />
            </div>
          </details>
        );
      })}
      {!overview && !topics.length ? (
        <span className="memory-card-text">{item.content || ''}</span>
      ) : null}
      {ts ? <span className="memory-ts">{ts}</span> : null}
      {onDelete ? <DeleteBtn onDelete={onDelete} /> : null}
    </div>
  );
}

type Content =
  | { kind: 'loading' }
  | { kind: 'empty'; msg: string }
  | { kind: 'error' }
  | { kind: 'sessions'; items: MemorySession[] }
  | { kind: 'semantic'; type: SemanticType; items: SemanticItem[] };

export interface MemoryPanelProps {
  /** Bumped by the chat (end of turn / '+ New') — refreshMemory(true). */
  refreshToken: number;
  /** Open (rehydrate) a prior session — shared with the session menu. */
  onRehydrate: (sessionId: string) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

/** The memory sidebar (#memory-sidebar). The collapse toggle button lives in
 * the shared .sidebar-toggles rail (ChatSidebar). */
export function MemoryPanel({
  refreshToken,
  onRehydrate,
  collapsed,
  onToggleCollapsed,
}: MemoryPanelProps) {
  const { t } = useI18n();
  const [tab, setTab] = useState<MemoryTab>('sessions');
  const [content, setContent] = useState<Content>({ kind: 'empty', msg: 'No prior sessions' });
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const load = useCallback((activeTab: MemoryTab, force = false) => {
    setDeleteError(null);
    // Sessions tab lists the USER's prior conversations (not session-scoped).
    if (activeTab !== 'sessions' && !window.currentSessionId) {
      setContent({ kind: 'empty', msg: 'No active session' });
      return;
    }
    setContent({ kind: 'loading' });
    if (activeTab === 'sessions') {
      const apply = (items: MemorySession[]) => {
        if (!items.length) setContent({ kind: 'empty', msg: 'No prior sessions' });
        else setContent({ kind: 'sessions', items });
      };
      getMemorySessions({ force, onFresh: apply })
        .then(apply)
        .catch((err: Error) =>
          setContent(
            err.message === '401' ? { kind: 'empty', msg: 'No prior sessions' } : { kind: 'error' },
          ),
        );
    } else {
      const sid = window.currentSessionId;
      if (!sid) {
        setContent({ kind: 'empty', msg: `No ${activeTab} yet` });
        return;
      }
      const apply = (items: SemanticItem[]) => {
        if (!items.length) setContent({ kind: 'empty', msg: `No ${activeTab} yet` });
        else setContent({ kind: 'semantic', type: activeTab, items });
      };
      getMemorySemantic(sid, activeTab, { force, onFresh: apply })
        .then(apply)
        .catch((err: Error) =>
          setContent(
            err.message === '401'
              ? { kind: 'empty', msg: `No ${activeTab} yet` }
              : { kind: 'error' },
          ),
        );
    }
  }, []);

  /** Remove one semantic record (confirm → DELETE → cache drop → reload). */
  const removeRecord = useCallback(
    async (recordId: string, activeTab: SemanticType) => {
      const remove = await askConfirm({
        key: `memory:remove:${recordId}`,
        title: 'Remove memory record?',
        body: 'The agent will no longer use it.',
        confirmLabel: 'Remove record',
        tone: 'danger',
      });
      if (!remove) return;
      setDeleteError(null);
      deleteMemoryRecord(recordId)
        .then(() => {
          window.showToast?.('Memory record removed');
          load(activeTab, true);
        })
        .catch(() => {
          setDeleteError('Could not remove memory record');
          window.showToast?.('Could not remove memory record');
        });
    },
    [load],
  );

  // Reload on tab switch, external refresh bumps, and un-collapse. A
  // refreshToken bump means the data CHANGED (end of turn / new session /
  // rehydrate) → bypass the cache; tab switches and un-collapse serve
  // cached-within-TTL.
  const lastToken = useRef(refreshToken);
  useEffect(() => {
    const force = refreshToken !== lastToken.current;
    lastToken.current = refreshToken;
    if (!collapsed) load(tab, force);
  }, [tab, refreshToken, collapsed, load]);

  return (
    <div
      className={`memory-sidebar${collapsed ? ' collapsed' : ''}`}
      id="memory-sidebar"
      // Collapsed = out of the Tab order (see drawer-attrs.ts).
      {...collapsedDrawerAttrs(collapsed)}
    >
      <div className="memory-header">
        <span>{t('memory.title')}</span>
        <div style={{ display: 'flex', gap: '0.25rem' }}>
          <button
            onClick={() => load(tab, true)}
            title={t('common.refresh')}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--muted)',
              cursor: 'pointer',
              fontSize: '0.8rem',
            }}
          >
            ↻
          </button>
          <button
            onClick={onToggleCollapsed}
            title={t('common.close')}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--muted)',
              cursor: 'pointer',
              fontSize: '0.8rem',
            }}
          >
            ✕
          </button>
        </div>
      </div>
      <div className="memory-tabs">
        {TABS.map((tabDef) => (
          <button
            key={tabDef.tab}
            className={`memory-tab${tab === tabDef.tab ? ' active' : ''}`}
            data-tab={tabDef.tab}
            onClick={() => setTab(tabDef.tab)}
          >
            {t(tabDef.labelKey)}
          </button>
        ))}
      </div>
      <div className="memory-content" id="memory-content">
        {deleteError ? (
          <ErrorState message={deleteError} compact className="memory-delete-error" />
        ) : null}
        {content.kind === 'loading' && (
          <LoadingState label="Loading memory…" compact className="memory-loading" />
        )}
        {content.kind === 'empty' && (
          <EmptyState message={content.msg} compact className="memory-empty" />
        )}
        {content.kind === 'error' && (
          <ErrorState
            message="Memory unavailable"
            onRetry={() => load(tab, true)}
            compact
            className="memory-empty"
          />
        )}
        {content.kind === 'sessions' &&
          content.items.map((s, i) => {
            const sid = s.sessionId || '';
            const label = s.label || sid || 'Session';
            const active = !!sid && sid === window.currentSessionId;
            const ts = fmtTime(s.createdAt);
            return (
              <button
                key={sid || i}
                type="button"
                className={`memory-event memory-session${active ? ' memory-session--active' : ''}`}
                data-session-id={sid}
                title="Open this conversation"
                onClick={() => {
                  openChatPanelIfCollapsed();
                  onRehydrate(sid);
                }}
              >
                <span className="memory-event-text">{label}</span>
                {ts ? <span className="memory-ts">{ts}</span> : null}
              </button>
            );
          })}
        {content.kind === 'semantic' &&
          content.items.map((item, i) => {
            const onDelete = item.id
              ? () => void removeRecord(item.id as string, content.type)
              : undefined;
            return content.type === 'summaries' ? (
              <Summary key={item.id || i} item={item} onDelete={onDelete} />
            ) : content.type === 'preferences' ? (
              <Preference key={item.id || i} item={item} onDelete={onDelete} />
            ) : (
              <Fact key={item.id || i} item={item} onDelete={onDelete} />
            );
          })}
      </div>
    </div>
  );
}
