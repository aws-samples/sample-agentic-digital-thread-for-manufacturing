// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Dashboard store (jsx-migration W2.1) — the single external store behind the
 * dashboard React components. Replaces the module singletons of state.ts and
 * the imperative create-or-update DOM writes of cards/sections/panels/
 * lineState/feed/systems/briefing/pinned/demo with store slices + reducers.
 *
 * Semantics kept 1:1 with the prior imperative modules:
 * - `upsertCards` keeps renderCards' create-or-update-in-place card
 *   semantics: existing cards update under their id, new cards append in
 *   first-seen order, cards absent from a later snapshot are NOT removed —
 *   only an empty/absent list clears everything. Stable `#kpi-card-<id>` DOM
 *   nodes fall out of React keys over `cardOrder`.
 * - The by-dimension / bar exclusion (Issue 1 regression guard, old
 *   cards.ts:91): a `group_by` / `viz_type==='bar'` / `resolver==='by-dimension'`
 *   card NEVER becomes a KPI tile — it is dropped from the map and routed to
 *   its panel loader ('machine-state' → loadLineState, 'quality-by-line' →
 *   loadQualityByLine). Loaders self-register via registerPanelLoader to
 *   avoid import cycles (same pattern as systems.setPlantReconnectHook).
 * - `handleCardUpdate` is the S3.2 re-derivation ripple: 'updating' marks the
 *   named cards, 'updated' re-upserts them and bumps `highlightSeq` (the
 *   KpiCard effect replays the CSS highlight pulse) + stores the audit line.
 * - The modal slice carries the shared detail-modal state (`.open` class,
 *   title/sub, section key + lineage, and a discriminated content union that
 *   DetailModal renders). `lastAction`-tagged feed slice preserves the
 *   seed-scrolls-to-bottom / prepend-keeps-top scroll behavior.
 *
 * Window parity kept from state.ts: `window._systemsById` (systems cache for
 * drill labels — dashboard.js kept it on window). No DOM access at import
 * time (vitest node environment). */
import type {
  ActiveIssuesPayload,
  CardMetric,
  CardUpdatePayload,
  ConnectedSystemInfo,
  DashboardCard,
  DashboardPayload,
  DimensionPayload,
  EntitySection,
  IotStats,
  Lineage,
  LineStatePayload,
  PinnedCard,
  HeatmapPayload,
} from '../../api/types/dashboard';
import { createStore } from '../../lib/store';
import {
  cardHasNoDataYet,
  cardNeedsStewardChoice,
  computeLineState,
  type LineStateBuckets,
  type LineStateModel,
} from './lib';

declare global {
  interface Window {
    /** Systems cache so drills can label a resolving system (S5.4) — kept on
     * window for parity with dashboard.js (`window._systemsById`). */
    _systemsById?: Record<string, ConnectedSystemInfo>;
  }
}

// ── State shape ───────────────────────────────────────────────────────────────

export interface CardMeta {
  updating?: boolean;
  audit?: string;
  /** Bumped per 'updated' ripple — KpiCard replays the highlight pulse. */
  highlightSeq?: number;
}

/** One withheld metric behind a steward-choice strip entry: the server's own
 * status and `detail` (the honest reason, naming the candidate columns). */
export interface StewardChoiceReason {
  metric_id?: string;
  status: string;
  detail?: string;
}

/** One awaiting-data strip entry (the `_uncoveredCards` registry). */
export interface UncoveredEntry {
  title: string;
  /** Server-supplied unmet concept ids (`missing_concepts`) — shown in the
   * chip's title tooltip. */
  missing: string[];
  /** Strip wording bucket (Task 2B): 'structural' = uncovered / needs-data
   * (no system maps the concept — "Awaiting data"); 'no-data' = covered but
   * the source returned zero samples ("No data yet"); 'steward-choice' =
   * every metric is withheld until a steward picks a column or binds a
   * filter concept ("Needs a steward choice"). Absent = structural
   * (back-compat). */
  kind?: 'structural' | 'no-data' | 'steward-choice';
  /** kind === 'steward-choice' only: one entry per withheld metric. */
  reasons?: StewardChoiceReason[];
}

