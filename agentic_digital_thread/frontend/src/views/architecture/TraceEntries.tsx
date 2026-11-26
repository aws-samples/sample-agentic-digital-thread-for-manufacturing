// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Investigation trace panel — addTraceEntry / _updateQuestionTimeline,
 * rendered from arch-store TraceEntry state.
 * Contractual DOM: #reasoning-trace > .trace-header + #question-timeline +
 * #trace-entries > .trace-entry[data-node-id][data-question] with .trace-label,
 * .trace-body/.trace-md and the .trace-expand-toggle caret. */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import type { TraceEntry } from '../../lib/arch-store';
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import { Md } from '../../shared/ui/Md';
import { correlationStore, selectCall } from './correlation-store';

/** Collapsed trace-body height (px) — matches dashboard.css `.trace-body
 * { max-height: 120px }`. */
export const TRACE_CLAMP_PX = 120;

/** Lead-in for the trace row's "why" line, per non-success outcome. */
const OUTCOME_REASON_LABEL: Record<string, string> = {
  failed: 'Marked failed',
  timeout: 'Timed out',
  empty: 'No data',
  partial: 'Partial',
  filtered: 'Filtered',
};

/** Pure overflow rule: does a trace body of `scrollHeight` px actually
 * overflow the collapsed clamp? 1px tolerance avoids a dangling toggle on
 * entries that graze the cap. Exported for unit tests. */
export function traceBodyOverflows(scrollHeightPx: number, clampPx = TRACE_CLAMP_PX): boolean {
  return scrollHeightPx > clampPx + 1;
}

interface TraceEntriesProps {
  entries: TraceEntry[];
  questionCounter: number;
  activeQuestionFilter: number | null;
  onFilterQuestion: (q: number | null) => void;
  /** Parent-controlled visibility (original: `hidden` class managed by
   * switchArchTab + the user_msg handler; a fresh question shows the pane). */
  visible: boolean;
}

function TraceEntryRow({ entry, hidden }: { entry: TraceEntry; hidden: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const selection = useStore(correlationStore);
  /* Show-more affordance (arch-visual-polish): the body is measured after
   * every render of its markdown — the toggle + bottom fade only exist when
   * the content actually overflows the collapsed clamp (no dangling button
   * on short entries). Expansion animates max-height between the clamp and
   * the measured scrollHeight (both explicit px, so the CSS transition in
   * enhancements.css runs; `.expanded { max-height: none }` would jump). */
  const bodyRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [contentH, setContentH] = useState(0);

  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    // scrollHeight is the full content height even while clamped.
    const h = body.scrollHeight;
    setContentH(h);
    setOverflows(traceBodyOverflows(h));
  }, [entry.text, expanded]);

  const stepLabel =
    entry.type === 'decision' ? `Step ${entry.step} · Reasoning` : `Step ${entry.step} · ${entry.label}`;
  const toggleLabel = expanded ? 'Collapse trace' : 'Expand trace';
  const showToggle = !!entry.text && overflows;
  const bodyClass = `trace-body${expanded ? ' expanded' : ''}${
    showToggle && !expanded ? ' clamped' : ''
  }`;
  // Honest outcome styling: failed/timeout → red dot; empty → hollow amber
  // dot; partial → half-filled amber dot.
  const outcomeClass = entry.outcome ? ` ${entry.outcome}` : '';
  // Correlation: rows that report a completed call are clickable — selecting
  // one highlights the matching HUD chip and pans the graph to the call's
  // nodes; a graph/hud-sourced selection highlights this row.
  const correlated = !!entry.callKey && selection?.callKey === entry.callKey;
  const selectRow = entry.callKey ? () => selectCall('trace', entry.callKey) : undefined;
  return (
    <div
      className={`trace-entry ${entry.type}${outcomeClass}${correlated ? ' correlated' : ''}${
        entry.callKey ? ' has-call' : ''
      }`}
      data-node-id={entry.nodeId || undefined}
      data-question={entry.question}
      data-outcome={entry.outcome || undefined}
      data-call-key={entry.callKey || undefined}
      style={hidden ? { display: 'none' } : undefined}
      onClick={selectRow}
    >
      <div className="trace-label">
        <span>
          {stepLabel}
          {entry.retry ? (
            <span className="trace-retry-badge" title="Re-call after an earlier failed call to this system">
              retry
            </span>
          ) : null}
          {entry.recovered ? (
            <span className="trace-retry-badge" title="A later retry to this system succeeded">
              retried ✓
            </span>
          ) : null}
        </span>
        {showToggle ? (
          <button
            type="button"
            className="trace-expand-toggle"
            title={toggleLabel}
            aria-label={toggleLabel}
            aria-expanded={expanded}
            onClick={(e) => {
              e.stopPropagation(); // expand/collapse must not toggle correlation
              setExpanded((x) => !x);
            }}
          >
            {expanded ? '⌃' : '⌄'}
          </button>
        ) : null}
      </div>
      {entry.text ? (
        // .trace-body (clamp/fade/animation host) wraps the shared markdown
        // component (.trace-md) — the classes previously shared one element;
        // all CSS consumers (`.trace-body …`, `.trace-md table`) still match.
        <div
          ref={bodyRef}
          className={bodyClass}
          style={expanded && contentH > 0 ? { maxHeight: contentH } : undefined}
        >
          {/* Shared markdown component (jsx-migration W2.4; react-markdown
              since the zero-innerHTML migration — element rendering, NOT the
              mdToSafeHtml string pipeline the old window probe resolved to;
              raw HTML in entry text is inert). MdErrorBoundary degrades to
              plain text if the parser throws. */}
          <Md text={entry.text} className="trace-md" />
        </div>
      ) : null}
      {entry.outcome && entry.outcomeReason ? (
        <div className="trace-outcome-reason">
          {OUTCOME_REASON_LABEL[entry.outcome] ?? 'Marked failed'}: {entry.outcomeReason}
        </div>
      ) : null}
      {showToggle ? (
        <button
          type="button"
          className="trace-show-toggle"
          aria-expanded={expanded}
          onClick={(e) => {
            e.stopPropagation(); // expand/collapse must not toggle correlation
            setExpanded((x) => !x);
          }}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      ) : null}
    </div>
  );
}

