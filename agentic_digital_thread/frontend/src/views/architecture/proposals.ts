// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Self-Learning panel behavior — loadProposedMappings, accept/reject/
 * edit (optimistic + silent reconcile), user-define flow, datalists, reset/
 * clear/accept-all, and Learned Guidance.
 *
 * jsx-migration W5.2: this module is now the DATA layer — a proposals store
 * (lib/store.ts) plus async actions that mutate it. Rendering moved to the
 * React components in ./ProposalsPanel.tsx (<ProposalsList/>, <EditOverlay/>,
 * <LearnedGuidanceList/>, <PendingBadge/>) over the card components in
 * ./SlCards.tsx. The optimistic accept/reject semantics are preserved: the
 * card flips to "✓ Applied"/"✗ Removed" immediately (optimistic mark in the
 * store), the API call runs, success triggers a SILENT background reconcile
 * (no loading blank), and failure reverts the mark + emits an error toast. */
import type {
  GuidanceActionResult,
  MappingActionResult,
  ProposedMapping,
  ProposedMappingsResponse,
  QueryGuidanceResponse,
  QueryGuidanceSystem,
} from '../../api/types/proposals';
import { requestJson } from '../../api/client';
import { createStore } from '../../lib/store';
import { systemName } from '../../lib/system-names';
import { askConfirm } from '../../shared/ui/confirm-store';
import { push } from '../../shared/ui/toast-store';

/* ── Store ──────────────────────────────────────────────────────────────── */

export type LoadPhase = 'idle' | 'loading' | 'ready' | 'error';
export type ProposalStatusFilter = 'pending' | 'accepted' | 'rejected' | 'superseded';

export interface ProposalsState {
  /** Proposed-mappings list lifecycle. A SILENT reconcile never re-enters
   * 'loading' (the optimistic card patch must not be blanked). */
  phase: LoadPhase;
  /** Rows for the SELECTED `statusFilter` only — the fetch is status-scoped
   * (`GET /api/proposed-mappings?status=<statusFilter>`); the unfiltered
   * payload reached 1,850 rows / 3.2 MB live and stalled the panel. */
  mappings: ProposedMapping[];
  /** Server `status_counts` from the latest scoped response (every lifecycle
   * status, so the other tabs keep their labels). Absent before the first
   * response or from a server that predates the key. */
  statusCounts?: Partial<Record<string, number>>;
  error: string;
  /** One visible lifecycle bucket at a time; pending is the actionable default. */
  statusFilter: ProposalStatusFilter;
  /** Optimistic accept/reject marks by mapping_id — cleared when fresh server
   * data lands (reconcile) or the API call fails (revert). */
  optimistic: Record<string, 'accept' | 'reject'>;
  /** Rule-lifecycle marks by GRAPH NODE id (neptune_node_id) — the proposal
   * rows themselves never carry the graph node's deactivated state (it lives
   * on the Neptune node, not the DynamoDB row), so a successful deactivate
   * POST records the mark here and reactivate clears it. Session-local and
   * honest about its scope: a rule deactivated in an earlier session renders
   * its Deactivate button again (the POST is idempotent server-side). */
  ruleLifecycle: Record<string, 'deactivated'>;
  /** Learned-guidance card list lifecycle. */
  guidancePhase: LoadPhase;
  guidance: QueryGuidanceSystem[];
  guidanceError: string;
  /** Mapping currently open in the edit overlay (null = closed). */
  edit: ProposedMapping | null;
}

export const proposalsStore = createStore<ProposalsState>({
  phase: 'idle',
  mappings: [],
  error: '',
  statusFilter: 'pending',
  optimistic: {},
  ruleLifecycle: {},
  guidancePhase: 'idle',
  guidance: [],
  guidanceError: '',
  edit: null,
});

/** Switch the visible lifecycle bucket and fetch ONLY that bucket (non-silent:
 * the previous bucket's rows are cleared, never shown under the new tab). */
export function setProposalStatusFilter(statusFilter: ProposalStatusFilter): void {
  if (proposalsStore.get().statusFilter === statusFilter) return;
  proposalsStore.update((state) => ({ ...state, statusFilter, mappings: [], optimistic: {} }));
  void loadProposedMappings();
}

