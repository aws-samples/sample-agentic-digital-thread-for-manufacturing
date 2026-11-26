// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Task 1G — the KG subtree renders ABOVE the Knowledge Graph node.
 *
 * `kgSubtreeY` is the pure band rule doLayout applies (inv-graph.ts) and
 * `inboundParentChain` is the pure topology walk that feeds it. Detection is
 * TOPOLOGY-ONLY (the `system-neptune` hub id + the inbound-edge parent chain,
 * never node types), so these assertions hold whether results attach to
 * per-query KG qinput nodes (post-1F store) or directly to the hub (pre-1F
 * store / the mint-on-miss fallback) — the parallel-safe 1F/1G contract. */
import { describe, expect, it } from 'vitest';

import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';
import {
  HUD_RESERVED_W,
  hudAwareFit,
  inboundParentChain,
  invNodeData,
  KG_BAND,
  KG_HUB_ID,
  kgSubtreeY,
  LABEL_FONT_FLOOR,
  labelFontSize,
} from '../views/architecture/inv-graph';

installArchStoreDisposal();

/** The tierY map doLayout uses (inv-graph.ts#doLayout). */
const tierY: Record<string, number> = {
  question: 340,
  neptune: 200,
  system: 490,
  qinput: 620,
  entity: 60,
  finding: 760,
};

/** Plan-spec topology: a neptune hub with 2 KG qinputs + their findings and
 * entities, and a system with its own qinput + finding. */
const edges = [
  { source: KG_HUB_ID, target: 'qinput-kg-1' },
  { source: KG_HUB_ID, target: 'qinput-kg-2' },
  { source: 'qinput-kg-1', target: 'finding-kg-1' },
  { source: 'qinput-kg-1', target: 'entity-Pump_A-q1' },
  { source: 'qinput-kg-2', target: 'finding-kgwrite-2' },
  { source: 'qinput-kg-2', target: 'entity-Line_2-q1' },
  { source: 'system-erp', target: 'qinput-erp-3' },
  { source: 'qinput-erp-3', target: 'finding-4' },
];

describe('kgSubtreeY — pure KG band rule (Task 1G)', () => {
  it('places the KG qinput fan one band ABOVE the neptune tier', () => {
    const y = kgSubtreeY([KG_HUB_ID], tierY, 'qinput');
    expect(y).toBe(tierY.neptune - KG_BAND); // 70
    expect(y as number).toBeLessThan(tierY.neptune);
  });

  it('places KG results one band above the KG qinput fan', () => {
    const y = kgSubtreeY(['qinput-kg-1', KG_HUB_ID], tierY, 'result');
    expect(y).toBe(tierY.neptune - 2 * KG_BAND); // -60
    expect(y as number).toBeLessThan(tierY.neptune);
  });

  it('results attached DIRECTLY to the hub (pre-1F store) land in the same result band', () => {
    expect(kgSubtreeY([KG_HUB_ID], tierY, 'result')).toBe(tierY.neptune - 2 * KG_BAND);
  });

  it('every KG-subtree y in the plan-spec topology is strictly less than tierY.neptune', () => {
    const kgNodes = [
      { id: 'qinput-kg-1', band: 'qinput' as const },
      { id: 'qinput-kg-2', band: 'qinput' as const },
      { id: 'finding-kg-1', band: 'result' as const },
      { id: 'finding-kgwrite-2', band: 'result' as const },
      { id: 'entity-Pump_A-q1', band: 'result' as const },
      { id: 'entity-Line_2-q1', band: 'result' as const },
    ];
    for (const { id, band } of kgNodes) {
      const y = kgSubtreeY(inboundParentChain(id, edges), tierY, band);
      expect(y, id).not.toBeNull();
      expect(Number.isFinite(y as number), id).toBe(true);
      expect(y as number, id).toBeLessThan(tierY.neptune);
    }
    // Upward stacking from the result band stays above the hub and finite.
    const base = kgSubtreeY(inboundParentChain('finding-kg-1', edges), tierY, 'result') as number;
    for (let i = 0; i < 3; i++) {
      const stacked = base - i * KG_BAND;
      expect(Number.isFinite(stacked)).toBe(true);
      expect(stacked).toBeLessThan(tierY.neptune);
    }
  });

  it('system-subtree nodes are NOT in the KG band — findings keep tierY.finding', () => {
    // doLayout falls back to the classic tier when kgSubtreeY returns null.
    expect(kgSubtreeY(inboundParentChain('qinput-erp-3', edges), tierY, 'qinput')).toBeNull();
    const kgY = kgSubtreeY(inboundParentChain('finding-4', edges), tierY, 'result');
    expect(kgY).toBeNull();
    const fallbackY = kgY != null ? kgY : tierY.finding;
    expect(fallbackY).toBe(tierY.finding);
  });

  it('missing parents return the fallback (null), never NaN', () => {
    expect(kgSubtreeY([], tierY)).toBeNull();
    expect(kgSubtreeY(inboundParentChain('orphan-node', edges), tierY, 'result')).toBeNull();
    // Even a degenerate tier map cannot produce NaN for a KG chain.
    const y = kgSubtreeY([KG_HUB_ID], {}, 'qinput');
    expect(Number.isFinite(y as number)).toBe(true);
  });
});

