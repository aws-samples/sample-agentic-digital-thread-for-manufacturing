// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Task 2.1 — module-graph smoke test. The chat tree is wired into the shell
 * only in Task 3.2, so no vite entry pulls it yet; this import guarantees the
 * FULL src/chat graph (components + engine + verbatim-lib bridge + npm deps)
 * resolves and the public interface exports what Task 3.2 will mount. */
import { describe, expect, it } from 'vitest';

describe('src/chat public interface', () => {
  // The full chat graph can exceed Vitest's 5s default under parallel suite load.
  it('imports the full module graph and exposes the mountable surface', async () => {
    const chat = await import('../chat/index');
    expect(typeof chat.ChatSidebar).toBe('function'); // default-exported component
    expect(typeof chat.ChatModal).toBe('function');
    expect(typeof chat.ChatThread).toBe('function'); // W3.1 store-driven thread
    expect(typeof chat.AssistantTurn).toBe('function');
    expect(typeof chat.chatThreadStore?.applyEvent).toBe('function'); // the ONE store
    expect(typeof chat.FollowupChips).toBe('function'); // replaced buildFollowupChips
    expect(typeof chat.MemoryPanel).toBe('function');
    expect(typeof chat.SessionMenu).toBe('function');
    expect(typeof chat.PromptLibrary).toBe('function');
    expect(typeof chat.SaveModal).toBe('function');
    expect(typeof chat.parseAnswerBlocks).toBe('function');
    expect(typeof chat.mdToSafeHtml).toBe('function');
    expect(typeof chat.selectFinalAnswer).toBe('function');
    expect(typeof chat.getSystemLabel).toBe('function');
    expect(typeof chat.handleArchEvent).toBe('function');
    expect(typeof chat.resetArchitectureState).toBe('function');
    expect(chat.MEMORY_COLLAPSED_KEY).toBe('adt-memory-collapsed'); // exact legacy key
  }, 20_000);

  it('keeps the legacy tool-label contract', async () => {
    const { getSystemLabel, getToolLabel } = await import('../chat/index');
    expect(getSystemLabel('explore_graph')).toBe('Knowledge Graph');
    // One-line names through the shared resolver (no catalog loaded here,
    // so the id fallback; a plant name only ever comes from the catalog).
    expect(getSystemLabel('mes-mil___query_mes_mil')).toBe('MES Mil');
    expect(getSystemLabel('query_mes_cedar_rapids')).toBe('MES Cedar Rapids');
    expect(getToolLabel('explore_graph')).toBe('Querying knowledge graph');
    expect(getToolLabel('erp___query_erp')).toBe('ERP — Query Erp');
  });
});
