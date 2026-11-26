// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ChatThread/> — the conversation thread rendered from the chat thread
 * store (jsx-migration W3.1). Mounted TWICE over the ONE `chatThreadStore`:
 * inside the sidebar's #messages and (while open) the pop-out modal's
 * #modal-messages — replacing the old `dst.innerHTML = src.innerHTML`
 * mirroring hack (_renderModalBody/syncToModal).
 *
 * Structure per store slice:
 * - history: plain messages that precede a reopened session's first question
 *   (user via textContent, assistant via sanitized markdown with its control
 *   blocks parsed into chips; no bookmark/feedback). A reopened session's
 *   questions rebuild as `turns` (chat/rehydrate.ts).
 * - turns: `.message.user` (bookmark button + data-raw-content stash) +
 *   <AssistantTurn/> per live turn, memoized on the value-captured `rev`
 *   so a token frame re-renders ONLY the streaming turn (rAF batching is in
 *   the store; the stream text lives in the turn leaf).
 * - `.thinking-inline` live progress indicator ("Working... m:ss" from the
 *   turn start, plus the slow-question hint) remains at the stream position
 *   while the newest turn runs, including beside an active tool activity line
 *   (the line says what runs, the clock how long the turn has taken; removed
 *   on done/error/abort — driven by `assistant.streaming`); the adjacent
 *   visually-hidden status announces phase changes without exposing streamed
 *   token text.
 *
 * Scroll-follow: each mounted surface follows while its reader is within the
 * near-bottom threshold. Scrolling up pauses following and reveals a native
 * “Jump to latest” control; the store's rAF token batching is unchanged. */
import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';

import { useI18n } from '../lib/i18n';
import { FollowupChips, parseAnswerBlocks } from '../shared/thread/answer-blocks';
import { Md } from '../shared/ui/Md';
import { AssistantTurn } from './AssistantTurn';
import type { ThreadStore } from './thread-store';
import type { ChatTurn } from './turn-model';

const BookmarkIcon = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z" />
  </svg>
);

/** One user+assistant pair; memoized on the value-captured rev (turn
 * internals mutate in place, so object identity alone never changes).
 * useI18n inside is safe: a setLang store notify re-renders past the memo
 * comparator (which only guards turn/rev). */
const TurnPair = memo(
  function TurnPair({ turn, rev, store }: { turn: ChatTurn; rev: number; store: ThreadStore }) {
    const { t } = useI18n();
    return (
      <>
        {/* Role labels are SIBLINGS of the bubbles, never children — the
            bookmark/data-raw-content contract reads the user bubble's
            textContent, which must stay exactly the raw question. */}
        <div className="msg-role-label role-user">{t('chat.roleYou')}</div>
        <div
          className="message user msg-enter"
          style={{ position: 'relative' }}
          data-raw-content={turn.user.text}
        >
          {turn.user.text}
          {/* Bookmark ('Save as prompt') — the delegated thread click handler
              reads data-raw-content off the enclosing .message. */}
          <button className="bookmark-btn" title="Save as prompt">
            <BookmarkIcon />
          </button>
        </div>
        <div className="msg-role-label role-assistant">{t('chat.orchestrator')}</div>
        <AssistantTurn turn={turn} rev={rev} store={store} />
      </>
    );
  },
  (prev, next) => prev.turn === next.turn && prev.rev === next.rev,
);

const NEAR_BOTTOM_THRESHOLD = 80;

/** Smallest jump worth animating (px); below it the slide reads as a
 * twitch. */
const MIN_SLIDE_PX = 4;
const SLIDE_MS = 320;

interface LandingState {
  /** The streaming turn whose last text run was measured. */
  turnId: string | null;
  /** That run's viewport top, measured AFTER the follow scroll. */
  liveTop: number | null;
}

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Answer landing (FLIP). When the answer finishes, the reasoning folds into
 * its <details> and the answer would jump up by the folded height. While the
 * last turn streams, remember where its last streamed text run sits on
 * screen; on the commit that finishes it, slide `.answer-body` from there to
 * its final place. Runs in ChatThread's layout effect AFTER the follow
 * scroll, so both rectangles are in the same, final viewport. Transform only
 * (layout and scroll are untouched); skipped under reduced motion; a turn
 * that mounted already finished (history) never slides; canceled/errored
 * turns have no answer body and just clear the state. */
