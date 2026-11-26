// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* DOM-contract tests for <ChatThread/>/<AssistantTurn/> (jsx-migration W3.1):
 * render a replayed thread store and assert the structural contract the old
 * imperative runAskTurn DOM carried (AUDIT/e2e + thread-clicks selectors):
 * `.message.user` + `.bookmark-btn`, `.message.assistant`,
 * `.tool-pill.inline[data-tool-id]` (+ `.done`), `.tool-trace.visible` with
 * `.trace-result`, the done-collapse `<details class="tool-calls-details">`
 * with the activity-summary label, `a.cite-badge` +
 * sources-row `data-cite-target` links, `.followup-chips` `data-question`
 * chips, `.feedback-row`, the `.thinking-inline` live indicator, the
 * `.response-canceled` quiet marker, and the red error paragraph replacing
 * the content. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import type { ProvenanceRecord } from '../chat/provenance';
import { createThreadClickHandler } from '../chat/thread-clicks';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import { getSystemLabel } from '../chat/tool-labels';
import { chatThreadStore } from '../chat/useAskStream';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** focusArchNode (thread-clicks.ts) schedules a 300 ms setTimeout after
 * switchView('architecture'). Tests that take that legacy path must not let
 * the callback outlive the file's jsdom: with real timers it fires after
 * teardown and vitest reports "ReferenceError: window is not defined" as an
 * unhandled error, failing the frontend lane intermittently (seen on main).
 * Call from a describe block that drives citation clicks: fake timers keep
 * the callback inside the test, and afterEach flushes it while window exists. */
function useLegacyArchNavTimers(): void {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    act(() => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
  });
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
});

const render = () => act(() => root.render(<ChatThread store={store} />));

const LONG_ANSWER =
  'Line 2 is running at 92% OEE [1]. The bottleneck is the filler on shift 3; ' +
  'CMMS shows an open work order for the capper. Recommend scheduling PM.';

function replayFullTurn(s: ThreadStore = store) {
  act(() => {
    s.startTurn('what is line 2 status?', 'Line 2 status?');
    s.applyEvent('token', { text: 'Checking the line…' });
    s.applyEvent('tool_start', {
      tool: 'explore_graph',
      tool_id: 'kg1',
      input: { question: 'which systems know line 2' },
    });
    s.applyEvent('tool_result', {
      tool: 'explore_graph',
      tool_id: 'kg1',
      input: 'Entity: Line matched 4 systems',
      summary: 'Found 4 systems tracking Line entities.',
    });
    s.applyEvent('tool_end', { tool: 'explore_graph', tool_id: 'kg1' });
    s.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: { q: 'line 2' } });
    s.applyEvent(
      'tool_result',
      { tool: 'query_mes', tool_id: 't1', input: 'RESULT', summary: '3 rows' },
      'finding-7',
    );
    s.applyEvent('tool_end', { tool: 'query_mes', tool_id: 't1' });
    s.applyEvent('token', {
      text: LONG_ANSWER + '\n---FOLLOWUPS---\nQ1: What about line 3?\nA1: Schedule PM\n',
    });
    s.applyEvent('done', { status: 'complete' });
  });
}

