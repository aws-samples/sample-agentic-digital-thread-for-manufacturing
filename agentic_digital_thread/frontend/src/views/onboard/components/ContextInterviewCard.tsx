// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ContextInterviewCard/> — conversational business-context capture (Wave 2b).
 *
 * Rendered inside the onboarding CHAT flow (mounted by the vocab_fit
 * checkpoint branch — Wave 3), after discover and before map. Transport is
 * exactly PreMapProbe's guided endpoints: question wording, sanitization, and
 * landing all stay server-side in `agents/context_interview/` — this card
 * carries ZERO question text and posts NO tables (the server derives
 * per-system questions from the agent-path stored discovery).
 *
 * Deployment-scope questions (GET /guided/questions → POST /guided/turn) are
 * asked only while the server reports first launch and not dismissed;
 * per-system ones ride POST /guided/probe/{id}. Steward answers are UNTRUSTED
 * text: controlled inputs and React text nodes only (zero-innerHTML policy);
 * the server sanitizes at write, and a rejected answer does not advance.
 *
 * The card is ADDITIVE, never a gate: a mount-time fetch failure collapses it
 * to a one-line "context capture unavailable" note, and Skip all folds the
 * card to a one-line summary. Confirm reporting adapts pre-provision: the
 * system agent is typically NOT provisioned yet at this point in the flow, so
 * written-but-roll-not-ok is success-with-note, not a failure (flag 5). */
import { useCallback, useEffect, useState } from 'react';

import { ApiError, getJson, postJson } from '../../../api/client';
import { systemName, useSystemNamesVersion } from '../../../lib/system-names';
import {
  confirmResultLine,
  confirmWarningText,
  GUIDED_CONFIRM_URL,
  GUIDED_DISMISS_URL,
  GUIDED_QUESTIONS_URL,
  GUIDED_STATE_URL,
  GUIDED_TURN_URL,
  turnResultLine,
  type ConfirmResponse,
  type GuidedQuestion,
  type GuidedState,
  type TurnResult,
} from './context-shared';
import { probeUrl, type DerivedEntry, type ProbeResponse } from './PreMapProbe';

const MUTED = 'var(--muted)';
const RED = 'var(--red, #f87171)';
const AMBER = 'var(--amber, #fbbf24)';

const TEXTAREA_STYLE: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  marginTop: '0.4rem',
  background: 'var(--surface)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.4rem',
  fontSize: '0.8rem',
  fontFamily: 'inherit',
};

type Line = { color: string; text: string };

type InterviewItem = {
  scope: 'deployment' | 'system';
  id: string;
  prompt: string;
  evidence?: string;
};

type CardState = {
  status: 'loading' | 'ready' | 'unavailable';
  items: InterviewItem[];
  total: number;
  derived: DerivedEntry[];
  skippedAll: boolean;
  confirmed: boolean;
  /** A confirm request is in flight; Save stays disabled until it returns. */
  saving: boolean;
  line: Line | null;
  confirmLine: Line | null;
};

const INITIAL: CardState = {
  status: 'loading',
  items: [],
  total: 0,
  derived: [],
  skippedAll: false,
  confirmed: false,
  saving: false,
  line: null,
  confirmLine: null,
};

type Patch = (p: Partial<CardState>) => void;

type Ctx = {
  systemId: string;
  answer: string;
  items: InterviewItem[];
  total: number;
  patch: Patch;
  setAnswer: (v: string) => void;
  setDraft: (v: string) => void;
};

function fetchErrorLine(e: unknown): Line {
  return {
    color: RED,
    text: e instanceof ApiError ? e.detail || e.message : 'Request failed',
  };
}

/** Mount-time load: guided state, deployment questions (first launch only),
 * and the per-system probe — answers: [] and NO tables, so the SERVER derives
 * questions from its stored agent-path discovery. */
