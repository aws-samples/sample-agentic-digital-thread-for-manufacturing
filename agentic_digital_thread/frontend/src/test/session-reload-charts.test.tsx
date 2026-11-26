// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A tool's charts come back when a chat is reopened from Recent sessions.
 *
 * Live, a tool chart reaches the page as answer text the runtime injects right
 * after the tool result that carried it ("\n\n" + tags + "\n\n"); AnswerSegments
 * harvests the tags and puts them above the selected answer. The events route
 * now returns those tags on the tool_result block (`charts`, read from the
 * stored tool result), and the reload replays them as the same token right
 * after that result, so the same placement code runs. A `charts` entry that is
 * not a single complete chart tag is dropped. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemoryEvent } from '../api/types/memory';
import { ChatThread } from '../chat/ChatThread';
import { buildRehydrateThread, rehydrateSession, type RehydrateDeps } from '../chat/rehydrate';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const TAG_A =
  `<chart-block type="line" title="unit-7: alpha" labels='["t1","t2"]' ` +
  `datasets='[{"label":"alpha","data":[1,2]}]' />`;
const TAG_B =
  `<chart-block type="line" title="unit-7: beta" labels='["t1","t2"]' ` +
  `datasets='[{"label":"beta","data":[3,4]}]' />`;
const Q = 'Show both series for unit 7';
const SYS = 'unit-log___query_unit_log';
const CHART_CALL = { tool: SYS, tool_id: 't1', input: { question: 'both series' } };
const CHART_RESULT = { tool: SYS, tool_id: 't1', summary: 'Two series over the window.' };
const LATER_CALL = { tool: SYS, tool_id: 't2', input: { question: 'limits' } };
const LATER_RESULT = { tool: SYS, tool_id: 't2', summary: '1 row' };
const ANSWER = 'The chart above shows both series over the window.';

/** The stored turn as GET /api/memory/events returns it: a chart-bearing
 * call, a later call, then the answer. */
function storedTurn(charts: unknown[] | undefined): MemoryEvent[] {
  return [
    { role: 'user', content: Q, blocks: [] },
    { role: 'assistant', content: `Tool call: ${SYS}`, blocks: [{ type: 'tool_use', ...CHART_CALL }] },
    {
      role: 'user',
      content: 'Tool result',
      blocks: [
        {
          type: 'tool_result',
          status: 'success',
          ...CHART_RESULT,
          ...(charts === undefined ? {} : { charts: charts as string[] }),
        },
      ],
    },
    { role: 'assistant', content: `Tool call: ${SYS}`, blocks: [{ type: 'tool_use', ...LATER_CALL }] },
    { role: 'user', content: 'Tool result', blocks: [{ type: 'tool_result', status: 'success', ...LATER_RESULT }] },
    { role: 'assistant', content: ANSWER, blocks: [{ type: 'text', text: ANSWER }] },
  ];
}

/** The same turn as the live stream drove the store: the runtime's chart
 * text arrives right after the chart-bearing result. */
function liveStore(): ThreadStore {
  const live = createThreadStore();
  live.startTurn(Q);
  live.applyEvent('tool_start', CHART_CALL);
  live.applyEvent('tool_result', CHART_RESULT);
  live.applyEvent('tool_end', { tool: SYS, tool_id: 't1' });
  live.applyEvent('token', { text: `\n\n${TAG_A}\n\n${TAG_B}\n\n` });
  live.applyEvent('tool_start', LATER_CALL);
  live.applyEvent('tool_result', LATER_RESULT);
  live.applyEvent('tool_end', { tool: SYS, tool_id: 't2' });
  live.applyEvent('token', { text: ANSWER });
  live.applyEvent('done', {});
  return live;
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function deps(store: ThreadStore): RehydrateDeps {
  return {
    store,
    adoptSession: vi.fn(),
    resetModal: vi.fn(),
    showThread: vi.fn(),
    refreshMemory: vi.fn(),
    onError: vi.fn(),
  };
}

let roots: Array<{ root: Root; host: HTMLDivElement }> = [];

function render(store: ThreadStore): HTMLDivElement {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(<ChatThread store={store} />));
  roots.push({ root, host });
  return host;
}

async function reopen(messages: MemoryEvent[]): Promise<HTMLDivElement> {
  const store = createThreadStore();
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ messages })));
  expect(await rehydrateSession('sess-1', deps(store))).toBe(true);
  return render(store);
}

/** What the answer body shows, in document order: each chart (by its
 * srcdoc) and each text paragraph. */
