// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const graphSpies = vi.hoisted(() => ({
  fitInvGraph: vi.fn(),
  resizeInvGraph: vi.fn(),
  resizeMap: vi.fn(),
  resizeNeptune: vi.fn(),
}));

vi.mock('../views/dashboard/AiBriefing', () => ({
  AiBriefing: () => (
    <div id="ai-briefing-card">
      <span className="briefing-title-tooltip" data-tooltip="Prompt detail">
        AI Operations Briefing
      </span>
      <span className="isa-tooltip">
        ISA-95
        <span className="isa-tooltip-body">Hierarchy detail</span>
      </span>
    </div>
  ),
}));
vi.mock('../views/dashboard/briefing', () => ({
  setBriefingLifecycle: vi.fn(),
  startBriefingLoop: vi.fn(),
  stopBriefingLoop: vi.fn(),
}));
vi.mock('../views/dashboard/Cards', () => ({
  GaugeCards: () => <div className="gauge-card" />,
  KpiRow: () => <div className="kpi-row" />,
  UncoveredStrip: () => <div className="uncovered-strip" />,
}));
vi.mock('../views/dashboard/demo', () => ({ initDemoGate: vi.fn(), registerDemoGlobals: vi.fn() }));
vi.mock('../views/dashboard/DemoModal', () => ({ DemoModal: () => null }));
vi.mock('../views/dashboard/DetailModal', () => ({ DetailModal: () => null }));
vi.mock('../views/dashboard/diag', () => ({ initDiagMode: vi.fn() }));
vi.mock('../views/dashboard/feed', () => ({ pollLiveFeed: vi.fn() }));
vi.mock('../views/dashboard/lib', () => ({ getToolLabel: vi.fn(() => 'tool') }));
vi.mock('../views/dashboard/LineStateCard', () => ({ LineStateCard: () => <div /> }));
vi.mock('../views/dashboard/LiveFeed', () => ({ LiveFeed: () => <div /> }));
vi.mock('../views/dashboard/OpsSummary', () => ({ OpsSummaryContent: () => <div /> }));
vi.mock('../views/dashboard/Panels', () => ({
  ActiveIssuesPanel: () => <div />,
  OutputByLinePanel: () => <div />,
  QualityByLinePanel: () => <div />,
  SensorHeatmapPanel: () => <div className="heatmap-table-wrap" />,
  WorkOrdersByStatusPanel: () => <div />,
}));
vi.mock('../views/dashboard/pinned', () => ({ loadPinnedCards: vi.fn() }));
vi.mock('../views/dashboard/PinnedInsights', () => ({ PinnedInsights: () => <div /> }));
vi.mock('../views/dashboard/stream', () => ({
  connectDashboardSSE: vi.fn(),
  disconnectDashboardSSE: vi.fn(),
}));
vi.mock('../views/dashboard/systems', () => ({ loadSystems: vi.fn() }));
vi.mock('../views/dashboard/SystemsStrip', () => ({
  EmptyHero: () => <div />,
  SystemsStrip: () => <div className="systems-bar" />,
}));

