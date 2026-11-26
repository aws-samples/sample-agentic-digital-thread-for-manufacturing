// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* NotificationBell — persistent header affordance for what needs steward
 * attention (v1: read-only aggregation of GET /api/notifications; no
 * per-user read-state).
 *
 * Bell trigger + badge (one per notification needing attention — a proposals
 * item with many pending rows is still ONE notification) and a
 * right-anchored dropdown grouping the feed: 'Needs review' (proposal counts
 * → Vocabulary tab), 'In progress' (running jobs with system ids),
 * 'Recently finished' (done/error with state coloring), plus a muted
 * 'Sources unavailable' note for server-side fail-soft error items.
 *
 * Polling: every 30 s while the tab is visible (visibilitychange-aware —
 * hidden tabs stop polling, and becoming visible refreshes immediately);
 * opening the dropdown also refetches. Open/close behavior mirrors
 * UserMenu.tsx: Escape closes and returns focus to the trigger; outside
 * click closes; in-panel clicks keep it open. Plain React rendering
 * throughout — zero innerHTML (frontend sink policy). */
import { useEffect, useRef, useState } from 'react';

import {
  attentionCount,
  type NotificationItem,
  notificationsStore,
  refreshNotifications,
} from './notifications-store';
import { useStore } from '../../lib/store';
import { catalogSystemName, useSystemNamesVersion } from '../../lib/system-names';
import './notification-bell.css';

const POLL_MS = 30_000;

const REMOVAL_LABELS: Record<string, (system: string) => string> = {
  running: (system) => `Removing ${system}`,
  done: (system) => `Removal of ${system} finished`,
  error: (system) => `Removal of ${system} failed`,
};

/** The row text a person reads. A system-removal row names the system by its
 * catalog name with the id after it ("Removing Plant MES (mes-a)"): a removal
 * runs in the background, so the exact id stays visible. Every other row, and
 * a removal whose system the catalog does not know, keeps the server's label
 * (agents/notification_routes.py). */
export function notificationLabel(item: NotificationItem): string {
  const sid = item.system_id;
  const compose = item.kind === 'removal' && item.state ? REMOVAL_LABELS[item.state] : undefined;
  const name = compose && sid ? catalogSystemName(sid) : null;
  if (compose && sid && name && name !== sid) return compose(`${name} (${sid})`);
  return item.label ?? item.kind;
}

function ItemRow({ item }: { item: NotificationItem }) {
  const meta = item.state ? (
    <span className={`notif-state notif-state-${item.state}`}>{item.state}</span>
  ) : item.count != null ? (
    <span className="notif-meta">{item.count}</span>
  ) : null;
  // 'questions' items carry their own visual identity (ADR-0059 D5): a
  // question-mark prefix distinguishes "the system has questions for you"
  // from the proposals-await-review rows sharing the Needs review group.
  const icon =
    item.kind === 'questions' ? (
      <span
        className="notif-item-icon notif-item-icon-question"
        aria-hidden="true"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: '1rem',
          height: '1rem',
          marginRight: '0.4rem',
          borderRadius: '50%',
          border: '1px solid currentColor',
          fontSize: '0.7rem',
          fontWeight: 700,
          flex: 'none',
        }}
      >
        ?
      </span>
    ) : null;
  const body = (
    <>
      {icon}
      <span className="notif-item-label">{notificationLabel(item)}</span>
      {meta}
    </>
  );
  if (item.href) {
    return (
      <a className="notif-item" role="menuitem" href={item.href}>
        {body}
      </a>
    );
  }
  return (
    <div className="notif-item" role="menuitem">
      {body}
    </div>
  );
}

function Group({ title, items }: { title: string; items: NotificationItem[] }) {
  if (items.length === 0) return null;
  return (
    <>
      <div className="notif-section">{title}</div>
      {items.map((item, i) => (
        <ItemRow key={`${item.kind}:${item.system_id ?? ''}:${i}`} item={item} />
      ))}
    </>
  );
}

export default function NotificationBell() {
  // Removal rows name systems: re-render when catalog names arrive.
  useSystemNamesVersion();
  const [open, setOpen] = useState(false);
  const { items } = useStore(notificationsStore);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  // Visibility-aware 30 s poll: refresh immediately on mount/visible, stop
  // while the tab is hidden.
  useEffect(() => {
    let timer: number | null = null;
    const start = () => {
      if (timer != null) return;
      void refreshNotifications();
      timer = window.setInterval(() => void refreshNotifications(), POLL_MS);
    };
    const stop = () => {
      if (timer != null) {
        window.clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') start();
      else stop();
    };
    onVisibility();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  // Escape closes (focus back to trigger); outside click closes — the same
  // contract as UserMenu.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (
        target &&
        !dropdownRef.current?.contains(target) &&
        !triggerRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('click', onClick);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('click', onClick);
    };
  }, [open]);

  const toggle = () => {
    setOpen((o) => {
      if (!o) void refreshNotifications(); // refetch on open
      return !o;
    });
  };

  const count = attentionCount(items);
  // Needs review = things awaiting a steward: pending proposals AND pending
  // runtime steward questions (kind='questions' — rendered with its own
  // question-mark identity, linking to the answering surface).
  const needsReview = items.filter(
    (i) => !i.error && (i.kind === 'proposals' || i.kind === 'questions'),
  );
  const inProgress = items.filter((i) => !i.error && i.state === 'running');
  const finished = items.filter(
    (i) => !i.error && (i.state === 'done' || i.state === 'error'),
  );
  const unavailable = items.filter((i) => i.error);
  const empty =
    needsReview.length + inProgress.length + finished.length === 0;

  return (
    <div className="notif-bell" id="notification-bell">
      <button
        id="notification-bell-trigger"
        className="notif-bell-trigger"
        type="button"
        ref={triggerRef}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="notification-dropdown"
        aria-label={
          count > 0 ? `Notifications (${count} need attention)` : 'Notifications'
        }
        title="Notifications"
        onClick={toggle}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {count > 0 && (
          <span className="notif-badge" id="notification-badge">
            {count > 99 ? '99+' : count}
          </span>
        )}
      </button>
      <div
        id="notification-dropdown"
        className="notif-dropdown"
        role="menu"
        aria-labelledby="notification-bell-trigger"
        ref={dropdownRef}
        hidden={!open}
      >
        {empty ? (
          <div className="notif-empty">Nothing needs your attention</div>
        ) : (
          <>
            <Group title="Needs review" items={needsReview} />
            <Group title="In progress" items={inProgress} />
            <Group title="Recently finished" items={finished} />
          </>
        )}
        {unavailable.length > 0 && (
          <div className="notif-unavailable" role="note">
            {`Sources unavailable: ${unavailable.map((i) => i.kind).join(', ')}`}
          </div>
        )}
      </div>
    </div>
  );
}
