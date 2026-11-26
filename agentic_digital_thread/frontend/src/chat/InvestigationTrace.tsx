// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <InvestigationTrace/> — the Wave-6 collapsible "Investigation trace"
 * section rendered at the BOTTOM of <EvidenceCard/> (frozen contract W6-F6).
 *
 * Shows the system agent's full path for ONE cited Mode-A call: the
 * delegated sub-question ("Asked: …"), each tool step (the agent's own
 * reasoning text, tool name/status/timing, bounded input and output with
 * honest truncation lines), the oldest-dropped honesty line when
 * `steps_dropped > 0`, and the final answer ("Responded: …") with the
 * graph names the route sends for its ids (`final_answer_names`) placed
 * after the markdown is parsed (chat/trace-id-names.ts), so a name shows
 * as text and never as markup, a link or a time.
 *
 * Fetch-on-FIRST-expand only (the Tier3TechnicalDetail onToggle pattern,
 * including the `e.target !== e.currentTarget` guard). No `queryRef` ⇒ the
 * honest line WITHOUT any fetch; 403/404/network collapse to the SAME
 * honest line — "Investigation trace not available for this claim" (covers
 * legacy agents and the 24 h TTL) — never an empty spinner.
 *
 * Sink policy (relaxed 2026-08-06 for PROSE ONLY — still zero innerHTML):
 * - MARKDOWN-RENDERED: step `reasoning` and the `final_answer` — the
 *   model's PROSE — through <TraceProse/>, the same react-markdown element
 *   path the answer <Md/> uses (no rehype-raw: raw HTML in the source is
 *   INERT — dropped, never parsed into live DOM; links render as plain
 *   text spans, never anchors; markdown images render as their alt text,
 *   never <img>). The component map is CONSTRAINED for the narrow trace
 *   column: h1–h6 stay near body size in subtle ascending steps, tables
 *   get compact borders in a horizontal-scroll wrapper, code/pre stay
 *   monospace and bounded.
 * - STILL LITERAL TEXT NODES: `question`, every truncation/honesty line,
 *   and — deliberate containment boundary — `tool_input`/`tool_output`.
 *   Tool payloads are DATA, not prose: they render inside literal-text
 *   <pre> blocks (now with copy buttons via <CopyablePre/>), never
 *   markdown, never highlighted, never HTML.
 * Hostile strings stay inert on BOTH paths (pinned in
 * investigation-trace.test.tsx). Defensive filters on every field:
 * non-string ⇒ omitted. */

