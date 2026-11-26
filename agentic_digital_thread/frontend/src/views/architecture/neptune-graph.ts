// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Neptune Full Knowledge Graph — loadNeptuneGraph, filterNeptuneGraph,
 * requestKgAiAnalysis and _renderNeptuneGraph (incl. the expand-on-click
 * neighbors handler). Module-level singleton (cyNeptune).
 *
 * View coherence (UI improvement D): BOTH dataset producers — the filtered
 * overview and the neighborhood expand — route through kg-view-store's
 * setCurrentView, so the legend counts (KgTypeLegend) and the breadcrumb
 * (KgViewBreadcrumb) always describe exactly what is rendered. The old
 * DOM-checkbox filter pipeline (handleKgFilterChange) is replaced by the
 * store's visibility toggles. */
import cytoscape from 'cytoscape';

import { requestJson } from '../../api/client';
import type {
  KgAnalyzeResponse,
  NeptuneEdge,
  NeptuneGraphResponse,
  NeptuneNeighborsResponse,
  NeptuneNode,
  NeptuneNodeDetail,
} from '../../api/types/neptune';
import { createStore } from '../../lib/store';
import { isInactiveStatus } from '../../lib/rule-status';
import { isDarkTheme, neptunePalette } from './colors';
import {
  registerKgRenderer,
  resetKgViewState,
  setCurrentView,
  type KgDataset,
  type KgViewState,
} from './kg-view-store';
import { ensureCola } from './system-map';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Cy = any;

let cyNeptune: Cy = null;
let _neptuneData: NeptuneGraphResponse | null = null;

/** Node types present in the rendered KG — <GraphLegend/> (Legends.tsx)
 * renders the swatches (jsx-migration W2.3 — was legend.innerHTML=). */
export const neptuneLegendStore = createStore<string[]>([]);

/** AI-analysis panel state (#kg-ai-analysis) — rendered by
 * <NeptuneAnalysisPanel/> (jsx-migration W2.3 — was panel.innerHTML=).
 * `ready.analysis` is raw LLM markdown; the component renders it through the
 * shared sanitized <Md/> sink (marked + DOMPurify). */
export type KgAnalysisState =
  | { status: 'idle' }
  | { status: 'analyzing' }
  | { status: 'ready'; analysis: string }
  | { status: 'none' }
  /** `message`: the server's public error text (category + reference). */
  | { status: 'error'; message?: string };

export const kgAnalysisStore = createStore<KgAnalysisState>({ status: 'idle' });

/** KG selection-details panel state (#kg-selection-info) — rendered by
 * <KgSelectionInfo/> (jsx-migration W6 — was an unescaped selPanel.innerHTML=
 * interpolation in the tap handler; graph-sourced labels/properties now go
 * through JSX text escaping). */
export interface KgSelection {
  label: string;
  name: string;
  /** properties minus `name`, values pre-truncated to 100 chars. */
  props: [key: string, value: string][];
  outgoing: number;
  incoming: number;
}

export const kgSelectionStore = createStore<KgSelection | null>(null);

export function getCyNeptune(): Cy {
  return cyNeptune;
}

/** Focus a KG node from outside the view (KG-contribution "Bridged via"
 * chips): reuse the EXISTING selection machinery — select the node
 * (the `node:selected` highlight style), pan/fit to its neighborhood (same
 * resize+fit+select path as thread-clicks' focusArchNode), and publish the
 * selection to kgSelectionStore (props sourced from the fetched graph data,
 * same shape/100-char value truncation as the tap handler). Returns false —
 * touching NO state — when the graph is not rendered or the node is absent,
 * so callers show an honest no-selection fallback, never a fake highlight. */
