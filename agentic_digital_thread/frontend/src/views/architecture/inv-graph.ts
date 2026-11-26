// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Investigation graph — initArchitecture / runLayout / _doLayout /
 * addGraphNode / addGraphEdge / filterToQuestion / _reapplyQuestionFilter.
 * The graph CONTENT lives in the arch-store
 * (session-persistent, fed by the chat bridge); this module owns the cytoscape
 * instance and reconciles it from store state (`syncFromStore`). */
import cytoscape from 'cytoscape';
import tippy from 'tippy.js';

import {
  archStore,
  buildInvCallModal,
  buildInvNodeModal,
  invTooltipText,
  type ArchGraphState,
  type ArchNode,
  type InvCallDetail,
  type InvModalExtras,
} from '../../lib/arch-store';
import { HIGHLIGHT_DARK, HIGHLIGHT_LIGHT, invNodeTextColor, invPalette, isDarkTheme, themeStore } from './colors';
import { clearCorrelation, correlationStore, selectCall } from './correlation-store';
import { invActiveEdges, maybeStopAnts, registerInvInstance, startMarchingAnts, antsRunning } from './flow-anim';
import { canFit, safeFit } from './cy-guards';
import { ensureCola, ensurePopper } from './system-map';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Cy = any;

let cy: Cy = null;
let _layoutTimer: ReturnType<typeof setTimeout> | null = null;
let _activeQuestionFilter: number | null = null;
let _questionCounterView = 0;
let _corrUnsub: (() => void) | null = null;
let _themeUnsub: (() => void) | null = null;

registerInvInstance(() => cy);

/* ── Label autofit (node-label overflow fix) ──────────────────────────────
 * Node labels are drawn at a per-type base size for short labels and step
 * down for longer ones, so aggregated labels ("MES\nMilwaukee ×3\n…") stay
 * inside their node bounds. The size is attached as a `fontSize` data
 * attribute at node-creation/update time and consumed by the stylesheet's
 * 'font-size': 'data(fontSize)' mapper (with 'text-wrap': 'wrap' +
 * 'text-overflow-wrap': 'anywhere' handling unbroken tokens). */

/** Base label font size (px) per node type — the former static per-type
 * stylesheet values; unknown types use the base node size. */
const LABEL_BASE_FONT: Record<string, number> = {
  question: 14,
  system: 15,
  qinput: 12,
  neptune: 13,
  entity: 13,
  schema: 11,
  finding: 14,
};
/** Minimum label font size (px) — long labels never shrink below this. */
export const LABEL_FONT_FLOOR = 8;

/** Length-based font-size autofit: labels of ≤12 chars keep the type's base
 * size; longer labels step down 1px per 10 extra chars, floored at
 * LABEL_FONT_FLOOR. Pure — exported for unit tests. */
export function labelFontSize(label: unknown, type?: string): number {
  const base = LABEL_BASE_FONT[type ?? ''] ?? 14;
  const len = String(label ?? '').trim().length;
  if (len <= 12) return base;
  return Math.max(LABEL_FONT_FLOOR, base - Math.ceil((len - 12) / 10));
}

/** Build the cy `data` payload for a store node — single source for node
 * creation, so every node carries the fontSize autofit attribute the
 * stylesheet mapper reads. Pure — exported for unit tests. */
export function invNodeData(
  n: ArchNode,
  c: { bg: string; border: string },
): Record<string, unknown> {
  return {
    id: n.id,
    type: n.type,
    label: n.label,
    fontSize: labelFontSize(n.label, n.type),
    bg: c.bg,
    border: c.border,
    detail: n.detail || '',
    questions: n.questions || [],
    // Honest completed-call outcome ('failed' | 'timeout' | 'partial' | '')
    // — drives the red/amber error-node border styles.
    outcome: typeof n.outcome === 'string' ? n.outcome : '',
    // Correlation key (HUD row id) — graph ↔ trace ↔ HUD selection.
    callKey: typeof n._callKey === 'string' ? n._callKey : '',
  };
}

export function getCy(): Cy {
  return cy;
}

/* ── Drill-down modal (node click) ────────────────────────────────────────
 * Rich per-call payload: finding/entity/qinput nodes minted by a tool_result
 * carry `_call` (InvCallDetail — system + tool, the question, the request in
 * English AND raw form, the FULL result summary, timing); system/neptune/
 * schema hubs carry the aggregated `_calls`. The modal receives the built
 * markdown as `content` (renders today with NO data-modal changes) plus the
 * structured `InvModalExtras` as an OPTIONAL third argument — the contract
 * the data-modal stage extends its openDataModal signature with. */
type OpenDataModalFn = (title: string, content: string, extras?: InvModalExtras) => void;

/** Open the drill-down modal for an investigation-graph node. Returns true
 * when the tap is consumed (modal opened, or an in-flight placeholder was
 * clicked — nothing to show yet); false → caller falls through to the
 * trace-entry scroll. Exported for unit tests (tap → modal payload spy). */
export function openInvNodeModal(
  nodeId: string,
  type: string | undefined,
  detail?: string,
  label?: string,
): boolean {
  const openDataModal = (window as unknown as Record<string, unknown>).openDataModal as
    | OpenDataModalFn
    | undefined;
  const storeNode = archStore.getState().nodes.find((n) => n.id === nodeId);
  // In-flight placeholder — no data yet; swallow the tap (no useless modal).
  if (storeNode?._inflight === true) return true;
  const call = storeNode?._call as InvCallDetail | undefined;
  if ((type === 'finding' || type === 'entity' || type === 'qinput') && call && storeNode) {
    const title = String(storeNode.label || label || '').split('\n')[0] || call.system;
    if (typeof openDataModal === 'function') {
      openDataModal(title, buildInvCallModal(storeNode), {
        kind: 'inv-call',
        nodeId,
        nodeType: String(type),
        calls: [call],
      });
    }
    return true;
  }
  // Legacy path — nodes minted before the per-call payload (or detail-only).
  if ((type === 'finding' || type === 'entity') && detail) {
    if (typeof openDataModal === 'function') openDataModal(String(label ?? ''), detail);
    return true;
  }
  // System / Neptune / Schema node → the FULL aggregated request+result
  // history for this system (I5) from the store node's aggregation.
  if (type === 'system' || type === 'neptune' || type === 'schema') {
    const md = storeNode ? buildInvNodeModal(storeNode) : '';
    if (md) {
      if (typeof openDataModal === 'function') {
        openDataModal(
          (storeNode?._baseLabel as string) || (storeNode?.label as string) || 'System',
          md,
          {
            kind: 'inv-node',
            nodeId,
            nodeType: String(type),
            calls: ((storeNode?._calls as InvCallDetail[]) || []),
          },
        );
      }
      return true;
    }
  }
  return false;
}

