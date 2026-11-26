// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tool-activity HUD — port of the #tool-hud DOM (architecture.js tool_start /
 * tool_result / done / user_msg HUD handling + toggleToolHud). Renders from
 * arch-store HudEntry state; a single 100ms interval ticks the elapsed time of
 * running rows (replacing the per-row setInterval of the original). Inline
 * styles and data-* attributes match the original rows.
 *
 * Collapse: the chip list is NOT rendered at all while collapsed — only the
 * toggle plus a count badge. (The previous CSS-only max-height squeeze lost
 * to the chips' inline padding/border, leaving overlapping colored slivers.)
 *
 * Correlation: clicking a tool chip selects its call (correlation-store);
 * the related trace row + graph nodes highlight, and a trace/graph-sourced
 * selection highlights the chip here via `.correlated`. */
import { useEffect, useState, type CSSProperties, type KeyboardEvent } from 'react';

import type { HudEntry } from '../../lib/arch-store';
import { useStore } from '../../lib/store';
import { correlationStore, selectCall } from './correlation-store';

interface ToolHudProps {
  entries: HudEntry[];
  activeQuestionFilter: number | null;
  onFilterQuestion: (q: number | null) => void;
}

const RUNNING_STYLE: CSSProperties = {
  padding: '0.2rem 0.5rem',
  background: 'rgba(0,216,255,0.12)',
  borderWidth: 1,
  borderStyle: 'solid',
  borderColor: 'rgba(0,216,255,0.3)',
  borderRadius: 3,
  color: '#00d8ff',
  whiteSpace: 'nowrap',
  // Chips are interactive (correlation click) — the .tool-hud container is
  // pointer-events: none, so each chip opts back in.
  pointerEvents: 'auto',
  cursor: 'pointer',
};
const OK_STYLE: CSSProperties = {
  ...RUNNING_STYLE,
  background: 'rgba(63,185,80,0.12)',
  borderColor: 'rgba(63,185,80,0.3)',
  color: '#3fb950',
};
/* Honest completion states — failed/empty/timeout/partial rows must NOT read
 * as green successes (and never as activity): static red / amber chips. */
const FAILED_STYLE: CSSProperties = {
  ...RUNNING_STYLE,
  background: 'rgba(248,81,73,0.12)',
  borderColor: 'rgba(248,81,73,0.4)',
  color: 'var(--red)',
};
const EMPTY_STYLE: CSSProperties = {
  ...RUNNING_STYLE,
  background: 'rgba(210,153,34,0.10)',
  borderColor: 'rgba(210,153,34,0.35)',
  color: 'var(--amber)',
};
const STOPPED_STYLE: CSSProperties = {
  ...RUNNING_STYLE,
  background: 'rgba(100,100,100,0.12)',
  borderColor: 'rgba(100,100,100,0.3)',
  color: 'var(--muted)',
};
const PARALLEL_STYLE: CSSProperties = {
  marginLeft: '0.75rem',
  borderLeftWidth: 2,
  borderLeftColor: 'rgba(0,216,255,0.5)',
};
const BATCH_STYLE: CSSProperties = {
  fontSize: '0.6rem',
  color: 'rgba(0,216,255,0.5)',
  padding: '0.1rem 0.5rem',
  letterSpacing: '0.05em',
  marginTop: '0.1rem',
};
const SUMMARY_STYLE: CSSProperties = {
  padding: '0.15rem 0.4rem',
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 3,
  color: 'var(--muted)',
  whiteSpace: 'nowrap',
  fontSize: '0.65rem',
  cursor: 'pointer',
  pointerEvents: 'auto',
};

/** Tooltip text plus WHY the outcome was chosen (classifier reason) — a red
 * ✗ must be explainable from the HUD alone. Pure; exported for tests. */
export function withReason(title: string | undefined, reason: string | undefined): string | undefined {
  if (!title) return reason ? `Why: ${reason}` : undefined;
  return reason ? `${title}\nWhy: ${reason}` : title;
}

function fmtSecs(ms: number): string {
  return (ms / 1000).toFixed(1) + 's';
}

/** Honest completion mark for a completed chip: ✗ (failed, tooltip = error
 * first line), '⏱ request timed out' (timeout — call it what it is), hollow
 * ○ "no data" (empty), ◐ partial (budget stop), ✓ only for real successes.
 * Exported for unit tests. */
export function hudMark(h: Pick<HudEntry, 'status' | 'outcome' | 'recovered'>): string {
  if (h.status === 'running') return '';
  if (h.recovered) return ' ✗ → retried ✓';
  switch (h.outcome) {
    case 'failed':
      return ' ✗';
    case 'timeout':
      return ' ⏱✗ request timed out';
    case 'empty':
      return ' ○ no data';
    case 'partial':
      return ' ◐ partial';
    case 'filtered':
      return ' ◇ filtered';
    default:
      return ' ✓';
  }
}

export default function ToolHud({ entries, activeQuestionFilter, onFilterQuestion }: ToolHudProps) {
  const [collapsed, setCollapsed] = useState(false);
  const selection = useStore(correlationStore);
  // Ticker for running rows — re-render every 100ms while any row is running.
  const [, setTick] = useState(0);
  const anyRunning = entries.some((h) => h.kind === 'tool' && h.status === 'running');
  useEffect(() => {
    if (collapsed || !anyRunning) return;
    const t = setInterval(() => setTick((n) => n + 1), 100);
    return () => clearInterval(t);
  }, [anyRunning, collapsed]);

  const toggleLabel = collapsed ? 'Expand tool activity' : 'Collapse tool activity';
  // Count badge for the collapsed state: current tool rows plus the calls
  // folded into prior-question summary chips.
  const toolCount =
    entries.filter((h) => h.kind === 'tool').length +
    entries.filter((h) => h.kind === 'summary').reduce((acc, h) => acc + (h.count || 0), 0);

  const rowOpacity = (h: HudEntry): CSSProperties =>
    activeQuestionFilter === null ? {} : { opacity: h.question === activeQuestionFilter ? 1 : 0.2 };

  const chipKeyDown = (e: KeyboardEvent, fn: () => void) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fn();
    }
  };

  return (
    <div id="tool-hud" className={`tool-hud${collapsed ? ' collapsed' : ''}`}>
      <button
        type="button"
        className="tool-hud-toggle"
        aria-expanded={!collapsed}
        aria-controls="tool-hud"
        aria-label={toggleLabel}
        title={toggleLabel}
        onClick={() => setCollapsed((c) => !c)}
      >
        ▾
      </button>
      {collapsed
        ? toolCount > 0 && (
            <div className="tool-hud-count" title={`${toolCount} tool calls — expand for details`}>
              {toolCount}
            </div>
          )
        : entries.map((h) => {
            if (h.kind === 'batch') {
              return (
                <div key={h.id} data-question={h.question} style={{ ...BATCH_STYLE, ...rowOpacity(h) }}>
                  ∥ parallel batch
                </div>
              );
            }
            if (h.kind === 'summary') {
              return (
                <div
                  key={h.id}
                  data-question={h.question}
                  style={{ ...SUMMARY_STYLE, ...rowOpacity(h) }}
                  onClick={() => onFilterQuestion(h.question)}
                >
                  Q{h.question} · {h.count} tool{(h.count || 0) > 1 ? 's' : ''} ✓
                </div>
              );
            }
            const failed = h.status === 'ok' && h.outcome === 'failed';
            const timedOut = h.status === 'ok' && h.outcome === 'timeout';
            const empty = h.status === 'ok' && h.outcome === 'empty';
            const partial = h.status === 'ok' && h.outcome === 'partial';
            const filtered = h.status === 'ok' && h.outcome === 'filtered';
            const base =
              h.recovered
                ? OK_STYLE
                : failed || timedOut
                ? FAILED_STYLE
                : empty || partial || filtered
                  ? EMPTY_STYLE
                  : h.status === 'ok'
                    ? OK_STYLE
                    : h.status === 'stopped'
                      ? STOPPED_STYLE
                      : RUNNING_STYLE;
            const style: CSSProperties = {
              ...base,
              ...(h.parallel ? PARALLEL_STYLE : {}),
              ...rowOpacity(h),
            };
            const elapsed =
              h.status === 'running' ? Date.now() - (h.t0 || Date.now()) : h.elapsedMs || 0;
            const correlated = selection?.callKey === h.id;
            const select = () => selectCall('hud', h.id);
            return (
              <div
                key={h.id}
                id={h.id}
                className={`hud-chip${correlated ? ' correlated' : ''}`}
                data-tool={h.tool}
                data-question={h.question}
                data-outcome={h.status === 'ok' ? (h.recovered ? 'recovered' : h.outcome || 'success') : undefined}
                title={
                  withReason(
                    h.recovered
                      ? `Original call failed; a retry succeeded.${h.errorLine ? ` ${h.errorLine}` : ''}`
                      : failed
                      ? h.errorLine || 'Call failed'
                      : timedOut || partial
                        ? h.errorLine || undefined
                        : undefined,
                    h.outcomeReason,
                  )
                }
                style={style}
                role="button"
                tabIndex={0}
                onClick={select}
                onKeyDown={(e) => chipKeyDown(e, select)}
              >
                <span style={{ fontWeight: 600 }}>#{h.num}</span> {h.label} —{' '}
                <span className="hud-time">
                  {fmtSecs(elapsed)}
                  {hudMark(h)}
                </span>
              </div>
            );
          })}
    </div>
  );
}
