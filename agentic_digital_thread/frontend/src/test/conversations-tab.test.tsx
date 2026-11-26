// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Admin → Conversations tab (GET /api/admin/conversations): rows render, a
 * row expands to its questions and answers (keyboard-reachable button with
 * aria-expanded/aria-controls), the empty and error states, the honest
 * "more" notes, paging with the server's cursor, the user filter, and the
 * sink policy (hostile text stays inert) — against a mocked fetch. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { ConversationSession, ConversationsPage } from '../api/types/admin';
import ConversationsTab from '../pages/admin/tabs/ConversationsTab';
import {
  conversationsUrl,
  isValidUserId,
  mergeUsers,
  moreNotes,
  ratingText,
  userLabel,
} from '../pages/admin/tabs/conversations-logic';
import { ADMIN_TAB_IDS, restoreTabFromHash } from '../pages/admin/tabs/admin-logic';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const HOSTILE = '<img src=x onerror="window.__pwned=1"><script>window.__pwned2=1</script>';

function session(over: Partial<ConversationSession> = {}): ConversationSession {
  return {
    session_id: 'session-b1',
    user: { user_id: 'sub-b', username: 'bob', email: 'bob@example.com', resolved: true },
    started_at: '2026-10-21T13:00:00+00:00',
    last_activity_at: '2026-10-21T13:00:03+00:00',
    turn_count: 1,
    turns: [
      {
        question: 'Which lots shipped late?',
        asked_at: '2026-10-21T13:00:00+00:00',
        answer: 'Two lots **shipped late**.',
        answered_at: '2026-10-21T13:00:03+00:00',
        answer_withheld: false,
        question_truncated: false,
        answer_truncated: false,
        feedback: { sentiment: 'negative', comment: 'missing lot 7', at: '2026-10-21T13:01:00+00:00' },
      },
    ],
    rating: { positive: 0, negative: 1 },
    unplaced_ratings: 0,
    unverified_ratings: 0,
    events_truncated: false,
    ...over,
  };
}

function page(over: Partial<ConversationsPage> = {}): ConversationsPage {
  return {
    sessions: [session()],
    next: null,
    matched: 1,
    window: { days: 7, start: '2026-10-14T00:00:00+00:00', end: '2026-10-21T00:00:00+00:00' },
    users: [session().user],
    logins_available: true,
    complete: true,
    scope: 'current_app_users',
    incomplete_users: [],
    feedback_available: true,
    feedback_error: null,
    more: { users: false, feedback: false },
    excluded: { automated: 0, undated: 0 },
    limits: {
      max_users: 600,
      max_session_pages: 40,
      session_page_size: 100,
      max_events_per_session: 400,
      max_page_size: 25,
      max_days: 7,
      max_feedback_pages_per_day: 5,
      feedback_page_size: 100,
    },
    ...over,
  };
}

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Service Unavailable' : 'OK',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

let container: HTMLDivElement;
let root: Root;
let calls: string[];

function stubFetch(handler: (url: string) => Response) {
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      return handler(url);
    }),
  );
}

async function settle() {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

async function mount() {
  const client = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <ConversationsTab />
      </QueryClientProvider>,
    );
  });
  await settle();
}

function buttons(text: string): HTMLButtonElement[] {
  return [...container.querySelectorAll('button')].filter((b) => b.textContent?.trim() === text);
}