function landAnswer(host: HTMLElement, last: ChatTurn | null, s: LandingState): void {
  if (!last) {
    s.turnId = null;
    s.liveTop = null;
    return;
  }
  const turnEl = [...host.querySelectorAll<HTMLElement>('.message.assistant[data-turn-id]')].find(
    (el) => el.dataset.turnId === last.id,
  );
  if (last.assistant.streaming) {
    const live = turnEl?.querySelector('.reasoning-container > .stream-md:last-child');
    s.turnId = last.id;
    s.liveTop = live ? live.getBoundingClientRect().top : null;
    return;
  }
  if (s.turnId !== last.id) return;
  const from = s.liveTop;
  s.turnId = null;
  s.liveTop = null;
  const answer = turnEl?.querySelector<HTMLElement>('.answer-body');
  if (from == null || !answer || typeof answer.animate !== 'function' || prefersReducedMotion()) {
    return;
  }
  const dy = from - answer.getBoundingClientRect().top;
  if (Math.abs(dy) < MIN_SLIDE_PX) return;
  answer.animate([{ transform: `translateY(${dy}px)` }, { transform: 'translateY(0)' }], {
    duration: SLIDE_MS,
    easing: 'cubic-bezier(.16,1,.3,1)', // --ease-enter
  });
}

/** After this many seconds the indicator adds the "2 to 5 minutes" line. */
export const SLOW_HINT_AFTER_SECONDS = 60;

/** m:ss for a non-negative second count ("1:42"). */
export function formatElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** The stream-position `.thinking-inline` indicator: dots, "Working... m:ss"
 * counted from the turn's own start (so the pop-out modal, mounted later,
 * shows the same clock), and after SLOW_HINT_AFTER_SECONDS a muted line that
 * sets the expectation for cross-system questions. Client clock only; it
 * re-renders once a second and only while mounted (streaming). The slow line
 * grows the thread with no store update, so `onGrow` lets the thread keep a
 * following reader at the bottom. */
function ThinkingIndicator({ startedAt, onGrow }: { startedAt: number; onGrow: () => void }) {
  const { t } = useI18n();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const seconds = (now - startedAt) / 1000;
  const showSlow = seconds >= SLOW_HINT_AFTER_SECONDS;
  useEffect(() => {
    if (showSlow) onGrow();
  }, [showSlow, onGrow]);
  return (
    <div className="thinking-inline">
      <div className="loading-dots">
        <span />
        <span />
        <span />
      </div>
      <span className="thinking-elapsed">{`${t('chat.thinking')} ${formatElapsed(seconds)}`}</span>
      {showSlow && <span className="thinking-slow-hint">{t('chat.slowHint')}</span>}
    </div>
  );
}

export interface ChatThreadProps {
  store: ThreadStore;
  /** The scrollable host (#messages / #modal-messages). Each mounted thread
   * tracks its own reader position so sidebar and modal following are
   * independent even though they share one store. */
  containerRef?: React.RefObject<HTMLElement | null>;
}

function distanceFromBottom(element: HTMLElement): number {
  return Math.max(0, element.scrollHeight - element.clientHeight - element.scrollTop);
}