/** Detail-modal body content — discriminated union rendered by DetailModal. */
export type ModalContent =
  | { kind: 'loading'; label: string }
  | { kind: 'error'; message: string }
  | { kind: 'section' } // renders sections[modal.key] + modal.lineage
  | { kind: 'oee'; card: DashboardCard }
  | { kind: 'machines'; payload: DimensionPayload }
  | { kind: 'distribution'; payload: DimensionPayload };

export interface ModalState {
  open: boolean;
  title: string;
  sub: string;
  /** Section cache key when a section drill is open (was overlay.dataset.key). */
  key: string | null;
  /** Lineage of the card that opened the drill (was _openDrillLineage). */
  lineage: Lineage | null;
  content: ModalContent | null;
}

export interface FeedLine {
  id: number;
  ts: string;
  topic: string;
  val: string;
}

export interface FeedState {
  lines: FeedLine[];
  paused: boolean;
  /** 'seed' scrolls to bottom, 'append' keeps top (old DOM scroll behavior). */
  lastAction: 'seed' | 'append' | null;
  seq: number;
}

export interface BriefingBulletVm {
  title: string;
  detail: string;
  color: string;
}

export interface BriefingState {
  /** init → the static "Initializing briefing..." placeholder;
   * placeholder → "Analyzing..." (first request with no prior content);
   * bullets/block → rendered result; unavailable → both attempts failed. */
  phase: 'init' | 'placeholder' | 'bullets' | 'block' | 'unavailable';
  /** Whether the active-view gate currently schedules automatic briefings. */
  lifecycle: 'active' | 'inactive';
  bullets: BriefingBulletVm[];
  /** Caveat lines the model wrote after the bullets (an italic note about a
   * figure a system flagged): shown under the cards, never as a card. */
  notes: string[];
  blockMd: string;
  analyzing: boolean;
  timestamp: string;
}

export interface DemoState {
  /** "Leave demo mode?" confirm dialog (demo exit is confirm-gated; demo.ts
   * pauses the run while this is up). The former pager-overlay fields are
   * gone — the demo narrates through the normal chat surfaces
   * (uniformity refactor; see views/dashboard/demo.ts). */
  confirmVisible: boolean;
}

export interface DashboardState {
  /** First-seen card order (renderCards append order). */
  cardOrder: string[];
  cards: Record<string, DashboardCard>;
  cardMeta: Record<string, CardMeta>;
  /** Uncovered / needs-data cards collapsed into the compact "Awaiting data"
   * strip (`_uncoveredCards`): card_id → {title, missing concept ids}.
   * These cards NEVER render as KPI tiles. */
  uncovered: Record<string, UncoveredEntry>;
  /** Last SSE/GET dashboard snapshot; null = never received (the Ops Summary
   * "Loading…" placeholder shows until the first snapshot). */
  dashboardData: DashboardPayload | null;
  /** True when snapshot delivery is failing BEFORE any snapshot arrived: the
   * Retry GET failed, or the stream errored and no snapshot came within the
   * grace window or by its second error (stream.ts SNAPSHOT_GRACE_MS) —
   * flips the Ops Summary "Loading…" into an error + retry state instead of
   * loading forever. Cleared by any snapshot. */
  snapshotFailed: boolean;
  /** Last /api/dashboard/line-state payload; undefined = never loaded. */
  lineState: LineStatePayload | undefined;
  quality: DimensionPayload | null | undefined;
  /** Work-orders-by-status COUNT bars; undefined = never loaded,
   * null = first load failed (panel hidden). */
  workOrders: DimensionPayload | null | undefined;
  /** Output-by-line SUM bars; same envelope semantics. */
  outputByLine: DimensionPayload | null | undefined;
  activeIssues: ActiveIssuesPayload | null | undefined;
  heatmap: HeatmapPayload | null | undefined;
  /** Entity-section cache keyed by an opaque serialized
   * [plant, system_id, table] tuple. */
  sections: Record<string, EntitySection>;
  modal: ModalState;
  feed: FeedState;
  /** Last /api/iot/stats payload (seed fetch + SSE 'stats' event, Task 1C);
   * null = never received — LiveFeed's empty states stay silent until then. */
  iotStats: IotStats | null;
  /** /api/systems list; null = never loaded. */
  systems: ConnectedSystemInfo[] | null;
  /** null = hero never created (matches the lazily-created #dash-empty-hero:
   * the SSE connected_types toggle only applies once it exists). */
  showEmptyHero: boolean | null;
  briefing: BriefingState;
  /** Pinned prompt cards; null = never loaded (container hidden). */
  pinned: PinnedCard[] | null;
  /** Per-card refresh failure message (replaces the card body, old refreshCard
   * catch path). */
  pinnedErrors: Record<string, string>;
  demo: DemoState;
}

