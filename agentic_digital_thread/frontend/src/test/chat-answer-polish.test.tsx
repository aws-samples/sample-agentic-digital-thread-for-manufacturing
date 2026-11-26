// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The finished-answer surface (2026-10 UX polish): the reasoning fold reads
 * as what the turn did, the answer slides into place instead of jumping,
 * everything under an answer is ONE footer band, follow-up chips are real
 * buttons with one main next step, thumbs announce their state and the
 * feedback record carries the tools that ran, and the chat strings follow
 * the language picker. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { requestJson } from '../api/client';
import { MAX_TOOLS_USED, toolsUsed } from '../chat/AnswerFooter';
import { ChatThread } from '../chat/ChatThread';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import { setLang } from '../lib/i18n';
import { FollowupChips } from '../shared/thread/answer-blocks';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});
const requestJsonMock = vi.mocked(requestJson);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let store: ThreadStore;
let ls: Map<string, string>;

beforeEach(() => {
  ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  setLang('en');
  requestJsonMock.mockReset();
  requestJsonMock.mockResolvedValue({ status: 'success' });
  container = document.createElement('div');
  container.id = 'messages';
  document.body.appendChild(container);
  root = createRoot(container);
  store = createThreadStore();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  setLang('en');
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const render = () => act(() => root.render(<ChatThread store={store} />));
const msg = () => container.querySelector<HTMLElement>('.message.assistant')!;

const ANSWER =
  'Line 2 is down [1].\n---FOLLOWUPS---\nQ1: What about line 3?\nQ2: Who owns the PM?\nA1: Schedule PM\n';

/** Two calls (one fails), narration between them, then the answer. */
function replayTurn({ failSecond = false, finish = true } = {}) {
  act(() => {
    store.startTurn('what is line 2 status?');
    store.applyEvent('token', { text: 'Checking the line…' });
    store.applyEvent('tool_start', { tool: 'explore_graph', tool_id: 'kg1', input: { question: 'q' } });
    store.applyEvent('tool_result', {
      tool: 'explore_graph',
      tool_id: 'kg1',
      input: 'x',
      summary: 'Found 4 systems tracking Line entities.',
    });
    store.applyEvent('tool_end', { tool: 'explore_graph', tool_id: 'kg1' });
    store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: { sql: 'SELECT 1' } });
    store.applyEvent(
      'tool_result',
      {
        tool: 'erp___query_erp',
        tool_id: 't1',
        input: 'x',
        summary: failSecond ? 'Error: connection refused' : '3 rows',
      },
      'finding-7',
    );
    store.applyEvent('tool_end', { tool: 'erp___query_erp', tool_id: 't1' });
    store.applyEvent('token', { text: ANSWER });
    if (finish) store.applyEvent('done', { status: 'complete' });
  });
}

describe('reasoning fold', () => {
  it('labels the fold with the turn activity and flags a failed call', () => {
    replayTurn({ failSecond: true });
    render();
    const details = msg().querySelector<HTMLDetailsElement>('details.tool-calls-details')!;
    expect(details.querySelector('.reasoning-fold-text')?.textContent).toBe(
      'Searched the knowledge graph · Queried ERP',
    );
    expect(details.querySelector('.reasoning-fold-meta')?.textContent).toBe('2 calls · 1 failed');
    expect(details.dataset.tone).toBe('error');
    expect(details.querySelector('summary')?.getAttribute('title')).toBe(
      'Reasoning · Searched the knowledge graph · Queried ERP · 2 calls · 1 failed',
    );
  });

  it.each([
    ['3 rows', '1 call', 'ok'],
    ['Error: connection refused', '1 call · 1 failed', 'error'],
  ])('a one-call turn still counts its call (%s)', (summary, meta, tone) => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('token', { text: 'Checking.' });
      store.applyEvent('tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: { sql: 'SELECT 1' } });
      store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 't1', input: 'x', summary });
      store.applyEvent('tool_end', { tool: 'erp___query_erp', tool_id: 't1' });
      store.applyEvent('token', { text: 'Done.' });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    const details = msg().querySelector<HTMLElement>('details.tool-calls-details')!;
    expect(details.querySelector('.reasoning-fold-text')?.textContent).toBe('Queried ERP');
    expect(details.querySelector('.reasoning-fold-meta')?.textContent).toBe(meta);
    expect(details.dataset.tone).toBe(tone);
    // The activity line itself keeps its compact one-call meta (no count).
    expect(details.querySelector('.tool-activity-meta')?.textContent ?? '').not.toContain('call');
  });

  it('renders no empty fold for an answer with nothing above it', () => {
    act(() => {
      store.startTurn('hi');
      store.applyEvent('token', { text: 'Hello there.' });
      store.applyEvent('done', { status: 'complete' });
    });
    render();
    expect(msg().querySelector('.tool-calls-details')).toBeNull();
    expect(msg().querySelector('.answer-body')?.textContent).toContain('Hello there.');
  });
});

