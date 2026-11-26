// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Admin → Conversations tab: every app user's web-chat questions and answers
 * from AgentCore Memory (GET /api/admin/conversations, admin only). A table of
 * sessions (user, start, turns, rating) with a user filter and a window of 1
 * to 7 days; a row expands to its questions and answers. Order and count are
 * claimed only when the server read every listed user's sessions (`complete`);
 * otherwise a warning names the users that were not fully read.
 *
 * Sink policy: questions and comments render as JSX text; answers render
 * through <Md/> (react-markdown, which never parses raw HTML), the chat's own
 * markdown component. Nothing here is an HTML string. */
import { useInfiniteQuery } from '@tanstack/react-query';
import { useEffect, useState, type CSSProperties, type FormEvent } from 'react';

import { ApiError } from '../../../api/client';
import type {
  ConversationSession,
  ConversationsPage,
  ConversationTurn,
  ConversationUser,
} from '../../../api/types/admin';
import { Md } from '../../../shared/ui/Md';
import { adminConversationsQueryOptions } from '../conversations-queries';
import {
  captionText,
  CONVERSATION_DAY_CHOICES,
  DEFAULT_CONVERSATION_DAYS,
  formatWhen,
  isValidUserId,
  mergeUsers,
  moreNotes,
  ratingText,
  userLabel,
} from './conversations-logic';

const MUTED = 'var(--muted)';
const SECTION_LABEL: CSSProperties = {
  fontSize: '0.72rem',
  fontWeight: 600,
  color: MUTED,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
  margin: '0.6rem 0 0.2rem',
};

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

function sessionKey(s: ConversationSession): string {
  return `${s.user.user_id}/${s.session_id}`;
}

function FeedbackLine({ turn }: { turn: ConversationTurn }) {
  const fb = turn.feedback;
  if (!fb) return null;
  const positive = fb.sentiment === 'positive';
  return (
    <p style={{ margin: '0.3rem 0 0' }}>
      <span className={'badge ' + (positive ? 'pos' : 'neg')}>
        {positive ? 'Thumbs up' : fb.sentiment === 'negative' ? 'Thumbs down' : 'Feedback'}
      </span>
      {fb.comment ? <span style={{ marginLeft: '0.5rem' }}>{fb.comment}</span> : null}
    </p>
  );
}

function TurnView({ turn, index }: { turn: ConversationTurn; index: number }) {
  return (
    <li style={{ padding: '0.6rem 0', borderTop: index ? '1px solid var(--border)' : 'none' }}>
      <div style={SECTION_LABEL}>
        Question · <time dateTime={turn.asked_at}>{formatWhen(turn.asked_at)}</time>
      </div>
      <p style={{ whiteSpace: 'pre-wrap', margin: 0, color: 'var(--text)' }}>{turn.question}</p>
      {turn.question_truncated && <p style={{ color: MUTED }}>(The question is cut here.)</p>}
      <div style={SECTION_LABEL}>Answer</div>
      {turn.answer_withheld ? (
        <p style={{ color: MUTED, margin: 0 }}>
          Not shown: the response-integrity check ran for this question, and its answer is not saved.
        </p>
      ) : turn.answer ? (
        <Md text={turn.answer} className="conversation-answer" />
      ) : (
        <p style={{ color: MUTED, margin: 0 }}>No answer was saved for this question.</p>
      )}
      {turn.answer_truncated && <p style={{ color: MUTED }}>(The answer is cut here.)</p>}
      <FeedbackLine turn={turn} />
    </li>
  );
}

function SessionRows({
  session,
  open,
  onToggle,
}: {
  session: ConversationSession;
  open: boolean;
  onToggle: () => void;
}) {
  const detailId = `conversation-${session.session_id}`;
  const label = userLabel(session.user);
  return (
    <>
      <tr>
        <td>
          <button
            type="button"
            aria-expanded={open}
            aria-controls={detailId}
            aria-label={`${open ? 'Hide' : 'Show'} the conversation from ${label}, started ${formatWhen(session.started_at)}`}
            onClick={onToggle}
          >
            {open ? 'Hide' : 'Show'}
          </button>
        </td>
        <td className={session.user.resolved ? undefined : 'mono'}>{label}</td>
        <td className="mono">{formatWhen(session.started_at)}</td>
        <td className="mono">{formatWhen(session.last_activity_at)}</td>
        <td>{session.turn_count}</td>
        <td>{ratingText(session)}</td>
      </tr>
      {open && (
        <tr id={detailId}>
          <td colSpan={6}>
            <SessionDetail session={session} />
          </td>
        </tr>
      )}
    </>
  );
}

