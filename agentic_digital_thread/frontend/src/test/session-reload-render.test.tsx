// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Recent sessions reload renders a past session like a live answer.
 *
 * Seen on a Workshop Studio test event: reopening a session
 * from the '+ New' menu showed the answer above the question, no Reasoning
 * fold, no Sources row, and the raw ---VALUE-MAPPING-PROPOSAL--- /
 * ---CORRELATION-PROPOSAL--- / ---FOLLOWUPS--- paragraphs. The reload path
 * built plain `.message` divs from flattened event text; a live answer goes
 * through the thread store (tool pills, citations, parseAnswerBlocks).
 *
 * The fixtures below are the GET /api/memory/events payload: oldest event
 * first, each carrying the stored turn's ordered `blocks` (text, tool_use,
 * tool_result with the live trace's summary) beside the display label. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { MemoryEvent } from '../api/types/memory';
import { ChatThread } from '../chat/ChatThread';
import { buildRehydrateThread, rehydrateSession, type RehydrateDeps } from '../chat/rehydrate';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ANSWER =
  'Line 3 stopped on a feeder jam [1].\n\n' +
  '---VALUE-MAPPING-PROPOSAL---\nsource: ERP.line\ntarget: MES.line\n---END-PROPOSAL---\n\n' +
  '---CORRELATION-PROPOSAL---\nfrom: CMMS.wo\nto: ERP.order\n---END-PROPOSAL---\n\n' +
  '---FOLLOWUPS---\nQ1: What caused the jam?\nA1: Schedule feeder maintenance\n';

/** One stored investigation turn, as the events route returns it. */
function turnEvents(question = 'Why did Line 3 stop overnight?', answer = ANSWER): MemoryEvent[] {
  return [
    { role: 'user', content: question, blocks: [] },
    {
      role: 'assistant',
      content: 'Checking the graph first.\nTool call: explore_graph',
      blocks: [
        { type: 'text', text: 'Checking the graph first.' },
        { type: 'tool_use', tool_id: 't1', tool: 'explore_graph', input: { question: 'line 3' } },
      ],
    },
    {
      role: 'user',
      content: 'Tool result',
      blocks: [
        { type: 'tool_result', tool_id: 't1', tool: 'explore_graph', status: 'success', summary: 'Line 3 is fed by ERP orders and CMMS work orders' },
      ],
    },
    {
      role: 'assistant',
      content: 'Tool call: erp___query_erp',
      blocks: [
        { type: 'tool_use', tool_id: 't2', tool: 'erp___query_erp', input: { sql: 'select * from downtime' } },
      ],
    },
    {
      role: 'user',
      content: 'Tool result',
      blocks: [
        { type: 'tool_result', tool_id: 't2', tool: 'erp___query_erp', status: 'success', summary: '2 rows: downtime 01:10 to 03:40' },
      ],
    },
    { role: 'assistant', content: answer, blocks: [{ type: 'text', text: answer }] },
  ];
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

let container: HTMLDivElement;
let root: Root;
let store: ThreadStore;

beforeEach(() => {
  container = document.createElement('div');
  container.id = 'messages';
  document.body.appendChild(container);
  root = createRoot(container);
  store = createThreadStore();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function reopen(messages: MemoryEvent[]): Promise<void> {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ messages })));
  expect(await rehydrateSession('sess-1', deps(store))).toBe(true);
  act(() => root.render(<ChatThread store={store} />));
}