describe('answer landing (FLIP)', () => {
  /** Streamed text sits at `liveTop`; the answer lands at `answerTop`. */
  function stubGeometry(liveTop: number, answerTop: number) {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      const top = this.classList.contains('answer-body') ? answerTop : liveTop;
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top } as DOMRect;
    });
    const animate = vi.fn();
    (Element.prototype as unknown as { animate: unknown }).animate = animate;
    return animate;
  }
  const reducedMotion = (matches: boolean) =>
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: matches && q.includes('reduce') }));

  afterEach(() => {
    delete (Element.prototype as unknown as { animate?: unknown }).animate;
  });

  it('slides the answer from where the streamed text was', () => {
    const animate = stubGeometry(600, 200);
    reducedMotion(false);
    replayTurn({ finish: false });
    render();
    act(() => store.applyEvent('done', { status: 'complete' }));
    expect(animate).toHaveBeenCalledTimes(1);
    const [frames, opts] = animate.mock.calls[0];
    expect(frames[0].transform).toBe('translateY(400px)');
    expect(frames[1].transform).toBe('translateY(0)');
    expect(opts.duration).toBe(320);
    // The slide runs once; later renders of the finished turn do not repeat it.
    act(() => store.openEvidence(1));
    expect(animate).toHaveBeenCalledTimes(1);
  });

  it('measures both ends AFTER the follow scroll (scroll height grows on done)', () => {
    // Content-space positions; viewport top = content y - scrollTop, the way
    // a real scroll container behaves. The follow-to-bottom scroll moves
    // scrollTop on every commit; the slide must use post-scroll rectangles.
    let scrollHeight = 1000;
    Object.defineProperty(container, 'scrollHeight', { configurable: true, get: () => scrollHeight });
    Object.defineProperty(container, 'clientHeight', { configurable: true, value: 200 });
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const contentY = this.classList.contains('answer-body') ? 500 : 900;
      const top = contentY - container.scrollTop;
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top } as DOMRect;
    });
    const animate = vi.fn();
    (Element.prototype as unknown as { animate: unknown }).animate = animate;
    reducedMotion(false);
    replayTurn({ finish: false });
    render();
    expect(container.scrollTop).toBe(1000);
    scrollHeight = 1400; // the footer and analysis panel land with the answer
    act(() => store.applyEvent('done', { status: 'complete' }));
    expect(container.scrollTop).toBe(1400);
    // live run at 900-1000 = -100 (measured after the streaming commit's
    // scroll); answer at 500-1400 = -900 (after done's scroll) → +800.
    expect(animate).toHaveBeenCalledTimes(1);
    expect(animate.mock.calls[0][0][0].transform).toBe('translateY(800px)');
  });

  it('does not animate under reduced motion', () => {
    const animate = stubGeometry(600, 200);
    reducedMotion(true);
    replayTurn({ finish: false });
    render();
    act(() => store.applyEvent('done', { status: 'complete' }));
    expect(animate).not.toHaveBeenCalled();
  });

  it('does not animate a turn that was already finished when it mounted', () => {
    const animate = stubGeometry(600, 200);
    reducedMotion(false);
    replayTurn();
    render();
    expect(animate).not.toHaveBeenCalled();
  });

  it('skips a jump too small to read as motion', () => {
    const animate = stubGeometry(202, 200);
    reducedMotion(false);
    replayTurn({ finish: false });
    render();
    act(() => store.applyEvent('done', { status: 'complete' }));
    expect(animate).not.toHaveBeenCalled();
  });
});