vi.mock('../views/architecture/inv-graph', () => ({
  applyQuestionFilter: vi.fn(),
  destroyInvGraph: vi.fn(),
  fitInvGraph: graphSpies.fitInvGraph,
  initArchitecture: vi.fn(),
  resizeInvGraph: graphSpies.resizeInvGraph,
  syncFromStore: vi.fn(),
}));
vi.mock('../views/architecture/neptune-graph', async () => {
  const { createStore } = await import('../lib/store');
  return {
    loadNeptuneGraph: vi.fn(),
    resizeNeptune: graphSpies.resizeNeptune,
    neptuneLegendStore: createStore<string[]>([]),
    kgAnalysisStore: createStore({ status: 'idle' }),
    kgSelectionStore: createStore(null),
  };
});
vi.mock('../views/architecture/system-map', async () => {
  const { createStore } = await import('../lib/store');
  return {
    clearTraversed: vi.fn(),
    destroySystemMap: vi.fn(),
    initSystemMap: vi.fn(),
    resizeMap: graphSpies.resizeMap,
    startMapFlow: vi.fn(),
    stopAllMapFlows: vi.fn(),
    stopMapFlow: vi.fn(),
    mapLoadingStore: createStore(false),
  };
});
vi.mock('../views/architecture/proposals', () => ({
  acceptAllPendingMappings: vi.fn(),
  clearAllLearnings: vi.fn(),
  clearRejectedMappings: vi.fn(),
  loadLearnedGuidance: vi.fn(),
  loadProposedMappings: vi.fn(),
  populateUserDefineDatalists: vi.fn(),
  registerProposalGlobals: vi.fn(),
  resetAllMappings: vi.fn(),
  submitUserDefinedMapping: vi.fn(),
}));
vi.mock('../views/architecture/KgSelectionInfo', () => ({ KgSelectionInfo: () => <div /> }));
vi.mock('../views/architecture/Legends', () => ({
  ArchLegend: () => <div className="arch-legend" />,
  GraphLegend: () => <div id="neptune-legend" />,
  MapLegend: () => <div id="map-legend" />,
}));
vi.mock('../views/architecture/NeptuneAnalysisPanel', () => ({
  NeptuneAnalysisPanel: () => <div />,
}));
vi.mock('../views/architecture/ProposalsPanel', () => ({
  LearnedGuidanceList: () => <div />,
  PendingBadge: () => null,
  ProposalsList: () => <div />,
}));
vi.mock('../views/architecture/ToolHud', () => ({ default: () => <div className="tool-hud" /> }));
vi.mock('../views/architecture/TraceEntries', () => ({
  default: ({ visible }: { visible: boolean }) => (
    <div id="reasoning-trace" className={visible ? '' : 'hidden'} />
  ),
}));
vi.mock('../lib/version-badge', () => ({ fillVersionBadge: vi.fn() }));

import { archStore, resetArchitectureState } from '../lib/arch-store';
import { AgentsFleet } from '../shared/agents/AgentsFleet';
import ArchitectureView from '../views/architecture/ArchitectureView';
import DashboardView from '../views/dashboard/DashboardView';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const css = readFileSync(path.resolve(TEST_DIR, '../styles/dashboard.css'), 'utf8');

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  vi.stubGlobal(
    'matchMedia',
    vi.fn((media: string) => ({
      matches: media === '(max-width: 760px)',
      media,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    })),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetArchitectureState();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function flushQueries(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await Promise.resolve();
  });
}