/** Port of initArchitecture — creates the cy instance on the #cy-inv-graph
 * host inside #cy-container. The host div is dedicated to cytoscape because
 * cy.destroy() removes every child of its container — mounting on
 * #cy-container itself would destroy the React-rendered #tool-hud /
 * #arch-empty / #arch-legend siblings. */
/** Build the full cytoscape stylesheet for the given theme. Extracted from
 * initArchitecture so a live theme toggle can rebuild the stylesheet in place
 * (applyInvTheme) instead of keeping the label/edge/highlight colors that
 * were baked in at init. Exported for unit tests. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function buildInvStyles(isDark: boolean): any[] {
  const textColor = invNodeTextColor('', isDark);
  // Light-contrast audit: edges were #94a3b8 (2.4:1 on the light --bg) and
  // 7px edge labels #64748b (4.5:1) — bumped to #64748b / #475569 (4.5 / 7.1).
  const edgeColor = isDark ? '#4a6080' : '#64748b';
  const edgeLabelColor = isDark ? '#7a9ab8' : '#475569';
  return [
      { selector: 'node', style: {
        label: 'data(label)',
        'text-wrap': 'wrap',
        'text-max-width': '105px',
        'text-overflow-wrap': 'anywhere',
        // Length-based autofit — set per node at creation (labelFontSize).
        'font-size': 'data(fontSize)',
        'font-family': 'Outfit, sans-serif',
        'text-valign': 'center',
        'text-halign': 'center',
        color: textColor,
        'background-color': 'data(bg)',
        'border-width': 1.5,
        'border-color': 'data(border)',
        width: 120,
        height: 65,
        padding: '16px',
        shape: 'round-rectangle',
        'text-outline-width': 0,
        'overlay-opacity': 0,
        'line-height': 1.4,
      }},
      { selector: 'node[type="question"]', style: {
        shape: 'round-rectangle',
        'font-weight': '600',
        color: invNodeTextColor('question', isDark),
        'border-width': 0,
        width: 260,
        height: 80,
        'text-max-width': '245px',
      }},
      { selector: 'node[type="system"]', style: {
        shape: 'round-rectangle',
        'font-weight': 700,
        // Was hardcoded '#ffffff' regardless of theme — 1.1:1 (invisible) on
        // the light #fef3c7 fill. Light mode gets a deep amber (7.8:1).
        color: invNodeTextColor('system', isDark),
        'border-width': 2,
        width: 160,
        height: 70,
        'text-max-width': '145px',
        'line-height': 1.5,
      }},
      { selector: 'node[type="qinput"]', style: {
        // Blue per-request input/question node (system → question → result).
        shape: 'round-rectangle',
        'font-weight': '600',
        color: invNodeTextColor('qinput', isDark), // deep-blue fill both themes
        'border-width': 2,
        width: 190,
        height: 60,
        'text-max-width': '175px',
        'line-height': 1.4,
      }},
      { selector: 'node[type="neptune"]', style: {
        shape: 'ellipse',
        'font-weight': '600',
        'border-width': 2,
        width: 150,
        height: 60,
        'text-max-width': '135px',
      }},
      { selector: 'node[type="entity"]', style: {
        shape: 'ellipse',
        'border-style': 'dashed',
        width: 130,
        height: 65,
        'text-max-width': '115px',
      }},
      { selector: 'node[type="schema"]', style: {
        shape: 'round-rectangle',
        'font-weight': '600',
        'border-style': 'dashed',
        'border-width': 1.5,
        width: 140,
        height: 55,
        'text-max-width': '125px',
        'background-opacity': 0.6,
      }},
      { selector: 'node[type="finding"]', style: {
        shape: 'round-rectangle',
        'font-weight': '600',
        'background-opacity': 0.85,
        'border-width': 1,
        width: 200,
        height: 70,
        'text-max-width': '185px',
        'line-height': 1.5,
      }},
      { selector: 'edge', style: {
        width: 3,
        'line-color': edgeColor,
        'target-arrow-color': edgeColor,
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'arrow-scale': 1.2,
        label: 'data(label)',
        'font-size': '7px',
        color: edgeLabelColor,
        'text-rotation': 'autorotate',
        'text-margin-y': -8,
        'font-family': 'IBM Plex Mono, monospace',
        'line-style': 'solid',
        opacity: 1,
      }},
      // Hover dimming — unconnected elements fade
      { selector: 'node.dimmed', style: { opacity: 0.15 } },
      { selector: 'edge.dimmed', style: { opacity: 0.08 } },
      { selector: 'node.highlighted', style: { opacity: 1, 'border-width': 3 } },
      { selector: 'edge.highlighted', style: { opacity: 1, width: 2, 'line-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT, 'target-arrow-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT } },
      { selector: 'node:selected', style: {
        'border-width': 2,
        'border-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT,
        'overlay-opacity': 0.08,
        'overlay-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT,
      }},
      { selector: 'node.querying', style: {
        'border-width': 3,
        'border-color': HIGHLIGHT_DARK,
        'overlay-opacity': 0.15,
        'overlay-color': HIGHLIGHT_DARK,
      }},
      // In-flight placeholder data node (store `_inflight`): green finding
      // palette with a dashed outline while the '…' label pulses via the
      // shared 'querying' overlay until the tool_result fills its spot.
      { selector: 'node.inflight', style: {
        'border-style': 'dashed',
        'border-width': 2,
        'background-opacity': 0.5,
      }},
      // Honest failure nodes — a failed/timed-out call's placeholder converts
      // to a small persistent error node (red border) instead of vanishing;
      // budget-stop partial results render amber. Same hues as the HUD chips.
      { selector: 'node[outcome="failed"], node[outcome="timeout"]', style: {
        'border-style': 'solid',
        'border-width': 2.5,
        'border-color': '#f85149',
        'background-opacity': 0.35,
        width: 120,
        height: 50,
      }},
      { selector: 'node[outcome="partial"]', style: {
        'border-style': 'solid',
        'border-width': 2.5,
        'border-color': '#d29922',
      }},
      { selector: 'edge.flowing', style: {
        // line-style MUST be dashed here: the base `edge` style is solid, and
        // the marching-ants loop animates `line-dash-offset`, which only moves
        // on a dashed line. Matches the system-map (cyMap) flowing look.
        'line-style': 'dashed', 'line-dash-pattern': [8, 6], width: 2,
        'line-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT,
        'target-arrow-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT,
      }},
      // Compound/parent node styling
      { selector: ':parent', style: {
        'background-opacity': 0.08,
        'border-width': 1.5,
        padding: '12px',
        'text-valign': 'top',
        'text-halign': 'center',
        'font-size': '9px',
      }},
  ];
}

export function initArchitecture(): void {
  if (cy) cy.destroy();
  const container =
    document.getElementById('cy-inv-graph') ?? document.getElementById('cy-container');
  if (!container) return;
  ensurePopper();
  cy = cytoscape({
    container,
    style: buildInvStyles(isDarkTheme()),
    layout: { name: 'preset' },
    minZoom: 0.3,
    maxZoom: 3,
    wheelSensitivity: 0.3,
  });

  // Register cola layout
  ensureCola();

  // Hover highlighting — dim unconnected elements
  cy.on('mouseover', 'node', (evt: { target: Cy }) => {
    const neighborhood = evt.target.closedNeighborhood();
    cy.elements().not(neighborhood).addClass('dimmed');
    neighborhood.addClass('highlighted');
  });
  cy.on('mouseout', 'node', () => {
    cy.elements().removeClass('dimmed highlighted');
    // The hover clear must not wipe an active correlation highlight.
    reapplyCorrelationHighlight();
  });

  // Tooltips on hover — show the detail as plain text (invTooltipText), one
  // line per content line; the 'cyto-lines' theme keeps the line breaks.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let activeTippy: any = null;
  cy.on('mouseover', 'node[type="finding"], node[type="entity"], node[type="neptune"]', (evt: { target: Cy }) => {
    const node = evt.target;
    const text = invTooltipText(node.data('detail'));
    if (!text || activeTippy) return;
    const ref = node.popperRef();
    activeTippy = tippy(document.createElement('div'), {
      getReferenceClientRect: ref.getBoundingClientRect,
      content: text,
      trigger: 'manual',
      placement: 'top',
      arrow: true,
      theme: 'cyto cyto-lines',
      maxWidth: 300,
      appendTo: document.body,
    });
    activeTippy.show();
  });
  cy.on('mouseout', 'node', () => {
    if (activeTippy) {
      activeTippy.destroy();
      activeTippy = null;
    }
  });

  // Click handler — select the call for cross-surface correlation (trace row
  // + HUD chip highlight) AND open the drill-down modal (rich per-call
  // payload when the store has one), else scroll to the associated trace
  // entry ("AND/OR": highlight + modal both happen on a call node).
  cy.on('tap', 'node', (evt: { target: Cy }) => {
    const node = evt.target;
    const nodeId = node.id();
    const ck = node.data('callKey');
    if (ck) selectCall('graph', String(ck));
    if (openInvNodeModal(nodeId, node.data('type'), node.data('detail'), node.data('label'))) return;
    let entries = document.querySelectorAll(`.trace-entry[data-node-id="${nodeId}"]`);
    if (!entries.length) {
      const neighbors = node.neighborhood('node[type="system"]');
      if (neighbors.length) {
        entries = document.querySelectorAll(`.trace-entry[data-node-id="${neighbors[0].id()}"]`);
      }
    }
    if (!entries.length) return;
    const entry = entries[entries.length - 1];
    document.querySelectorAll('.trace-entry.highlighted').forEach((e) => e.classList.remove('highlighted'));
    entry.classList.add('highlighted');
    entry.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => entry.classList.remove('highlighted'), 3000);
  });

  // Clicking empty canvas space clears the cross-surface correlation.
  cy.on('tap', (evt: { target: Cy }) => {
    if (evt.target === cy) clearCorrelation();
  });

  // Correlation subscription (module lifetime — applies to whichever cy
  // instance is current): trace/hud-sourced selections highlight AND pan to
  // the call's subtree; graph-sourced ones highlight without re-panning.
  if (!_corrUnsub) {
    _corrUnsub = correlationStore.subscribe(() => applyCorrelationToGraph());
  }
  // Theme subscription (module lifetime): a live theme toggle re-styles the
  // CURRENT instance in place — before this hook, the stylesheet's label/
  // edge/highlight colors were baked in at init and existing nodes kept their
  // minted-theme bg/border data (dark navy/green fills on the light canvas).
  if (!_themeUnsub) {
    _themeUnsub = themeStore.subscribe(() => applyInvTheme());
  }
  // A pre-existing selection (view remount) re-applies to the fresh instance.
  applyCorrelationToGraph();

  // Publish the instance as window.cy — the retired
  // legacy UI exposed `cy` as an implicit global, and
  // guarded consumers (thread-clicks focusArchNode's resize/fit/select/
  // openDataModal, ChatSidebar's cy?.resize()) silently no-oped while the
  // SPA kept it module-private.
  window.cy = cy as unknown as Window['cy'];
}

/** Re-style a LIVE instance for the current theme: rebuild the stylesheet
 * (label/edge/highlight colors are theme-derived) and re-map every node's
 * bg/border data from the theme palette — syncFromStore only colors NEW
 * nodes, so without this pass nodes minted under the other theme kept their
 * stale fills (the dark-navy-on-light-canvas bug). Driven by the themeStore
 * subscription in initArchitecture; exported (with an explicit instance) for
 * unit tests. */
