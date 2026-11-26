// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Persona-suggestions store (jsx-migration W5.1) — ONE store behind BOTH
 * `.persona-groups-container` hosts (the chat sidebar's
 * #persona-groups-container and the pop-out modal's
 * #modal-persona-groups-container). refreshSuggestions() (chat.ts) fetches
 * and reduces into this store; <PersonaGroups/> renders it in both places —
 * replacing the old "innerHTML into every matching container" builder. */
import { createStore } from '../../lib/store';

export interface PersonaPromptVm {
  templateId: string;
  title: string;
  /** Full text sent to the orchestrator. */
  text: string;
  /** Short display text shown as the user message (display_text || title). */
  display?: string;
}

export interface PersonaGroupVm {
  persona: string;
  prompts: PersonaPromptVm[];
}

export type SuggestionsState =
  /** Boot state — first /api/onboard/status + /api/prompts reduce pending. */
  | { kind: 'loading' }
  /** No-systems seed chip. */
  | { kind: 'default' }
  /** The status/prompts read failed before any suggestions loaded; a retry
   * is scheduled. Never shown as "no systems": the systems may be fine. */
  | { kind: 'error' }
  /** /api/onboard/status answered 403: this user's role cannot read it. No
   * retry is scheduled; retrying cannot change a role. */
  | { kind: 'forbidden' }
  /** Single non-clickable "Onboard a system…" hint. */
  | { kind: 'single'; text: string }
  /** Plain question chips (no registry prompts) → askQuestion(q). */
  | { kind: 'plain'; questions: string[] }
  | { kind: 'personas'; groups: PersonaGroupVm[] };

export const suggestionsStore = createStore<SuggestionsState>({ kind: 'loading' });

export function setSuggestions(state: SuggestionsState): void {
  suggestionsStore.set(state);
}