export function ChatThread({ store, containerRef }: ChatThreadProps) {
  const { t } = useI18n();
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const statusRef = useRef<HTMLDivElement | null>(null);
  const shouldFollowRef = useRef(true);
  const landingRef = useRef<LandingState>({ turnId: null, liveTop: null });
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);

  const last = state.turns.length ? state.turns[state.turns.length - 1] : null;
  const streaming = !!last?.assistant.streaming;
  const announcement = !last
    ? ''
    : last.assistant.streaming
      ? t('chat.statusResponding')
      : last.assistant.error
        ? t('chat.statusError')
        : last.assistant.canceled
          ? t('chat.statusStopped')
          : last.assistant.answerMd !== null
            ? t('chat.statusComplete')
            : '';

  const scrollHost = useCallback(
    () => containerRef?.current ?? statusRef.current?.parentElement ?? null,
    [containerRef],
  );

  // The host lives outside <ChatThread/> so both the sidebar and modal keep
  // their contractual IDs/classes. Attach the log semantics and scroll
  // listener to that existing element instead of adding a wrapper.
  useEffect(() => {
    const element = scrollHost();
    if (!element) return;

    element.setAttribute('role', 'log');
    element.setAttribute('aria-live', 'polite');
    element.setAttribute('aria-relevant', 'additions');

    shouldFollowRef.current = true;
    element.scrollTop = element.scrollHeight;
    setShowJumpToLatest(false);

    const handleScroll = () => {
      const nearBottom = distanceFromBottom(element) <= NEAR_BOTTOM_THRESHOLD;
      shouldFollowRef.current = nearBottom;
      setShowJumpToLatest(!nearBottom);
    };

    element.addEventListener('scroll', handleScroll, { passive: true });
    return () => element.removeEventListener('scroll', handleScroll);
  }, [scrollHost]);

  // Follow store updates only while the reader remains near the bottom. The
  // announcement text is phase-derived and stable across token notifications,
  // so assistive technology receives no per-token status churn.
  //
  // A layout effect (before paint), and the answer landing runs in the SAME
  // effect AFTER the follow scroll: the slide offset is measured in viewport
  // space, so measuring before the scroll moved the content would start the
  // slide from the wrong place.
  useLayoutEffect(() => {
    const element = scrollHost();
    if (!element) return;
    element.setAttribute('aria-busy', streaming ? 'true' : 'false');
    if (shouldFollowRef.current) {
      element.scrollTop = element.scrollHeight;
      setShowJumpToLatest(false);
    }
    landAnswer(element, last, landingRef.current);
  }, [state, streaming, scrollHost, last]);

  const jumpToLatest = () => {
    const element = scrollHost();
    if (!element) return;
    shouldFollowRef.current = true;
    element.scrollTop = element.scrollHeight;
    setShowJumpToLatest(false);
  };

  // Local growth without a store update (the indicator's 60 s line): keep a
  // following reader at the bottom; a reader who scrolled up stays put.
  const followLocalGrowth = useCallback(() => {
    const element = scrollHost();
    if (element && shouldFollowRef.current) element.scrollTop = element.scrollHeight;
  }, [scrollHost]);

  return (
    <>
      {state.history.map((m, i) => {
        if (m.role !== 'assistant') {
          return (
            <Fragment key={`h-${i}`}>
              <div className="msg-role-label role-user">{t('chat.roleYou')}</div>
              <div className="message user msg-enter">{m.content}</div>
            </Fragment>
          );
        }
        // Same control-block parsing as a live answer: the proposal blocks
        // are machine payload and ---FOLLOWUPS--- becomes the chip row,
        // never raw paragraphs.
        const parsed = parseAnswerBlocks(m.content);
        return (
          <Fragment key={`h-${i}`}>
            <div className="msg-role-label role-assistant">{t('chat.orchestrator')}</div>
            <Md className="message assistant msg-enter" text={parsed.answer} />
            <FollowupChips followups={parsed.followups} actions={parsed.actions} />
          </Fragment>
        );
      })}
      {state.turns.map((turn) => (
        <TurnPair key={turn.id} turn={turn} rev={turn.rev} store={store} />
      ))}
      <div
        ref={statusRef}
        className="visually-hidden chat-stream-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {announcement}
      </div>
      {streaming && last && (
        <ThinkingIndicator startedAt={last.startedAt} onGrow={followLocalGrowth} />
      )}
      {showJumpToLatest && (
        <button
          type="button"
          className="chat-jump-to-latest"
          onClick={jumpToLatest}
          style={{
            position: 'sticky',
            bottom: '0.75rem',
            zIndex: 4,
            display: 'block',
            margin: '0.5rem auto 0',
            padding: '0.5rem 0.75rem',
            border: '1px solid var(--accent)',
            borderRadius: '999px',
            background: 'var(--surface)',
            color: 'var(--text)',
            cursor: 'pointer',
            transition:
              'opacity var(--motion-base) var(--ease-standard), transform var(--motion-base) var(--ease-standard)',
          }}
        >
          {t('chat.jumpToLatest')}
        </button>
      )}
    </>
  );
}
