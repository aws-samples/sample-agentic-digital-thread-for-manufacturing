// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* DashboardSections — MARKUP-ONLY sections of the dashboard.
 *
 * Extracted from DashboardView.tsx so that file can be a short lifecycle /
 * composition root. These components carry NO state and NO effects: element
 * order, ids, classes and props are byte-for-byte the ones DashboardView
 * rendered before the split, because they are CONTRACTUAL
 * (tests/e2e/test_dashboard_layout.py + the verbatim CSS). A React component
 * boundary is invisible to the DOM, so the rendered tree is unchanged. */
import { AiBriefing } from './AiBriefing';
import { GaugeCards, UncoveredStrip } from './Cards';
import { useI18n } from '../../lib/i18n';
import { LineStateCard } from './LineStateCard';
import { OpsSummaryContent } from './OpsSummary';
import { OutputByLinePanel, QualityByLinePanel, WorkOrdersByStatusPanel } from './Panels';
import { StewardDigest } from './StewardDigest';
import { SystemsStrip } from './SystemsStrip';

/** Connected Systems Bar. */
export function SystemsBar() {
  return (
    <div className="dashboard-strip-scroll" role="region" aria-label="Connected systems">
      <SystemsStrip />
    </div>
  );
}

/** Operations Summary (1fr) + AI Operations Briefing (2fr). Both dynamic: the
 * summary is computed client-side from the resolver cards snapshot; the
 * briefing streams from /api/ask. */
export function OpsSummaryGrid() {
  const { t } = useI18n();
  return (
    <div className="dashboard-summary-grid">
      {/* Left: dynamic operations summary derived from the cards */}
      <div
        className="ai-insights"
        id="ops-summary-card"
        style={{ display: 'flex', flexDirection: 'column' }}
      >
        <div className="ai-insights-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="3" />
            <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
          </svg>
          {t('dash.opsSummary')}
          <span className="diag-badge">diag</span>
        </div>
        <div id="insights-content" style={{ flex: 1 }}>
          <OpsSummaryContent />
          {/* Steward digest (ADR-0052 B5) — compact "Recent learning" section
              at the bottom of the summary card (steward feedback 2026-08-06:
              the old full-width card was too technical and too large). Renders
              null (no layout impact) for non-admins or on any fetch failure. */}
          <StewardDigest />
        </div>
      </div>

      {/* Right: persistent AI briefing (2x width) */}
      <AiBriefing />
    </div>
  );
}

/** Dimension panels share the two-column panel grid. Output therefore stays
 * panel-sized instead of stretching across the dashboard. */
export function DimensionPanelsGrid() {
  return (
    <div className="panels-grid dashboard-dimension-grid">
      {/* Quality-by-line bars (S5.3 W2) — lazy POST resolve-by-dimension. */}
      <QualityByLinePanel />

      {/* Concept-driven count bars — work orders by state and output
          by production line; hidden unless a resolve returned rows. */}
      <WorkOrdersByStatusPanel />
      <OutputByLinePanel />
    </div>
  );
}

/** OEE gauge + Line State (S5.4) — dedicated metrics row. */
export function MetricsRow() {
  return (
    <div className="metrics-row" id="metrics-row">
      <div id="metrics-oee" className="diag-reveal">
        <div className="diag-note" id="oee-diag" />
        {/* gauge card (#kpi-card-oee) renders here (old renderCard append) */}
        <GaugeCards />
      </div>
      <LineStateCard />
    </div>
  );
}

/** Uncovered-cards "Awaiting data" strip — all uncovered /
 * needs-data cards collapse into ONE compact chip strip; absent when empty.
 * DELIBERATE DEVIATION from the legacy placement (the old
 * insertBefore(row.nextSibling) put it right after the KPI row): on data-poor
 * environments the gap chips would dominate the top of the page, so the strip
 * renders at the BOTTOM of the dashboard (empty-env UX). */
export function DataStatusStrip() {
  return (
    <div className="dashboard-strip-scroll" role="region" aria-label="Dashboard data status">
      <UncoveredStrip />
    </div>
  );
}