/** The status-scoped list URL for the currently selected bucket. */
function scopedMappingsUrl(): string {
  const status = proposalsStore.get().statusFilter;
  return `/api/proposed-mappings?status=${encodeURIComponent(status)}`;
}

/* ── Impact-first ordering (B2) ─────────────────────────────────────────── */

function consequenceValue(mapping: ProposedMapping): number | null {
  const raw = mapping.consequence;
  if (raw === undefined || raw === null || String(raw).trim() === '') return null;
  const consequence = Number(raw);
  return Number.isFinite(consequence) ? consequence : null;
}

/** Highest-consequence (evidence strength × blast radius, task 2.2 contract)
 * first. PURE + STABLE: equal scores keep input order, rows without a usable
 * consequence go last in input order — so pre-enrichment payloads (no
 * consequence anywhere) come back byte-identical, preserving the caller's
 * existing confidence ordering. */
export function sortMappingsByConsequence(mappings: ProposedMapping[]): ProposedMapping[] {
  return mappings
    .map((mapping, index) => ({ mapping, index, consequence: consequenceValue(mapping) }))
    .sort((a, b) => {
      if (a.consequence === null && b.consequence === null) return a.index - b.index;
      if (a.consequence === null) return 1;
      if (b.consequence === null) return -1;
      return b.consequence - a.consequence || a.index - b.index;
    })
    .map(({ mapping }) => mapping);
}

/* ── Proposed mappings ──────────────────────────────────────────────────── */

/** Load generation: only the LATEST request may write the store, so a slow
 * response for a previous status (or an older silent reconcile) that lands
 * after a tab switch is discarded instead of overwriting the current tab. */
let _loadGeneration = 0;

export async function loadProposedMappings(silent?: boolean): Promise<void> {
  const generation = ++_loadGeneration;
  // Loading state UNLESS this is a silent background refresh (optimistic
  // accept/reject already updated the card in place — do NOT blank the view).
  if (!silent) {
    proposalsStore.update((s) => ({ ...s, phase: 'loading' }));
  }
  try {
    const data = await requestJson<ProposedMappingsResponse>(scopedMappingsUrl());
    if (generation !== _loadGeneration) return; // superseded — discard
    const mappings = data.mappings || [];
    const statusCounts = data.status_counts;
    // Fresh server data replaces the optimistic marks (reconcile).
    proposalsStore.update((s) => ({
      ...s, phase: 'ready', mappings, statusCounts, optimistic: {}, error: '',
    }));
  } catch (err) {
    if (generation !== _loadGeneration) return;
    console.error('Failed to load proposed mappings:', err);
    proposalsStore.update((s) => ({ ...s, phase: 'error', error: (err as Error).message }));
  }
}

/* ── User-defined relationship ──────────────────────────────────────────── */

function udMsg(text: string, ok: boolean): void {
  const el = document.getElementById('ud-message');
  if (!el) return;
  el.textContent = text;
  el.style.color = ok ? 'var(--green)' : 'var(--red)';
}

function udVal(id: string): string {
  const el = document.getElementById(id) as HTMLInputElement | null;
  return el ? el.value.trim() : '';
}

function setDatalistOptions(listId: string, values: unknown[]): void {
  const list = document.getElementById(listId);
  if (!list) return;
  list.replaceChildren();
  const seen = new Set<string>();
  values.forEach((v) => {
    const val = (v == null ? '' : String(v)).trim();
    if (!val || seen.has(val)) return;
    seen.add(val);
    const opt = document.createElement('option');
    opt.value = val; // .value property is safe — not parsed as HTML
    list.appendChild(opt);
  });
}

let _udDatalistsLoaded = false;
interface UserDefineCatalogSystem {
  id?: string;
  status?: string;
}