describe('inboundParentChain — pure topology walk', () => {
  it('walks the inbound-edge chain immediate-parent-first up to the hub', () => {
    expect(inboundParentChain('finding-kg-1', edges)).toEqual(['qinput-kg-1', KG_HUB_ID]);
    expect(inboundParentChain('qinput-kg-2', edges)).toEqual([KG_HUB_ID]);
    expect(inboundParentChain('finding-4', edges)).toEqual(['qinput-erp-3', 'system-erp']);
  });

  it('returns [] for nodes without inbound edges', () => {
    expect(inboundParentChain(KG_HUB_ID, edges)).toEqual([]);
    expect(inboundParentChain('orphan-node', edges)).toEqual([]);
  });

  it('terminates on cycles and respects the depth bound', () => {
    const cyclic = [
      { source: 'b', target: 'a' },
      { source: 'a', target: 'b' },
    ];
    expect(inboundParentChain('a', cyclic)).toEqual(['b']);
    const deep = Array.from({ length: 10 }, (_, i) => ({
      source: `n${i + 1}`,
      target: `n${i}`,
    }));
    expect(inboundParentChain('n0', deep, 4)).toHaveLength(4);
  });
});

describe('KG band rule against a real store snapshot (1F-agnostic)', () => {
  it('every finding/entity whose chain reaches the hub gets a band above tierY.neptune; system findings do not', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'How do the systems relate?' });
    store.handleArchEvent('tool_start', {
      tool: 'explore_graph',
      input: { query: 'systems related to pump A' },
    });
    store.handleArchEvent('tool_result', {
      tool: 'explore_graph',
      summary: 'System: sys-a\nSystem: sys-b',
    });
    store.handleArchEvent('tool_start', {
      tool: 'query_sys_a',
      input: { query: 'SELECT * FROM work_orders' },
    });
    store.handleArchEvent('tool_result', {
      tool: 'query_sys_a',
      summary: '3 records found\nwo-1, wo-2, wo-3',
      input: { query: 'SELECT * FROM work_orders' },
    });
    store.handleArchEvent('done', {});
    const state = store.getState();
    const storeEdges = state.edges.map((e) => ({ source: e.source, target: e.target }));

    const kgAttached = state.nodes.filter((n) =>
      inboundParentChain(n.id, storeEdges).includes(KG_HUB_ID),
    );
    // The KG turn must have produced at least one hub-attached node in BOTH
    // store generations (entity/finding pre-1F; qinput+result post-1F).
    expect(kgAttached.length).toBeGreaterThan(0);
    for (const n of kgAttached) {
      const chain = inboundParentChain(n.id, storeEdges);
      const band = chain[0] === KG_HUB_ID && n.type === 'qinput' ? 'qinput' : 'result';
      const y = kgSubtreeY(chain, tierY, band);
      expect(y, n.id).not.toBeNull();
      expect(y as number, n.id).toBeLessThan(tierY.neptune);
    }

    // The system-agent finding stays out of the KG band → bottom tier.
    const sysFindings = state.nodes.filter(
      (n) =>
        n.type === 'finding' && !inboundParentChain(n.id, storeEdges).includes(KG_HUB_ID),
    );
    expect(sysFindings.length).toBeGreaterThan(0);
    for (const n of sysFindings) {
      expect(kgSubtreeY(inboundParentChain(n.id, storeEdges), tierY, 'result')).toBeNull();
    }
  });
});

