// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* DashboardView — composition root of the dashboard React components
 * (jsx-migration W2.1; originally a 1:1 port of the retired legacy UI's
 * #dashboard-view markup plus its boot/teardown lifecycle).
 * All element ids and classes are CONTRACTUAL
 * (tests/e2e/test_dashboard_layout.py + the verbatim CSS): #ops-summary-card
 * #ai-briefing-card .kpi-row #metrics-row #metrics-oee #line-state-card
 * #ls-running/#ls-idle/#ls-down #ls-sites #ls-systems-badge #live-feed-panel
 * #live-feed #entity-sections #detail-modal-overlay #detail-modal-title
 * #detail-modal-sub #detail-modal-body .detail-modal-close
 * #quality-by-line-panel #active-issues-panel #sensor-heatmap-panel
 * #pinned-cards-container #systems-chips #insights-content
 * #ai-briefing-content #briefing-timestamp #briefing-lifecycle #demo-overlay (+children)
 * #oee-diag #line-state-diag .diag-badge.
 *
 * The dynamic content is driven by the dashboard store (store.ts): the SSE /
 * poll handlers dispatch, the components subscribe. Stable #kpi-card-<id>
 * nodes come from React keys (create-or-update semantics preserved in the
 * store reducers).
 *
 * MUST stay eagerly mounted in AppShell (AUDIT.md "Local e2e triage"): the
 * skeleton is attached from first paint and the boot effects
 * (initDemoGate/initDiagMode) run unconditionally, like the original static
 * markup + dashboard.js rAF boot. The RECURRING work — the SSE/systems/pinned
 * connection and the auto-briefing loop — follows the `active` prop instead,
 * so an eagerly-mounted-but-unviewed dashboard costs nothing. */
import { useEffect } from 'react';

import {
  setBriefingLifecycle,
  startBriefingLoop,
  stopBriefingLoop,
} from './briefing';
import { KpiRow } from './Cards';
import {
  DataStatusStrip,
  DimensionPanelsGrid,
  MetricsRow,
  OpsSummaryGrid,
  SystemsBar,
} from './DashboardSections';
import { initDemoGate, registerDemoGlobals } from './demo';
import { DemoModal } from './DemoModal';
import { DetailModal } from './DetailModal';
import { initDiagMode } from './diag'; // import side effect: window.toggleDiag
import { pollLiveFeed } from './feed';
import { getToolLabel } from './lib';
import { LiveFeed } from './LiveFeed';
import { ActiveIssuesPanel, SensorHeatmapPanel } from './Panels';
import { loadPinnedCards } from './pinned';
import { PinnedInsights } from './PinnedInsights';
import { connectDashboardSSE, disconnectDashboardSSE } from './stream';
import { observeSystems } from './systems';
import { EmptyHero } from './SystemsStrip';

declare global {
  interface Window {
    /** Cross-file global from dashboard.js — chat.js's tool pills read it. */
    getToolLabel?: (toolName: string) => string;
  }
}

const DISCLOSURE_SELECTOR = '.briefing-title-tooltip, .isa-tooltip';

/** Add touch and keyboard disclosure to the two legacy tooltip hosts without
 * changing their content owners. New ISA hosts are enhanced as they appear. */
function installTooltipDisclosure(): () => void {
  const enhance = (rootNode: ParentNode) => {
    rootNode.querySelectorAll<HTMLElement>(DISCLOSURE_SELECTOR).forEach((host) => {
      if (!host.hasAttribute('tabindex')) host.tabIndex = 0;
      if (!host.hasAttribute('role')) host.setAttribute('role', 'button');
      if (!host.hasAttribute('aria-expanded')) host.setAttribute('aria-expanded', 'false');
      if (host.classList.contains('briefing-title-tooltip') && !host.hasAttribute('aria-label')) {
        const detail = host.dataset.tooltip;
        host.setAttribute(
          'aria-label',
          detail ? `${host.textContent}: ${detail}` : host.textContent || 'Tooltip',
        );
      }
    });
  };
  const close = (except?: HTMLElement) => {
    document
      .querySelectorAll<HTMLElement>(
        '.briefing-title-tooltip.tooltip-open, .isa-tooltip.tooltip-open',
      )
      .forEach((host) => {
        if (host === except) return;
        host.classList.remove('tooltip-open');
        host.setAttribute('aria-expanded', 'false');
      });
  };
  const toggle = (host: HTMLElement) => {
    const opening = !host.classList.contains('tooltip-open');
    close(host);
    host.classList.toggle('tooltip-open', opening);
    host.setAttribute('aria-expanded', String(opening));
  };
  const onClick = (event: MouseEvent) => {
    const host = (event.target as Element | null)?.closest<HTMLElement>(DISCLOSURE_SELECTOR);
    if (host) toggle(host);
    else close();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    const host = (event.target as Element | null)?.closest<HTMLElement>(DISCLOSURE_SELECTOR);
    if (host && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      toggle(host);
    } else if (event.key === 'Escape') {
      close();
      host?.focus();
    }
  };

  enhance(document);
  const observer = new MutationObserver((records) => {
    records.forEach((record) =>
      record.addedNodes.forEach((node) => {
        if (node instanceof Element) enhance(node.matches(DISCLOSURE_SELECTOR) ? node.parentNode || document : node);
      }),
    );
  });
  observer.observe(document.body, { childList: true, subtree: true });
  document.addEventListener('click', onClick);
  document.addEventListener('keydown', onKeyDown);
  return () => {
    observer.disconnect();
    document.removeEventListener('click', onClick);
    document.removeEventListener('keydown', onKeyDown);
  };
}

// Window-global registrations (module import side effects, like the old
// <script> load): the e2e suite needs toggleDiag (registered in ./diag);
// chat consumes getToolLabel; the shell's #demo-btn uses handleDemoBtn.
if (typeof window !== 'undefined') {
  window.getToolLabel = getToolLabel;
  registerDemoGlobals();
}

export interface DashboardViewProps {
  /** Mirrors switchView: connect SSE/pollers while the dashboard view is
   * active, disconnect when another view takes over. */
  active?: boolean;
}

/** Once-per-mount boot, mirroring dashboard.js's requestAnimationFrame boot:
 * tooltip disclosure, the demo gate, and diagnostic mode. Runs unconditionally
 * (the dashboard is eagerly mounted) because it only attaches to the skeleton
 * — it starts no recurring work. */
function useDashboardBoot(): void {
  useEffect(() => installTooltipDisclosure(), []);
  useEffect(() => {
    initDemoGate();
    initDiagMode();
  }, []);
}

/** switchView('dashboard') semantics: (re)connect the SSE + systems poll +
 * pinned cards on activation; disconnect on deactivation/unmount. */
function useDashboardConnection(active: boolean): void {
  useEffect(() => {
    if (!active) {
      disconnectDashboardSSE();
      return;
    }
    const stopSystemsObserver = observeSystems();
    connectDashboardSSE();
    void loadPinnedCards();
    // Seed the live feed + stats from /api/iot/recent + /api/iot/stats
    // (live updates then arrive over the SSE 'mqtt'/'stats' events).
    void pollLiveFeed();
    return () => {
      stopSystemsObserver();
      disconnectDashboardSSE();
    };
  }, [active]);
}

/** Persistent AI briefing loop (first run after 4s, then every 5 minutes) —
 * started ONLY while the dashboard is the active view and torn down on
 * deactivation/unmount (no timer leaks, no in-flight stream left open). The
 * dashboard stays eagerly mounted in the shell, so an unconditional start meant
 * every session paid for a recurring, expensive orchestrator turn even when
 * nobody ever looked at the dashboard — a measured contributor to the
 * 2026-08-24 single-task CPU saturation. Markup is unaffected. */
function useBriefingLoop(active: boolean): void {
  useEffect(() => {
    setBriefingLifecycle(active);
    if (!active) return;
    startBriefingLoop();
    return () => {
      setBriefingLifecycle(false);
      stopBriefingLoop();
    };
  }, [active]);
}

export default function DashboardView({ active = true }: DashboardViewProps) {
  useDashboardBoot();
  useDashboardConnection(active);
  useBriefingLoop(active);

  // Section order is CONTRACTUAL (see the header comment). Each section below
  // is markup-only (DashboardSections.tsx) — no state, no effects.
  return (
    <>
      {/* Empty-state hero (old dash-empty-hero was PREPENDED into
          #dashboard-view → first in the fragment). */}
      <EmptyHero />
      <SystemsBar />
      <OpsSummaryGrid />

      {/* KPI Cards — resolver-driven (S5.3). One #kpi-card-<id> node per
          declarative card, keyed on card_id (create-or-update in place). */}
      <KpiRow />
      <DimensionPanelsGrid />

      {/* Lazy panels — each hides itself when uncovered/empty (S5.3 W2 §6.2,
          W3 §6.3). */}
      <ActiveIssuesPanel />
      <SensorHeatmapPanel />

      {/* Pinned Prompt Cards */}
      <PinnedInsights />
      <MetricsRow />

      {/* Live Factory Events (S5.4) — MQTT ticker footer. Self-hides
          (display:none) until event data has actually flowed (LiveFeed.tsx). */}
      <LiveFeed />
      <DataStatusStrip />

      {/* Entity table data cache (S5.4) — hidden modal cache anchor, never an
          inline table dump (regression guard in test_dashboard_layout.py). */}
      <div id="entity-sections" style={{ display: 'none' }} />

      {/* Click-to-drill detail modal — entity table renders here on card click. */}
      <DetailModal />

      {/* Demo Mode Result Overlay (position:fixed — placement independent). */}
      <DemoModal />
    </>
  );
}