const INITIAL: DashboardState = {
  cardOrder: [],
  cards: {},
  cardMeta: {},
  uncovered: {},
  dashboardData: null,
  snapshotFailed: false,
  lineState: undefined,
  quality: undefined,
  workOrders: undefined,
  outputByLine: undefined,
  activeIssues: undefined,
  heatmap: undefined,
  sections: {},
  modal: { open: false, title: '', sub: '', key: null, lineage: null, content: null },
  feed: { lines: [], paused: false, lastAction: null, seq: 0 },
  iotStats: null,
  systems: null,
  showEmptyHero: null,
  briefing: {
    phase: 'init',
    lifecycle: 'inactive',
    bullets: [],
    notes: [],
    blockMd: '',
    analyzing: false,
    timestamp: '',
  },
  pinned: null,
  pinnedErrors: {},
  demo: { confirmVisible: false },
};

export const dashboardStore = createStore<DashboardState>(INITIAL);

function patch(partial: Partial<DashboardState>): void {
  dashboardStore.update((s) => ({ ...s, ...partial }));
}

// ── Snapshot / cards (renderCards / renderCard / handleCardUpdate) ───────────

export function setDashboardData(data: DashboardPayload): void {
  patch({ dashboardData: data, snapshotFailed: false });
}

/** Snapshot delivery failed (SSE error / GET fallback catch). Only meaningful
 * while dashboardData is still null — the Ops Summary consults it to break
 * out of the eternal "Loading…" placeholder. */
export function setSnapshotFailed(failed: boolean): void {
  patch({ snapshotFailed: failed });
}

/** Register/clear an UNCOVERED dimension PANEL in the awaiting-data strip.
 * Panels (quality-by-line, work-orders, …) previously rendered a full dashed
 * "Waiting for data" card each (steward feedback 2026-08-06: placeholder
 * cards crowd out the data the user came for). An uncovered panel now joins
 * the SAME compact strip as uncovered cards — one chip, click for detail —
 * and renders nothing in the grid. Keyed `panel:<id>` so panel entries can
 * never collide with card_ids. Presence-driven: a panel that recovers
 * coverage removes its chip. */
export function setPanelUncovered(
  panelId: string, title: string, missing: string[] | undefined, uncovered: boolean,
): void {
  const key = `panel:${panelId}`;
  dashboardStore.update((s) => {
    const has = key in s.uncovered;
    if (!uncovered && !has) return s;
    const next = { ...s.uncovered };
    if (uncovered) {
      next[key] = { title, missing: (missing || []).map(String), kind: 'structural' };
    } else {
      delete next[key];
    }
    return { ...s, uncovered: next };
  });
}

/** Panel loaders for by-dimension cards — registered by their owning modules
 * (lineState.ts → 'machine-state', panels.ts → 'quality-by-line') to avoid a
 * store↔loader import cycle. */