describe('label autofit — labelFontSize / invNodeData', () => {
  it('labels of ≤12 chars keep the per-type base size', () => {
    expect(labelFontSize('ERP', 'system')).toBe(15); // 3 chars
    expect(labelFontSize('111122223333', 'system')).toBe(15); // exactly 12
    expect(labelFontSize('Short', 'qinput')).toBe(12);
    expect(labelFontSize('Short', 'schema')).toBe(11);
    expect(labelFontSize('Short')).toBe(14); // unknown type → base node size
    expect(labelFontSize('')).toBe(14); // empty label → base
  });

  it('steps down 1px per 10 chars beyond 12', () => {
    expect(labelFontSize('1234567890123', 'system')).toBe(14); // 13 chars → base-1
    expect(labelFontSize('a'.repeat(22), 'system')).toBe(14); // 22 → still base-1
    expect(labelFontSize('a'.repeat(23), 'system')).toBe(13); // 23 → base-2
    expect(labelFontSize('a'.repeat(33), 'finding')).toBe(11); // 14 - 3
  });

  it('never shrinks below the floor', () => {
    expect(labelFontSize('a'.repeat(200), 'system')).toBe(LABEL_FONT_FLOOR);
    expect(labelFontSize('a'.repeat(200), 'schema')).toBe(LABEL_FONT_FLOOR);
    expect(LABEL_FONT_FLOOR).toBe(8);
  });

  it('whitespace does not count toward the length', () => {
    expect(labelFontSize('   ERP   ', 'system')).toBe(15);
  });

  it('invNodeData attaches the fontSize data attribute at node creation', () => {
    const n = {
      id: 'system-erp',
      type: 'system',
      label: 'MES Milwaukee ×3 — line state history',
      detail: 'd',
      questions: [1],
    };
    const data = invNodeData(n, { bg: '#111', border: '#222' });
    expect(data.fontSize).toBe(labelFontSize(n.label, n.type));
    expect(data.fontSize).toBeLessThan(15); // long label stepped down
    expect(data).toMatchObject({
      id: 'system-erp',
      type: 'system',
      label: n.label,
      bg: '#111',
      border: '#222',
      detail: 'd',
      questions: [1],
    });
  });

  it('every node in a real store snapshot gets a fontSize within [floor, base]', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'How do the systems relate?' });
    store.handleArchEvent('tool_start', { tool: 'query_sys_a', input: { query: 'x' } });
    store.handleArchEvent('tool_result', { tool: 'query_sys_a', summary: '3 records' });
    store.handleArchEvent('done', {});
    for (const n of store.getState().nodes) {
      const data = invNodeData(n, { bg: '#111', border: '#222' });
      expect(data.fontSize as number, n.id).toBeGreaterThanOrEqual(LABEL_FONT_FLOOR);
      expect(data.fontSize as number, n.id).toBeLessThanOrEqual(15);
    }
  });
});