/** Set a controlled input's value the way a user typing does. */
function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  delete (window as { __pwned?: number }).__pwned;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ConversationsTab', () => {
  it('renders one row per session with user, turns and rating', async () => {
    stubFetch(() => response(page()));
    await mount();
    expect(calls[0]).toBe('/api/admin/conversations?days=7');
    const rows = container.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('bob (bob@example.com)');
    expect(rows[0].textContent).toContain('0 up, 1 down');
    expect(container.textContent).toContain('last 7 days');
    expect(container.textContent).toContain('AWS Quick');
    expect(container.textContent).toContain('removed from the user pool are not listed');
    expect(container.textContent).toContain('enter it in User id');
  });

  it('expands a row to its question, answer and feedback, keyboard-reachable', async () => {
    stubFetch(() => response(page()));
    await mount();
    const toggle = buttons('Show')[0];
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-label')).toContain('bob (bob@example.com)');
    await act(async () => toggle.click());
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const detail = container.querySelector(`#${toggle.getAttribute('aria-controls')}`)!;
    expect(detail.textContent).toContain('Which lots shipped late?');
    expect(detail.querySelector('strong')?.textContent).toBe('shipped late');
    expect(detail.textContent).toContain('Thumbs down');
    expect(detail.textContent).toContain('missing lot 7');
    await act(async () => toggle.click());
    expect(container.querySelector(`#${toggle.getAttribute('aria-controls')}`)).toBeNull();
  });

  it('keeps hostile question, answer and comment text inert', async () => {
    const hostile = session({
      turns: [{ ...session().turns[0], question: HOSTILE, answer: HOSTILE,
        feedback: { sentiment: 'negative', comment: HOSTILE } }],
    });
    stubFetch(() => response(page({ sessions: [hostile] })));
    await mount();
    await act(async () => buttons('Show')[0].click());
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('<img src=x');
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('says when an answer is withheld or missing and when a session is cut', async () => {
    const turns = [
      { ...session().turns[0], answer: null, answer_withheld: true, feedback: null },
      { ...session().turns[0], answer: null, answer_withheld: false, feedback: null },
    ];
    stubFetch(() => response(page({ sessions: [session({ turns, turn_count: 2, events_truncated: true })] })));
    await mount();
    await act(async () => buttons('Show')[0].click());
    expect(container.textContent).toContain('its answer is not saved');
    expect(container.textContent).toContain('No answer was saved for this question.');
    expect(container.textContent).toContain('earliest questions are not shown');
  });

  it('shows an empty state when no session is in the window', async () => {
    stubFetch(() => response(page({ sessions: [], users: [], matched: 0 })));
    await mount();
    expect(container.querySelector('[role="status"]')?.textContent).toContain(
      'No web-chat conversations in the last 7 days',
    );
    expect(container.querySelector('table')).toBeNull();
  });

  it('shows the server error with a Retry button, never an empty list', async () => {
    stubFetch(() => response({ detail: 'The conversation memory could not be read.', source: 'memory' }, 503));
    await mount();
    const alert = container.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('The conversation memory could not be read.');
    expect(container.textContent).not.toContain('No web-chat conversations');
    expect(calls).toHaveLength(1); // no automatic retry storm
    stubFetch(() => response(page()));
    await act(async () => buttons('Retry')[0].click());
    await settle();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(1);
  });

  it('loads the next page with the server cursor', async () => {
    const second = session({ session_id: 'session-a1',
      user: { user_id: 'sub-a', username: 'alice', email: 'alice@example.com', resolved: true } });
    stubFetch((url) =>
      response(url.includes('cursor=') ? page({ sessions: [second], matched: 2 }) : page({ next: 'CUR1', matched: 2 })),
    );
    await mount();
    await act(async () => buttons('Load more')[0].click());
    await settle();
    expect(calls[1]).toBe('/api/admin/conversations?days=7&cursor=CUR1');
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
    expect(buttons('Load more')).toHaveLength(0);
  });

  it('filters by user and window through labelled controls', async () => {
    stubFetch(() => response(page()));
    await mount();
    const userSelect = container.querySelector<HTMLSelectElement>('#conversations-user')!;
    const daysSelect = container.querySelector<HTMLSelectElement>('#conversations-days')!;
    expect(container.querySelector('label[for="conversations-user"]')).not.toBeNull();
    expect(container.querySelector('label[for="conversations-days"]')).not.toBeNull();
    await act(async () => {
      userSelect.value = 'sub-b';
      userSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await settle();
    expect(calls.at(-1)).toBe('/api/admin/conversations?days=7&user=sub-b');
    await act(async () => {
      daysSelect.value = '3';
      daysSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await settle();
    expect(calls.at(-1)).toBe('/api/admin/conversations?days=3&user=sub-b');
  });

  it('reaches a former user by a typed user id', async () => {
    stubFetch(() => response(page()));
    await mount();
    const input = container.querySelector<HTMLInputElement>('#conversations-user-id')!;
    expect(container.querySelector('label[for="conversations-user-id"]')?.textContent).toBe('User id');
    typeInto(input, 'sub-removed-1');
    await act(async () => buttons('Show user')[0].click());
    await settle();
    expect(calls.at(-1)).toBe('/api/admin/conversations?days=7&user=sub-removed-1');
    const select = container.querySelector<HTMLSelectElement>('#conversations-user')!;
    expect(select.value).toBe('sub-removed-1');
    expect(select.selectedOptions[0].textContent).toBe('sub-removed-1 (entered id)');
  });

  it('refuses a typed user id the server would reject, without a request', async () => {
    stubFetch(() => response(page()));
    await mount();
    const before = calls.length;
    const input = container.querySelector<HTMLInputElement>('#conversations-user-id')!;
    typeInto(input, 'bad id; drop');
    await act(async () => buttons('Show user')[0].click());
    await settle();
    expect(calls.length).toBe(before);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    const error = container.querySelector('#conversations-user-id-error')!;
    expect(input.getAttribute('aria-describedby')).toBe('conversations-user-id-error');
    expect(error.getAttribute('role')).toBe('alert');
    expect(error.textContent).toContain('letters, digits');
  });

  it('claims newest-first order and a full count only for a complete scan', async () => {
    stubFetch(() => response(page()));
    await mount();
    expect(container.querySelector('caption')?.textContent).toBe('1 of 1 sessions, newest first');
    expect(container.querySelector('[role="note"]')).toBeNull();
  });

  it('names the users not fully read and drops the completeness claims', async () => {
    const carol = { user_id: 'sub-c', username: 'carol', email: 'carol@example.com', resolved: true };
    stubFetch(() => response(page({ complete: false, incomplete_users: [carol], matched: 1 })));
    await mount();
    const warning = container.querySelector('[role="note"]')!;
    expect(warning.textContent).toContain('This list is incomplete');
    expect(warning.textContent).toContain('carol (carol@example.com)');
    expect(warning.textContent).toContain('User filter');
    const caption = container.querySelector('caption')!.textContent!;
    expect(caption).not.toContain('newest first');
    expect(caption).not.toContain(' of ');
    expect(container.textContent).not.toContain('newest first');
  });

  it('says when the user pool was not all read', async () => {
    stubFetch(() => response(page({ complete: false, more: { users: true, feedback: false } })));
    await mount();
    expect(container.querySelector('[role="note"]')?.textContent).toContain('first 600 users');
    expect(container.querySelector('caption')?.textContent).not.toContain('newest first');
  });

  it('shows ratings unavailable, never no ratings, when feedback could not be read', async () => {
    stubFetch(() =>
      response(page({
        feedback_available: false,
        feedback_error: { error: 'An upstream service returned an error. Reference: abc123' },
        sessions: [session({ rating: null, turns: [{ ...session().turns[0], feedback: null }] })],
      })),
    );
    await mount();
    const row = container.querySelector('tbody tr')!;
    expect(row.textContent).toContain('Ratings unavailable');
    expect(row.textContent).not.toContain('—');
    expect(container.querySelector('[role="note"]')?.textContent).toContain('Reference: abc123');
  });

  it('says when ratings were not all read', async () => {
    stubFetch(() => response(page({ more: { users: false, feedback: true } })));
    await mount();
    expect(container.querySelector('[role="note"]')?.textContent).toContain('Not every rating');
  });

  it('shows ratings that could not be matched to one question', async () => {
    stubFetch(() =>
      response(page({ sessions: [session({ unplaced_ratings: 2, rating: { positive: 0, negative: 0 } })] })),
    );
    await mount();
    expect(container.querySelector('tbody tr')!.textContent).toContain('2 unmatched');
    await act(async () => buttons('Show')[0].click());
    expect(container.textContent).toContain('2 ratings in this session could not be matched to one question');
  });

  it('shows ratings held back because a newer one may not have been read', async () => {
    stubFetch(() =>
      response(page({ sessions: [session({ unverified_ratings: 1, rating: { positive: 0, negative: 0 } })] })),
    );
    await mount();
    expect(container.querySelector('tbody tr')!.textContent).toContain('1 not shown');
    await act(async () => buttons('Show')[0].click());
    expect(container.textContent).toContain('1 rating in this session is not shown');
  });
});

describe('conversations-logic', () => {
  it('accepts exactly the user ids the server accepts', () => {
    expect(isValidUserId('sub-removed-1')).toBe(true);
    expect(isValidUserId('Federate_ab:cd_12')).toBe(true);
    expect(isValidUserId('a'.repeat(128))).toBe(true);
    for (const bad of ['', 'bad id', 'x;drop', '-leading', '../etc', 'a'.repeat(129)]) {
      expect(isValidUserId(bad)).toBe(false);
    }
  });

  it('builds the URL with only the set filters, encoded', () => {
    expect(conversationsUrl(7, '')).toBe('/api/admin/conversations?days=7');
    expect(conversationsUrl(2, 'sub a', 'c/1+')).toBe('/api/admin/conversations?days=2&user=sub+a&cursor=c%2F1%2B');
  });

  it('labels an unresolved user by its id', () => {
    expect(userLabel({ user_id: 'sub-x', username: null, email: null, resolved: false })).toBe('sub-x');
    expect(userLabel({ user_id: 'sub-b', username: 'bob', email: 'bob', resolved: true })).toBe('bob');
  });

  it('merges user lists by id and shows a dash when no thumbs', () => {
    const a = { user_id: 'a', username: 'zed', email: null, resolved: true };
    const b = { user_id: 'b', username: 'amy', email: null, resolved: true };
    expect(mergeUsers([a], [b, a]).map((u) => u.user_id)).toEqual(['b', 'a']);
    expect(ratingText({ rating: { positive: 0, negative: 0 }, unplaced_ratings: 0 })).toBe('—');
    expect(ratingText({ rating: null, unplaced_ratings: 0 })).toBe('Ratings unavailable');
    expect(ratingText({ rating: { positive: 1, negative: 0 }, unplaced_ratings: 1 })).toBe('1 up, 0 down, 1 unmatched');
    expect(moreNotes(undefined)).toEqual([]);
  });

  it('is a contractual admin tab restorable from its hash', () => {
    expect(ADMIN_TAB_IDS).toContain('tab-conversations');
    expect(restoreTabFromHash('#tab-conversations', false, 'hidden')).toBe('tab-conversations');
  });
});
