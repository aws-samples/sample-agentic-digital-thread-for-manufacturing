// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* How the Architecture view's Investigation graph shows a call's result
 * beyond its line: the data node's hover tooltip, the Knowledge Graph
 * finding label, and the count a result line may carry.
 *
 * Live walk (2026-10-28):
 * 1. Hovering a data node showed its detail as raw markdown ("there are
 *    **no vibration sensor readings ... / - **Requested window:** ... / - *").
 * 2. The Knowledge Graph finding node read "Knowledge Graph / Looked up this
 *    question in the knowledge graph before asking any system. - Conne…": a
 *    list marker joined into the middle of the label.
 * 3. Counts did not match what came back: a 3-row table of batches read
 *    "Returned ~5 shipments", 3 runs and 6 checks read "Returned ~9
 *    production runs", 47 line items in 21 shipments read "Returned ~6
 *    shipments". The number counted every table row line in the reply (and
 *    every later table's header) and took its noun from the request.
 * 4. The same kind of count on the Knowledge Graph: the routing step's three
 *    lines of text read "Found 3 graph entities", and a result the server cut
 *    at 8 lines ("(+12 more)") read "Found 8 graph entities". The number
 *    counted the summary's lines.
 *
 * The rules pinned here:
 * - the tooltip is the detail as plain text (plainSummaryText), capped with
 *   "…": inline syntax removed like the label and the Result line, but a
 *   table row kept as its cells' text and fenced code as written, so a
 *   result that is only a table or only code still has a preview;
 * - a label that joins several lines into one drops each line's list marker;
 * - a reply's tables give no count: the line is the reply's first content
 *   sentence; typed counts (records found, telemetry reads, no rows) stay;
 * - a knowledge-graph result gives no count either: nothing typed says how
 *   many entities came back, so its line is its first content line. */
import { describe, expect, it } from 'vitest';

import { buildInvNodeModal, invTooltipText, type ArchNode, type ArchStore, type InvCallDetail } from '../lib/arch-store';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

const ERP_TOOL = 'erp___query_erp';
const ERP_SYS = 'system-erp';
const MES_TOOL = 'plant-mes___query_plant_mes';
const MES_SYS = 'system-plant-mes';

/** The walk's empty-window telemetry reply, in the shape it was shown. */
const EMPTY_WINDOW_REPLY = [
  'Based on the query results, there are **no vibration sensor readings available for the 5-Axis-Milling-CMX-441 ' +
    'asset during the requested time window of 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z**.',
  '',
  'The system indicates that:',
  '- **Requested window:** 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z',
  '- **Data available from:** 2026-10-24T17:10:10Z',
].join('\n');

function call(store: ArchStore, tool: string, question: string, summary: string, id = 't1'): void {
  store.handleArchEvent('tool_start', { tool, tool_id: id, input: { question } });
  store.handleArchEvent('tool_result', { tool, tool_id: id, summary, input: '{"raw": "result"}', elapsed_ms: 12_000 });
}

/** Every place the view shows the result line of the `index`th call to a system. */
function resultLines(store: ArchStore, sysId: string, index = 0) {
  const nodes = store.getState().nodes;
  const sys = nodes.find((n) => n.id === sysId) as ArchNode;
  const detail = (sys._calls as InvCallDetail[])[index];
  const finding = nodes.filter((n) => n.type === 'finding' && (n._call as InvCallDetail | undefined)?.tool === detail.tool)[index];
  return {
    drilldown: String(detail.resultEnglish),
    legacy: String((sys._results as string[])[index]),
    nodeLabel: String(finding.label).split('\n')[1] ?? '',
  };
}

function noCount(lines: ReturnType<typeof resultLines>, expected: string): void {
  expect(lines).toEqual({ drilldown: expected, legacy: expected, nodeLabel: expected });
  expect(Object.values(lines).join(' ')).not.toMatch(/~\d/);
}

describe('1. the data node tooltip reads as plain text', () => {
  it('shows the detail without markdown, one line per content line', () => {
    const text = invTooltipText(EMPTY_WINDOW_REPLY);
    expect(text).not.toMatch(/\*\*|`|^#|^\s*[-*+]\s/m);
    expect(text.split('\n').slice(0, 2)).toEqual([
      'Based on the query results, there are no vibration sensor readings available for the 5-Axis-Milling-CMX-441 ' +
        'asset during the requested time window of 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z.',
      'The system indicates that:',
    ]);
    expect(text).toContain('\nRequested window: 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z');
  });

  it('is capped with "…" and never cuts into markdown, because there is none left', () => {
    const long = `**${'reading '.repeat(60).trim()}**`;
    const text = invTooltipText(long);
    expect(text.endsWith('…')).toBe(true);
    expect(text.length).toBeLessThanOrEqual(301);
    expect(text).not.toContain('*');
  });

  it('is "" for a detail of markdown structure only, and plain details are unchanged', () => {
    expect(invTooltipText('## Result\n\n---')).toBe('');
    expect(invTooltipText('')).toBe('');
    expect(invTooltipText(undefined)).toBe('');
    expect(invTooltipText('Concept — identity')).toBe('Concept — identity');
  });

  it('keeps a table-only or code-only detail as text, and pipes in prose as prose', () => {
    // Review: table rows and fenced lines were dropped, so these details had
    // no tooltip at all, and a prose line with "||" was read as a table row.
    expect(invTooltipText('## Result\n\n| batch | line items |\n|---|---|\n| B-1 | 45 |')).toBe(
      'batch · line items\nB-1 · 45',
    );
    expect(invTooltipText('```json\n{"orders": 1}\n```')).toBe('{"orders": 1}');
    expect(invTooltipText('batch | line items\n--- | ---\nB-1 | 45')).toBe('batch · line items\nB-1 · 45');
    expect(invTooltipText("Query: status = 'open' || status = 'held'")).toBe("Query: status = 'open' || status = 'held'");
  });

  it('reads a detail in any script, and caps a long table at the same length', () => {
    // Review: a Japanese or Arabic detail gave no tooltip.
    expect(invTooltipText('検査結果は正常です。\n- 第一項')).toBe('検査結果は正常です。\n第一項');
    expect(invTooltipText('نتيجة الفحص طبيعية.')).toBe('نتيجة الفحص طبيعية.');
    const rows = Array.from({ length: 40 }, (_, i) => `| B-${i} | ${i} |`);
    const text = invTooltipText(['| batch | items |', '|---|---|', ...rows].join('\n'));
    expect(text.endsWith('…')).toBe(true);
    expect(text.length).toBeLessThanOrEqual(301);
    expect(text).not.toContain('|');
  });

  it('the node keeps the full summary as its detail, so the drill-down still renders it', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'Any vibration on the CMX-441 on Oct 20?' });
    call(store, 'sitewise___query_sitewise', 'vibration readings 2026-10-20 00:00-01:00', EMPTY_WINDOW_REPLY);
    const finding = store.getState().nodes.find((n) => n.type === 'finding')!;
    expect(finding.detail).toBe(EMPTY_WINDOW_REPLY);
    expect(invTooltipText(finding.detail)).not.toContain('**');
  });
});

describe('2. a label that joins lines into one leaves no list marker mid-line', () => {
  const SEED =
    'Looked up this question in the knowledge graph before asking any system.\n' +
    '- Connected systems: ERP, Plant MES\n' +
    '- Shared concepts linking them: batch, shipment';

  function kgLabel(summary: string): string {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'Which batches did the shipments come from?' });
    store.handleArchEvent('tool_start', { tool: 'explore_graph', tool_id: 'mode-a-route-seed', input: { question: 'seed' } });
    store.handleArchEvent('tool_result', { tool: 'explore_graph', tool_id: 'mode-a-route-seed', summary });
    const finding = store.getState().nodes.find((n) => n.id.startsWith('finding-kg-'))!;
    return String(finding.label);
  }

  it('the Knowledge Graph finding label', () => {
    // Reproduction: "... before asking any system. - Conne…".
    const label = kgLabel(SEED);
    expect(label).toBe(
      'Knowledge Graph\nLooked up this question in the knowledge graph before asking any system. Connect…',
    );
    expect(label).not.toMatch(/\s[-*+]\s/);
  });

  it('short lines are joined whole, each without its marker', () => {
    expect(kgLabel('- Connected systems: ERP\n- Shared concepts: lot')).toBe(
      'Knowledge Graph\nConnected systems: ERP Shared concepts: lot',
    );
  });

  it('a scalar summary is unchanged', () => {
    expect(kgLabel('42')).toBe('Knowledge Graph\n42');
  });

  it('a summary in any script keeps its text', () => {
    // Review: a non-Latin summary left the label with no second line.
    expect(kgLabel('- 第一項\n- 第二項')).toBe('Knowledge Graph\n第一項 第二項');
  });
});

describe('3. a result line never carries a count the call did not return', () => {
  it('ERP Request 2: a 3-row batch table and a total table read the reply sentence, not "~5 shipments"', () => {
    const summary = [
      'The 47 shipment line items came from 3 distinct production batches.',
      '',
      '| batch | line items |',
      '|---|---|',
      '| RUN-20261027-00003 | 1 |',
      '| RUN-20261024-93000 | 1 |',
      '| RUN-20261026-90001 | 45 |',
      '',
      '| total line items |',
      '|---|',
      '| 47 |',
    ].join('\n');
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'Which batches did the shipments come from?' });
    call(store, ERP_TOOL, 'Which production batches did the shipments of part 7742 come from?', summary);
    noCount(resultLines(store, ERP_SYS), 'The 47 shipment line items came from 3 distinct production batches.');
  });

  it('the plant MES call: a runs table and a checks table are not added into "production runs"', () => {
    const runs = ['| run | line | status |', '|---|---|---|', '| R1 | L1 | ok |', '| R2 | L2 | ok |', '| R3 | L3 | failed |'];
    const checks = ['| run | check | result |', '|---|---|---|'].concat(
      Array.from({ length: 6 }, (_, i) => `| R${(i % 3) + 1} | C${i + 1} | ${i < 3 ? 'pass' : 'fail'} |`),
    );
    const summary = ['Three production runs were found; one failed inspection.', '', ...runs, '', ...checks].join('\n');
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'Did any of those batches fail inspection?' });
    call(store, MES_TOOL, 'Did production runs R1, R2 or R3 fail inspection?', summary);
    noCount(resultLines(store, MES_SYS), 'Three production runs were found; one failed inspection.');
  });

  it('ERP Request 1: a table of 6 rows over 47 line items does not read "~6 shipments"', () => {
    const body = Array.from({ length: 6 }, (_, i) => `| 2026-10-2${i} | ${i + 3} |`);
    const summary = [
      'There are 47 shipment line items across 21 unique shipment IDs in the window.',
      '',
      '| ship date | line items |',
      '|---|---|',
      ...body,
    ].join('\n');
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'Which batches did the shipments come from?' });
    call(store, ERP_TOOL, 'List the shipments of part 7742 over the past 7 days.', summary);
    noCount(resultLines(store, ERP_SYS), 'There are 47 shipment line items across 21 unique shipment IDs in the window.');
  });

  it('a reply that is only a table shows no count on any surface', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'List the orders.' });
    call(store, ERP_TOOL, 'List the open orders.', '| order |\n|---|\n| A |\n| B |\n| C |');
    const sys = store.getState().nodes.find((n) => n.id === ERP_SYS) as ArchNode;
    expect((sys._calls as InvCallDetail[])[0].resultEnglish).toBe('');
    expect(buildInvNodeModal(sys)).not.toContain('**Result:**');
    const finding = store.getState().nodes.find((n) => n.type === 'finding')!;
    expect(String(finding.label)).not.toMatch(/Returned|~\d/);
  });

  it("its node label takes no table name from a system agent's English request", () => {
    // A system query's request is English by contract: "from the last week"
    // named the table "the".
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'List the orders.' });
    const q = 'List the open orders from the last week.';
    store.handleArchEvent('tool_start', { tool: ERP_TOOL, tool_id: 't1', input: { question: q } });
    store.handleArchEvent('tool_result', { tool: ERP_TOOL, tool_id: 't1', summary: '| order |\n|---|\n| A |\n| B |', input: { question: q } });
    const finding = store.getState().nodes.find((n) => n.type === 'finding')!;
    expect(String(finding.label).split('\n')[1]).toBe('Result');
  });

  it('a SQL request still names its table when the reply gives no line', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'List the orders.' });
    const sql = 'SELECT order_id FROM open_orders';
    store.handleArchEvent('tool_start', { tool: 'lake___athena_run_query', tool_id: 't1', input: { sql } });
    store.handleArchEvent('tool_result', {
      tool: 'lake___athena_run_query',
      tool_id: 't1',
      summary: '| order_id |\n|---|\n| A |\n| B |',
      input: { sql },
    });
    const finding = store.getState().nodes.find((n) => n.type === 'finding')!;
    expect(String(finding.label).split('\n')[1]).toBe('open orders');
  });

  it('a typed count stays: the records-found summary the server builds from the result rows', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'List the orders.' });
    call(store, 'lake___athena_run_query', 'SELECT * FROM orders', '12 records found: A, B, C, D...');
    expect(resultLines(store, 'system-lake')).toEqual({
      drilldown: 'Returned 12 records',
      legacy: 'Returned 12 records',
      nodeLabel: 'Returned 12 records',
    });
  });
});

describe('4. a Knowledge Graph result line never counts the lines of its summary', () => {
  const KG_SYS = 'system-neptune';

  function kgResult(toolId: string, summary: string): { line: string; modal: string } {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'Which batches did the shipments come from?' });
    store.handleArchEvent('tool_start', { tool: 'explore_graph', tool_id: toolId, input: { question: 'kg' } });
    store.handleArchEvent('tool_result', { tool: 'explore_graph', tool_id: toolId, summary });
    const hub = store.getState().nodes.find((n) => n.id === KG_SYS) as ArchNode;
    const line = String((hub._calls as InvCallDetail[])[0].resultEnglish);
    expect((hub._results as string[])[0]).toBe(line);
    return { line, modal: buildInvNodeModal(hub) };
  }

  it('the routing step reads its first sentence, not "Found 3 graph entities" for three lines of text', () => {
    const seed =
      'Looked up this question in the knowledge graph before asking any system.\n' +
      '- Connected systems: ERP, Plant MES\n' +
      '- Shared concepts linking them: batch, shipment';
    const { line, modal } = kgResult('mode-a-route-seed', seed);
    expect(line).toBe('Looked up this question in the knowledge graph before asking any system.');
    expect(modal).not.toMatch(/graph entit/);
  });

  it('a result the server cut at 8 lines does not read "Found 8 graph entities"', () => {
    const shown = Array.from({ length: 8 }, (_, i) => `Concept: c${i + 1} (domain)`);
    const { line, modal } = kgResult('t-kg', shown.join('\n') + ' (+12 more)');
    expect(line).toBe('Concept: c1 (domain)');
    expect(modal).not.toMatch(/Found \d+ graph/);
  });

  it('an empty result keeps its typed line', () => {
    expect(kgResult('t-kg', 'No matching entities found.').line).toBe('No matching data');
  });
});