function SessionDetail({ session }: { session: ConversationSession }) {
  return (
    <>
      {session.events_truncated && (
        <p role="note" style={{ color: MUTED }}>
          This session is longer than the page reads; its earliest questions are not shown.
        </p>
      )}
      {session.rating === null && (
        <p style={{ color: MUTED }}>Ratings are unavailable for this session.</p>
      )}
      {session.unplaced_ratings > 0 && (
        <p style={{ color: MUTED }}>
          {session.unplaced_ratings === 1
            ? '1 rating in this session could not be matched to one question'
            : `${session.unplaced_ratings} ratings in this session could not be matched to one question`}
          {' '}(the same question was asked more than once, or the rated question is not shown).
        </p>
      )}
      {session.unverified_ratings > 0 && (
        <p style={{ color: MUTED }}>
          {session.unverified_ratings === 1
            ? '1 rating in this session is not shown'
            : `${session.unverified_ratings} ratings in this session are not shown`}
          {' '}(a newer rating may be among the ratings that were not read).
        </p>
      )}
      {session.turns.length === 0 ? (
        <p style={{ color: MUTED }}>No questions were saved in this session.</p>
      ) : (
        <ol style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {session.turns.map((t, i) => (
            <TurnView key={`${t.asked_at}-${i}`} turn={t} index={i} />
          ))}
        </ol>
      )}
    </>
  );
}

function Intro() {
  return (
    <p style={{ fontSize: '0.8rem', color: MUTED, margin: '0 0 0.8rem' }}>
      Questions the app&apos;s users asked in the web chat and the answers they got, read from
      the conversation memory. Only web-chat questions from the last 7 days appear: the memory
      keeps them for 7 days, and questions asked through AWS Quick are not saved to it. Users
      removed from the user pool are not listed; if you know such a user&apos;s id, enter it in
      User id to see their conversations.
    </p>
  );
}

const USER_ID_ERROR =
  'A user id is 1 to 128 letters, digits, "_", ":" or "-", starting with a letter or digit.';

/** A typed user id (a former user's, who is no longer in the dropdown):
 * checked against the route's own pattern before it is sent. */
function UserIdField({ onUser }: { onUser: (user: string) => void }) {
  const [text, setText] = useState('');
  const [invalid, setInvalid] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const id = text.trim();
    if (!isValidUserId(id)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    setText('');
    onUser(id);
  };
  return (
    <form onSubmit={submit} style={{ display: 'inline-flex', gap: '0.4rem', alignItems: 'center' }}>
      <label htmlFor="conversations-user-id">User id</label>
      <input
        id="conversations-user-id"
        type="text"
        value={text}
        maxLength={128}
        autoComplete="off"
        spellCheck={false}
        aria-invalid={invalid}
        aria-describedby={invalid ? 'conversations-user-id-error' : undefined}
        onChange={(e) => setText(e.target.value)}
      />
      <button type="submit">Show user</button>
      {invalid && (
        <span id="conversations-user-id-error" role="alert" style={{ color: 'var(--red)' }}>
          {USER_ID_ERROR}
        </span>
      )}
    </form>
  );
}

function ConversationsControls(props: {
  days: number;
  user: string;
  users: ConversationUser[];
  refreshing: boolean;
  onDays: (days: number) => void;
  onUser: (user: string) => void;
  onRefresh: () => void;
}) {
  return (
    <div
      className="filter-bar"
      style={{ display: 'flex', gap: '0.8rem', alignItems: 'center', flexWrap: 'wrap', marginBottom: '0.8rem' }}
    >
      <label htmlFor="conversations-user">User</label>
      <select id="conversations-user" value={props.user} onChange={(e) => props.onUser(e.target.value)}>
        <option value="">All users</option>
        {props.users.map((u) => (
          <option key={u.user_id} value={u.user_id}>
            {userLabel(u)}
          </option>
        ))}
        {props.user && !props.users.some((u) => u.user_id === props.user) && (
          <option value={props.user}>{props.user} (entered id)</option>
        )}
      </select>
      <UserIdField onUser={props.onUser} />
      <label htmlFor="conversations-days">Window</label>
      <select id="conversations-days" value={props.days} onChange={(e) => props.onDays(Number(e.target.value))}>
        {CONVERSATION_DAY_CHOICES.map((d) => (
          <option key={d} value={d}>
            {d === 1 ? 'Last day' : `Last ${d} days`}
          </option>
        ))}
      </select>
      <button type="button" onClick={props.onRefresh} disabled={props.refreshing}>
        Refresh
      </button>
    </div>
  );
}

