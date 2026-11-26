// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* System Map (static topology) — initSystemMap / _startMapFlow /
 * _stopMapFlow / _stopAllMapFlows and the shared cola/popper registration.
 * Operates on the #cy-map container rendered by
 * ArchitectureView; module-level singleton. */
import cytoscape from 'cytoscape';
import cytoscapeCola from 'cytoscape-cola';
import cytoscapePopper from 'cytoscape-popper';
import tippy from 'tippy.js';

import { fetchSystems } from '../../api/systems-query';
import {
  HIGHLIGHT_DARK,
  HIGHLIGHT_LIGHT,
  ISA_LABEL,
  ISA_LEVEL,
  isDarkTheme,
  MAP_COLORS,
  MAP_COLORS_LIGHT,
} from './colors';
import {
  mapActiveEdges,
  mapTraversedEdges,
  registerMapInstance,
  startMarchingAnts,
  stopMarchingAnts,
  antsRunning,
} from './flow-anim';
import { safeFit } from './cy-guards';
import { createStore } from '../../lib/store';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Cy = any;

/** One legend row for the System Map legend (#map-legend). `swatchClass`
 * renders the pre-init class-based swatches; `bg`/`border` carry the palette
 * colors initSystemMap resolves (jsx-migration W2.3 — was legend.innerHTML=).
 * <MapLegend/> (views/architecture/Legends.tsx) renders the store. */
export interface MapLegendItem {
  label: string;
  swatchClass?: string;
  bg?: string;
  border?: string;
}

/** Initial (pre-init) legend rows — the old MAP_LEGEND_INITIAL markup. */
const MAP_LEGEND_INITIAL_ITEMS: MapLegendItem[] = [
  { label: 'Mfg Intelligence', swatchClass: 'question' },
  { label: 'Knowledge Graph', swatchClass: 'system' },
  { label: 'Level 4 (ERP/PLM)', swatchClass: 'entity' },
  { label: 'Level 3 (MES/CMMS)', swatchClass: 'finding' },
  { label: 'Level 2 (Historian)' },
];

export const mapLegendStore = createStore<MapLegendItem[]>(MAP_LEGEND_INITIAL_ITEMS);

/** True only while the very FIRST map build's /api/systems fetch runs (no map
 * exists yet, so the canvas would otherwise be blank with no indicator).
 * Rendered by <MapLoadingOverlay/> (ArchitectureView) — the same overlay
 * pattern as the Knowledge Graph tab's #cy-neptune-empty. Revisits are a
 * keep-previous silent reconcile and never set it. */
export const mapLoadingStore = createStore<boolean>(false);

let cyMap: Cy = null;
let mapInitialized = false;
/** Last rendered systems payload + theme — a re-activation whose data (and
 * theme) are unchanged keeps the existing instance (zoom/pan and the
 * chat-produced traversed-edge highlights survive a navigate-away-and-back). */
let _lastMapKey = '';
const _systemTypeMap: Record<string, string> = {}; // tool name → system_type

registerMapInstance(() => cyMap);

// Register the cola force-directed layout extension exactly once (the npm
// adapter replaces the old vendored auto-attach global `cytoscapeCola`).
// Registering twice emits an "already exists" warning; returns true when cola
// is available (graceful degrade to built-in cose when missing).
let _colaRegistered = false;
export function ensureCola(): boolean {
  if (_colaRegistered) return true;
  if (typeof cytoscapeCola !== 'undefined') {
    try {
      cytoscape.use(cytoscapeCola);
    } catch {
      /* already registered */
    }
    _colaRegistered = true;
    return true;
  }
  return false;
}

// cytoscape-popper: the old vendored UMD auto-registered on load; the npm
// package must be cytoscape.use()'d once so node.popperRef() exists.
let _popperRegistered = false;
export function ensurePopper(): boolean {
  if (_popperRegistered) return true;
  if (typeof cytoscapePopper !== 'undefined') {
    try {
      cytoscape.use(cytoscapePopper);
    } catch {
      /* already registered */
    }
    _popperRegistered = true;
    return true;
  }
  return false;
}

export function getCyMap(): Cy {
  return cyMap;
}

interface SystemEntry {
  id: string;
  name?: string;
  type?: string;
  plant?: string;
  status?: string;
  connection_type?: string;
}

/** Port of initSystemMap — refreshed on every entry so newly onboarded
 * systems appear, but as a KEEP-PREVIOUS SILENT RECONCILE (same pattern as
 * proposals.ts): the existing map keeps rendering while /api/systems is
 * fetched in the background, and the instance is only destroyed + rebuilt
 * when fresh data actually arrives AND differs from what is rendered. A
 * failed refresh keeps the existing map; unchanged data keeps the existing
 * instance (zoom/pan + traversed-edge chat history survive re-activation). */
