// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <ThreadPanel/> — Digital Thread Explorer (the reimagined explorer:
 * "topology map
 * with gap callouts, takeaway-led coverage, pipeline trace"). Shared by the
 * admin Thread tab and the index architecture area. Mount it when the host
 * activates (the admin page called initAdminThread() on tab click); it loads
 * the overview on mount.
 *
 * Contractual ids/classes from the legacy markup are preserved: #thread-meta,
 * #topo-loading/-empty/-content, #topo-headline, #topo-pairbar, #topo-gaps,
 * #topo-map, #topo-drawer, #topo-graphwide(-badges/-body), #cov-loading/
 * -empty/-content, #cov-banner, #vocab-stats, #coverage-heatmap,
 * #impact-panel/-concept/-trace-btn/-systems/-paths/-reachable/-correlations,
 * #trace-picker-bar, #entity-type-tag, #entity-select, #trace-empty,
 * #trace-intro, #thread-resolve-section, #thread-raw/-resolve/-unified,
 * #raw-cards, #resolution-steps, #unified-card, .thread-tab(-panel).
 * All rendering goes through React elements (zero-innerHTML policy);
 * interactive elements take React onClick handlers instead of the previous
 * inline onclick / data-* delegation. */

import { useQuery } from '@tanstack/react-query';
import { useCallback, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react';

import { systemsQueryOptions, useSystemsQueryClient } from '../../api/systems-query';
import { useAuth } from '../../contexts/AuthContext';
import { asyncStateData, type AsyncState } from '../ui/async-state';
import { EmptyState, ErrorState, LoadingState, RefreshingOverlay } from '../ui/AsyncStates';
import type {
  GraphSummary,
  ImpactAnalysis,
  LearnProposal,
  LearnResponse,
  PathsResponse,
  ResolveResponse,
  ThreadEntities,
} from '../../api/types/thread';
import type { FetchLike } from '../agents/pricing';
import { showToast } from './data-modal';
import {
  analyticsQueryOptions,
  conceptsQueryOptions,
  entitiesQueryOptions,
  graphSummaryQueryOptions,
  requestThreadJson,
  type ThreadAnalytics,
} from './thread-queries';
import type { StepUiState, ThreadSystem, TopoPair } from './html';
import {
  aggregatePairs,
  emptyMetaText,
  entityTypeKey,
  learnRequestBody,
  linkedPairKeys,
  matchEntityOption,
  metaText,
  onboardedSystems,
  pairGaps,
  totalRuleCount,
} from './html';
import {
  canLearnRules,
  CovBanner,
  CoverageHeatmap,
  CovLegend,
  GraphwideBadges,
  GraphwideBody,
  graphwideCounts,
  hasImpactCorrelations,
  ImpactConcept,
  ImpactPaths,
  ImpactReachable,
  ImpactSystems,
  ImpactCorrelations,
  NodeDrawer,
  PairDrawer,
  RankedPaths,
  RawCards,
  ResolutionSteps,
  TopoGaps,
  TopoHeadline,
  TopoPairChips,
  TopoSvg,
  TraceIntro,
  UnifiedCard,
  VocabPanel,
} from './panels';

export interface ThreadPanelProps {
  /** Injectable raw fetch retained as a focused test seam. Production calls
   * use requestJson and its shared cookie/error contract. */
  fetchFn?: FetchLike;
  /** Empty-state "Onboard a system" link handler — the index host switches
   * views (old markup called switchView('onboard')); default navigates to
   * /#onboard. */
  onNavigateOnboard?: () => void;
}

type TopologyData = {
  onboarded: ThreadSystem[];
  pairs: Record<string, TopoPair>;
  graph: GraphSummary;
  totalRules: number;
};

type OverviewState = AsyncState<TopologyData>;
type AnalyticsState = AsyncState<ThreadAnalytics>;
type EntitiesState = AsyncState<ThreadEntities>;
type ResolveState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; data: ResolveResponse }
  | { phase: 'error' };

type Selection = { kind: 'pair'; key: string } | { kind: 'node'; sysId: string } | null;

type ImpactState = {
  conceptId: string;
  result: AsyncState<ImpactAnalysis>;
  /** Ranked paths replacing the "Joins via" column (loadPathsForSystems). */
  pathsOverride?: { fromSys: string; toSys: string; paths: PathsResponse };
};

const THREAD_TABS = [
  { id: 'thread-tab-graph', controlId: 'thread-tab-graph-control', label: 'Topology' },
  { id: 'thread-tab-coverage', controlId: 'thread-tab-coverage-control', label: 'Coverage' },
  { id: 'thread-tab-trace', controlId: 'thread-tab-trace-control', label: 'Trace' },
];

/** Impact bottom-sheet snap detents (% of #cov-content height): half / tall. */
const IMPACT_SHEET_SNAPS: readonly [number, number] = [45, 85];