describe('<ChatThread/> DOM contract', () => {
  it('renders the user message with bookmark + raw-content stash', () => {
    act(() => store.startTurn('raw question', 'Display text'));
    render();
    const user = container.querySelector<HTMLElement>('.message.user')!;
    expect(user).not.toBeNull();
    expect(user.textContent).toContain('Display text');
    expect(user.dataset.rawContent).toBe('Display text');
    expect(user.classList.contains('msg-enter')).toBe(true);
    expect(container.querySelector('.message.assistant.msg-enter')).not.toBeNull();
    expect(user.querySelector('.bookmark-btn')).not.toBeNull();
  });

  it('shows the thinking indicator + live stream text while streaming', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('token', { text: 'thinking about **it**' });
    });
    render();
    expect(container.querySelector('.thinking-inline')?.textContent).toContain('Working... 0:00');
    const stream = container.querySelector('.message.assistant .reasoning-container .stream-md')!;
    expect(stream.innerHTML).toContain('<strong>it</strong>');
    // Not collapsed yet.
    expect(container.querySelector('.tool-calls-details')).toBeNull();
  });

  it('shows the thinking indicator for a SECOND question in an existing thread, pre-token (follow-up regression)', () => {
    replayFullTurn();
    render();
    // First turn settled — no indicator.
    expect(container.querySelector('.thinking-inline')).toBeNull();
    // Follow-up sent: new turn pushed, no tokens yet — the indicator MUST
    // appear immediately as the LAST element ("is anything even happening").
    act(() => store.startTurn('and what about line 3?'));
    const thinking = container.querySelector('.thinking-inline');
    expect(thinking).not.toBeNull();
    expect(container.lastElementChild).toBe(thinking);
    // …and it clears when the follow-up settles.
    act(() => {
      store.applyEvent('token', { text: 'Line 3 is idle.' });
      store.applyEvent('done', { status: 'complete' });
    });
    expect(container.querySelector('.thinking-inline')).toBeNull();
  });

  it('shows the thinking indicator for the first question AFTER session rehydration (history + turns split)', () => {
    act(() =>
      store.setHistory([
        { role: 'user', content: 'old question' },
        { role: 'assistant', content: 'old answer' },
      ]),
    );
    render();
    expect(container.querySelector('.thinking-inline')).toBeNull();
    act(() => store.startTurn('new question in rehydrated session'));
    expect(container.querySelector('.thinking-inline')).not.toBeNull();
  });

  it('marks the scroll host as a polite log and announces phases without token text', () => {
    const host = { current: container as HTMLElement };
    render();
    expect(container.getAttribute('role')).toBe('log');
    expect(container.getAttribute('aria-live')).toBe('polite');
    expect(container.getAttribute('aria-relevant')).toBe('additions');
    expect(container.getAttribute('aria-busy')).toBe('false');

    act(() => store.startTurn('q2'));
    const status = container.querySelector<HTMLElement>('.chat-stream-status')!;
    expect(container.getAttribute('aria-busy')).toBe('true');
    expect(status.getAttribute('role')).toBe('status');
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.getAttribute('aria-atomic')).toBe('true');
    expect(status.textContent).toBe('Assistant responding');

    act(() => store.applyEvent('token', { text: 'token content is not announced as status' }));
    expect(status.textContent).toBe('Assistant responding');

    act(() => store.applyEvent('done', { status: 'complete' }));
    expect(container.getAttribute('aria-busy')).toBe('false');
    expect(status.textContent).toBe('Response complete');
    expect(host.current).toBe(container);
  });

  it('announces stopped and error terminal phases', () => {
    render();
    act(() => {
      store.startTurn('stop this');
      store.abortTurn();
    });
    expect(container.querySelector('.chat-stream-status')?.textContent).toBe('Response stopped');

    act(() => store.startTurn('fail this'));
    expect(container.querySelector('.chat-stream-status')?.textContent).toBe('Assistant responding');
    act(() => store.errorTurn('runtime unavailable'));
    expect(container.querySelector('.chat-stream-status')?.textContent).toBe('Response error');
  });

  it('initially follows the containerRef host to the bottom', () => {
    const host = { current: container as HTMLElement };
    Object.defineProperty(container, 'scrollHeight', { value: 1234, configurable: true });
    container.scrollTop = 0;
    act(() => root.render(<ChatThread store={store} containerRef={host} />));
    expect(container.scrollTop).toBe(1234);
  });

  it('renders pills + traces with the tool-trace/tool-pill contract', () => {
    replayFullTurn();
    render();
    const pills = [...container.querySelectorAll<HTMLElement>('.tool-pill.inline')];
    expect(pills.map((p) => p.dataset.toolId)).toEqual(['kg1', 't1']);
    expect(pills.every((p) => p.classList.contains('done'))).toBe(true);
    expect(pills[0].dataset.kgq).toBe('1');
    expect(pills[0].textContent).toBe('KG: which systems know line 2');
    const traces = [...container.querySelectorAll<HTMLElement>('.tool-trace')];
    expect(traces.map((t) => t.dataset.toolId)).toEqual(['kg1', 't1']);
    expect(traces.every((t) => t.classList.contains('visible'))).toBe(true);
    // Input block from the tool_start stash + the trace-result summary.
    expect(traces[1].querySelector('.trace-result')?.textContent).toContain('3 rows');
    expect(traces[1].innerHTML).toContain('line 2');
    expect(traces[1].innerHTML).not.toContain('RESULT');
  });

  it('collapses reasoning into details labelled with the activity summary on done', () => {
    replayFullTurn();
    render();
    const details = container.querySelector<HTMLDetailsElement>('details.tool-calls-details')!;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    const summary = details.querySelector('summary.tool-calls-summary-toggle')!;
    // What the turn did, not a bare call count: the same past-tense phrases
    // as the activity lines, plus the call count as meta.
    expect(summary.querySelector('.reasoning-fold-title')?.textContent).toBe('Reasoning');
    expect(summary.querySelector('.reasoning-fold-text')?.textContent).toBe(
      'Searched the knowledge graph · Queried MES',
    );
    expect(summary.querySelector('.reasoning-fold-meta')?.textContent).toBe('2 calls');
    expect(details.dataset.tone).toBe('ok');
    // The pre-tool reasoning run stays inside the collapsed container; the
    // trailing run became the final answer (removed from the trace).
    const runs = [...details.querySelectorAll('.stream-md')];
    expect(runs).toHaveLength(1);
    expect(runs[0].textContent).toContain('Checking the line…');
    // Thinking indicator gone after done.
    expect(container.querySelector('.thinking-inline')).toBeNull();
  });

  it('renders the final answer with cite badges, sources row, chips, feedback', () => {
    replayFullTurn();
    render();
    const msg = container.querySelector<HTMLElement>('.message.assistant')!;
    // [1] replaced with a citation badge targeting the finding node.
    const badge = msg.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.dataset.citeTarget).toBe('finding-7');
    expect(badge.textContent).toBe('1');
    // Sources row: numbered link with the same data-cite-target contract.
    const source = [...msg.querySelectorAll<HTMLAnchorElement>('a[data-cite-target]')].filter(
      (a) => !a.classList.contains('cite-badge'),
    );
    expect(source).toHaveLength(1);
    expect(source[0].textContent).toContain('[1]');
    // Follow-up chips with data-question (delegated click contract).
    const chips = [...msg.querySelectorAll<HTMLElement>('.followup-chips .suggestion-chip')];
    expect(chips.map((c) => c.dataset.question)).toEqual(['What about line 3?', 'Schedule PM']);
    expect(chips[1].classList.contains('action-chip')).toBe(true);
    // Feedback thumbs row + message datasets for the feedback payload.
    expect(msg.querySelectorAll('.feedback-row .feedback-btn[data-sentiment]')).toHaveLength(2);
    expect(msg.dataset.messageId).toMatch(/^msg-/);
    expect(msg.dataset.userMessage).toBe('what is line 2 status?');
  });

  it('pill click toggles the matching trace visibility (store-backed)', () => {
    replayFullTurn();
    render();
    const pill = container.querySelector<HTMLElement>('.tool-pill[data-tool-id="t1"]')!;
    act(() => pill.click());
    expect(
      container.querySelector<HTMLElement>('.tool-trace[data-tool-id="t1"]')!.classList.contains('visible'),
    ).toBe(false);
    act(() => pill.click());
    expect(
      container.querySelector<HTMLElement>('.tool-trace[data-tool-id="t1"]')!.classList.contains('visible'),
    ).toBe(true);
  });

  it('renders the quiet canceled marker under the partial content on abort', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('token', { text: 'partial answer' });
      store.abortTurn();
    });
    render();
    const marker = container.querySelector('.response-canceled')!;
    expect(marker.textContent).toBe('⏹ Response canceled by user');
    // Partial stream text stays visible above the marker; no red banner.
    expect(container.querySelector('.stream-md')?.textContent).toContain('partial answer');
    expect(container.querySelector('.thinking-inline')).toBeNull();
  });

  it('renders the red error paragraph BELOW the partial reasoning, with Ask again', () => {
    act(() => {
      store.startTurn('q raw', 'q shown');
      store.applyEvent('token', { text: 'partial' });
      store.applyEvent('tool_start', { tool: 'mes___query_mes', tool_id: 't1', input: {} });
      // The transport stores the PLAIN message; the renderer builds the
      // red paragraph as JSX (zero-innerHTML).
      store.errorTurn('agent runtime unavailable', 'turn-ref-1');
    });
    render();
    const msg = container.querySelector<HTMLElement>('.message.assistant')!;
    const p = msg.querySelector<HTMLParagraphElement>('.turn-error p')!;
    expect(p.textContent).toBe(
      'Error: agent runtime unavailableReference for support: turn-ref-1',
    );
    expect(p.classList.contains('turn-error-text')).toBe(true); // red via answer.css
    // The reasoning that ran stays visible, and the error sits after it.
    const stream = msg.querySelector('.reasoning-container .stream-md')!;
    expect(stream.textContent).toContain('partial');
    expect(msg.querySelector('.reasoning-container .tool-pill[data-tool-id="t1"]')).not.toBeNull();
    const reasoning = msg.querySelector('.reasoning-container')!;
    expect(reasoning.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Ask again re-sends the POSTed question (user.raw) through the
    // delegated suggestion-chip path.
    const again = msg.querySelector<HTMLButtonElement>('.turn-error button.ask-again-btn')!;
    expect(again.textContent).toBe('Ask again');
    expect(again.type).toBe('button');
    expect(again.classList.contains('suggestion-chip')).toBe(true);
    expect(again.dataset.question).toBe('q raw');
  });

  it('omits the support reference line when the error carries no id', () => {
    act(() => {
      store.startTurn('q');
      store.errorTurn('boom');
    });
    render();
    const p = container.querySelector<HTMLParagraphElement>('.turn-error p')!;
    expect(p.textContent).toBe('Error: boom');
  });

  it('renders rehydrated history as plain message divs (no bookmark/feedback)', () => {
    act(() =>
      store.setHistory([
        { role: 'user', content: 'earlier question' },
        { role: 'assistant', content: 'earlier **answer**' },
      ]),
    );
    render();
    const msgs = [...container.querySelectorAll<HTMLElement>('.message')];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].className).toBe('message user msg-enter');
    expect(msgs[0].textContent).toBe('earlier question');
    expect(msgs[1].className).toBe('message assistant msg-enter');
    expect(msgs[1].innerHTML).toContain('<strong>answer</strong>');
    expect(container.querySelector('.bookmark-btn')).toBeNull();
    expect(container.querySelector('.feedback-row')).toBeNull();
  });

  it("clear() empties the rendered thread ('+ New')", () => {
    replayFullTurn();
    render();
    expect(container.querySelector('.message')).not.toBeNull();
    act(() => store.clear());
    expect(container.querySelector('.message')).toBeNull();
  });
});

