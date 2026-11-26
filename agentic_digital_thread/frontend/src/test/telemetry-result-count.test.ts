// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Architecture drill-down's result line for a telemetry call.
 *
 * A system query call that read telemetry got its "Returned:" line (and its
 * graph data-node label) from counting the markdown table rows in the
 * system agent's reply: "Returned ~37 rows" above typed coverage lines that
 * said 54 points, "~33 rows" above 72 points. A call with typed
 * `telemetry_coverage` lines now takes its line from them: how many reads
 * they report (one line is one read of one series over one window, and a
 * series read over two windows is two lines), counted from the lines and
 * never parsed from their text, with an honest "or more" at the server's
 * 16-line bound. */
import { describe, expect, it } from 'vitest';

import { buildInvNodeModal, telemetryReadsLine, type ArchNode, type ArchStore, type InvCallDetail } from '../lib/arch-store';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

const TOOL = 'telem-sitewise___query_telem_sitewise';
const SYS = 'system-telem-sitewise';

/** A markdown table of `rows` data rows (the system agent's reply shape). */
function table(rows: number): string {
  const body = Array.from({ length: rows }, (_, i) => `| 2026-10-27T06:${String(i).padStart(2, '0')}Z | ${i}.5 |`);
  return ['| time | value |', '|---|---|', ...body].join('\n');
}

function coverage(count: number): string[] {
  return Array.from(
    { length: count },
    (_, i) => `Telemetry read: CMX-441 / axis-${i}, 2026-10-27T05:30Z to 2026-10-27T06:30Z, 18 points`,
  );
}

const SUMMARY = `Vibration on CMX-441 over the last hour:\n\n${table(37)}`;

function runLive(store: ArchStore, lines: string[] | undefined, summary = SUMMARY): void {
  store.handleArchEvent('user_msg', { question: 'List every vibration reading on the CMX-441 from the last hour.' });
  store.handleArchEvent('tool_start', { tool: TOOL, tool_id: 't1', input: { question: 'vibration readings last hour' } });
  store.handleArchEvent('tool_result', {
    tool: TOOL,
    tool_id: 't1',
    summary,
    input: '{"raw": "result"}',
    elapsed_ms: 41_000,
    ...(lines ? { telemetry_coverage: lines } : {}),
  });
  store.handleArchEvent('done', {});
}

/** Every place the Architecture view shows this call's result line. */
function resultLines(store: ArchStore): { drilldown: string; legacy: string; nodeLabel: string; modal: string } {
  const nodes = store.getState().nodes;
  const sys = nodes.find((n) => n.id === SYS) as ArchNode;
  const call = (sys._calls as InvCallDetail[])[0];
  const finding = nodes.find((n) => n.type === 'finding' && (n._call as InvCallDetail | undefined)?.tool === TOOL)!;
  const modalLine = /\*\*Result:\*\* (.*)/.exec(buildInvNodeModal(sys))?.[1] ?? '';
  return {
    drilldown: String(call.resultEnglish),
    legacy: String((sys._results as string[])[0]),
    nodeLabel: String(finding.label).split('\n')[1],
    modal: modalLine,
  };
}

function allEqual(lines: ReturnType<typeof resultLines>, expected: string): void {
  expect(lines).toEqual({ drilldown: expected, legacy: expected, nodeLabel: expected, modal: expected });
}

describe('a telemetry call takes its result line from its typed coverage lines', () => {
  it('counts the reads the lines report, not the reply table rows', () => {
    // Reproduction: "Returned ~37 rows" above three lines of 18 points each.
    const store = createArchStore();
    runLive(store, coverage(3));
    allEqual(resultLines(store), '3 telemetry reads');
  });

  it('one line reads "1 telemetry read"', () => {
    const store = createArchStore();
    runLive(store, coverage(1));
    allEqual(resultLines(store), '1 telemetry read');
  });

  it('one series read over two windows is two reads, as the two lines below say', () => {
    const store = createArchStore();
    runLive(store, [
      'Telemetry read: CMX-441 / Vibration_X, 2026-10-27T05:30Z to 2026-10-27T05:35Z, 30 points',
      'Telemetry read: CMX-441 / Vibration_X, 2026-10-27T05:35Z to 2026-10-27T05:40Z, 30 points',
    ]);
    allEqual(resultLines(store), '2 telemetry reads');
  });

  it('at the server bound of 16 lines it says the call may have read more', () => {
    // Reproduction (at cap): MAX_COVERAGE_LINES keeps 16 lines per call.
    const store = createArchStore();
    runLive(store, coverage(16));
    allEqual(resultLines(store), '16 or more telemetry reads');
  });

  it('the line is counted from the lines, never read from their text', () => {
    expect(telemetryReadsLine(['Telemetry read: A / B, 900 points', 'not a coverage spelling at all'])).toBe(
      '2 telemetry reads',
    );
    expect(telemetryReadsLine([])).toBe('');
    expect(telemetryReadsLine(undefined)).toBe('');
  });
});

