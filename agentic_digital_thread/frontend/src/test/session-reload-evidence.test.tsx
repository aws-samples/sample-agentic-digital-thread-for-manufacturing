// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A reopened answer's Sources open the evidence cards they opened live.
 *
 * A live answer's Sources chips open their evidence cards from the turn's
 * `provenance` metadata frame. The Recent sessions reload rebuilt the chips
 * from the stored turn, which holds no metadata frames, so the chips
 * rendered but opened no card. The events route now returns the frame the
 * live stream sent on the reloaded question, and the reload replays it
 * through the same reducer before the turn's end. A session stored before
 * that (or past the server's keep window) says plainly that its cards are
 * not available.
 *
 * The payloads come from fixtures/session-reload-evidence.json, written by
 * tests/unit/test_reload_evidence_cards.py from the production /api/ask stream
 * and the events route, so the server and the chat halves test one payload.
 * The schema names are made up. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemoryEventsResponse } from '../api/types/memory';
import { ChatThread } from '../chat/ChatThread';
import { boundRecordFor } from '../chat/provenance';
import { rehydrateSession } from '../chat/rehydrate';
import { createThreadClickHandler } from '../chat/thread-clicks';
import { chatThreadStore } from '../chat/useAskStream';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  'fixtures/session-reload-evidence.json',
);
type Frame = { event: string; data: Record<string, unknown> };
type Scenario = { question: string; live: Frame[] | null; payload: MemoryEventsResponse };
const SCENARIOS = (JSON.parse(readFileSync(FIXTURE, 'utf8')) as {
  scenarios: Record<string, Scenario>;
}).scenarios;

const NOT_KEPT = 'Evidence cards for these sources are no longer available.';
const NO_EVIDENCE = 'No captured evidence for this claim';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  act(() => {
    vi.runOnlyPendingTimers();
  });
  vi.useRealTimers();
  act(() => chatThreadStore.clear());
  vi.unstubAllGlobals();
});

/** The turn as its live SSE stream built it. */
function playLive(name: string): void {
  const { question, live } = SCENARIOS[name];
  act(() => {
    chatThreadStore.clear();
    chatThreadStore.startTurn(question);
    for (const frame of live ?? []) chatThreadStore.applyEvent(frame.event, frame.data);
  });
}

/** The same session reopened from Recent sessions. */
async function reopen(name: string): Promise<void> {
  act(() => chatThreadStore.clear());
  const payload = SCENARIOS[name].payload;
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload } as Response),
  );
  const applied = await rehydrateSession('sess-1', {
    store: chatThreadStore,
    adoptSession: vi.fn(),
    resetModal: vi.fn(),
    showThread: vi.fn(),
    refreshMemory: vi.fn(),
    onError: vi.fn(),
  });
  expect(applied).toBe(true);
}

function lastAssistant() {
  return chatThreadStore.getState().turns.at(-1)!.assistant;
}

/** Each Sources chip of the shown turn: its label, its evidence badge, and
 * the text of the card a click opens (null when no card opens). */
function chipsAndCards(): Array<{ label: string; badge?: string; card: string | null }> {
  act(() => root.render(<ChatThread store={chatThreadStore} />));
  const click = createThreadClickHandler({ askQuestion: vi.fn(), savePrompt: vi.fn() });
  const chips = [...container.querySelectorAll<HTMLAnchorElement>('.answer-sources a.source-chip')];
  return chips.map((chip) => {
    act(() => click({ target: chip, preventDefault: vi.fn() }));
    const card = container.querySelector<HTMLElement>('.evidence-card');
    const text = card ? card.textContent : null;
    act(() => chatThreadStore.openEvidence(null));
    return { label: chip.textContent ?? '', badge: chip.dataset.verification, card: text };
  });
}

/** The record each citation binds to and the turn's graph queries. */
function bindings() {
  const a = lastAssistant();
  return {
    records: a.citations.map((c) => boundRecordFor(a.provenance, a.pills.values(), c.num)),
    graphQueries: a.graphQueries,
  };
}

describe('a reopened answer opens the evidence cards it opened live', () => {
  it.each(['open', 'renamed', 'two_frames'])('%s: the same records, chips and cards', async (name) => {
    playLive(name);
    const liveBindings = bindings();
    const liveChips = chipsAndCards();
    act(() => root.render(<></>));

    await reopen(name);
    expect(bindings()).toEqual(liveBindings);
    expect(chipsAndCards()).toEqual(liveChips);
    // Three cited system calls, each opening its own card.
    expect(liveChips).toHaveLength(3);
    expect(liveChips.every((c) => c.card !== null)).toBe(true);
    expect(container.textContent).not.toContain(NOT_KEPT);
  });

  it('gated: the same cards as the gated stream, and nothing it hid', async () => {
    playLive('gated');
    const liveBindings = bindings();
    const liveChips = chipsAndCards();
    act(() => root.render(<></>));

    await reopen('gated');
    expect(bindings()).toEqual(liveBindings);
    expect(chipsAndCards()).toEqual(liveChips);
    expect(liveChips.map((c) => c.badge)).toEqual(['verified', 'verified', 'verified']);
    expect(container.textContent || '').not.toMatch(/HIDDEN_[A-Z]+/);
  });

  it('api_read: calls that read their source through its API reopen the same partial cards', async () => {
    // The calls ran no SQL; each system agent wrote a read entry, so the
    // records are partial live and on reload, and no card claims nothing
    // was captured.
    playLive('api_read');
    const liveBindings = bindings();
    const liveChips = chipsAndCards();
    act(() => root.render(<></>));

    await reopen('api_read');
    expect(bindings()).toEqual(liveBindings);
    expect(chipsAndCards()).toEqual(liveChips);
    expect(liveChips.map((c) => c.badge)).toEqual(['partial', 'partial', 'partial']);
    expect(liveChips.every((c) => c.card !== null && !c.card.includes(NO_EVIDENCE))).toBe(true);
  });
});

describe('a reopened answer whose evidence was not kept', () => {
  it('keeps its Sources chips, opens no card and says so plainly', async () => {
    await reopen('legacy');
    const chips = chipsAndCards();
    expect(chips).toHaveLength(3);
    expect(chips.every((c) => c.card === null && c.badge === undefined)).toBe(true);
    const notes = [...container.querySelectorAll('.answer-evidence-note')];
    expect(notes.map((n) => n.textContent)).toEqual([NOT_KEPT]);
  });

  it('a live answer never carries the note', () => {
    playLive('open');
    chipsAndCards();
    expect(container.querySelector('.answer-evidence-note')).toBeNull();
  });
});
