// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboard module-graph init smoke test (Task 2.4). The ported modules keep
 * their original cross-references, which become ESM import cycles
 * (flow ↔ chat ↔ catalog ↔ wizard). All calls are runtime-only, so the graph
 * must evaluate cleanly — this test catches any TDZ/circular-init regression
 * without needing a DOM. */
import { describe, expect, it } from 'vitest';

describe('onboard module graph', () => {
  it('evaluates the full cyclic module graph without errors', async () => {
    // Sequential imports: vite-node's runner can deadlock on CONCURRENT dynamic
    // imports of circularly-dependent modules; a single entry pulls the whole
    // graph, and the rest resolve from cache.
    const view = await import('../views/onboard/index');
    const flow = await import('../views/onboard/flow');
    const chat = await import('../views/onboard/chat');
    const catalog = await import('../views/onboard/catalog');
    const wizard = await import('../views/onboard/wizard');
    const globals = await import('../views/onboard/globals');
    const state = await import('../views/onboard/state');
    const flowStore = await import('../views/onboard/flow-store');
    const catalogStore = await import('../views/onboard/catalog-store');
    const suggestions = await import('../views/onboard/suggestions-store');
    // Cross-cycle exports resolve to callable functions (live bindings intact).
    expect(typeof flow.obGuardStart).toBe('function');
    expect(typeof flow.obResetAll).toBe('function');
    expect(typeof flow.obAppendCheckpoint).toBe('function');
    expect(typeof chat.obWelcomeChat).toBe('function');
    expect(typeof chat.obStreamChat).toBe('function');
    expect(typeof chat.welcomeSubmit).toBe('function');
    expect(typeof chat.refreshSuggestions).toBe('function');
    expect(typeof catalog.obLoadCatalog).toBe('function');
    expect(typeof catalog.startCustomOnboard).toBe('function');
    expect(typeof wizard.obStart).toBe('function');
    expect(typeof globals.registerOnboardGlobals).toBe('function');
    // Shared workflow state starts idle; an unpopulated query cache is honest.
    expect(state.ob.active).toBeNull();
    expect(state.getObConcepts()).toEqual([]);
    // The jsx-migration stores evaluate DOM-free; catalogStore is UI-only.
    expect(flowStore.flowStore.get().mode).toBe('welcome');
    expect(catalogStore.catalogStore.get().connValues).toEqual({});
    expect(suggestions.suggestionsStore.get().kind).toBe('loading');
    // The mountable component is exported for the Task-3.2 integration pass.
    expect(typeof view.OnboardView).toBe('function');
    expect(view.default).toBe(view.OnboardView);
  });
});
