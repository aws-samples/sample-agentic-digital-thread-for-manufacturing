// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Try-it validation drawer (B2, task 3.2 — ADR-0052) — the honest impact
 * preview for a pending proposal: POST /api/admin/learning/tryit runs the
 * row's representative question TWICE server-side (baseline route seed vs
 * seed with the candidate rule injected) as an async JobStore job; this
 * panel polls the job every 5 s with plain fetches (NEVER the SWR panel
 * loader — a run takes ~2 min/question and CloudFront caps requests at
 * 120 s, which is exactly why the backend is job+poll) and renders the
 * side-by-side diff: systems queried, rules applied (candidate-only rows
 * highlighted), translation steps, answer excerpts, and a routing-delta
 * strip. States: confirm (explicit cost warning — one real question, twice,
 * a few minutes, normal model cost), running, busy (409 — ONE concurrent
 * job app-wide), error (stored honestly, never a fabricated diff), done.
 * Zero innerHTML: every server string renders as JSX text. */
import { Fragment, useEffect, useState, type Dispatch, type SetStateAction } from 'react';

import { ApiError, requestJson } from '../../api/client';
import type {
  TryItDiff,
  TryItJob,
  TryItLaunch,
  TryItResult,
  TryItRuleRef,
  TryItRun,
} from '../../api/types/learning';
import type { ProposedMapping } from '../../api/types/proposals';
import { systemName, useSystemNamesVersion } from '../../lib/system-names';

export const TRYIT_POLL_MS = 5_000;

/* ── Steward visibility (module-cached /api/auth/me — obCheckAdmin idiom) ── */

let _stewardCheck: Promise<boolean> | null = null;

function checkSteward(): Promise<boolean> {
  if (!_stewardCheck) {
    _stewardCheck = requestJson<{ is_admin?: boolean }>('/api/auth/me')
      .then((me) => !!me?.is_admin)
      .catch(() => false); // non-admin / unauthenticated / offline — hide, never break
  }
  return _stewardCheck;
}

/** Test seam — clear the module-cached steward check between tests. */
export function _resetStewardCheck(): void {
  _stewardCheck = null;
}

/** True once /api/auth/me confirms is_admin; false (hidden) otherwise.
 * The check runs ONCE per page (module cache) no matter how many cards
 * mount, and a failed check never flips state (no act-time churn). */
export function useStewardVisible(): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void checkSteward().then((ok) => {
      if (!cancelled && ok) setVisible(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return visible;
}

/* ── Job state machine ──────────────────────────────────────────────────── */

type TryItPhase = 'confirm' | 'running' | 'busy' | 'error' | 'done';

interface TryItState {
  phase: TryItPhase;
  jobId: string;
  error: string;
  result: TryItResult | null;
}

const INITIAL: TryItState = { phase: 'confirm', jobId: '', error: '', result: null };

type SetTryItState = Dispatch<SetStateAction<TryItState>>;

/** POST the launch. The steward-editable test question rides the body as
 * 'question' when non-empty (trimmed) — the server uses it for BOTH runs;
 * an emptied input falls back to the proposal's query_context server-side
 * (which 400s honestly when that context is an unusable prompt fragment).
 * 202 → running (job id kept for the poll loop); 409 → the honest "already
 * running" state (ONE concurrent job app-wide); any other failure → error
 * state with the server's own message. */
async function launchTryIt(
  mappingId: string,
  question: string,
  setState: SetTryItState,
): Promise<void> {
  setState((s) => ({ ...s, error: '' }));
  const q = question.trim();
  try {
    const res = await requestJson<TryItLaunch>('/api/admin/learning/tryit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(q ? { mapping_id: mappingId, question: q } : { mapping_id: mappingId }),
    });
    setState({ phase: 'running', jobId: res.job_id, error: '', result: null });
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      setState((s) => ({ ...s, phase: 'busy' }));
    } else {
      setState((s) => ({ ...s, phase: 'error', error: (err as Error).message }));
    }
  }
}

/** Poll GET /tryit/{job_id} every 5 s while running; the interval is torn
 * down the moment the phase leaves 'running' (done/error/unmount), so
 * polling can never outlive the job. Plain setInterval — deliberately NOT
 * the SWR loader (30 s FETCH_TIMEOUT_MS budget is for panel data, and a
 * try-it job outlives it by design). */
