// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* ArchitectureView — the architecture view markup
 * plus switchArchTab and the view-level init.
 * All contractual ids/classes are preserved: .arch-tabs/.arch-tab,
 * #active-query, .arch-split, #cy-map/#map-legend, #cy-container/#tool-hud/
 * #arch-empty/#arch-legend, #cy-neptune(-graph|-empty)/#neptune-legend,
 * #reasoning-trace/#question-timeline/#trace-entries, #kg-explorer-panel/
 * #neptune-node-filter/#neptune-edge-filter/#kg-selection-info/#kg-ai-analysis,
 * #proposed-mappings-panel, the #self-learning-… ids, #learned-guidance-cards
 * and the ud-* user-define inputs + datalists.
 *
 * Graph content lives in the session-persistent arch-store (fed by the chat
 * bridge); this component owns the cytoscape instances via the imperative
 * modules (system-map / inv-graph / neptune-graph) and re-renders trace/HUD
 * from store state.
 *
 * Task 1H — DELIBERATE DIVERGENCE from the retired legacy UI, whose
 * switchArchTab called
 * resetArchitectureState() on any switch to a non-investigation tab. That
 * wiped a LIVE investigation mid-stream (user_msg already consumed → the
 * question node/title were erased, the placeholder re-appeared over a
 * repopulating rootless graph). The SPA does NOT port that reset: the graph
 * is session-scoped, tab switches only toggle visibility, and the reset
 * owners remain '+ New' (startNewSession) and the dashboard demo. */
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import { archPlaceholderHidden, archStore } from '../../lib/arch-store';
import {
  consumePendingArchTab,
  consumePendingArchTabFocus,
  focusArchTabSoon,
} from './arch-tab-bridge';
import { clearCorrelation } from './correlation-store';
import { fillVersionBadge } from '../../lib/version-badge';
import {
  applyQuestionFilter,
  destroyInvGraph,
  fitInvGraph,
  initArchitecture,
  resizeInvGraph,
  syncFromStore,
} from './inv-graph';
import { loadNeptuneGraph, resizeNeptune } from './neptune-graph';
import { KgSelectionInfo } from './KgSelectionInfo';
import { KgTypeLegend } from './KgTypeLegend';
import { KgNodeSearch } from './KgNodeSearch';
import { KgViewBreadcrumb } from './KgViewBreadcrumb';
import { ArchLegend, GraphLegend, MapLegend } from './Legends';
import { NeptuneAnalysisPanel } from './NeptuneAnalysisPanel';
import { LearnedGuidanceList, PendingBadge, ProposalsList } from './ProposalsPanel';

import {
  acceptAllPendingMappings,
  clearAllLearnings,
  clearRejectedMappings,
  loadLearnedGuidance,
  loadProposedMappings,
  populateUserDefineDatalists,
  registerProposalGlobals,
  resetAllMappings,
  submitUserDefinedMapping,
} from './proposals';
import {
  clearTraversed,
  destroySystemMap,
  initSystemMap,
  mapLoadingStore,
  resizeMap,
  startMapFlow,
  stopAllMapFlows,
  stopMapFlow,
} from './system-map';
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import ToolHud from './ToolHud';
import TraceEntries from './TraceEntries';

type ArchSnapshot = ReturnType<typeof archStore.getState>;

const subscribeArch = (listener: () => void) => archStore.subscribe(listener);

/** useSyncExternalStore selector with an optional semantic key. Arch snapshots
 * copy trace/HUD arrays on every notify, so their JSON key preserves the prior
 * selected reference when an unrelated graph slice changes. */
function useArchSlice<S>(
  selector: (state: ArchSnapshot) => S,
  keyOf: (value: S) => unknown = (value) => value,
): S {
  const cache = useRef<{ key: unknown; value: S } | null>(null);
  const getSelected = () => {
    const value = selector(archStore.getState());
    const key = keyOf(value);
    if (cache.current && Object.is(cache.current.key, key)) return cache.current.value;
    cache.current = { key, value };
    return value;
  };
  return useSyncExternalStore(subscribeArch, getSelected, getSelected);
}

type ArchTab = 'map' | 'investigation' | 'neptune' | 'proposed';
type MobilePane = 'graph' | 'secondary';

const ARCH_MOBILE_QUERY = '(max-width: 760px)';