export async function initSystemMap(): Promise<void> {
  // FIRST-LOAD indicator: when no map has ever been built, the fetch-first
  // reconcile below would leave the canvas blank with no feedback — show the
  // loading overlay for that case only. Every later call has the previous
  // map rendered, so it stays silent (no re-blanking on revisit). The
  // finally clears it on every exit path (success, failure, missing host).
  const firstLoad = !(mapInitialized && cyMap);
  if (firstLoad) mapLoadingStore.set(true);
  try {
    await _initSystemMapInner();
  } finally {
    mapLoadingStore.set(false);
  }
}

async function _initSystemMapInner(): Promise<void> {
  // Fetch FIRST — never blank the previous map while loading.
  let systems: SystemEntry[] = [];
  let fetchOk = false;
  try {
    const d = await fetchSystems();
    systems = (d.systems || []) as SystemEntry[];
    fetchOk = true;
  } catch (e) {
    console.error('System map: fetch failed', e);
    // Refresh failed but a map is already rendered — keep it as-is.
    if (mapInitialized && cyMap) return;
    // First load failed: fall through and build the static hub (orchestrator
    // + knowledge graph) with no system spokes, as before.
  }

  // Unchanged systems + theme ⇒ keep the existing instance untouched.
  const mapKey = JSON.stringify(systems) + '|' + (isDarkTheme() ? 'dark' : 'light');
  if (fetchOk && mapInitialized && cyMap && mapKey === _lastMapKey) return;
  _lastMapKey = mapKey;

  if (mapInitialized && cyMap) {
    try {
      cyMap.destroy();
    } catch {
      /* already destroyed */
    }
  }
  mapInitialized = true;

  // Mount on the dedicated #cy-map-graph host (cy.destroy() removes every
  // child of its container; #cy-map also holds the React-rendered legend).
  const container =
    document.getElementById('cy-map-graph') ?? document.getElementById('cy-map');
  if (!container) return;

  const isDark = isDarkTheme();
  const txt = isDark ? '#e2e8f0' : '#1e293b';
  // Light edges were #cbd5e1 (~1.5:1 on the light --bg — barely visible
  // hairlines); #64748b clears the 3:1 non-text minimum (4.5:1). Dark kept.
  const edgeC = isDark ? '#334155' : '#64748b';
  const pal = isDark ? MAP_COLORS : MAP_COLORS_LIGHT;

  ensurePopper();

  cyMap = cytoscape({
    container,
    style: [
      { selector: 'node', style: {
        label: 'data(label)', 'text-wrap': 'wrap', 'text-max-width': '140px',
        'font-size': '10px', 'font-family': 'Outfit, sans-serif',
        'text-valign': 'center', 'text-halign': 'center', color: txt,
        'background-color': 'data(bg)', 'border-width': 2, 'border-color': 'data(border)',
        width: 130, height: 55, shape: 'round-rectangle', 'overlay-opacity': 0,
      }},
      { selector: 'node[tier="orchestrator"]', style: { 'font-size': '12px', 'font-weight': 700, width: 160, height: 60 }},
      { selector: 'node[tier="neptune"]', style: { 'font-size': '11px', 'font-weight': '600', width: 150, height: 55, shape: 'ellipse' }},
      { selector: 'edge', style: {
        width: 1.5, 'line-color': edgeC, 'target-arrow-color': edgeC,
        'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'arrow-scale': 0.8,
        'line-dash-pattern': [6, 4], 'line-dash-offset': 0, 'line-style': 'dashed',
        label: 'data(label)', 'font-size': '7px', color: isDark ? '#64748b' : '#475569',
        'text-rotation': 'autorotate', 'text-margin-y': -8, 'font-family': 'IBM Plex Mono, monospace',
      }},
      { selector: 'node.dimmed', style: { opacity: 0.15 }},
      { selector: 'edge.dimmed', style: { opacity: 0.08 }},
      { selector: 'node.highlighted', style: { opacity: 1, 'border-width': 3 }},
      { selector: 'edge.highlighted', style: { opacity: 1, width: 2.5, 'line-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT, 'target-arrow-color': isDark ? HIGHLIGHT_DARK : HIGHLIGHT_LIGHT }},
      { selector: 'edge.flowing', style: {
        'line-style': 'dashed', 'line-dash-pattern': [8, 6], width: 2.5,
        'line-color': HIGHLIGHT_DARK, 'target-arrow-color': HIGHLIGHT_DARK,
      }},
      { selector: 'edge.traversed', style: {
        'line-style': 'dashed', 'line-dash-pattern': [8, 6], width: 2,
        'line-color': isDark ? '#58a6ff' : HIGHLIGHT_LIGHT,
        'target-arrow-color': isDark ? '#58a6ff' : HIGHLIGHT_LIGHT,
      }},
      { selector: 'node.querying', style: {
        'border-width': 3, 'border-color': HIGHLIGHT_DARK,
        'overlay-opacity': 0.15, 'overlay-color': HIGHLIGHT_DARK,
      }},
    ],
    layout: { name: 'preset' },
    minZoom: 0.4,
    maxZoom: 2.5,
    wheelSensitivity: 0.3,
  });

  // Radial hub — Mfg Intelligence at center, everything else around it
  const W = 900;
  const cY = 300;
  const cOrch = pal.orchestrator;
  cyMap.add({ data: { id: 'map-orch', label: 'AI\nOrchestrator', tier: 'orchestrator', bg: cOrch.bg, border: cOrch.border, detail: 'AI Orchestrator — queries systems directly and consults the knowledge graph for identity resolution.' }, position: { x: W / 2, y: cY } });

  // Knowledge Graph at 12 o'clock, systems fill the rest of the circle
  const cNep = pal.neptune;
  interface Spoke {
    id: string; label: string; tier: string; bg: string; border: string;
    detail: string; edgeLabel: string; sysId?: string;
  }
  const spokes: Spoke[] = [
    { id: 'map-neptune', label: 'Knowledge\nGraph', tier: 'neptune', bg: cNep.bg, border: cNep.border, detail: 'Graph database — concepts, identity rules, join paths, value mappings.', edgeLabel: 'resolves via' },
  ];
  const connected = systems.filter((s) => s.status === 'connected');
  const sorted = connected.slice().sort((a, b) => (ISA_LEVEL[b.type || ''] || 3) - (ISA_LEVEL[a.type || ''] || 3));
  sorted.forEach((s) => {
    const toolName = 'query_' + s.id.replace(/-/g, '_');
    _systemTypeMap[toolName] = s.type || '';
    const lvl = ISA_LEVEL[s.type || ''] || 3;
    const c = pal[lvl];
    const status = s.status === 'connected' ? '● Connected' : '○ Registered';
    spokes.push({ id: `map-${s.id}`, label: `${s.name}\n${s.type} · ${s.plant}`, tier: String(lvl), sysId: s.id, bg: c.bg, border: c.border, detail: `${s.name}\nType: ${s.type}\nISA-95: ${ISA_LABEL[lvl]}\nPlant: ${s.plant}\nConnection: ${s.connection_type}\nStatus: ${status}`, edgeLabel: '' });
  });
  const radius = Math.min(W * 0.4, 320);
  spokes.forEach((s, i) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / spokes.length;
    const x = W / 2 + radius * Math.cos(angle);
    const y = cY + radius * Math.sin(angle);
    cyMap.add({ data: { id: s.id, label: s.label, tier: s.tier, sysId: s.sysId, bg: s.bg, border: s.border, detail: s.detail }, position: { x, y } });
    cyMap.add({ data: { id: `e-orch-${s.id}`, source: 'map-orch', target: s.id, label: s.edgeLabel } });
  });

  // Tool events can arrive while the first /api/systems fetch is still in
  // flight. Flow state is recorded independently of the Cytoscape instance;
  // replay it after every build so those starts are not lost (W8/W9 async map
  // initialization regression).
  mapActiveEdges.forEach((edgeId) => {
    const edge = cyMap.getElementById(edgeId);
    if (!edge.length) return;
    edge.addClass('flowing');
    const nodeId = edgeId.replace(/^e-orch-/, '');
    const node = cyMap.getElementById(nodeId);
    if (node.length) node.addClass('querying');
  });
  mapTraversedEdges.forEach((edgeId) => {
    const edge = cyMap.getElementById(edgeId);
    if (edge.length) edge.addClass('traversed');
  });
  if ((mapActiveEdges.size > 0 || mapTraversedEdges.size > 0) && !antsRunning()) {
    startMarchingAnts();
  }

  safeFit(cyMap, null, 40);

  // Hover dimming
  cyMap.on('mouseover', 'node', (evt: { target: Cy }) => {
    const neighborhood = evt.target.closedNeighborhood();
    cyMap.elements().not(neighborhood).addClass('dimmed');
    neighborhood.addClass('highlighted');
  });
  cyMap.on('mouseout', 'node', () => cyMap.elements().removeClass('dimmed highlighted'));

  // Tippy tooltips
  if (ensurePopper()) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let tip: any = null;
    cyMap.on('mouseover', 'node', (evt: { target: Cy }) => {
      const d = evt.target.data('detail');
      if (!d || tip) return;
      const ref = evt.target.popperRef();
      tip = tippy(document.createElement('div'), {
        getReferenceClientRect: ref.getBoundingClientRect,
        content: d,
        trigger: 'manual',
        placement: 'right',
        arrow: true,
        theme: 'cyto',
        maxWidth: 280,
        appendTo: document.body,
      });
      tip.show();
    });
    cyMap.on('mouseout', 'node', () => {
      if (tip) {
        tip.destroy();
        tip = null;
      }
    });
  }

  // Update legend colors — publish to the legend store; <MapLegend/>
  // (Legends.tsx) renders it (jsx-migration W2.3 — was legend.innerHTML=).
  mapLegendStore.set(
    (
      [
        ['orchestrator', 'Mfg Intelligence'],
        ['neptune', 'Knowledge Graph'],
        [4, 'Level 4 (ERP/PLM)'],
        [3, 'Level 3 (MES/CMMS)'],
        [2, 'Level 2 (Historian)'],
      ] as [string | number, string][]
    ).map(([k, lbl]) => {
      const c = pal[k];
      return { label: lbl, bg: c.bg, border: c.border };
    }),
  );
}

