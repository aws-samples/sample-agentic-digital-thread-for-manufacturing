// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A reopened session shows what its live stream showed, under the
 * response-integrity gate too.
 *
 * When the live chat runs the gate (Mode A, SYSTEM_QUERY_MODE other than
 * orchestrator, with RESPONSE_INTEGRITY_MODE=enforce), the stream carries each
 * call's tool name and a metadata-only result summary, never the model's
 * narration, the call inputs or the rows. Mode B with enforce set never runs
 * the gate, so its stream is the usual one. The payloads come from
 * fixtures/session-reload-events.json, the events route's own output pinned by
 * tests/unit/test_memory_events_rehydrate.py, so the server and the chat
 * halves test one payload. Each reload is compared with the same turn
 * replayed as its live stream. HIDDEN_* markers stand for content the gated
 * stream never shows. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MemoryEventsResponse, MemoryTurnBlock } from '../api/types/memory';
import { ChatThread } from '../chat/ChatThread';
import { rehydrateSession } from '../chat/rehydrate';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  'fixtures/session-reload-events.json',
);
type Scenario = { payload: MemoryEventsResponse };
const SCENARIOS = (JSON.parse(readFileSync(FIXTURE, 'utf8')) as {
  scenarios: Record<string, Scenario>;
}).scenarios;

const HIDDEN = /HIDDEN_[A-Z]+/;

const mounted: Array<{ root: Root; el: HTMLElement }> = [];

afterEach(() => {
  for (const { root, el } of mounted.splice(0)) {
    act(() => root.unmount());
    el.remove();
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount(store: ThreadStore): HTMLElement {
  const el = document.createElement('div');
  document.body.appendChild(el);
  const root = createRoot(el);
  act(() => root.render(<ChatThread store={store} />));
  mounted.push({ root, el });
  return el;
}

async function reopen(name: string): Promise<{ el: HTMLElement; store: ThreadStore }> {
  const payload = SCENARIOS[name].payload;
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload } as Response),
  );
  const store = createThreadStore();
  const applied = await rehydrateSession('sess-1', {
    store,
    adoptSession: vi.fn(),
    resetModal: vi.fn(),
    showThread: vi.fn(),
    refreshMemory: vi.fn(),
    onError: vi.fn(),
  });
  expect(applied).toBe(true);
  return { el: mount(store), store };
}

/** The result summary the route reported for a call ('' when absent). */
function summaryOf(name: string, toolId: string): string {
  for (const m of SCENARIOS[name].payload.messages || []) {
    for (const b of (m.blocks || []) as MemoryTurnBlock[]) {
      if (b.type === 'tool_result' && b.tool_id === toolId) return b.summary || '';
    }
  }
  return '';
}

type LiveCall = { tool: string; id: string; input: unknown; summary: string };

/** Replay one turn as its live SSE stream (tool_start input is null on the
 * gated stream; the gate's answer is one token after the calls). */
function liveTurn(store: ThreadStore, question: string, narration: string, calls: LiveCall[], answer: string) {
  store.startTurn(question);
  if (narration) store.applyEvent('token', { text: narration });
  for (const c of calls) {
    store.applyEvent('tool_start', { tool: c.tool, tool_id: c.id, input: c.input });
    store.applyEvent('tool_result', { tool: c.tool, tool_id: c.id, summary: c.summary, input: null });
    store.applyEvent('tool_end', { tool: c.tool, tool_id: c.id });
  }
  store.applyEvent('token', { text: answer });
  store.applyEvent('done', {});
}

function live(build: (store: ThreadStore) => void): HTMLElement {
  const store = createThreadStore();
  act(() => build(store));
  // The reload has no call timing and no feedback id; compare like for like.
  for (const turn of store.getState().turns) {
    turn.assistant.messageId = null;
    for (const t of turn.assistant.traces.values()) t.elapsedMs = undefined;
  }
  return mount(store);
}

/** The Reasoning and Sources structure of each assistant turn. */
function structure(el: HTMLElement) {
  return [...el.querySelectorAll<HTMLElement>('.message.assistant')].map((turn) => ({
    calls: turn.querySelectorAll('.tool-pill.inline').length,
    fold: turn.querySelector('.reasoning-fold-title')?.textContent ?? null,
    foldText: turn.querySelector('.reasoning-fold-text')?.textContent ?? null,
    foldMeta: turn.querySelector('.reasoning-fold-meta')?.textContent ?? null,
    sources: [...turn.querySelectorAll<HTMLElement>('.answer-sources a.source-chip')].map(
      (a) => a.textContent,
    ),
  }));
}

