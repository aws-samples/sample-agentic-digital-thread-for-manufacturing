// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Notifications store — the header bell's tiny external store (createStore/
 * useStore pattern, lib/store.ts). Read-only mirror of GET /api/notifications:
 * pending learning proposals + in-flight/recently-finished async jobs.
 *
 * refreshNotifications() is the ONLY writer; it is invoked by the bell's
 * visibility-aware 30 s poll and on dropdown open. Fetch failures are
 * fail-soft: the last snapshot is kept and the next poll retries. */
import { requestJson } from '../../api/client';
import { createStore } from '../../lib/store';

/** One feed item (server contract — agents/notification_routes.py). */
export interface NotificationItem {
  kind: string;
  label?: string;
  count?: number;
  state?: string;
  system_id?: string;
  started_at?: string;
  href?: string;
  /** Present when the SOURCE failed server-side (fail-soft item). */
  error?: string;
}

export interface NotificationsState {
  items: NotificationItem[];
  /** True once at least one fetch has succeeded. */
  loaded: boolean;
}

export const notificationsStore = createStore<NotificationsState>({
  items: [],
  loaded: false,
});

/** Fetch the feed and publish it. Fail-soft on any error. */
export async function refreshNotifications(): Promise<void> {
  try {
    const data = await requestJson<{ items?: unknown }>('/api/notifications');
    const items = Array.isArray(data.items)
      ? (data.items as NotificationItem[])
      : [];
    notificationsStore.set({ items, loaded: true });
  } catch {
    // Keep the last snapshot; the 30 s poll retries.
  }
}

/** Badge total: the NUMBER OF NOTIFICATIONS needing attention — each
 * non-error item contributes at most 1. A proposals item with 534 pending is
 * ONE notification (the dropdown row carries the real count); counting the
 * pending rows made the badge read 99+ over a single dropdown entry. A
 * questions item ("the system has N questions for you", ADR-0059 D5) follows
 * the same rule. An item with a MEASURED zero count needs nothing (0); an
 * item with NO count key is still a notification (absence ≠ measured zero).
 * Finished jobs and source-error items inform but do not count as "needs
 * attention". */
export function attentionCount(items: NotificationItem[]): number {
  let total = 0;
  for (const item of items) {
    if (item.error) continue;
    if (item.kind === 'proposals' || item.kind === 'questions') {
      total += item.count === 0 ? 0 : 1;
    } else if (item.state === 'running') total += 1;
  }
  return total;
}
