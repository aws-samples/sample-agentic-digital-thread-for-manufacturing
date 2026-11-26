// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Grouped citations render as chips (review 2026-10-29).
 *
 * The client linked only a single `[N]` or `[E<k>]`. An answer that grouped
 * its citations, `[1, 2]` or `[E1, E2]`, showed them as plain text, and a
 * follow-up citing only earlier results that way had no Sources row (the
 * footer counted only a single `[E<k>]`). A group whose members are all
 * citations of the answer now renders one chip per member, in order; a group
 * with any other member stays as written. Driven through the real thread
 * store and <ChatThread/> (buildAnswerModel, then AnswerFooter).
 *
 * Grammar parity with the server: test/citation-marker-parity.test.ts. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import { citedEarlierCitations } from '../chat/provenance';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import type { Citation } from '../chat/turn-model';
import { setLang } from '../lib/i18n';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let store: ThreadStore;

beforeEach(() => {
  const ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  setLang('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  store = createThreadStore();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const render = () => act(() => root.render(<ChatThread store={store} />));
const chips = () =>
  [...container.querySelectorAll<HTMLAnchorElement>('a.source-chip')].map((c) => c.textContent);
const badges = () =>
  [...container.querySelectorAll<HTMLAnchorElement>('.answer-body a.cite-badge')].map(
    (b) => b.textContent,
  );
const body = () => container.querySelector('.answer-body')!.textContent ?? '';

function call(tool: string, id: string) {
  store.applyEvent('tool_start', { tool, tool_id: id, input: { question: 'q' } });
  store.applyEvent('tool_result', {
    tool,
    tool_id: id,
    input: 'x',
    summary: '3 records found\n| a |\n|---|\n| 1 |',
    elapsed_ms: 1_000,
  });
  store.applyEvent('tool_end', { tool, tool_id: id });
}

/** One turn: `tools` are this turn's cited calls (after a graph call that is
 * never numbered), `earlier` the runtime's earlier_sources. */
function turn(text: string, tools: string[], earlier: unknown[] = []) {
  act(() => {
    store.startTurn('did they move together?');
    call('explore_graph', 'g1');
    tools.forEach((tool, i) => call(tool, `t${i + 1}`));
    store.applyEvent('token', { text });
    store.applyEvent('metadata', {
      intelligence_summary: { total_tool_calls: tools.length + 1, earlier_sources: earlier },
    });
    store.applyEvent('done', { status: 'complete' });
  });
  render();
}

const EARLIER = [
  { label: 'E1', tool: 'erp___query_erp' },
  { label: 'E2', tool: 'mes___query_mes' },
];

describe('a grouped citation of this turn', () => {
  it('renders one chip per member, in order, and a Sources row', () => {
    turn('Both rose [2, 1], then [1,2].', ['erp___query_erp', 'mes___query_mes']);
    expect(badges()).toEqual(['2', '1', '1', '2']);
    expect(body()).not.toContain('[2, 1]');
    expect(body()).not.toContain('[1,2]');
    expect(chips()).toEqual(['[1] ERP', '[2] MES']);
  });

  it('stays as written when a member is not a citation of the answer', () => {
    turn('Both rose [1, 3]; the range [4, 7].', ['erp___query_erp', 'mes___query_mes']);
    expect(badges()).toEqual([]);
    expect(body()).toContain('[1, 3]');
    expect(body()).toContain('[4, 7]');
  });
});

describe('a no-call follow-up citing earlier results', () => {
  it('a grouped [E1, E2] renders two "earlier in this chat" chips and badges', () => {
    turn('Both moved together [E1, E2].', [], EARLIER);
    expect(chips()).toEqual([
      '[E1] ERP · earlier in this chat',
      '[E2] MES · earlier in this chat',
    ]);
    expect(badges()).toEqual(['E1', 'E2']);
    expect(body()).not.toContain('[E1, E2]');
  });

  it('a single [E2] renders its "earlier in this chat" chip', () => {
    turn('The second moved [E2].', [], EARLIER);
    expect(chips()).toEqual(['[E2] MES · earlier in this chat']);
    expect(badges()).toEqual(['E2']);
  });

  it('a group with a label the turn does not have stays as written and shows no chip', () => {
    turn('Both moved together [E1, E3].', [], EARLIER);
    expect(chips()).toEqual([]);
    expect(badges()).toEqual([]);
    expect(body()).toContain('[E1, E3]');
  });

  it('a mixed group links this turn and the earlier result', () => {
    turn('Now and before [1, E1].', ['mes___query_mes'], EARLIER);
    expect(badges()).toEqual(['1', 'E1']);
    expect(chips()).toEqual(['[1] MES', '[E1] ERP · earlier in this chat']);
  });
});

describe('earlier labels the inline links never link give no earlier source', () => {
  // The footer used to scan the raw answer, so a grouped label inside code or
  // a chart tag showed an "earlier in this chat" chip and a Sources row that
  // no inline badge backed (review 2026-10-29). It now walks the same
  // stretches as the inline links.
  const sourcesRow = () => container.querySelector('.answer-sources');

  it('in inline code', () => {
    turn('The matrix is `m[E1, E2]`, written `[E1, E2]`.', [], EARLIER);
    expect(badges()).toEqual([]);
    expect(chips()).toEqual([]);
    expect(sourcesRow()).toBeNull();
    expect(body()).toContain('[E1, E2]');
  });

  it('in a fenced block', () => {
    turn('The matrix:\n\n```\nm[E1, E2]\n[E1, E2]\n```\n\nDone.', [], EARLIER);
    expect(badges()).toEqual([]);
    expect(chips()).toEqual([]);
    expect(sourcesRow()).toBeNull();
  });

  it('in a chart tag', () => {
    turn(
      'Trend:\n<chart-block type="line" title="[E1, E2]" labels=\'["a"]\' datasets=\'[{"data":[1]}]\'/>\nDone.',
      [],
      EARLIER,
    );
    expect(badges()).toEqual([]);
    expect(chips()).toEqual([]);
    expect(sourcesRow()).toBeNull();
  });

  it('right after a word', () => {
    turn('Read x[E1] and y[E1, E2] first.', [], EARLIER);
    expect(badges()).toEqual([]);
    expect(chips()).toEqual([]);
    expect(body()).toContain('x[E1]');
  });

  it('while a label outside code in the same answer still gets its chip', () => {
    turn('Code `[E1, E2]`, cited [E2].', [], EARLIER);
    expect(badges()).toEqual(['E2']);
    expect(chips()).toEqual(['[E2] MES · earlier in this chat']);
  });
});

describe('brackets that are not citations stay as written', () => {
  it('an index or a footnote line keeps its number, a citation beside it links', () => {
    turn('Read sensor[1] and a[1, 2], then [1].\n\n[1]: the method.', [
      'erp___query_erp',
      'mes___query_mes',
    ]);
    expect(badges()).toEqual(['1']);
    expect(body()).toContain('sensor[1]');
    expect(body()).toContain('a[1, 2]');
    expect(body()).toContain('[1]: the method.');
  });
});

describe('citedEarlierCitations (pure)', () => {
  const current: Citation = {
    tool: 'mes___query_mes',
    label: 'MES',
    nodeId: 'system-mes___query_mes',
    num: 1,
    findingNodeId: null,
  };
  const e1: Citation = { ...current, tool: 'erp___query_erp', label: 'ERP', earlier: true };
  const e2: Citation = { ...e1, num: 2 };

  it('counts the members of a group whose every member is a citation', () => {
    expect(citedEarlierCitations([current, e2, e1], 'X [E2, E1].')).toEqual([e1, e2]);
    expect(citedEarlierCitations([current, e1], 'X [1, E1].')).toEqual([e1]);
  });

  it('counts nothing from a group with another member', () => {
    expect(citedEarlierCitations([current, e1, e2], 'X [E1, E3] [3, E2].')).toEqual([]);
  });

  it('counts nothing inside code, a fence, a chart tag or right after a word', () => {
    const md = 'X `[E1, E2]`\n```\n[E2]\n```\n<chart-block title="[E1]"/> y[E2, E1].';
    expect(citedEarlierCitations([current, e1, e2], md)).toEqual([]);
    expect(citedEarlierCitations([current, e1, e2], `${md} Then [E2].`)).toEqual([e2]);
  });
});