describe('mobile dashboard and fleet layout', () => {
  it('renders responsive wrappers and provides click/keyboard tooltip disclosure', () => {
    act(() => root.render(<DashboardView active={false} />));

    expect(container.querySelector('.dashboard-summary-grid')).not.toBeNull();
    expect(container.querySelectorAll('.dashboard-strip-scroll')).toHaveLength(2);

    const briefing = container.querySelector<HTMLElement>('.briefing-title-tooltip')!;
    const isa = container.querySelector<HTMLElement>('.isa-tooltip')!;
    expect(briefing.tabIndex).toBe(0);
    expect(isa.tabIndex).toBe(0);
    expect(briefing.getAttribute('aria-expanded')).toBe('false');

    act(() => briefing.click());
    expect(briefing.classList.contains('tooltip-open')).toBe(true);
    expect(briefing.getAttribute('aria-expanded')).toBe('true');

    act(() => {
      isa.focus();
      isa.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(isa.classList.contains('tooltip-open')).toBe(true);
    expect(briefing.classList.contains('tooltip-open')).toBe(false);

    act(() => isa.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(isa.classList.contains('tooltip-open')).toBe(false);
  });

  it('uses responsive fleet classes and an explicit local pricing scroll region', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    const fetchFn = vi.fn(async (url: string) => {
      const body = url.startsWith('/api/admin/agents/neptune')
        ? { status: 'AVAILABLE', graph_id: 'g', cost_usd: 0 }
        : url.startsWith('/api/admin/agents/log-tail')
          ? { lines: [] }
          : url.startsWith('/api/admin/agents')
            ? {
                agents: [
                  { runtime_name: 'adt_orchestrator', display_name: 'Orchestrator', status: 'READY' },
                  { runtime_name: 'adt_system', display_name: 'System Agent', status: 'READY' },
                ],
              }
            : {};
      return { ok: true, status: 200, json: async () => body } as Response;
    });

    act(() =>
      root.render(
        <QueryClientProvider client={queryClient}>
          <AgentsFleet containerId="mobile-fleet" fetchFn={fetchFn} />
        </QueryClientProvider>,
      ),
    );
    for (let attempt = 0; attempt < 8 && !container.querySelector('.fleet-summary'); attempt += 1) {
      await flushQueries();
    }

    expect(container.querySelector('.agents-fleet')).not.toBeNull();
    expect(container.querySelector('.fleet-summary.agents-grid')).not.toBeNull();
    expect(container.querySelector('.fleet-primary-grid')).not.toBeNull();
    expect(container.querySelector('.fleet-systems-layout')).not.toBeNull();
    expect(container.querySelector('[aria-label="Agent model pricing"].fleet-table-scroll')).not.toBeNull();
    queryClient.clear();
  });

  it('pins one-column grids, local horizontal scrolling, and coarse-pointer targets in CSS', () => {
    expect(css).toMatch(/@media\s*\(max-width:\s*760px\)/);
    expect(css).toMatch(/\.dashboard-summary-grid,[\s\S]*\.metrics-row,[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
    expect(css).toMatch(/\.dashboard-strip-scroll,[\s\S]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.fleet-table-scroll table\s*\{\s*min-width:/);
    expect(css).toMatch(/@media\s*\(hover:\s*none\),\s*\(pointer:\s*coarse\)[\s\S]*min-height:\s*44px/);
  });
});

describe('mobile architecture panes', () => {
  it('switches every narrow pane on-screen and refits the graph after returning to it', () => {
    act(() => root.render(<ArchitectureView active />));
    const clickTab = (label: string) => {
      const tab = [...container.querySelectorAll<HTMLButtonElement>('.arch-tab')].find(
        (button) => button.textContent === label,
      );
      expect(tab).toBeDefined();
      act(() => tab!.click());
    };

    clickTab('Investigation');
    act(() => archStore.handleArchEvent('user_msg', { question: 'Trace this issue' }));

    const paneGroup = container.querySelector<HTMLElement>('[aria-label="Architecture pane"]')!;
    expect(paneGroup).not.toBeNull();
    expect(paneGroup.textContent).toContain('Graph');
    expect(paneGroup.textContent).toContain('Investigation trace');

    const secondary = [...paneGroup.querySelectorAll('button')].find(
      (button) => button.textContent === 'Investigation trace',
    )!;
    act(() => secondary.click());
    expect(container.querySelector('.arch-split')!.classList.contains('arch-mobile-pane-secondary')).toBe(true);

    graphSpies.fitInvGraph.mockClear();
    const graph = [...paneGroup.querySelectorAll('button')].find((button) => button.textContent === 'Graph')!;
    act(() => graph.click());
    expect(container.querySelector('.arch-split')!.classList.contains('arch-mobile-pane-graph')).toBe(true);
    expect(graphSpies.fitInvGraph).toHaveBeenCalled();

    clickTab('Knowledge Graph');
    const kgGroup = container.querySelector<HTMLElement>('[aria-label="Architecture pane"]')!;
    expect(kgGroup.textContent).toContain('Graph explorer');
    const explorer = [...kgGroup.querySelectorAll('button')].find(
      (button) => button.textContent === 'Graph explorer',
    )!;
    act(() => explorer.click());
    expect(container.querySelector('.arch-split')!.classList.contains('arch-mobile-pane-secondary')).toBe(true);
    expect(container.querySelector('#kg-explorer-panel')).not.toBeNull();
  });

  it('keeps tabs scrollable and makes graph, trace, and explorer full-width mobile panes', () => {
    expect(css).toMatch(/\.arch-tabs\s*\{[\s\S]*overflow-x:\s*auto/);
    expect(css).toMatch(/\.arch-split\.arch-narrow #cy-container,[\s\S]*#kg-explorer-panel[\s\S]*width:\s*100%\s*!important/);
    expect(css).toMatch(/\.arch-split\.arch-tab-neptune\.arch-mobile-pane-secondary #kg-explorer-panel\s*\{\s*display:\s*flex\s*!important/);
    expect(css).toMatch(/#kg-explorer-panel\s*\{[\s\S]*left:\s*0;[\s\S]*right:\s*0/);
  });
});
