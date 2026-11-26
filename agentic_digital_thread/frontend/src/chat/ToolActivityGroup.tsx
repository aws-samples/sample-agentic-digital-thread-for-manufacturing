// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ToolActivityGroup/> — ONE activity line for a run of back-to-back tool
 * calls (chat/tool-activity.ts groups + summarizes them).
 *
 *   div.tool-activity [data-state=active|done] [data-tone] [data-open]
 *     button.tool-activity-line [aria-expanded] [aria-controls]
 *       span.tool-activity-status          (pulsing dot while active)
 *       span.tool-activity-text            (.shimmer-text while active)
 *       span.tool-activity-meta            ("1 of 3" / "4 calls · 3.1s")
 *       span.tool-activity-chevron
 *     div.tool-activity-body#…             (grid-rows 0fr → 1fr expand)
 *       div.tool-activity-calls
 *         div.tool-activity-call [data-outcome]
 *           button.tool-pill.inline[data-tool-id] (+ .done, data-kgq)
 *                                    ("ERP · <what was asked>"; KG keeps its
 *                                     question label; title = full text +
 *                                     the outcome reason)
 *           span.tool-activity-call-time
 *         div.tool-trace[data-tool-id] (+ .visible)  — input + result
 *
 * The per-call rows keep the `.tool-pill.inline` / `.tool-trace` contract
 * (chat-thread.test.tsx, thread-clicks.ts); collapsed rows stay mounted and
 * are removed from the accessibility tree by `visibility: hidden` once the
 * collapse transition ends (tool-activity.css). Open state is view-local:
 * it never touches the thread store, so expanding never re-renders siblings. */
import { useId, useState } from 'react';

import { tf, useI18n } from '../lib/i18n';
import { useSystemNamesVersion } from '../lib/system-names';
import { Md } from '../shared/ui/Md';
import { callRowLabel, callState, formatDuration, summarizeToolGroup } from './tool-activity';
import { formatToolInput } from './tool-labels';
import { ToolInputBlock } from './ToolInputBlock';
import type { ThreadStore } from './thread-store';
import type { ToolPill, ToolTrace } from './turn-model';
import './tool-activity.css';

/** One `.tool-trace` — input block alone until tool_result, then input +
 * the call's stored telemetry-coverage lines (`.trace-coverage-line`), the
 * `.trace-result` summary markdown, and the call's stored telemetry sample
 * line (`.trace-sample-line`) below it; the stored lines are plain text. */
export function ToolTraceDiv({
  trace,
  showInputSummary = true,
  id,
}: {
  trace: ToolTrace;
  showInputSummary?: boolean;
  /** DOM id, so the call row's button can name it in aria-controls. */
  id?: string;
}) {
  const showResult = trace.visible || !!trace.resultMd || !!trace.sampleLine || !!trace.coverageLines;
  return (
    <div className={`tool-trace${trace.visible ? ' visible' : ''}`} data-tool-id={trace.toolId} id={id}>
      {trace.toolInput != null ? (
        <ToolInputBlock
          input={trace.toolInput}
          tool={trace.tool}
          showSummary={showInputSummary}
          routeContext={trace.routeContext}
        />
      ) : null}
      {showResult ? (
        <div className="trace-result">
          {trace.coverageLines?.map((line, i) => (
            <p key={i} className="trace-coverage-line">
              {line}
            </p>
          ))}
          <Md bare text={trace.resultMd || ''} />
          {trace.sampleLine ? <p className="trace-sample-line">{trace.sampleLine}</p> : null}
        </div>
      ) : null}
    </div>
  );
}


function CallRow({
  pill,
  trace,
  turnId,
  store,
}: {
  pill: ToolPill;
  trace: ToolTrace | undefined;
  turnId: string;
  store: ThreadStore;
}) {
  const { t } = useI18n();
  const state = callState(pill, trace);
  const traceId = useId();
  const reason = trace?.outcomeReason;
  const label = callRowLabel(pill, trace);
  // The row already names what was asked: don't repeat it as "input: …"
  // (a truncated label keeps the full summary line).
  const asked = trace?.toolInput != null ? formatToolInput(pill.tool, trace.toolInput).summary : '';
  const showInputSummary = !(asked && label.includes(asked));
  const time = trace?.elapsedMs != null && pill.done ? formatDuration(trace.elapsedMs) : '';
  return (
    <>
      <div className="tool-activity-call" data-outcome={state}>
        <button
          type="button"
          className={`tool-pill inline${pill.done ? ' done' : ''}`}
          data-tool-id={pill.toolId}
          data-kgq={pill.kgq ? '1' : undefined}
          aria-expanded={trace ? trace.visible : undefined}
          aria-controls={trace ? traceId : undefined}
          aria-label={tf('activity.callAria', { label, outcome: t(`activity.outcome.${state}`) })}
          title={reason ? `${label} (${reason})` : label}
          onClick={(e) => {
            // stopPropagation: the container's delegated thread-click handler
            // (thread-clicks.ts) must never double-toggle store-owned state.
            e.stopPropagation();
            store.toggleTrace(turnId, pill.toolId);
          }}
        >
          {label}
        </button>
        {time ? <span className="tool-activity-call-time">{time}</span> : null}
      </div>
      {trace ? <ToolTraceDiv trace={trace} showInputSummary={showInputSummary} id={traceId} /> : null}
    </>
  );
}

export interface ToolActivityGroupProps {
  pills: ToolPill[];
  traces: ReadonlyMap<string, ToolTrace>;
  turnId: string;
  store: ThreadStore;
}

export function ToolActivityGroup({ pills, traces, turnId, store }: ToolActivityGroupProps) {
  // Subscribes to the language store: the summary phrases are dictionary
  // strings, so a language switch must re-render the line. Same for the
  // catalog system names the line and its rows show (lib/system-names.ts).
  useI18n();
  useSystemNamesVersion();
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const s = summarizeToolGroup(pills, traces);
  return (
    <div
      className="tool-activity"
      data-state={s.active ? 'active' : 'done'}
      data-tone={s.tone}
      data-open={open ? 'true' : 'false'}
    >
      <button
        type="button"
        className="tool-activity-line"
        aria-expanded={open}
        aria-controls={bodyId}
        title={s.meta ? `${s.text} (${s.meta})` : s.text}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
      >
        <span className="tool-activity-status" aria-hidden="true" />
        <span className={`tool-activity-text${s.active ? ' shimmer-text' : ''}`}>{s.text}</span>
        {s.meta ? <span className="tool-activity-meta">{s.meta}</span> : null}
        <span className="tool-activity-chevron" aria-hidden="true" />
      </button>
      <div className="tool-activity-body" id={bodyId}>
        <div className="tool-activity-calls">
          {pills.map((pill) => (
            <CallRow
              key={pill.toolId}
              pill={pill}
              trace={traces.get(pill.toolId)}
              turnId={turnId}
              store={store}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