export function applyInvTheme(cyInstance: Cy = cy): void {
  if (!cyInstance) return;
  const isDark = isDarkTheme();
  cyInstance.style().fromJson(buildInvStyles(isDark)).update();
  const palette = invPalette(isDark);
  cyInstance.nodes().forEach((n: Cy) => {
    const c = palette[(n.data('type') as string) || ''] || palette.default;
    n.data('bg', c.bg);
    n.data('border', c.border);
  });
}

/** Debounced layout — prevents thrashing when parallel tool results arrive. */
export function runLayout(): void {
  if (!cy || cy.nodes().length === 0) return;
  if (_layoutTimer) clearTimeout(_layoutTimer);
  _layoutTimer = setTimeout(() => {
    _layoutTimer = null;
    doLayout();
  }, 200);
}

/** Pure horizontal sweep for one row of nodes: given sorted-by-x items with
 * center x and width, return new center xs such that adjacent items keep at
 * least `margin` px of clear space, preserving order and re-centering the row
 * around its original midpoint. Exported for unit tests. */
export function sweepRowPositions(
  items: ReadonlyArray<{ x: number; w: number }>,
  margin = 40,
): number[] {
  if (items.length < 2) return items.map((it) => it.x);
  const xs = items.map((it) => it.x);
  for (let i = 1; i < items.length; i++) {
    const minGap = (items[i - 1].w + items[i].w) / 2 + margin;
    if (xs[i] - xs[i - 1] < minGap) xs[i] = xs[i - 1] + minGap;
  }
  // Re-center: keep the row's midpoint where the tier layout put it.
  const midBefore = (items[0].x + items[items.length - 1].x) / 2;
  const midAfter = (xs[0] + xs[xs.length - 1]) / 2;
  const shift = midBefore - midAfter;
  return xs.map((x) => x + shift);
}