/** Nearest snap detent for a dragged height. */
function snapSheetPct(pct: number): number {
  return IMPACT_SHEET_SNAPS.reduce((a, b) => (Math.abs(b - pct) < Math.abs(a - pct) ? b : a));
}

/** Empty-state copy per tab (_renderThreadEmpties). */
function topoEmptyText(conceptCount: number): string {
  return (
    'This view will draw a live map of your connected systems and every translation rule linking them — and flag ' +
    'the pairs where the thread is broken. ' +
    conceptCount +
    ' manufacturing concepts are already seeded in the knowledge graph.'
  );
}
const COV_EMPTY_TEXT =
  'This view will show which manufacturing concepts each system covers, grouped by ISA-95 level — the cross-system ' +
  'concepts are what power cross-system answers.';
const TRACE_EMPTY_TEXT =
  'This view will trace a single entity across your systems: raw records, the translation rules that prove they ' +
  'match, and the unified result.';

function scrollTo(el: Element | null, opts: ScrollIntoViewOptions) {
  // jsdom has no scrollIntoView; guard for tests.
  (el as { scrollIntoView?: (o: ScrollIntoViewOptions) => void } | null)?.scrollIntoView?.(opts);
}

export function ThreadPanel({ fetchFn, onNavigateOnboard }: ThreadPanelProps) {
  const fetchRef = useRef(fetchFn);
  fetchRef.current = fetchFn;
  // Saving a rule is steward-only (POST /api/thread/learn answers 403 otherwise).
  const canLearn = canLearnRules(useAuth());

  const queryClient = useSystemsQueryClient();
  const systemsQuery = useQuery(
    { ...systemsQueryOptions(fetchFn), retry: false },
    queryClient,
  );
  const conceptsQuery = useQuery(
    { ...conceptsQueryOptions(fetchFn), retry: false },
    queryClient,
  );
  const currentSystems = (systemsQuery.data?.systems || []) as ThreadSystem[];
  const currentOnboarded = onboardedSystems(currentSystems);
  const systemsSignature = currentOnboarded
    .map((system) => String(system.id || system.name || ''))
    .sort()
    .join('|');
  const overviewReferencesReady = !!systemsQuery.data && !!conceptsQuery.data;
  const graphQuery = useQuery(
    {
      ...graphSummaryQueryOptions(systemsSignature, fetchFn),
      enabled: overviewReferencesReady && currentOnboarded.length > 0,
      retry: false,
      placeholderData: (previous) => previous,
    },
    queryClient,
  );

  let overview: OverviewState;
  if (!systemsQuery.data) {
    overview = systemsQuery.isError
      ? { status: 'error', message: 'Error loading knowledge graph.', retryable: true }
      : { status: 'loading' };
  } else if (!conceptsQuery.data) {
    overview = conceptsQuery.isError
      ? { status: 'error', message: 'Error loading knowledge graph.', retryable: true }
      : { status: 'loading' };
  } else if (currentOnboarded.length === 0) {
    overview = {
      status: 'empty',
      reason: 'not-configured',
      message: topoEmptyText(conceptsQuery.data.concepts?.length || 0),
    };
  } else if (graphQuery.data) {
    overview = {
      status: 'ready',
      data: {
        onboarded: currentOnboarded,
        pairs: aggregatePairs(graphQuery.data, currentOnboarded),
        graph: graphQuery.data,
        totalRules: totalRuleCount(graphQuery.data),
      },
      refreshing: systemsQuery.isFetching || conceptsQuery.isFetching || graphQuery.isFetching,
    };
  } else if (graphQuery.isError) {
    overview = { status: 'error', message: 'Error loading knowledge graph.', retryable: true };
  } else {
    overview = { status: 'loading' };
  }

  const overviewData = asyncStateData(overview);
  const overviewReady = !!overviewData;
  const entitiesQuery = useQuery(
    { ...entitiesQueryOptions(fetchFn), enabled: overviewReady, retry: false },
    queryClient,
  );
  const analyticsQuery = useQuery(
    { ...analyticsQueryOptions(fetchFn), enabled: overviewReady, retry: false },
    queryClient,
  );
  const analytics: AnalyticsState = !overviewReady
    ? { status: 'idle' }
    : analyticsQuery.data
      ? { status: 'ready', data: analyticsQuery.data, refreshing: analyticsQuery.isFetching }
      : analyticsQuery.isError
        ? { status: 'error', message: 'Error loading concept coverage.', retryable: true }
        : { status: 'loading' };
  const entities: EntitiesState = !overviewReady
    ? { status: 'idle' }
    : entitiesQuery.data
      ? { status: 'ready', data: entitiesQuery.data, refreshing: entitiesQuery.isFetching }
      : entitiesQuery.isError
        ? { status: 'error', message: 'Failed to load entities.', retryable: true }
        : { status: 'loading' };

  // Default active tab: Topology.
  const [activeTab, setActiveTab] = useState('thread-tab-graph');
  const [selection, setSelection] = useState<Selection>(null);
  const [covHover, setCovHover] = useState<{ row: number | null; col: number | null }>({ row: null, col: null });
  const [entityValue, setEntityValue] = useState('');
  const [typeTag, setTypeTag] = useState<string | null>(null);
  const [traceStarted, setTraceStarted] = useState(false);
  const [resolve, setResolve] = useState<ResolveState>({ phase: 'idle' });
  const [stepStates, setStepStates] = useState<Record<number, StepUiState>>({});
  const [impact, setImpact] = useState<ImpactState | null>(null);
  // Impact bottom-sheet height (% of #cov-content) — draggable via the grab
  // handle, snapping to detents on release (see .impact-panel in thread.css).
  const [sheetPct, setSheetPct] = useState<number>(IMPACT_SHEET_SNAPS[0]);
  const [sheetDragging, setSheetDragging] = useState(false);
  const entityValueRef = useRef('');
  entityValueRef.current = entityValue;
  const drawerRef = useRef<HTMLDivElement | null>(null);
  const impactPanelRef = useRef<HTMLDivElement | null>(null);
  const pickerBarRef = useRef<HTMLDivElement | null>(null);

  const retryOverview = useCallback(async () => {
    const retries: Array<Promise<unknown>> = [];
    if (systemsQuery.isError) retries.push(systemsQuery.refetch());
    if (conceptsQuery.isError) retries.push(conceptsQuery.refetch());
    if (graphQuery.isError) retries.push(graphQuery.refetch());
    if (retries.length === 0) {
      retries.push(systemsQuery.refetch(), conceptsQuery.refetch());
    }
    await Promise.all(retries);
  }, [conceptsQuery, graphQuery, systemsQuery]);

  const loadGraphAnalytics = useCallback(async () => {
    await analyticsQuery.refetch();
  }, [analyticsQuery]);

  const loadThreadEntities = useCallback(async () => {
    await entitiesQuery.refetch();
  }, [entitiesQuery]);

  // ── switchThreadTab ─────────────────────────────────────────────────────────
  const switchThreadTab = useCallback((tabId: string) => {
    setActiveTab(tabId);
  }, []);

  const handleTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
      let nextIndex: number;
      switch (event.key) {
        case 'ArrowRight':
        case 'ArrowDown':
          nextIndex = (index + 1) % THREAD_TABS.length;
          break;
        case 'ArrowLeft':
        case 'ArrowUp':
          nextIndex = (index - 1 + THREAD_TABS.length) % THREAD_TABS.length;
          break;
        case 'Home':
          nextIndex = 0;
          break;
        case 'End':
          nextIndex = THREAD_TABS.length - 1;
          break;
        default:
          return;
      }

      event.preventDefault();
      switchThreadTab(THREAD_TABS[nextIndex].id);
      event.currentTarget.parentElement
        ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
        [nextIndex]?.focus();
    },
    [switchThreadTab],
  );

  // ── Topology drawer (_openPairDrawer / _openNodeDrawer / _closeTopoDrawer) ──
  const openPairDrawer = useCallback((key: string) => {
    setSelection({ kind: 'pair', key });
    setTimeout(() => scrollTo(drawerRef.current, { behavior: 'smooth', block: 'nearest' }), 0);
  }, []);

  const openNodeDrawer = useCallback((sysId: string) => {
    setSelection({ kind: 'node', sysId });
    setTimeout(() => scrollTo(drawerRef.current, { behavior: 'smooth', block: 'nearest' }), 0);
  }, []);

  const closeTopoDrawer = useCallback(() => setSelection(null), []);

  // ── Rule lifecycle (maintain-side: soft deactivate/reactivate) ─────────────
  // Fires the lifecycle POST for every rule id in the binding, then refetches
  // the graph summary so the drawer re-buckets. Errors propagate (fail-loud).
  const graphRefetch = graphQuery.refetch;
  const onRuleLifecycle = useCallback(
    async (ruleIds: string[], action: 'deactivate' | 'reactivate') => {
      for (const id of ruleIds) {
        await requestThreadJson(fetchRef.current, `/api/thread/rules/${encodeURIComponent(id)}/${action}`, {
          method: 'POST',
        });
      }
      await graphRefetch();
    },
    [graphRefetch],
  );

  // ── showEntity ──────────────────────────────────────────────────────────────
  const showEntity = useCallback(async (value: string) => {
    if (!value) return;
    setEntityValue(value);
    const [type, ...rest] = value.split(':');
    const id = rest.join(':');
    setTypeTag(entityTypeKey(type)); // Batches→batch, Orders→order
    setTraceStarted(true);
    setResolve({ phase: 'loading' });
    setStepStates({});
    try {
      const data = await requestThreadJson<ResolveResponse>(fetchRef.current, '/api/thread/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_type: type, identifier: id }),
      });
      setResolve({ phase: 'ready', data });
    } catch {
      setResolve({ phase: 'error' });
    }
  }, []);

  // ── surpriseTrace ───────────────────────────────────────────────────────────
  const surpriseTrace = useCallback(() => {
    if (entities.status !== 'ready') return;
    const values: string[] = [];
    for (const [groupLabel, items] of Object.entries(entities.data)) {
      for (const item of items) values.push(groupLabel + ':' + item.id);
    }
    if (!values.length) return;
    const pick = values[Math.floor(Math.random() * values.length)];
    void showEntity(pick);
  }, [entities, showEntity]);

  // ── learnRule / dismissProposal ─────────────────────────────────────────────
  const learnRule = useCallback(
    async (proposal: LearnProposal, stepIdx: number) => {
      setStepStates((s) => ({ ...s, [stepIdx]: { ...s[stepIdx], learning: true } }));
      try {
        const data = await requestThreadJson<LearnResponse>(fetchRef.current, '/api/thread/learn', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(learnRequestBody(proposal, entityValueRef.current)),
        });
        if (data.error) throw new Error(data.error);
        showToast('Knowledge graph updated — 1 new identity rule created');
        setStepStates((s) => ({ ...s, [stepIdx]: { learned: true } }));
        setTimeout(() => {
          void showEntity(entityValueRef.current);
        }, 1000);
      } catch (e) {
        setStepStates((s) => ({
          ...s,
          [stepIdx]: { failed: e instanceof Error ? e.message : String(e) },
        }));
      }
    },
    [showEntity],
  );

  const dismissProposal = useCallback((stepIdx: number) => {
    setStepStates((s) => ({ ...s, [stepIdx]: { ...s[stepIdx], dismissed: true } }));
  }, []);

  // ── showImpact / loadPathsForSystems / closeImpactPanel ────────────────────
  const showImpact = useCallback(async (conceptId: string) => {
    setImpact({ conceptId, result: { status: 'loading' } });
    try {
      const data = await requestThreadJson<ImpactAnalysis>(
        fetchRef.current,
        '/api/thread/analytics/impact/' + encodeURIComponent(conceptId),
      );
      setImpact({ conceptId, result: { status: 'ready', data } });
      // No scroll-into-view: the impact panel is a bottom sheet overlaying
      // the heatmap, visible immediately when open.
      if ((data.direct_systems || []).length >= 2) {
        const sysIds = data.direct_systems!.map((system) => system.system_id);
        try {
          const paths = await requestThreadJson<PathsResponse>(
            fetchRef.current,
            '/api/thread/analytics/paths?from_system=' + encodeURIComponent(sysIds[0]) +
              '&to_system=' + encodeURIComponent(sysIds[1]),
          );
          if (paths.total_paths) {
            setImpact({
              conceptId,
              result: { status: 'ready', data },
              pathsOverride: { fromSys: sysIds[0], toSys: sysIds[1], paths },
            });
          }
        } catch (error) {
          console.error('Path ranking error:', error);
        }
      }
    } catch (error) {
      console.error('Impact analysis error:', error);
      setImpact({
        conceptId,
        result: { status: 'error', message: 'Error loading impact analysis.', retryable: true },
      });
    }
  }, []);

  const closeImpactPanel = useCallback(() => setImpact(null), []);

  // ── Impact bottom-sheet drag / snap (grab handle) ───────────────────────
  const sheetDragRef = useRef(false);
  const onSheetPointerDown = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    sheetDragRef.current = true;
    setSheetDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);
  const onSheetPointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (!sheetDragRef.current) return;
    const container = impactPanelRef.current?.parentElement;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    if (!rect.height) return;
    const pct = ((rect.bottom - e.clientY) / rect.height) * 100;
    setSheetPct(Math.min(90, Math.max(25, pct)));
  }, []);
  const onSheetPointerEnd = useCallback(() => {
    if (!sheetDragRef.current) return;
    sheetDragRef.current = false;
    setSheetDragging(false);
    setSheetPct((p) => snapSheetPct(p));
  }, []);
  const toggleSheetSnap = useCallback(() => {
    setSheetPct((p) =>
      snapSheetPct(p) === IMPACT_SHEET_SNAPS[0] ? IMPACT_SHEET_SNAPS[1] : IMPACT_SHEET_SNAPS[0]);
  }, []);
  const onSheetKeyDown = useCallback((e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSheetPct(IMPACT_SHEET_SNAPS[1]);
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSheetPct(IMPACT_SHEET_SNAPS[0]);
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleSheetSnap();
    }
  }, [toggleSheetSnap]);

  // ── traceFromImpact — switch to Trace, token-match an entity option ─────────
  const traceFromImpact = useCallback(() => {
    if (!impact) return;
    setActiveTab('thread-tab-trace');
    if (entities.status === 'ready') {
      const values: string[] = [];
      for (const [groupLabel, items] of Object.entries(entities.data)) {
        for (const item of items) values.push(groupLabel + ':' + item.id);
      }
      const match = matchEntityOption(impact.conceptId, values);
      if (match) void showEntity(match);
    }
    setTimeout(() => scrollTo(pickerBarRef.current, { behavior: 'smooth' }), 0);
  }, [impact, entities, showEntity]);

  // ── Render (markup: #tab-thread .thread-explorer) ──────────────────────────
  const impactData = impact?.result.status === 'ready' ? impact.result.data : null;
  const showTraceBtn = !!impactData && (impactData.direct_systems || []).length > 0;
  const analyticsData = analytics.status === 'ready' ? analytics.data : null;
  const entitiesEmpty =
    entities.status === 'ready' &&
    Object.values(entities.data).every((items) => items.length === 0);
  const topology = overviewData;
  const conceptCount = conceptsQuery.data?.concepts?.length || 0;

  const headerMeta =
    overview.status === 'empty'
      ? emptyMetaText(conceptCount)
      : overview.status === 'error'
        ? 'Error loading graph state'
        : topology
          ? metaText(topology.onboarded.length, topology.totalRules)
          : '';

  const empty = overview.status === 'empty';
  const topoState: 'loading' | 'empty' | 'ready' | 'error' =
    overview.status === 'error' ? 'error' : topology ? 'ready' : empty ? 'empty' : 'loading';
  const covState: 'loading' | 'empty' | 'ready' | 'error' = empty
    ? 'empty'
    : overview.status === 'error' || analytics.status === 'error'
      ? 'error'
      : topology && analytics.status === 'ready'
        ? 'ready'
        : 'loading';

  const linkedKeys = topology ? linkedPairKeys(topology.pairs) : [];
  const possible = topology ? (topology.onboarded.length * (topology.onboarded.length - 1)) / 2 : 0;
  const hasGaps = !!topology && pairGaps(topology.onboarded, topology.pairs).length > 0;
  const gw = topology ? graphwideCounts(topology.graph) : { ar: 0, co: 0, db: 0 };
  const hasGraphwide = gw.ar + gw.co + gw.db > 0;
  const selectedPairKey = selection?.kind === 'pair' ? selection.key : null;
  const selectedSysId = selection?.kind === 'node' ? selection.sysId : null;
  const selectedPair = topology && selectedPairKey ? topology.pairs[selectedPairKey] : null;
  const selectedSys =
    topology && selectedSysId
      ? topology.onboarded.find((system) => String(system.id || '') === selectedSysId)
      : null;
  const drawerOpen = !!(selectedPair || selectedSys);
  const resolvedSystemNames =
    resolve.phase === 'ready'
      ? Array.from(new Set((resolve.data.raw || []).map((record) => record.system).filter(Boolean)))
      : [];
  const singleSystemTrace = resolve.phase === 'ready' && resolvedSystemNames.length === 1;
  const consideredSystems = (topology?.onboarded || currentOnboarded)
    .map((system) => String(system.name || system.id || ''))
    .filter(Boolean);

  return (
    <div className="thread-explorer thread-mobile-safe">
      <div className="thread-header">
        <h3 style={{ margin: 0, fontSize: '1rem' }}>Digital Thread Explorer</h3>
        <div className="thread-meta" id="thread-meta">
          {headerMeta}
        </div>
      </div>
      <div className="thread-tabs" role="tablist" aria-label="Digital Thread views">
        {THREAD_TABS.map((t, index) => (
          <button
            key={t.id}
            id={t.controlId}
            type="button"
            role="tab"
            aria-selected={activeTab === t.id}
            aria-controls={t.id}
            tabIndex={activeTab === t.id ? 0 : -1}
            className={'thread-tab' + (activeTab === t.id ? ' active' : '')}
            data-tab={t.id}
            onClick={() => switchThreadTab(t.id)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Topology: how systems are connected, and where the thread is broken */}
      <div
        className={'thread-tab-panel' + (activeTab === 'thread-tab-graph' ? ' active' : '')}
        id="thread-tab-graph"
        role="tabpanel"
        aria-labelledby="thread-tab-graph-control"
        style={{ display: activeTab === 'thread-tab-graph' ? undefined : 'none' }}
      >
        <p className="thread-explainer">How your systems are actually connected — and where the thread is broken.</p>
        {topoState === 'loading' && (
          <div id="topo-loading">
            <LoadingState label="Loading knowledge graph" className="thread-loading" />
          </div>
        )}
        {topoState === 'error' && (
          <ErrorState
            message="Error loading knowledge graph."
            onRetry={() => void retryOverview()}
            urgent
            className="thread-error"
          />
        )}
        <div className="thread-empty" id="topo-empty" style={{ display: topoState === 'empty' ? undefined : 'none' }}>
          {empty && (
            <EmptyState
              message={overview.status === 'empty' ? overview.message : topoEmptyText(conceptCount)}
              actionLabel="Onboard a system →"
              onAction={onNavigateOnboard ?? (() => { window.location.hash = 'onboard'; })}
            />
          )}
        </div>
        <div
          className="thread-content"
          id="topo-content"
          aria-busy={overview.status === 'ready' && overview.refreshing ? 'true' : 'false'}
          style={{ display: topoState === 'ready' ? undefined : 'none' }}
        >
          {overview.status === 'ready' && overview.refreshing && (
            <RefreshingOverlay label="Refreshing topology" className="thread-refreshing" />
          )}
          {topology && (
            <>
              <div className="topo-headline" id="topo-headline">
                <TopoHeadline
                  systemCount={topology.onboarded.length}
                  totalRules={topology.totalRules}
                  linkedCount={linkedKeys.length}
                  possible={possible}
                />
              </div>
              <div className="topo-pairbar" id="topo-pairbar">
                <TopoPairChips
                  systems={topology.onboarded}
                  pairs={topology.pairs}
                  selectedPair={selectedPairKey}
                  onOpenPair={openPairDrawer}
                />
              </div>
              <div className="topo-gaps" id="topo-gaps" style={{ display: hasGaps ? undefined : 'none' }}>
                <TopoGaps systems={topology.onboarded} pairs={topology.pairs} />
              </div>
              <div className="topo-map" id="topo-map">
                <TopoSvg
                  systems={topology.onboarded}
                  pairs={topology.pairs}
                  selectedPair={selectedPairKey}
                  selectedSys={selectedSysId}
                  onOpenPair={openPairDrawer}
                  onOpenNode={openNodeDrawer}
                />
              </div>
              <div
                className="topo-drawer"
                id="topo-drawer"
                ref={drawerRef}
                style={{ display: drawerOpen ? undefined : 'none' }}
              >
                {selectedPair && (
                  <PairDrawer pair={selectedPair} systems={topology.onboarded} onClose={closeTopoDrawer} onRuleLifecycle={onRuleLifecycle} />
                )}
                {selectedSys && <NodeDrawer system={selectedSys} pairs={topology.pairs} onClose={closeTopoDrawer} onRuleLifecycle={onRuleLifecycle} />}
              </div>
              <details
                className="topo-graphwide"
                id="topo-graphwide"
                style={{ display: hasGraphwide ? undefined : 'none' }}
              >
                <summary>
                  Graph-wide intelligence <span id="topo-graphwide-badges">{<GraphwideBadges g={topology.graph} />}</span>
                </summary>
                <div id="topo-graphwide-body">
                  <GraphwideBody g={topology.graph} onRuleLifecycle={onRuleLifecycle} />
                </div>
              </details>
            </>
          )}
        </div>
      </div>

      {/* Coverage: what can be asked across systems */}
      <div
        className={'thread-tab-panel' + (activeTab === 'thread-tab-coverage' ? ' active' : '')}
        id="thread-tab-coverage"
        role="tabpanel"
        aria-labelledby="thread-tab-coverage-control"
        style={{ display: activeTab === 'thread-tab-coverage' ? undefined : 'none' }}
      >
        <p className="thread-explainer">
          What you can ask across systems — concept coverage per system, grouped by ISA-95 level.
        </p>
        {covState === 'loading' && (
          <div id="cov-loading">
            <LoadingState label="Loading concept coverage" className="thread-loading" />
          </div>
        )}
        {covState === 'error' && (
          <ErrorState
            message={overview.status === 'error' ? 'Error loading knowledge graph.' : 'Error loading concept coverage.'}
            onRetry={() => void (overview.status === 'error' ? retryOverview() : loadGraphAnalytics())}
            urgent
            className="thread-error"
          />
        )}
        <div className="thread-empty" id="cov-empty" style={{ display: covState === 'empty' ? undefined : 'none' }}>
          {empty && (
            <EmptyState
              message={COV_EMPTY_TEXT}
              actionLabel="Onboard a system →"
              onAction={onNavigateOnboard ?? (() => { window.location.hash = 'onboard'; })}
            />
          )}
        </div>
        <div
          className="thread-content"
          id="cov-content"
          aria-busy={analytics.status === 'ready' && analytics.refreshing ? 'true' : 'false'}
          style={{ display: covState === 'ready' ? undefined : 'none' }}
        >
          {analytics.status === 'ready' && analytics.refreshing && (
            <RefreshingOverlay label="Refreshing concept coverage" className="thread-refreshing" />
          )}
          {analyticsData && (
            <>
              <div className="cov-banner" id="cov-banner">
                <CovBanner vocab={analyticsData.vocab} cov={analyticsData.coverage} />
              </div>
              <div className="vocab-stats" id="vocab-stats">
                <VocabPanel vocab={analyticsData.vocab} />
              </div>
              <CoverageStateLegend />
              {/* Role-dot legend pinned ABOVE the scrolling heatmap (was at
                  the bottom) so it stays visible while reading the grid. */}
              <CovLegend />
              <div className="coverage-heatmap" id="coverage-heatmap">
                <CoverageHeatmap
                  data={analyticsData.coverage}
                  activeConceptId={impact?.conceptId ?? null}
                  onSelectConcept={(id) => void showImpact(id)}
                  hover={covHover}
                  onHover={setCovHover}
                />
              </div>
              <div
                className={'impact-panel' + (sheetDragging ? ' dragging' : '')}
                id="impact-panel"
                ref={impactPanelRef}
                style={{ display: impact ? undefined : 'none', height: `${sheetPct}%` }}
              >
                <div
                  className="impact-sheet-handle"
                  role="separator"
                  aria-orientation="horizontal"
                  aria-label="Resize impact panel — drag, arrow keys, or double-click to snap"
                  tabIndex={0}
                  onPointerDown={onSheetPointerDown}
                  onPointerMove={onSheetPointerMove}
                  onPointerUp={onSheetPointerEnd}
                  onPointerCancel={onSheetPointerEnd}
                  onDoubleClick={toggleSheetSnap}
                  onKeyDown={onSheetKeyDown}
                >
                  <span className="impact-sheet-grip" aria-hidden="true" />
                </div>
                <div className="impact-sheet-scroll">
                <div className="impact-header">
                  <div className="impact-concept" id="impact-concept">
                    {impact?.result.status === 'loading' ? (
                      <LoadingState label="Loading impact analysis" compact />
                    ) : impact?.result.status === 'error' ? (
                      <ErrorState
                        message={impact.result.message}
                        onRetry={() => void showImpact(impact.conceptId)}
                        compact
                      />
                    ) : impactData ? (
                      <ImpactConcept d={impactData} />
                    ) : null}
                  </div>
                  <div className="impact-actions">
                    <button
                      type="button"
                      className="impact-trace-btn"
                      id="impact-trace-btn"
                      style={{ display: showTraceBtn ? undefined : 'none' }}
                      onClick={traceFromImpact}
                    >
                      Trace Live Data →
                    </button>
                    <button
                      type="button"
                      className="impact-close-btn"
                      onClick={closeImpactPanel}
                      aria-label="Close impact panel"
                    >
                      ✕
                    </button>
                  </div>
                </div>
                <div className="impact-body">
                  <div className="impact-col" id="impact-systems">
                    {impactData && <ImpactSystems d={impactData} />}
                  </div>
                  <div className="impact-col" id="impact-paths">
                    {impactData &&
                      (impact?.pathsOverride ? (
                        <RankedPaths
                          fromSys={impact.pathsOverride.fromSys}
                          toSys={impact.pathsOverride.toSys}
                          d={impact.pathsOverride.paths}
                        />
                      ) : (
                        <ImpactPaths d={impactData} />
                      ))}
                  </div>
                  <div className="impact-col" id="impact-reachable">
                    {impactData && <ImpactReachable d={impactData} />}
                  </div>
                </div>
                <div
                  className="impact-correlations"
                  id="impact-correlations"
                  style={{ display: impactData && hasImpactCorrelations(impactData) ? undefined : 'none' }}
                >
                  {impactData && <ImpactCorrelations d={impactData} />}
                </div>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {/* Trace: prove one entity resolves across systems end-to-end */}
      <div
        className={'thread-tab-panel' + (activeTab === 'thread-tab-trace' ? ' active' : '')}
        id="thread-tab-trace"
        role="tabpanel"
        aria-labelledby="thread-tab-trace-control"
        style={{ display: activeTab === 'thread-tab-trace' ? undefined : 'none' }}
      >
        <p className="thread-explainer">Prove one entity resolves across systems end-to-end.</p>
        {entities.status === 'ready' && entities.refreshing && (
          <RefreshingOverlay label="Refreshing entities" className="trace-entity-state" />
        )}
        <div
          className="trace-picker-bar"
          id="trace-picker-bar"
          ref={pickerBarRef}
          style={{ display: empty ? 'none' : undefined }}
        >
          <span className={'entity-type-tag' + (typeTag ? ' ' + typeTag : '')} id="entity-type-tag">
            {typeTag ?? 'select'}
          </span>
          {entities.status === 'loading' && (
            <LoadingState label="Loading entities" compact className="trace-entity-state" />
          )}
          {entities.status === 'error' && (
            <ErrorState
              message={entities.message}
              onRetry={() => void loadThreadEntities()}
              compact
              className="trace-entity-state"
            />
          )}
          {entitiesEmpty && (
            <EmptyState
              message="No entities are available to trace yet."
              compact
              className="trace-entity-state"
            />
          )}
          <select
            id="entity-select"
            aria-label="Entity to trace"
            aria-busy={entities.status === 'loading' || (entities.status === 'ready' && entities.refreshing)}
            value={entityValue}
            onChange={(e) => void showEntity(e.target.value)}
          >
            {entities.status === 'error' ? (
              <option value="">Failed to load entities</option>
            ) : entities.status === 'ready' ? (
              <>
                <option value="">Pick an entity to trace…</option>
                {Object.entries(entities.data).map(([groupLabel, items]) => (
                  <optgroup key={groupLabel} label={groupLabel}>
                    {items.map((item) => (
                      <option key={item.id} value={groupLabel + ':' + item.id}>
                        {item.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </>
            ) : (
              <option value="">Pick an entity to trace…</option>
            )}
          </select>
          <button type="button" className="trace-surprise-btn" title="Trace a random entity" onClick={surpriseTrace}>
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <polyline points="16 3 21 3 21 8" />
              <line x1="4" y1="20" x2="21" y2="3" />
              <polyline points="21 16 21 21 16 21" />
              <line x1="15" y1="15" x2="21" y2="21" />
              <line x1="4" y1="4" x2="9" y2="9" />
            </svg>
            Surprise me
          </button>
        </div>
        <div className="thread-empty" id="trace-empty" style={{ display: empty ? undefined : 'none' }}>
          {empty && (
            <EmptyState
              message={TRACE_EMPTY_TEXT}
              actionLabel="Onboard a system →"
              onAction={onNavigateOnboard ?? (() => { window.location.hash = 'onboard'; })}
            />
          )}
        </div>
        <div
          className="trace-intro"
          id="trace-intro"
          style={{ display: !empty && !traceStarted ? undefined : 'none' }}
        >
          <TraceIntro />
        </div>
        <div
          className="thread-resolve-section"
          id="thread-resolve-section"
          style={{ display: traceStarted ? undefined : 'none' }}
        >
          {singleSystemTrace && (
            <div className="trace-scope-banner" role="status">
              <strong>
                Found in 1 of {consideredSystems.length} systems ({resolvedSystemNames[0]}).
              </strong>{' '}
              No cross-system matches resolved for this entity.
              {consideredSystems.length > 0 && (
                <span className="trace-scope-systems">
                  Connected systems considered: {consideredSystems.join(', ')}.
                </span>
              )}
            </div>
          )}
          <div className="trace-pipeline">
            <div className="thread-panel trace-stage" id="thread-raw">
              <div className="trace-stage-head">
                <span className="trace-stage-num">1</span>
                <div>
                  <div className="panel-title">Source records</div>
                  <div className="trace-stage-sub">Same entity, different names</div>
                </div>
              </div>
              <div id="raw-cards">
                <ResolveBody resolve={resolve} render={(d) => <RawCards raw={d.raw || []} />} />
              </div>
            </div>
            <div className="trace-flow-arrow">
              <FlowArrow />
            </div>
            <div className="thread-panel trace-stage center" id="thread-resolve">
              <div className="trace-stage-head">
                <span className="trace-stage-num">2</span>
                <div>
                  <div className="panel-title">Translation</div>
                  <div className="trace-stage-sub">Rules the graph applies</div>
                </div>
              </div>
              <div id="resolution-steps">
                <ResolveBody
                  resolve={resolve}
                  render={(d) => (
                    <ResolutionSteps
                      resolution={d.resolution || []}
                      stepStates={stepStates}
                      canLearn={canLearn}
                      onLearn={(p, i) => void learnRule(p, i)}
                      onDismiss={dismissProposal}
                    />
                  )}
                />
              </div>
            </div>
            <div className="trace-flow-arrow">
              <FlowArrow />
            </div>
            <div
              className={'thread-panel trace-stage ' + (singleSystemTrace ? 'single-system' : 'payoff')}
              id="thread-unified"
            >
              <div className="trace-stage-head">
                <span className="trace-stage-num">3</span>
                <div>
                  <div className="panel-title">{singleSystemTrace ? 'Single-system record' : 'Unified entity'}</div>
                  <div className="trace-stage-sub">
                    {singleSystemTrace ? 'No cross-system unification' : 'One cross-system view'}
                  </div>
                </div>
              </div>
              <div id="unified-card">
                <ResolveBody
                  resolve={resolve}
                  render={(d) => {
                    if (singleSystemTrace) return <RawCards raw={d.raw || []} />;
                    const [, ...rest] = entityValueRef.current.split(':');
                    return <UnifiedCard unified={d.unified || []} entityId={rest.join(':')} />;
                  }}
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function CoverageStateLegend() {
  return (
    <div className="coverage-state-legend" aria-label="Coverage status legend">
      <span className="coverage-state-title">Coverage status</span>
      <span className="coverage-state-item"><span className="coverage-state-swatch mapped" />Mapped</span>
      <span className="coverage-state-item"><span className="coverage-state-swatch partial" />Partial</span>
      <span className="coverage-state-item"><span className="coverage-state-swatch unmapped" />Unmapped</span>
      <span className="coverage-state-note">solid dots · mixed solid/hollow · hollow cells</span>
    </div>
  );
}

function FlowArrow() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

function ResolveBody({
  resolve,
  render,
}: {
  resolve: ResolveState;
  render: (d: ResolveResponse) => React.ReactNode;
}) {
  if (resolve.phase === 'loading') return <div className="drawer-note">Loading…</div>;
  if (resolve.phase === 'error')
    return <div style={{ color: 'var(--red)', padding: '1rem' }}>Error loading entity</div>;
  if (resolve.phase === 'ready') return <>{render(resolve.data)}</>;
  return null;
}
