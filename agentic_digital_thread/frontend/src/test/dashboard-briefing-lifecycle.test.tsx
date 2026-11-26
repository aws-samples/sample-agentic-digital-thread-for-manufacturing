// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The auto-briefing loop must follow the dashboard's ACTIVE lifecycle (P0-A).
 *
 * Incident evidence (2026-08-24 Management full-e2e): one app task at cpu=512
 * with a single Uvicorn process pinned ~99.9-100% CPU for ~45 minutes while
 * ALB averages sat at 3-6 s (max 18-31 s). The briefing is an EXPENSIVE
 * orchestrator turn on a 5-minute interval, and the dashboard is eagerly
 * mounted in the shell — so the loop used to boot (and keep ticking) even for
 * a session that never looked at the dashboard, on top of every other view's
 * traffic.
 *
 * The SSE/systems/pinned effect already follows `active`; the briefing loop
 * now does too. No visual change: the markup is untouched, only WHEN the
 * generation fires. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const briefingSpies = vi.hoisted(() => ({
  startBriefingLoop: vi.fn(),
  stopBriefingLoop: vi.fn(),
  setBriefingLifecycle: vi.fn(),
}));

/* Everything else the dashboard arms on activation. Grouped here so the
 * inactive case can assert the ABSENCE of the whole recurring cost, not just
 * the briefing turn. */
const connectionSpies = vi.hoisted(() => ({
  connectDashboardSSE: vi.fn(),
  disconnectDashboardSSE: vi.fn(),
  observeSystems: vi.fn(() => vi.fn()),
  loadPinnedCards: vi.fn(),
  pollLiveFeed: vi.fn(),
}));

vi.mock('../views/dashboard/briefing', () => briefingSpies);
vi.mock('../views/dashboard/AiBriefing', () => ({ AiBriefing: () => <div id="ai-briefing-card" /> }));
vi.mock('../views/dashboard/Cards', () => ({
  GaugeCards: () => <div />,
  KpiRow: () => <div className="kpi-row" />,
  UncoveredStrip: () => <div />,
}));
vi.mock('../views/dashboard/demo', () => ({ initDemoGate: vi.fn(), registerDemoGlobals: vi.fn() }));
vi.mock('../views/dashboard/DemoModal', () => ({ DemoModal: () => null }));
vi.mock('../views/dashboard/DetailModal', () => ({ DetailModal: () => null }));
vi.mock('../views/dashboard/diag', () => ({ initDiagMode: vi.fn() }));
vi.mock('../views/dashboard/feed', () => ({ pollLiveFeed: connectionSpies.pollLiveFeed }));
vi.mock('../views/dashboard/lib', () => ({ getToolLabel: vi.fn(() => 'tool') }));
vi.mock('../views/dashboard/LineStateCard', () => ({ LineStateCard: () => <div /> }));
vi.mock('../views/dashboard/LiveFeed', () => ({ LiveFeed: () => <div /> }));
vi.mock('../views/dashboard/OpsSummary', () => ({ OpsSummaryContent: () => <div /> }));
vi.mock('../views/dashboard/Panels', () => ({
  ActiveIssuesPanel: () => <div />,
  OutputByLinePanel: () => <div />,
  QualityByLinePanel: () => <div />,
  SensorHeatmapPanel: () => <div />,
  WorkOrdersByStatusPanel: () => <div />,
}));
vi.mock('../views/dashboard/pinned', () => ({ loadPinnedCards: connectionSpies.loadPinnedCards }));
vi.mock('../views/dashboard/PinnedInsights', () => ({ PinnedInsights: () => <div /> }));
vi.mock('../views/dashboard/stream', () => ({
  connectDashboardSSE: connectionSpies.connectDashboardSSE,
  disconnectDashboardSSE: connectionSpies.disconnectDashboardSSE,
}));
vi.mock('../views/dashboard/StewardDigest', () => ({ StewardDigest: () => null }));
vi.mock('../views/dashboard/systems', () => ({ observeSystems: connectionSpies.observeSystems }));
vi.mock('../views/dashboard/SystemsStrip', () => ({
  EmptyHero: () => <div />,
  SystemsStrip: () => <div />,
}));