/* T2.2 — metadata.provenance transport (storage only; frozen contract D1).
 * Additive cases: rendering is Task 2.4 — these assert the store/turn-model
 * wiring and the legacy proof (absent key ⇒ zero behavior change). */
describe('metadata.provenance transport (T2.2)', () => {
  const record = (over: Partial<ProvenanceRecord> = {}): ProvenanceRecord => ({
    citation_num: 1,
    tool_id: 't1',
    source_system: 'mes',
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'partial',
    ...over,
  });

  const lastTurn = () => {
    const { turns } = store.getState();
    return turns[turns.length - 1];
  };

  it('populates the turn from a metadata event carrying provenance', () => {
    replayFullTurn();
    const records = [record(), record({ citation_num: 2, tool_id: 't2', source_system: 'erp' })];
    act(() => store.applyEvent('metadata', { provenance: { records, truncated: false } }));
    expect(lastTurn().assistant.provenance).toEqual(records);
  });

  it('leaves provenance EMPTY (and every rendered contract intact) when the stream never carries the key — legacy-client proof', () => {
    replayFullTurn(); // includes a metadata-free stream: no provenance frame
    render();
    const a = lastTurn().assistant;
    expect(a.provenance).toEqual([]);
    expect(a.openEvidenceNum).toBeNull();
    // The rendered Sources-row/cite-badge contract is byte-identical to the
    // pre-provenance assertions above (same selectors, same values).
    const msg = container.querySelector<HTMLElement>('.message.assistant')!;
    const badge = msg.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge.dataset.citeTarget).toBe('finding-7');
    const source = [...msg.querySelectorAll<HTMLAnchorElement>('a[data-cite-target]')].filter(
      (el) => !el.classList.contains('cite-badge'),
    );
    expect(source).toHaveLength(1);
    expect(source[0].textContent).toContain('[1]');
  });

  it('ignores a metadata frame with a provenance key but no records (allowlist posture)', () => {
    replayFullTurn();
    act(() => store.applyEvent('metadata', { provenance: {} }));
    expect(lastTurn().assistant.provenance).toEqual([]);
  });

  it('populates graphQueries from the additive top-level graph_queries key — even with EMPTY records (Mode A: only the uncited explore_graph ran)', () => {
    replayFullTurn();
    const rows = [{ query_ref: 'q-abc123', tool_name: 'explore_graph' }];
    act(() =>
      store.applyEvent('metadata', {
        provenance: { records: [], truncated: false, graph_queries: rows },
      }),
    );
    const a = lastTurn().assistant;
    expect(a.graphQueries).toEqual(rows);
    expect(a.provenance).toEqual([]); // records stay legacy-empty
  });

  it('leaves graphQueries EMPTY when the payload never carries the key — legacy proof', () => {
    replayFullTurn();
    act(() =>
      store.applyEvent('metadata', { provenance: { records: [record()], truncated: false } }),
    );
    const a = lastTurn().assistant;
    expect(a.graphQueries).toEqual([]);
    expect(a.provenance).toHaveLength(1);
  });

  it('coexists with intelligence_summary/agent_analysis in the SAME frame', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('metadata', {
        intelligence_summary: { total_tool_calls: 3 },
        agent_analysis: { summary: 'fine' },
        provenance: { records: [record()] },
      });
    });
    const a = lastTurn().assistant;
    expect(a.scratch.lastIntelligenceSummary).toEqual({ total_tool_calls: 3 });
    expect(a.scratch.lastAgentAnalysis).toEqual({ summary: 'fine' });
    expect(a.provenance).toHaveLength(1);
    expect(a.provenance[0].tool_id).toBe('t1');
  });

  it('a later provenance frame REPLACES the records (last-write-wins, not accrete)', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('metadata', { provenance: { records: [record()] } });
      store.applyEvent('metadata', {
        provenance: { records: [record({ citation_num: 2, tool_id: 't9' })] },
      });
    });
    const p = lastTurn().assistant.provenance;
    expect(p).toHaveLength(1);
    expect(p[0].tool_id).toBe('t9');
  });

  it('provenance survives done() and scopes per turn', () => {
    act(() => {
      store.startTurn('q1');
      store.applyEvent('metadata', { provenance: { records: [record()] } });
      store.applyEvent('done', { status: 'complete' });
      store.startTurn('q2');
      store.applyEvent('done', { status: 'complete' });
    });
    const { turns } = store.getState();
    expect(turns[0].assistant.provenance).toHaveLength(1);
    expect(turns[1].assistant.provenance).toEqual([]);
  });

  it('openEvidence sets/clears openEvidenceNum on the ACTIVE turn and bumps rev', () => {
    replayFullTurn();
    const before = lastTurn().rev;
    act(() => store.openEvidence(1));
    expect(lastTurn().assistant.openEvidenceNum).toBe(1);
    expect(lastTurn().rev).toBe(before + 1);
    act(() => store.openEvidence(null));
    expect(lastTurn().assistant.openEvidenceNum).toBeNull();
  });

  it('openEvidence is a no-op on the same value and with no active turn', () => {
    // No turns at all — must not throw.
    act(() => store.openEvidence(3));
    expect(store.getState().turns).toHaveLength(0);
    replayFullTurn();
    act(() => store.openEvidence(2));
    const rev = lastTurn().rev;
    act(() => store.openEvidence(2)); // same value — no rev bump, no notify churn
    expect(lastTurn().rev).toBe(rev);
    expect(lastTurn().assistant.openEvidenceNum).toBe(2);
  });
});