export function focusKgNode(nodeId: string): boolean {
  if (!cyNeptune) return false;
  // Chips carry the node's `id` PROPERTY (e.g. subj-…/vm-learned-…), while cy
  // elements are keyed by the engine-internal ~id — resolve via the fetched
  // data first (either key), then look up the cy element by its actual id.
  const src = _neptuneData?.nodes.find(
    (x) => x.id === nodeId || x.props?.id === nodeId,
  );
  const n = cyNeptune.getElementById(src?.id ?? nodeId);
  if (!n || n.length === 0 || !n.isNode()) return false;
  cyNeptune.elements().unselect();
  n.select();
  cyNeptune.resize();
  cyNeptune.fit(n.closedNeighborhood(), 60);
  kgSelectionStore.set({
    label: String(n.data('type') || 'Unknown'),
    name: String(n.data('label') || nodeId),
    props: Object.entries(src?.props || {})
      .filter(([k]) => k !== 'name')
      .map(([k, v]): [string, string] => [k, String(v).slice(0, 100)]),
    outgoing: n.outgoers('edge').length,
    incoming: n.incomers('edge').length,
  });
  return true;
}

/** @param force re-render even when the fetched data is unchanged (the
 * explicit ↻ Refresh button — restores the filtered overview after an
 * expand-on-click exploration without a blanking overlay). */
export async function loadNeptuneGraph(force = false): Promise<void> {
  const empty = document.getElementById('cy-neptune-empty');
  // KEEP-PREVIOUS SILENT RECONCILE (same pattern as proposals.ts): when a
  // graph is already loaded, keep rendering it — no loading overlay, refresh
  // in the background, and only replace the rendered elements when fresh data
  // actually arrives AND differs (a tab re-entry with unchanged data keeps
  // the current instance, so zoom/pan, an expanded-neighbors view and the
  // kg selection/analysis panels all survive re-activation).
  const silent = _neptuneData !== null;
  if (!silent && empty) {
    empty.style.display = 'flex';
    empty.textContent = 'Loading knowledge graph…';
  }

  try {
    const data = await requestJson<NeptuneGraphResponse>('/api/neptune/graph');
    // A failed read answers 200 with an empty graph and the server's public
    // error text: treat it as a failure, so a refresh keeps the current graph
    // and a first load shows the message and its reference.
    if (data.error && !(data.nodes || []).length) throw new Error(data.error);
    const changed = JSON.stringify(data) !== JSON.stringify(_neptuneData);
    _neptuneData = data;
    if (empty) empty.style.display = 'none';
    // Publish the filtered overview through kg-view-store (legend toggles
    // apply at render time; a force refresh restores filtered mode).
    if (!silent || changed || force) filterNeptuneGraph();
  } catch (e) {
    if (silent) {
      // Failed background refresh — keep the existing graph untouched.
      console.error('Knowledge graph refresh failed (keeping current graph):', e);
    } else if (empty) {
      empty.style.display = 'flex';
      empty.textContent = 'Failed to load graph: ' + (e as Error).message;
    }
  }
}

/** The filtered-overview producer: publish the full fetched dataset as the
 * current view (mode 'filtered') through kg-view-store — the legend's
 * visibility toggles do the per-type hiding at render time. loadNeptuneGraph
 * keeps calling this, so the ↻ Refresh force path also restores filtered
 * mode (preserving the documented refresh behavior). */
export function filterNeptuneGraph(): void {
  if (!_neptuneData) return;
  setCurrentView(
    {
      nodes: [..._neptuneData.nodes],
      edges: [..._neptuneData.edges],
      notDrawn: { ...(_neptuneData.not_drawn ?? UNKNOWN_NOT_DRAWN) },
    },
    { mode: 'filtered' },
  );
}

/** What a response without `not_drawn` means: a server that predates the
 * field (a rolling deploy can pair a new bundle with an older task), so the
 * labels the overview leaves out have unknown counts. `null` renders as
 * "count unavailable"; an empty map would render the static rows as 0. */
const UNKNOWN_NOT_DRAWN: Readonly<Record<string, number | null>> = Object.freeze({
  Field: null,
  Property: null,
  OnboardingLog: null,
});