/** The Knowledge Graph hub node id minted by the arch-store. KG-subtree
 * detection is TOPOLOGY-ONLY: this id + the inbound-edge parent chain — never
 * node types — so the layout works with or without the per-query KG qinput
 * store changes (parallel-safe 1F/1G contract). */
export const KG_HUB_ID = 'system-neptune';
/** Vertical distance between KG bands (hub → qinput fan → result stack). */
export const KG_BAND = 130;

/** Pure inbound-edge parent chain walk: immediate parent first, following the
 * FIRST inbound edge at each hop (same choice doLayout's per-tier passes
 * make), cycle-guarded and depth-bounded. Exported for unit tests. */
export function inboundParentChain(
  nodeId: string,
  edges: ReadonlyArray<{ source: string; target: string }>,
  maxDepth = 4,
): string[] {
  const chain: string[] = [];
  const seen = new Set<string>([nodeId]);
  let cur = nodeId;
  for (let d = 0; d < maxDepth; d++) {
    const inEdge = edges.find((e) => e.target === cur);
    if (!inEdge) break;
    const parent = inEdge.source;
    if (!parent || seen.has(parent)) break;
    chain.push(parent);
    seen.add(parent);
    cur = parent;
  }
  return chain;
}

/** Pure KG-band rule (Task 1G): given a node's inbound-edge parent chain
 * (immediate parent first) and the tier map, return the base y for that node
 * in the KG subtree ABOVE the Knowledge Graph hub — or null when the chain
 * never reaches the hub (caller falls back to the normal tier, so a missing
 * parent can never produce NaN).
 *  - band 'qinput': the per-query fan directly above the hub
 *    (tierY.neptune - KG_BAND).
 *  - band 'result': the findings/entities band above the qinput fan
 *    (tierY.neptune - 2 * KG_BAND); callers stack further results UPWARD from
 *    it. Results attached DIRECTLY to the hub (store without per-query KG
 *    qinputs, or the mint-on-miss fallback) land in the same band. */
export function kgSubtreeY(
  parentChain: ReadonlyArray<string>,
  tierY: Readonly<Record<string, number>>,
  band: 'qinput' | 'result' = 'qinput',
): number | null {
  if (!parentChain.includes(KG_HUB_ID)) return null;
  const neptuneY = typeof tierY.neptune === 'number' ? tierY.neptune : 200;
  const qinputBandY = neptuneY - KG_BAND;
  return band === 'qinput' ? qinputBandY : qinputBandY - KG_BAND;
}

/** Post-layout overlap resolution: group nodes into horizontal rows (same
 * tier y, bucketed) and sweep each row so no two nodes render on top of each
 * other — including after incremental additions (doLayout re-runs per event). */
function resolveRowOverlaps(): void {
  if (!cy) return;
  const rows: Record<string, Cy[]> = {};
  cy.nodes().forEach((n: Cy) => {
    const key = String(Math.round(n.position().y / 30) * 30);
    (rows[key] = rows[key] || []).push(n);
  });
  Object.values(rows).forEach((nodes) => {
    if (nodes.length < 2) return;
    nodes.sort((a: Cy, b: Cy) => a.position().x - b.position().x);
    const items = nodes.map((n: Cy) => ({ x: n.position().x, w: n.width() || 120 }));
    const xs = sweepRowPositions(items);
    nodes.forEach((n: Cy, i: number) => {
      if (xs[i] !== items[i].x) n.position({ x: xs[i], y: n.position().y });
    });
  });
}

