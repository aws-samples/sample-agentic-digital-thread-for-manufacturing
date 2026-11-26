// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Catalog names that arrive AFTER a call.
 *
 * The Architecture view built every system-named string (graph node, HUD
 * row, trace step, trace narrative, finding node, drill-down modal metadata)
 * once, at tool_start / tool_result. A call that landed before
 * GET /api/systems answered (a fast first question, a replayed turn) kept
 * the id-derived fallback ("MES Ind") forever, and a later call to the same
 * system could show the catalog name beside it. The chat Agent Analysis
 * share bar grouped calls by the label baked at tool_result, so the same
 * system could split into two segments.
 *
 * Now the store records which tool each label came from and relabels in
 * place when the catalog's names change; the analysis panel derives its
 * labels at render. The catalog is loaded through the REAL data path
 * (systemsQueryOptions with an injected fetch). Fixture names are neutral. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { systemsQueryOptions } from '../api/systems-query';
import { AgentAnalysisPanel, type AgentAnalysisPanelData } from '../chat/analysis-panel';
import { getSystemLabel as chatSystemLabel } from '../chat/tool-labels';
import {
  buildInvCallModal,
  type ArchNode,
  type ArchStore,
} from '../lib/arch-store';
import { _resetSystemNames } from '../lib/system-names';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installArchStoreDisposal();

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function catalog(mesName = 'Riverside MES') {
  return {
    systems: [
      { id: 'erp', name: 'ERP', type: 'ERP', status: 'connected', plant: 'All Sites' },
      { id: 'mes-ind', name: mesName, type: 'MES', status: 'connected', plant: 'Riverside' },
      {
        id: 'cmms-ind',
        name: 'Riverside Plant Maintenance',
        type: 'CMMS',
        status: 'connected',
        plant: 'Riverside',
      },
    ],
  };
}

/** GET /api/systems answering, through the same query options the app uses. */
async function loadCatalog(mesName?: string): Promise<void> {
  await act(async () => {
    await createQueryClient().fetchQuery(systemsQueryOptions(async () => response(catalog(mesName))));
  });
}

const MES = 'mes-ind___query_mes_ind';
const CMMS = 'cmms-ind___query_cmms_ind';

beforeEach(() => {
  _resetSystemNames();
});

/** One question: a composition heartbeat, a MES call with rows, and a CMMS
 * call with no matching data — all BEFORE the catalog has answered. */
function replayEarlyTurn(store: ArchStore): void {
  store.handleArchEvent('user_msg', { question: 'what ran on line 2 yesterday?' });
  store.handleArchEvent('reasoning', { text: `preparing ${MES} query…` });
  store.handleArchEvent('tool_start', { tool: MES, input: { question: 'runs on line 2 yesterday' } });
  store.handleArchEvent('tool_result', {
    tool: MES,
    input: { sql: 'SELECT * FROM runs' },
    summary: '2 records found\n| run | line |\n|---|---|\n| R-1 | 2 |\n| R-2 | 2 |',
  });
  store.handleArchEvent('tool_start', { tool: CMMS, input: { question: 'work orders for line 2' } });
  store.handleArchEvent('tool_result', {
    tool: CMMS,
    input: { sql: 'SELECT * FROM work_orders' },
    summary: 'No matching entities found.',
  });
}

const nodeById = (store: ArchStore, id: string): ArchNode =>
  store.getState().nodes.find((n) => n.id === id)!;
const findingsOf = (store: ArchStore, prefix: string): ArchNode[] =>
  store
    .getState()
    .nodes.filter((n) => n.type === 'finding' && String(n.label || '').startsWith(prefix));