/** Map a neighbors payload to a KgDataset — center node (real props) +
 * deduped neighbors + direction-correct, deduped edges (outgoing:
 * center→target; incoming: source→center). Exported for tests: a flipped
 * direction or duplicated node would corrupt the legend counts. */
export function neighborsToDataset(nodeId: string, data: NeptuneNeighborsResponse): KgDataset {
  const center = data.node || {};
  const props = center.properties || {};
  const nodes: NeptuneNode[] = [{
    id: nodeId,
    label: String(props.name || props.id || nodeId),
    type: (center.labels || [])[0] || 'Unknown',
    props,
  }];
  const seenNodes = new Set([nodeId]);
  const edges: NeptuneEdge[] = [];
  const seenEdges = new Set<string>();
  const pushEdge = (source: string, target: string, label: string) => {
    const key = `${source}-${label}-${target}`;
    if (seenEdges.has(key)) return;
    seenEdges.add(key);
    edges.push({ source, target, label });
  };
  for (const edge of data.outgoing || []) {
    if (!seenNodes.has(edge.target_id)) {
      seenNodes.add(edge.target_id);
      nodes.push({ id: edge.target_id, label: edge.target_name, type: edge.target_label, props: {} });
    }
    pushEdge(nodeId, edge.target_id, edge.relationship);
  }
  for (const edge of data.incoming || []) {
    if (!seenNodes.has(edge.source_id)) {
      seenNodes.add(edge.source_id);
      nodes.push({ id: edge.source_id, label: edge.source_name, type: edge.source_label, props: {} });
    }
    pushEdge(edge.source_id, nodeId, edge.relationship);
  }
  return { nodes, edges };
}

/* AI Analysis of selected KG nodes */
let _kgAiAbortController: AbortController | null = null;
export async function requestKgAiAnalysis(nodeData: NeptuneNodeDetail): Promise<void> {
  // Cancel any in-flight request
  if (_kgAiAbortController) _kgAiAbortController.abort();
  _kgAiAbortController = new AbortController();

  kgAnalysisStore.set({ status: 'analyzing' });

  const props = nodeData.properties || {};
  const label = (nodeData.labels || [])[0] || 'Unknown';
  const name = (props.name as string) || (props.id as string) || '';
  const propsText = Object.entries(props)
    .map(([k, v]) => `${k}: ${String(v).slice(0, 100)}`)
    .join(', ');

  try {
    const data = await requestJson<KgAnalyzeResponse>('/api/kg/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_type: label, node_name: name, properties: propsText }),
      signal: _kgAiAbortController.signal,
    });
    if (data.analysis) {
      // LLM-authored markdown — <NeptuneAnalysisPanel/> renders it through the
      // shared sanitized <Md/> sink (marked + DOMPurify), same as every other
      // LLM output in the app.
      kgAnalysisStore.set({ status: 'ready', analysis: data.analysis });
    } else if (data.error) {
      kgAnalysisStore.set({ status: 'error', message: data.error });
    } else {
      kgAnalysisStore.set({ status: 'none' });
    }
  } catch (e) {
    if ((e as Error).name !== 'AbortError') {
      kgAnalysisStore.set({ status: 'error' });
    }
  }
}

export interface RuleNodeColors {
  bg: string;
  border: string;
  textColor: string;
  /** A switched-off rule node: drawn muted with a dashed border. */
  inactive: boolean;
  /** Leads the tooltip of a switched-off rule node. */
  tooltipPrefix: string;
}

const INACTIVE_RULE_NOTE = 'Deactivated — kept for audit, not used as a join or a rule\n';

/** Colours of one KG node. A JoinPath is distinguished by method (self-learned
 * green, cross-system orange, internal grey); a switched-off JoinPath (its own
 * status, or the identity rule it was made from: the server's
 * `effective_status`) or identity rule is drawn in the inactive style. */