/** Port of _doLayout — hierarchical tiers: question → system → qinput → finding. */
function doLayout(): void {
  if (!cy || cy.nodes().length === 0) return;
  const tierY: Record<string, number> = { question: 340, neptune: 200, system: 490, qinput: 620, entity: 60, finding: 760 };
  const tierNodes: Record<string, Cy[]> = { question: [], neptune: [], system: [], qinput: [], entity: [], finding: [], schema: [] };
  cy.nodes().forEach((n: Cy) => {
    const t = n.data('type') || 'system';
    if (tierNodes[t]) tierNodes[t].push(n);
    else tierNodes.system.push(n);
  });
  // Place system nodes evenly across a wide canvas (>=300px spacing so wide
  // finding nodes stacked beneath adjacent systems don't collide).
  const canvasW = Math.max(1100, tierNodes.system.length * 300);
  const systemXMap: Record<string, number> = {};
  tierNodes.system.forEach((n, i) => {
    const spacing = canvasW / (tierNodes.system.length + 1);
    const x = spacing * (i + 1);
    n.position({ x, y: tierY.system });
    systemXMap[n.id()] = x;
  });
  tierNodes.neptune.forEach((n) => {
    n.position({ x: canvasW / 2, y: tierY.neptune });
  });
  tierNodes.question.forEach((n, i) => {
    const spacing = Math.max(300, canvasW / (tierNodes.question.length + 1));
    const startX = canvasW / 2 - (spacing * (tierNodes.question.length - 1)) / 2;
    n.position({ x: startX + i * spacing, y: tierY.question });
  });
  // Blue "input/question" nodes sit under their parent system; multiple
  // distinct requests fan out horizontally around the system's column.
  // KG-attached qinputs instead fan ABOVE the Knowledge Graph hub (Task 1G).
  // Pure edge list once — feeds the topology-only KG-subtree detection.
  const edgeList: Array<{ source: string; target: string }> = cy
    .edges()
    .map((e: Cy) => ({
      source: e.data('source') as string,
      target: e.data('target') as string,
    }));
  const qinputXMap: Record<string, number> = {};
  const qinputsByParent: Record<string, Cy[]> = {};
  tierNodes.qinput.forEach((n) => {
    const inEdges = cy.edges(`[target="${n.id()}"]`);
    const parentId = inEdges.length ? inEdges[0].data('source') : null;
    (qinputsByParent[parentId] = qinputsByParent[parentId] || []).push(n);
  });
  Object.entries(qinputsByParent).forEach(([parentId, qnodes]) => {
    const px = systemXMap[parentId] != null ? systemXMap[parentId] : canvasW / 2;
    const spread = 220;
    const startX = px - (spread * (qnodes.length - 1)) / 2;
    // Topology-only: hub-parented qinputs go to the KG band above the hub
    // (px already falls back to canvasW / 2 — the hub's x). Others keep the
    // classic below-system tier.
    const kgY = kgSubtreeY([parentId], tierY, 'qinput');
    const y = kgY != null ? kgY : tierY.qinput;
    qnodes.forEach((n, i) => {
      const x = startX + i * spread;
      n.position({ x, y });
      qinputXMap[n.id()] = x;
    });
  });
  // Schema nodes: to the LEFT of the question node, stacked vertically
  const questionX = canvasW / 2;
  const schemaStartX = questionX - 320;
  tierNodes.schema.forEach((n, i) => {
    n.position({ x: schemaStartX, y: tierY.question - 80 + i * 90 });
  });
  // Place findings under their parent (qinput / system / schema), staggered
  const findingsByParent: Record<string, Cy[]> = {};
  tierNodes.finding.forEach((n) => {
    const fEdges = cy.edges(`[target="${n.id()}"]`);
    const parentId = fEdges.length ? fEdges[0].data('source') : null;
    if (!findingsByParent[parentId]) findingsByParent[parentId] = [];
    findingsByParent[parentId].push(n);
  });
  Object.entries(findingsByParent).forEach(([parentId, fnodes]) => {
    const parentNode = cy.getElementById(parentId);
    const parentType = parentNode.length ? parentNode.data('type') : 'system';
    const parentPos = parentNode.length ? parentNode.position() : { x: canvasW / 2, y: tierY.system };
    if (parentType === 'schema') {
      // Schema findings go to the left of the schema node
      fnodes.forEach((n, i) => {
        n.position({ x: parentPos.x - 180, y: parentPos.y + i * 70 });
      });
    } else {
      // Findings hang off their blue input node (qinput) for system queries;
      // fall back to the system column, then canvas center.
      const parentX =
        qinputXMap[parentId] != null
          ? qinputXMap[parentId]
          : systemXMap[parentId] != null
            ? systemXMap[parentId]
            : parentPos.x || canvasW / 2;
      // KG-subtree findings (parent is a KG qinput, or the hub directly —
      // robustness for stores without per-query KG qinputs) stack UPWARD from
      // the KG qinput band instead of dropping to the crowded bottom tier.
      const kgY = kgSubtreeY(
        [parentId, ...inboundParentChain(parentId, edgeList)],
        tierY,
        'result',
      );
      if (kgY != null) {
        fnodes.forEach((n, i) => {
          n.position({ x: parentX, y: kgY - i * KG_BAND });
        });
      } else {
        fnodes.forEach((n, i) => {
          n.position({ x: parentX, y: tierY.finding + i * 130 });
        });
      }
    }
  });
  // Entities attached to a KG qinput fan around their parent's x in the upper
  // KG band; everything else keeps the classic y=60 entity row (which is the
  // fallback for hub-attached entities from stores without KG qinputs).
  const kgEntitiesByParent: Record<string, Cy[]> = {};
  const rowEntities: Cy[] = [];
  tierNodes.entity.forEach((n) => {
    const chain = inboundParentChain(n.id(), edgeList);
    const underKgQinput =
      chain.length > 1 && chain[0] !== KG_HUB_ID && chain.includes(KG_HUB_ID);
    if (underKgQinput) (kgEntitiesByParent[chain[0]] = kgEntitiesByParent[chain[0]] || []).push(n);
    else rowEntities.push(n);
  });
  Object.entries(kgEntitiesByParent).forEach(([parentId, enodes]) => {
    const px = qinputXMap[parentId] != null ? qinputXMap[parentId] : canvasW / 2;
    const y = kgSubtreeY([parentId, KG_HUB_ID], tierY, 'result') as number;
    const spread = 150;
    const startX = px - (spread * (enodes.length - 1)) / 2;
    enodes.forEach((n, i) => {
      n.position({ x: startX + i * spread, y });
    });
  });
  rowEntities.forEach((n, i) => {
    const spacing = Math.max(150, canvasW / (rowEntities.length + 1));
    const startX = canvasW / 2 - (spacing * (rowEntities.length - 1)) / 2;
    n.position({ x: startX + i * spacing, y: tierY.entity });
  });
  // Push apart any nodes the tiered placement left overlapping (fan-outs of
  // adjacent systems, dense entity rows, multi-question accumulations).
  resolveRowOverlaps();
  // Keep the newest/active question's subtree in focus as nodes stream in.
  reapplyQuestionFilter();
  if (canFit(cy)) {
    const focus =
      _activeQuestionFilter !== null
        ? cy.elements().filter((el: Cy) => (el.data('questions') || []).includes(_activeQuestionFilter))
        : null;
    const hudW = measuredHudWidth();
    if (focus && focus.length) hudAwareFit(cy, focus, 40, hudW);
    else hudAwareFit(cy, null, 40, hudW);
  }
}