describe('Architecture: names that arrive after the call relabel what is on screen', () => {
  it('the early calls read the id-derived fallback before the catalog answers', () => {
    const store = createArchStore();
    replayEarlyTurn(store);
    expect(nodeById(store, 'system-mes-ind').label).toBe('MES Ind');
    expect(store.getState().hud.find((h) => h.tool === MES)!.label).toBe('MES Ind');
  });

  it('graph nodes, HUD rows, trace steps, trace narrative and modal metadata all switch to the catalog name', async () => {
    const store = createArchStore();
    replayEarlyTurn(store);
    const seen = vi.fn();
    store.subscribe(seen);

    await loadCatalog();

    // The view subscribes to the arch store only: the relabel must notify it.
    expect(seen).toHaveBeenCalled();
    const s = store.getState();

    // Graph system nodes (and their base label, used by later ×N badges).
    expect(nodeById(store, 'system-mes-ind').label).toBe('Riverside MES');
    expect(nodeById(store, 'system-mes-ind')._baseLabel).toBe('Riverside MES');
    expect(nodeById(store, 'system-cmms-ind').label).toBe('Riverside Plant Maintenance');

    // HUD rows.
    expect(s.hud.filter((h) => h.kind === 'tool').map((h) => h.label)).toEqual([
      'Riverside MES',
      'Riverside Plant Maintenance',
    ]);

    // Investigation Trace steps ("Step N · <label>") and the narrative the
    // tool_start wrote into the heartbeat row.
    const stepLabels = s.trace.filter((t) => t.nodeId?.startsWith('system-')).map((t) => t.label);
    expect(stepLabels).toContain('Riverside MES');
    expect(stepLabels).toContain('Riverside Plant Maintenance');
    expect(s.trace.map((t) => t.text)).toContain('Asking Riverside MES: “runs on line 2 yesterday”');

    // Finding nodes: the system line of a result node and of an empty result.
    const mesFinding = findingsOf(store, 'Riverside MES\n');
    expect(mesFinding).toHaveLength(1);
    expect(findingsOf(store, 'Riverside Plant Maintenance\nNo matching data')).toHaveLength(1);

    // Drill-down modal metadata (the call payload attached to the node).
    expect(buildInvCallModal(mesFinding[0])).toContain('**System:** Riverside MES');
    const aggregated = nodeById(store, 'system-mes-ind')._calls as { system: string }[];
    expect(aggregated.map((c) => c.system)).toEqual(['Riverside MES']);

    // Nothing anywhere still carries the fallback.
    expect(JSON.stringify(s)).not.toMatch(/MES Ind|CMMS Ind/);
  });

  it('a later call after the catalog never shows both names', async () => {
    const store = createArchStore();
    replayEarlyTurn(store);
    await loadCatalog();
    store.handleArchEvent('tool_start', { tool: MES, input: { question: 'run R-1 details' } });

    expect(nodeById(store, 'system-mes-ind').label).toBe('Riverside MES  ×2');
    const mesRows = store.getState().hud.filter((h) => h.tool === MES).map((h) => h.label);
    expect(mesRows).toEqual(['Riverside MES', 'Riverside MES']);
    expect(JSON.stringify(store.getState())).not.toContain('MES Ind');
  });

  it('a catalog rename relabels again, and an unchanged catalog does not re-notify', async () => {
    const store = createArchStore();
    replayEarlyTurn(store);
    await loadCatalog();
    const seen = vi.fn();
    store.subscribe(seen);

    await loadCatalog(); // same names: nothing to relabel
    expect(seen).not.toHaveBeenCalled();

    await loadCatalog('Riverside Production');
    expect(seen).toHaveBeenCalled();
    expect(nodeById(store, 'system-mes-ind').label).toBe('Riverside Production');
    expect(store.getState().hud.find((h) => h.tool === MES)!.label).toBe('Riverside Production');
    expect(store.getState().trace.map((t) => t.text)).toContain(
      'Asking Riverside Production: “runs on line 2 yesterday”',
    );
  });

  it('a disposed store stops following catalog names (no relabel, no notify)', async () => {
    const store = createArchStore();
    replayEarlyTurn(store);
    const seen = vi.fn();
    store.subscribe(seen);
    store.dispose();
    store.dispose(); // idempotent

    await loadCatalog();
    expect(seen).not.toHaveBeenCalled();
    expect(nodeById(store, 'system-mes-ind').label).toBe('MES Ind');
    // The store itself keeps working after dispose.
    store.handleArchEvent('tool_start', { tool: CMMS, input: { question: 'open work orders' } });
    expect(seen).toHaveBeenCalledTimes(1);
  });
});

describe('chat Agent Analysis share bar: one segment per system, named from the catalog', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const entry = (tool: string) => ({
    tool,
    // The thread store bakes the label at tool_result, exactly like this.
    label: chatSystemLabel(tool),
    type: 'system',
    guided: false,
    summary: '',
  });

  const panelData = (seq: ReturnType<typeof entry>[]): AgentAnalysisPanelData => ({
    toolCallSequence: seq,
    summary: null,
    agentAnalysis: null,
    hasBrowser: true,
    totalTools: seq.length,
    graphLookups: 0,
    noDataSteps: 0,
  });

  const legend = () =>
    [...container.querySelectorAll('.analysis-legend-item')].map((n) => n.textContent);

  it('relabels the legend when the catalog answers after the turn', async () => {
    const data = panelData([entry(MES)]);
    act(() => root.render(<AgentAnalysisPanel data={data} />));
    expect(legend()).toEqual(['MES Ind · 1']);

    await loadCatalog();
    expect(legend()).toEqual(['Riverside MES · 1']);
  });

  it('a call baked before and one after the catalog count as ONE system', async () => {
    const early = entry(MES);
    await loadCatalog();
    const data = panelData([early, entry(MES)]);
    act(() => root.render(<AgentAnalysisPanel data={data} />));
    expect(legend()).toEqual(['Riverside MES · 2']);
  });
});