describe('hudAwareFit — HUD-aware viewport fit (question-node left clipping)', () => {
  interface Bb {
    x1: number;
    y1: number;
    x2: number;
    y2: number;
    w: number;
    h: number;
  }
  const bb = (x1: number, y1: number, x2: number, y2: number): Bb => ({
    x1,
    y1,
    x2,
    y2,
    w: x2 - x1,
    h: y2 - y1,
  });

  /** Minimal fake cy — exposes exactly the surface hudAwareFit touches and
   * captures the zoom/pan/fit calls it makes (cytoscape defaults:
   * minZoom 1e-50, maxZoom 1e50). */
  function fakeCy(opts: { w?: number; h?: number; bb: Bb; minZoom?: number; maxZoom?: number }) {
    const calls = {
      zoom: [] as number[],
      pan: [] as { x: number; y: number }[],
      fit: [] as unknown[][],
    };
    const collection = { length: 1, boundingBox: () => opts.bb };
    const cy = {
      width: () => opts.w ?? 1000,
      height: () => opts.h ?? 700,
      minZoom: () => opts.minZoom ?? 1e-50,
      maxZoom: () => opts.maxZoom ?? 1e50,
      elements: () => collection,
      zoom: (z: number) => calls.zoom.push(z),
      pan: (p: { x: number; y: number }) => calls.pan.push(p),
      fit: (...a: unknown[]) => calls.fit.push(a),
    };
    return { cy, collection, calls };
  }

  /** rendered_x = model_x · zoom + pan.x */
  const renderedLeft = (b: Bb, zoom: number, pan: { x: number }) => b.x1 * zoom + pan.x;
  const renderedRight = (b: Bb, zoom: number, pan: { x: number }) => b.x2 * zoom + pan.x;

  const PAD = 40;
  const HUD = HUD_RESERVED_W;

  it('(a) wide graph: the focus bounding box\u2019s rendered left edge stays \u2265 0', () => {
    // Content much wider than the viewport — the old blind panBy(-110)
    // pushed the leftmost (question) node off-screen here.
    const b = bb(-500, 0, 1500, 400);
    const { cy, collection, calls } = fakeCy({ bb: b });
    hudAwareFit(cy, collection, PAD, HUD);
    expect(calls.zoom).toHaveLength(1);
    expect(calls.pan).toHaveLength(1);
    const [zoom] = calls.zoom;
    const [pan] = calls.pan;
    expect(renderedLeft(b, zoom, pan)).toBeGreaterThanOrEqual(0);
    // And the right edge stays inside the HUD-reduced band.
    expect(renderedRight(b, zoom, pan)).toBeLessThanOrEqual(1000 - HUD);
    // Width-limited fit against the REDUCED viewport, not the full one.
    expect(zoom).toBeCloseTo((1000 - HUD - 2 * PAD) / b.w, 10);
    expect(calls.fit).toHaveLength(0); // no plain-fit fallback taken
  });

  it('(b) narrow graph centers within the reduced band — not glued to the left', () => {
    const b = bb(400, 200, 600, 300); // small content around x=500
    const { cy, collection, calls } = fakeCy({ bb: b, maxZoom: 2 }); // zoom clamps below fill
    hudAwareFit(cy, collection, PAD, HUD);
    const [zoom] = calls.zoom;
    const [pan] = calls.pan;
    expect(zoom).toBe(2);
    const left = renderedLeft(b, zoom, pan);
    const right = renderedRight(b, zoom, pan);
    const bandW = 1000 - HUD;
    expect(left).toBeGreaterThan(0); // not pinned to the edge
    expect((left + right) / 2).toBeCloseTo(bandW / 2, 6); // centered in the band
  });

  it('(b2) null eles fits the whole graph via cy.elements()', () => {
    const b = bb(0, 0, 500, 300);
    const { cy, calls } = fakeCy({ bb: b });
    hudAwareFit(cy, null, PAD, HUD);
    const [zoom] = calls.zoom;
    const [pan] = calls.pan;
    expect(renderedLeft(b, zoom, pan)).toBeGreaterThanOrEqual(0);
    expect(renderedRight(b, zoom, pan)).toBeLessThanOrEqual(1000 - HUD);
  });

  it('(c) degenerate/empty bounding box falls back to a plain fit without throwing', () => {
    // Empty-collection bb: cytoscape reports zeros/infinities — zoom is
    // non-finite either way.
    const b = { x1: Infinity, y1: Infinity, x2: -Infinity, y2: -Infinity, w: 0, h: 0 };
    const { cy, collection, calls } = fakeCy({ bb: b as Bb });
    expect(() => hudAwareFit(cy, collection, PAD, HUD)).not.toThrow();
    expect(calls.fit).toEqual([[undefined, PAD]]); // plain fit, default pad 40
    expect(calls.zoom).toHaveLength(0); // no pan/zoom applied
    expect(calls.pan).toHaveLength(0);
  });

  it('(c2) a band smaller than the padding also falls back to a plain fit', () => {
    const b = bb(0, 0, 500, 300);
    const { cy, collection, calls } = fakeCy({ w: 150, bb: b }); // 150 - 110 - 80 < 0
    hudAwareFit(cy, collection, PAD, HUD);
    expect(calls.fit).toEqual([[undefined, PAD]]);
    expect(calls.zoom).toHaveLength(0);
  });

  it('(d) zoom respects the maxZoom clamp', () => {
    const b = bb(0, 0, 2000, 400); // unclamped width-fit zoom ≈ 0.405
    const { cy, collection, calls } = fakeCy({ bb: b, maxZoom: 0.2 });
    hudAwareFit(cy, collection, PAD, HUD);
    expect(calls.zoom).toEqual([0.2]);
  });

  it('(d2) a minZoom clamp that overflows the band pins the left edge to 0, never negative', () => {
    const b = bb(0, 0, 2000, 400);
    const { cy, collection, calls } = fakeCy({ bb: b, minZoom: 1 }); // forces overflow
    hudAwareFit(cy, collection, PAD, HUD);
    const [zoom] = calls.zoom;
    const [pan] = calls.pan;
    expect(zoom).toBe(1);
    expect(renderedLeft(b, zoom, pan)).toBe(0); // clamped, not centered-negative
  });
});
