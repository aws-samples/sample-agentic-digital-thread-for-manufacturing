// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <SessionDrilldown/> — per-session observability drill-down for the Agents
 * page. A precision flight-recorder view: compact selectable session rows
 * (mono ids, relative time, paired token mini-bars, error chips) drive a
 * CSS-only waterfall timeline — proportional span bars colored by kind, a
 * summary stat strip, and thin per-span token-attribution sub-bars. All
 * geometry/format math lives in ./waterfall.ts (pure, unit-tested).
 *
 * Graceful degrade (DELIBERATE CONTRACTS — keep exactly):
 * - `{"enabled": false}` or a 404 (older backend) renders NOTHING, so
 *   gated-off deployments keep only the CloudWatch link-out affordance and
 *   never see a dead panel; the pending phase also renders nothing.
 * - The HTTP-200 honest-error payload (`error` string beside empty rows)
 *   renders the amber DegradedNotice INSTEAD of quiet-looking empty states.
 * JSX-only rendering (zero innerHTML-family sinks per repo policy). */

import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { ApiError } from '../../api/client';
import type { TraceSessionSummary, TraceSpan } from '../../api/types/agents';
import type { FetchLike } from './pricing';
import {
  traceSessionDetailQueryOptions,
  traceSessionsQueryOptions,
} from './trace-queries';
import {
  barGeometry,
  fmtDuration,
  fmtK,
  isErrorStatus,
  kindClass,
  maxSessionTokens,
  relativeTime,
  sessionMeta,
  sessionStats,
  spanAriaLabel,
  spanWindow,
  timeTicks,
  tokenSplit,
} from './waterfall';
import './session-drilldown.css';