export default function TraceEntries({
  entries,
  questionCounter,
  activeQuestionFilter,
  onFilterQuestion,
  visible,
}: TraceEntriesProps) {
  const { t } = useI18n();
  const traceRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the newest entry (original: trace.scrollTop = scrollHeight).
  useEffect(() => {
    const el = traceRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries.length]);

  // Question timeline — "All · Q1 · Q2 …" (inline styles match the original).
  const timeline: JSX.Element[] = [];
  if (questionCounter > 0) {
    timeline.push(
      <span
        key="all"
        style={{
          cursor: 'pointer',
          color: activeQuestionFilter === null ? 'var(--accent)' : 'var(--muted)',
          fontWeight: activeQuestionFilter === null ? 600 : undefined,
        }}
        onClick={() => onFilterQuestion(null)}
      >
        All
      </span>,
    );
    for (let i = 1; i <= questionCounter; i++) {
      const isActive = activeQuestionFilter === i;
      timeline.push(
        <span key={`dot-${i}`} style={{ color: 'var(--muted)', marginLeft: '0.15rem' }}>
          ·
        </span>,
      );
      timeline.push(
        <span
          key={`q-${i}`}
          style={{
            cursor: 'pointer',
            color: isActive ? 'var(--accent)' : 'var(--muted)',
            fontWeight: isActive ? 600 : undefined,
          }}
          onClick={() => onFilterQuestion(i)}
        >
          Q{i}
        </span>,
      );
    }
  }

  return (
    <div
      id="reasoning-trace"
      ref={traceRef}
      className={visible ? '' : 'hidden'}
      style={visible ? { flex: '0 0 35%' } : undefined}
    >
      <div className="trace-header">
        <div>{t('arch.trace')}</div>
        <div
          id="question-timeline"
          style={{
            display: 'flex',
            gap: '0.25rem',
            padding: '0.35rem 0 0',
            flexWrap: 'wrap',
            fontSize: '0.7rem',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          {timeline}
        </div>
      </div>
      <div id="trace-entries">
        {entries.map((e) => (
          <TraceEntryRow
            key={e.id}
            entry={e}
            hidden={activeQuestionFilter !== null && e.question !== activeQuestionFilter}
          />
        ))}
      </div>
    </div>
  );
}
