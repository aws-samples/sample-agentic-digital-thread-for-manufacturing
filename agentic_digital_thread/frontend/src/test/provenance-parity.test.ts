// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* D2 ordinal-parity fixture — the FRONTEND half of the pinned parity pair
 * (frozen contract D2). Mirrors the Python fixture in
 * tests/unit/test_provenance_builder.py::test_parity_fixture_yields_records_1_to_4
 * EXACTLY: bare `explore_graph` is NOT numbered; a GATEWAY-PREFIXED graph
 * name (`digital-thread___explore_graph`) IS; so is `get_system_schema`;
 * expected ordinals are 1..4 in call order. The counting predicate, the
 * pill→ordinal walk (citationToolId), and the full dual-binding recipe
 * (boundRecordFor) all live in ONE frontend home (chat/provenance.ts),
 * consumed by the thread-store numbering, the chips map, and
 * thread-clicks — this file pins that they all agree with the server's
 * `citation_ordinals`. */
// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import {
  boundRecordFor,
  citationToolId,
  isCitedTool,
  type ProvenanceRecord,
} from '../chat/provenance';
import { createThreadStore } from '../chat/thread-store';

/** The pinned parity calls — same names/ids as the Python `_PARITY_CALLS`. */
const PARITY_PILLS = [
  { tool: 'explore_graph', toolId: 't-graph' }, //           NOT cited (bare)
  { tool: 'digital-thread___explore_graph', toolId: 't-gwgraph' }, // cited: 1
  { tool: 'get_system_schema', toolId: 't-schema' }, //              cited: 2
  { tool: 'sysa___query_sysa', toolId: 't-q1' }, //                  cited: 3
  { tool: 'sysb___query_sysb', toolId: 't-q2' }, //                  cited: 4
];

/** The expected (toolId, ordinal) bindings — the CITED pills only, in call
 * order (the bare graph pill never earns a number). */
const CITED_PAIRS: ReadonlyArray<[toolId: string, num: number]> = [
  ['t-gwgraph', 1],
  ['t-schema', 2],
  ['t-q1', 3],
  ['t-q2', 4],
];

/** Explicit ordinal → toolId lookup (survives fixture edits — no reliance
 * on PARITY_PILLS index offsets). */
const CITED_TOOL_ID_BY_NUM = new Map(CITED_PAIRS.map(([toolId, num]) => [num, toolId]));

/** One sparse D1 record per expected ordinal (mirrors the Python payload's
 * `[1, 2, 3, 4]` / `[t-gwgraph, t-schema, t-q1, t-q2]` assertions). */
const PARITY_RECORDS: ProvenanceRecord[] = CITED_PAIRS.map(([toolId, num]) => ({
  citation_num: num,
  tool_id: toolId,
  source_system: '',
  captured_at: '2026-08-03T19:00:00Z',
  verification: 'unverified',
}));

describe('D2 parity fixture — frontend mirror of test_parity_fixture_yields_records_1_to_4', () => {
  it('isCitedTool matches the exact server predicate (is_client_cited)', () => {
    expect(isCitedTool('explore_graph')).toBe(false);
    // The gateway-prefixed spelling IS cited — the exact-string test, not
    // isExploreGraphTool (which would also match the prefix).
    expect(isCitedTool('digital-thread___explore_graph')).toBe(true);
    expect(isCitedTool('get_system_schema')).toBe(true);
  });

  it('citationToolId yields ordinals 1..4 over the parity pills', () => {
    expect([1, 2, 3, 4].map((n) => citationToolId(PARITY_PILLS, n))).toEqual([
      't-gwgraph',
      't-schema',
      't-q1',
      't-q2',
    ]);
    // The bare graph pill never consumes a number…
    expect(citationToolId(PARITY_PILLS, 5)).toBeNull();
    // …and never binds one.
    expect([1, 2, 3, 4].map((n) => citationToolId(PARITY_PILLS, n))).not.toContain('t-graph');
  });

  it('thread-store numbering cites the same calls with the same ordinals', () => {
    const store = createThreadStore();
    store.startTurn('parity');
    for (const p of PARITY_PILLS) {
      store.applyEvent('tool_start', { tool: p.tool, tool_id: p.toolId, input: {} });
    }
    const a = store.getState().turns[0].assistant;
    expect(a.citations.map((c) => c.num)).toEqual([1, 2, 3, 4]);
    expect(a.citations.map((c) => c.tool)).toEqual([
      'digital-thread___explore_graph',
      'get_system_schema',
      'sysa___query_sysa',
      'sysb___query_sysb',
    ]);
    // Chip binding parity: for every citation, the ONE production D2
    // dual-binding recipe (boundRecordFor — toolId primary via the pills,
    // ordinal fallback) lands on the record carrying the SAME ordinal the
    // Python builder assigns.
    for (const c of a.citations) {
      const bound = boundRecordFor(PARITY_RECORDS, a.pills.values(), c.num);
      expect(bound?.citation_num).toBe(c.num);
      expect(bound?.tool_id).toBe(CITED_TOOL_ID_BY_NUM.get(c.num));
    }
  });
});
