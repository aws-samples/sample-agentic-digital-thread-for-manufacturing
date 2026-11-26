// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Bidirectional graph ↔ trace ↔ HUD call correlation (arch-interactivity).
 *
 * One tool call is identified everywhere by its `callKey` — the HUD row id
 * minted at tool_start (`hud-<tool>-q<N>-<ts>`), threaded by the arch-store
 * onto the call's trace entry (`TraceEntry.callKey`) and its minted graph
 * nodes (`_callKey` on qinput / finding / entity / error nodes; `callKey` in
 * the cy node data).
 *
 * Selecting a call from ANY surface publishes {source, callKey} here:
 *  - ToolHud rows + TraceEntries rows subscribe and render `.correlated`;
 *  - inv-graph subscribes, applies the existing `.highlighted` class to the
 *    call's nodes/edges, and (for trace/hud-sourced selections) pans to the
 *    subtree via hudAwareFit. Graph-sourced selections do NOT re-pan (the
 *    user is already looking at the node).
 * Clicking empty graph space (or re-clicking the same row on the same
 * surface) clears the selection. */
import { createStore } from '../../lib/store';

export type CorrelationSource = 'graph' | 'trace' | 'hud';

export interface CorrelationSelection {
  source: CorrelationSource;
  /** The call's HUD row id (tool_start hudId) — pairs toolUseId-less SSE
   * events by the pending-call identity the store already tracks. */
  callKey: string;
}

/** Shared selection — null = nothing correlated. */
export const correlationStore = createStore<CorrelationSelection | null>(null);

/** Select a call from one surface. Re-selecting the SAME call from the SAME
 * surface toggles the selection off; selecting it from another surface just
 * moves the source (highlights stay). */
export function selectCall(source: CorrelationSource, callKey: string | undefined | null): void {
  if (!callKey) return;
  const cur = correlationStore.get();
  if (cur && cur.callKey === callKey && cur.source === source) {
    correlationStore.set(null);
    return;
  }
  correlationStore.set({ source, callKey });
}

/** Clear the correlation (empty-space click, new question, reset). */
export function clearCorrelation(): void {
  correlationStore.set(null);
}