describe("guards: calls without typed lines keep today's text, and the outcome does not move", () => {
  it('an empty call with coverage lines keeps "No matching data" on every surface', () => {
    // The lines say 0 points; the call's own line still says no data came back.
    const store = createArchStore();
    runLive(
      store,
      ['Telemetry read: CMX-441 / Vibration_X, 2026-10-27T05:30Z to 2026-10-27T06:30Z, 0 points (no data)'],
      'No matching records for that window.',
    );
    const sys = store.getState().nodes.find((n) => n.id === SYS)!;
    const call = (sys._calls as InvCallDetail[])[0];
    expect(call.outcome).toBe('empty');
    expect(call.resultEnglish).toBe('No matching data');
    expect((sys._results as string[])[0]).toBe('No matching data');
    expect(/\*\*Result:\*\* (.*)/.exec(buildInvNodeModal(sys))?.[1]).toBe('No matching data');
    expect(store.getState().nodes.some((n) => String(n.label).includes('telemetry read'))).toBe(false);
  });

  it('a partial call with coverage lines keeps its summary line, not a reads count', () => {
    const store = createArchStore();
    runLive(store, coverage(3), `partial: stopped at the scan budget\n\n${table(37)}`);
    const sys = store.getState().nodes.find((n) => n.id === SYS)!;
    const call = (sys._calls as InvCallDetail[])[0];
    expect(call.outcome).toBe('partial');
    allEqual(resultLines(store), 'partial: stopped at the scan budget');
  });

  it('a successful no-data prose answer with 0-point lines keeps "No matching data" on every surface', () => {
    // The summary classifies as success (prose), its line says no data, and
    // so do the lines; nothing counts table rows, so nothing is replaced.
    const store = createArchStore();
    runLive(
      store,
      ['Telemetry read: CMX-441 / Vibration_X, 2026-10-27T05:30Z to 2026-10-27T06:30Z, 0 points (no data)'],
      'No telemetry data was found for that window.',
    );
    const sys = store.getState().nodes.find((n) => n.id === SYS)!;
    expect((sys._calls as InvCallDetail[])[0].outcome).toBe('success');
    allEqual(resultLines(store), 'No matching data');
  });

  it('a successful prose answer keeps its own line; only a line read past a reply table is replaced', () => {
    const store = createArchStore();
    runLive(store, coverage(2), 'The average vibration over the hour was 3.2 mm/s across both axes.');
    allEqual(resultLines(store), 'The average vibration over the hour was 3.2 mm/s across both axes.');
  });

  it('readsPastTable is true only where the line reads past a reply table', () => {
    const g = globalThis as unknown as {
      readsPastTable: (tool: string, summary: string) => boolean;
      describeToolResult: (tool: string, summary: string, requestText: string) => string;
    };
    const past: Array<[string, string]> = [
      [TOOL, SUMMARY],
      [TOOL, '| a | b |\n|---|---|\n| 1 | 2 |'],
      // A graph result has no count line either, so its line reads past a table.
      ['digital-thread___explore_graph', '| a |\n|---|\n| 1 |'],
    ];
    const not: Array<[string, string]> = [
      [TOOL, 'No telemetry data was found for that window.'],
      [TOOL, 'No matching records found.'],
      [TOOL, 'The average vibration over the hour was 3.2 mm/s.'],
      [TOOL, '| a |\n|---|'],
      [TOOL, '12 records found\n| a |\n|---|\n| 1 |'],
      ['digital-thread___explore_graph', 'Error: Graph query failed\n| a |\n|---|\n| 1 |'],
      ['digital-thread___save_correlation', '| a |\n|---|\n| 1 |'],
      [TOOL, ''],
    ];
    for (const [tool, summary] of past) {
      expect(g.readsPastTable(tool, summary)).toBe(true);
      // The line it replaces is never a count of the table's rows.
      expect(g.describeToolResult(tool, summary, 'readings')).not.toMatch(/^Returned ~/);
    }
    for (const [tool, summary] of not) expect(g.readsPastTable(tool, summary)).toBe(false);
  });

  it('a table reply without coverage lines reads its own first sentence, never a count of the table rows', () => {
    const store = createArchStore();
    runLive(store, undefined);
    allEqual(resultLines(store), 'Vibration on CMX-441 over the last hour:');
  });

  it('the outcome is classified from the summary alone, lines or not', () => {
    const withLines = createArchStore();
    runLive(withLines, coverage(3));
    const without = createArchStore();
    runLive(without, undefined);
    const outcomeOf = (s: ArchStore) => ((s.getState().nodes.find((n) => n.id === SYS)!._calls as InvCallDetail[])[0]).outcome;
    expect(outcomeOf(withLines)).toBe('success');
    expect(outcomeOf(without)).toBe('success');
  });

  it('a failed call with coverage lines keeps its failure rendering and claims no reads', () => {
    const store = createArchStore();
    runLive(store, coverage(3), 'Error executing query: table line_events was not found.');
    const s = store.getState();
    const sys = s.nodes.find((n) => n.id === SYS)!;
    expect((sys._calls as InvCallDetail[] | undefined) ?? []).toHaveLength(0);
    const err = s.nodes.find((n) => n.outcome === 'failed')!;
    expect(err.label).toBe('failed');
    expect((err._call as InvCallDetail).outcome).toBe('failed');
    expect((err._call as InvCallDetail).resultEnglish ?? '').not.toContain('telemetry read');
  });
});
