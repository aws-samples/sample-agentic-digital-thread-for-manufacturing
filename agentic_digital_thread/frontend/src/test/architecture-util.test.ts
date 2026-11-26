// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Vitest port of tests/unit/test_architecture_util.py.
 *
 * The helpers live in src/lib/architecture-util.js (a CJS-compatible IIFE
 * retained from the removed legacy bundle). We load it through
 * a CommonJS-emulating shim so the `module.exports` path is exercised exactly
 * like the python test's node subprocess (`require(...)`), independent of the
 * package.json "type" field or the vitest environment.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const UTIL_JS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../lib/architecture-util.js',
);

const source = readFileSync(UTIL_JS, 'utf-8');

/** Emulate `require()` of the verbatim CJS-compatible IIFE. */
function loadCjs(): Record<string, any> {
  const mod = { exports: {} as Record<string, any> };
  // eslint-disable-next-line no-new-func
  new Function('module', 'exports', source)(mod, mod.exports);
  return mod.exports;
}

const m = loadCjs();

const run = (toolName: unknown): string | null => m.mapToolToSystemId(toolName);

// --- Module surface ----------------------------------------------------------

describe('module surface', () => {
  it('helper is exported and node-requirable', () => {
    expect(source).toContain('mapToolToSystemId');
    expect(source).toContain('module.exports');
    expect(typeof m.mapToolToSystemId).toBe('function');
  });
});

// --- Parsing contract ----------------------------------------------------------

describe('mapToolToSystemId', () => {
  it('gateway format returns system id prefix', () => {
    // The canonical gateway tool shape: "<system_id>___<tool>".
    expect(run('a___b')).toBe('a');
    expect(run('lake-athena___athena_run_query')).toBe('lake-athena');
    expect(run('telem-sitewise___sitewise_get_asset_property_value')).toBe('telem-sitewise');
  });

  it('gateway format with legacy query tool suffix', () => {
    // A gateway name whose tool half happens to be a legacy "query_*" verb —
    // the split on "___" still wins and yields the system id prefix.
    expect(run('mes-mil___query_mes_mil')).toBe('mes-mil');
  });

  it('explore_graph is the hub, not a leaf', () => {
    expect(run('explore_graph')).toBeNull();
  });

  it('legacy query_ prefix is dash-cased', () => {
    expect(run('query_mes_mil')).toBe('mes-mil');
    expect(run('query_sap_erp_indy')).toBe('sap-erp-indy');
  });

  it('non-system tools and empty return null', () => {
    expect(run('get_system_schema')).toBeNull();
    expect(run('')).toBeNull();
    expect(run(null)).toBeNull();
  });
});

// --- Investigation-graph NL helpers: describeToolInput / describeToolResult ---
// These pure helpers (appended alongside mapToolToSystemId) turn a tool call's
// request/result into short plain-English descriptions for the reasoning-trace
// graph — no raw Cypher/SQL, no LLM.

describe('module surface — NL helpers', () => {
  it('new helpers are exported additively', () => {
    expect(source).toContain('describeToolInput');
    expect(source).toContain('describeToolResult');
    // Additive export augmentation must not clobber the mapToolToSystemId export.
    expect(source).toContain('module.exports.describeToolInput');
    expect(source).toContain('module.exports = { mapToolToSystemId: mapToolToSystemId }');
    expect(typeof m.plainSummaryText).toBe('function');
    expect(typeof m.readsPastTable).toBe('function');
  });
});