export async function populateUserDefineDatalists(force?: boolean): Promise<void> {
  if (_udDatalistsLoaded && !force) return;
  _udDatalistsLoaded = true;
  try {
    const systems = await requestJson<UserDefineCatalogSystem[]>('/api/onboard/catalog');
    const systemIds = Array.isArray(systems)
      ? systems.map((s) => s && s.id).filter(Boolean)
      : [];
    setDatalistOptions('ud-systems-list', systemIds);

    // Field paths: gather table.column from each onboarded system's mappings.
    const onboarded = Array.isArray(systems)
      ? systems.filter((s) => s && s.status === 'onboarded')
      : [];
    const fieldLists: string[][] = await Promise.all(
      onboarded.map((s) =>
        requestJson<{ mappings?: { field?: string }[] }>(
          '/api/onboard/mappings/' + encodeURIComponent(s.id || ''),
        ).then((d) =>
          (d.mappings || []).map((m) => m && m.field).filter(Boolean) as string[],
        ),
      ),
    );
    setDatalistOptions('ud-fields-list', fieldLists.flat());
  } catch (err) {
    console.error('Failed to populate system/field datalists:', err);
    _udDatalistsLoaded = false;
    udMsg('Failed to load system and field suggestions: ' + (err as Error).message, false);
  }
  try {
    const data = await requestJson<{ concepts?: { id?: string }[] }>('/api/onboard/concepts');
    const conceptIds = ((data.concepts || []) as { id?: string }[])
      .map((c) => c && c.id)
      .filter(Boolean);
    setDatalistOptions('ud-concepts-list', conceptIds);
  } catch (err) {
    console.error('Failed to populate concept datalist:', err);
    _udDatalistsLoaded = false;
    udMsg('Failed to load concept suggestions: ' + (err as Error).message, false);
  }
}

