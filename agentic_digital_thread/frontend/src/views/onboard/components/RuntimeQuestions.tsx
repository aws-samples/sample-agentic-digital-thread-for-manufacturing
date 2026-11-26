// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <RuntimeQuestions/> — the runtime steward questions (ADR-0059 D4/D5) as a
 * CONVERSATION with the onboarding agent, rendered inside the onboarding chat
 * flow (flow entry kind 'runtimeQuestions', opened by the bell's
 * /#onboard/questions deep link — see OnboardView + openRuntimeQuestions).
 *
 * The agent asks ONE pending question at a time; the steward answers in an
 * inline composer (or skips it for now); each answer renders as a user
 * bubble and the landing outcome as the agent's reply. Transport and landing
 * semantics are unchanged from the panel it replaces (ADR-0059 D4):
 *  - GET /api/onboard/guided/runtime-questions lists the queue;
 *  - POST /api/onboard/guided/runtime-turn lands ONE answer through the
 *    existing context_interview landing machinery — a landed answer
 *    (`question_superseded: true`) is a PENDING proposal a steward reviews,
 *    never an auto-applied write;
 *  - a landed answer whose row could not be closed says so and stays put;
 *  - a REJECTED answer relays the server's reason/detail verbatim and stays
 *    re-askable (the draft is kept in the composer to fix);
 *  - a conflict is a refusal to create ambiguous authority, not an error;
 *  - "Skip for now" posts NOTHING — the question stays pending server-side.
 *
 * Question prompts are machine-composed but UNTRUSTED text — React text
 * nodes only, zero innerHTML (frontend sink policy). The prompt renders as a
 * paragraph, not a <label>, so the login page's global label styling
 * (uppercase, letter-spaced) can never leak onto it. */
import { useMutation } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, requestJson } from '../../../api/client';
import { promoteRequirementMutationOptions } from '../../../pages/admin/guidance-queries';
import { useI18n } from '../../../lib/i18n';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import { Dots } from './Checkpoint';

export const RUNTIME_QUESTIONS_URL = '/api/onboard/guided/runtime-questions';
export const RUNTIME_TURN_URL = '/api/onboard/guided/runtime-turn';
/** Answer bound — mirrors the server's context_interview ANSWER_MAX_LEN. */
export const ANSWER_MAX_LEN = 2000;
/** The hash sub-view that opens this conversation (`/#onboard/questions`). */
export const QUESTIONS_SUBVIEW = 'questions';

const MUTED = 'var(--muted)';
const RED = 'var(--red)';
const GREEN = 'var(--green)';
const AMBER = 'var(--amber)';

/** Structured correction terms (context_not_reflected rows only). */
export type RuntimeRequirementTerms = {
  asserted: string;
  negated: string;
};

/** One pending question (server contract — context_interview/routes.py
 * `_question_view`). `shape` is the failure shape the sweep classified. */
export type RuntimeQuestion = {
  id: string;
  system: string;
  shape: string;
  prompt: string;
  filed_at: string;
  requirement_terms?: RuntimeRequirementTerms;
};

/** The runtime-turn landing payload (`_result_payload` + question fields). */
export type RuntimeTurnResult = {
  outcome: 'proposed' | 'draft' | 'conflict' | 'rejected' | 'skipped' | string;
  question_id?: string;
  landing?: string;
  reason?: string;
  detail?: string;
  conflict_with?: string;
  proposal_ids?: string[];
  question_superseded?: boolean;
};

type Line = { color: string; text: string };

/** Honest per-answer feedback line. Rejections surface the server's pinned
 * reason/detail VERBATIM — a swallowed rejection would tell the steward
 * their answer was taken when it was not. */