function answerParts(host: HTMLElement): string[] {
  const body = host.querySelector('.message.assistant .answer-body');
  expect(body).not.toBeNull();
  const parts: string[] = [];
  const walk = (node: Element) => {
    for (const child of node.children) {
      if (child.tagName === 'IFRAME') parts.push(`chart:${child.getAttribute('srcdoc')}`);
      else if (child.tagName === 'P') parts.push(`text:${child.textContent}`);
      else walk(child);
    }
  };
  walk(body!);
  return parts;
}

beforeEach(() => {
  roots = [];
});

afterEach(() => {
  for (const { root, host } of roots) {
    act(() => root.unmount());
    host.remove();
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Recent sessions reload brings back the charts a tool drew', () => {
  it('renders the charts above the answer, as the live answer did', async () => {
    const reopened = await reopen(storedTurn([TAG_A, TAG_B]));
    const live = render(liveStore());
    const parts = answerParts(reopened);
    expect(parts.filter((p) => p.startsWith('chart:'))).toHaveLength(2);
    // Both charts come before the answer text that refers to them.
    expect(parts.findIndex((p) => p.startsWith('text:'))).toBe(2);
    expect(parts.at(-1)).toBe(`text:${ANSWER}`);
    expect(parts).toEqual(answerParts(live));
  });

  it('replays the same answer text the live stream built', () => {
    const { turns } = buildRehydrateThread(storedTurn([TAG_A, TAG_B]));
    const live = liveStore().getState().turns[0];
    expect(turns[0].assistant.answerMd).toBe(live.assistant.answerMd);
    expect(turns[0].assistant.answerMd?.indexOf(TAG_A)).toBe(0);
  });

  it('drops a charts entry that is not one complete chart tag', () => {
    const { turns } = buildRehydrateThread(
      storedTurn([
        TAG_A,
        42,
        '<script>alert(1)</script>',
        '<chart-block type="line" title="cut',
        `prose before ${TAG_B}`,
        `${TAG_B}\n${TAG_B}`,
        { tag: TAG_B },
      ]),
    );
    const answer = turns[0].assistant.answerMd;
    expect(answer).toContain(TAG_A);
    expect(answer).not.toContain('unit-7: beta');
    expect(answer).not.toContain('script');
    expect(answer).not.toContain('title="cut');
    expect(answer).not.toContain('prose before');
  });

  it('adds nothing when the block carries no charts', () => {
    const { turns } = buildRehydrateThread(storedTurn(undefined));
    expect(turns[0].assistant.answerMd).toBe(ANSWER);
    const notAList = buildRehydrateThread(storedTurn('nope' as unknown as unknown[]));
    expect(notAList.turns[0].assistant.answerMd).toBe(ANSWER);
  });
});

/* The events route marks a question `charts_not_shown` when its turn's stored
 * results held charts the reload does not replay (no record of what its
 * stream sent, a gated turn, or the response's chart byte bound). The chat
 * says so beside that answer, once, in plain words. */
describe('Recent sessions reload says when an answer\'s charts are not shown', () => {
  const NOTE = 'Charts drawn for this answer are not all shown in a reopened chat.';

  function marked(charts: unknown[] | undefined, mark: boolean): MemoryEvent[] {
    const events = storedTurn(charts);
    events[0] = { ...events[0], ...(mark ? { charts_not_shown: true } : {}) };
    return events;
  }

  it('shows one plain note beside a marked answer', async () => {
    const host = await reopen(marked(undefined, true));
    const notes = [...host.querySelectorAll('.answer-charts-note')];
    expect(notes.map((n) => n.textContent)).toEqual([NOTE]);
    expect(host.querySelector('.message.assistant')?.contains(notes[0])).toBe(true);
    expect(answerParts(host).some((p) => p.startsWith('chart:'))).toBe(false);
  });

  it('shows no note on an answer whose charts were restored', async () => {
    const host = await reopen(marked([TAG_A, TAG_B], false));
    expect(host.querySelector('.answer-charts-note')).toBeNull();
    expect(answerParts(host).filter((p) => p.startsWith('chart:'))).toHaveLength(2);
  });

  it('marks only the turn the server marked, and never a live turn', () => {
    const { turns } = buildRehydrateThread([...marked(undefined, true), ...marked([TAG_A], false)]);
    expect(turns.map((t) => t.assistant.chartsNotShown)).toEqual([true, false]);
    expect(liveStore().getState().turns[0].assistant.chartsNotShown).toBe(false);
    const odd = marked(undefined, false);
    odd[0] = { ...odd[0], charts_not_shown: 'yes' as unknown as boolean };
    expect(buildRehydrateThread(odd).turns[0].assistant.chartsNotShown).toBe(false);
  });
});