import { useMemo, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';

import {
  fetchInvestigationTrace,
  type InvestigationTraceDetail,
  type InvestigationTraceStep,
} from '../api/provenance';
import { getSessionId } from '../lib/session';
import { useI18n } from '../lib/i18n';
import { MD_REMARK_PLUGINS, MdErrorBoundary } from '../shared/ui/Md';
import { CopyablePre } from './CopyButton';
import { readIdNames, remarkIdNames, type IdNames } from './trace-id-names';

/** The W6-F6 honest-absence line (legacy / expired / 403 / 404 / no ref). */
const TRACE_UNAVAILABLE = 'Investigation trace not available for this claim';

const MUTED_ITALIC: React.CSSProperties = {
  color: 'var(--muted)',
  fontStyle: 'italic',
};

const PRE_STYLE: React.CSSProperties = {
  margin: '0.2rem 0 0',
  padding: '0.3rem 0.5rem',
  border: '1px solid var(--border)',
  borderRadius: 4,
  fontSize: '0.65rem',
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
};

/* ---- constrained markdown for trace PROSE (reasoning / final answer) ---- */

type MdChild = { children?: ReactNode };

/** Small-header factory: headers stay close to body size (subtle
 * ascending-only steps — explicitly NOT large document headers). */
function traceHeading(Tag: 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6', fontSize: string) {
  return function TraceHeading({ children }: MdChild) {
    return <Tag style={{ fontSize, fontWeight: 600, margin: '0.5em 0 0.25em' }}>{children}</Tag>;
  };
}

const CELL_STYLE: React.CSSProperties = {
  border: '1px solid var(--border)',
  padding: '0.1rem 0.35rem',
  textAlign: 'left',
};

/** Constrained component map: small headers, compact scroll-safe tables,
 * bounded monospace code, NO live links (<a> renders as a text span) and
 * NO <img> (renders as its alt text) — model output stays contained. */
const TRACE_MD_COMPONENTS: Components = {
  h1: traceHeading('h1', '1.12em'),
  h2: traceHeading('h2', '1.09em'),
  h3: traceHeading('h3', '1.06em'),
  h4: traceHeading('h4', '1.03em'),
  h5: traceHeading('h5', '1em'),
  h6: traceHeading('h6', '1em'),
  p: ({ children }: MdChild) => <p style={{ margin: '0.3em 0' }}>{children}</p>,
  ul: ({ children }: MdChild) => (
    <ul style={{ margin: '0.3em 0', paddingLeft: '1.2em' }}>{children}</ul>
  ),
  ol: ({ children }: MdChild) => (
    <ol style={{ margin: '0.3em 0', paddingLeft: '1.2em' }}>{children}</ol>
  ),
  a: ({ children }: MdChild) => <span>{children}</span>,
  img: (props: { alt?: string }) => <span>{typeof props.alt === 'string' ? props.alt : ''}</span>,
  table: ({ children }: MdChild) => (
    <div style={{ maxWidth: '100%', overflowX: 'auto', margin: '0.3em 0' }}>
      <table style={{ borderCollapse: 'collapse', fontSize: '0.95em' }}>{children}</table>
    </div>
  ),
  th: ({ children }: MdChild) => <th style={{ ...CELL_STYLE, fontWeight: 600 }}>{children}</th>,
  td: ({ children }: MdChild) => <td style={CELL_STYLE}>{children}</td>,
  pre: ({ children }: MdChild) => (
    <pre style={{ ...PRE_STYLE, maxHeight: '12rem', overflow: 'auto' }}>{children}</pre>
  ),
  code: ({ children }: MdChild) => <code style={{ fontSize: '0.95em' }}>{children}</code>,
};

/** Model PROSE (reasoning / final answer) as constrained markdown — react
 * elements only (zero innerHTML); parser failure degrades to the raw text
 * as a literal text node via MdErrorBoundary. The trace prose goes through
 * no text pre-transform (unlike <Md/>), only MD_REMARK_PLUGINS; `names`
 * (the answer's only) are placed by a plugin listed after all of them. */
function TraceProse({
  text,
  className,
  names = null,
}: {
  text: string;
  className: string;
  names?: IdNames | null;
}) {
  const plugins = useMemo(
    () => (names ? [...MD_REMARK_PLUGINS, remarkIdNames(names)] : MD_REMARK_PLUGINS),
    [names],
  );
  return (
    <div className={className}>
      <MdErrorBoundary fallbackText={text}>
        <ReactMarkdown remarkPlugins={plugins} components={TRACE_MD_COMPONENTS}>
          {text}
        </ReactMarkdown>
      </MdErrorBoundary>
    </div>
  );
}

/* ------------------------------ trace body ------------------------------ */

type TraceState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; detail: InvestigationTraceDetail }
  | { status: 'unavailable' };

/** Defensive string read — non-string wire values are omitted, never
 * coerced into a rendered node. */
