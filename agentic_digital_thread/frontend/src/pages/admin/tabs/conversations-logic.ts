// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure Conversations-tab logic (DOM-free, unit-testable under node vitest):
 * the request URL, user labels, rating text and the honest "more" notes the
 * server's bounds produce (agents/admin_conversations.py). */

import type {
  ConversationSession,
  ConversationUser,
  ConversationsPage,
} from '../../../api/types/admin';

/** The `days` choices: 1 to 7, the memory's raw-event retention. */
export const CONVERSATION_DAY_CHOICES = [1, 2, 3, 4, 5, 6, 7] as const;
export const DEFAULT_CONVERSATION_DAYS = 7;

/** The route's `user` parameter shape (agents/admin_conversations.py
 * ACTOR_ID_PATTERN, pinned by tests/unit/test_admin_conversations.py): a
 * typed user id the server would refuse is never sent. */
const USER_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$/;

/** True when `id` is a user id the route accepts. */
export function isValidUserId(id: string): boolean {
  return USER_ID_PATTERN.test(id);
}

/** GET /api/admin/conversations URL; `user` '' means every user. */
export function conversationsUrl(days: number, user: string, cursor?: string | null): string {
  const params = new URLSearchParams({ days: String(days) });
  if (user) params.set('user', user);
  if (cursor) params.set('cursor', cursor);
  return `/api/admin/conversations?${params.toString()}`;
}

/** A login as the page shows it: "username (email)", or the raw id when the
 * user pool has no such user. */
export function userLabel(u: ConversationUser): string {
  if (!u.resolved) return u.user_id;
  if (u.username && u.email && u.email !== u.username) return `${u.username} (${u.email})`;
  return u.username || u.email || u.user_id;
}

/** Union of user lists by id, sorted by label (the filter keeps every user it
 * has seen, so narrowing to one user never empties the choice list). */
export function mergeUsers(
  known: ConversationUser[],
  incoming: ConversationUser[],
): ConversationUser[] {
  const byId = new Map(known.map((u) => [u.user_id, u]));
  for (const u of incoming) byId.set(u.user_id, u);
  return [...byId.values()].sort((a, b) => userLabel(a).localeCompare(userLabel(b)));
}

/** The rating cell: thumbs counted per turn, plus ratings that could not be
 * matched to one question and ratings held back because a newer one may not
 * have been read; "Ratings unavailable" when they could not be read (never
 * the em dash that means "no ratings"). */
export function ratingText(
  s: Pick<ConversationSession, 'rating' | 'unplaced_ratings'> &
    Partial<Pick<ConversationSession, 'unverified_ratings'>>,
): string {
  if (!s.rating) return 'Ratings unavailable';
  const { positive, negative } = s.rating;
  const unplaced = s.unplaced_ratings ?? 0;
  const unverified = s.unverified_ratings ?? 0;
  if (!positive && !negative && !unplaced && !unverified) return '—';
  const parts = [`${positive} up, ${negative} down`];
  if (unplaced) parts.push(`${unplaced} unmatched`);
  if (unverified) parts.push(`${unverified} not shown`);
  return parts.join(', ');
}

/** The table caption. Newest-first order and "N of M" are claimed only for a
 * complete scan; otherwise the count says only how many are shown. */
export function captionText(page: ConversationsPage, shown: number): string {
  if (page.complete) return `${shown} of ${page.matched} sessions, newest first`;
  return `${shown} ${shown === 1 ? 'session' : 'sessions'} shown`;
}

/** The warning for an incomplete scan, naming who was not fully read. */
export function incompleteWarning(page: ConversationsPage | undefined): string | null {
  if (!page || page.complete) return null;
  const parts: string[] = ['This list is incomplete, so it may miss newer sessions.'];
  if (page.incomplete_users.length) {
    parts.push(
      'Not all sessions were read for: ' +
        page.incomplete_users.map(userLabel).join(', ') +
        '. Pick one of them in the User filter to read that user\'s sessions.',
    );
  }
  if (page.more.users) {
    parts.push(
      `Only the first ${page.limits.max_users} users in the user pool were read.`,
    );
  }
  return parts.join(' ');
}

/** A timestamp in the viewer's locale, or an em dash. */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** One sentence per other bound the server reached, so a partial list never
 * reads as the whole. */
export function moreNotes(page: ConversationsPage | undefined): string[] {
  if (!page) return [];
  const notes: string[] = [];
  const warning = incompleteWarning(page);
  if (warning) notes.push(warning);
  if (!page.feedback_available) {
    notes.push(
      'Ratings unavailable: ' + (page.feedback_error?.error || 'the ratings could not be read.'),
    );
  } else if (page.more.feedback) {
    notes.push(
      `Not every rating was read (at most ${page.limits.max_feedback_pages_per_day} pages a day), ` +
        'so some sessions may show fewer ratings than they have.',
    );
  }
  if (!page.logins_available) {
    notes.push('No user pool is configured, so only the local user\'s conversations are listed.');
  }
  if (page.excluded.undated) {
    notes.push(`${page.excluded.undated} session(s) without a start time were left out.`);
  }
  return notes;
}