async function loadInterview(
  systemId: string,
): Promise<{ items: InterviewItem[]; derived: DerivedEntry[]; draft: string; firstLaunch: boolean }> {
  const st = await getJson<GuidedState>(GUIDED_STATE_URL);
  let dep: InterviewItem[] = [];
  if (st.first_launch && !st.dismissed) {
    const res = await getJson<{ questions: GuidedQuestion[] }>(GUIDED_QUESTIONS_URL);
    dep = (res.questions ?? []).map((q) => ({
      scope: 'deployment' as const,
      id: q.id,
      prompt: q.prompt,
    }));
  }
  const probe = await postJson<ProbeResponse>(probeUrl(systemId), { answers: [] });
  const sys = (probe.questions ?? []).map((q) => ({
    scope: 'system' as const,
    id: q.id,
    prompt: q.prompt,
    evidence: q.evidence,
  }));
  return {
    items: [...dep, ...sys],
    derived: probe.derived ?? [],
    draft: probe.draft_business_context || st.draft_business_context || '',
    firstLaunch: Boolean(st.first_launch && !st.dismissed),
  };
}

/** Land one answer (or a skip) through the scope's endpoint. A rejected
 * outcome shows the server's reason verbatim and does NOT advance. */
async function submitAnswer(ctx: Ctx, skip: boolean): Promise<void> {
  const q = ctx.items[0];
  if (!q) return;
  ctx.patch({ line: null });
  const answer = skip ? '' : ctx.answer;
  try {
    let outcome: TurnResult | undefined;
    let newDraft: string | undefined;
    if (q.scope === 'deployment') {
      const res = await postJson<TurnResult & { draft_business_context?: string }>(
        GUIDED_TURN_URL,
        { question_id: q.id, answer },
      );
      outcome = res;
      newDraft = res.draft_business_context;
    } else {
      const res = await postJson<ProbeResponse>(probeUrl(ctx.systemId), {
        answers: [{ question_id: q.id, answer }],
      });
      outcome = res.results?.[0];
      newDraft = res.draft_business_context;
    }
    if (outcome) ctx.patch({ line: turnResultLine(outcome) });
    if (outcome?.outcome === 'rejected') return; // fix, don't advance
    if (newDraft !== undefined) ctx.setDraft(newDraft);
    ctx.setAnswer('');
    ctx.patch({ items: ctx.items.slice(1) });
  } catch (e) {
    ctx.patch({ line: fetchErrorLine(e) });
  }
}

/** Decline the DEPLOYMENT-scope questions only — per-system ones remain. */
async function dismissDeployment(ctx: Ctx): Promise<void> {
  try {
    await postJson(GUIDED_DISMISS_URL, {});
    const kept = ctx.items.filter((x) => x.scope !== 'deployment');
    const answered = ctx.total - ctx.items.length;
    ctx.patch({ items: kept, total: answered + kept.length, line: null });
  } catch (e) {
    ctx.patch({ line: fetchErrorLine(e) });
  }
}

/** Flag-5 adaptation: at this point in the flow the system agent is usually
 * not provisioned yet, so a durable write with a non-ok roll is success with
 * a note (the agent reads business context at container start) — while a
 * dropped WRITE still reports through confirmResultLine's honest red path. */
export function preProvisionConfirmLine(res: ConfirmResponse): Line {
  const entries = Object.values(res.systems ?? {});
  const allWritten = entries.length > 0 && entries.every((v) => v.written);
  const rollPending = entries.some((v) => v.roll && v.roll !== 'ok');
  if (res.status === 'ok' && allWritten && rollPending) {
    return {
      color: AMBER,
      text:
        'Context saved. If the agent is not provisioned yet, it will read the ' +
        'context when it starts; a running agent picks it up on its next restart.',
    };
  }
  return confirmResultLine(res);
}

async function saveDraft(systemId: string, draft: string, patch: Patch): Promise<void> {
  patch({ confirmLine: null, saving: true });
  try {
    const res = await postJson<ConfirmResponse>(GUIDED_CONFIRM_URL, {
      systems: [systemId],
      business_context: draft,
    });
    const entries = Object.values(res.systems ?? {});
    const allWritten = entries.length > 0 && entries.every((v) => v.written);
    patch({ confirmLine: preProvisionConfirmLine(res), confirmed: allWritten, saving: false });
  } catch (e) {
    patch({ confirmLine: fetchErrorLine(e), saving: false });
  }
}