describe('answer footer', () => {
  it('stays hidden while the answer streams, even once citations exist', () => {
    replayTurn({ finish: false });
    render();
    expect(store.getState().turns[0].assistant.citations.length).toBeGreaterThan(0);
    expect(msg().querySelector('.answer-footer')).toBeNull();
    expect(msg().querySelector('a[data-cite-num]:not(.cite-badge)')).toBeNull();
  });

  it('puts chips, sources and thumbs in ONE footer band, sources and thumbs on one row', () => {
    replayTurn();
    render();
    const footers = msg().querySelectorAll('.answer-footer');
    expect(footers).toHaveLength(1);
    const footer = footers[0];
    expect(footer.querySelector(':scope > .followup-chips')).not.toBeNull();
    const meta = footer.querySelector(':scope > .answer-footer-meta')!;
    expect(meta.querySelector('.answer-sources')).not.toBeNull();
    expect(meta.querySelector('.feedback-row')).not.toBeNull();
    // Sources chips keep the e2e contract and carry no inline styles.
    const chip = meta.querySelector<HTMLAnchorElement>('a.source-chip[data-cite-target][data-cite-num]')!;
    expect(chip.classList.contains('cite-badge')).toBe(false);
    expect(chip.getAttribute('style')).toBeNull();
    expect(meta.querySelector('.answer-sources-label')?.textContent).toBe('Sources');
  });

  it('renders follow-ups as real buttons with ONE primary next step', () => {
    replayTurn();
    render();
    const chips = [...msg().querySelectorAll<HTMLButtonElement>('.followup-chips .suggestion-chip')];
    expect(chips.map((c) => c.tagName)).toEqual(['BUTTON', 'BUTTON', 'BUTTON']);
    expect(chips.every((c) => c.type === 'button')).toBe(true);
    expect(chips.map((c) => c.dataset.question)).toEqual([
      'What about line 3?',
      'Who owns the PM?',
      'Schedule PM',
    ]);
    const primary = chips.filter((c) => c.classList.contains('suggestion-chip-primary'));
    expect(primary).toEqual([chips[0]]);
    // An action reads as a recommendation to assistive tech; visible text
    // and data-question stay the suggestion itself.
    expect(chips[2].classList.contains('action-chip')).toBe(true);
    expect(chips[2].getAttribute('aria-label')).toBe('Recommended action: Schedule PM');
    expect(chips[2].textContent).toBe('Schedule PM');
    const group = msg().querySelector('.followup-chips')!;
    expect(group.getAttribute('role')).toBe('group');
    expect(group.getAttribute('aria-label')).toBe('Suggested next steps');
  });

  it('makes the first action primary when there are no follow-up questions', () => {
    act(() => root.render(<FollowupChips actions={['Schedule PM', 'Order bearing']} />));
    const chips = [...container.querySelectorAll<HTMLButtonElement>('.suggestion-chip')];
    expect(chips[0].classList.contains('suggestion-chip-primary')).toBe(true);
    expect(chips[1].classList.contains('suggestion-chip-primary')).toBe(false);
  });

  it('calls onAsk from a chip button (the data-modal path)', () => {
    const onAsk = vi.fn();
    act(() => root.render(<FollowupChips followups={['Why?']} onAsk={onAsk} />));
    act(() => container.querySelector<HTMLButtonElement>('.suggestion-chip')!.click());
    expect(onAsk).toHaveBeenCalledWith('Why?');
  });
});

