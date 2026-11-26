// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests — KG question labels (src/lib/kg-label.ts) and the
 * investigation-graph overlap sweep (src/views/architecture/inv-graph.ts
 * sweepRowPositions). Inputs mirror live tool_start events captured from a
 * deployed /api/ask stream (see the SSE trace in the Part-A investigation). */
import { describe, expect, it } from 'vitest';

import {
  getSystemLabel,
  getToolLabel as getChatToolLabel,
} from '../chat/tool-labels';
import { isExploreGraphTool, kgPillLabel, kgQuestionText } from '../lib/kg-label';
import { sweepRowPositions } from '../views/architecture/inv-graph';

describe('isExploreGraphTool', () => {
  it('matches local and gateway shapes', () => {
    expect(isExploreGraphTool('explore_graph')).toBe(true);
    expect(isExploreGraphTool('digital-thread___explore_graph')).toBe(true);
  });
  it('rejects system tools and junk', () => {
    expect(isExploreGraphTool('lake-athena___query_lake_athena')).toBe(false);
    expect(isExploreGraphTool('query_erp')).toBe(false);
    expect(isExploreGraphTool('')).toBe(false);
    expect(isExploreGraphTool(undefined)).toBe(false);
  });
});

describe('chat tool identity presentation', () => {
  it('keeps graph-node and HUD labels distinct over the shared parser', () => {
    // Both name the system the same way (shared resolver); the HUD label adds the operation.
    expect(getSystemLabel('lake-athena___athena_run_query')).toBe('Lake Athena');
    expect(getChatToolLabel('lake-athena___athena_run_query')).toBe(
      'Lake Athena — Athena Run Query',
    );
    expect(getSystemLabel('query_mes_cedar_rapids')).toBe('MES Cedar Rapids');
  });
});

describe('kgQuestionText', () => {
  it('englishizes a live-captured cypher input', () => {
    // Verbatim input from a live digital-thread___explore_graph tool_start.
    const input = {
      cypher:
        'MATCH (s:System) RETURN s.system_id, s.name, s.capabilities, s.plant, s.connection_type, s.query_guidance LIMIT 20',
    };
    expect(kgQuestionText('digital-thread___explore_graph', input)).toBe('System node');
  });

  it('includes the name filter from the cypher', () => {
    const input = { cypher: "MATCH (a:Asset) WHERE a.name CONTAINS 'FTW7' RETURN a" };
    const q = kgQuestionText('digital-thread___explore_graph', input);
    expect(q).toContain('Asset node');
    expect(q).toContain('FTW7');
  });

  it('prefers an explicit natural-language field over cypher', () => {
    const input = {
      question: 'what joins mes.cell to sitewise sites?',
      cypher: 'MATCH (c:Concept) RETURN c',
    };
    expect(kgQuestionText('explore_graph', input)).toBe('what joins mes.cell to sitewise sites?');
  });

  it('phrases save_* writes via describeToolInput', () => {
    const input = { from_system: 'mes', to_system: 'sitewise', concept: 'site' };
    expect(kgQuestionText('digital-thread___save_value_mapping', input)).toBe(
      'Saving learned mapping: mes → sitewise (site)',
    );
  });

  it('returns empty for underivable input', () => {
    expect(kgQuestionText('explore_graph', null)).toBe('');
    expect(kgQuestionText('explore_graph', {})).toBe('');
  });

  it('truncates long questions to ~90 chars', () => {
    const long = 'x'.repeat(200);
    const q = kgQuestionText('explore_graph', { question: long });
    expect(q.length).toBeLessThanOrEqual(91); // 90 + ellipsis
    expect(q.endsWith('…')).toBe(true);
  });
});

describe('kgPillLabel', () => {
  it('prefixes the derived question with KG:', () => {
    expect(
      kgPillLabel('digital-thread___explore_graph', {
        cypher: 'MATCH (s:System) RETURN s LIMIT 20',
      }),
    ).toBe('KG: System node');
  });
  it('falls back to the original generic label', () => {
    expect(kgPillLabel('explore_graph', {})).toBe('Querying knowledge graph');
  });
});

describe('sweepRowPositions', () => {
  it('leaves non-overlapping rows untouched', () => {
    const items = [
      { x: 100, w: 120 },
      { x: 400, w: 120 },
    ];
    expect(sweepRowPositions(items)).toEqual([100, 400]);
  });

  it('pushes overlapping nodes apart to at least width+margin', () => {
    const items = [
      { x: 100, w: 160 },
      { x: 120, w: 160 }, // fully overlapping the first
    ];
    const xs = sweepRowPositions(items, 40);
    expect(xs[1] - xs[0]).toBeGreaterThanOrEqual(160 + 40);
  });

  it('re-centers the row around the original midpoint', () => {
    const items = [
      { x: 100, w: 200 },
      { x: 110, w: 200 },
      { x: 120, w: 200 },
    ];
    const xs = sweepRowPositions(items, 40);
    const midBefore = (items[0].x + items[2].x) / 2;
    const midAfter = (xs[0] + xs[2]) / 2;
    expect(Math.abs(midAfter - midBefore)).toBeLessThan(0.001);
    // Order preserved and every adjacent pair separated.
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
    expect(xs[1] - xs[0]).toBeGreaterThanOrEqual(240);
    expect(xs[2] - xs[1]).toBeGreaterThanOrEqual(240);
  });

  it('handles single-node and empty rows', () => {
    expect(sweepRowPositions([{ x: 50, w: 100 }])).toEqual([50]);
    expect(sweepRowPositions([])).toEqual([]);
  });
});
