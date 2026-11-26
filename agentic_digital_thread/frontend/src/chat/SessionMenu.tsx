// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Keyboard-navigable session menu ('+ New').
 *
 * SPLIT BUTTON: the left '+ New' segment starts a new session in ONE click
 * (the action that was the dropdown's default-highlighted item); the caret
 * segment opens the dropdown — '➕ New Investigation' followed by recent
 * sessions, newest-first and capped at 10 (preferring the last 7 days).
 * Session reads share the memory panel's SWR cache and in-flight request;
 * selecting a session rehydrates it.
 *
 * Accessibility: two real <button>s — the caret carries aria-haspopup /
 * aria-expanded / aria-controls; role=menu container holds focus while open;
 * role=menuitem rows with a roving highlight via aria-activedescendant;
 * ArrowUp/Down wrap, Home/End jump, Enter/Space activate, Esc /
 * outside-click / focus-leave close. */
import { useEffect, useRef, useState } from 'react';

import type { MemorySession } from '../api/types/memory';
import { useI18n } from '../lib/i18n';
import { formatRelativeTime } from '../lib/relative-time';
import { EmptyState, ErrorState, LoadingState } from '../shared/ui/AsyncStates';
import { getMemorySessions } from './memory-cache';

import './session-menu.css';

export const SESSION_MENU_MAX = 10; // cap: 10 most recent
export const SESSION_MENU_WINDOW_MS = 7 * 24 * 60 * 60 * 1000; // prefer last 7 days

/** Best-effort parse of a session's timestamp (backend sends `createdAt`). */
export function sessionTimeMs(s: MemorySession): number | null {
  const raw = s.createdAt || s.timestamp || s.last_updated || s.created_at;
  if (!raw) return null;
  const t = new Date(raw).getTime();
  return isNaN(t) ? null : t;
}

/** Relative-time formatter — 'just now' / 'Nm ago' / 'Nh ago' / 'Nd ago' / 'Nw ago'. */
export function relativeTime(ms: number | null): string {
  return formatRelativeTime(ms, { maxUnit: 'week', empty: '' });
}

/** Cap the (already newest-first) list: prefer the last 7 days when
 * timestamps exist (falling back to plain newest-first), then hard-cap. */
export function capSessions(sessions: MemorySession[]): MemorySession[] {
  const now = Date.now();
  const withTs = sessions.filter((s) => sessionTimeMs(s) != null);
  let pool = sessions;
  if (withTs.length) {
    const recent = withTs.filter((s) => now - (sessionTimeMs(s) as number) <= SESSION_MENU_WINDOW_MS);
    pool = recent.length ? recent : sessions;
  }
  return pool.slice(0, SESSION_MENU_MAX);
}

interface MenuItem {
  kind: 'new' | 'session';
  id: string;
  sessionId?: string;
  label?: string;
  rel?: string;
}

export interface SessionMenuProps {
  onNewSession: () => void;
  /** Production rehydrators resolve whether history was actually applied.
   * `void` remains accepted for inert embedding/test callbacks. */
  onRehydrate: (sessionId: string) => Promise<boolean> | void;
  /** Focus the composer after 'New Investigation'. */
  focusQuestionInput: () => void;
}

export function SessionMenu({ onNewSession, onRehydrate, focusQuestionInput }: SessionMenuProps) {
  const { t } = useI18n();
  const [isOpen, setIsOpen] = useState(false);
  // null = loading state — 'New Investigation' is usable immediately.
  const [sessions, setSessions] = useState<MemorySession[] | null>(null);
  const [sessionsError, setSessionsError] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const items: MenuItem[] = [{ kind: 'new', id: 'session-menu-item-new' }];
  (sessions || []).forEach((s, i) => {
    const sid = s.sessionId || s.session_id || s.id || '';
    if (!sid) return;
    items.push({
      kind: 'session',
      id: 'session-menu-item-' + i,
      sessionId: sid,
      label: s.label || sid,
      rel: relativeTime(sessionTimeMs(s)),
    });
  });

  const loadSessions = (force = false) => {
    setSessions(null);
    setSessionsError(false);
    getMemorySessions({ force, onFresh: (fresh) => setSessions(capSessions(fresh)) })
      .then((data) => setSessions(capSessions(data)))
      .catch((error: Error) => {
        // Preserve the established unauthenticated empty-state contract while
        // making operational failures visible and retryable.
        if (error.message === '401') setSessions([]);
        else setSessionsError(true);
      });
  };

  const open = () => {
    setIsOpen(true);
    setActiveIdx(0); // default highlight on 'New Investigation'
    loadSessions();
  };

  const close = () => setIsOpen(false);

  // Focus the menu container when it opens (don't scroll the chat content).
  useEffect(() => {
    if (isOpen) menuRef.current?.focus({ preventScroll: true });
  }, [isOpen]);

  // Outside-click closes.
  useEffect(() => {
    if (!isOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (menuRef.current && t && !menuRef.current.contains(t) && t !== triggerRef.current) {
        close();
      }
    };
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  }, [isOpen]);

  const setActive = (idx: number) => {
    const clamped = Math.max(0, Math.min(idx, items.length - 1));
    setActiveIdx(clamped);
    const el = document.getElementById(items[clamped]?.id || '');
    el?.scrollIntoView?.({ block: 'nearest' });
  };

  /** Default action — shared by the split '+ New' segment and the menu's
   * 'New Investigation' row (this WAS the dropdown's default-highlighted
   * action; the split button promotes it to one click). */
  const startNewAction = () => {
    onNewSession();
    // Explicit feedback so 'New' never feels dead.
    window.showToast?.('New session started');
    focusQuestionInput();
  };

  const activate = async (idx: number) => {
    const item = items[idx];
    if (!item) return;
    close();
    if (item.kind === 'new') {
      startNewAction();
    } else if (item.kind === 'session' && item.sessionId) {
      const applied = await onRehydrate(item.sessionId);
      if (applied === true) window.showToast?.('Session loaded');
      // False is either a guarded no-op (intentionally silent) or a fetch
      // failure already reported through ChatSidebar's onError hook.
    }
  };

  const onMenuKeyDown = (e: React.KeyboardEvent) => {
    const n = items.length;
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        if (n) setActive((activeIdx + 1) % n);
        break;
      case 'ArrowUp':
        e.preventDefault();
        if (n) setActive((activeIdx - 1 + n) % n);
        break;
      case 'Home':
        e.preventDefault();
        if (n) setActive(0);
        break;
      case 'End':
        e.preventDefault();
        if (n) setActive(n - 1);
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        activate(activeIdx);
        break;
      case 'Escape':
        e.preventDefault();
        close();
        triggerRef.current?.focus();
        break;
      case 'Tab':
        close(); // let focus proceed naturally
        break;
    }
  };

  // Focus leaving the menu subtree (e.g. Tab away) closes it.
  const onFocusOut = (e: React.FocusEvent) => {
    const wrap = (e.currentTarget as HTMLElement).closest('.session-menu-wrap');
    if (isOpen && wrap && !wrap.contains(e.relatedTarget as Node | null)) {
      close();
    }
  };

  return (
    <div className="session-menu-wrap">
      {/* Split button: '+ New' acts immediately; the caret opens the menu. */}
      <button
        id="session-menu-new-btn"
        type="button"
        className="session-menu-trigger session-menu-trigger--new"
        title="Start a new investigation"
        onClick={(e) => {
          e.stopPropagation();
          close();
          startNewAction();
        }}
      >
        {t('chat.new')}
      </button>
      <button
        id="session-menu-trigger"
        ref={triggerRef}
        type="button"
        className="session-menu-trigger session-menu-trigger--caret"
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls="session-menu"
        aria-label="Recent sessions"
        title="Switch to a recent session"
        onClick={(e) => {
          e.stopPropagation();
          if (isOpen) close();
          else open();
        }}
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            open();
          }
        }}
      >
        <span className="session-menu-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      <div
        id="session-menu"
        ref={menuRef}
        className="session-menu"
        role="menu"
        aria-label="Sessions"
        tabIndex={-1}
        hidden={!isOpen}
        aria-activedescendant={items[activeIdx]?.id || ''}
        onKeyDown={onMenuKeyDown}
        onBlur={onFocusOut}
      >
        <button
          type="button"
          role="menuitem"
          tabIndex={-1}
          id="session-menu-item-new"
          className={`session-menu-item session-menu-item--new${activeIdx === 0 ? ' active' : ''}`}
          onMouseDown={(e) => e.preventDefault()}
          onMouseEnter={() => setActive(0)}
          onClick={(e) => {
            e.stopPropagation();
            activate(0);
          }}
        >
          ➕ New Investigation
        </button>
        <div className="session-menu-divider" role="separator" />
        <div className="session-menu-label" role="presentation">
          Recent sessions
        </div>
        {sessionsError ? (
          <ErrorState
            message="Sessions unavailable"
            onRetry={() => loadSessions(true)}
            compact
            className="session-menu-muted"
          />
        ) : sessions === null ? (
          <LoadingState
            label="Loading sessions"
            compact
            className="session-menu-muted"
          />
        ) : items.length === 1 ? (
          <EmptyState
            message="No recent sessions"
            compact
            className="session-menu-muted"
          />
        ) : (
          items.slice(1).map((item, i) => {
            const idx = i + 1;
            return (
              <button
                key={item.id}
                type="button"
                role="menuitem"
                tabIndex={-1}
                id={item.id}
                className={`session-menu-item session-menu-item--session${activeIdx === idx ? ' active' : ''}`}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(idx)}
                onClick={(e) => {
                  e.stopPropagation();
                  activate(idx);
                }}
              >
                <span className="session-menu-item-label">{item.label}</span>
                {item.rel ? <span className="session-menu-item-time">{item.rel}</span> : null}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