describe('Recent sessions reload renders like the live answer', () => {
  it('shows the question first and the answer after it', async () => {
    await reopen(turnEvents());
    const msgs = [...container.querySelectorAll<HTMLElement>('.message')];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].classList.contains('user')).toBe(true);
    expect(msgs[0].getAttribute('data-raw-content')).toBe('Why did Line 3 stop overnight?');
    expect(msgs[1].classList.contains('assistant')).toBe(true);
    expect(msgs[1].querySelector('.answer-body')?.textContent).toContain('Line 3 stopped on a feeder jam');
  });

  it('shows the Reasoning fold with the calls the turn made', async () => {
    await reopen(turnEvents());
    const fold = container.querySelector<HTMLDetailsElement>('details.tool-calls-details');
    expect(fold).not.toBeNull();
    expect(fold!.querySelector('.reasoning-fold-title')?.textContent).toBe('Reasoning');
    expect(fold!.querySelectorAll('.tool-pill.inline')).toHaveLength(2);
    expect(fold!.textContent).toContain('Checking the graph first.');
    // The trace shows each call's stored result summary.
    expect(fold!.textContent).toContain('2 rows: downtime 01:10 to 03:40');
  });

  it('shows the numbered Sources row and links the [1] badge', async () => {
    await reopen(turnEvents());
    const sources = container.querySelector('.answer-sources');
    expect(sources).not.toBeNull();
    expect(sources!.textContent).toContain('Sources');
    const chips = [...sources!.querySelectorAll<HTMLAnchorElement>('a.source-chip')];
    expect(chips.map((c) => c.dataset.citeNum)).toEqual(['1']);
    expect(container.querySelector('.answer-body a.cite-badge')).not.toBeNull();
  });

  it('turns the machine blocks into Suggested next steps, never raw text', async () => {
    await reopen(turnEvents());
    const text = container.textContent || '';
    expect(text).not.toContain('VALUE-MAPPING-PROPOSAL');
    expect(text).not.toContain('CORRELATION-PROPOSAL');
    expect(text).not.toContain('FOLLOWUPS');
    expect(text).not.toContain('END-PROPOSAL');
    const chips = container.querySelector('.followup-chips[aria-label="Suggested next steps"]');
    expect(chips).not.toBeNull();
    const labels = [...chips!.querySelectorAll<HTMLButtonElement>('.suggestion-chip')].map(
      (b) => b.dataset.question,
    );
    expect(labels).toEqual(['What caused the jam?', 'Schedule feeder maintenance']);
  });

  it('shows no call durations: the stored turn has no timing', async () => {
    await reopen(turnEvents());
    expect(container.querySelector('.tool-activity-call-time')).toBeNull();
    for (const meta of container.querySelectorAll('.tool-activity-meta')) {
      expect(meta.textContent).not.toMatch(/\d(?:\.\d)?s\b/);
    }
  });

  it('rebuilds every turn of a multi-question session in order, with distinct ids', async () => {
    await reopen([
      ...turnEvents('first question'),
      ...turnEvents('second question', 'Second answer [1].'),
    ]);
    const users = [...container.querySelectorAll<HTMLElement>('.message.user')];
    expect(users.map((u) => u.getAttribute('data-raw-content'))).toEqual([
      'first question',
      'second question',
    ]);
    const ids = [...container.querySelectorAll<HTMLElement>('.message.assistant')].map(
      (a) => a.dataset.turnId,
    );
    expect(new Set(ids).size).toBe(2);
    // Feedback is keyed to the answer it rates; a reload has no id for the
    // original answer, so it offers no thumbs (the old reload had none).
    expect(container.querySelector('.feedback-row')).toBeNull();
  });

  it('keeps answer content that precedes the first question (a cut-off window) without raw blocks', async () => {
    await reopen([
      { role: 'assistant', content: 'Tool call: erp___query_erp', blocks: [{ type: 'tool_use', tool_id: 'x', tool: 'erp___query_erp', input: {} }] },
      { role: 'assistant', content: ANSWER, blocks: [{ type: 'text', text: ANSWER }] },
      ...turnEvents('next question', 'Next answer.'),
    ]);
    const msgs = [...container.querySelectorAll<HTMLElement>('.message')];
    expect(msgs[0].classList.contains('assistant')).toBe(true);
    expect(msgs[0].textContent).toContain('Line 3 stopped on a feeder jam');
    expect(container.textContent).not.toContain('FOLLOWUPS');
    expect(container.textContent).not.toContain('PROPOSAL');
    expect(container.querySelector('.message.user')?.getAttribute('data-raw-content')).toBe('next question');
  });
});