export function turnResultLine(r: RuntimeTurnResult): Line {
  switch (r.outcome) {
    case 'proposed':
      return r.question_superseded
        ? { color: GREEN, text: 'Answer landed as a proposal for review.' }
        : {
            color: AMBER,
            text:
              'Answer landed as a proposal for review, but the question could not ' +
              'be marked answered — it may reappear until the queue refreshes.',
          };
    case 'conflict':
      return {
        color: AMBER,
        text:
          `Not applied — another system (${r.conflict_with ?? 'unknown'}) is already ` +
          'the authority for this concept. Deactivate that rule first if you want to change it.',
      };
    case 'skipped':
      return { color: MUTED, text: 'Skipped — the question stays pending.' };
    default:
      // 'rejected' and anything unmodeled: relay the server's reason/detail.
      return { color: RED, text: r.detail || r.reason || 'Not accepted.' };
  }
}

/** Filed date for display — locale date when parseable, raw string otherwise
 * (an unparseable timestamp is still information, never hidden). */
export function filedDateLabel(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleDateString();
}

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.detail || e.message : 'Request failed';
}

/** One finished exchange in the thread (answered or skipped). */
type Turn = { question: RuntimeQuestion; answer: string | null; line: Line; landed: boolean };

function AgentBubble({ children, className }: { children: React.ReactNode; className?: string }) {
  const { t } = useI18n();
  return (
    <div className={`ob-msg-wrap${className ? ` ${className}` : ''}`}>
      <span className="ob-agent-badge">{t('onboard.agentBadge')}</span>
      <div className="ob-msg">{children}</div>
    </div>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="ob-msg-wrap ob-msg-user">
      <span className="ob-user-badge">You</span>
      <div className="ob-msg user rq-user-answer">{text}</div>
    </div>
  );
}

function QuestionMeta({ q, index, total }: { q: RuntimeQuestion; index: number; total: number }) {
  // `q.system` is a system id on the wire; the chip shows its catalog name.
  return (
    <div className="rq-meta">
      <span className="rq-system">{systemName(q.system)}</span>
      <span className="rq-shape">{q.shape}</span>
      <span className="rq-filed">filed {filedDateLabel(q.filed_at)}</span>
      {total > 1 && <span className="rq-count">question {index} of {total}</span>}
    </div>
  );
}

function PastTurn({ turn }: { turn: Turn }) {
  return (
    <>
      <AgentBubble className="rq-asked">
        <QuestionMeta q={turn.question} index={0} total={0} />
        <p className="rq-prompt">{turn.question.prompt}</p>
      </AgentBubble>
      {turn.answer !== null && <UserBubble text={turn.answer} />}
      <AgentBubble className={turn.landed ? 'rq-landed' : 'rq-skipped'}>
        <span style={{ color: turn.line.color }}>{turn.line.text}</span>
      </AgentBubble>
    </>
  );
}

type PromoteState = { busy: boolean; line: Line | null };

function PromoteControl({ q, state, onPromote }: {
  q: RuntimeQuestion;
  state: PromoteState;
  onPromote: () => void;
}) {
  return (
    <div className="rq-promote">
      <button
        type="button"
        className="ob-rerun-btn rq-promote-requirement"
        aria-label={`Promote structured correction for ${systemName(q.system)} to answer requirement`}
        disabled={state.busy}
        onClick={onPromote}
      >
        {state.busy ? 'Creating pending proposal…' : 'Promote to answer requirement'}
      </button>
      <div className="rq-note">
        Creates a PENDING proposal for steward review; it does not apply a requirement.
      </div>
      {state.line && (
        <div
          className="rq-promotion-line"
          role={state.line.color === RED ? 'alert' : 'status'}
          style={{ color: state.line.color }}
        >
          {state.line.text}
        </div>
      )}
    </div>
  );
}

type ComposerProps = {
  q: RuntimeQuestion;
  answer: string;
  setAnswer: (v: string) => void;
  submitting: boolean;
  onSubmit: () => void;
  onSkip: () => void;
};

