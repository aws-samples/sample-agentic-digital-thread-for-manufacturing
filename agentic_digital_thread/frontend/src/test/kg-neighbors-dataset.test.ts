// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — neptune-graph neighborsToDataset (UI improvement D wiring).
 *
 * Pins (each names the mutation it kills):
 * 1. Edge direction: outgoing maps center→target, incoming maps
 *    source→center — kills flipping either direction (which would corrupt
 *    the neighborhood the legend now counts).
 * 2. Node dedup: a neighbor appearing in both directions (or twice) yields
 *    ONE node; the center is never duplicated — kills duplicate nodes
 *    inflating the legend counts.
 * 3. Edge dedup: identical source/label/target pairs collapse to one edge —
 *    renderNeptuneGraph mints deterministic edge ids, so duplicates would
 *    crash the cytoscape add.
 * 4. Center node carries the REAL properties (name resolution props.name →
 *    props.id → nodeId) — kills dropping the center's props/tooltip. */
// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

// cytoscape is a module-boundary dependency of neptune-graph (jsdom has no
// canvas) — neighborsToDataset itself never touches it.
vi.mock('cytoscape', () => ({ default: Object.assign(vi.fn(), { use: vi.fn() }) }));

import { neighborsToDataset } from '../views/architecture/neptune-graph';

const PAYLOAD = {
  node: { labels: ['Concept'], properties: { name: 'work order', domain: 'ops' } },
  outgoing: [
    { target_id: 'n2', target_name: 'ERP', target_label: 'System', relationship: 'LINKS_CONCEPT' },
    // Duplicate of the exact same edge — must collapse.
    { target_id: 'n2', target_name: 'ERP', target_label: 'System', relationship: 'LINKS_CONCEPT' },
  ],
  incoming: [
    // The SAME neighbor also points back at the center — node must dedup,
    // but this distinctly-directed edge must survive.
    { source_id: 'n2', source_name: 'ERP', source_label: 'System', relationship: 'HAS_CONCEPT' },
    { source_id: 'n3', source_name: 'MES', source_label: 'System', relationship: 'HAS_CONCEPT' },
  ],
};

describe('neighborsToDataset', () => {
  it('maps direction correctly and dedups nodes and edges', () => {
    const ds = neighborsToDataset('c1', PAYLOAD);

    // Nodes: center + 2 unique neighbors (n2 deduped across directions).
    expect(ds.nodes.map((n) => n.id)).toEqual(['c1', 'n2', 'n3']);
    // Center carries the real properties and the resolved name.
    expect(ds.nodes[0]).toMatchObject({
      id: 'c1',
      label: 'work order',
      type: 'Concept',
      props: { name: 'work order', domain: 'ops' },
    });

    // Edges: duplicate outgoing collapsed; directions preserved.
    expect(ds.edges).toEqual([
      { source: 'c1', target: 'n2', label: 'LINKS_CONCEPT' }, // outgoing: center→target
      { source: 'n2', target: 'c1', label: 'HAS_CONCEPT' }, // incoming: source→center
      { source: 'n3', target: 'c1', label: 'HAS_CONCEPT' },
    ]);
  });

  it('falls back to props.id then nodeId for the center label', () => {
    const noName = neighborsToDataset('x9', {
      node: { labels: ['Asset'], properties: { id: 'asset-7' } },
    });
    expect(noName.nodes[0].label).toBe('asset-7');

    const bare = neighborsToDataset('x9', { node: { labels: [] } });
    expect(bare.nodes[0]).toMatchObject({ id: 'x9', label: 'x9', type: 'Unknown' });
  });
});