export interface SessionDrilldownProps {
  /** Host container id — derived ids follow the AgentsFleet DOM convention. */
  containerId: string;
  /** Look-back window, kept in sync with the fleet time filter. */
  hours: number;
  /** Injectable raw-response transport retained for focused tests. */
  fetchFn?: FetchLike;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Honest-error contract (Wave O2 routes): a Logs Insights failure answers
 * HTTP 200 with EMPTY rows plus an `error` string — read it so a failed
 * query never masquerades as a genuinely quiet session/window. */
function degradedError(data: { error?: unknown }): string | null {
  return typeof data.error === 'string' && data.error !== '' ? data.error : null;
}

/** Distinct degraded affordance (repo honest-data convention): amber and
 * explicitly labelled, so it can never be confused with the muted
 * "nothing happened" empty states. */
function DegradedNotice({ id, error }: { id: string; error: string }) {
  return (
    <div id={id} role="status" style={degradedNoticeStyle}>
      <span aria-hidden="true">⚠</span> Trace query failed — results are incomplete, not
      empty: {error}
    </div>
  );
}

export function SessionDrilldown({ containerId, hours, fetchFn }: SessionDrilldownProps) {
  const [expanded, setExpanded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const sessionsQuery = useQuery(traceSessionsQueryOptions(hours, fetchFn));

  // Feature detection: gated-off deployments answer {"enabled": false}; a
  // backend without the routes 404s. Both mean "feature absent" — render
  // nothing rather than a dead panel. The pending phase also renders nothing
  // so gated-off environments never flash a placeholder.
  if (sessionsQuery.isPending) return null;
  if (sessionsQuery.isError) {
    if (sessionsQuery.error instanceof ApiError && sessionsQuery.error.status === 404) {
      return null;
    }
    return (
      <div
        id={`${containerId}-drilldown-error`}
        role="status"
        style={{ color: 'var(--red)', fontSize: '0.75rem', marginTop: '0.75rem' }}
      >
        Session drill-down unavailable: {errorMessage(sessionsQuery.error)}
      </div>
    );
  }
  if (sessionsQuery.data.enabled === false) return null;
  // Positive-signal feature detection: only a payload carrying the frozen
  // `sessions` array renders the panel — anything else degrades to nothing.
  if (!Array.isArray(sessionsQuery.data.sessions)) return null;

  const sessions: TraceSessionSummary[] = sessionsQuery.data.sessions;
  const sessionsError = degradedError(sessionsQuery.data);
  const panelId = `${containerId}-drilldown-panel`;
  const tokenScale = maxSessionTokens(sessions);

  return (
    <section
      id={`${containerId}-drilldown`}
      className="fleet-drilldown"
      aria-label="Per-session trace drill-down"
      style={{ marginTop: '1rem' }}
    >
      <button
        type="button"
        id={`${containerId}-drilldown-toggle`}
        className="sdd-toggle"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((value) => !value)}
      >
        <span aria-hidden="true">{expanded ? '▾' : '▸'}</span>
        <span className="sdd-toggle-label">
          Session Drill-down ({sessions.length} session{sessions.length === 1 ? '' : 's'})
        </span>
      </button>
      {expanded && (
        <div id={panelId}>
          {sessionsError && (
            <DegradedNotice id={`${containerId}-drilldown-degraded`} error={sessionsError} />
          )}
          {sessions.length === 0 ? (
            /* Only claim a quiet window when the query actually succeeded —
             * a failed query already rendered the degraded notice above. */
            !sessionsError && (
              <div style={mutedTextStyle}>No traced sessions in this window yet.</div>
            )
          ) : (
            <ul aria-label="Recent sessions" className="sdd-session-list">
              {sessions.map((session) => (
                <li key={session.session_id}>
                  <SessionRow
                    session={session}
                    tokenScale={tokenScale}
                    selected={selectedId === session.session_id}
                    onToggle={() =>
                      setSelectedId((current) =>
                        current === session.session_id ? null : session.session_id,
                      )
                    }
                  />
                </li>
              ))}
            </ul>
          )}
          {selectedId && (
            <SessionDetail
              containerId={containerId}
              sessionId={selectedId}
              hours={hours}
              fetchFn={fetchFn}
            />
          )}
        </div>
      )}
    </section>
  );
}

/** One compact selectable sessions-list row. Only session_id is contractual;
 * time / token mini-bars / error chip render solely from data the row
 * actually carries (never fabricated). */
function SessionRow({
  session,
  tokenScale,
  selected,
  onToggle,
}: {
  session: TraceSessionSummary;
  tokenScale: number;
  selected: boolean;
  onToggle: () => void;
}) {
  const meta = sessionMeta(session);
  const hasTokenData = tokenScale > 0 && (meta.tokensIn != null || meta.tokensOut != null);
  return (
    <button
      type="button"
      className="sdd-session-row"
      aria-pressed={selected}
      onClick={onToggle}
    >
      <span className="sdd-session-id" title={session.session_id}>
        {session.session_id}
      </span>
      <span className="sdd-session-time">{relativeTime(meta.lastMs)}</span>
      <span className="sdd-session-spans">
        {session.span_count != null ? `${session.span_count} spans` : '\u2014'}
      </span>
      {hasTokenData ? (
        <span
          className="sdd-token-mini"
          role="img"
          aria-label={`Tokens in ${fmtK(meta.tokensIn)}, out ${fmtK(meta.tokensOut)}`}
          title={`tokens in ${fmtK(meta.tokensIn)} / out ${fmtK(meta.tokensOut)}`}
        >
          <span
            className="sdd-token-in"
            style={{ width: `${((meta.tokensIn ?? 0) / tokenScale) * 100}%` }}
          />
          <span
            className="sdd-token-out"
            style={{ width: `${((meta.tokensOut ?? 0) / tokenScale) * 100}%` }}
          />
        </span>
      ) : (
        <span aria-hidden="true" />
      )}
      {meta.errors > 0 ? (
        <span className="sdd-err-chip" aria-label={`${meta.errors} errors`}>
          {meta.errors} err
        </span>
      ) : (
        <span aria-hidden="true" />
      )}
    </button>
  );
}

function SessionDetail({
  containerId,
  sessionId,
  hours,
  fetchFn,
}: {
  containerId: string;
  sessionId: string;
  /** Same look-back window as the sessions list — the detail Logs Insights
   * query must scan the SAME window or spans outside the default fall off. */
  hours: number;
  fetchFn?: FetchLike;
}) {
  const detailQuery = useQuery(traceSessionDetailQueryOptions(sessionId, hours, fetchFn));

  if (detailQuery.isPending) {
    return (
      <div className="sdd-skeleton skeleton-shimmer" aria-busy="true">
        <span className="visually-hidden">Loading session trace…</span>
        <div className="sdd-skeleton-row" style={{ width: '38%' }} aria-hidden="true" />
        <div className="sdd-skeleton-row" style={{ width: '82%' }} aria-hidden="true" />
        <div className="sdd-skeleton-row" style={{ width: '64%' }} aria-hidden="true" />
        <div className="sdd-skeleton-row" style={{ width: '71%' }} aria-hidden="true" />
      </div>
    );
  }
  if (detailQuery.isError) {
    return (
      <div role="status" style={{ color: 'var(--red)', fontSize: '0.75rem' }}>
        Failed to load session trace: {errorMessage(detailQuery.error)}
      </div>
    );
  }

  const detailError = degradedError(detailQuery.data);
  const spans = detailQuery.data.spans ?? [];

  // Honest-data: a failed Logs Insights query answers HTTP 200 with empty
  // spans + `error` — render the degraded affordance INSTEAD of a waterfall
  // whose empty state would falsely read as a quiet session.
  if (detailError && spans.length === 0) {
    return (
      <div id={`${containerId}-drilldown-detail`} style={{ marginTop: '0.5rem' }}>
        <DegradedNotice id={`${containerId}-drilldown-detail-degraded`} error={detailError} />
      </div>
    );
  }

  const stats = sessionStats(detailQuery.data);

  return (
    <div id={`${containerId}-drilldown-detail`} style={{ marginTop: '0.5rem' }}>
      {detailError && (
        <DegradedNotice id={`${containerId}-drilldown-detail-degraded`} error={detailError} />
      )}
      <dl className="sdd-stats" aria-label="Session totals">
        <StatCell label="Duration" value={fmtDuration(stats.wallClockMs)} />
        <StatCell label="Spans" value={String(stats.spanCount)} />
        <StatCell label="Tokens In" value={fmtK(stats.tokensIn)} />
        <StatCell label="Tokens Out" value={fmtK(stats.tokensOut)} />
        <StatCell label="Tool Calls" value={stats.toolCalls == null ? '\u2014' : String(stats.toolCalls)} />
        <StatCell label="Errors" value={String(stats.errorCount)} error={stats.errorCount > 0} />
      </dl>
      <Waterfall spans={spans} />
    </div>
  );
}

function StatCell({ label, value, error }: { label: string; value: string; error?: boolean }) {
  return (
    <div className="sdd-stat">
      <dt>{label}</dt>
      <dd className={error ? 'sdd-stat-error' : undefined}>{value}</dd>
    </div>
  );
}

/** CSS-only waterfall: each span a proportional horizontal bar positioned by
 * (start_ms − min) / range, colored by kind, with a subtle 25/50/75% tick
 * grid and per-row token-attribution sub-bars. Rows are a list whose
 * aria-labels carry name + duration + status. */
function Waterfall({ spans }: { spans: TraceSpan[] }) {
  if (spans.length === 0) {
    return <div style={mutedTextStyle}>No spans recorded for this session.</div>;
  }
  const timeWindow = spanWindow(spans);
  return (
    <div className="sdd-waterfall">
      <div className="sdd-wf-scroll">
        <div className="sdd-wf-list" role="list" aria-label="Span waterfall">
          {timeWindow && (
            /* Inside the list (not the scroll viewport) so the absolute
             * inset-0 layer spans the FULL content height — tick lines stay
             * behind every row on long, scrolled traces. */
            <div className="sdd-ticks" aria-hidden="true">
              <div className="sdd-ticks-track">
                {timeTicks(timeWindow).map((tick) => (
                  <span key={tick.pct}>
                    <span className="sdd-tick" style={{ left: `${tick.pct}%` }} />
                    <span className="sdd-tick-label" style={{ left: `${tick.pct}%` }}>
                      {tick.label}
                    </span>
                  </span>
                ))}
              </div>
            </div>
          )}
          {spans.map((span, index) => (
            <WaterfallRow
              key={`${span.name ?? 'span'}-${index}`}
              span={span}
              timeWindow={timeWindow}
              index={index}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Cap the entrance stagger so long traces don't queue seconds of delay. */
const MAX_STAGGER_STEPS = 20;

function WaterfallRow({
  span,
  timeWindow,
  index,
}: {
  span: TraceSpan;
  timeWindow: ReturnType<typeof spanWindow>;
  index: number;
}) {
  const geometry = timeWindow ? barGeometry(span, timeWindow) : null;
  const split = tokenSplit(span);
  const error = isErrorStatus(span.status);
  const name = span.name ?? '\u2014';
  return (
    <div
      role="listitem"
      className={`sdd-row ${kindClass(span.kind)}`}
      aria-label={spanAriaLabel(span)}
      style={{ animationDelay: `${Math.min(index, MAX_STAGGER_STEPS) * 30}ms` }}
    >
      <span className="sdd-row-label" title={span.name ?? undefined}>
        {name}
        {error && (
          <span className="sdd-err-glyph" aria-hidden="true">
            ✕
          </span>
        )}
      </span>
      <span className="sdd-track" aria-hidden="true">
        {geometry && (
          <>
            <span
              className={`sdd-bar${error ? ' sdd-bar--error' : ''}`}
              style={{ left: `${geometry.leftPct}%`, width: `${geometry.widthPct}%` }}
            />
            {split && (
              <span
                className="sdd-sub"
                style={{ left: `${geometry.leftPct}%`, width: `${geometry.widthPct}%` }}
              >
                <span className="sdd-sub-in" style={{ width: `${split.inPct}%` }} />
                <span className="sdd-sub-out" style={{ width: `${split.outPct}%` }} />
              </span>
            )}
          </>
        )}
      </span>
      <span className="sdd-dur">{fmtDuration(span.duration_ms)}</span>
    </div>
  );
}

const mutedTextStyle: React.CSSProperties = {
  color: 'var(--muted)',
  fontSize: '0.75rem',
  padding: '0.25rem 0',
};

/** Amber, bordered, and explicit — visually distinct from mutedTextStyle so
 * a failed trace query never reads as an honestly quiet window/session. */
const degradedNoticeStyle: React.CSSProperties = {
  color: 'var(--amber)',
  border: '1px solid var(--amber)',
  borderRadius: 3,
  fontSize: '0.75rem',
  padding: '0.35rem 0.6rem',
  margin: '0.5rem 0',
};