const PANEL_LOADERS: Record<string, () => unknown> = {};

export function registerPanelLoader(cardId: string, loader: () => unknown): void {
  PANEL_LOADERS[cardId] = loader;
}

/** Issue 1 regression guard — a by-dimension / bar card is NOT a KPI tile. */
function isByDimension(card: DashboardCard): boolean {
  return !!card.group_by || card.viz_type === 'bar' || card.resolver === 'by-dimension';
}

/** Upsert ONE card into draft maps; returns a panel loader to fire (or null). */
function upsertOne(
  card: DashboardCard,
  draft: {
    cards: Record<string, DashboardCard>;
    cardOrder: string[];
    cardMeta: Record<string, CardMeta>;
    uncovered: Record<string, UncoveredEntry>;
  },
): (() => unknown) | null {
  const cardId = card.card_id;
  if (!cardId) return null;
  // Uncovered / needs-data cards collapse into the compact "Awaiting data"
  // strip — never a full dead tile (and never a panel-loader call).
  // Any prior tile for this card is removed; a card flipping OUT of uncovered
  // is dropped from the strip registry below.
  if (card.status === 'uncovered' || card.status === 'needs-data') {
    if (cardId in draft.cards) {
      delete draft.cards[cardId];
      delete draft.cardMeta[cardId];
      draft.cardOrder = draft.cardOrder.filter((id) => id !== cardId);
    }
    draft.uncovered[cardId] = {
      title: card.title || cardId,
      missing: Array.isArray(card.missing_concepts) ? card.missing_concepts : [],
      kind: 'structural',
    };
    return null;
  }
  // A covered card whose EVERY metric is withheld pending a steward choice
  // (ambiguous-binding / unresolved-predicate) has nothing to show but the
  // reason: it joins the strip as its own "Needs a steward choice" group,
  // carrying each metric's server `detail` for the chip's detail view. Checked
  // BEFORE no-data so a withhold is never relabelled as an empty source.
  if (cardNeedsStewardChoice(card)) {
    if (cardId in draft.cards) {
      delete draft.cards[cardId];
      delete draft.cardMeta[cardId];
      draft.cardOrder = draft.cardOrder.filter((id) => id !== cardId);
    }
    draft.uncovered[cardId] = {
      title: card.title || cardId,
      missing: [],
      kind: 'steward-choice',
      reasons: (card.metrics || [])
        .filter((m): m is CardMetric => !!m)
        .map((m) => ({
          ...(m.metric_id ? { metric_id: m.metric_id } : {}),
          status: m.status || '',
          ...(m.detail ? { detail: m.detail } : {}),
        })),
    };
    return null;
  }
  // Task 2B honest-data rule: a covered card whose data is DEGENERATE (card
  // status 'no-data', or every metric resolved 'no-data' — Task 2A sample
  // honesty) also collapses into the strip, under the distinct "No data yet"
  // wording bucket. Generic and presence-driven — no metric/card id literals.
  if (cardHasNoDataYet(card)) {
    if (cardId in draft.cards) {
      delete draft.cards[cardId];
      delete draft.cardMeta[cardId];
      draft.cardOrder = draft.cardOrder.filter((id) => id !== cardId);
    }
    draft.uncovered[cardId] = {
      title: card.title || cardId,
      missing: [],
      kind: 'no-data',
    };
    return null;
  }
  if (cardId in draft.uncovered) delete draft.uncovered[cardId];
  if (isByDimension(card)) {
    // Strip any stale tile, route to the matching panel loader, NEVER create.
    if (cardId in draft.cards) {
      delete draft.cards[cardId];
      delete draft.cardMeta[cardId];
      draft.cardOrder = draft.cardOrder.filter((id) => id !== cardId);
    }
    const loader = PANEL_LOADERS[cardId];
    return typeof loader === 'function' ? loader : null;
  }
  draft.cards[cardId] = card;
  if (!draft.cardOrder.includes(cardId)) draft.cardOrder = [...draft.cardOrder, cardId];
  return null;
}