export function ruleNodeColors(
  n: NeptuneNode,
  isDark: boolean,
  pal: ReturnType<typeof neptunePalette> = neptunePalette(isDark),
): RuleNodeColors {
  const c = pal[n.type] || pal.default;
  const props = n.props || {};
  const inactive =
    (n.type === 'JoinPath' || n.type === 'IdentityRule') &&
    isInactiveStatus(props.effective_status ?? props.status);
  if (inactive) {
    return isDark
      ? { bg: '#111827', border: '#4b5563', textColor: '#6b7280', inactive, tooltipPrefix: INACTIVE_RULE_NOTE }
      : { bg: '#f3f4f6', border: '#9ca3af', textColor: '#6b7280', inactive, tooltipPrefix: INACTIVE_RULE_NOTE };
  }
  const base = { bg: c.bg, border: c.border, textColor: c.text, inactive: false, tooltipPrefix: '' };
  if (n.type !== 'JoinPath' || !n.props) return base;
  const method = (props.method as string) || 'foreign_key_match';
  const fromSys = (props.from_system as string) || '';
  const toSys = (props.to_system as string) || '';
  if (method === 'user_confirmed') {
    // Self-learned: green
    return isDark
      ? { ...base, bg: '#1a3a2a', border: '#22c55e', textColor: '#86efac' }
      : { ...base, bg: '#dcfce7', border: '#22c55e', textColor: '#166534' };
  }
  if (fromSys && toSys && fromSys !== toSys) {
    // Cross-system: bright orange
    return isDark
      ? { ...base, bg: '#3b2a1a', border: '#f59e0b', textColor: '#fcd34d' }
      : { ...base, bg: '#fef3c7', border: '#f59e0b', textColor: '#92400e' };
  }
  // Internal FK: muted gray
  return isDark
    ? { ...base, bg: '#1e293b', border: '#475569', textColor: '#94a3b8' }
    : { ...base, bg: '#e2e8f0', border: '#64748b', textColor: '#334155' };
}