function Composer({ q, answer, setAnswer, submitting, onSubmit, onSkip }: ComposerProps) {
  return (
    <div className="rq-composer">
      <div className="ob-chat-input-row">
        <textarea
          id={`rq-answer-${q.id}`}
          className="ob-chat-input rq-answer"
          aria-label={`Answer: ${q.prompt}`}
          placeholder="Type your answer…"
          value={answer}
          maxLength={ANSWER_MAX_LEN}
          rows={2}
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              onSubmit();
            }
          }}
        />
        <button
          type="button"
          className="ob-chat-send btn-send rq-send"
          disabled={!answer.trim() || submitting}
          onClick={onSubmit}
        >
          {submitting ? 'Sending…' : 'Send'}
        </button>
      </div>
      <div className="rq-composer-foot">
        <button type="button" className="ob-rerun-btn rq-skip" disabled={submitting} onClick={onSkip}>
          Skip for now
        </button>
        <span className="rq-char-count">
          {answer.length}/{ANSWER_MAX_LEN}
        </span>
      </div>
    </div>
  );
}

type Loaded = { status: 'loading' } | { status: 'error'; text: string } | { status: 'ready' };

/** Queue state + the two network actions (answer, promote). */
function useRuntimeQuestions(openSeq: number) {
  const [loaded, setLoaded] = useState<Loaded>({ status: 'loading' });
  const [queue, setQueue] = useState<RuntimeQuestion[]>([]);
  const [total, setTotal] = useState(0);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [line, setLine] = useState<Line | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [promote, setPromote] = useState<PromoteState>({ busy: false, line: null });
  const promoteMutation = useMutation(promoteRequirementMutationOptions());

  const load = useCallback(async () => {
    setLoaded({ status: 'loading' });
    try {
      const data = await requestJson<{ questions?: RuntimeQuestion[] }>(RUNTIME_QUESTIONS_URL);
      const qs = data.questions ?? [];
      setQueue(qs);
      setTotal(qs.length);
      setTurns([]);
      setLine(null);
      setLoaded({ status: 'ready' });
    } catch (e) {
      setLoaded({ status: 'error', text: errorText(e) });
    }
  }, []);

  // Every (re)open from the bell refetches the live queue.
  useEffect(() => {
    void load();
  }, [openSeq, load]);

  const advance = (turn: Turn) => {
    setTurns((prev) => [...prev, turn]);
    setQueue((prev) => prev.slice(1));
    setLine(null);
    setPromote({ busy: false, line: null });
  };

  async function submit(answer: string): Promise<boolean> {
    const q = queue[0];
    const text = answer.trim();
    if (!q || !text || submitting) return false;
    setSubmitting(true);
    setLine(null);
    try {
      const res = await requestJson<RuntimeTurnResult>(RUNTIME_TURN_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question_id: q.id, answer: text }),
      });
      if (res.outcome === 'proposed' && res.question_superseded) {
        advance({ question: q, answer: text, line: turnResultLine(res), landed: true });
        return true;
      }
      setLine(turnResultLine(res)); // rejected / conflict / not-closed: stays re-askable
      return false;
    } catch (e) {
      setLine({ color: RED, text: errorText(e) });
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  const skip = () => {
    const q = queue[0];
    if (q) advance({ question: q, answer: null, line: turnResultLine({ outcome: 'skipped' }), landed: false });
  };

  async function promoteCurrent() {
    const q = queue[0];
    const terms = q?.requirement_terms;
    if (!q || !terms?.negated || promote.busy) return;
    setPromote({ busy: true, line: null });
    try {
      const result = await promoteMutation.mutateAsync({
        systemId: q.system,
        asserted: terms.asserted,
        negated: terms.negated,
      });
      setPromote({
        busy: false,
        line: {
          color: GREEN,
          text: `PENDING proposal ${result.mapping_id} created — the answer requirement is not applied.`,
        },
      });
    } catch (e) {
      setPromote({ busy: false, line: { color: RED, text: errorText(e) } });
    }
  }

  return { loaded, load, queue, total, turns, line, submitting, promote, submit, skip, promoteCurrent };
}

