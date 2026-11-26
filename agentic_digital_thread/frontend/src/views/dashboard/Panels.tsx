// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Lazy dashboard panels (S5.3 W2/W3, S4.2) — React half of the old panels.ts
 * (jsx-migration W2.1): quality-by-line dimension bars, active issues, and the
 * sensor heatmap, rendered from store slices. All presence/data-driven and
 * fail-soft, exactly like the old renderers: errors → muted friendly note or
 * hidden panel; empty → the panel div stays attached but display:none (the
 * e2e contract: #quality-by-line-panel attached, hidden-when-empty). The
 * '.dim-bar-subtitle' string is byte-identical:
 * "Pass rate = % of units passing quality checks." */
import { useEffect, useState, type ReactNode } from 'react';

import { requestJson } from '../../api/client';
import type {
  ActiveIssue,
  ActiveIssuesPayload,
  CoveragePayload,
  DimensionPayload,
  HeatmapPayload,
} from '../../api/types/dashboard';
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import {
  EmptyState,
  ErrorState,
  LoadingState,
  NoDataState,
} from '../../shared/ui/AsyncStates';
import { makeDimensionLoader } from './dimension-loader';
import {
  fmtCardValue,
  friendlyMetricError,
  gaugeColor,
  hasNonDegenerateRows,
  heatmapCellText,
  heatmapSeverityClass,
  issueSeverityColor,
  panelErrorSummary,
  prettyConcept,
  toPercent,
} from './lib';
import {
  dashboardStore,
  registerPanelLoader,
  setActiveIssues,
  setHeatmap,
  setOutputByLine,
  setPanelUncovered,
  setQuality,
  setWorkOrders,
  systemLabel,
} from './store';
import { makeSwrLoader } from './swr';

// ── Quality-by-line bar panel (metric-by-dimension) ───────────────────────────

/** Operator-safe error headline with exact resolver detail retained on demand.
 * `structural` = a data-model constraint no retry can fix (server marks the
 * payload): the Retry affordance is dropped and the headline says so —
 * offering Retry for an unfixable constraint misleads the operator. */
function PanelErrorDisclosure({
  detail,
  fallback,
  onRetry,
  structural = false,
}: {
  detail: unknown;
  fallback: string;
  onRetry: () => void;
  structural?: boolean;
}) {
  const raw = String(detail || '');
  return (
    <div className="panel-error-disclosure">
      <ErrorState
        message={
          structural
            ? 'Not supported for this system\u2019s data model.'
            : panelErrorSummary(raw, fallback)
        }
        onRetry={structural ? undefined : onRetry}
        compact
      />
      {raw ? (
        <details className="panel-error-details">
          <summary>Details</summary>
          <div className="panel-error-detail-text">{raw}</div>
        </details>
      ) : null}
    </div>
  );
}

/** Source label for a dimension panel's context subtitle. A single-system
 * payload names its `system_id`. A multi-plant payload (ADR-0094: `system_id`
 * null, site-prefixed rows) names every plant whose own resolve answered
 * (`status: 'ok'`), in payload order; an uncovered or failed plant
 * contributed no bars, so it is not named as a source. '' = no subtitle. */
function dimensionSourceLabel(payload: DimensionPayload): string {
  if (payload.system_id) return systemLabel(payload.system_id);
  const sites = Array.isArray(payload.sites) ? payload.sites : [];
  return sites
    .filter((s) => s && s.status === 'ok')
    .map((s) => (s.system_id ? systemLabel(s.system_id) : s.system_name || s.site || ''))
    .filter(Boolean)
    .join(', ');
}

function DimensionBarContent({ payload }: { payload: DimensionPayload }) {
  const cols = Array.isArray(payload.columns) ? payload.columns : [];
  const dimLabel = (cols[0] && cols[0].label) || prettyConcept(payload.group_by) || 'Line';
  const valLabel = (cols[1] && cols[1].label) || 'Pass Rate';
  const source = dimensionSourceLabel(payload);

  // Fail-soft: a resolve error → muted friendly note, panel shown.
  if (payload.status === 'error') {
    return (
      <>
        <div className="dim-bar-title">{valLabel + ' by ' + dimLabel}</div>
        <div className="dim-bar-empty">
          {'Unable to load ' +
            valLabel +
            ' by ' +
            dimLabel +
            (payload.error ? ' \u2014 ' + friendlyMetricError(payload.error) : '') +
            '.'}
        </div>
      </>
    );
  }

  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  return (
    <>
      <div className="dim-bar-title">{valLabel + ' by ' + dimLabel}</div>
      {/* Issue 3 — context subtitle: which source system·site + what the % means.
          A multi-plant payload (system_id null, ADR-0094) names every plant
          that answered instead. */}
      {source ? (
        <div className="dim-bar-subtitle">
          {'Source: ' +
            source +
            ' \u00b7 Pass rate = % of units passing quality checks.'}
        </div>
      ) : null}
      {rows.map((row, i) => {
        if (!Array.isArray(row)) return null;
        const lineVal = row[0];
        const rawRate = row[1];
        const rate = typeof rawRate === 'number' ? rawRate : Number(rawRate);
        const pct = toPercent(rate);
        const color = gaugeColor(pct);
        const clamped = pct === null ? 0 : Math.max(0, Math.min(100, pct));
        return (
          <div key={i} className="dim-bar-row">
            <div className="dim-bar-label">
              {lineVal === null || lineVal === undefined ? '' : String(lineVal)}
            </div>
            <div className="dim-bar-track">
              <div
                className="dim-bar-fill"
                style={{ width: clamped + '%', background: color }}
              />
            </div>
            {pct !== null ? (
              <div className="dim-bar-value">{fmtCardValue(pct) + '%'}</div>
            ) : (
              <div className="dim-bar-value muted">{'\u2014'}</div>
            )}
          </div>
        );
      })}
    </>
  );
}


/** Route an UNCOVERED panel into the awaiting-data strip instead of a full
 * dashed placeholder card (steward feedback 2026-08-06: placeholder cards
 * crowd out the data the user came for; the strip already carries uncovered
 * KPI cards). Returns true when the caller should render nothing. Registration
 * is presence-driven and cleans up on unmount and on recovery. */
function useUncoveredPanel(
  panelId: string,
  title: string,
  payload: { status?: string; missing_concepts?: string[] } | null | undefined,
): boolean {
  const uncovered = !!payload && payload.status === 'uncovered';
  const missingKey = uncovered ? (payload?.missing_concepts || []).join(',') : '';
  useEffect(() => {
    setPanelUncovered(
      panelId, title, missingKey ? missingKey.split(',') : [], uncovered);
    return () => setPanelUncovered(panelId, title, undefined, false);
  }, [panelId, title, uncovered, missingKey]);
  return uncovered;
}

export function QualityByLinePanel() {
  const payload = useStore(dashboardStore, (state) => state.quality);
  const rows = payload && Array.isArray(payload.rows) ? payload.rows : [];
  const uncovered = useUncoveredPanel('quality-by-line', 'Quality by line', payload);
  if (uncovered) return null;
  let body: ReactNode;
  if (payload === undefined) {
    body = <LoadingState label="Loading quality by line" compact />;
  } else if (payload === null) {
    body = (
      <ErrorState
        message="Quality by line unavailable."
        onRetry={() => void loadQualityByLine()}
        compact
      />
    );
  } else if (payload.status === 'error') {
    body = (
      <PanelErrorDisclosure
        detail={payload.error}
        fallback="Quality by line unavailable."
        onRetry={() => void loadQualityByLine()}
        structural={payload.structural}
      />
    );
  } else if (payload.status === 'no-data' || (rows.length > 0 && !hasNonDegenerateRows(rows))) {
    body = <NoDataState message="Quality is mapped, but no samples are available yet." compact />;
  } else if (rows.length === 0) {
    body = <EmptyState message="No quality-by-line rows were returned." compact />;
  } else {
    body = <DimensionBarContent payload={payload} />;
  }
  return (
    <div className="quality-by-line-panel" id="quality-by-line-panel">
      {body}
    </div>
  );
}

// ── Concept-driven dimension-bar panels (declarative resolve-by-dimension) ────
// Port of dashboard.js renderCountBar: the generic COUNT/quantity
// flavor of the quality panel — bar width proportional to the MAX row value
// (not a 0-100 percentage), value rendered with the payload's unit.
// Payload-driven titles/labels (columns[].label / group_by) — zero dataset/
// system literals. Presence-driven and honest: an error or empty resolve
// renders NOTHING (panel hidden) so an unmapped concept never produces a dead
// panel. All text via JSX (XSS-safe, mirrors DimensionBarContent).

function CountBarContent({
  payload,
  caption,
  showMaxScale = false,
}: {
  payload: DimensionPayload;
  caption?: string;
  showMaxScale?: boolean;
}) {
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const cols = Array.isArray(payload.columns) ? payload.columns : [];
  const dimLabel = (cols[0] && cols[0].label) || prettyConcept(payload.group_by) || 'Group';
  const valLabel = (cols[1] && cols[1].label) || payload.label || 'Value';
  const unit = payload.unit ? String(payload.unit) : '';

  const numeric = rows.map((r) =>
    Array.isArray(r) && typeof r[1] === 'number' && isFinite(r[1])
      ? r[1]
      : Number(Array.isArray(r) ? r[1] : NaN),
  );
  const max = Math.max(0, ...numeric.filter((n) => typeof n === 'number' && isFinite(n)));
  const source = dimensionSourceLabel(payload);

  return (
    <>
      <div className="dim-bar-title">{valLabel + ' by ' + dimLabel}</div>
      {/* Task 2D — window caption under the panel title (presence-driven: the
          owning panel derives it from resolve lineage; no caption prop → no
          caption node, other count-bar panels byte-identical). */}
      {caption ? (
        <div className="dim-bar-subtitle dim-bar-window-caption">{caption}</div>
      ) : null}
      {/* Context subtitle: which source system·site supplied the data (generic,
          via the shared systemLabel helper). Presence-driven; a multi-plant
          payload (system_id null, ADR-0094) names every plant that answered. */}
      {source ? (
        <div className="dim-bar-subtitle">{'Source: ' + source}</div>
      ) : null}
      {showMaxScale ? (
        <div
          className="dim-bar-axis"
          aria-label={'Normalized scale from zero to ' + fmtCardValue(max) + (unit ? ' ' + unit : '')}
        >
          <span>0</span>
          {/* Plain axis end-label: bars are normalized to the largest row
              value — 'Max' read like a statistic rather than a scale. */}
          <span>{fmtCardValue(max) + (unit ? ' ' + unit : '')}</span>
        </div>
      ) : null}
      {rows.map((row, i) => {
        if (!Array.isArray(row)) return null;
        const groupVal = row[0];
        const n = numeric[i];
        const hasVal = typeof n === 'number' && isFinite(n);
        const pct = hasVal && max > 0 ? Math.max(0, Math.min(100, (n / max) * 100)) : 0;
        return (
          <div key={i} className="dim-bar-row">
            <div className="dim-bar-label">
              {groupVal === null || groupVal === undefined ? '' : String(groupVal)}
            </div>
            <div className="dim-bar-track">
              <div
                className="dim-bar-fill"
                style={{ width: pct + '%', background: 'var(--accent, var(--green))' }}
              />
            </div>
            {hasVal ? (
              <div className="dim-bar-value">{fmtCardValue(n) + (unit ? ' ' + unit : '')}</div>
            ) : (
              <div className="dim-bar-value muted">{'\u2014'}</div>
            )}
          </div>
        );
      })}
    </>
  );
}

/** Shared count-bar panel shell — honest empty state: only an 'ok' payload
 * with NON-DEGENERATE rows renders (legacy renderCountBar: anything else →
 * hidden panel, NEVER an error note). All-null rows hide too (Task 2B shared
 * degenerate-hide rule; all-zero rows are honest and stay visible). Reuses
 * the .quality-by-line-panel styling, exactly like the legacy _ensureDimPanel
 * dynamic panels. */
function CountBarPanel({
  id,
  payload,
  caption,
  onRetry,
  preserveHiddenShell = false,
  showMaxScale = false,
}: {
  id: string;
  payload: DimensionPayload | null | undefined;
  /** Optional context caption rendered under the panel title (Task 2D). */
  caption?: string;
  onRetry: () => void;
  /** Keep the historical hidden contractual panel node while its async state remains visible. */
  preserveHiddenShell?: boolean;
  /** Label the normalized bar scale with its maximum value. */
  showMaxScale?: boolean;
}) {
  const rows = payload && Array.isArray(payload.rows) ? payload.rows : [];
  // "work-orders-by-status-panel" → "Work orders by status" (generic,
  // id-derived: an uncovered payload carries no display labels to use).
  const title = id.replace(/-panel$/, '').replace(/-/g, ' ')
    .replace(/^./, (c) => c.toUpperCase());
  const uncovered = useUncoveredPanel(id, title, payload);
  if (uncovered) return null;
  let body: ReactNode;
  let hasRenderableRows = false;
  if (payload === undefined) {
    body = <LoadingState label="Loading panel" compact />;
  } else if (payload === null) {
    body = <ErrorState message="Panel unavailable." onRetry={onRetry} compact />;
  } else if (payload.status === 'error') {
    body = (
      <PanelErrorDisclosure
        detail={payload.error}
        fallback="This chart is unavailable."
        onRetry={onRetry}
        structural={payload.structural}
      />
    );
  } else if (payload.status === 'no-data' || (rows.length > 0 && !hasNonDegenerateRows(rows))) {
    body = <NoDataState message="Metric is mapped, but no samples are available yet." compact />;
  } else if (rows.length === 0) {
    body = <EmptyState message="No rows were returned." compact />;
  } else {
    hasRenderableRows = true;
    body = <CountBarContent payload={payload} caption={caption} showMaxScale={showMaxScale} />;
  }
  if (preserveHiddenShell && !hasRenderableRows) {
    return (
      <>
        <div className="quality-by-line-panel" id={id} style={{ display: 'none' }} />
        {body}
      </>
    );
  }
  return (
    <div className="quality-by-line-panel" id={id}>
      {body}
    </div>
  );
}

/** Work Orders by Status — COUNT over the `work-order` concept grouped by the
 * `lifecycle-state` dimension (lights up for ANY system mapping those
 * concepts). */
export function WorkOrdersByStatusPanel() {
  const payload = useStore(dashboardStore, (s) => s.workOrders);
  return (
    <CountBarPanel
      id="work-orders-by-status-panel"
      payload={payload}
      onRetry={() => void loadWorkOrdersByStatus()}
    />
  );
}

/** Output-by-line payload — the stored DimensionPayload augmented client-side
 * with the resolve window derived from per-row lineage (Task 2D; additive —
 * structurally assignable to DimensionPayload, no shared-type change). */
type OutputPayload = DimensionPayload & { window?: string | null };

/** Derive the resolve window token from the payload's per-row lineage list
 * (each row's `query_plan.window` — identical across rows; `null` = the honest
 * lifetime fallback: no event-time mapping on the measure table). */
export function windowFromLineage(lineage: unknown): string | null {
  if (!Array.isArray(lineage) || !lineage.length) return null;
  const first = lineage[0] as { query_plan?: { window?: unknown } } | null | undefined;
  const w = first && first.query_plan ? first.query_plan.window : null;
  return typeof w === 'string' && w ? w : null;
}

/** Human caption for a window token: '24h' → 'Last 24 hours', '7d' → 'Last 7
 * days'; null/absent → 'Lifetime total' (the honest no-event-time fallback).
 * Unrecognized tokens render as-is ('Last <token>') — presence-driven, no
 * dataset literals. */
export function windowCaption(window: string | null | undefined): string {
  if (!window) return 'Lifetime total';
  const m = /^(\d+)([hd])$/.exec(window);
  if (!m) return 'Last ' + window;
  const n = Number(m[1]);
  const unit = m[2] === 'h' ? 'hour' : 'day';
  return 'Last ' + n + ' ' + unit + (n === 1 ? '' : 's');
}

/** Output by Line — SUM of the `actual-quantity` concept grouped by the
 * `production-line` dimension, resolved through the WINDOWED metric variant
 * (Task 2D): a 24h event-time window when the source maps `event-time`,
 * otherwise the honest lifetime fallback. The caption under the title states
 * which one the reader is looking at. */
export function OutputByLinePanel() {
  const payload = useStore(dashboardStore, (s) => s.outputByLine);
  const output = payload as OutputPayload | null | undefined;
  const windowLabel = output?.window ? 'last ' + output.window : 'lifetime total';
  const caption = payload ? 'Units produced \u00b7 ' + windowLabel : undefined;
  return (
    <CountBarPanel
      id="output-by-line-panel"
      payload={payload}
      caption={caption}
      onRetry={() => void loadOutputByLine()}
      preserveHiddenShell
      showMaxScale
    />
  );
}

// ── Active-issues cross-entity list ───────────────────────────────────────────

/** One issue row (XSS-safe: all text via JSX). Severity is a closed server
 * token mapped CLIENT-SIDE to a known class + accent color. */
function IssueRow({ issue }: { issue: ActiveIssue }) {
  const sev = issue.severity === 'critical' ? 'critical' : 'warning';
  return (
    <div
      className={'active-issue-row severity-' + sev}
      style={{ borderLeftColor: issueSeverityColor(sev) }}
    >
      <div className="active-issue-main">
        <div className="active-issue-title">
          {issue.title === null || issue.title === undefined ? '' : String(issue.title)}
        </div>
        <div className="active-issue-detail">
          {issue.detail === null || issue.detail === undefined ? '' : String(issue.detail)}
        </div>
      </div>
      <span className="active-issue-badge">
        {issue.source_system === null || issue.source_system === undefined
          ? ''
          : String(issue.source_system)}
      </span>
    </div>
  );
}

const ISSUES_INITIAL = 6; // most-recent N, reveal the rest via "show more".

function ActiveIssuesContent({ payload }: { payload: ActiveIssuesPayload }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);

  // Fail-soft: a composer error → muted friendly note, panel shown.
  if (payload.status === 'error') {
    return (
      <>
        <div className="active-issues-title">{t('dash.activeIssues')}</div>
        <div className="active-issues-empty">
          {payload.error || 'Unable to load active issues.'}
        </div>
      </>
    );
  }

  const issues = Array.isArray(payload.issues) ? payload.issues : [];
  const valid = issues.filter((issue) => issue);
  const shown = expanded ? valid : valid.slice(0, ISSUES_INITIAL);
  const remaining = valid.length - ISSUES_INITIAL;
  return (
    <>
      <div className="active-issues-title">{t('dash.activeIssues')}</div>
      <div className="active-issues-list">
        {shown.map((issue, i) => (
          <IssueRow key={i} issue={issue} />
        ))}
      </div>
      {!expanded && remaining > 0 && (
        <button
          type="button"
          className="active-issues-showmore"
          onClick={() => setExpanded(true)} // client-side only, no refetch.
        >
          {'Show ' + remaining + ' more'}
        </button>
      )}
    </>
  );
}

