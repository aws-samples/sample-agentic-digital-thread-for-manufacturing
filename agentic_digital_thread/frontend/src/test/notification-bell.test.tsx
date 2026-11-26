// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — NotificationBell (header bell, v1 read-only aggregation):
 * badge total from the mocked /api/notifications payload (each non-error
 * item needing attention counts as ONE notification; finished/error-source
 * items don't count), dropdown groups ('Needs review' / 'In progress' /
 * 'Recently finished' with state coloring classes), the empty state, and
 * the refetch-on-open contract. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import NotificationBell from '../pages/app/NotificationBell';
import {
  attentionCount,
  type NotificationItem,
  notificationsStore,
} from '../pages/app/notifications-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let fetchMock: ReturnType<typeof vi.fn>;

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

const PAYLOAD: { items: NotificationItem[] } = {
  items: [
    { kind: 'proposals', label: '3 learning proposals awaiting review', count: 3, href: '/admin.html#tab-vocabulary' },
    { kind: 'removal', label: 'Removing system erp', state: 'running', system_id: 'erp', href: '/#onboard' },
    { kind: 'import', label: 'Graph snapshot import finished', state: 'done', href: '/admin.html' },
    { kind: 'repair', label: 'Graph reconcile repair failed', state: 'error', href: '/admin.html' },
  ],
};

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  notificationsStore.set({ items: [], loaded: false });
  fetchMock = vi.fn(async () => ok(PAYLOAD));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function mountBell() {
  await act(async () => {
    root.render(<NotificationBell />);
  });
  // Flush the mount-time refreshNotifications() promise chain.
  await act(async () => {});
}