/* T2.4 — Tier-1 chips + Tier-2 evidence-card wiring. Chip enrichment and
 * verification state apply ONLY when a provenance record binds (a record-less
 * chip renders today's markup verbatim — legacy proof); the delegated
 * citation click opens the card when a record binds on the ACTIVE turn and
 * runs the legacy focusArchNode path VERBATIM when none does. The click tests
 * use the SINGLETON chatThreadStore — thread-clicks dispatches openEvidence
 * there (the same store the app renders). */
describe('Tier-1 chips + evidence-card wiring (T2.4)', () => {
  const record = (over: Partial<ProvenanceRecord> = {}): ProvenanceRecord => ({
    citation_num: 1,
    tool_id: 't1',
    source_system: 'mes',
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'partial',
    ...over,
  });

  const makeHandler = () => createThreadClickHandler({ askQuestion: vi.fn(), savePrompt: vi.fn() });

  /** The first NON-badge citation anchor (the Sources-row chip). */
  const sourcesChip = () =>
    [...container.querySelectorAll<HTMLAnchorElement>('a[data-cite-target]')].filter(
      (el) => !el.classList.contains('cite-badge'),
    )[0];

  useLegacyArchNavTimers();

  afterEach(() => {
    act(() => chatThreadStore.clear());
    delete window.switchView;
  });

  it("renders today's chip verbatim when no record binds (legacy fallback)", () => {
    replayFullTurn();
    render();
    const chip = sourcesChip();
    expect(chip.textContent).toBe(`[1] ${getSystemLabel('query_mes')}`);
    expect(chip.dataset.verification).toBeUndefined();
    expect(chip.getAttribute('title')).toBeNull();
    expect(container.querySelector('.evidence-card')).toBeNull();
  });

  it('enriches the chip label + verification state when a record binds — tool_id PRIMARY (D2)', () => {
    replayFullTurn();
    // citation_num deliberately WRONG (99): the tool_id match must bind.
    act(() =>
      store.applyEvent('metadata', {
        provenance: { records: [record({ citation_num: 99, entity: { table: 'work_orders' } })] },
      }),
    );
    render();
    const chip = sourcesChip();
    // source_system is a system id; the chip names it through the shared
    // resolver (no catalog here, so the id-derived fallback).
    expect(chip.textContent).toBe('[1] MES · work_orders');
    expect(chip.dataset.verification).toBe('partial');
  });

  it('a sparse bound record keeps the fallback label but shows the verification state', () => {
    replayFullTurn();
    act(() => store.applyEvent('metadata', { provenance: { records: [record()] } }));
    render();
    const chip = sourcesChip();
    expect(chip.textContent).toBe(`[1] ${getSystemLabel('query_mes')}`);
    expect(chip.dataset.verification).toBe('partial');
  });

  it('citation-badge click WITH a bound record opens the evidence card (no arch nav)', () => {
    replayFullTurn(chatThreadStore);
    act(() =>
      chatThreadStore.applyEvent('metadata', {
        provenance: { records: [record({ entity: { table: 'work_orders' } })] },
      }),
    );
    act(() => root.render(<ChatThread store={chatThreadStore} />));
    const switchView = vi.fn();
    window.switchView = switchView;
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    const preventDefault = vi.fn();
    act(() => makeHandler()({ target: badge, preventDefault }));
    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(switchView).not.toHaveBeenCalled(); // NOT the legacy arch path
    const card = container.querySelector<HTMLElement>('.evidence-card')!;
    expect(card).not.toBeNull();
    expect(card.getAttribute('data-cite-num')).toBe('1');
    // Close button clears openEvidenceNum and unmounts the card.
    act(() =>
      card.querySelector<HTMLButtonElement>('button[aria-label="Close evidence card"]')!.click(),
    );
    expect(container.querySelector('.evidence-card')).toBeNull();
    expect(chatThreadStore.getState().turns.at(-1)?.assistant.openEvidenceNum).toBeNull();
  });

  it('citation click WITHOUT a record falls back to the legacy focusArchNode path VERBATIM', () => {
    replayFullTurn(chatThreadStore); // no provenance frame — legacy stream
    act(() => root.render(<ChatThread store={chatThreadStore} />));
    const switchView = vi.fn();
    window.switchView = switchView;
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    act(() => makeHandler()({ target: badge, preventDefault: vi.fn() }));
    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(chatThreadStore.getState().turns.at(-1)?.assistant.openEvidenceNum).toBeNull();
    expect(container.querySelector('.evidence-card')).toBeNull();
  });

  it('sources-row chip click opens the card; "show in architecture" runs the legacy path', () => {
    replayFullTurn(chatThreadStore);
    act(() => chatThreadStore.applyEvent('metadata', { provenance: { records: [record()] } }));
    act(() => root.render(<ChatThread store={chatThreadStore} />));
    const switchView = vi.fn();
    window.switchView = switchView;
    act(() => makeHandler()({ target: sourcesChip(), preventDefault: vi.fn() }));
    expect(container.querySelector('.evidence-card')).not.toBeNull();
    expect(switchView).not.toHaveBeenCalled();
    const link = container.querySelector<HTMLAnchorElement>('.evidence-arch-link')!;
    expect(link.textContent).toBe('Show in architecture');
    act(() => link.click());
    expect(switchView).toHaveBeenCalledWith('architecture');
  });

  it('openEvidence on a citation without a record renders the honest null-record card', () => {
    replayFullTurn();
    act(() => store.openEvidence(1));
    render();
    const card = container.querySelector<HTMLElement>('.evidence-card')!;
    expect(card).not.toBeNull();
    // Fallback label verbatim + the explicit no-evidence banner (D4).
    expect(card.textContent).toContain(getSystemLabel('query_mes'));
    expect(card.querySelector('.evidence-unverified-banner')?.textContent).toBe(
      'No captured evidence for this claim',
    );
  });
});