describe('describeToolInput', () => {
  const describeInput = (tool: string, input: unknown): string =>
    m.describeToolInput(tool, input);

  it('mode A question passes through', () => {
    const q = 'How many work orders failed final inspection yesterday?';
    expect(describeInput('mes-mil___query_mes_mil', { question: q })).toBe(q);
  });

  it('long question is truncated', () => {
    const q = 'word '.repeat(60); // ~300 chars
    const out = describeInput('erp___query_erp', { question: q });
    expect(out.endsWith('…')).toBe(true);
    expect(out.length).toBeLessThanOrEqual(122);
  });

  it('cypher is englishized, not raw', () => {
    const cy =
      "MATCH (s:System)-[:MAPS_TO_CONCEPT]->(c:Concept) WHERE c.name='availability' RETURN s LIMIT 20";
    const out = describeInput('digital-thread___explore_graph', { cypher: cy });
    expect(out).not.toContain('MATCH'); // not raw Cypher
    expect(out).toContain('System');
    expect(out).toContain('Concept');
    expect(out).not.toContain('MAPS_TO_CONCEPT'); // relationship type excluded
    expect(out).toContain('availability');
  });

  it('save_value_mapping phrasing', () => {
    const out = describeInput('digital-thread___save_value_mapping', {
      from_system: 'erp',
      to_system: 'mes-mil',
      concept: 'order_status',
    });
    expect(out).toBe('Saving learned mapping: erp → mes-mil (order_status)');
  });

  it('save_correlation phrasing', () => {
    const out = describeInput('digital-thread___save_correlation', {
      pattern: 'x',
      systems: ['erp', 'mes-mil'],
    });
    expect(out).toBe('Saving learned correlation: erp → mes-mil — “x”');
  });

  it('empty input returns empty string', () => {
    expect(describeInput('erp___query_erp', {})).toBe('');
  });

  it('an English system-agent request containing "return" is NOT cypher', () => {
    // Regression: query_* tool requests are English by contract — a bare
    // "return" must not read as knowledge-graph exploration.
    const q = 'List failed quality checks this week and return the count per site';
    const out = describeInput('lake-athena___query_lake_athena', { question: q });
    expect(out).toBe(q);
    expect(out).not.toContain('knowledge graph');
  });

  it('a real MATCH cypher still englishizes without the explore_graph half', () => {
    const out = describeInput('graph_query', 'MATCH (s:System) RETURN s');
    expect(out).not.toContain('MATCH');
    expect(out).toContain('System');
    expect(out).toContain('knowledge graph');
  });

  it('the explore_graph half still englishizes even without MATCH', () => {
    const out = describeInput('digital-thread___explore_graph', { question: 'RETURN 1' });
    expect(out).toBe('Explored the knowledge graph');
  });
});