function ConversationsTable(props: {
  sessions: ConversationSession[];
  caption: string;
  openKeys: Set<string>;
  onToggle: (key: string) => void;
}) {
  return (
    <div className="table-wrap">
      <table>
        <caption style={{ textAlign: 'left', padding: '0.5rem 0.75rem', color: MUTED }}>
          {props.caption}
        </caption>
        <thead>
          <tr>
            <th scope="col"><span className="visually-hidden">Expand</span></th>
            <th scope="col">User</th>
            <th scope="col">Started</th>
            <th scope="col">Last activity</th>
            <th scope="col">Turns</th>
            <th scope="col">Rating</th>
          </tr>
        </thead>
        <tbody>
          {props.sessions.map((s) => (
            <SessionRows
              key={sessionKey(s)}
              session={s}
              open={props.openKeys.has(sessionKey(s))}
              onToggle={() => props.onToggle(sessionKey(s))}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EmptyState({ page, days, user }: { page?: ConversationsPage; days: number; user: string }) {
  const span = days === 1 ? 'day' : `${days} days`;
  return (
    <p role="status" style={{ color: MUTED }}>
      {page && !page.complete
        ? `No web-chat conversations from the last ${span} were found in the sessions that were read.`
        : `No web-chat conversations in the last ${span}${user ? ' for this user' : ''}.`}
    </p>
  );
}

function StatusNotes({ notes }: { notes: string[] }) {
  if (!notes.length) return null;
  return (
    <ul role="note" style={{ color: MUTED, fontSize: '0.78rem' }}>
      {notes.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  );
}

function useOpenKeys(): [Set<string>, (key: string) => void] {
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());
  const toggle = (key: string) =>
    setOpenKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  return [openKeys, toggle];
}

export default function ConversationsTab() {
  const [days, setDays] = useState<number>(DEFAULT_CONVERSATION_DAYS);
  const [user, setUser] = useState('');
  const [knownUsers, setKnownUsers] = useState<ConversationUser[]>([]);
  const [openKeys, toggle] = useOpenKeys();
  const query = useInfiniteQuery(adminConversationsQueryOptions(days, user));
  const pages = query.data?.pages ?? [];
  const sessions = pages.flatMap((p) => p.sessions);
  const last = pages[pages.length - 1];

  useEffect(() => {
    const incoming = (query.data?.pages ?? []).flatMap((p) => p.users);
    if (incoming.length) setKnownUsers((known) => mergeUsers(known, incoming));
  }, [query.data]);

  return (
    <div>
      <Intro />
      <ConversationsControls days={days} user={user} users={knownUsers} refreshing={query.isFetching}
        onDays={setDays} onUser={setUser} onRefresh={() => void query.refetch()} />
      {query.isPending && <p role="status" style={{ color: MUTED }}>Loading conversations…</p>}
      {query.isError && !query.isFetchingNextPage && (
        <div role="alert">
          <p>Could not load conversations: {errorDetail(query.error)}</p>
          <button type="button" onClick={() => void query.refetch()}>Retry</button>
        </div>
      )}
      {query.isSuccess && sessions.length === 0 && <EmptyState page={last} days={days} user={user} />}
      <StatusNotes notes={moreNotes(last)} />
      {sessions.length > 0 && last && (
        <ConversationsTable sessions={sessions} caption={captionText(last, sessions.length)}
          openKeys={openKeys} onToggle={toggle} />
      )}
      {query.hasNextPage && (
        <button type="button" style={{ marginTop: '0.8rem' }} disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}>
          {query.isFetchingNextPage ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  );
}