function useTryItPoll(state: TryItState, setState: SetTryItState): void {
  const { phase, jobId } = state;
  useEffect(() => {
    if (phase !== 'running' || !jobId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const job = await requestJson<TryItJob>(
          `/api/admin/learning/tryit/${encodeURIComponent(jobId)}`,
        );
        if (cancelled || !job) return;
        if (job.status === 'done') {
          setState((s) => ({ ...s, phase: 'done', result: job.result ?? null }));
        } else if (job.status === 'error') {
          // X-5: the server's sanitized detail when present; generic copy
          // only when absent.
          const detail = job.error_detail || job.error || 'try-it run failed';
          setState((s) => ({ ...s, phase: 'error', error: detail }));
        } // 'running' — keep polling
      } catch (err) {
        if (!cancelled) setState((s) => ({ ...s, phase: 'error', error: (err as Error).message }));
      }
    };
    const id = setInterval(() => void poll(), TRYIT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [phase, jobId, setState]);
}

/* ── Presentational pieces ──────────────────────────────────────────────── */

const secLabelStyle = {
  fontSize: 10,
  fontWeight: 600,
  color: 'var(--muted)',
  textTransform: 'uppercase',
  marginTop: 6,
} as const;

/** Explicit cost warning BEFORE anything launches — a try-it answers one
 * real question twice against the live agent (~2 min each). The test
 * question is steward-editable (pre-filled with the proposal's
 * query_context, which is often a prompt FRAGMENT that confuses the
 * baseline run — the quality fix behind the 'question' override). */
function TryItConfirm({
  question,
  onQuestionChange,
  onLaunch,
  onClose,
}: {
  question: string;
  onQuestionChange: (value: string) => void;
  onLaunch: () => void;
  onClose: () => void;
}) {
  return (
    <div className="tryit-confirm">
      <div className="tryit-warning" role="note" style={{ fontSize: 12, color: 'var(--text2)' }}>
        ⚠ This runs <b>one real question twice</b> — once without this rule and once with it —
        against the live agent. Each run takes about 2 minutes and incurs normal model cost.
        Nothing is written to the knowledge graph.
      </div>
      <label
        className="tryit-question-label"
        style={{ display: 'block', marginTop: 6, fontSize: 12, color: 'var(--text2)' }}
      >
        Test question — edit if this isn&apos;t a real, answerable question
        <input
          className="tryit-question-input"
          type="text"
          value={question}
          maxLength={500}
          onChange={(e) => onQuestionChange(e.target.value)}
          style={{ display: 'block', width: '100%', marginTop: 4, fontSize: 12, boxSizing: 'border-box' }}
        />
      </label>
      <div className="tryit-actions" style={{ marginTop: 6, display: 'flex', gap: 8 }}>
        <button className="tryit-launch" onClick={onLaunch} title="Start the two runs">
          ▶ Run try-it
        </button>
        <button className="tryit-close" onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function TryItRunning({ question, onClose }: { question: string; onClose: () => void }) {
  return (
    <div className="tryit-running" role="status" style={{ fontSize: 12, color: 'var(--text2)' }}>
      <div className="sl-spinner" />
      {question.trim() ? (
        <div className="tryit-question">
          <b>Question:</b> {question.trim()}
        </div>
      ) : null}
      <div>
        Try-it running — answering one real question twice (baseline first, then with this rule).
        This takes a few minutes; results appear here.
      </div>
      <button className="tryit-close" onClick={onClose} style={{ marginTop: 6 }}>
        Hide (the run continues on the server)
      </button>
    </div>
  );
}

function TryItBusy({ onRetry, onClose }: { onRetry: () => void; onClose: () => void }) {
  return (
    <div className="tryit-busy" role="status" style={{ fontSize: 12, color: 'var(--amber)' }}>
      A try-it is already running — only one can run at a time. Try again when it finishes.
      <div className="tryit-actions" style={{ marginTop: 6, display: 'flex', gap: 8 }}>
        <button className="tryit-launch" onClick={onRetry}>
          ↻ Try again
        </button>
        <button className="tryit-close" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

function TryItError({ error, onClose }: { error: string; onClose: () => void }) {
  return (
    <div className="tryit-error" role="alert" style={{ fontSize: 12, color: 'var(--red)' }}>
      <b>Try-it failed:</b> {error || 'unknown error'}
      <div className="tryit-actions" style={{ marginTop: 6 }}>
        <button className="tryit-close" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

/** Systems-queried chips for one run column. `systems_queried` holds system
 * ids (derived from tool names): each chip reads the system's catalog name
 * (lib/system-names.ts) and keeps the id as its key and hover title. */
function TryItSystems({ systems }: { systems: string[] }) {
  if (systems.length === 0) return <span className="tryit-none">none</span>;
  return (
    <>
      {systems.map((s) => (
        <span key={s} className="sl-chip" title={s}>
          <span className="sys">{systemName(s)}</span>
        </span>
      ))}
    </>
  );
}

/** Rules-applied list — candidate-only rules (per highlightIds) highlighted. */
function TryItRules({
  rules,
  highlightIds,
}: {
  rules: { id: string; label: string }[];
  highlightIds?: Set<string>;
}) {
  if (rules.length === 0) return <li className="tryit-none">none</li>;
  return (
    <>
      {rules.map((r) => (
        <li
          key={r.id}
          className={`tryit-rule${highlightIds?.has(r.id) ? ' tryit-rule--new' : ''}`}
          style={highlightIds?.has(r.id) ? { color: 'var(--green)', fontWeight: 600 } : undefined}
        >
          {r.label || r.id}
        </li>
      ))}
    </>
  );
}

/** One run column — same shape for baseline and candidate; candidate rules
 * absent from the baseline are highlighted via highlightIds. */
function TryItRunColumn({
  title,
  run,
  highlightIds,
}: {
  title: string;
  run: TryItRun;
  highlightIds?: Set<string>;
}) {
  const steps = run.translation_steps || [];
  const elapsed = Number(run.elapsed_seconds);
  return (
    <div className="tryit-col" style={{ flex: 1, minWidth: 0 }}>
      <div className="tryit-col-head" style={{ fontSize: 12, fontWeight: 600 }}>
        {title}
        {Number.isFinite(elapsed) ? (
          <span className="tryit-elapsed" style={{ marginLeft: 6, color: 'var(--muted)', fontWeight: 400 }}>
            {elapsed.toFixed(0)}s
          </span>
        ) : null}
      </div>
      <div style={secLabelStyle}>Systems queried</div>
      <div className="tryit-systems">
        <TryItSystems systems={run.systems_queried || []} />
      </div>
      <div style={secLabelStyle}>Rules applied</div>
      <ul className="tryit-rules" style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
        <TryItRules rules={run.rules_applied || []} highlightIds={highlightIds} />
      </ul>
      <div style={secLabelStyle}>Translation steps</div>
      <ol className="tryit-steps" style={{ margin: 0, paddingLeft: 16, fontSize: 12 }}>
        {steps.length > 0 ? (
          steps.map((step, i) => <li key={i}>{step}</li>)
        ) : (
          <li className="tryit-none">none</li>
        )}
      </ol>
      <div style={secLabelStyle}>Answer excerpt</div>
      <blockquote
        className="tryit-excerpt"
        style={{ margin: 0, padding: '4px 8px', borderLeft: '2px solid var(--border)', fontSize: 12 }}
      >
        {run.answer_excerpt || '(no answer text captured)'}
      </blockquote>
    </div>
  );
}

/** One rules delta segment — each rule renders its human label as text with
 * the rule id on the title attr (X-1: the diff carries {id,label} objects;
 * never string-join objects). */
function TryItDiffRules({ label, rules }: { label: string; rules: TryItRuleRef[] }) {
  return (
    <span className="tryit-diff-seg">
      {label}
      {': '}
      {rules.map((r, i) => (
        <Fragment key={r.id || String(i)}>
          {i > 0 ? ', ' : null}
          <span className="tryit-diff-rule" title={r.id}>
            {r.label || r.id}
          </span>
        </Fragment>
      ))}
    </span>
  );
}

/** Routing-delta strip (what the candidate CHANGED, server-computed).
 * When the candidate was not injected (X-4) an empty diff renders NOTHING
 * here — the honest not-injected banner speaks instead of the "no routing
 * difference" verdict, which would be a false comparison claim. */
function TryItDiffStrip({
  diff,
  suppressNoDiff,
}: {
  diff: TryItDiff | undefined;
  suppressNoDiff?: boolean;
}) {
  const segments: JSX.Element[] = [];
  const addSystems = (label: string, values: string[] | undefined) => {
    if (values && values.length > 0) {
      segments.push(
        <span className="tryit-diff-seg" key={label}>{`${label}: ${values.map((v) => systemName(v)).join(', ')}`}</span>,
      );
    }
  };
  addSystems('＋ systems', diff?.systems_added);
  addSystems('− systems', diff?.systems_removed);
  if (diff?.rules_added && diff.rules_added.length > 0) {
    segments.push(<TryItDiffRules key="＋ rules" label="＋ rules" rules={diff.rules_added} />);
  }
  if (diff?.rules_removed && diff.rules_removed.length > 0) {
    segments.push(<TryItDiffRules key="− rules" label="− rules" rules={diff.rules_removed} />);
  }
  if (segments.length === 0) {
    if (suppressNoDiff) return null;
    return (
      <div className="tryit-diff-strip tryit-diff-none" style={{ fontSize: 12, color: 'var(--muted)' }}>
        No routing difference observed between the two runs.
      </div>
    );
  }
  return (
    <div className="tryit-diff-strip" style={{ fontSize: 12, color: 'var(--text2)', fontWeight: 600 }}>
      {segments.map((seg, i) => (
        <Fragment key={i}>
          {i > 0 ? ' · ' : null}
          {seg}
        </Fragment>
      ))}
    </div>
  );
}

function TryItResultView({ result, onClose }: { result: TryItResult | null; onClose: () => void }) {
  // The run columns and the routing delta name systems: re-render when
  // catalog names arrive.
  useSystemNamesVersion();
  if (!result) {
    return <TryItError error="job finished but returned no result" onClose={onClose} />;
  }
  // Candidate-only rules = applied by the candidate run but not the baseline
  // (computed client-side from the two runs — robust to the diff's id/label
  // representation).
  const baselineIds = new Set((result.baseline?.rules_applied || []).map((r) => r.id));
  const newIds = new Set(
    (result.candidate?.rules_applied || [])
      .filter((r) => !baselineIds.has(r.id))
      .map((r) => r.id),
  );
  // X-4: an explicit false means the candidate run never carried the rule —
  // the two runs are the same seed, so a "no difference" verdict would lie.
  const notInjected = result.candidate_injected === false;
  return (
    <div className="tryit-result">
      <div className="tryit-question" style={{ fontSize: 12, color: 'var(--text2)' }}>
        <b>Question:</b> {result.question}
      </div>
      {notInjected ? (
        <div
          className="tryit-notinjected"
          role="alert"
          style={{ fontSize: 12, color: 'var(--amber)', fontWeight: 600, marginTop: 4 }}
        >
          {"The candidate wasn't part of this run's routing context, so this comparison does not reflect the mapping's effect."}
        </div>
      ) : null}
      <TryItDiffStrip diff={result.diff} suppressNoDiff={notInjected} />
      <div className="tryit-cols" style={{ display: 'flex', gap: 12, marginTop: 6 }}>
        <TryItRunColumn title="Baseline (without this rule)" run={result.baseline} />
        <TryItRunColumn
          title="Candidate (with this rule)"
          run={result.candidate}
          highlightIds={newIds}
        />
      </div>
      <div className="tryit-actions" style={{ marginTop: 8 }}>
        <button className="tryit-close" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

/* ── The drawer ─────────────────────────────────────────────────────────── */

export function TryItPanel({ m, onClose }: { m: ProposedMapping; onClose: () => void }) {
  const [state, setState] = useState<TryItState>(INITIAL);
  // Pre-filled with the proposal's query_context; steward-editable before
  // launch (many proposals carry a prompt fragment there, not a question).
  // Reviewed (react-props-in-state): deliberate seed-once — the drawer is
  // conditionally mounted per open (SlCards), so it reseeds on each open.
  // nosemgrep
  const [question, setQuestion] = useState(() => (m.query_context ?? '').trim());
  useTryItPoll(state, setState);
  const launch = () => void launchTryIt(m.mapping_id, question, setState);

  let body: JSX.Element;
  if (state.phase === 'confirm')
    body = (
      <TryItConfirm
        question={question}
        onQuestionChange={setQuestion}
        onLaunch={launch}
        onClose={onClose}
      />
    );
  else if (state.phase === 'running') body = <TryItRunning question={question} onClose={onClose} />;
  else if (state.phase === 'busy') body = <TryItBusy onRetry={launch} onClose={onClose} />;
  else if (state.phase === 'error') body = <TryItError error={state.error} onClose={onClose} />;
  else body = <TryItResultView result={state.result} onClose={onClose} />;

  return (
    <div
      className="tryit-panel"
      data-mapping-id={m.mapping_id}
      style={{ marginTop: 8, padding: 8, border: '1px solid var(--border)', borderRadius: 6 }}
    >
      <div className="tryit-head" style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
        Try it — impact preview
      </div>
      {body}
    </div>
  );
}