describe('describeToolResult', () => {
  const describeResult = (tool: string, summary: string, requestText: string, resultPayload?: unknown): string =>
    m.describeToolResult(tool, summary, requestText, resultPayload);

  // A markdown table in a reply is the reply's own layout: its rows are not a
  // count of what the call returned, and the request's words do not say what
  // a row is. No count is read from it; the line reads past the table.
  it('a reply table gives no count, and the request names no noun for one', () => {
    const md = 'Three work orders matched.\n\n| id | status |\n|---|---|\n| 1 | ok |\n| 2 | bad |\n| 3 | ok |';
    const out = describeResult('mes-mil___query_mes_mil', md, 'how many work orders');
    expect(out).toBe('Three work orders matched.');
  });

  it('two tables are not added into one count', () => {
    // Reproduction (walk 2026-10-28, ERP Request 2): a 3-row table of batches
    // and a 1-row total table read "Returned ~5 shipments".
    const md = [
      'The 47 shipment line items came from 3 distinct production batches.',
      '',
      '| batch | line items |',
      '|---|---|',
      '| RUN-A | 1 |',
      '| RUN-B | 1 |',
      '| RUN-C | 45 |',
      '',
      '| total line items |',
      '|---|',
      '| 47 |',
    ].join('\n');
    const out = describeResult('erp___query_erp', md, 'List the shipments of part 7742 in the last 7 days');
    expect(out).toBe('The 47 shipment line items came from 3 distinct production batches.');
    expect(out).not.toMatch(/Returned ~/);
  });

  it('a reply that is only a table has no line', () => {
    expect(describeResult('erp___query_erp', '| id |\n|---|\n| 1 |\n| 2 |', 'list the orders')).toBe('');
  });

  it('a typed count stays: the records-found summary the server builds from the result rows', () => {
    expect(describeResult('lake___athena_run_query', '12 records found: A, B, C, D...', '')).toBe('Returned 12 records');
  });

  it('no matching data', () => {
    expect(describeResult('erp___query_erp', 'No matching records found.', '')).toBe(
      'No matching data',
    );
  });

  // Nothing typed on a knowledge-graph result says how many entities came
  // back: the summary shows at most 8 lines, a row can be a scalar, and the
  // routing step is prose. So its line is the first line as written, with no
  // count, as for every other result without a typed count.
  it('explore_graph gives no count from its summary lines', () => {
    const summary = 'System A — foo\nSystem B — bar\nConcept C — baz';
    const out = describeResult('digital-thread___explore_graph', summary, '');
    expect(out).toBe('System A — foo');
    expect(out).not.toMatch(/Found \d/);
  });

  it('the knowledge-graph routing step reads its first sentence, not a count of its lines', () => {
    const seed =
      'Looked up this question in the knowledge graph before asking any system.\n' +
      '- Connected systems: System A, System B\n' +
      '- Shared concepts linking them: lot, order';
    expect(describeResult('explore_graph', seed, '')).toBe(
      'Looked up this question in the knowledge graph before asking any system.',
    );
  });

  it('a cut entity list does not read as the lines it shows', () => {
    const shown = Array.from({ length: 8 }, (_, i) => `Concept: c${i + 1} (domain)`);
    const summary = shown.join('\n') + ' (+12 more)';
    const out = describeResult('digital-thread___explore_graph', summary, '');
    expect(out).toBe('Concept: c1 (domain)');
    expect(out).not.toMatch(/\b8\b|Found/);
  });

  it('a scalar graph row is not counted as an entity', () => {
    expect(describeResult('explore_graph', '42', '')).toBe('42');
    expect(describeResult('explore_graph', 'No matching entities found.', '')).toBe('No matching data');
  });

  it('explore_graph failure reads as failed, never as entities or no data', () => {
    const long =
      'Error: Graph query failed (ValidationException): All sub queries in an UNION must have the same column names (line 2, column 1';
    expect(describeResult('explore_graph', long, '')).toBe('Graph query failed');
    expect(describeResult('digital-thread___explore_graph', 'Error: no such label found', '')).toBe(
      'Graph query failed',
    );
  });

  it('save is logged', () => {
    const out = describeResult(
      'digital-thread___save_value_mapping',
      'Result with 3 fields: status, mapping_id, type',
      '',
      { status: 'logged', mapping_id: 'mapping-123456789', type: 'value_mapping' },
    );
    expect(out).toBe('Saved mapping mapping-…');
  });

  it('reports learning-gate filtering from the raw result status', () => {
    const out = describeResult(
      'digital-thread___save_correlation',
      'Result with 2 fields: status, reason',
      '',
      { status: 'dropped', reason: 'duplicate evidence' },
    );
    expect(out).toBe('Save filtered by learning gates: duplicate evidence');
  });

  it('empty summary returns empty string', () => {
    expect(describeResult('erp___query_erp', '', '')).toBe('');
  });
});