/** renderCards: empty/absent clears ALL `#kpi-card-*` tiles; otherwise each
 * card create-or-updates in place (never removes cards missing from the list). */
export function upsertCards(cards: DashboardCard[] | undefined | null): void {
  if (!cards || !cards.length) {
    dashboardStore.update((s) => ({
      ...s,
      cards: {},
      cardOrder: [],
      cardMeta: {},
      uncovered: {},
    }));
    return;
  }
  const loaders: Array<() => unknown> = [];
  dashboardStore.update((s) => {
    const draft = {
      cards: { ...s.cards },
      cardOrder: s.cardOrder,
      cardMeta: { ...s.cardMeta },
      uncovered: { ...s.uncovered },
    };
    cards.forEach((card) => {
      const loader = upsertOne(card, draft);
      if (loader) loaders.push(loader);
    });
    return { ...s, ...draft };
  });
  loaders.forEach((loader) => {
    try {
      loader();
    } catch {
      /* fail-soft */
    }
  });
}

/** The canonical card collection after an incremental `card_update`: each
 * updated card replaces its snapshot entry by card_id, so the Operations
 * Summary — which derives every rollup from `dashboardData.cards` — reads the
 * SAME current cards as the strip and the KPI grid. REPLACE-ONLY, mirroring
 * the server's `update_cards_cache`: a card the snapshot does not carry is
 * never added. The snapshot deliberately excludes by-dimension (`group_by`)
 * cards, while the corrections ripple publishes them as `metrics: []`
 * descriptors; appended, they would count as "unavailable" until the next
 * snapshot. Before the first snapshot there is no collection to update: the
 * summary keeps its Loading state. Returns a fresh object/array when anything
 * changed so store subscribers re-render. */
function withUpdatedCards(
  data: DashboardPayload | null,
  updated: DashboardCard[],
): DashboardPayload | null {
  if (data === null || !Array.isArray(data.cards)) return data;
  const byId = new Map<string, DashboardCard>();
  updated.forEach((c) => {
    if (c && c.card_id) byId.set(c.card_id, c);
  });
  if (!byId.size) return data;
  let changed = false;
  const next = data.cards.map((c) => {
    const replacement = c && c.card_id ? byId.get(c.card_id) : undefined;
    if (!replacement) return c;
    changed = true;
    return replacement;
  });
  return changed ? { ...data, cards: next } : data;
}

/** S3.2 re-derivation ripple (card_update SSE): 'updating' marks the named
 * cards, 'updated' re-renders them + highlight pulse + audit line, and patches
 * the canonical `dashboardData.cards` in the same update. */
export function handleCardUpdate(payload: CardUpdatePayload | null | undefined): void {
  if (!payload) return;
  if (payload.status === 'updating') {
    const ids = Array.isArray(payload.card_ids) ? payload.card_ids : [];
    dashboardStore.update((s) => {
      const cardMeta = { ...s.cardMeta };
      ids.forEach((cardId) => {
        if (!(cardId in s.cards)) return; // old code: no tile → no-op
        cardMeta[cardId] = { ...cardMeta[cardId], updating: true };
      });
      return { ...s, cardMeta };
    });
    return;
  }
  if (payload.status === 'updated') {
    const cards = Array.isArray(payload.cards) ? payload.cards : [];
    const loaders: Array<() => unknown> = [];
    dashboardStore.update((s) => {
      const draft = {
        cards: { ...s.cards },
        cardOrder: s.cardOrder,
        cardMeta: { ...s.cardMeta },
        uncovered: { ...s.uncovered },
      };
      cards.forEach((card) => {
        const cardId = card.card_id;
        if (!cardId) return;
        const loader = upsertOne(card, draft);
        if (loader) loaders.push(loader);
        if (!(cardId in draft.cards)) return; // by-dimension/uncovered routed → no pulse
        const prev = draft.cardMeta[cardId] || {};
        draft.cardMeta[cardId] = {
          ...prev,
          updating: false,
          highlightSeq: (prev.highlightSeq || 0) + 1,
          ...(payload.audit && payload.audit.text ? { audit: payload.audit.text } : {}),
        };
      });
      return { ...s, ...draft, dashboardData: withUpdatedCards(s.dashboardData, cards) };
    });
    loaders.forEach((loader) => {
      try {
        loader();
      } catch {
        /* fail-soft */
      }
    });
  }
}

