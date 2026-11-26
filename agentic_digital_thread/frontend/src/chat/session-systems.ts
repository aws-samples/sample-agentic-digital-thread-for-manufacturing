// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* G3 (window._sessionSystemsUsed): canonical system ids
 * queried during this session, accumulated from gateway tool-name prefixes
 * ("<system_id>___<tool>") on tool_start events. Never cleared on new turns —
 * the save-as-prompt prefill reflects the whole conversation. Non-prefixed
 * tools (explore_graph etc.) are ignored. Module-scoped instead of the legacy
 * window global: both the producer (useAskStream) and the consumer
 * (PromptLibrary SaveModal) live in src/chat. */

const sessionSystemsUsed = new Set<string>();

/** Record the canonical system id from a tool_start tool name, if prefixed. */
export function noteSessionSystem(tool: unknown): void {
  if (typeof tool === 'string' && tool.includes('___')) {
    sessionSystemsUsed.add(tool.split('___')[0]);
  }
}

/** Sorted, comma-joined ids for the save-modal required-systems prefill
 * (post-conversation saves only — the text box remains the override). */
export function sessionSystemsPrefill(): string {
  return [...sessionSystemsUsed].sort().join(', ');
}

/** Test-only reset (module state persists across vitest cases). */
export function _resetSessionSystems(): void {
  sessionSystemsUsed.clear();
}