/* Citation IDENTITY resolution. `data-cite-target`
 * (`system-<tool>`) is NON-UNIQUE within a turn (two same-system citations)
 * and across turns (any repeat of the same system). The delegated click
 * handler must therefore resolve by (turn container, `data-cite-num`
 * ordinal) — never find-first-target — and only the ACTIVE turn may open
 * the Tier-2 card (earlier turns run the legacy focusArchNode path
 * verbatim). Both historical failure modes are pinned below. */
describe('citation identity — non-unique data-cite-target', () => {
  const record = (over: Partial<ProvenanceRecord> = {}): ProvenanceRecord => ({
    citation_num: 1,
    tool_id: 't1',
    source_system: 'mes',
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'partial',
    ...over,
  });

  const makeHandler = () => createThreadClickHandler({ askQuestion: vi.fn(), savePrompt: vi.fn() });

  useLegacyArchNavTimers();

  afterEach(() => {
    act(() => chatThreadStore.clear());
    delete window.switchView;
  });

  /** One settled turn with a single query_mes citation `[1]` (target
   * `system-query_mes` — no findingNodeId, so the target is the shared
   * system nodeId). */
  const replayMesTurn = (s: ThreadStore, toolId: string) =>
    act(() => {
      s.startTurn('line status?');
      s.applyEvent('tool_start', { tool: 'query_mes', tool_id: toolId, input: { q: 'line' } });
      s.applyEvent('tool_result', { tool: 'query_mes', tool_id: toolId, input: 'R', summary: '3 rows' });
      s.applyEvent('tool_end', { tool: 'query_mes', tool_id: toolId });
      s.applyEvent('token', { text: 'MES reports 90% OEE [1].' });
      s.applyEvent('done', { status: 'complete' });
    });

  it('two same-system citations in ONE turn: clicking the SECOND opens the SECOND record', () => {
    act(() => {
      chatThreadStore.startTurn('compare shift 1 and 2');
      chatThreadStore.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't1', input: { q: 's1' } });
      chatThreadStore.applyEvent('tool_result', { tool: 'query_mes', tool_id: 't1', input: 'R1', summary: '3 rows' });
      chatThreadStore.applyEvent('tool_end', { tool: 'query_mes', tool_id: 't1' });
      chatThreadStore.applyEvent('tool_start', { tool: 'query_mes', tool_id: 't2', input: { q: 's2' } });
      chatThreadStore.applyEvent('tool_result', { tool: 'query_mes', tool_id: 't2', input: 'R2', summary: '5 rows' });
      chatThreadStore.applyEvent('tool_end', { tool: 'query_mes', tool_id: 't2' });
      chatThreadStore.applyEvent('token', { text: 'Shift 1 ran 90% [1]. Shift 2 ran 80% [2].' });
      chatThreadStore.applyEvent('done', { status: 'complete' });
      chatThreadStore.applyEvent('metadata', {
        provenance: {
          records: [
            record({ citation_num: 1, tool_id: 't1', entity: { table: 'shift_one' } }),
            record({ citation_num: 2, tool_id: 't2', entity: { table: 'shift_two' } }),
          ],
        },
      });
    });
    act(() => root.render(<ChatThread store={chatThreadStore} />));
    const switchView = vi.fn();
    window.switchView = switchView;
    const badges = [...container.querySelectorAll<HTMLAnchorElement>('a.cite-badge')];
    expect(badges).toHaveLength(2);
    // Both badges carry the SAME non-unique target — identity must come
    // from the data-cite-num ordinal, never find-first-target.
    expect(badges.map((b) => b.dataset.citeTarget)).toEqual([
      'system-query_mes',
      'system-query_mes',
    ]);
    expect(badges.map((b) => b.dataset.citeNum)).toEqual(['1', '2']);
    act(() => makeHandler()({ target: badges[1], preventDefault: vi.fn() }));
    expect(switchView).not.toHaveBeenCalled();
    const card = container.querySelector<HTMLElement>('.evidence-card')!;
    expect(card).not.toBeNull();
    expect(card.getAttribute('data-cite-num')).toBe('2'); // the SECOND's record
    expect(card.textContent).toContain('shift_two');
    expect(card.textContent).not.toContain('shift_one');
    expect(chatThreadStore.getState().turns.at(-1)?.assistant.openEvidenceNum).toBe(2);
  });

  it('an EARLIER-turn badge whose target matches an active-turn citation runs the legacy path (no card)', () => {
    replayMesTurn(chatThreadStore, 't1'); // turn 1 — same system, same target
    replayMesTurn(chatThreadStore, 't2'); // turn 2 (ACTIVE) — record bound below
    act(() =>
      chatThreadStore.applyEvent('metadata', {
        provenance: { records: [record({ tool_id: 't2', entity: { table: 'oee_daily' } })] },
      }),
    );
    act(() => root.render(<ChatThread store={chatThreadStore} />));
    const switchView = vi.fn();
    window.switchView = switchView;
    const messages = [...container.querySelectorAll<HTMLElement>('.message.assistant')];
    expect(messages).toHaveLength(2);
    const earlierBadge = messages[0].querySelector<HTMLAnchorElement>('a.cite-badge')!;
    const activeBadge = messages[1].querySelector<HTMLAnchorElement>('a.cite-badge')!;
    // The earlier badge matches the active turn's citation on BOTH target
    // and ordinal — only the turn container disambiguates.
    expect(earlierBadge.dataset.citeTarget).toBe(activeBadge.dataset.citeTarget);
    expect(earlierBadge.dataset.citeNum).toBe(activeBadge.dataset.citeNum);
    act(() => makeHandler()({ target: earlierBadge, preventDefault: vi.fn() }));
    // Legacy focusArchNode path VERBATIM — never the active turn's card.
    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(container.querySelector('.evidence-card')).toBeNull();
    expect(chatThreadStore.getState().turns.at(-1)?.assistant.openEvidenceNum).toBeNull();
    // Sanity: the ACTIVE turn's own badge still opens its card.
    act(() => makeHandler()({ target: activeBadge, preventDefault: vi.fn() }));
    expect(container.querySelector('.evidence-card')).not.toBeNull();
  });
});