// ── Panels / line state ───────────────────────────────────────────────────────

export function setLineStatePayload(payload: LineStatePayload): void {
  patch({ lineState: payload });
}

export function setQuality(payload: DimensionPayload | null): void {
  patch({ quality: payload });
}

export function setWorkOrders(payload: DimensionPayload | null): void {
  patch({ workOrders: payload });
}

export function setOutputByLine(payload: DimensionPayload | null): void {
  patch({ outputByLine: payload });
}

export function setActiveIssues(payload: ActiveIssuesPayload | null): void {
  patch({ activeIssues: payload });
}

export function setHeatmap(payload: HeatmapPayload | null): void {
  patch({ heatmap: payload });
}

/** Derive the {running, idle, down} headline the Ops Summary consumes (was the
 * `_lineStateSummary` singleton set by renderLineState). */
export function deriveLineStateSummary(
  payload: LineStatePayload | undefined,
): LineStateBuckets | null {
  if (payload === undefined) return null;
  const model: LineStateModel = computeLineState(payload);
  if (model.kind === 'error' || model.kind === 'uncovered') return null;
  return model.headline;
}

// ── Sections + detail modal ───────────────────────────────────────────────────

export function sectionKey(plant: unknown, systemId: unknown, table: unknown): string {
  return JSON.stringify([String(plant), String(systemId), String(table)]);
}

function sectionsByKey(
  sections: EntitySection[] | undefined | null,
  plant: string,
): Record<string, EntitySection> {
  const byKey: Record<string, EntitySection> = {};
  if (!Array.isArray(sections)) return byKey;
  sections.forEach((section) => {
    if (section && section.table) {
      byKey[sectionKey(plant, section.system_id, section.table)] = section;
    }
  });
  return byKey;
}

/** Compatibility full-load write: replace the section cache for one plant. */
export function setSections(
  sections: EntitySection[] | undefined | null,
  plant = 'all',
): void {
  patch({ sections: sectionsByKey(sections, plant) });
}

/** Targeted drill write: preserve every previously loaded target. */
export function mergeSections(
  sections: EntitySection[] | undefined | null,
  plant: string,
): void {
  const additions = sectionsByKey(sections, plant);
  dashboardStore.update((state) => ({
    ...state,
    sections: { ...state.sections, ...additions },
  }));
}

export function patchModal(partial: Partial<ModalState>): void {
  dashboardStore.update((s) => ({ ...s, modal: { ...s.modal, ...partial } }));
}

export function closeModal(): void {
  patchModal({ open: false, key: null, lineage: null });
}

// ── Live feed ─────────────────────────────────────────────────────────────────

let _feedLineId = 0;

export function makeFeedLine(ts: string, topic: string, val: string): FeedLine {
  return { id: ++_feedLineId, ts, topic, val };
}

/** One-shot seed (pollLiveFeed): replaces the list, scrolls to bottom. */
export function seedFeed(lines: FeedLine[]): void {
  dashboardStore.update((s) => ({
    ...s,
    feed: { ...s.feed, lines, lastAction: 'seed', seq: s.feed.seq + 1 },
  }));
}