/** First-load overlay for the System Map — visible only while the very first
 * /api/systems fetch runs (system-map.ts mapLoadingStore; the fetch-first
 * silent reconcile otherwise left a dead-blank canvas on first visit).
 * Revisits keep the previous map rendered and never show this. */
function MapLoadingOverlay() {
  const loading = useStore(mapLoadingStore);
  if (!loading) return null;
  return (
    <div className="cy-map-loading" id="cy-map-loading" role="status">
      Loading system map…
    </div>
  );
}

const ARCH_TABS: { id: ArchTab; labelKey: string }[] = [
  { id: 'map', labelKey: 'arch.systemMap' },
  { id: 'investigation', labelKey: 'arch.investigation' },
  { id: 'neptune', labelKey: 'arch.knowledgeGraph' },
  { id: 'proposed', labelKey: 'arch.selfLearning' },
];

/* Legends are React components over external stores (Legends.tsx) — the
 * imperative graph modules publish items (mapLegendStore/neptuneLegendStore)
 * instead of writing legend.innerHTML (jsx-migration W2.3). */

export const NODE_FILTERS: { value: string; label: string }[] = [
  { value: 'System', label: 'System' },
  { value: 'Plant', label: 'Plant' },
  { value: 'Concept', label: 'Concept' },
  { value: 'SchemaEntry', label: 'Table' },
  { value: 'AssetModel', label: 'Asset Model' },
  { value: 'Field', label: 'Field' },
  { value: 'Asset', label: 'Asset' },
  { value: 'IdentityRule', label: 'Identity Rule' },
  { value: 'JoinPath', label: 'Join Path' },
  { value: 'ValueMapping', label: 'Value Mapping' },
  { value: 'AuthorityRule', label: 'Authority Rule' },
  { value: 'OnboardingLog', label: 'Onboarding Log' },
];

const EDGE_FILTERS: { value: string; label: string }[] = [
  { value: 'HAS_SCHEMA', label: 'Has Schema' },
  { value: 'HAS_FIELD', label: 'Has Field' },
  { value: 'MAPS_TO', label: 'Maps To' },
  { value: 'HAS_CHILD', label: 'Has Child' },
  { value: 'HAS_PARENT', label: 'Has Parent' },
  { value: 'INSTANCE_OF', label: 'Instance Of' },
  { value: 'FROM_SYSTEM', label: 'From System' },
  { value: 'TO_SYSTEM', label: 'To System' },
  { value: 'LINKS_CONCEPT', label: 'Links Concept' },
  { value: 'LOCATED_AT', label: 'Located At' },
  { value: 'APPLIES_TO', label: 'Applies To' },
];

export interface ArchitectureViewProps {
  /** True while the Architecture view is the active shell view. The system map
   * refreshes on every (re-)activation so newly onboarded systems appear —
   * matching the old switchView('architecture') → initSystemMap() call —
   * but keeps the previous map rendered until fresh data arrives (silent
   * reconcile: no blanking refetch on navigate-away-and-back). */
  active?: boolean;
}