/** What the schema already answered — read-only, above the questions (the
 * PreMapProbe DerivedNotes idiom; rejected derivations never render). */
function DerivedNotes({ derived }: { derived: DerivedEntry[] }) {
  const notes = derived.filter((d) => d.outcome !== 'rejected' && d.derived_text);
  if (notes.length === 0) return null;
  return (
    <div style={{ margin: '0.45rem 0' }}>
      <div style={{ fontSize: '0.72rem', color: MUTED }}>
        Noted from the discovered schema (no answer needed):
      </div>
      <ul style={{ margin: '0.25rem 0 0', paddingLeft: '1.1rem' }}>
        {notes.map((d) => (
          <li key={d.question_id} style={{ fontSize: '0.78rem', color: MUTED }}>
            {d.derived_text}
            {d.evidence && (
              <div style={{ fontSize: '0.72rem', color: MUTED, marginTop: '0.1rem' }}>
                based on: {d.evidence}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Note({ text }: { text: string }) {
  return (
    <section className="ob-checkpoint" aria-label="Context interview">
      <div style={{ fontSize: '0.78rem', color: MUTED }}>{text}</div>
    </section>
  );
}

type QuestionFormProps = {
  item: InterviewItem;
  answer: string;
  setAnswer: (v: string) => void;
  submit: (skip: boolean) => void;
  skipAll: () => void;
  showDismiss: boolean;
  dismiss: () => void;
};

function QuestionForm(props: QuestionFormProps) {
  const { item, answer } = props;
  return (
    <div>
      <label className="ob-prompt-label" htmlFor="ctx-interview-answer" style={{ fontSize: '0.85rem' }}>
        {item.prompt}
      </label>
      {item.evidence && (
        <div style={{ fontSize: '0.72rem', color: MUTED, marginTop: '0.2rem' }}>
          based on: {item.evidence}
        </div>
      )}
      <textarea
        id="ctx-interview-answer"
        aria-label="Context answer"
        value={answer}
        onChange={(e) => props.setAnswer(e.target.value)}
        rows={3}
        style={TEXTAREA_STYLE}
      />
      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
        <button
          type="button"
          className="ob-confirm-btn"
          disabled={!answer.trim()}
          onClick={() => props.submit(false)}
        >
          Answer
        </button>
        <button type="button" className="ob-rerun-btn" onClick={() => props.submit(true)}>
          I don&apos;t know
        </button>
        <button type="button" className="ob-rerun-btn" onClick={props.skipAll}>
          Skip all
        </button>
        {props.showDismiss && (
          <button type="button" className="ob-rerun-btn" onClick={props.dismiss}>
            Don&apos;t ask during onboarding again
          </button>
        )}
      </div>
    </div>
  );
}

type ConfirmSectionProps = {
  systemId: string;
  draft: string;
  setDraft: (v: string) => void;
  confirmed: boolean;
  saving: boolean;
  line: Line | null;
  save: () => void;
};

function ConfirmSection(props: ConfirmSectionProps) {
  return (
    <div style={{ marginTop: '0.5rem' }}>
      <label className="ob-prompt-label" htmlFor="ctx-interview-draft" style={{ fontSize: '0.85rem' }}>
        Business context draft — review and edit before saving
      </label>
      <textarea
        id="ctx-interview-draft"
        aria-label="Business context draft"
        value={props.draft}
        onChange={(e) => props.setDraft(e.target.value)}
        rows={5}
        disabled={props.confirmed || props.saving}
        style={TEXTAREA_STYLE}
      />
      <div style={{ fontSize: '0.72rem', color: MUTED, marginTop: '0.3rem' }}>
        {confirmWarningText([props.systemId])} If the agent is not provisioned yet,
        the context is saved and read when the agent starts.
      </div>
      <button
        type="button"
        className="ob-confirm-btn"
        disabled={props.confirmed || props.saving || !props.draft.trim()}
        onClick={props.save}
      >
        {props.saving ? 'Saving…' : `Save context for ${systemName(props.systemId)}`}
      </button>
      {props.line && (
        <div style={{ color: props.line.color, fontSize: '0.75rem', marginTop: '0.5rem' }}>
          {props.line.text}
        </div>
      )}
    </div>
  );
}

type BodyProps = {
  systemId: string;
  firstLaunch: boolean;
  st: CardState;
  ctx: Ctx;
  answer: string;
  setAnswer: (v: string) => void;
  draft: string;
  setDraft: (v: string) => void;
};

function CardBody({ systemId, firstLaunch, st, ctx, answer, setAnswer, draft, setDraft }: BodyProps) {
  const current = st.items[0];
  return (
    <section className="ob-checkpoint" aria-label="Context interview">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.6rem' }}>
        <h4 style={{ margin: 0 }}>✦ Business context — {systemName(systemId)}</h4>
        {current && (
          <span style={{ fontSize: '0.72rem', color: MUTED }}>
            question {st.total - st.items.length + 1} of {st.total}
          </span>
        )}
      </div>
      <DerivedNotes derived={st.derived} />
      {current ? (
        <QuestionForm
          item={current}
          answer={answer}
          setAnswer={setAnswer}
          submit={(skip) => void submitAnswer(ctx, skip)}
          skipAll={() => ctx.patch({ skippedAll: true })}
          showDismiss={firstLaunch && current.scope === 'deployment'}
          dismiss={() => void dismissDeployment(ctx)}
        />
      ) : draft.trim() ? (
        <ConfirmSection
          systemId={systemId}
          draft={draft}
          setDraft={setDraft}
          confirmed={st.confirmed}
          saving={st.saving}
          line={st.confirmLine}
          save={() => void saveDraft(systemId, draft, ctx.patch)}
        />
      ) : (
        <div style={{ fontSize: '0.78rem', color: MUTED, marginTop: '0.4rem' }}>
          {st.total > 0
            ? 'Context capture complete.'
            : 'No context questions for this system.'}
        </div>
      )}
      {st.line && (
        <div style={{ color: st.line.color, fontSize: '0.75rem', marginTop: '0.5rem' }}>
          {st.line.text}
        </div>
      )}
    </section>
  );
}

type Props = {
  systemId: string;
  /** Optional override for the dismiss affordance. When omitted, the card
   * follows the server's own first_launch/dismissed state (the only honest
   * source — the onboarding runtime cannot see the app-process draft). */
  firstLaunch?: boolean;
};

export function ContextInterviewCard({ systemId, firstLaunch }: Props) {
  // Title and save button name the system: re-render when names arrive.
  useSystemNamesVersion();
  const [st, setSt] = useState<CardState>(INITIAL);
  const [serverFirstLaunch, setServerFirstLaunch] = useState(false);
  const [answer, setAnswer] = useState('');
  const [draft, setDraft] = useState('');
  const patch = useCallback<Patch>((p) => setSt((s) => ({ ...s, ...p })), []);

  useEffect(() => {
    let live = true;
    loadInterview(systemId)
      .then((r) => {
        if (!live) return;
        setDraft(r.draft);
        setServerFirstLaunch(r.firstLaunch);
        setSt((s) => ({
          ...s,
          status: 'ready',
          items: r.items,
          total: r.items.length,
          derived: r.derived,
        }));
      })
      // Context capture is additive — never stand between the steward and
      // the rest of onboarding.
      .catch(() => live && patch({ status: 'unavailable' }));
    return () => {
      live = false;
    };
  }, [systemId, patch]);

  if (st.status === 'loading') return null;
  if (st.status === 'unavailable') {
    return <Note text="context capture unavailable — continue" />;
  }
  if (st.skippedAll) {
    return <Note text="Context questions skipped — business context can be added later from the Admin page." />;
  }
  const ctx: Ctx = { systemId, answer, items: st.items, total: st.total, patch, setAnswer, setDraft };
  return (
    <CardBody
      systemId={systemId}
      firstLaunch={firstLaunch ?? serverFirstLaunch}
      st={st}
      ctx={ctx}
      answer={answer}
      setAnswer={setAnswer}
      draft={draft}
      setDraft={setDraft}
    />
  );
}

export default ContextInterviewCard;
