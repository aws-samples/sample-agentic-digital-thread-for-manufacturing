// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — #active-query tab scoping (arch-visual-polish).
 *
 * The active-query bubble renders OUTSIDE the tab-scoped containers
 * (.arch-split / #proposed-mappings-panel), so its old store-only visibility
 * (`state.activeQuestion` ⇒ .visible) bled the investigation question onto
 * every tab — reported on Self-Learning. It must be visible ONLY on the
 * Investigation tab while a question is active. The imperative cytoscape
 * modules are mocked (no canvas in jsdom); tab switching and the arch-store
 * are real. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


const toolHudRender = vi.hoisted(() => vi.fn());

vi.mock('../views/architecture/ToolHud', () => ({
  default: () => {
    toolHudRender();
    return <div id="tool-hud" />;
  },
}));
vi.mock('../views/architecture/inv-graph', () => ({
  applyQuestionFilter: vi.fn(),
  destroyInvGraph: vi.fn(),
  fitInvGraph: vi.fn(),
  initArchitecture: vi.fn(),
  resizeInvGraph: vi.fn(),
  syncFromStore: vi.fn(),
}));
vi.mock('../views/architecture/neptune-graph', async () => {
  const { createStore } = await import('../lib/store');
  return {
    loadNeptuneGraph: vi.fn(),
    resizeNeptune: vi.fn(),
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
    resizeMap: vi.fn(),
    startMapFlow: vi.fn(),
    stopAllMapFlows: vi.fn(),
    stopMapFlow: vi.fn(),
    ensureCola: vi.fn(() => false),
    ensurePopper: vi.fn(() => false),
    mapLegendStore: createStore<unknown[]>([]),
    mapLoadingStore: createStore(false),
  };
});
vi.mock('../views/architecture/proposals', () => ({
  acceptAllPendingMappings: vi.fn(),
  acceptMapping: vi.fn(),
  clearAllLearnings: vi.fn(),
  clearRejectedMappings: vi.fn(),
  clearSystemGuidance: vi.fn(),
  closeEditDialog: vi.fn(),
  editMapping: vi.fn(),
  loadLearnedGuidance: vi.fn(),
  loadProposedMappings: vi.fn(),
  populateUserDefineDatalists: vi.fn(),
  pruneGuidanceNote: vi.fn(),
  registerProposalGlobals: vi.fn(),
  rejectMapping: vi.fn(),
  resetAllMappings: vi.fn(),
  setRuleLifecycle: vi.fn(),
  sortMappingsByConsequence: (mappings: unknown[]) => mappings,
  submitEdit: vi.fn(),
  submitUserDefinedMapping: vi.fn(),
  proposalsStore: (() => {
    // Inline store literal (vi.mock factories are hoisted; keep it self-made).
    let state = {
      phase: 'idle',
      mappings: [],
      error: '',
      optimistic: {},
      ruleLifecycle: {},
      guidancePhase: 'idle',
      guidance: [],
      guidanceError: '',
      edit: null,
    };
    const subs = new Set<() => void>();
    return {
      get: () => state,
      set: (s: typeof state) => {
        state = s;
        subs.forEach((f) => f());
      },
      subscribe: (f: () => void) => {
        subs.add(f);
        return () => subs.delete(f);
      },
    };
  })(),
}));
vi.mock('../lib/version-badge', () => ({ fillVersionBadge: vi.fn() }));

import ArchitectureView from '../views/architecture/ArchitectureView';
import { archStore, resetArchitectureState } from '../lib/arch-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe('#active-query visibility is scoped to the Investigation tab', () => {
  let container: HTMLDivElement;
  let root: Root;

  const clickTab = (label: string) => {
    const tab = [...container.querySelectorAll<HTMLButtonElement>('.arch-tab')].find(
      (b) => b.textContent === label,
    );
    expect(tab, `tab '${label}'`).toBeDefined();
    act(() => tab!.click());
  };

  const bubble = () => container.querySelector<HTMLElement>('#active-query')!;

  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
    );
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<ArchitectureView active />));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    resetArchitectureState();
    vi.unstubAllGlobals();
  });

  it('stays hidden on every tab while no question is active', () => {
    for (const label of ['System Map', 'Investigation', 'Knowledge Graph', 'Self-Learning']) {
      clickTab(label);
      expect(bubble().classList.contains('visible'), label).toBe(false);
    }
  });

  it('with an active question: visible ONLY on Investigation (regression: bled into Self-Learning)', () => {
    act(() => archStore.handleArchEvent('user_msg', { question: 'Which batches failed QC?' }));

    // Default tab is System Map — hidden there too.
    expect(bubble().classList.contains('visible')).toBe(false);

    clickTab('Investigation');
    expect(bubble().classList.contains('visible')).toBe(true);
    expect(bubble().textContent).toBe('Which batches failed QC?');

    clickTab('Self-Learning');
    expect(bubble().classList.contains('visible')).toBe(false);

    clickTab('Knowledge Graph');
    expect(bubble().classList.contains('visible')).toBe(false);

    clickTab('Investigation');
    expect(bubble().classList.contains('visible')).toBe(true);
  });

  it('does not rerender the architecture JSX root for an unrelated edge-only update', () => {
    toolHudRender.mockClear();

    act(() =>
      archStore.addEdge({ id: 'unrelated', source: 'system-a', target: 'system-b' }),
    );

    expect(toolHudRender).not.toHaveBeenCalled();
  });
});
