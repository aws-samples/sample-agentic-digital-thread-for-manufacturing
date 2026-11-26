// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Architecture view's result line for a call that found nothing, and for
 * a summary that opens with markdown.
 *
 * Live walk (2026-10-27): a telemetry call whose reads all came back with 0
 * points showed "Returned: ## Result" and the graph data node "<system> /
 * ## Result"; SQL calls that found none of the requested runs showed
 * "## Result" / "## Results" the same way; every count line read
 * "Returned: Returned ~7 shipments".
 *
 * The rules pinned here:
 * - the line never shows markdown syntax: the first line of real content, as
 *   plain text;
 * - a call the engine's typed no-data line says came back with no rows (the
 *   verdict its 'empty' outcome already rests on) reads "No matching data",
 *   whatever the reply's own tables hold;
 * - the drill-down labels the line "Result:", so a count line does not say
 *   "Returned" twice; the graph node keeps the "Returned ..." line.
 * Failed, timed-out and partial calls keep their lines. */
import { describe, expect, it } from 'vitest';

import { buildInvNodeModal, type ArchNode, type ArchStore, type InvCallDetail } from '../lib/arch-store';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

const SQL_TOOL = 'plant-ops___query_plant_ops';
const SQL_SYS = 'system-plant-ops';
const TELEM_TOOL = 'telem-src___query_telem_src';
const TELEM_SYS = 'system-telem-src';

const NO_ROWS_LINE = '[engine] No query this call ran returned data (no rows).';
const WHERE_LINE =
  "[engine] WHERE filters of this turn's queries that returned results, in run order, as executed " +
  '(a query with no WHERE is not listed): Q1 (0 rows): WHERE pr.run_number IN (<3 values>)';

interface Run {
  tool: string;
  question: string;
  summary: string;
  coverage?: string[];
  elapsedMs?: number;
}

function runLive(store: ArchStore, run: Run): void {
  store.handleArchEvent('user_msg', { question: 'What happened on the line?' });
  store.handleArchEvent('tool_start', { tool: run.tool, tool_id: 't1', input: { question: run.question } });
  store.handleArchEvent('tool_result', {
    tool: run.tool,
    tool_id: 't1',
    summary: run.summary,
    input: '{"raw": "result"}',
    elapsed_ms: run.elapsedMs ?? 19_000,
    ...(run.coverage ? { telemetry_coverage: run.coverage } : {}),
  });
  store.handleArchEvent('done', {});
}

function callOf(store: ArchStore, sysId: string): InvCallDetail {
  const sys = store.getState().nodes.find((n) => n.id === sysId) as ArchNode;
  return (sys._calls as InvCallDetail[])[0];
}

/** Every place the Architecture view shows this call's result line. */
function resultLines(store: ArchStore, sysId: string, tool: string) {
  const nodes = store.getState().nodes;
  const sys = nodes.find((n) => n.id === sysId) as ArchNode;
  const finding = nodes.find((n) => n.type === 'finding' && (n._call as InvCallDetail | undefined)?.tool === tool)!;
  return {
    drilldown: String(callOf(store, sysId).resultEnglish),
    legacy: String((sys._results as string[])[0]),
    nodeLabel: String(finding.label).split('\n')[1],
    modal: /\*\*Result:\*\* (.*)/.exec(buildInvNodeModal(sys))?.[1] ?? '',
  };
}

function allEqual(lines: ReturnType<typeof resultLines>, expected: string): void {
  expect(lines).toEqual({ drilldown: expected, legacy: expected, nodeLabel: expected, modal: expected });
}

const ZERO_READS = ['Vib_X', 'Vib_Y', 'Vib_Z'].map(
  (p) => `Telemetry read: AX-9-Press / ${p}, 2026-10-27T20:11Z to 2026-10-27T21:11Z, 0 points (no data)`,
);

const TELEM_HEADING_SUMMARY = [
  '## Result',
  '',
  'No vibration readings were found for press AX-9 in the requested window.',
  '',
  'Details of the search:',
  '',
  '- Asset: AX-9-Press',
  '- Properties queried: Vib_X, Vib_Y, Vib_Z',
  '- Result: zero readings returned for all three axes',
].join('\n');

const SQL_HEADING_SUMMARY = [
  '## Results',
  '',
  'None of the three runs you asked about exist in this system.',
  '',
  'The runs you searched for:',
  '',
  'RUN-A-001: no record found',
  'RUN-A-002: no record found',
  'RUN-A-003: no record found',
  '',
  WHERE_LINE,
].join('\n');

function requestedTable(): string {
  return [
    '| REQUESTED RUN | FOUND IN SYSTEM? |',
    '|---|---|',
    '| RUN-A-001 | No |',
    '| RUN-A-002 | No |',
    '| RUN-A-003 | No |',
  ].join('\n');
}

const NO_ROWS_TABLE_SUMMARY = [
  '**Result:**',
  '',
  'None of the three requested runs exist in this system:',
  '',
  requestedTable(),
  '',
  NO_ROWS_LINE,
].join('\n');

function shipmentsSummary(rows: number): string {
  const ids = Array.from({ length: Math.min(rows, 4) }, (_, i) => `S-${i + 1} (${i + 2})`);
  return `${rows} records found: ${ids.join(', ')}${rows > 4 ? '...' : ''}`;
}