/** SSE append (old insertBefore firstChild): newest first, capped at 50. */
export function appendFeed(line: FeedLine): void {
  dashboardStore.update((s) => ({
    ...s,
    feed: {
      ...s.feed,
      lines: [line, ...s.feed.lines].slice(0, 50),
      lastAction: 'append',
      seq: s.feed.seq + 1,
    },
  }));
}

export function setFeedPaused(paused: boolean): void {
  dashboardStore.update((s) => ({ ...s, feed: { ...s.feed, paused } }));
}

/** IoT subscriber stats (Task 1C) — replaces the direct-DOM `#mqtt-stats`
 * writes in stream.ts/feed.ts; LiveFeed renders stats + empty states from it. */
export function setIotStats(stats: IotStats | null): void {
  patch({ iotStats: stats });
}

// ── Systems / empty hero ──────────────────────────────────────────────────────

export function setSystems(systems: ConnectedSystemInfo[]): void {
  patch({ systems });
}

/** loadSystems path: 0 connected creates/shows the hero; >0 hides it only if
 * it was ever created (old code: `else if (hero) hero.style.display='none'`). */
export function applyHeroFromSystems(connectedCount: number): void {
  dashboardStore.update((s) => {
    if (connectedCount === 0) return { ...s, showEmptyHero: true };
    if (s.showEmptyHero === null) return s;
    return { ...s, showEmptyHero: false };
  });
}

/** SSE snapshot path: toggles the hero from connected_types, but only once the
 * hero exists (old code guarded on getElementById). */
export function applyHeroFromSnapshot(connectedTypes: string[]): void {
  dashboardStore.update((s) => {
    if (s.showEmptyHero === null) return s;
    return { ...s, showEmptyHero: connectedTypes.length === 0 };
  });
}

/** Human label for a system id from the client systems cache:
 * "Name · Site", falling back to the id (dashboard.js `_systemLabel`). */
export function systemLabel(sid: string | null | undefined): string {
  const s = (typeof window !== 'undefined' ? window._systemsById || {} : {})[sid || ''];
  if (!s) return sid || '';
  const parts = [s.name || sid || ''];
  if (s.plant && String(s.plant).toLowerCase() !== 'all') parts.push(s.plant);
  return parts.join(' \u00b7 ');
}

export function setSystemsById(map: Record<string, ConnectedSystemInfo>): void {
  if (typeof window !== 'undefined') window._systemsById = map;
}

// ── Briefing ──────────────────────────────────────────────────────────────────

export function patchBriefing(partial: Partial<BriefingState>): void {
  dashboardStore.update((s) => ({ ...s, briefing: { ...s.briefing, ...partial } }));
}

// ── Pinned cards ──────────────────────────────────────────────────────────────

export function setPinned(cards: PinnedCard[]): void {
  patch({ pinned: cards });
}

/** Replace one pinned card after a refresh (old card.replaceWith). */
export function updatePinnedCard(cardId: string, updated: PinnedCard): void {
  dashboardStore.update((s) => {
    if (!s.pinned) return s;
    const pinnedErrors = { ...s.pinnedErrors };
    delete pinnedErrors[cardId];
    return {
      ...s,
      pinned: s.pinned.map((c) => (c.card_id === cardId ? updated : c)),
      pinnedErrors,
    };
  });
}

export function setPinnedError(cardId: string, message: string): void {
  dashboardStore.update((s) => ({
    ...s,
    pinnedErrors: { ...s.pinnedErrors, [cardId]: message },
  }));
}

// ── Demo confirm dialog ───────────────────────────────────────────────────────
// (The former demoShow/demoSetPage/demoHide pager-overlay actions are gone —
// the demo runs through the normal chat surfaces; see views/dashboard/demo.ts.)

export function demoConfirmShow(): void {
  dashboardStore.update((s) => ({ ...s, demo: { ...s.demo, confirmVisible: true } }));
}

export function demoConfirmHide(): void {
  dashboardStore.update((s) => ({ ...s, demo: { ...s.demo, confirmVisible: false } }));
}