export async function submitUserDefinedMapping(): Promise<void> {
  const body: Record<string, string> = {
    from_system: udVal('ud-from-system'),
    from_field: udVal('ud-from-field'),
    from_value: udVal('ud-from-value'),
    to_system: udVal('ud-to-system'),
    to_field: udVal('ud-to-field'),
    to_value: udVal('ud-to-value'),
    concept: udVal('ud-concept'),
    proposed_type: udVal('ud-proposed-type') || 'value_mapping',
    description: udVal('ud-description'),
  };

  // Client-side required-field check mirrors the route's 400 (fast feedback).
  const required = ['from_system', 'from_field', 'from_value', 'to_system', 'to_field', 'to_value', 'concept'];
  const missing = required.filter((k) => !body[k]);
  if (missing.length) {
    udMsg('Fill in: ' + missing.join(', '), false);
    return;
  }

  udMsg('Saving…', true);
  try {
    const result = await requestJson<MappingActionResult>('/api/proposed-mappings/user-define', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (result.status === 'accepted' || result.status === 'pending') {
      // A steward's assertion is accepted at once; anyone else's is filed
      // pending and waits in the steward queue.
      udMsg(
        result.status === 'accepted'
          ? 'Saved. Relationship added to the knowledge graph.'
          : 'Sent for review. A steward has to accept it before the knowledge graph changes.',
        true,
      );
      [
        'ud-from-system', 'ud-from-field', 'ud-from-value', 'ud-to-system',
        'ud-to-field', 'ud-to-value', 'ud-concept', 'ud-description',
      ].forEach((id) => {
        const el = document.getElementById(id) as HTMLInputElement | null;
        if (el) el.value = '';
      });
      loadProposedMappings(); // refresh: the new row is accepted or pending
    } else {
      udMsg('Save failed: ' + (result.error || 'Unknown error'), false);
    }
  } catch (err) {
    udMsg('Save failed: ' + (err as Error).message, false);
  }
}

/* ── Optimistic accept / reject ─────────────────────────────────────────── */

function setOptimistic(mappingId: string, verb: 'accept' | 'reject' | null): void {
  proposalsStore.update((s) => {
    const optimistic = { ...s.optimistic };
    if (verb) optimistic[mappingId] = verb;
    else delete optimistic[mappingId];
    return { ...s, optimistic };
  });
}

export async function acceptMapping(mappingId: string): Promise<void> {
  setOptimistic(mappingId, 'accept');
  try {
    const result = await requestJson<MappingActionResult>(
      `/api/proposed-mappings/${mappingId}/accept`,
      { method: 'POST' },
    );
    if (result.status === 'accepted') {
      loadProposedMappings(true); // silent background reconcile — no blanking
    } else {
      setOptimistic(mappingId, null); // revert optimistic state
      push('error', 'Accept failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    setOptimistic(mappingId, null);
    push('error', 'Accept failed: ' + (err as Error).message, { urgent: true });
  }
}

export async function rejectMapping(mappingId: string): Promise<void> {
  setOptimistic(mappingId, 'reject');
  try {
    const result = await requestJson<MappingActionResult>(
      `/api/proposed-mappings/${mappingId}/reject`,
      { method: 'POST' },
    );
    if (result.status === 'rejected') {
      loadProposedMappings(true); // silent background reconcile — no blanking
    } else {
      setOptimistic(mappingId, null);
      push('error', 'Reject failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    setOptimistic(mappingId, null);
    push('error', 'Reject failed: ' + (err as Error).message, { urgent: true });
  }
}

/* ── Rule lifecycle (modify loop, B2) ───────────────────────────────────── */

/** Soft deactivate/reactivate an ACCEPTED rule's GRAPH node through the
 * existing lifecycle routes (POST /api/thread/rules/{id}/deactivate|
 * reactivate — same endpoints the Digital Thread admin drawer uses). On
 * success the session-local mark flips the button, and the standard silent
 * reconcile refreshes the list without blanking it. */
export async function setRuleLifecycle(
  nodeId: string,
  action: 'deactivate' | 'reactivate',
): Promise<void> {
  try {
    await requestJson<MappingActionResult>(
      `/api/thread/rules/${encodeURIComponent(nodeId)}/${action}`,
      { method: 'POST' },
    );
    proposalsStore.update((s) => {
      const ruleLifecycle = { ...s.ruleLifecycle };
      if (action === 'deactivate') ruleLifecycle[nodeId] = 'deactivated';
      else delete ruleLifecycle[nodeId];
      return { ...s, ruleLifecycle };
    });
    loadProposedMappings(true); // silent background reconcile — no blanking
    push(
      'success',
      action === 'deactivate'
        ? 'Rule deactivated — routing will skip it (reactivate any time)'
        : 'Rule reactivated — routing will use it again',
    );
  } catch (err) {
    const verb = action === 'deactivate' ? 'Deactivate' : 'Reactivate';
    push('error', `${verb} failed: ` + (err as Error).message, { urgent: true });
  }
}

/** Requeue an ACCEPTED proposal for steward re-review (POST
 * /api/proposed-mappings/{id}/requeue). The server honestly undoes the graph
 * artifact the accept created (removed or soft-deactivated) and flips the row
 * back to pending with a review_reason. setRuleLifecycle idiom: POST, then
 * the standard silent reconcile refreshes the list without blanking it. */
export async function requeueMapping(mappingId: string): Promise<void> {
  try {
    const result = await requestJson<MappingActionResult>(
      `/api/proposed-mappings/${encodeURIComponent(mappingId)}/requeue`,
      { method: 'POST' },
    );
    if (result.status === 'requeued') {
      loadProposedMappings(true); // silent background reconcile — no blanking
      push(
        'success',
        result.graph_artifact === 'deactivated'
          ? 'Requeued for review — the applied rule was deactivated (routing skips it)'
          : 'Requeued for review — the applied rule was removed from the knowledge graph',
      );
    } else {
      push('error', 'Requeue failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    push('error', 'Requeue failed: ' + (err as Error).message, { urgent: true });
  }
}

/** Re-open a REJECTED proposal (POST /api/proposed-mappings/{id}/reopen) —
 * the ADR-0053 E6 tombstone escape hatch: the row flips back to pending and,
 * being pending, no longer matches the tombstone predicate (rejected-status
 * rows only), so the shape becomes reviewable and proposable again.
 * requeueMapping idiom: POST, then the standard silent reconcile refreshes
 * the list without blanking it; a failed POST (404 unknown id / 400
 * non-rejected) surfaces as the standard urgent error toast. */
export async function reopenMapping(mappingId: string): Promise<void> {
  try {
    // Success returns the updated row itself (status flipped to pending).
    const result = await requestJson<MappingActionResult>(
      `/api/proposed-mappings/${encodeURIComponent(mappingId)}/reopen`,
      { method: 'POST' },
    );
    if (result.status === 'pending') {
      loadProposedMappings(true); // silent background reconcile — no blanking
      push('success', 'Re-opened for review — the proposal is back in the pending queue');
    } else {
      push('error', 'Re-open failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    push('error', 'Re-open failed: ' + (err as Error).message, { urgent: true });
  }
}

/* ── Inline edit dialog (rendered by <EditOverlay/> in ./Proposals.tsx) ──── */

export async function editMapping(mappingId: string): Promise<void> {
  // Fresh copy of the SELECTED bucket (the card being edited is rendered from it).
  try {
    const data = await requestJson<ProposedMappingsResponse>(scopedMappingsUrl());
    const mapping = (data.mappings || []).find((m) => m.mapping_id === mappingId);
    if (!mapping) {
      push('error', 'Mapping not found', { urgent: true });
      return;
    }
    proposalsStore.update((s) => ({ ...s, edit: mapping }));
  } catch (err) {
    push('error', 'Failed to load mapping: ' + (err as Error).message, { urgent: true });
  }
}

export function closeEditDialog(): void {
  proposalsStore.update((s) => ({ ...s, edit: null }));
}

/** POST the edit; on success close the overlay, silently reconcile, and toast.
 * Returns null on success or the error message (the overlay displays it). */
export async function submitEdit(
  mappingId: string,
  updates: Record<string, string>,
): Promise<string | null> {
  if (Object.keys(updates).length === 0) return 'No changes to save';
  try {
    const result = await requestJson<MappingActionResult>(`/api/proposed-mappings/${mappingId}/edit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates),
    });
    if (result.mapping || result.status === 'updated') {
      closeEditDialog();
      loadProposedMappings(true); // silent refresh
      push('success', 'Proposal updated');
      return null;
    }
    return result.error || 'Unknown error';
  } catch (err) {
    return (err as Error).message;
  }
}

/* ── Bulk actions ───────────────────────────────────────────────────────── */

export async function resetAllMappings(): Promise<void> {
  const reset = await askConfirm({
    key: 'proposals:reset-accepted',
    title: 'Reset accepted mappings?',
    body: 'This will remove all accepted self-learned mappings from the knowledge graph.',
    confirmLabel: 'Reset mappings',
    tone: 'danger',
  });
  if (!reset) return;
  try {
    const result = await requestJson<MappingActionResult>('/api/proposed-mappings/reset', {
      method: 'POST',
    });
    if (result.status === 'reset') {
      loadProposedMappings(); // Refresh table
      push('success', `Reset ${result.count} mappings to pending.`);
    } else {
      push('error', 'Reset failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    push('error', 'Reset failed: ' + (err as Error).message, { urgent: true });
  }
}

export async function clearRejectedMappings(): Promise<void> {
  const clear = await askConfirm({
    key: 'proposals:clear-rejected',
    title: 'Clear rejected mappings?',
    body: 'All rejected mappings are cleared from the list.',
    confirmLabel: 'Clear rejected',
    tone: 'danger',
  });
  if (!clear) return;
  try {
    // The endpoint returns HTTP 200 even on failure, with {"error": ...} in
    // the body — check it, or the toast lies (silent AccessDenied 2026-07-19).
    const result = await requestJson<MappingActionResult>('/api/proposed-mappings/clear-rejected', {
      method: 'POST',
    });
    if (result.error) throw new Error(result.error);
    loadProposedMappings();
    push('success', `Rejected mappings cleared (${result.count ?? 0})`);
  } catch (e) {
    console.error('Clear rejected failed:', e);
    push('error', 'Clear rejected failed: ' + (e as Error).message, { urgent: true });
  }
}

export async function clearAllLearnings(): Promise<void> {
  const clear = await askConfirm({
    key: 'proposals:delete-all',
    title: 'Delete all learnings?',
    body: 'This will delete all learnings (pending, accepted, rejected) and remove accepted nodes from Neptune.',
    confirmLabel: 'Delete all learnings',
    tone: 'danger',
  });
  if (!clear) return;
  try {
    const result = await requestJson<MappingActionResult>('/api/proposed-mappings/clear-all', {
      method: 'POST',
    });
    if (result.error) throw new Error(result.error);
    loadProposedMappings();
    push('success', `All learnings cleared (${result.count ?? 0})`);
  } catch (e) {
    console.error('Clear all failed:', e);
    push('error', 'Clear all failed: ' + (e as Error).message, { urgent: true });
  }
}

export async function acceptAllPendingMappings(): Promise<void> {
  const accept = await askConfirm({
    key: 'proposals:accept-all',
    title: 'Accept all pending mappings?',
    body: 'Every pending mapping is accepted and written to the knowledge graph.',
    confirmLabel: 'Accept all',
  });
  if (!accept) return;
  try {
    const result = await requestJson<MappingActionResult>('/api/proposed-mappings/accept-all', {
      method: 'POST',
    });
    if (result.error) throw new Error(result.error);
    loadProposedMappings();
    // skipped_insights (additive key): findings are never bulk-accepted — say
    // so when any were left pending. Escape-safe: coerced to a finite number,
    // never interpolated as raw server text.
    const skipped = Number(result.skipped_insights);
    const suffix =
      Number.isFinite(skipped) && skipped > 0
        ? ` (${skipped} insight${skipped === 1 ? '' : 's'} left pending for review)`
        : '';
    push('success', `Accepted ${result.count || 0} mappings${suffix}`);
  } catch (e) {
    console.error('Accept all failed:', e);
    push('error', 'Accept all failed: ' + (e as Error).message, { urgent: true });
  }
}

/* ── Learned Guidance (accumulated query_guidance) ──────────────────────── */

export async function loadLearnedGuidance(): Promise<void> {
  proposalsStore.update((s) => ({ ...s, guidancePhase: 'loading' }));
  try {
    const data = await requestJson<QueryGuidanceResponse>('/api/query-guidance');
    const systems = (data.systems || []).filter((s) => (s.notes || []).length > 0);
    proposalsStore.update((s) => ({
      ...s,
      guidancePhase: 'ready',
      guidance: systems,
      guidanceError: '',
    }));
  } catch (err) {
    console.error('Failed to load learned guidance:', err);
    proposalsStore.update((s) => ({
      ...s,
      guidancePhase: 'error',
      guidanceError: (err as Error).message,
    }));
  }
}

export async function pruneGuidanceNote(systemId: string, note: string): Promise<void> {
  const remove = await askConfirm({
    key: `guidance:prune:${systemId}:${note}`,
    title: 'Remove learned note?',
    body: 'This removes the learned note from the system guidance.',
    confirmLabel: 'Remove note',
    tone: 'danger',
  });
  if (!remove) return;
  try {
    const result = await requestJson<GuidanceActionResult>(
      `/api/query-guidance/${encodeURIComponent(systemId)}/prune`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note }),
      },
    );
    if (result.status === 'pruned') {
      loadLearnedGuidance();
      push('success', 'Learned note pruned');
    } else {
      push('error', 'Prune failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    push('error', 'Prune failed: ' + (err as Error).message, { urgent: true });
  }
}

export async function clearSystemGuidance(systemId: string): Promise<void> {
  const clear = await askConfirm({
    key: `guidance:clear:${systemId}`,
    title: `Remove all learned notes for ${systemName(systemId)}?`,
    body: 'The onboarded base guidance is kept.',
    confirmLabel: 'Remove all notes',
    tone: 'danger',
  });
  if (!clear) return;
  try {
    const result = await requestJson<GuidanceActionResult>(
      `/api/query-guidance/${encodeURIComponent(systemId)}/clear`,
      { method: 'POST' },
    );
    if (result.status === 'cleared') {
      loadLearnedGuidance();
      push('success', `Cleared ${result.removed || 0} learned note${result.removed === 1 ? '' : 's'}`);
    } else {
      push('error', 'Clear failed: ' + (result.error || 'Unknown error'), { urgent: true });
    }
  } catch (err) {
    push('error', 'Clear failed: ' + (err as Error).message, { urgent: true });
  }
}

/* ── Window-global registration (documented cross-module shims) ─────────── */

/** The acceptMapping/rejectMapping/editMapping family stays window-registered
 * (documented shim set — legacy inline links in md-rendered content and the
 * classic-script contract). The React cards call the module actions directly;
 * `_submitEdit` was only ever invoked by the generated overlay HTML and has no
 * remaining consumer now that <EditOverlay/> is a React component — dropped. */
export function registerProposalGlobals(): void {
  const w = window as unknown as Record<string, unknown>;
  w.acceptMapping = acceptMapping;
  w.rejectMapping = rejectMapping;
  w.editMapping = editMapping;
  w.pruneGuidanceNote = pruneGuidanceNote;
  w.clearSystemGuidance = clearSystemGuidance;
}