/* ── HUD-aware fit ────────────────────────────────────────────────────────
 * The tool-activity HUD (#tool-hud) overlays the TOP-RIGHT of the graph
 * canvas, so a plain cy.fit() can hide the rightmost nodes under it. The old
 * fix — cy.fit(...) followed by a blind cy.panBy({ x: -110 }) — pushed the
 * LEFTMOST node (the question node) off-screen whenever the fitted content
 * already spanned the viewport. Instead, fit against a REDUCED viewport that
 * excludes the HUD strip: the content is zoomed/panned to center within the
 * [0, viewportW - hudW] band, and the rendered left edge is never negative. */

/** Horizontal strip (px) reserved on the RIGHT of the graph viewport for the
 * #tool-hud overlay (position:absolute, right-anchored, sizes to content).
 * Used when the live HUD element can't be measured; matches the magnitude of
 * the previous blind panBy(-110). */
export const HUD_RESERVED_W = 110;

/** Measure the live #tool-hud overlay width, falling back to HUD_RESERVED_W
 * when the element is absent or unlaid-out (hidden tab, headless tests). */
function measuredHudWidth(): number {
  try {
    const el = typeof document !== 'undefined' ? document.getElementById('tool-hud') : null;
    const w = el ? el.offsetWidth : 0;
    return w > 0 ? w : HUD_RESERVED_W;
  } catch {
    return HUD_RESERVED_W;
  }
}

/** Fit `eles` (or the whole graph when null/empty) into the viewport band
 * that excludes the right-side HUD strip.
 *
 * zoom = min((viewportW - hudW - 2·pad) / bb.w, (viewportH - 2·pad) / bb.h),
 * clamped to cy's [minZoom, maxZoom]; the pan centers the bounding box within
 * the [0, viewportW - hudW] band, with the rendered LEFT edge clamped to ≥ 0
 * (when a minZoom clamp makes the content wider than the band, the left edge
 * pins to 0 so the question node stays visible). A degenerate/empty bounding
 * box — or an unusably small band — falls back to a plain cy.fit with no pan.
 *
 * Exported for unit tests; callers must have passed canFit(). Touches only
 * elements()/boundingBox()/width()/height()/minZoom()/maxZoom()/zoom()/pan()/
 * fit() on the instance. */
export function hudAwareFit(
  cyInstance: Cy,
  eles: Cy | null | undefined,
  pad = 40,
  hudW = HUD_RESERVED_W,
): void {
  const target = eles && eles.length ? eles : cyInstance.elements();
  const bb = target.boundingBox();
  const vw = cyInstance.width();
  const vh = cyInstance.height();
  const bandW = vw - hudW; // usable horizontal band: [0, bandW]
  const rawZoom = Math.min((bandW - 2 * pad) / bb.w, (vh - 2 * pad) / bb.h);
  if (!Number.isFinite(rawZoom) || rawZoom <= 0) {
    // Empty/zero bounding box or a viewport too small for the band — plain
    // fit (no pan) rather than a broken transform.
    cyInstance.fit(undefined, pad);
    return;
  }
  let zoom = rawZoom;
  const maxZ = typeof cyInstance.maxZoom === 'function' ? cyInstance.maxZoom() : NaN;
  const minZ = typeof cyInstance.minZoom === 'function' ? cyInstance.minZoom() : NaN;
  if (Number.isFinite(maxZ)) zoom = Math.min(zoom, maxZ);
  if (Number.isFinite(minZ)) zoom = Math.max(zoom, minZ);
  // rendered_x = model_x · zoom + pan.x — center the bb in the band, but
  // never let the rendered left edge (bb.x1 · zoom + pan.x) go negative.
  const centeredPanX = bandW / 2 - ((bb.x1 + bb.x2) / 2) * zoom;
  const panX = Math.max(centeredPanX, -bb.x1 * zoom);
  const panY = vh / 2 - ((bb.y1 + bb.y2) / 2) * zoom;
  cyInstance.zoom(zoom);
  cyInstance.pan({ x: panX, y: panY });
}

/* ── Cross-surface correlation (graph ↔ trace ↔ HUD) ─────────────────────
 * The correlation-store publishes {source, callKey}; a call's graph nodes
 * carry `callKey` in their cy data (invNodeData). The graph applies the
 * existing `.highlighted` class to the call's nodes + their edges, and pans
 * to the subtree (hudAwareFit) when the selection came from the trace/HUD. */