describe('buildRehydrateThread on events from an older server (no blocks)', () => {
  it('filters tool noise and replays each question with its answer', () => {
    const { history, turns } = buildRehydrateThread([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'Tool call: query_mes' },
      { role: 'user', content: 'Tool result' },
      { role: 'assistant', content: 'a1\n\n---FOLLOWUPS---\nQ1: next?\n' },
    ]);
    expect(history).toEqual([]);
    expect(turns).toHaveLength(1);
    expect(turns[0].user.text).toBe('q1');
    expect(turns[0].assistant.answerMd).toBe('a1');
    expect(turns[0].assistant.followups).toEqual(['next?']);
    expect(turns[0].assistant.streaming).toBe(false);
  });
});

describe('plain rehydrated history', () => {
  it('never renders control blocks raw', () => {
    act(() =>
      store.setHistory([
        { role: 'assistant', content: 'An answer.\n\n---FOLLOWUPS---\nQ1: Next step?\n' },
      ]),
    );
    act(() => root.render(<ChatThread store={store} />));
    expect(container.textContent).not.toContain('FOLLOWUPS');
    expect(container.querySelector('.message.assistant')?.textContent).toContain('An answer.');
    expect(
      container.querySelector<HTMLButtonElement>('.followup-chips .suggestion-chip')?.dataset.question,
    ).toBe('Next step?');
  });
});

/* The gateway merged the route seed's facts into a delegation whose model
 * arguments had no kg_context; the events route replays the block it sent
 * as the call's `route_context`. */
describe('Recent sessions reload keeps the routing context the system was sent', () => {
  const ROUTE_LINES = [
    'target_system: line-log',
    'translated_identifiers: not given',
    'canonical_concept: batch-lot',
    'scope: not given',
    'join_rule: jp-lr-1 line-log.floor.runs.run_code -> order-book.books.lines.lot_ref',
  ];
  /** As stored and replayed: fenced. The Routing context shows it without
   * the fence lines. */
  const ROUTE = ['```ROUTE:', ...ROUTE_LINES, '```'].join('\n');
  const ROUTE_SHOWN = ROUTE_LINES.join('\n');
  const INPUT = { question: 'For runs (run_code) R-20261022-0001: which line made it?' };

  function delegationTurn(routeContext?: string): MemoryEvent[] {
    return [
      { role: 'user', content: 'Which batches failed?', blocks: [] },
      {
        role: 'assistant',
        content: 'Tool call: line-log___query_line_log',
        blocks: [{ type: 'tool_use', tool_id: 't2', tool: 'line-log___query_line_log', input: INPUT }],
      },
      {
        role: 'user',
        content: 'Tool result',
        blocks: [
          {
            type: 'tool_result',
            tool_id: 't2',
            tool: 'line-log___query_line_log',
            status: 'success',
            summary: '1 row',
            ...(routeContext === undefined ? {} : { route_context: routeContext }),
          },
        ],
      },
      { role: 'assistant', content: 'Run R-20261022-0001 passed.', blocks: [{ type: 'text', text: 'Run R-20261022-0001 passed.' }] },
    ];
  }

  it('shows the replayed Routing context, with raw args still the model input', async () => {
    await reopen(delegationTurn(ROUTE));
    const row = container.querySelector('.tool-trace[data-tool-id="t2"]')!;
    expect(row.querySelector('.trace-input-route pre')?.textContent).toBe(ROUTE_SHOWN);
    expect(row.querySelector('.trace-input-route-added')).not.toBeNull();
    expect(row.querySelector('.trace-input-raw pre')?.textContent).toBe(JSON.stringify(INPUT, null, 2));
  });

  it('shows no Routing context for a call the events route kept none for', async () => {
    await reopen(delegationTurn());
    const row = container.querySelector('.tool-trace[data-tool-id="t2"]')!;
    expect(row.querySelector('.trace-input-route')).toBeNull();
  });
});

/* The Knowledge Graph step a seeded turn streams before the model runs is not
 * one of the stored messages; the events route rebuilds it from the stored
 * seed and attaches it to the question as `prelude`. The reload replays it
 * right after the turn starts, as the live stream did. */
