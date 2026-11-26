// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Feedback thumbs — the feedback row + submitFeedback.
 *
 * jsx-migration W3.1: <FeedbackRow/> is the React component (local
 * selected-thumb state, direct onClick handlers) that <AssistantTurn/>
 * mounts under each assistant answer; the legacy imperative
 * buildFeedbackRow() DOM wrapper is gone. submitFeedback stays for the
 * shared thread click delegation (thread-clicks.ts) so any feedback buttons
 * living in sanitized-markdown content remain live. POST failures restore the
 * controls and surface through the existing toast/inline error affordances. */
import { useState, type ReactNode } from 'react';

import { ApiError, requestJson } from '../api/client';
import type { FeedbackPayload } from '../api/types/chat';
import { tf, useI18n } from '../lib/i18n';
import { getSessionId } from '../lib/session';
import { showToast } from '../shared/thread/data-modal';

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

/** Checked POST /api/feedback (shared by the delegated submit path
 * and <FeedbackRow/>). */
async function postFeedback(body: FeedbackPayload): Promise<void> {
  await requestJson<{ status?: string }>('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** Submit thumbs feedback (submitFeedback).
 * Consumed by the thread click delegation (thread-clicks.ts) for feedback
 * buttons inside sanitized-markdown content; <FeedbackRow/> posts directly. */
export async function submitFeedback(btn: HTMLButtonElement, messageId: string): Promise<void> {
  const row = btn.parentElement;
  const sentiment = (btn.dataset.sentiment === 'negative' ? 'negative' : 'positive') as
    | 'positive'
    | 'negative';
  const msgDiv = btn.closest('.message') as HTMLElement | null;
  const body: FeedbackPayload = {
    message_id: messageId,
    session_id: getSessionId(),
    user_message: msgDiv?.dataset.userMessage || '',
    assistant_response: msgDiv?.dataset.assistantResponse || '',
    tools_used: [],
    sentiment,
  };
  if (row) {
    row.querySelectorAll<HTMLButtonElement>('.feedback-btn').forEach((b) => (b.disabled = true));
    btn.style.opacity = '1';
    row.querySelectorAll<HTMLButtonElement>('.feedback-btn').forEach((b) => {
      if (b !== btn) b.style.opacity = '0.3';
    });
  }
  try {
    await postFeedback(body);
  } catch (error) {
    if (row) {
      row.querySelectorAll<HTMLButtonElement>('.feedback-btn').forEach((button) => {
        button.disabled = false;
        button.style.opacity = '';
      });
    }
    showToast('Could not submit feedback: ' + errorDetail(error));
  }
}

// ── React component (mounted by <AssistantTurn/> under each answer) ─────────

function ThumbUpIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" />
    </svg>
  );
}

function ThumbDownIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17" />
    </svg>
  );
}

export interface FeedbackRowProps {
  messageId: string;
  userMessage?: string;
  assistantResponse?: string;
  /** Distinct tool names the answer's turn called (AnswerFooter.toolsUsed),
   * so a feedback record shows what ran. */
  toolsUsed?: string[];
  /** Cited context of the answer — system IDS derived from the citation
   * tool names (citedSystemIds — never display labels) plus the
   * applied rules — forwarded with an optional comment on the details POST;
   * the server re-caps and id-shape-normalizes regardless
   * (≤12 systems / ≤20 rules). */
  citedSystems?: string[];
  citedRules?: { id?: string; label?: string }[];
}

type Sentiment = 'positive' | 'negative';

interface FeedbackDetailsProps {
  messageId: string;
  userMessage: string;
  assistantResponse: string;
  sentiment: Sentiment;
  toolsUsed: string[];
  citedSystems: string[];
  citedRules: { id?: string; label?: string }[];
}

/** Optional free-text details revealed AFTER a thumb click: a SECOND
 * postFeedback carrying the comment + the cited context with the SAME
 * message_id/sentiment (the thumb's immediate POST already happened and is
 * untouched). Deliberately NO `.feedback-btn` class in here — the
 * thumbs-count DOM contract stays exactly 2. JSX only, zero innerHTML.
 * Styling: answer.css (.feedback-details / .feedback-thanks). */