// The prose line is the first line of real content, as plain text: markdown
// structure (a heading, a rule, a table row, a fence and what it holds, a line
// that is one bold span) never becomes the line, and inline syntax is removed.
describe('describeToolResult prose line never shows markdown syntax', () => {
  const prose = (summary: string): string => m.describeToolResult('plant-ops___query_plant_ops', summary, '');

  it('skips a heading-only line', () => {
    expect(prose('## Result\n\nThe average cycle time for the line was 41.2 seconds across the shift.\n\nMore detail.')).toBe(
      'The average cycle time for the line was 41.2 seconds across the shift.',
    );
  });

  it('skips a line that is one bold span, with or without a colon', () => {
    expect(prose('**Result:**\n\nThree orders shipped after their promised date this week.')).toBe(
      'Three orders shipped after their promised date this week.',
    );
    expect(prose('**Summary**\n\nThree orders shipped after their promised date this week.')).toBe(
      'Three orders shipped after their promised date this week.',
    );
  });

  it('skips a fence and the lines inside it', () => {
    expect(prose('```json\n{"orders": 1}\n```\nThe query returned one order object for the requested window.')).toBe(
      'The query returned one order object for the requested window.',
    );
  });

  it('skips table rows that are not counted and rules', () => {
    expect(prose('| a | b |\n|---|---|\n\n---\n\nThe header was listed without any rows under it in this reply.')).toBe(
      'The header was listed without any rows under it in this reply.',
    );
  });

  it('removes emphasis, inline code, links, quote and list markers', () => {
    expect(prose('- **Total:** 42 *open* orders and `3` on hold, see [the report](https://example.invalid/r) for the rest')).toBe(
      'Total: 42 open orders and 3 on hold, see the report for the rest',
    );
    expect(prose('> _Note_: the press reported its last cycle at shift change and nothing since then')).toBe(
      'Note: the press reported its last cycle at shift change and nothing since then',
    );
  });

  it('keeps underscores inside names', () => {
    expect(prose('Column run_number was read from table prod_runs; tool ops___query_ops ran twice today.')).toBe(
      'Column run_number was read from table prod_runs; tool ops___query_ops ran twice today.',
    );
  });

  it('keeps a bold line that is the result itself, without its markers', () => {
    expect(prose('**42 orders shipped late.**')).toBe('42 orders shipped late.');
    expect(prose('**Average cycle time: 41.2 seconds**\nMore detail follows.')).toBe('Average cycle time: 41.2 seconds');
  });

  it('keeps a one-word bold answer or status, and skips only a section label', () => {
    expect(prose('**Yes**')).toBe('Yes');
    expect(prose('**Failed**\nThe last step did not finish.')).toBe('Failed');
    expect(prose('**Results**\n\nThree orders shipped after their promised date this week.')).toBe(
      'Three orders shipped after their promised date this week.',
    );
  });

  it('keeps tildes that are literal inside a code span', () => {
    expect(prose('Current token: `~~NOT_A_VALUE~~` is what the reader holds')).toBe(
      'Current token: ~~NOT_A_VALUE~~ is what the reader holds',
    );
    expect(prose('**`3` orders are on hold** until the next shift starts')).toBe('3 orders are on hold until the next shift starts');
    expect(prose('Token  `A  B`   matched the key the reader sent')).toBe('Token A  B matched the key the reader sent');
  });

  it('drops struck-through text instead of showing it as current', () => {
    expect(prose('~~42 open orders~~ 17 open orders remain on the board today')).toBe(
      '17 open orders remain on the board today',
    );
    expect(prose('~~42 open orders~~\n17 open orders remain on the board today')).toBe(
      '17 open orders remain on the board today',
    );
  });

  it('is empty when the summary holds only markdown structure', () => {
    expect(prose('## Result')).toBe('');
    expect(prose('## Result\n\n| a |\n|---|')).toBe('');
  });

  it('reads letters and numbers in any script as content', () => {
    // Review: a line was content only when it held an ASCII letter or digit,
    // so a reply in Japanese or Arabic had no line at all.
    expect(prose('検査結果は正常です。')).toBe('検査結果は正常です。');
    expect(prose('نتيجة الفحص طبيعية.')).toBe('نتيجة الفحص طبيعية.');
    expect(prose('- **第一項**\n- 第二項')).toBe('第一項');
  });
});