import DashboardView from '../views/dashboard/DashboardView';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  briefingSpies.startBriefingLoop.mockClear();
  briefingSpies.stopBriefingLoop.mockClear();
  briefingSpies.setBriefingLifecycle.mockClear();
  Object.values(connectionSpies).forEach((spy) => spy.mockClear());
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

  it('never starts the loop while the dashboard is inactive', () => {
    act(() => root.render(<DashboardView active={false} />));
    expect(briefingSpies.startBriefingLoop).not.toHaveBeenCalled();
    expect(briefingSpies.setBriefingLifecycle).toHaveBeenLastCalledWith(false);
  });

  it('arms NO recurring dashboard work at all while inactive', () => {
    /* The whole boot cost, not just the briefing: an eagerly mounted but
     * unviewed dashboard must issue no SSE connection (which immediately
     * schedules the seven panel loaders), no systems observer, no pinned-card
     * load and no live-feed seed. */
    act(() => root.render(<DashboardView active={false} />));

    expect(connectionSpies.connectDashboardSSE).not.toHaveBeenCalled();
    expect(connectionSpies.observeSystems).not.toHaveBeenCalled();
    expect(connectionSpies.loadPinnedCards).not.toHaveBeenCalled();
    expect(connectionSpies.pollLiveFeed).not.toHaveBeenCalled();
    expect(briefingSpies.startBriefingLoop).not.toHaveBeenCalled();
  });

  it('arms every recurring dashboard read exactly once on activation', () => {
    act(() => root.render(<DashboardView active={false} />));
    act(() => root.render(<DashboardView active />));

    expect(connectionSpies.connectDashboardSSE).toHaveBeenCalledTimes(1);
    expect(connectionSpies.observeSystems).toHaveBeenCalledTimes(1);
    expect(connectionSpies.loadPinnedCards).toHaveBeenCalledTimes(1);
    expect(connectionSpies.pollLiveFeed).toHaveBeenCalledTimes(1);
    expect(briefingSpies.startBriefingLoop).toHaveBeenCalledTimes(1);
  });

  it('starts the loop once when mounted active', () => {
    act(() => root.render(<DashboardView active />));
    expect(briefingSpies.startBriefingLoop).toHaveBeenCalledTimes(1);
    expect(briefingSpies.setBriefingLifecycle).toHaveBeenLastCalledWith(true);
  });

  it('stops the loop on the active → inactive transition', () => {
    act(() => root.render(<DashboardView active />));
    expect(briefingSpies.startBriefingLoop).toHaveBeenCalledTimes(1);

    act(() => root.render(<DashboardView active={false} />));

    expect(briefingSpies.stopBriefingLoop).toHaveBeenCalled();
    expect(briefingSpies.startBriefingLoop).toHaveBeenCalledTimes(1);
  });

  it('starts exactly once on the inactive → active transition', () => {
    act(() => root.render(<DashboardView active={false} />));
    act(() => root.render(<DashboardView active />));

    expect(briefingSpies.startBriefingLoop).toHaveBeenCalledTimes(1);
  });

  it('stops the loop on unmount while active', () => {
    act(() => root.render(<DashboardView active />));
    act(() => root.unmount());

    expect(briefingSpies.stopBriefingLoop).toHaveBeenCalled();
    // A second unmount in afterEach must stay harmless.
    root = createRoot(container);
  });

  it('keeps the dashboard markup identical regardless of the briefing gate', () => {
    act(() => root.render(<DashboardView active={false} />));
    const inactiveHtml = container.innerHTML;
    act(() => root.render(<DashboardView active />));

    expect(container.innerHTML).toBe(inactiveHtml);
  });

  it('renders the contractual ids and regions in the contractual order', () => {
    /* The markup-only sections were split out of this component
     * (DashboardSections.tsx) so DashboardView could shrink to a lifecycle
     * root. A React component boundary must be invisible to the DOM, so the
     * ORDER of the contractual ids/regions is pinned here — a moved section
     * fails offline instead of only in the deployed e2e layout suite. */
    act(() => root.render(<DashboardView active />));

    expect(Array.from(container.querySelectorAll('[id]'), (n) => n.id)).toEqual([
      'ops-summary-card',
      'insights-content',
      'ai-briefing-card',
      'metrics-row',
      'metrics-oee',
      'oee-diag',
      'entity-sections',
    ]);
    expect(
      Array.from(container.querySelectorAll('[role="region"]'), (n) =>
        n.getAttribute('aria-label'),
      ),
    ).toEqual(['Connected systems', 'Dashboard data status']);
    expect(container.querySelector('.kpi-row')).not.toBeNull();
    expect(container.querySelector('.panels-grid.dashboard-dimension-grid')).not.toBeNull();
    expect(container.querySelector('#entity-sections')).toHaveProperty(
      'style.display',
      'none',
    );
  });