function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Honest truncation line for a bounded field ("input truncated — N bytes
 * total"); '' when the flag is absent (nothing renders). */
function truncationLine(label: string, flagged: unknown, bytes: unknown): string {
  if (flagged !== true) return '';
  return typeof bytes === 'number'
    ? `${label} truncated — ${bytes} bytes total`
    : `${label} truncated`;
}

/** A muted-italic honesty line; renders nothing for ''. */
function Note({ text }: { text: string }) {
  if (text === '') return null;
  return (
    <div className="investigation-truncation" style={MUTED_ITALIC}>
      {text}
    </div>
  );
}

/** One tool step: reasoning as constrained markdown PROSE (when non-empty),
 * the tool header (name · status · duration), bounded input/output as
 * literal-text <pre> blocks (DATA — never markdown) with copy buttons and
 * honest truncation lines. */
function StepView({ step }: { step: InvestigationTraceStep }) {
  const { t } = useI18n();
  const reasoning = str(step.reasoning);
  const toolName = str(step.tool_name) || 'tool';
  const status = str(step.status) || 'unknown';
  const duration =
    typeof step.duration_ms === 'number' ? ` · ${step.duration_ms} ms` : '';
  const input = str(step.tool_input);
  const output = str(step.tool_output);
  const inputNote = truncationLine('input', step.input_truncated, step.input_bytes);
  const outputNote = truncationLine('output', step.output_truncated, step.output_bytes);
  const reasoningNote = truncationLine('reasoning', step.reasoning_truncated, undefined);
  return (
    <div className="investigation-step" style={{ marginTop: '0.4rem' }}>
      {reasoning !== '' && <TraceProse text={reasoning} className="investigation-reasoning" />}
      <Note text={reasoningNote} />
      <div className="investigation-tool-header" style={{ fontWeight: 600, color: 'var(--muted)' }}>
        {toolName} · {status}
        {duration}
      </div>
      {input !== '' && (
        <CopyablePre
          text={input}
          className="investigation-tool-input"
          preStyle={PRE_STYLE}
          copyLabel={t('chat.copyToolInput')}
        />
      )}
      <Note text={inputNote} />
      {output !== '' && (
        <CopyablePre
          text={output}
          className="investigation-tool-output"
          preStyle={PRE_STYLE}
          copyLabel={t('chat.copyToolOutput')}
        />
      )}
      <Note text={outputNote} />
    </div>
  );
}

/** The loaded trace body: question (literal text), dropped-steps honesty
 * line, ordered steps, final answer (constrained markdown PROSE). */
function TraceLoaded({ detail }: { detail: InvestigationTraceDetail }) {
  const question = str(detail.question);
  const answer = str(detail.final_answer);
  const names = useMemo(() => readIdNames(detail.final_answer_names), [detail]);
  const steps = (Array.isArray(detail.steps) ? detail.steps : []).filter(
    (s): s is InvestigationTraceStep => !!s && typeof s === 'object',
  );
  const dropped = typeof detail.steps_dropped === 'number' ? detail.steps_dropped : 0;
  const questionNote = truncationLine('question', detail.question_truncated, undefined);
  const answerNote = truncationLine(
    'answer',
    detail.final_answer_truncated,
    detail.final_answer_bytes,
  );
  return (
    <div className="investigation-body" style={{ marginTop: '0.3rem' }}>
      {question !== '' && (
        <div className="investigation-question">
          <span style={{ fontWeight: 600, color: 'var(--muted)' }}>Asked: </span>
          {question}
        </div>
      )}
      {questionNote !== '' && <Note text={questionNote} />}
      {dropped > 0 && (
        <div className="investigation-dropped" style={{ ...MUTED_ITALIC, marginTop: '0.2rem' }}>
          first {dropped} step{dropped === 1 ? '' : 's'} omitted to stay within storage bounds
        </div>
      )}
      {steps.map((step, i) => (
        <StepView key={`step-${typeof step.seq === 'number' ? step.seq : i}`} step={step} />
      ))}
      {answer !== '' && (
        <div className="investigation-answer" style={{ marginTop: '0.4rem' }}>
          <span style={{ fontWeight: 600, color: 'var(--muted)' }}>Responded: </span>
          <TraceProse text={answer} className="investigation-answer-prose" names={names} />
        </div>
      )}
      {answerNote !== '' && <Note text={answerNote} />}
    </div>
  );
}

export interface InvestigationTraceProps {
  /** The claim's Tier-3 redemption handle — '' (legacy/no record) renders
   * the honest line without any fetch. */
  queryRef: string;
}

/** The W6-F6 collapsible section. Fetches the trace on FIRST open only;
 * every failure path collapses to the honest absence line. */
export function InvestigationTrace({ queryRef }: InvestigationTraceProps) {
  const [state, setState] = useState<TraceState>({ status: 'idle' });

  const onToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    // Only this details' own toggle, and only the first open (fetch once).
    if (e.target !== e.currentTarget) return;
    if (!e.currentTarget.open || state.status !== 'idle') return;
    if (!queryRef) {
      setState({ status: 'unavailable' });
      return;
    }
    setState({ status: 'loading' });
    fetchInvestigationTrace(queryRef, getSessionId()).then(
      (detail) => setState({ status: 'loaded', detail }),
      () => setState({ status: 'unavailable' }),
    );
  };

  return (
    <details
      className="evidence-investigation"
      onToggle={onToggle}
      style={{ marginTop: '0.4rem' }}
    >
      <summary style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--muted)' }}>
        Investigation trace
      </summary>
      {state.status === 'loading' && (
        <div className="investigation-loading" style={{ marginTop: '0.3rem' }}>
          Loading investigation trace…
        </div>
      )}
      {state.status === 'unavailable' && (
        <div
          className="investigation-unavailable"
          style={{ ...MUTED_ITALIC, marginTop: '0.3rem' }}
        >
          {TRACE_UNAVAILABLE}
        </div>
      )}
      {state.status === 'loaded' && <TraceLoaded detail={state.detail} />}
    </details>
  );
}