// plainSummaryText: a summary's content as plain text, one line per content
// line, for surfaces that show more than the Result line: the graph node
// tooltip, and a label that joins lines into one. It keeps what the Result
// line skips: a table's cells (by the GFM table grammar the drill-down
// renders with) and fenced code as written.
describe('plainSummaryText', () => {
  const plain = (text: unknown): string => m.plainSummaryText(text);

  it('removes markdown from every content line, and keeps table cells and code as text', () => {
    // Reproduction (walk 2026-10-28): the data node's tooltip showed this
    // reply's raw markdown, "... there are **no vibration ... / - *".
    const reply = [
      'Based on the query results, there are **no vibration sensor readings available for the asset** in that window.',
      '',
      'The system indicates that:',
      '- **Requested window:** 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z',
      '- *Data available from:* 2026-10-24T17:10:10Z',
      '',
      '## Telemetry read',
      '',
      '| property | points |',
      '|---|---|',
      '| Vibration_X | 0 |',
      '```',
      'raw',
      '```',
    ].join('\n');
    expect(plain(reply)).toBe(
      [
        'Based on the query results, there are no vibration sensor readings available for the asset in that window.',
        'The system indicates that:',
        'Requested window: 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z',
        'Data available from: 2026-10-24T17:10:10Z',
        'property · points',
        'Vibration_X · 0',
        'raw',
      ].join('\n'),
    );
  });

  it('drops the list marker of every line, so lines joined into one carry none mid-line', () => {
    const seed =
      'Looked up this question in the knowledge graph before asking any system.\n' +
      '- Connected systems: Kiln Log, Order Book\n' +
      '1. Shared concepts linking them: lot';
    expect(plain(seed).replace(/\n/g, ' ')).toBe(
      'Looked up this question in the knowledge graph before asking any system. ' +
        'Connected systems: Kiln Log, Order Book Shared concepts linking them: lot',
    );
  });

  it('keeps plain text as it is, and gives "" for structure only or no text', () => {
    expect(plain('Concept — identity')).toBe('Concept — identity');
    expect(plain('KG: which systems hold lot K-4471?\nKG: how do they join?')).toBe(
      'KG: which systems hold lot K-4471?\nKG: how do they join?',
    );
    expect(plain('## Result\n\n---\n\n**Summary**')).toBe('');
    expect(plain('')).toBe('');
    expect(plain(null)).toBe('');
  });

  it('reads letters and numbers in any script as content', () => {
    // Review: only an ASCII letter or digit made a line content, so these
    // gave "" and the tooltip and the Knowledge Graph label lost them.
    expect(plain('検査結果は正常です。')).toBe('検査結果は正常です。');
    expect(plain('نتيجة الفحص طبيعية.')).toBe('نتيجة الفحص طبيعية.');
    expect(plain('- 第一項\n- 第二項')).toBe('第一項\n第二項');
  });

  it('keeps a table-only result as its cells, leaving out only the delimiter row', () => {
    // Review: every table row was dropped, so a table-only result had no text.
    const table = [
      '| batch | line items |',
      '|:---|---:|',
      '| B-1 | **1** |',
      '| a \\| b | 45 | extra cell |',
      '| --- | --- |',
      '| B-3 |',
    ].join('\n');
    expect(plain(table)).toBe(['batch · line items', 'B-1 · 1', 'a | b · 45', 'B-3'].join('\n'));
    expect(plain('The result:\n| a | b |\n|---|---|\n| 1 | 2 |\n\nEnd of list.')).toBe(
      'The result:\na · b\n1 · 2\nEnd of list.',
    );
  });

  it('keeps a code-only result as its code, written as it is', () => {
    // Review: every fenced line was dropped, so a code-only result had no text.
    const code = [
      '```sql',
      'SELECT run_id, COUNT(*) FROM runs -- **as written**',
      '| a | b |',
      '|---|---|',
      '~~~',
      '```',
    ].join('\n');
    expect(plain(code)).toBe(['SELECT run_id, COUNT(*) FROM runs -- **as written**', '| a | b |', '|---|---|', '~~~'].join('\n'));
    expect(plain('~~~\nline one\n```\n~~~\nAfter the block.')).toBe('line one\n```\nAfter the block.');
  });

  it('reads pipes in prose as prose: a line is a table row only under a delimiter row', () => {
    // Review: any line with two pipes was read as a table row and dropped.
    expect(plain("Filter: status = 'open' || status = 'held'\nThe filter matched 3 orders.")).toBe(
      "Filter: status = 'open' || status = 'held'\nThe filter matched 3 orders.",
    );
    expect(plain('a | b | c\nd | e | f')).toBe('a | b | c\nd | e | f');
    // A head row whose cell count is not the delimiter row's is no table.
    expect(plain('| a | b |\n|---|\n| 1 |')).toBe('| a | b |\n| 1 |');
  });

  it('recognizes a table without outer pipes', () => {
    // Review: a valid table without outer pipes kept its raw "|" separators.
    expect(plain('property | points\n--- | ---:\nVibration_X | 0')).toBe('property · points\nVibration_X · 0');
  });
});