describe('a summary that opens with markdown never shows markdown as its line', () => {
  it('case 1: a telemetry call whose reply opens "## Result" reads its first line of content', () => {
    // Reproduction: all four surfaces said "## Result".
    const store = createArchStore();
    runLive(store, {
      tool: TELEM_TOOL,
      question: 'List every vibration reading on press AX-9 in the last hour.',
      summary: TELEM_HEADING_SUMMARY,
      coverage: ZERO_READS,
    });
    allEqual(
      resultLines(store, TELEM_SYS, TELEM_TOOL),
      'No vibration readings were found for press AX-9 in the requested window.',
    );
  });

  it('case 2: a SQL call whose reply opens "## Results" reads its first line of content', () => {
    // Reproduction: all four surfaces said "## Results".
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Do any of these production runs exist: RUN-A-001, RUN-A-002, RUN-A-003?',
      summary: SQL_HEADING_SUMMARY,
    });
    allEqual(resultLines(store, SQL_SYS, SQL_TOOL), 'None of the three runs you asked about exist in this system.');
  });
});

describe('a call the engine says came back with no rows reads "No matching data"', () => {
  it('the typed no-rows line beats a reply table', () => {
    // Reproduction: "Returned ~3 production runs", the rows of the agent's
    // REQUESTED / FOUND table, on a call whose every query came back empty.
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Do any of these production runs exist: RUN-A-001, RUN-A-002, RUN-A-003?',
      summary: NO_ROWS_TABLE_SUMMARY,
    });
    expect(callOf(store, SQL_SYS).outcome).toBe('empty');
    allEqual(resultLines(store, SQL_SYS, SQL_TOOL), 'No matching data');
  });

  it('the typed no-rows line beats a markdown heading', () => {
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Which work orders are open on the press?',
      summary: `## Result\n\nThere are no open work orders for that press in the order table.\n\n${NO_ROWS_LINE}`,
    });
    expect(callOf(store, SQL_SYS).outcome).toBe('empty');
    allEqual(resultLines(store, SQL_SYS, SQL_TOOL), 'No matching data');
  });

  it('the outcome and the line rest on the same line: no engine line, no "No matching data"', () => {
    // The same reply without the engine line is a success with a table; its
    // line is its own first sentence, never a count of the table rows.
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Do any of these production runs exist: RUN-A-001, RUN-A-002, RUN-A-003?',
      summary: NO_ROWS_TABLE_SUMMARY.replace(NO_ROWS_LINE, ''),
    });
    expect(callOf(store, SQL_SYS).outcome).toBe('success');
    allEqual(resultLines(store, SQL_SYS, SQL_TOOL), 'None of the three requested runs exist in this system:');
  });
});

describe("guards: failed, timed-out and partial calls keep their lines", () => {
  it('a no-data line that names a failure keeps the failure rendering', () => {
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Which work orders are open?',
      summary: `## Result\n\nThe queries did not complete.\n\n[engine] No query this call ran returned data (sql_error).`,
    });
    const err = store.getState().nodes.find((n) => n.outcome === 'failed')!;
    expect(err.label).toBe('failed');
    expect((err._call as InvCallDetail).resultEnglish).not.toBe('No matching data');
  });

  it('a no-rows call past the time budget is a timeout, not "No matching data"', () => {
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Which work orders are open?',
      summary: NO_ROWS_TABLE_SUMMARY,
      elapsedMs: 70_000,
    });
    const err = store.getState().nodes.find((n) => n.outcome === 'timeout')!;
    expect(err.label).toBe('timed out');
    expect((err._call as InvCallDetail).resultEnglish).not.toBe('No matching data');
  });

  it('a partial call keeps its own line', () => {
    const store = createArchStore();
    runLive(store, {
      tool: SQL_TOOL,
      question: 'Do any of these production runs exist: RUN-A-001, RUN-A-002, RUN-A-003?',
      summary: `partial: stopped at the scan budget\n\n${NO_ROWS_TABLE_SUMMARY}`,
    });
    expect(callOf(store, SQL_SYS).outcome).toBe('partial');
    allEqual(resultLines(store, SQL_SYS, SQL_TOOL), 'partial: stopped at the scan budget');
  });

  it("a successful table reply with typed telemetry lines still reads its reads count", () => {
    const store = createArchStore();
    runLive(store, {
      tool: TELEM_TOOL,
      question: 'List every vibration reading on press AX-9 in the last hour.',
      summary: `## Result\n\n${requestedTable()}`,
      coverage: ZERO_READS.map((l) => l.replace('0 points (no data)', '18 points')),
    });
    allEqual(resultLines(store, TELEM_SYS, TELEM_TOOL), '3 telemetry reads');
  });
});

describe('case 4: a count line does not say "Returned" twice', () => {
  it('the drill-down labels the line "Result:" and the graph node keeps "Returned ..."', () => {
    // Reproduction: "Returned: Returned ~7 shipments". The count line here
    // is the server's typed records-found count.
    const store = createArchStore();
    runLive(store, { tool: SQL_TOOL, question: 'List all shipments in the last 7 days.', summary: shipmentsSummary(7) });
    const sys = store.getState().nodes.find((n) => n.id === SQL_SYS) as ArchNode;
    const modal = buildInvNodeModal(sys);
    expect(modal).not.toContain('Returned:** Returned');
    expect(modal).toContain('**Result:** Returned 7 records');
    allEqual(resultLines(store, SQL_SYS, SQL_TOOL), 'Returned 7 records');
  });

  it('the legacy fallback copy is labelled the same way', () => {
    const md = buildInvNodeModal({ id: 'n', _requests: ['Fetch orders'], _results: ['Returned ~3 rows'], _fullBodies: ['x'] });
    expect(md).toContain('**Result:** Returned ~3 rows');
    expect(md).not.toContain('**Returned:**');
  });
});
