// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboard flow store (jsx-migration W5.1) — the single external store behind
 * the #onboard-flow area. Replaces the imperative innerHTML/appendChild flow
 * building of flow.ts/chat.ts/wizard.ts with an ordered-entries model that
 * <ObFlow/> renders:
 *
 * - `obAppendMsg/obAppendLoading/obAppendError/obAppendCheckpoint` become
 *   store actions returning entry ids (flow.ts keeps thin wrappers so the
 *   wizard's imports stay stable).
 * - `obAppendCheckpoint` takes STRUCTURED content (CheckpointContent union)
 *   instead of an HTML string — the mapping checkpoint's rows are store
 *   state, so the wizard's confirm resolution and the payload builders read
 *   React state, never the DOM.
 * - `obCollapseStep` folds prior message-like entries into a collapsed-group
 *   entry (the old `<details class="ob-collapsed-section">` wrap).
 * - `ob._obSavedFlow` (innerHTML save/restore for detail back-nav) becomes a
 *   store snapshot: `saved` keeps the {mode, entries, …} slice by reference.
 * - The system-detail screen (which replaced the flow's innerHTML
 *   wholesale) becomes the `screen: 'detail'` slice.
 *
 * No DOM access at import time (vitest node environment). */
import type {
  CatalogSystem,
  CoverageRecommendation,
  DiscoveredTable,
  FieldMapping,
  IdentityRule,
  JoinCandidate,
  PreflightResult,
  ProposedConcept,
  ProvisionStatusResponse,
  RejectedIdentityRule,
  RemapSuggestion,
  ResolveEvidence,
  TransformCheck,
  VocabFit,
} from '../../api/types/onboard';
import { createStore } from '../../lib/store';
import type { ObStepContext } from './state';

// ── View-models ───────────────────────────────────────────────────────────────

/** One concept tag on a mapping-checkpoint row. `skip` tags render the muted
 * "skipped" chip; `manual` tags show "manual" instead of a percentage. */
export interface ConceptTagVm {
  concept: string;
  /** 0..1 confidence (manual picks keep the legacy 0.5 fallback). */
  confidence: number;
  /** Render "manual" as the tag-conf label (picker/manual adds). */
  manual?: boolean;
  /** Suggestion basis (value_overlap / isa95_synonym / semantic) — <BasisBadge/>. */
  basis?: string;
  skip?: boolean;
}

/** One row of the Confirm Mappings checkpoint table. */
export interface MappingRowVm {
  field: string;
  tags: ConceptTagVm[];
  /** Row still highlighted for review (no clean concept yet). */
  review: boolean;
  /** Concept picker open for this row. */
  pickerOpen?: boolean;
}

export type CheckpointContent =
  | { kind: 'loading'; label: string }
  | { kind: 'error'; text: string }
  | { kind: 'schemaTable'; tables: DiscoveredTable[] }
  | { kind: 'mcpSummary'; name: string; count: number }
  /** E1 preflight verdict card (ADR-0053) — <PreflightBody/> renders the
   * verdict badge, blocked banner, per-table sample reads, completeness
   * counts, and warnings. Additive; carried on the SAME checkpoint entry
   * kind as every other pipeline gate. */
  | { kind: 'preflight'; result: PreflightResult }
  | {
      kind: 'mappingTable';
      rows: MappingRowVm[];
      /** Wave 2 (C5, additive): vocabulary-fit stats for the map result —
       * <ObFlow/> renders the fit banner above the checkpoint when present. */
      vocab_fit?: VocabFit;
    }
  | {
      kind: 'rules';
      rules: IdentityRule[];
      extras: ResolveExtrasData | null;
    };

/** The resolve step's evidence / recommendations / join candidates payload
 * (renderResolveExtras input). */
export interface ResolveExtrasData {
  evidence?: ResolveEvidence[];
  /** Translation checks (resolve_gates.TRANSFORM_CHECKS_KEY). */
  transform_checks?: TransformCheck[];
  recommendations?: CoverageRecommendation[];
  join_candidates?: JoinCandidate[];
  /** Candidates the resolve evidence gates withheld, with reasons — shown
   * so an empty rules table reads as a judgement, not as "first system". */
  rejected_rules?: RejectedIdentityRule[];
}

export interface CheckpointChatBubble {
  id: number;
  role: 'user' | 'agent';
  /** user bubbles render text; agent bubbles render markdown. */
  text: string;
  /** agent bubble still streaming (empty text ⇒ nothing rendered yet). */
  typing?: boolean;
  error?: string;
}

export interface CheckpointSpec {
  /** Checkpoint heading, without the leading "✦ " (component adds it). */
  title: string;
  content: CheckpointContent;
  confirmLabel: string;
  /** Initial confirm gate (mapping checkpoint starts disabled). */
  confirmDisabled?: boolean;
  /** Render the "Skip All Unmapped" helper button. */
  showSkipAll?: boolean;
  stepContext: ObStepContext;
  onConfirm: (entryId: number) => void;
  onRerun?: (entryId: number) => void;
}

export interface DiscoveryRowVm {
  proposal: ProposedConcept;
  decided: 'created' | 'skipped' | null;
}

export interface ReconcileRowVm {
  suggestion: RemapSuggestion;
  decided: 'accepted' | 'skipped' | null;
  /** Why the last Accept did not tag the field (shown on the row). */
  error?: string;
}

export type FlowEntry =
  | { id: number; kind: 'msg'; md: string }
  | { id: number; kind: 'user'; text: string; variant: 'bubble' | 'msg' }
  | { id: number; kind: 'file'; name: string; sizeFmt: string }
  | { id: number; kind: 'loading'; label: string; strong?: string }
  | {
      id: number;
      kind: 'error';
      text: string;
      /** Retry affordance (obAppendRetryError): removes the entry and resolves
       * the step's retry promise so the retried step renders fresh. */
      retry?: () => void;
    }
  | {
      id: number;
      kind: 'stream';
      md: string;
      typing: boolean;
      errorText?: string;
    }
  | { id: number; kind: 'toolPill'; toolId: string; label: string; done: boolean }
  | { id: number; kind: 'collapsed'; label: string; children: FlowEntry[] }
  | ({
      id: number;
      kind: 'checkpoint';
      confirmed: boolean;
      confirmDisabled: boolean;
      chatOpen: boolean;
      chatBubbles: CheckpointChatBubble[];
    } & Omit<CheckpointSpec, 'confirmDisabled'>)
  | {
      id: number;
      kind: 'discovery';
      rows: DiscoveryRowVm[];
      onCreate: (rowIdx: number) => void;
      onDone: () => void;
    }
  | { id: number; kind: 'connections'; result: ProvisionStatusResponse; systemId?: string }
  | {
      id: number;
      kind: 'chatCheckpoint';
      step: string;
      result: Record<string, unknown>;
      confirmed: boolean;
    }
  | {
      id: number;
      kind: 'credentialForm';
      fields: { name: string; type?: string }[];
      systemId: string;
      status: 'idle' | 'storing' | 'stored';
      errorText?: string;
      /** What a stored answer says still uses the old credentials. */
      note?: string;
    }
  | { id: number; kind: 'cta'; sysName: string }
  /** Pre-map probe pause (T-H). NOT in COLLAPSIBLE: runProbeStep removes the
   * entry on continue (a collapsed remount would re-fire the probe fetch). */
  | {
      id: number;
      kind: 'probe';
      systemId: string;
      tables: DiscoveredTable[];
      onContinue: () => void;
    }
  /** Runtime steward questions (ADR-0059) as an in-chat conversation with
   * the onboarding agent. Opened by the bell deep link (/#onboard/questions);
   * `openSeq` bumps on every re-open so the thread refetches the queue.
   * NOT collapsible — a step collapse must not bury pending questions. */
  | { id: number; kind: 'runtimeQuestions'; openSeq: number };

/** Wizard progress bar (obStart) / welcome-chat progress dots (obStreamChat). */
export interface ProgressState {
  /** 'wizard' renders status text + Cancel; 'chat' renders dots only. */
  variant: 'wizard' | 'chat';
  activeIdx: number;
  statusText: string;
  allDone: boolean;
  cancelHidden: boolean;
}

export interface DetailState {
  phase: 'loading' | 'error' | 'ready';
  sys: CatalogSystem;
  tables: DiscoveredTable[];
  mappings: FieldMapping[];
  errorText?: string;
}

export type ObModal =
  | { kind: 'cancel'; then?: () => void }
  | { kind: 'resetAll' }
  | {
      kind: 'disconnect';
      systemId: string;
      systemName?: string;
      registration?: boolean;
      /** Mode of a removal that did not finish ("Remove again"). */
      retryFull?: boolean;
      /** Opened by "Remove again": re-check the system before confirming. */
      retry?: boolean;
      /** The system's catalog connection_origin is 'user' (the user
       * connected it). Absent counts as not user-connected. */
      userOrigin?: boolean;
    }
  | { kind: 'connectNew' };

interface FlowCore {
  mode: 'welcome' | 'wizard';
  entries: FlowEntry[];
  /** Reconcile banners — rendered FIRST (old flow.insertBefore(firstChild)). */
  banners: { id: number; rows: ReconcileRowVm[]; total: number }[];
  progress: ProgressState | null;
  /** Welcome-mode seed chips (rendered inside .ob-welcome-input). */
  chips: string[] | null;
  /** Welcome variant flag: initial mount shows the example button + video
   * modal host; obReset()'s welcome does not (matches onboard.js). */
  welcomeInitial: boolean;
}

export interface FlowState extends FlowCore {
  /** 'flow' renders mode-driven welcome/wizard; detail replaces it. */
  screen: 'flow' | 'detail';
  detail: DetailState | null;
  /** _obSavedFlow snapshot-as-store-state (detail back-nav). */
  saved: FlowCore | null;
  modal: ObModal | null;
  /** Bumped on structural appends — <ObFlow/> scrolls to bottom. */
  appendSeq: number;
}

// ── Store ─────────────────────────────────────────────────────────────────────

const WELCOME_CORE: FlowCore = {
  mode: 'welcome',
  entries: [],
  banners: [],
  progress: null,
  chips: null,
  welcomeInitial: true,
};

const INITIAL: FlowState = {
  ...WELCOME_CORE,
  screen: 'flow',
  detail: null,
  saved: null,
  modal: null,
  appendSeq: 0,
};

export const flowStore = createStore<FlowState>(INITIAL);

/** Stable-reference selectors for narrow React subscriptions. */
export const selectFlowScreen = (state: FlowState) => state.screen;
export const selectFlowMode = (state: FlowState) => state.mode;
export const selectFlowProgress = (state: FlowState) => state.progress;
export const selectFlowEntries = (state: FlowState) => state.entries;
export const selectFlowBanners = (state: FlowState) => state.banners;
export const selectFlowChips = (state: FlowState) => state.chips;
export const selectFlowAppendSeq = (state: FlowState) => state.appendSeq;

let nextId = 1;
export function newEntryId(): number {
  return nextId++;
}

function patch(partial: Partial<FlowState>): void {
  flowStore.update((s) => ({ ...s, ...partial }));
}

function append(entry: FlowEntry): number {
  flowStore.update((s) => ({
    ...s,
    entries: [...s.entries, entry],
    appendSeq: s.appendSeq + 1,
  }));
  return entry.id;
}

/** Immutable per-entry update (checkpoints, streams, pills, …). */
export function updateEntry(id: number, fn: (e: FlowEntry) => FlowEntry): void {
  flowStore.update((s) => ({
    ...s,
    entries: s.entries.map((e) => (e.id === id ? fn(e) : e)),
  }));
}

export function removeEntry(id: number): void {
  flowStore.update((s) => ({
    ...s,
    entries: s.entries.filter((e) => e.id !== id),
  }));
}

export function getEntry(id: number): FlowEntry | undefined {
  return flowStore.get().entries.find((e) => e.id === id);
}

// ── Message actions (obAppendMsg / obAppendLoading / obAppendError) ──────────

export function appendMsg(md: string): number {
  return append({ id: newEntryId(), kind: 'msg', md: md || '' });
}

export function appendUser(text: string, variant: 'bubble' | 'msg'): number {
  return append({ id: newEntryId(), kind: 'user', text, variant });
}

export function appendFile(name: string, sizeFmt: string): number {
  return append({ id: newEntryId(), kind: 'file', name, sizeFmt });
}

/** `strong` renders `<strong>` around it after the label (the old
 * 'Connecting to <strong>name</strong>' interpolation, now structured). */
export function appendLoading(label: string, strong?: string): number {
  return append({ id: newEntryId(), kind: 'loading', label, strong });
}

export function updateLoadingLabel(id: number, label: string): void {
  updateEntry(id, (e) => (e.kind === 'loading' ? { ...e, label, strong: undefined } : e));
}

export function appendError(text: string): number {
  return append({ id: newEntryId(), kind: 'error', text });
}

/** Step error with a Retry affordance (onboard.js obAppendRetryError port):
 * renders visibly in the flow and resolves when the user clicks Retry (the
 * entry is removed so the retried step renders fresh). The wizard's Cancel
 * button in the progress bar remains the escape hatch to abandon the flow. */
export function appendRetryError(text: string): Promise<void> {
  return new Promise((resolve) => {
    const id = newEntryId();
    append({
      id,
      kind: 'error',
      text,
      retry: () => {
        removeEntry(id);
        resolve();
      },
    });
  });
}

/** Streaming agent message (typing dots until first token). */
export function appendStream(): number {
  return append({ id: newEntryId(), kind: 'stream', md: '', typing: true });
}

// Streaming markdown can arrive token-by-token. Keep only the latest complete
// markdown for each row and publish at most once per animation frame. All
// structural actions still update flowStore synchronously through append/updateEntry.
const pendingStreamMd = new Map<number, string>();
let pendingStreamFrame: number | null = null;

function cancelPendingStreamFrameIfEmpty(): void {
  if (pendingStreamMd.size > 0 || pendingStreamFrame == null) return;
  if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(pendingStreamFrame);
  pendingStreamFrame = null;
}

function publishPendingStreamMd(): void {
  pendingStreamFrame = null;
  if (pendingStreamMd.size === 0) return;
  const pending = new Map(pendingStreamMd);
  pendingStreamMd.clear();
  flowStore.update((state) => {
    let changed = false;
    const entries = state.entries.map((entry) => {
      const md = pending.get(entry.id);
      if (entry.kind !== 'stream' || md === undefined) return entry;
      if (entry.md === md && !entry.typing) return entry;
      changed = true;
      return { ...entry, md, typing: false };
    });
    return changed ? { ...state, entries } : state;
  });
}

export function setStreamMd(id: number, md: string): void {
  const entry = getEntry(id);
  if (!entry || entry.kind !== 'stream') return;
  pendingStreamMd.set(id, md);
  if (pendingStreamFrame != null) return;
  if (typeof requestAnimationFrame === 'function') {
    pendingStreamFrame = requestAnimationFrame(publishPendingStreamMd);
  } else {
    publishPendingStreamMd();
  }
}

export function resetStream(id: number): void {
  pendingStreamMd.delete(id);
  cancelPendingStreamFrameIfEmpty();
  updateEntry(id, (e) =>
    e.kind === 'stream' ? { ...e, md: '', typing: true, errorText: undefined } : e,
  );
}

export function setStreamError(id: number, errorText: string): void {
  pendingStreamMd.delete(id);
  cancelPendingStreamFrameIfEmpty();
  updateEntry(id, (e) =>
    e.kind === 'stream' ? { ...e, typing: false, errorText } : e,
  );
}

// ── Tool pills (onboard chat SSE) ─────────────────────────────────────────────

export function appendToolPill(toolId: string, label: string): number {
  return append({ id: newEntryId(), kind: 'toolPill', toolId, label, done: false });
}

/** tool_end marks the LAST not-done pill done (legacy selector semantics). */
export function markLastToolPillDone(): void {
  flowStore.update((s) => {
    for (let i = s.entries.length - 1; i >= 0; i--) {
      const e = s.entries[i];
      if (e.kind === 'toolPill' && !e.done) {
        const entries = [...s.entries];
        entries[i] = { ...e, done: true };
        return { ...s, entries };
      }
    }
    return s;
  });
}

// ── Checkpoints ───────────────────────────────────────────────────────────────

export function appendCheckpoint(spec: CheckpointSpec): number {
  const { confirmDisabled, ...rest } = spec;
  return append({
    id: newEntryId(),
    kind: 'checkpoint',
    confirmed: false,
    confirmDisabled: !!confirmDisabled,
    chatOpen: false,
    chatBubbles: [],
    ...rest,
  });
}

export function setCheckpointContent(id: number, content: CheckpointContent): void {
  updateEntry(id, (e) => {
    if (e.kind !== 'checkpoint') return e;
    // Mapping content re-derives the confirm gate (the old checkMappingState
    // call after a re-run rebuilt the table).
    const confirmDisabled =
      content.kind === 'mappingTable'
        ? mappingConfirmBlocked(content.rows)
        : e.confirmDisabled;
    return { ...e, content, confirmDisabled };
  });
}

export function confirmCheckpoint(id: number): void {
  const entry = getEntry(id);
  if (!entry || entry.kind !== 'checkpoint' || entry.confirmed) return;
  updateEntry(id, (e) =>
    e.kind === 'checkpoint' ? { ...e, confirmed: true, chatOpen: false } : e,
  );
  entry.onConfirm(id);
}

export function toggleCheckpointChat(id: number): void {
  updateEntry(id, (e) =>
    e.kind === 'checkpoint' ? { ...e, chatOpen: !e.chatOpen } : e,
  );
}

let nextBubbleId = 1;

export function appendCheckpointBubble(
  cpId: number,
  bubble: Omit<CheckpointChatBubble, 'id'>,
): number {
  const id = nextBubbleId++;
  updateEntry(cpId, (e) =>
    e.kind === 'checkpoint'
      ? { ...e, chatBubbles: [...e.chatBubbles, { ...bubble, id }] }
      : e,
  );
  return id;
}

export function updateCheckpointBubble(
  cpId: number,
  bubbleId: number,
  fn: (b: CheckpointChatBubble) => CheckpointChatBubble,
): void {
  updateEntry(cpId, (e) =>
    e.kind === 'checkpoint'
      ? {
          ...e,
          chatBubbles: e.chatBubbles.map((b) => (b.id === bubbleId ? fn(b) : b)),
        }
      : e,
  );
}

// ── Mapping-checkpoint row actions (React state, not DOM) ────────────────────

/** Confirm Mappings stays disabled while any row has no concept (and no
 * skip), and when there are no rows at all: confirming an empty mapping
 * provisions nothing and ends in the provision step's refusal. */
export function mappingConfirmBlocked(rows: readonly MappingRowVm[]): boolean {
  return rows.length === 0 || rows.some((r) => r.tags.length === 0);
}

function updateMappingRows(
  cpId: number,
  fn: (rows: MappingRowVm[]) => MappingRowVm[],
): void {
  updateEntry(cpId, (e) => {
    if (e.kind !== 'checkpoint' || e.content.kind !== 'mappingTable') return e;
    const rows = fn(e.content.rows);
    return {
      ...e,
      // Spread keeps the additive vocab_fit slice (C5) across row updates.
      content: { ...e.content, rows },
      confirmDisabled: mappingConfirmBlocked(rows),
    };
  });
}

export function addRowConcept(cpId: number, rowIdx: number, concept: string): void {
  updateMappingRows(cpId, (rows) =>
    rows.map((r, i) =>
      i === rowIdx
        ? {
            ...r,
            review: false,
            pickerOpen: false,
            tags: [...r.tags, { concept, confidence: 0.5, manual: true }],
          }
        : r,
    ),
  );
}

export function removeRowTag(cpId: number, rowIdx: number, tagIdx: number): void {
  updateMappingRows(cpId, (rows) =>
    rows.map((r, i) =>
      i === rowIdx ? { ...r, tags: r.tags.filter((_, ti) => ti !== tagIdx) } : r,
    ),
  );
}

export function skipRow(cpId: number, rowIdx: number): void {
  updateMappingRows(cpId, (rows) =>
    rows.map((r, i) =>
      i === rowIdx
        ? { ...r, review: false, tags: [{ concept: 'skip', confidence: 0, skip: true }] }
        : r,
    ),
  );
}

export function skipAllUnmapped(cpId: number): void {
  updateMappingRows(cpId, (rows) =>
    rows.map((r) =>
      r.tags.length === 0
        ? { ...r, review: false, tags: [{ concept: 'skip', confidence: 0, skip: true }] }
        : r,
    ),
  );
}

export function setRowPickerOpen(cpId: number, rowIdx: number, open: boolean): void {
  updateMappingRows(cpId, (rows) =>
    rows.map((r, i) => (i === rowIdx ? { ...r, pickerOpen: open } : r)),
  );
}

// ── Identity-rules checkpoint row actions ────────────────────────────────────

/** Steward rejects one proposed identity rule at the Identity Rules
 * checkpoint. Removes it from the rendered table AND from the step context's
 * `data.rules` — the SAME array reference the wizard's onConfirm hands to
 * provision (the chat `remove_rule` action edits it the same way), so a
 * rejected rule is never provisioned. */
export function rejectRule(cpId: number, ruleIdx: number): void {
  updateEntry(cpId, (e) => {
    if (e.kind !== 'checkpoint' || e.content.kind !== 'rules') return e;
    const data = e.stepContext.data as { rules?: unknown[] };
    if (Array.isArray(data.rules)) data.rules.splice(ruleIdx, 1);
    return {
      ...e,
      content: { ...e.content, rules: e.content.rules.filter((_, i) => i !== ruleIdx) },
    };
  });
}

// ── Collapse (obCollapseStep) ─────────────────────────────────────────────────

/** Entry kinds the legacy selector wrapped ('.ob-msg-wrap, .ob-msg,
 * .ob-checkpoint, .ob-tool-pill' — connections cards carry .ob-checkpoint). */
const COLLAPSIBLE = new Set<FlowEntry['kind']>([
  'msg',
  'user',
  'file',
  'loading',
  'error',
  'stream',
  'toolPill',
  'checkpoint',
  'connections',
  'chatCheckpoint',
]);

export function collapseStep(label: string): void {
  flowStore.update((s) => {
    const toWrap = s.entries.filter((e) => COLLAPSIBLE.has(e.kind));
    if (toWrap.length === 0) return s;
    const rest = s.entries.filter((e) => !COLLAPSIBLE.has(e.kind));
    const firstIdx = s.entries.findIndex((e) => COLLAPSIBLE.has(e.kind));
    const section: FlowEntry = {
      id: newEntryId(),
      kind: 'collapsed',
      label,
      children: toWrap,
    };
    // Insert the collapsed group where the first wrapped entry sat.
    const before = s.entries.slice(0, firstIdx).filter((e) => !COLLAPSIBLE.has(e.kind));
    const after = rest.slice(before.length);
    return { ...s, entries: [...before, section, ...after] };
  });
}

// ── Discovery / reconcile / connections / cta ─────────────────────────────────

export function appendDiscovery(
  proposals: ProposedConcept[],
  onCreate: (rowIdx: number) => void,
  onDone: () => void,
): number {
  return append({
    id: newEntryId(),
    kind: 'discovery',
    rows: proposals.map((proposal) => ({ proposal, decided: null })),
    onCreate,
    onDone,
  });
}

export function decideDiscoveryRow(
  entryId: number,
  rowIdx: number,
  decided: 'created' | 'skipped',
): void {
  updateEntry(entryId, (e) =>
    e.kind === 'discovery'
      ? { ...e, rows: e.rows.map((r, i) => (i === rowIdx ? { ...r, decided } : r)) }
      : e,
  );
}

let nextBannerId = 1;

export function showReconcileBanner(suggestions: RemapSuggestion[]): void {
  // Every suggestion gets a row; the panel scrolls a long list.
  flowStore.update((s) => ({
    ...s,
    banners: [
      {
        id: nextBannerId++,
        rows: suggestions.map((suggestion) => ({ suggestion, decided: null })),
        total: suggestions.length,
      },
      ...s.banners,
    ],
  }));
}

export function decideReconcileRow(
  bannerId: number,
  rowIdx: number,
  decided: 'accepted' | 'skipped',
): void {
  flowStore.update((s) => ({
    ...s,
    banners: s.banners.map((b) =>
      b.id === bannerId
        ? {
            ...b,
            rows: b.rows.map((r, i) =>
              i === rowIdx ? { ...r, decided, error: undefined } : r,
            ),
          }
        : b,
    ),
  }));
}

/** Show why Accept did not tag this row's field; the row stays open. */
export function setReconcileRowError(bannerId: number, rowIdx: number, error: string): void {
  flowStore.update((s) => ({
    ...s,
    banners: s.banners.map((b) =>
      b.id === bannerId
        ? { ...b, rows: b.rows.map((r, i) => (i === rowIdx ? { ...r, error } : r)) }
        : b,
    ),
  }));
}

export function removeReconcileBanner(bannerId: number): void {
  flowStore.update((s) => ({
    ...s,
    banners: s.banners.filter((b) => b.id !== bannerId),
  }));
}

/** The provision step's connections card; `systemId` adds the button to the
 * system's detail page. */
export function appendConnections(result: ProvisionStatusResponse, systemId?: string): number {
  return append({ id: newEntryId(), kind: 'connections', result, systemId });
}

export function appendChatCheckpoint(
  step: string,
  result: Record<string, unknown>,
): number {
  return append({
    id: newEntryId(),
    kind: 'chatCheckpoint',
    step,
    result,
    confirmed: false,
  });
}

export function confirmChatCheckpoint(id: number): void {
  updateEntry(id, (e) =>
    e.kind === 'chatCheckpoint' ? { ...e, confirmed: true } : e,
  );
}

export function appendCredentialForm(
  fields: { name: string; type?: string }[],
  systemId: string,
): number {
  return append({
    id: newEntryId(),
    kind: 'credentialForm',
    fields,
    systemId,
    status: 'idle',
  });
}

export function setCredentialFormStatus(
  id: number,
  status: 'idle' | 'storing' | 'stored',
  errorText?: string,
  note?: string,
): void {
  updateEntry(id, (e) =>
    e.kind === 'credentialForm' ? { ...e, status, errorText, note } : e,
  );
}

export function appendCta(sysName: string): number {
  return append({ id: newEntryId(), kind: 'cta', sysName });
}

/** Pre-map probe entry (T-H). The step helper (probe-step.ts) removes the
 * entry when the component continues — deliberately NOT collapsible. */
export function appendProbe(
  systemId: string,
  tables: DiscoveredTable[],
  onContinue: () => void,
): number {
  return append({ id: newEntryId(), kind: 'probe', systemId, tables, onContinue });
}

/** Open (or re-open) the runtime-questions conversation in the chat flow.
 * One thread per flow: a re-open bumps `openSeq` (refetch) and scrolls to
 * it instead of stacking duplicates. The read-only detail screen returns to
 * the saved flow first so the conversation is visible. */
export function openRuntimeQuestions(): number {
  if (flowStore.get().screen === 'detail' && !restoreSaved()) {
    patch({ screen: 'flow', detail: null });
  }
  const existing = flowStore.get().entries.find((e) => e.kind === 'runtimeQuestions');
  if (existing) {
    flowStore.update((s) => ({
      ...s,
      entries: s.entries.map((e) =>
        e.kind === 'runtimeQuestions' ? { ...e, openSeq: e.openSeq + 1 } : e,
      ),
      appendSeq: s.appendSeq + 1,
    }));
    return existing.id;
  }
  return append({ id: newEntryId(), kind: 'runtimeQuestions', openSeq: 1 });
}

// ── Progress ──────────────────────────────────────────────────────────────────

export function setProgress(progress: ProgressState | null): void {
  patch({ progress });
}

export function patchProgress(partial: Partial<ProgressState>): void {
  flowStore.update((s) =>
    s.progress ? { ...s, progress: { ...s.progress, ...partial } } : s,
  );
}

// ── Chips ─────────────────────────────────────────────────────────────────────

export function setChips(chips: string[] | null): void {
  patch({ chips });
}

// ── Mode / screen transitions ─────────────────────────────────────────────────

/** obReset(): back to the welcome flow (the reset variant — no example
 * button / video modal, matching onboard.js OB_RESET_WELCOME_HTML). */
export function resetToWelcome(): void {
  flowStore.update((s) => ({
    ...s,
    ...WELCOME_CORE,
    welcomeInitial: false,
    screen: 'flow',
    detail: null,
    saved: null,
    appendSeq: s.appendSeq + 1,
  }));
}

/** obStart(): clear the flow into wizard mode. */
export function startWizard(): void {
  flowStore.update((s) => ({
    ...s,
    mode: 'wizard',
    entries: [],
    banners: [],
    chips: null,
    progress: {
      variant: 'wizard',
      activeIdx: 0,
      statusText: 'Connecting...',
      allDone: false,
      cancelHidden: false,
    },
    screen: 'flow',
    detail: null,
    saved: null,
    appendSeq: s.appendSeq + 1,
  }));
}

/** showSystemDetail(): snapshot the current flow, switch to the detail
 * screen (the _obSavedFlow innerHTML save, as store state). */
export function openDetail(sys: CatalogSystem): void {
  flowStore.update((s) => ({
    ...s,
    saved: {
      mode: s.mode,
      entries: s.entries,
      banners: s.banners,
      progress: s.progress,
      chips: s.chips,
      welcomeInitial: s.welcomeInitial,
    },
    screen: 'detail',
    detail: { phase: 'loading', sys, tables: [], mappings: [] },
  }));
}

export function setDetail(detail: DetailState): void {
  patch({ detail });
}

/** obRestoreFlow(): back from the detail screen to the flow (or the caller
 * falls back to obReset when nothing is saved). Returns whether a snapshot
 * existed. openDetail leaves the flow slice in place and every call that
 * replaces it (startWizard, resetToWelcome) also leaves the detail screen, so
 * the live flow is current: a step an onboarding run finished while the
 * detail was open stays, where putting the snapshot back would drop it. */
export function restoreSaved(): boolean {
  const s = flowStore.get();
  if (!s.saved) return false;
  flowStore.update((prev) => ({
    ...prev,
    saved: null,
    screen: 'flow',
    detail: null,
    appendSeq: prev.appendSeq + 1,
  }));
  return true;
}

/** Leave the detail screen for the LIVE flow, dropping the snapshot openDetail
 * took. A running onboarding keeps updating the live flow (entries, progress,
 * checkpoints) while a detail screen is open, so returning to that run must
 * not put the older snapshot back over it. Same result as restoreSaved, but
 * keyed on the detail screen showing rather than on a snapshot existing: a
 * no-op when the detail screen is not showing. */
export function closeDetailKeepFlow(): void {
  flowStore.update((prev) =>
    prev.screen !== 'detail'
      ? prev
      : {
          ...prev,
          saved: null,
          screen: 'flow',
          detail: null,
          appendSeq: prev.appendSeq + 1,
        },
  );
}

// ── Modals ────────────────────────────────────────────────────────────────────

export function openModal(modal: ObModal): void {
  patch({ modal });
}

export function closeModal(): void {
  patch({ modal: null });
}