export default function ArchitectureView({ active = true }: ArchitectureViewProps) {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<ArchTab>('map');
  const [narrowLayout, setNarrowLayout] = useState(false);
  const [mobilePane, setMobilePane] = useState<MobilePane>('graph');
  // Trace pane hidden-by-tab-switch flag (original: switchArchTab adds
  // `hidden`, the next user_msg removes it again).
  const [traceHiddenByTab, setTraceHiddenByTab] = useState(false);
  const filterRegistered = useRef(false);

  const activeQuestion = useArchSlice((state) => state.activeQuestion);
  const trace = useArchSlice((state) => state.trace, JSON.stringify);
  const hud = useArchSlice((state) => state.hud, JSON.stringify);
  const activeQuestionFilter = useArchSlice((state) => state.activeQuestionFilter);
  const questionCounter = useArchSlice((state) => state.questionCounter);
  const placeholderHidden = useArchSlice(archPlaceholderHidden);


  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia(ARCH_MOBILE_QUERY);
    const apply = () => {
      setNarrowLayout(query.matches);
      if (query.matches) setMobilePane('graph');
    };
    apply();
    query.addEventListener?.('change', apply);
    return () => query.removeEventListener?.('change', apply);
  }, []);

  /* One-time init: window globals for generated card HTML, inv-graph instance,
   * store subscriptions, version badge, resize bindings. */
  useEffect(() => {
    registerProposalGlobals();
    if (!filterRegistered.current) {
      // filterToQuestion is referenced by legacy inline handlers (intelligence
      // summary links etc.) — keep the window-global contract.
      (window as unknown as Record<string, unknown>).filterToQuestion = (q: number | null) =>
        archStore.setQuestionFilter(q);
      // switchArchTab is a documented cross-module shim (thread-clicks.ts,
      // dashboard demo.ts, onboard ResolveExtras.tsx call it via `?.`) —
      // register it here (jsx-migration W6; the closure only captures stable
      // setters, so the mount-time registration stays current).
      (window as unknown as Record<string, unknown>).switchArchTab = (tab: string) =>
        switchArchTab(tab as ArchTab);
      filterRegistered.current = true;
    }

    initArchitecture();
    syncFromStore(archStore.getState()); // replay events streamed pre-mount

    // Apply any pre-mount switchArchTab request (dashboard demo calls
    // requestArchTab('investigation') synchronously after switchView — on
    // the FIRST visit this view is not mounted yet, so the window global is
    // still unset; see arch-tab-bridge.ts).
    const pendingTab = consumePendingArchTab();
    if (pendingTab) switchArchTab(pendingTab as ArchTab);
    const pendingFocusTab = consumePendingArchTabFocus();
    if (pendingFocusTab) focusArchTabSoon(pendingFocusTab);

    const unsubState = archStore.subscribe((s) => syncFromStore(s));
    const unsubEvents = archStore.subscribeEvents((type, data) => {
      // System-map animations — marching ants on active edges.
      if (type === 'tool_start') startMapFlow(String(data.tool ?? ''));
      else if (type === 'tool_result') stopMapFlow(String(data.tool ?? ''));
      else if (type === 'user_msg') {
        // Reset traversed edges on new question + refit for the trace panel.
        stopAllMapFlows();
        clearTraversed();
        resizeMap();
        setTraceHiddenByTab(false);
        // A new question invalidates the selected call — clear the
        // cross-surface correlation highlight.
        clearCorrelation();
      } else if (type === 'done' || type === 'reset') {
        stopAllMapFlows();
        if (type === 'reset') clearCorrelation();
      }
    });

    // Fetch and display version from ECS task metadata (architecture.js init).
    // Idempotent repeat of the AppShell boot fill — kept for direct-landing
    // timing (shared helper in lib/version-badge.ts).
    fillVersionBadge();

    // Re-fit the investigation graph when the viewport (or chat sidebar)
    // changes size — window resize AND container resize, debounced 200ms.
    let resizeTimer: ReturnType<typeof setTimeout> | null = null;
    const onResize = () => {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => resizeInvGraph(), 200);
    };
    window.addEventListener('resize', onResize);
    let ro: ResizeObserver | null = null;
    const invContainer = document.getElementById('cy-container');
    if (invContainer && typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(onResize);
      ro.observe(invContainer);
    }

    return () => {
      unsubState();
      unsubEvents();
      window.removeEventListener('resize', onResize);
      if (resizeTimer) clearTimeout(resizeTimer);
      if (ro) ro.disconnect();
      destroyInvGraph();
      destroySystemMap();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* Refresh the system map on every activation (newly onboarded systems).
   * initSystemMap is a keep-previous silent reconcile: the existing map keeps
   * rendering while /api/systems loads, and is only rebuilt when the data
   * changed — a navigate-away-and-back never blanks the graph. */
  useEffect(() => {
    if (active) {
      initSystemMap();
      if (activeTab === 'investigation') fitInvGraph();
      if (activeTab === 'neptune') resizeNeptune();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  /* Port of switchArchTab — per-tab activation work. Display toggling is
   * declarative below; the imperative loads/resizes happen here.
   * Task 1H: the legacy reset-on-tab-switch is deliberately NOT ported (see
   * the file header) — switching tabs only toggles visibility, so a live
   * investigation survives any mid-stream tab flick. */
  const switchArchTab = (tab: ArchTab) => {
    if (tab !== 'investigation') {
      setTraceHiddenByTab(true);
    }
    setMobilePane('graph');
    setActiveTab(tab);
    if (tab === 'map') {
      resizeMap();
    } else if (tab === 'neptune') {
      resizeNeptune();
      loadNeptuneGraph();
    } else if (tab === 'proposed') {
      loadProposedMappings();
      loadLearnedGuidance();
      populateUserDefineDatalists();
    } else {
      // Investigation tab
      setTraceHiddenByTab(false);
      fitInvGraph();
    }
  };

  /* Derived visibility (original: inline style/class toggling). */
  const showSplit = activeTab !== 'proposed';
  const showMap = activeTab === 'map';
  const showInv = activeTab === 'investigation';
  const showNeptune = activeTab === 'neptune';
  const traceVisible =
    !traceHiddenByTab && activeTab !== 'proposed' && (questionCounter > 0 || trace.length > 0);
  const hasMobilePaneChoice =
    narrowLayout &&
    (activeTab === 'neptune' || (activeTab === 'investigation' && traceVisible));
  const secondaryPaneLabel = activeTab === 'neptune' ? 'Graph explorer' : 'Investigation trace';

  /* Cytoscape sizes itself from its host box. Re-run the existing resize/refit
   * path after a breakpoint or pane change, once React/CSS has committed. */
  useEffect(() => {
    if (!active) return;
    const frame = requestAnimationFrame(() => {
      if (activeTab === 'map') resizeMap();
      else if (activeTab === 'investigation') fitInvGraph();
      else if (activeTab === 'neptune') resizeNeptune();
    });
    return () => cancelAnimationFrame(frame);
  }, [active, activeTab, mobilePane, narrowLayout, traceVisible]);

  return (
    <div className="arch-view-root">
      <div className="arch-tabs">
        {ARCH_TABS.map((tab) => (
          <button
            key={tab.id}
            className={`arch-tab${activeTab === tab.id ? ' active' : ''}`}
            data-arch-tab={tab.id}
            onClick={() => switchArchTab(tab.id)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>
      {/* Active-query bubble — this element lives OUTSIDE the tab-scoped
          containers (.arch-split / #proposed-mappings-panel), so store-only
          visibility made the question bleed onto every tab (reported on
          Self-Learning, whose panel starts at top:7rem — below the bubble).
          Scope it to the Investigation tab, the only tab it describes. */}
      <div
        id="active-query"
        className={`active-query${activeQuestion && activeTab === 'investigation' ? ' visible' : ''}`}
      >
        {activeQuestion}
      </div>
      {hasMobilePaneChoice && (
        <div className="arch-pane-toggle" role="group" aria-label="Architecture pane">
          <button
            type="button"
            className={mobilePane === 'graph' ? 'active' : ''}
            aria-pressed={mobilePane === 'graph'}
            onClick={() => setMobilePane('graph')}
          >
            Graph
          </button>
          <button
            type="button"
            className={mobilePane === 'secondary' ? 'active' : ''}
            aria-pressed={mobilePane === 'secondary'}
            onClick={() => setMobilePane('secondary')}
          >
            {secondaryPaneLabel}
          </button>
        </div>
      )}
      <div
        className={`arch-split arch-tab-${activeTab}${narrowLayout ? ` arch-narrow arch-mobile-pane-${mobilePane}` : ''}${hasMobilePaneChoice ? ' arch-has-pane-toggle' : ''}`}
        style={{ display: showSplit ? '' : 'none' }}
      >
        <div
          id="cy-map"
          className="arch-graph-pane"
          style={{ position: 'relative', display: showMap ? '' : 'none' }}
        >
          {/* Dedicated cytoscape host (same pattern as #cy-neptune-graph):
              cy.destroy() removes ALL children of its container, so mounting
              cytoscape directly on #cy-map would wipe the React-rendered
              legend (and React would never restore it — it still thinks the
              nodes are mounted). initSystemMap destroys + rebuilds whenever
              fresh systems data arrives, so this isolation is essential. */}
          <div id="cy-map-graph" style={{ position: 'absolute', inset: 0 }} />
          <MapLoadingOverlay />
          <MapLegend />
        </div>
        <div
          id="cy-container"
          className="arch-graph-pane"
          style={{
            position: 'relative',
            display: showInv ? '' : 'none',
            ...(traceVisible ? { flex: '0 0 65%' } : {}),
          }}
        >
          {/* Dedicated cytoscape host — cy.destroy() removes ALL children of
              its container (StrictMode dev double-mount runs the cleanup's
              destroyInvGraph), which silently wiped the React-rendered HUD,
              empty state and legend below when cytoscape mounted directly on
              #cy-container. Keep it FIRST so the absolutely-positioned
              overlays paint above the canvas, as in the original DOM. */}
          <div id="cy-inv-graph" style={{ position: 'absolute', inset: 0 }} />
          {/* Tool call HUD */}
          <ToolHud
            entries={hud}
            activeQuestionFilter={activeQuestionFilter}
            onFilterQuestion={(q) => archStore.setQuestionFilter(q)}
          />
          {/* Empty state — presence-driven (Task 1H): hidden as soon as the
              investigation has any content, even if a reset raced the stream. */}
          <div
            className="arch-empty"
            id="arch-empty"
            style={{ display: placeholderHidden ? 'none' : '' }}
          >
            <div className="arch-empty-title">{t('arch.emptyTitle')}</div>
            <div className="arch-empty-desc">{t('arch.emptyDesc')}</div>
            <div className="arch-empty-hint">{t('arch.emptyHint')}</div>
          </div>
          <ArchLegend />
        </div>
        <div
          id="cy-neptune"
          className="arch-graph-pane arch-neptune-pane"
          style={{
            display: showNeptune ? '' : 'none',
            position: 'absolute',
            top: 0,
            left: 0,
            bottom: 0,
            right: '35%',
            zIndex: 5,
            background: 'var(--bg)',
          }}
        >
          <div style={{ position: 'absolute', top: '0.5rem', left: '50%', transform: 'translateX(-50%)', zIndex: 10 }}>
            <button
              onClick={() => loadNeptuneGraph(true)}
              style={{
                fontSize: '0.75rem',
                padding: '0.3rem 0.8rem',
                background: 'var(--surface2)',
                border: '1px solid var(--border)',
                borderRadius: 4,
                color: 'var(--text2)',
                cursor: 'pointer',
              }}
            >
              ↻ Refresh
            </button>
          </div>
          <div id="cy-neptune-graph" style={{ position: 'absolute', top: 0, left: 0, bottom: 0, right: 0 }} />
          <GraphLegend />
          <div
            id="cy-neptune-empty"
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              bottom: 0,
              right: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--muted)',
              fontSize: '0.9rem',
            }}
          >
            Choose Refresh to load the knowledge graph
          </div>
        </div>
        <TraceEntries
          entries={trace}
          questionCounter={questionCounter}
          activeQuestionFilter={activeQuestionFilter}
          onFilterQuestion={(q) => archStore.setQuestionFilter(q)}
          visible={traceVisible}
        />
        {/* Graph Explorer panel — shown in reasoning-trace area when KG tab is active */}
        <div id="kg-explorer-panel" className={showNeptune ? '' : 'hidden'}>
          {/* View-state breadcrumb: filtered overview vs neighborhood-of-node,
              with a one-click return (kg-view-store). */}
          <KgViewBreadcrumb />
          <div style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text)', marginBottom: '0.75rem' }}>
            GRAPH EXPLORER
          </div>
          <KgNodeSearch />
          <div className="kg-filter-grid">
            <div>
              <div style={{ fontSize: '0.7rem', color: 'var(--muted)', marginBottom: '0.3rem', fontWeight: 600 }}>
                NODE TYPES
              </div>
              <div id="neptune-node-filter" className="kg-checkbox-list kg-checkbox-sm">
                {/* View-scoped legend toggles with live counts — the 'all'
                    lockstep row is retired (visibility is per-type; counts
                    always describe the rendered view). */}
                <KgTypeLegend
                  kind="nodes"
                  staticTypes={NODE_FILTERS}
                />
              </div>
            </div>
            <div>
              <div style={{ fontSize: '0.7rem', color: 'var(--muted)', marginBottom: '0.3rem', fontWeight: 600 }}>
                EDGE TYPES
              </div>
              <div id="neptune-edge-filter" className="kg-checkbox-list kg-checkbox-sm">
                <KgTypeLegend
                  kind="edges"
                  staticTypes={EDGE_FILTERS}
                />
              </div>
            </div>
          </div>
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: '0.75rem', marginTop: '0.75rem' }}>
            <div style={{ fontSize: '0.75rem', color: 'var(--muted)', marginBottom: '0.4rem', fontWeight: 600 }}>
              SELECTION
            </div>
            <KgSelectionInfo />
          </div>
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: '0.75rem', marginTop: '0.75rem' }}>
            <div style={{ fontSize: '0.75rem', color: 'var(--muted)', marginBottom: '0.4rem', fontWeight: 600 }}>
              AI ANALYSIS
            </div>
            <NeptuneAnalysisPanel />
          </div>
        </div>
      </div>

      {/* Proposed Mappings Tab Panel (hidden by default) */}
      <div
        id="proposed-mappings-panel"
        style={{
          display: activeTab === 'proposed' ? '' : 'none',
          padding: '1rem',
          position: 'absolute',
          top: '7rem',
          left: 0,
          right: 0,
          bottom: 0,
          overflowY: 'auto',
          zIndex: 10,
        }}
      >
        <div className="sl-toolbar">
          <h4>
            {t('sl.title')} <PendingBadge />
          </h4>
          <div className="sl-actions-group">
            <button className="sl-btn primary" onClick={() => acceptAllPendingMappings()}>
              {t('sl.acceptAll')}
            </button>
            <button className="sl-btn" onClick={() => resetAllMappings()}>
              {t('sl.resetAccepted')}
            </button>
            <button className="sl-btn" onClick={() => clearRejectedMappings()}>
              {t('sl.clearRejected')}
            </button>
            <button className="sl-btn danger" onClick={() => clearAllLearnings()}>
              {t('sl.clearAll')}
            </button>
          </div>
        </div>
        <p className="sl-intro">
          As the AI answers cross-system questions, it discovers relationships and domain context it
          had to work out the hard way. Accept a discovery to save it to the knowledge graph so
          future questions are answered faster and more accurately.
        </p>

        {/* User-defined relationship: assert a learning the AI never proposed. */}
        <details className="sl-define" id="sl-define-relationship">
          <summary>{t('sl.define')}</summary>
          <p className="sl-intro">
            Assert a cross-system relationship yourself — for example that two fields hold the same
            value under different codes. It is saved to the knowledge graph immediately and takes
            precedence over anything the AI proposed for the same fields.
          </p>
          <div className="sl-define-grid">
            <label>
              From system
              <input id="ud-from-system" type="text" list="ud-systems-list" placeholder="e.g. erp" />
            </label>
            <label>
              From field
              <input id="ud-from-field" type="text" list="ud-fields-list" placeholder="e.g. orders.customer_code" />
            </label>
            <label>
              From value
              <input id="ud-from-value" type="text" placeholder="e.g. ANYCO-001" />
            </label>
            <label>
              To system
              <input id="ud-to-system" type="text" list="ud-systems-list" placeholder="e.g. mes" />
            </label>
            <label>
              To field
              <input id="ud-to-field" type="text" list="ud-fields-list" placeholder="e.g. runs.cust_id" />
            </label>
            <label>
              To value
              <input id="ud-to-value" type="text" placeholder="e.g. C1001" />
            </label>
            <label>
              Concept
              <input id="ud-concept" type="text" list="ud-concepts-list" placeholder="e.g. customer" />
            </label>
            <label>
              Type
              <select id="ud-proposed-type" defaultValue="value_mapping">
                <option value="value_mapping">Value mapping</option>
                <option value="identity_rule">Identity rule</option>
                <option value="join_path">Join path</option>
              </select>
            </label>
            <label className="sl-define-wide">
              Description (optional)
              <input id="ud-description" type="text" placeholder="Why these are related" />
            </label>
          </div>
          {/* Autocomplete option lists — populated at runtime via DOM APIs. */}
          <datalist id="ud-systems-list" />
          <datalist id="ud-fields-list" />
          <datalist id="ud-concepts-list" />
          <div className="sl-define-actions">
            <button className="sl-btn primary" onClick={() => submitUserDefinedMapping()}>
              {t('sl.saveRelationship')}
            </button>
            <span id="ud-message" className="sl-define-msg" role="status" />
          </div>
        </details>

        <ProposalsList />

        <LearnedGuidanceList />
      </div>
    </div>
  );
}

// Re-export the filter application for parity with the old global (unused
// directly here but part of the module surface the integration pass wires).
export { applyQuestionFilter };