function gatedCalls(name: string): LiveCall[] {
  return [
    { tool: 'explore_graph', id: 'tooluse_g1', input: null, summary: summaryOf(name, 'tooluse_g1') },
    { tool: 'erp___query_erp', id: 'tooluse_g2', input: null, summary: summaryOf(name, 'tooluse_g2') },
  ];
}

const LEGACY_CALLS: LiveCall[] = [
  { tool: 'explore_graph', id: 'tooluse_l1', input: { question: 'open orders' }, summary: '' },
  { tool: 'erp___query_erp', id: 'tooluse_l2', input: { sql: 'select id from orders where open' }, summary: '' },
];
const LEGACY_ANSWER =
  'Two orders are still open [1].\n\n---FOLLOWUPS---\nQ1: Which order is oldest?\nA1: Notify the order owner\n';

function legacyCalls(name: string): LiveCall[] {
  return LEGACY_CALLS.map((c) => ({ ...c, summary: summaryOf(name, c.id) }));
}

describe('reload under the response-integrity gate (Mode A, enforce)', () => {
  it('never renders the narration, the call inputs, the rows or the draft claims', async () => {
    const { el, store } = await reopen('mode_a_enforce');
    expect(el.textContent || '').not.toMatch(HIDDEN);
    // Not in the rebuilt model either (an expanded trace renders its input).
    for (const turn of store.getState().turns) {
      for (const t of turn.assistant.traces.values()) {
        expect(JSON.stringify(t.toolInput ?? null)).not.toMatch(HIDDEN);
        expect(t.resultMd).not.toMatch(HIDDEN);
      }
      expect(turn.assistant.segments.completedRuns.join('')).not.toMatch(HIDDEN);
      expect(turn.assistant.answerMd ?? '').not.toMatch(HIDDEN);
    }
  });

  it('shows the question first and the same Reasoning and Sources as the live stream', async () => {
    const { el } = await reopen('mode_a_enforce');
    const users = [...el.querySelectorAll<HTMLElement>('.message.user')];
    expect(users.map((u) => u.getAttribute('data-raw-content'))).toEqual([
      'How many orders shipped late last week?',
    ]);
    const liveEl = live((s) =>
      liveTurn(s, 'How many orders shipped late last week?', '', gatedCalls('mode_a_enforce'), 'Seven orders shipped late [1].'),
    );
    const reloaded = structure(el);
    expect(reloaded).toEqual(structure(liveEl));
    expect(reloaded[0].calls).toBe(2);
    expect(reloaded[0].fold).toBe('Reasoning');
    expect(reloaded[0].sources).toHaveLength(1);
  });

  it('a turn stored under the gate stays gated after the gate is switched off', async () => {
    const { el } = await reopen('gate_turned_off');
    expect(el.textContent || '').not.toMatch(HIDDEN);
    const liveEl = live((s) => {
      liveTurn(s, 'How many orders shipped late last week?', '', gatedCalls('gate_turned_off'), 'Seven orders shipped late [1].');
      liveTurn(s, 'Which orders are still open?', 'Checking the order history first.', legacyCalls('gate_turned_off'), LEGACY_ANSWER);
    });
    expect(structure(el)).toEqual(structure(liveEl));
    // The later legacy turn keeps its narration, as it streamed.
    const turns = el.querySelectorAll<HTMLElement>('.message.assistant');
    expect(turns[1].querySelector('.tool-calls-details')?.textContent).toContain(
      'Checking the order history first.',
    );
  });
});

describe('reload in Mode B with enforce set (the gate never runs)', () => {
  it('rebuilds the legacy stream: narration, inputs, answer, Sources and chips', async () => {
    const { el } = await reopen('orchestrator_enforce');
    const liveEl = live((s) =>
      liveTurn(s, 'Which orders are still open?', 'Checking the order history first.', legacyCalls('orchestrator_enforce'), LEGACY_ANSWER),
    );
    expect(structure(el)).toEqual(structure(liveEl));
    expect(structure(el)[0].calls).toBe(2);
    const fold = el.querySelector('details.tool-calls-details');
    expect(fold?.textContent).toContain('Checking the order history first.');
    expect(el.querySelector('.answer-body')?.textContent).toBe(
      liveEl.querySelector('.answer-body')?.textContent,
    );
    const chips = [...el.querySelectorAll<HTMLButtonElement>('.followup-chips .suggestion-chip')];
    expect(chips.map((c) => c.dataset.question)).toEqual([
      'Which order is oldest?',
      'Notify the order owner',
    ]);
  });
});