describe('Recent sessions reload shows the Knowledge Graph step the live trace showed', () => {
  const Q = 'Which order holds firing batch K-4471?';
  const KG_ID = 'mode-a-route-seed';
  const KG_INPUT = {
    question: `Which systems hold this data, and how do their identifiers relate, to answer: “${Q}”`,
  };
  const KG_TEXT =
    'Looked up this question in the knowledge graph before asking any system.\n' +
    '- Connected systems: Kiln Log, Order Book';
  const SYS = 'order-book___query_order_book';
  const KG_START = { tool: 'explore_graph', tool_id: KG_ID, input: KG_INPUT };
  const KG_RESULT = { tool: 'explore_graph', tool_id: KG_ID, summary: KG_TEXT };
  const SYS_START = { tool: SYS, tool_id: 't2', input: { question: 'Lot OB-4471?' } };
  const SYS_RESULT = { tool: SYS, tool_id: 't2', summary: '1 row' };

  function seededTurn(prelude?: MemoryEvent['prelude']): MemoryEvent[] {
    return [
      { role: 'user', content: Q, blocks: [], ...(prelude ? { prelude } : {}) },
      { role: 'assistant', content: `Tool call: ${SYS}`, blocks: [{ type: 'tool_use', ...SYS_START }] },
      { role: 'user', content: 'Tool result', blocks: [{ type: 'tool_result', status: 'success', ...SYS_RESULT }] },
      { role: 'assistant', content: 'Order 9 holds it.', blocks: [{ type: 'text', text: 'Order 9 holds it.' }] },
    ];
  }
  const PRELUDE: MemoryEvent['prelude'] = [
    { type: 'tool_use', ...KG_START },
    { type: 'tool_result', status: 'success', ...KG_RESULT },
  ];

  /** The same turn as the live stream drove the store, rendered beside the reload. */
  function renderLive(): { host: HTMLDivElement; close: () => void } {
    const live = createThreadStore();
    live.startTurn(Q);
    for (const [start, result] of [[KG_START, KG_RESULT], [SYS_START, SYS_RESULT]] as const) {
      live.applyEvent('tool_start', start);
      live.applyEvent('tool_result', result);
      live.applyEvent('tool_end', { tool: start.tool, tool_id: start.tool_id });
    }
    live.applyEvent('token', { text: 'Order 9 holds it.' });
    live.applyEvent('done', {});
    const host = document.createElement('div');
    document.body.appendChild(host);
    const liveRoot = createRoot(host);
    act(() => liveRoot.render(<ChatThread store={live} />));
    return {
      host,
      close: () => {
        act(() => liveRoot.unmount());
        host.remove();
      },
    };
  }

  const fold = (el: Element) => el.querySelector('details.tool-calls-details');
  const pills = (el: Element) =>
    [...(fold(el)?.querySelectorAll<HTMLElement>('.tool-pill.inline') ?? [])].map((p) => p.textContent);
  const traceText = (el: Element) => el.querySelector(`.tool-trace[data-tool-id="${KG_ID}"]`)?.textContent;

  it('replays the step after the question with the live label, summary and count', async () => {
    await reopen(seededTurn(PRELUDE));
    const live = renderLive();
    expect(pills(container)).toHaveLength(2);
    expect(pills(container)).toEqual(pills(live.host));
    expect(pills(container)[0]).toContain('KG: Which systems hold this data');
    expect(traceText(container)).toContain('Connected systems: Kiln Log, Order Book');
    expect(traceText(container)).toBe(traceText(live.host));
    for (const el of [container, live.host]) {
      expect(fold(el)?.querySelector('.reasoning-fold-meta')?.textContent).toContain('2 calls');
    }
    // Still one question, shown as typed.
    const users = [...container.querySelectorAll<HTMLElement>('.message.user')];
    expect(users.map((u) => u.getAttribute('data-raw-content'))).toEqual([Q]);
    live.close();
  });

  it('replays each turn its own step', () => {
    const { turns } = buildRehydrateThread([...seededTurn(PRELUDE), ...seededTurn(PRELUDE)]);
    expect(turns.map((t) => [...t.assistant.traces.values()].map((tr) => tr.tool))).toEqual([
      ['explore_graph', SYS],
      ['explore_graph', SYS],
    ]);
  });

  it('adds nothing for a question without one', () => {
    const { turns } = buildRehydrateThread(seededTurn());
    expect([...turns[0].assistant.traces.values()].map((tr) => tr.tool)).toEqual([SYS]);
  });
});