/* System map animation — marching ants on active edges.
 * Delegates to the pure, unit-tested helper (lib/architecture-util.js). */
function _mapToolToSysId(toolName: string): string | null {
  const fn = (globalThis as Record<string, unknown>).mapToolToSystemId;
  return typeof fn === 'function' ? (fn as (t: string) => string | null)(toolName) : null;
}

function _mapTargetNodeId(toolName: string): string | null {
  // Gateway names the shared graph target `digital-thread___explore_graph`;
  // classify the tool half before generic gateway-prefix → system-id mapping.
  if (toolName === 'explore_graph' || toolName.endsWith('___explore_graph')) {
    return 'map-neptune';
  }
  const sysId = _mapToolToSysId(toolName);
  return sysId ? `map-${sysId}` : null;
}

export function startMapFlow(toolName: string): void {
  const targetNodeId = _mapTargetNodeId(toolName);
  if (targetNodeId) {
    const edgeId = `e-orch-${targetNodeId}`;
    // Record first: cyMap is created asynchronously after /api/systems returns.
    // The build path replays this state when the instance becomes available.
    mapTraversedEdges.delete(edgeId);
    mapActiveEdges.add(edgeId);
    if (cyMap) {
      const edge = cyMap.getElementById(edgeId);
      if (edge.length) edge.addClass('flowing');
      const node = cyMap.getElementById(targetNodeId);
      if (node.length) node.addClass('querying');
    }
  }
  if (!antsRunning()) startMarchingAnts();
}