/** Escape a callKey for use inside a cy attribute selector. */
function cySelectorEscape(v: string): string {
  return v.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/** The selected call's nodes ∪ their connected edges (empty when none). */
function correlatedElements(): Cy | null {
  if (!cy) return null;
  const sel = correlationStore.get();
  if (!sel) return null;
  const els = cy.nodes(`[callKey = "${cySelectorEscape(sel.callKey)}"]`);
  if (!els.length) return null;
  let group = els.union(els.connectedEdges());
  // The question→system edge connects two nodes that carry NO callKey (the
  // question node and the shared system node), so callKey collection alone
  // never reaches it — the highlight visibly stopped at the system agent
  // instead of tracing back to the question. Walk each selected node's
  // incoming side: any directly-connected system node's edges FROM question
  // nodes join the group (plus the endpoints, so the path reads complete).
  els.forEach((n: Cy) => {
    const sysNeighbors = n.closedNeighborhood('node[type="system"], node[type="neptune"], node[type="schema"]');
    sysNeighbors.forEach((sysN: Cy) => {
      const qEdges = sysN.connectedEdges().filter((e: Cy) => {
        const src = e.source();
        return src && src.data('type') === 'question' && e.target().id() === sysN.id();
      });
      if (qEdges.length) {
        group = group.union(qEdges).union(qEdges.sources()).union(sysN);
      }
    });
  });
  return group;
}

/** Re-add the correlation highlight (after hover/mouseout wiped classes). */
export function reapplyCorrelationHighlight(): void {
  const group = correlatedElements();
  if (group) group.addClass('highlighted');
}

/** Apply the current selection to the graph: clear the previous highlight,
 * highlight the call's nodes/edges, and pan to them for trace/hud-sourced
 * selections (graph-sourced clicks don't yank the viewport). */
export function applyCorrelationToGraph(): void {
  if (!cy) return;
  cy.elements().removeClass('highlighted');
  const sel = correlationStore.get();
  const group = correlatedElements();
  if (!sel || !group) return;
  group.addClass('highlighted');
  if (sel.source !== 'graph' && canFit(cy)) {
    hudAwareFit(cy, group, 60, measuredHudWidth());
  }
}

/** Re-apply the active per-question dimming to every element. */
function reapplyQuestionFilter(): void {
  if (!cy || _activeQuestionFilter === null) return;
  cy.elements().forEach((el: Cy) => {
    const qs = el.data('questions') || [];
    if (qs.includes(_activeQuestionFilter)) el.removeClass('dimmed').addClass('highlighted');
    else el.addClass('dimmed').removeClass('highlighted');
  });
}

/** Port of filterToQuestion's cy half (trace/HUD filtering is React-side). */
export function applyQuestionFilter(qNum: number | null): void {
  _activeQuestionFilter = qNum;
  if (!cy) return;
  if (qNum === null) {
    cy.elements().removeClass('dimmed highlighted');
    safeFit(cy, null, 50);
    return;
  }
  // Filter by data attribute instead of graph traversal (shared system nodes
  // connect all questions).
  reapplyQuestionFilter();
  // Always show the question root node connected to this question
  const qNode = cy.getElementById(`question-${qNum}`);
  if (qNode.length) {
    qNode.removeClass('dimmed').addClass('highlighted');
  }
  const visible = cy.elements().not('.dimmed');
  safeFit(cy, visible, 50);
}

/** Pure reconcile diff between the current cy element ids and a store
 * snapshot. Extracted from syncFromStore so the reconcile-on-(re)activation
 * guarantee — a fresh/partial cy instance receives the FULL accumulated
 * snapshot, not just future events — is unit-testable offline. */
export interface GraphDiff {
  removeNodeIds: string[];
  removeEdgeIds: string[];
  /** State nodes already in cy — label/detail/questions refresh. */
  updateNodes: ArchNode[];
  addNodes: ArchNode[];
  addEdges: ArchGraphState['edges'];
}

export function computeGraphDiff(
  existingNodeIds: ReadonlySet<string>,
  existingEdgeIds: ReadonlySet<string>,
  state: Pick<ArchGraphState, 'nodes' | 'edges'>,
): GraphDiff {
  const stateNodeIds = new Set(state.nodes.map((n) => n.id));
  const stateEdgeIds = new Set(state.edges.map((e) => e.id));
  return {
    removeNodeIds: [...existingNodeIds].filter((id) => !stateNodeIds.has(id)),
    removeEdgeIds: [...existingEdgeIds].filter((id) => !stateEdgeIds.has(id)),
    updateNodes: state.nodes.filter((n) => existingNodeIds.has(n.id)),
    addNodes: state.nodes.filter((n) => !existingNodeIds.has(n.id)),
    // Post-reconcile the cy node set equals the state node set, so an edge is
    // addable iff both endpoints are state nodes (same guard as before).
    addEdges: state.edges.filter(
      (e) =>
        !!e.id &&
        !existingEdgeIds.has(e.id) &&
        stateNodeIds.has(e.source) &&
        stateNodeIds.has(e.target),
    ),
  };
}

/** Pure idle-but-live pulse rule (Task 1H): the CURRENT question node carries
 * the existing 'querying' marching-ants pulse while the investigation is live
 * (user_msg received, done not yet) and NO tool is in flight — the silent
 * gaps between parallel tool batches. Presence-driven off the snapshot:
 * a tool_start (hudInFlight > 0) or the turn settling (done / error-path
 * done → investigationActive false) clears it on the next sync. Exported for
 * unit tests. */
export function questionPulseActive(
  state: Pick<ArchGraphState, 'investigationActive' | 'hudInFlight'>,
): boolean {
  return !!state.investigationActive && (state.hudInFlight ?? 0) === 0;
}

/** Reconcile the cy instance from store state (add / update / remove),
 * preserving the original add animations and querying/flowing classes. */
export function syncFromStore(state: ArchGraphState): void {
  if (!cy) return;
  _questionCounterView = state.questionCounter;

  const isDark = isDarkTheme();
  const palette = invPalette(isDark);
  const existingNodeIds = new Set<string>(cy.nodes().map((n: Cy) => n.id() as string));
  const existingEdgeIds = new Set<string>(
    cy
      .edges()
      .map((e: Cy) => e.data('id') as string | undefined)
      .filter((id: string | undefined): id is string => !!id),
  );
  const diff = computeGraphDiff(existingNodeIds, existingEdgeIds, state);

  // Remove cy elements no longer in the store (reset / '+ New').
  diff.removeNodeIds.forEach((id) => cy.getElementById(id).remove());
  diff.removeEdgeIds.forEach((id) => cy.getElementById(id).remove());

  // Update nodes already present.
  diff.updateNodes.forEach((n: ArchNode) => {
    const existing = cy.getElementById(n.id);
    if (!existing.length) return;
    existing.data('label', n.label);
    existing.data('fontSize', labelFontSize(n.label, n.type));
    existing.data('detail', n.detail || '');
    existing.data('questions', n.questions || []);
    // Placeholder→error-node conversion and correlation keys mutate in
    // place — refresh them so the red/amber border styles + selection apply.
    existing.data('outcome', typeof n.outcome === 'string' ? n.outcome : '');
    existing.data('callKey', typeof n._callKey === 'string' ? n._callKey : '');
  });

  // Add new nodes (with the original fade-in animation).
  diff.addNodes.forEach((n: ArchNode) => {
    // Unknown types use the palette's theme-matched `default` swatch (the old
    // hardcoded fallback was the DARK slate pair in both themes — a dark blob
    // with invisible dark text on the light canvas).
    const c = palette[n.type || ''] || palette.default;
    const el = cy.add({ data: invNodeData(n, c) });
    el.style('opacity', 0);
    el.animate({ style: { opacity: 1 }, duration: 600, easing: 'ease-in-out-cubic' });
  });

  // querying class — parallel-safe active count from the store; in-flight
  // placeholder nodes (`_inflight`) share the same breathing pulse plus a
  // dashed 'inflight' outline. Removing the class also clears the inline
  // pulse opacity the ants loop applied, so the node returns to its resting
  // style. (Placeholders are REMOVED by the store on their tool_result /
  // done / user_msg — they are never un-flagged in place.)
  state.nodes.forEach((n: ArchNode) => {
    const node = cy.getElementById(n.id);
    if (!node.length) return;
    const inflight = n._inflight === true;
    if (inflight) node.addClass('inflight');
    else if (node.hasClass('inflight')) node.removeClass('inflight');
    if (((n._activeQueries as number) || 0) > 0 || inflight) node.addClass('querying');
    else if (node.hasClass('querying')) {
      node.removeClass('querying');
      node.removeStyle('overlay-opacity');
    }
  });

  // Task 1H: idle-but-live pulse — while the turn is live with no tool in
  // flight, the current question node gets the same 'querying' pulse class
  // the in-flight system nodes use (the shared marching-ants rAF loop drives
  // the breathing overlay; no polling). NOTE: this runs AFTER the loop above
  // — question nodes carry no _activeQueries, so that loop's else-branch
  // would otherwise strip the class right back off.
  const pulseQ = questionPulseActive(state);
  const qNode = cy.getElementById(state.currentQuestionId);
  if (qNode.length) {
    if (pulseQ) qNode.addClass('querying');
    else if (qNode.hasClass('querying')) {
      qNode.removeClass('querying');
      qNode.removeStyle('overlay-opacity');
    }
  }
  if (pulseQ && !antsRunning()) startMarchingAnts();

  // Add edges (jitter the target near its source pre-layout, as before).
  diff.addEdges.forEach((e) => {
    if (!cy.getElementById(e.source).length || !cy.getElementById(e.target).length) return;
    const target = cy.getElementById(e.target);
    const source = cy.getElementById(e.source);
    if (
      target.position().x === 0 &&
      target.position().y === 0 &&
      (source.position().x !== 0 || source.position().y !== 0)
    ) {
      const jitter = () => (Math.random() - 0.5) * 80;
      target.position({ x: source.position().x + jitter(), y: source.position().y + jitter() });
    }
    const edge = cy.add({
      data: { id: e.id, source: e.source, target: e.target, label: e.label || '', questions: e.questions || [] },
    });
    edge.style('opacity', 0);
    edge.animate({ style: { opacity: 1 }, duration: 800 });
  });

  // Flowing-edge animation state.
  const flowing = new Set(state.flowingEdges);
  invActiveEdges.forEach((id) => {
    if (!flowing.has(id)) {
      const e = cy.getElementById(id);
      if (e.length) e.removeClass('flowing');
      invActiveEdges.delete(id);
    }
  });
  flowing.forEach((id) => {
    const e = cy.getElementById(id);
    if (e.length && !invActiveEdges.has(id)) {
      e.addClass('flowing');
      invActiveEdges.add(id);
    }
  });
  if (invActiveEdges.size > 0 && !antsRunning()) startMarchingAnts();
  maybeStopAnts();

  // Question-filter dimming follows the store's active filter.
  if (state.activeQuestionFilter !== _activeQuestionFilter) {
    applyQuestionFilter(state.activeQuestionFilter);
  }

  runLayout();
}

/** cy.resize() + relayout — used on window/container resize + tab activation. */
export function resizeInvGraph(): void {
  if (cy && cy.nodes().length > 0) {
    cy.resize();
    doLayout();
  } else if (cy) {
    cy.resize();
  }
}

export function fitInvGraph(): void {
  if (cy) {
    requestAnimationFrame(() => {
      if (!cy) return;
      cy.resize();
      safeFit(cy, null, 50);
    });
  }
}

export function invNodeCount(): number {
  return cy ? cy.nodes().length : 0;
}

export function currentQuestion(): number {
  return _questionCounterView;
}

export function destroyInvGraph(): void {
  if (_layoutTimer) {
    clearTimeout(_layoutTimer);
    _layoutTimer = null;
  }
  if (cy) {
    try {
      cy.destroy();
    } catch {
      /* already destroyed */
    }
    cy = null;
  }
  // Clear the published global so guarded consumers no-op instead of
  // touching a destroyed instance (pairs with initArchitecture's publish).
  window.cy = null;
}