export function ActiveIssuesPanel() {
  const payload = useStore(dashboardStore, (state) => state.activeIssues);
  const issues = payload && Array.isArray(payload.issues) ? payload.issues : [];
  let body: ReactNode;
  if (payload === undefined) {
    body = <LoadingState label="Loading active issues" compact />;
  } else if (payload === null) {
    body = (
      <ErrorState
        message="Active issues unavailable."
        onRetry={() => void loadActiveIssues()}
        compact
      />
    );
  } else if (payload.status === 'error') {
    body = (
      <PanelErrorDisclosure
        detail={payload.error}
        fallback="Active issues unavailable."
        onRetry={() => void loadActiveIssues()}
      />
    );
  } else if (issues.length === 0) {
    body = <EmptyState message="No active issues." compact />;
  } else {
    body = <ActiveIssuesContent payload={payload} />;
  }
  return (
    <div className="active-issues-panel" id="active-issues-panel">
      {body}
    </div>
  );
}

// ── Sensor heatmap (asset × sensor severity grid) ─────────────────────────────

function HeatmapContent({ payload }: { payload: HeatmapPayload }) {
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  const cols = Array.isArray(payload.columns) ? payload.columns : [];
  const dims = Array.isArray(payload.dims) ? payload.dims : [];
  const xLabel = (cols[0] && cols[0].label) || prettyConcept(dims[0]) || 'Asset';
  const yLabel = (cols[1] && cols[1].label) || prettyConcept(dims[1]) || 'Sensor';
  const valLabel = (cols[2] && cols[2].label) || 'Reading';

  // Pivot [d1, d2, value, threshold, severity] into a d1 (rows) × d2 (cols)
  // grid, preserving first-seen order for both axes.
  const yKeys: string[] = [];
  const ySeen = new Set<string>();
  const xKeys: string[] = [];
  const xSeen = new Set<string>();
  const cells = new Map<string, [unknown, unknown, unknown]>();
  rows.forEach((row) => {
    if (!Array.isArray(row)) return;
    const d1 = row[0] === null || row[0] === undefined ? '' : String(row[0]);
    const d2 = row[1] === null || row[1] === undefined ? '' : String(row[1]);
    if (!xSeen.has(d1)) {
      xSeen.add(d1);
      xKeys.push(d1);
    }
    if (!ySeen.has(d2)) {
      ySeen.add(d2);
      yKeys.push(d2);
    }
    cells.set(d1 + '\u0000' + d2, [row[2], row[3], row[4]]);
  });
  if (!xKeys.length || !yKeys.length) return null;

  return (
    <>
      <div className="heatmap-title">
        {valLabel +
          ' \u2014 ' +
          xLabel +
          ' \u00d7 ' +
          yLabel +
          (payload.threshold_status === 'uncovered' ? ' (no threshold)' : '')}
      </div>
      <div className="heatmap-table-wrap">
        <table className="heatmap-table">
          <thead>
            <tr>
              <th className="heatmap-corner" />
              {yKeys.map((yk) => (
                <th key={yk}>{yk}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {xKeys.map((xk) => (
              <tr key={xk}>
                <th className="heatmap-rowhead">{xk}</th>
                {yKeys.map((yk) => {
                  const cell = cells.get(xk + '\u0000' + yk);
                  if (!cell) {
                    return (
                      <td key={yk} className="heatmap-cell severity-uncovered">
                        {'\u2014'}
                      </td>
                    );
                  }
                  const [value, threshold, sev] = cell;
                  const title =
                    threshold !== null && threshold !== undefined
                      ? valLabel +
                        ': ' +
                        heatmapCellText(value) +
                        ' / threshold ' +
                        fmtCardValue(threshold)
                      : undefined;
                  return (
                    <td
                      key={yk}
                      className={'heatmap-cell ' + heatmapSeverityClass(sev)}
                      title={title}
                    >
                      {heatmapCellText(value)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function SensorHeatmapPanel() {
  const payload = useStore(dashboardStore, (state) => state.heatmap);
  const rows = payload && Array.isArray(payload.rows)
    ? payload.rows.filter((row) => Array.isArray(row))
    : [];
  const allValuesMissing = rows.length > 0 && rows.every((row) => row[2] == null);
  const uncovered = useUncoveredPanel('sensor-heatmap', 'Sensor heatmap', payload);
  if (uncovered) return null;
  let body: ReactNode;
  if (payload === undefined) {
    body = <LoadingState label="Loading sensor heatmap" compact />;
  } else if (payload === null) {
    body = (
      <ErrorState
        message="Sensor heatmap unavailable."
        onRetry={() => void loadSensorHeatmap()}
        compact
      />
    );
  } else if (payload.status === 'error') {
    body = (
      <PanelErrorDisclosure
        detail={payload.error}
        fallback="Sensor heatmap unavailable."
        onRetry={() => void loadSensorHeatmap()}
      />
    );
  } else if (payload.status === 'no-data' || allValuesMissing) {
    body = <NoDataState message="Sensors are mapped, but no samples are available yet." compact />;
  } else if (rows.length === 0) {
    body = <EmptyState message="No sensor readings were returned." compact />;
  } else {
    body = <HeatmapContent payload={payload} />;
  }
  return (
    <div className="sensor-heatmap-panel" id="sensor-heatmap-panel">
      {body}
    </div>
  );
}

// ── Lazy loaders (the fetch half of the old panels.ts) ────────────────────────
// All fire-and-forget and fail-safe: a failed FIRST load stores null (panel
// hidden — intended fail-soft design, now console.error-logged by the SWR
// guard instead of silent), resolve errors store the error payload (muted
// friendly note). Each loader is wrapped in the house SWR guard (swr.ts):
// repeat SSE snapshot events share one in-flight fetch and skip the network
// while the stored data is <15s fresh; a failed refresh KEEPS the cached
// panel data.

const _qualityLoader = makeDimensionLoader({
  name: 'quality-by-line',
  metricId: 'quality-yield-rate',
  dimensionConcept: 'production-line',
  setPayload: setQuality,
  firstFailure: () => null,
});

/** Lazy loader for the quality-by-line panel (fire-and-forget, fail-safe,
 * SWR-guarded). */
export function loadQualityByLine(): Promise<void> {
  return _qualityLoader.load();
}

// By-dimension card routing (Issue 1): 'quality-by-line' snapshots re-resolve
// through this loader instead of ever becoming a KPI tile.
registerPanelLoader('quality-by-line', loadQualityByLine);

// ── Concept-driven count-bar loaders — same declarative
// resolve-by-dimension seam + fire-and-forget contract as quality-by-line.

const _workOrdersLoader = makeDimensionLoader({
  name: 'work-orders-by-status',
  metricId: 'work-order-count',
  dimensionConcept: 'lifecycle-state',
  setPayload: setWorkOrders,
  firstFailure: () => null,
});

/** Lazy loader for the work-orders-by-status panel (fire-and-forget,
 * fail-safe, SWR-guarded). */
export function loadWorkOrdersByStatus(): Promise<void> {
  return _workOrdersLoader.load();
}

registerPanelLoader('work-orders-by-status', loadWorkOrdersByStatus);

const _outputByLineLoader = makeDimensionLoader<OutputPayload>({
  name: 'output-by-line',
  // Task 2D: the WINDOWED output variant (24h event-time window when the
  // source maps `event-time`; the backend falls back to the lifetime shape
  // with `window: null` in lineage otherwise — never fabricated).
  metricId: 'actual-quantity-windowed',
  dimensionConcept: 'production-line',
  setPayload: setOutputByLine,
  // Stamp the resolve window (from per-row lineage) onto the stored payload
  // so the panel caption is presence-driven off the store slice.
  transform: (payload) => ({ ...payload, window: windowFromLineage(payload.lineage) }),
  firstFailure: () => null,
});

/** Lazy loader for the output-by-line panel (fire-and-forget, fail-safe,
 * SWR-guarded). */
export function loadOutputByLine(): Promise<void> {
  return _outputByLineLoader.load();
}

registerPanelLoader('output-by-line', loadOutputByLine);

const _activeIssuesLoader = makeSwrLoader({
  name: 'active-issues',
  fetch: async (signal) => {
    const payload = await requestJson<ActiveIssuesPayload>('/api/dashboard/active-issues', {
      signal,
    });
    setActiveIssues(payload);
  },
  onFirstFailure: () => setActiveIssues(null),
});

/** Lazy loader for the active-issues panel (fire-and-forget, fail-safe,
 * SWR-guarded). */
export function loadActiveIssues(): Promise<void> {
  return _activeIssuesLoader.load();
}

const _heatmapLoader = makeSwrLoader({
  name: 'sensor-heatmap',
  fetch: async (signal) => {
    const payload = await requestJson<HeatmapPayload>('/api/metrics/resolve-heatmap', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal,
      body: JSON.stringify({
        metric_id: 'sensor-reading-latest',
        dimension_concept_x: 'asset',
        dimension_concept_y: 'production-line',
        threshold_concept: 'specification',
      }),
    });
    setHeatmap(payload);
  },
  onFirstFailure: () => setHeatmap(null),
});

/** Lazy loader for the sensor-heatmap panel (fire-and-forget, fail-safe,
 * SWR-guarded). */
export function loadSensorHeatmap(): Promise<void> {
  return _heatmapLoader.load();
}

/** Test hook — reset the panel-loader SWR guards between specs. */
export function _resetPanelLoaders(): void {
  _qualityLoader.reset();
  _workOrdersLoader.reset();
  _outputByLineLoader.reset();
  _activeIssuesLoader.reset();
  _heatmapLoader.reset();
}

// ── S4.2 coverage summary — compact thread-level gap roll-up ──────────────────
// Kept 1:1 (pure DOM, null-guarded): the #coverage-summary target is not part
// of the SPA markup today, matching the old module.

export function renderCoverageSummary(coverage: CoveragePayload | null | undefined): void {
  const panel = document.getElementById('coverage-summary');
  if (!panel) return;
  // Always reset first so a prior render can't linger when the data goes empty.
  panel.textContent = '';
  panel.style.display = 'none';
  const summary = coverage && coverage.summary;
  const gaps = coverage && Array.isArray(coverage.gaps) ? coverage.gaps : [];
  // Nothing actionable → render nothing.
  if (!summary || !summary.uncovered) return;

  const head = document.createElement('div');
  head.className = 'coverage-summary-head';
  const title = document.createElement('span');
  title.className = 'coverage-summary-title';
  title.textContent = 'Coverage gaps';
  const count = document.createElement('span');
  count.className = 'coverage-summary-count';
  count.textContent = summary.uncovered + ' / ' + summary.total + ' cards uncovered';
  head.appendChild(title);
  head.appendChild(count);
  panel.appendChild(head);

  if (gaps.length) {
    const list = document.createElement('ul');
    list.className = 'coverage-gap-list';
    gaps.forEach((gap) => {
      const li = document.createElement('li');
      li.className = 'coverage-gap';
      const concept = document.createElement('span');
      concept.className = 'coverage-gap-concept';
      concept.textContent = gap.concept || '';
      const sug = document.createElement('span');
      sug.className = 'coverage-gap-suggestion';
      sug.textContent = gap.suggestion || '';
      li.appendChild(concept);
      li.appendChild(sug);
      list.appendChild(li);
    });
    panel.appendChild(list);
  }
  panel.style.display = '';
}

/** Fetch the thread-level gap report (GET /api/coverage). Fail-safe. */
export async function loadCoverageSummary(): Promise<void> {
  try {
    renderCoverageSummary(await requestJson<CoveragePayload>('/api/coverage'));
  } catch {
    /* fail-safe: render nothing */
  }
}