export function stopMapFlow(toolName: string): void {
  const targetNodeId = _mapTargetNodeId(toolName);
  if (targetNodeId) {
    const edgeId = `e-orch-${targetNodeId}`;
    mapActiveEdges.delete(edgeId);
    mapTraversedEdges.add(edgeId);
    if (cyMap) {
      cyMap.getElementById(edgeId).removeClass('flowing').addClass('traversed');
      const node = cyMap.getElementById(targetNodeId);
      if (node.length) {
        node.removeClass('querying');
        node.removeStyle('overlay-opacity'); // clear the ants-loop pulse
      }
    }
  }
}

export function stopAllMapFlows(): void {
  stopMarchingAnts();
  mapActiveEdges.clear();
  mapTraversedEdges.clear();
  if (cyMap) {
    cyMap.edges().removeClass('flowing');
    cyMap.nodes().removeClass('querying').removeStyle('overlay-opacity');
    // traversed class stays for visual history — just stops animating
  }
}

/** Clear traversed history (user_msg starts a fresh question). */
export function clearTraversed(): void {
  if (cyMap) cyMap.edges().removeClass('traversed');
}

export function resizeMap(): void {
  if (cyMap) {
    requestAnimationFrame(() => {
      if (!cyMap) return;
      cyMap.resize();
      safeFit(cyMap, null, 40);
    });
  }
}

export function destroySystemMap(): void {
  if (cyMap) {
    try {
      cyMap.destroy();
    } catch {
      /* already destroyed */
    }
    cyMap = null;
  }
  mapInitialized = false;
  _lastMapKey = '';
}