function Intro({ total }: { total: number }) {
  return (
    <AgentBubble className="rq-intro">
      {total === 1 ? 'I have a question for you. ' : `I have ${total} questions for you. `}
      Repeated query failures suggested some context is missing. I&apos;ll ask one at a
      time — each answer becomes a pending proposal a steward reviews; nothing is applied
      automatically.
    </AgentBubble>
  );
}

function Outro({ turns }: { turns: Turn[] }) {
  const skipped = turns.filter((t) => !t.landed).length;
  return (
    <AgentBubble className="rq-done">
      {skipped > 0
        ? `That's all for now. ${skipped} skipped question${skipped > 1 ? 's stay' : ' stays'} pending — the bell will bring ${skipped > 1 ? 'them' : 'it'} back.`
        : "That's all for now — thanks. Your answers are waiting for steward review."}
    </AgentBubble>
  );
}

export function RuntimeQuestions({ openSeq = 1 }: { openSeq?: number }) {
  // The question chips and the promote button's label name systems: one
  // subscription here re-renders the whole thread when catalog names arrive.
  useSystemNamesVersion();
  const s = useRuntimeQuestions(openSeq);
  const [answer, setAnswer] = useState('');
  const currentRef = useRef<HTMLDivElement | null>(null);
  const current = s.queue[0];

  // A re-open refetches the queue — drop any stale draft with it.
  useEffect(() => setAnswer(''), [openSeq]);

  // Keep the question being asked in view as the conversation advances.
  useEffect(() => {
    currentRef.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [current?.id, openSeq]);

  const send = async () => {
    if (await s.submit(answer)) setAnswer('');
  };
  const skip = () => {
    setAnswer('');
    s.skip();
  };

  return (
    <section id="runtime-questions" className="rq-thread" aria-label="Questions from the system">
      {s.loaded.status === 'loading' && (
        <AgentBubble>
          <Dots />
        </AgentBubble>
      )}
      {s.loaded.status === 'error' && (
        <div className="ob-msg error rq-load-error">
          <span>⚠ Couldn&apos;t load the questions: {s.loaded.text} </span>
          <button type="button" className="ob-rerun-btn" onClick={() => void s.load()}>
            Retry
          </button>
        </div>
      )}
      {s.loaded.status === 'ready' && s.total === 0 && (
        <AgentBubble className="rq-empty">
          <p className="rq-prompt">No questions right now.</p>
          <p className="rq-note">
            When repeated query failures suggest missing context I&apos;ll ask here. Answers
            become pending proposals a steward reviews — nothing is applied automatically.
          </p>
        </AgentBubble>
      )}
      {s.loaded.status === 'ready' && s.total > 0 && (
        <>
          <Intro total={s.total} />
          {s.turns.map((turn) => (
            <PastTurn key={turn.question.id} turn={turn} />
          ))}
          {current ? (
            <div ref={currentRef} className="rq-question" data-question-id={current.id}>
              <AgentBubble>
                <QuestionMeta q={current} index={s.total - s.queue.length + 1} total={s.total} />
                <p className="rq-prompt">{current.prompt}</p>
                {current.shape === 'context_not_reflected' && !!current.requirement_terms?.negated && (
                  <PromoteControl q={current} state={s.promote} onPromote={() => void s.promoteCurrent()} />
                )}
              </AgentBubble>
              {s.line && (
                <AgentBubble className="rq-reply">
                  <span
                    className="rq-line"
                    role={s.line.color === RED ? 'alert' : 'status'}
                    style={{ color: s.line.color }}
                  >
                    {s.line.text}
                  </span>
                </AgentBubble>
              )}
              <Composer
                q={current}
                answer={answer}
                setAnswer={setAnswer}
                submitting={s.submitting}
                onSubmit={() => void send()}
                onSkip={skip}
              />
            </div>
          ) : (
            <Outro turns={s.turns} />
          )}
        </>
      )}
    </section>
  );
}

export default RuntimeQuestions;