function FeedbackDetails({
  messageId,
  userMessage,
  assistantResponse,
  sentiment,
  toolsUsed,
  citedSystems,
  citedRules,
}: FeedbackDetailsProps) {
  const { t } = useI18n();
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState('');

  if (sent) {
    return (
      <div className="feedback-thanks" role="status">
        {t('chat.feedbackThanks')}
      </div>
    );
  }

  const send = async () => {
    if (!text.trim() || sending) return;
    setSending(true);
    setError('');
    try {
      await postFeedback({
        message_id: messageId,
        session_id: getSessionId(),
        user_message: userMessage,
        assistant_response: assistantResponse,
        tools_used: toolsUsed,
        sentiment,
        user_comment: text.trim(),
        cited_systems: citedSystems.slice(0, 12),
        cited_rules: citedRules.slice(0, 20),
      });
      setSent(true);
    } catch (submitError) {
      setError(tf('chat.feedbackFailed', { detail: errorDetail(submitError) }));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="feedback-details">
      <textarea
        className="feedback-comment"
        maxLength={1000}
        rows={2}
        aria-label={t('chat.feedbackDetailsPlaceholder')}
        placeholder={t('chat.feedbackDetailsPlaceholder')}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onClick={(e) => e.stopPropagation()}
      />
      <button
        type="button"
        className="feedback-send"
        disabled={sending || !text.trim()}
        onClick={(e) => {
          e.stopPropagation();
          void send();
        }}
      >
        {t('chat.feedbackSend')}
      </button>
      {error && (
        <span className="feedback-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

/** Thumbs row as a React component: same .feedback-row/.feedback-btn +
 * data-sentiment contract as the original row, with local selected
 * state (click disables both, dims the unselected thumb via
 * [data-selected] in answer.css). Each thumb carries an aria-label (the
 * tooltip text) and aria-pressed, so a screen reader hears which one was
 * chosen. Clicks stop propagation so the thread-clicks delegation never
 * double-submits. */
export function FeedbackRow({
  messageId,
  userMessage = '',
  assistantResponse = '',
  toolsUsed = [],
  citedSystems = [],
  citedRules = [],
}: FeedbackRowProps) {
  const { t } = useI18n();
  const [selected, setSelected] = useState<Sentiment | null>(null);
  const [pending, setPending] = useState<Sentiment | null>(null);
  const [error, setError] = useState('');

  const choose = async (sentiment: Sentiment) => {
    if (selected || pending) return;
    setPending(sentiment);
    setError('');
    try {
      await postFeedback({
        message_id: messageId,
        session_id: getSessionId(),
        user_message: userMessage,
        assistant_response: assistantResponse,
        tools_used: toolsUsed,
        sentiment,
      });
      setSelected(sentiment);
    } catch (submitError) {
      setError(tf('chat.feedbackFailed', { detail: errorDetail(submitError) }));
    } finally {
      setPending(null);
    }
  };

  const thumb = (sentiment: Sentiment, label: string, icon: ReactNode) => (
    <button
      type="button"
      className="feedback-btn"
      data-sentiment={sentiment}
      title={label}
      aria-label={label}
      aria-pressed={selected === sentiment}
      disabled={selected != null || pending != null}
      onClick={(e) => {
        e.stopPropagation();
        void choose(sentiment);
      }}
    >
      {icon}
    </button>
  );

  return (
    <>
      <div className="feedback-row" data-selected={selected ?? undefined}>
        {thumb('positive', t('chat.feedbackGood'), <ThumbUpIcon />)}
        {thumb('negative', t('chat.feedbackBad'), <ThumbDownIcon />)}
        {error && (
          <span className="feedback-error" role="alert">
            {error}
          </span>
        )}
      </div>
      {selected != null && (
        <FeedbackDetails
          messageId={messageId}
          userMessage={userMessage}
          assistantResponse={assistantResponse}
          sentiment={selected}
          toolsUsed={toolsUsed}
          citedSystems={citedSystems}
          citedRules={citedRules}
        />
      )}
    </>
  );
}