async function openDropdown() {
  const trigger = document.getElementById('notification-bell-trigger')!;
  await act(async () => {
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await act(async () => {});
}

describe('NotificationBell', () => {
  it('renders the badge counting NOTIFICATIONS (1 proposals item + 1 running job = 2)', async () => {
    await mountBell();
    const badge = document.getElementById('notification-badge');
    expect(badge).not.toBeNull();
    expect(badge!.textContent).toBe('2');
  });

  it('a large proposal count is still ONE notification — never a 99+ badge over one row', async () => {
    fetchMock.mockImplementation(async () =>
      ok({
        items: [
          { kind: 'proposals', label: '534 learning proposals awaiting review', count: 534, href: '/admin.html#tab-vocabulary' },
        ],
      }),
    );
    await mountBell();
    expect(document.getElementById('notification-badge')?.textContent).toBe('1');
  });

  it('hides the badge when nothing needs attention', async () => {
    fetchMock.mockImplementation(async () => ok({ items: [] }));
    await mountBell();
    expect(document.getElementById('notification-badge')).toBeNull();
  });

  it('renders the grouped dropdown with state coloring', async () => {
    await mountBell();
    await openDropdown();
    const dropdown = document.getElementById('notification-dropdown')!;
    expect(dropdown.hidden).toBe(false);

    const sections = [...dropdown.querySelectorAll('.notif-section')].map(
      (el) => el.textContent,
    );
    expect(sections).toEqual(['Needs review', 'In progress', 'Recently finished']);

    // Needs review links to the Vocabulary tab.
    const review = dropdown.querySelector('a[href="/admin.html#tab-vocabulary"]');
    expect(review?.textContent).toContain('3 learning proposals awaiting review');

    // In progress carries the system id label and running state.
    expect(dropdown.querySelector('.notif-state-running')?.textContent).toBe('running');
    expect(dropdown.textContent).toContain('Removing system erp');

    // Recently finished: done + error coloring classes.
    expect(dropdown.querySelector('.notif-state-done')?.textContent).toBe('done');
    expect(dropdown.querySelector('.notif-state-error')?.textContent).toBe('error');
  });

  it("renders kind='questions' in Needs review with its own icon and the answering-surface href", async () => {
    fetchMock.mockImplementation(async () =>
      ok({
        items: [
          { kind: 'proposals', label: '3 learning proposals awaiting review', count: 3, href: '/admin.html#tab-vocabulary' },
          { kind: 'questions', label: 'The system has 2 question(s) for you', count: 2, href: '/#onboard/questions' },
          { kind: 'removal', label: 'Removing system erp', state: 'running', system_id: 'erp', href: '/#onboard' },
        ],
      }),
    );
    await mountBell();
    await openDropdown();
    const dropdown = document.getElementById('notification-dropdown')!;

    // The questions item links to the answering surface…
    const link = dropdown.querySelector('a[href="/#onboard/questions"]')!;
    expect(link).not.toBeNull();
    expect(link.textContent).toContain('The system has 2 question(s) for you');
    // …carries its own visual identity (the ? prefix — proposals rows don't)…
    expect(link.querySelector('.notif-item-icon-question')).not.toBeNull();
    const proposals = dropdown.querySelector('a[href="/admin.html#tab-vocabulary"]')!;
    expect(proposals.querySelector('.notif-item-icon-question')).toBeNull();
    // …and sits in the Needs review group (both review rows precede the
    // In progress section header).
    const children = [...dropdown.children];
    const needsReviewIdx = children.findIndex((el) => el.textContent === 'Needs review');
    const inProgressIdx = children.findIndex((el) => el.textContent === 'In progress');
    const questionIdx = children.indexOf(link);
    expect(needsReviewIdx).toBeGreaterThanOrEqual(0);
    expect(questionIdx).toBeGreaterThan(needsReviewIdx);
    expect(questionIdx).toBeLessThan(inProgressIdx);

    // Badge: proposals + questions + running = 3 notifications.
    expect(document.getElementById('notification-badge')?.textContent).toBe('3');
  });

  it('shows the empty state when the feed is empty', async () => {
    fetchMock.mockImplementation(async () => ok({ items: [] }));
    await mountBell();
    await openDropdown();
    const dropdown = document.getElementById('notification-dropdown')!;
    expect(dropdown.textContent).toContain('Nothing needs your attention');
    expect(dropdown.querySelectorAll('.notif-section')).toHaveLength(0);
  });

  it('fetches on mount and refetches when the dropdown opens', async () => {
    await mountBell();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/notifications');
    await openDropdown();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('surfaces server-side fail-soft error items as a muted note', async () => {
    fetchMock.mockImplementation(async () =>
      ok({
        items: [
          { kind: 'proposals', error: 'RuntimeError: graph unavailable' },
          { kind: 'removal', label: 'Removing system mes', state: 'running', system_id: 'mes', href: '/#onboard' },
        ],
      }),
    );
    await mountBell();
    await openDropdown();
    const dropdown = document.getElementById('notification-dropdown')!;
    expect(dropdown.querySelector('.notif-unavailable')?.textContent).toBe(
      'Sources unavailable: proposals',
    );
    // The healthy source still renders — fail-soft never empties the feed.
    expect(dropdown.textContent).toContain('Removing system mes');
    // Error items don't count toward the badge (1 running job only).
    expect(document.getElementById('notification-badge')?.textContent).toBe('1');
  });
});

describe('attentionCount', () => {
  it('counts each non-error item needing attention as ONE notification', () => {
    // 1 proposals item (count 3 → still one notification) + 1 running job.
    expect(attentionCount(PAYLOAD.items)).toBe(2);
    expect(attentionCount([])).toBe(0);
    expect(attentionCount([{ kind: 'proposals', error: 'x' }])).toBe(0);
    // Absent count is still a notification (absence ≠ measured zero)…
    expect(attentionCount([{ kind: 'proposals' }])).toBe(1);
    // …but a MEASURED zero needs no attention.
    expect(attentionCount([{ kind: 'proposals', count: 0 }])).toBe(0);
    // A huge pending count is still one notification.
    expect(attentionCount([{ kind: 'proposals', count: 534 }])).toBe(1);
  });

  it("counts a 'questions' item like a proposals item (one notification; measured zero counts nothing)", () => {
    expect(attentionCount([{ kind: 'questions', count: 2 }])).toBe(1);
    expect(attentionCount([{ kind: 'questions' }])).toBe(1);
    expect(attentionCount([{ kind: 'questions', count: 0 }])).toBe(0);
    expect(attentionCount([{ kind: 'questions', error: 'x' }])).toBe(0);
    expect(
      attentionCount([
        { kind: 'proposals', count: 3 },
        { kind: 'questions', count: 2 },
      ]),
    ).toBe(2);
  });
});