describe('feedback thumbs', () => {
  it('names each thumb, marks the chosen one pressed, and sends the tools that ran', async () => {
    replayTurn();
    render();
    const [up, down] = [...msg().querySelectorAll<HTMLButtonElement>('.feedback-row .feedback-btn')];
    expect(up.getAttribute('aria-label')).toBe('Good response');
    expect(down.getAttribute('aria-label')).toBe('Bad response');
    expect(up.getAttribute('aria-pressed')).toBe('false');
    await act(async () => up.click());
    expect(up.getAttribute('aria-pressed')).toBe('true');
    expect(down.getAttribute('aria-pressed')).toBe('false');
    expect(msg().querySelector('.feedback-row')?.getAttribute('data-selected')).toBe('positive');
    const body = JSON.parse(String(requestJsonMock.mock.calls[0][1]?.body));
    expect(body.tools_used).toEqual(['explore_graph', 'erp___query_erp']);
  });

  it('dedupes and caps tools_used', () => {
    act(() => {
      store.startTurn('q');
      for (let i = 0; i < MAX_TOOLS_USED + 10; i++) {
        store.applyEvent('tool_start', { tool: `t${i % (MAX_TOOLS_USED + 5)}`, tool_id: `id${i}`, input: {} });
      }
      store.applyEvent('done', { status: 'complete' });
    });
    const turn = store.getState().turns[0];
    const used = toolsUsed(turn);
    expect(used).toHaveLength(MAX_TOOLS_USED);
    expect(new Set(used).size).toBe(used.length);
  });
});

describe('chat strings follow the language picker', () => {
  it('renders the fold, activity, sources, chips and canceled marker in Japanese', () => {
    replayTurn();
    render();
    act(() => setLang('ja'));
    const m = msg();
    expect(m.querySelector('.reasoning-fold-title')?.textContent).toBe('推論');
    expect(m.querySelector('.reasoning-fold-text')?.textContent).toBe(
      'ナレッジグラフを検索しました · ERP を照会しました',
    );
    expect(m.querySelector('.reasoning-fold-meta')?.textContent).toBe('2 件の呼び出し');
    expect(m.querySelector('.answer-sources-label')?.textContent).toBe('出典');
    expect(m.querySelector('.followup-chips')?.getAttribute('aria-label')).toBe('次のステップの候補');
    expect(m.querySelector('.feedback-btn')?.getAttribute('aria-label')).toBe('良い回答');
  });

  it('translates the canceled marker and the error prefix', () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('token', { text: 'partial' });
      store.abortTurn();
      store.startTurn('q2');
      store.errorTurn('boom');
    });
    render();
    act(() => setLang('de'));
    expect(container.querySelector('.response-canceled')?.textContent).toBe(
      '⏹ Antwort vom Benutzer abgebrochen',
    );
    expect(container.querySelector('.turn-error-text')?.textContent).toBe('Fehler: boom');
  });
});

describe('source hygiene', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const read = (rel: string) => readFileSync(path.resolve(here, rel), 'utf-8');
  it.each([
    '../chat/AnswerFooter.tsx',
    '../chat/AssistantTurn.tsx',
    '../chat/Feedback.tsx',
    '../chat/EvidenceCard.tsx',
  ])('%s carries no inline style objects (styles live in stylesheets)', (rel) => {
    expect(read(rel)).not.toMatch(/style=\{\{/);
  });

  it('the analysis panel keeps inline style ONLY for data-driven segment colour/width', () => {
    const lines = read('../chat/analysis-panel.tsx')
      .split('\n')
      .filter((l) => l.includes('style={'))
      .map((l) => l.trim());
    expect(lines).toEqual([
      'style={{ width: `${seg.pct}%`, background: seg.color }}',
      '<span className="analysis-legend-swatch" style={{ background: seg.color }} />',
    ]);
  });

  it('every verification badge has a stylesheet class', () => {
    const css = read('../chat/evidence-card.css');
    for (const badge of ['verified', 'partial', 'unverified']) {
      expect(css).toContain(`.evidence-badge-${badge} {`);
    }
  });

  it('one focus ring: a zero-specificity 2px accent baseline and no 1px focus outlines', () => {
    const styles = path.resolve(here, '../styles');
    const base = read('../styles/base.css');
    expect(base).toMatch(
      /:where\(button, a\[href\], summary, \[role="button"\], \[tabindex\]:not\(\[tabindex="-1"\]\)\):focus-visible \{\s*outline: 2px solid var\(--accent\);\s*outline-offset: 2px;/,
    );
    const all = ['base.css', 'dashboard.css', 'enhancements.css']
      .map((f) => readFileSync(path.join(styles, f), 'utf-8'))
      .concat(['../chat/answer.css', '../chat/evidence-card.css', '../chat/analysis-panel.css', '../chat/tool-activity.css'].map(read))
      .join('\n');
    expect(all).not.toMatch(/:focus-visible[^{]*\{[^}]*outline:\s*1px/);
  });
});