function renderNeptuneGraph(data: { nodes: NeptuneNode[]; edges: NeptuneEdge[] }, view?: KgViewState): void {
  const container = document.getElementById('cy-neptune-graph');
  if (!container) return;
  const isDark = isDarkTheme();

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const elements: any[] = [];

  // Nodes — theme-resolved palette (arch-visual-polish): light mode used to
  // render the DARK swatches (navy/forest fills + pale 300-level text) on the
  // light canvas; neptunePalette flips to light fills + 700/800-level text.
  const pal = neptunePalette(isDark);
  for (const n of data.nodes) {
    const { bg, border, textColor, inactive, tooltipPrefix } = ruleNodeColors(n, isDark, pal);
    const tooltip =
      tooltipPrefix +
      (Object.entries(n.props || {})
        .map(([k, v]) => `${k}: ${v}`)
        .join('\n') || `[${n.type}] ${n.label}`);
    elements.push({ data: { id: n.id, label: n.label, type: n.type, bg, border, textColor, tooltip, inactive } });
  }

  // Edges
  for (const e of data.edges) {
    elements.push({
      data: { id: `${e.source}-${e.label}-${e.target}`, source: e.source, target: e.target, label: e.label },
    });
  }

  if (cyNeptune) {
    cyNeptune.destroy();
    cyNeptune = null;
  }

  // Prefer the cola force-directed layout; degrade to built-in cose. For large
  // / "all" views, lay out concentrically by node TYPE (K4) — data-driven,
  // never keyed to a plant/system name.
  const useCola = ensureCola();
  const TYPE_RANK: Record<string, number> = {
    System: 6, Plant: 5, Concept: 4, SchemaEntry: 3, AssetModel: 3,
    JoinPath: 3, IdentityRule: 3, Field: 2, Asset: 2, ValueMapping: 2, AuthorityRule: 2,
    OnboardingLog: 1,
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let neptuneLayout: any;
  if (view?.mode === 'neighborhood') {
    // Expand-on-click: center the clicked node, ring the neighbors (moved
    // from the old in-place tap handler).
    const centerId = view.nodeId;
    neptuneLayout = {
      name: 'concentric',
      concentric: (cn: Cy) => (cn.id() === centerId ? 2 : 1),
      levelWidth: () => 1,
      minNodeSpacing: 60,
      animate: true,
      animationDuration: 300,
      fit: true,
      padding: 50,
    };
  } else if (data.nodes.length > 40) {
    neptuneLayout = {
      name: 'concentric',
      concentric: (n: Cy) => TYPE_RANK[n.data('type')] || 1,
      levelWidth: () => 1,
      minNodeSpacing: 22,
      avoidOverlap: true,
      animate: false,
      fit: true,
      padding: 50,
    };
  } else if (useCola) {
    neptuneLayout = {
      name: 'cola', animate: false, avoidOverlap: true, nodeSpacing: 14,
      edgeLength: 110, randomize: true, maxSimulationTime: 2500,
      convergenceThreshold: 0.01, handleDisconnected: true,
      nodeDimensionsIncludeLabels: true, fit: true, padding: 50,
    };
  } else {
    neptuneLayout = {
      name: 'cose', animate: false, randomize: true, nodeRepulsion: 12000,
      idealEdgeLength: 110, componentSpacing: 120, gravity: 0.2,
      numIter: 1200, fit: true, padding: 50,
    };
  }

  cyNeptune = cytoscape({
    container,
    elements,
    style: [
      { selector: 'node', style: {
        label: 'data(label)',
        'text-wrap': 'wrap',
        'text-max-width': '120px',
        'font-size': '11px',
        'font-family': 'Outfit, sans-serif',
        'text-valign': 'center',
        'text-halign': 'center',
        color: 'data(textColor)',
        'background-color': 'data(bg)',
        'border-width': 1.5,
        'border-color': 'data(border)',
        width: 100,
        height: 40,
        shape: 'round-rectangle',
        'overlay-opacity': 0,
      }},
      { selector: 'node[type="System"]', style: { width: 160, height: 60, 'font-size': '14px', 'font-weight': 700, 'border-width': 3.5, 'z-index': 30, 'text-outline-width': 2, 'text-outline-color': 'data(bg)' }},
      { selector: 'node[type="Concept"]', style: { shape: 'ellipse', width: 90, height: 40 }},
      { selector: 'node[type="Field"]', style: { width: 80, height: 30, 'font-size': '9px' }},
      { selector: 'node[type="IdentityRule"]', style: { shape: 'diamond', width: 110, height: 50 }},
      { selector: 'node[type="JoinPath"]', style: { shape: 'hexagon', width: 100, height: 45 }},
      { selector: 'node[?inactive]', style: { 'border-style': 'dashed', opacity: 0.6 }},
      { selector: 'edge', style: {
        width: 1,
        // Light lines were #cbd5e1 (~1.5:1 on the light --bg); #94a3b8 lines
        // with #64748b arrows keep the hairline feel while staying visible.
        'line-color': isDark ? '#334155' : '#94a3b8',
        'target-arrow-color': isDark ? '#475569' : '#64748b',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'arrow-scale': 0.6,
        label: 'data(label)',
        'font-size': '8px',
        // Light edge labels were #94a3b8 (2.6:1 on the #f8fafc label
        // background) — #64748b reads 4.5:1 there and matches dark's value.
        color: '#64748b',
        'text-rotation': 'autorotate',
        'text-background-color': isDark ? '#0d1117' : '#f8fafc',
        'text-background-opacity': 0.8,
        'text-background-padding': '2px',
      }},
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#f97316', 'overlay-opacity': 0.1 }},
    ],
    layout: neptuneLayout,
    wheelSensitivity: 0.3,
  });

  // Tooltip on hover
  cyNeptune.on('mouseover', 'node', (e: { target: Cy }) => {
    const node = e.target;
    const tooltip = node.data('tooltip');
    if (!tooltip) return;
    let tip = document.getElementById('neptune-tooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'neptune-tooltip';
      tip.style.cssText =
        'position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:0.5rem 0.75rem;font-size:0.7rem;font-family:IBM Plex Mono,monospace;color:var(--text2);z-index:9999;pointer-events:none;max-width:300px;white-space:pre-wrap;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
      document.body.appendChild(tip);
    }
    tip.textContent = `[${node.data('type')}] ${node.data('label')}\n${tooltip}`;
    tip.style.display = 'block';
  });
  cyNeptune.on('mouseout', 'node', () => {
    const tip = document.getElementById('neptune-tooltip');
    if (tip) tip.style.display = 'none';
  });

  // Click handler — visual graph traversal: re-render the graph centered on
  // the clicked node, routed through setCurrentView so the legend counts and
  // breadcrumb describe the neighborhood (UI improvement D).
  cyNeptune.on('tap', 'node', async (e: { target: Cy }) => {
    const node = e.target;
    const nodeId = node.id();
    try {
      const data = await requestJson<NeptuneNeighborsResponse>(
        `/api/neptune/node/${encodeURIComponent(nodeId)}/neighbors`,
      );
      if (!data.node) return;

      // Remove old edges panel if it exists
      const oldPanel = document.getElementById('neptune-edges-panel');
      if (oldPanel) oldPanel.remove();

      const props = data.node.properties || {};
      setCurrentView(neighborsToDataset(nodeId, data), {
        mode: 'neighborhood',
        nodeId,
        nodeLabel: String(props.name || props.id || nodeId),
        hops: 1,
      });

      // Update KG selection details panel — publish to the selection store;
      // <KgSelectionInfo/> renders it with JSX-escaped text (jsx-migration W6
      // — was an unescaped selPanel.innerHTML= interpolation).
      {
        const dn = data.node;
        const label = (dn.labels || [])[0] || 'Unknown';
        kgSelectionStore.set({
          label,
          name: String(props.name || props.id || nodeId),
          props: Object.entries(props)
            .filter(([k]) => k !== 'name')
            .map(([k, v]): [string, string] => [k, String(v).slice(0, 100)]),
          outgoing: (data.outgoing || []).length,
          incoming: (data.incoming || []).length,
        });
      }
      // Trigger AI analysis
      requestKgAiAnalysis(data.node);
    } catch (err) {
      console.error('Failed to load node neighbors:', err);
    }
  });
  cyNeptune.on('mousemove', (e: { originalEvent: MouseEvent }) => {
    const tip = document.getElementById('neptune-tooltip');
    if (tip && tip.style.display !== 'none') {
      tip.style.left = e.originalEvent.clientX + 12 + 'px';
      tip.style.top = e.originalEvent.clientY + 12 + 'px';
    }
  });

  // Legend — publish the rendered node types to the legend store;
  // <GraphLegend/> (Legends.tsx) renders them into the KG-tab legend host
  // (#neptune-legend). Labels come from graph data — JSX escapes them.
  neptuneLegendStore.set([...new Set(data.nodes.map((n) => n.type))]);
}

// Module init (once): renderNeptuneGraph is the render sink for every view
// published through kg-view-store's setCurrentView/toggleType.
registerKgRenderer((visible, view) => renderNeptuneGraph(visible, view));

export function resizeNeptune(): void {
  if (cyNeptune) {
    requestAnimationFrame(() => {
      if (cyNeptune) cyNeptune.resize();
    });
  }
}

export function destroyNeptuneGraph(): void {
  if (cyNeptune) {
    try {
      cyNeptune.destroy();
    } catch {
      /* already destroyed */
    }
    cyNeptune = null;
  }
  _neptuneData = null;
  neptuneLegendStore.set([]);
  kgAnalysisStore.set({ status: 'idle' });
  resetKgViewState();
}
