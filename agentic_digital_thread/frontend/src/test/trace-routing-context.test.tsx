// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A system call's routing context in the chat trace: the orchestrator sends
 * the fenced ROUTE block (with its `join_rule:` line) as the call's
 * `kg_context` argument. The call row shows it in its own collapsed
 * "Routing context" expander with the real line breaks; "raw args" keeps
 * the unchanged JSON and the `input:` summary is untouched
 * (chat/ToolInputBlock.tsx, chat/tool-labels.ts#routingContextOf). The
 * derived display drops the opening and closing fence lines the block is
 * wrapped in at its source, and is bounded: CRLF shown as LF, capped at
 * ROUTING_CONTEXT_MAX_CHARS with a localised truncation note, and the
 * expanded block has a themed max-block-size and scrolls. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ChatThread } from '../chat/ChatThread';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';
import { ROUTING_CONTEXT_MAX_CHARS, routingContextAdded, routingContextOf } from '../chat/tool-labels';
import { ToolInputBlock } from '../chat/ToolInputBlock';
import { setLang } from '../lib/i18n';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ROUTE_LINES = [
  'target_system: erp',
  'translated_identifiers: LOT-0042',
  'canonical_concept: production lot',
  'scope: site A, last 7 days',
  'join_rule: J1 mes.work_orders.lot_id -> erp.batches.lot_number',
];

/** The value as the source sends it: fenced, with a `ROUTE:` info string. */
const ROUTE = ['```ROUTE:', ...ROUTE_LINES, '```'].join('\n');

/** What the Routing context shows: the same lines without the fence lines. */
const ROUTE_SHOWN = ROUTE_LINES.join('\n');

const QUESTION = 'Which batches used production lot LOT-0042?';

let container: HTMLDivElement;
let root: Root;
let ls: Map<string, string>;

beforeEach(() => {
  ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  setLang('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  setLang('en');
  vi.unstubAllGlobals();
});

const renderBlock = (input: unknown) =>
  act(() => root.render(<ToolInputBlock input={input} tool="erp___query_erp" />));

describe('routingContextOf', () => {
  it('returns a non-empty string kg_context (key matched case-insensitively), else null', () => {
    expect(routingContextOf({ question: QUESTION, kg_context: ROUTE })).toEqual({
      text: ROUTE_SHOWN,
      truncated: false,
    });
    expect(routingContextOf({ question: QUESTION, KG_Context: `\n${ROUTE}\n  ` })).toEqual({
      text: ROUTE_SHOWN,
      truncated: false,
    });
    expect(routingContextOf({ question: QUESTION })).toBeNull();
    expect(routingContextOf({ question: QUESTION, kg_context: '   ' })).toBeNull();
    expect(routingContextOf({ question: QUESTION, kg_context: { target_system: 'erp' } })).toBeNull();
    expect(routingContextOf('plain string input')).toBeNull();
    expect(routingContextOf(null)).toBeNull();
  });

  it(`caps the display copy at ${ROUTING_CONTEXT_MAX_CHARS} characters, never splitting a surrogate pair`, () => {
    expect(ROUTING_CONTEXT_MAX_CHARS).toBe(4000);
    const exact = 'a'.repeat(ROUTING_CONTEXT_MAX_CHARS);
    expect(routingContextOf({ kg_context: exact })).toEqual({ text: exact, truncated: false });
    expect(routingContextOf({ kg_context: exact + 'b' })).toEqual({ text: exact, truncated: true });
    // An emoji straddling the cut is dropped whole, not halved.
    const straddle = 'a'.repeat(ROUTING_CONTEXT_MAX_CHARS - 1) + '\u{1F600}' + 'b';
    expect(routingContextOf({ kg_context: straddle })).toEqual({
      text: 'a'.repeat(ROUTING_CONTEXT_MAX_CHARS - 1),
      truncated: true,
    });
    // CRLF collapses to LF before the cap is measured: 1,500 CRLF lines are
    // 4,501 source characters (over the cap) but display as 3,001, uncapped.
    const crlf = 'x\r\n'.repeat(1500).trim() + '\r\ny';
    expect(routingContextOf({ kg_context: crlf })).toEqual({
      text: crlf.replace(/\r\n/g, '\n'),
      truncated: false,
    });
  });
});

describe('<ToolInputBlock/> routing context — bounded, normalised display', () => {
  it('shows a multi-megabyte kg_context as a bounded block with a truncation marker; raw args unchanged', () => {
    const huge = `${ROUTE}\r\n${'x'.repeat(3 * 1024 * 1024)}`;
    const input = { question: QUESTION, kg_context: huge };
    renderBlock(input);
    const route = container.querySelector<HTMLDetailsElement>('details.trace-input-route')!;
    expect(route.dataset.truncated).toBe('true');
    const shown = route.querySelector('pre')!.textContent!;
    expect(shown.length).toBe(ROUTING_CONTEXT_MAX_CHARS + 1);
    // The opening fence line is dropped; the closing one is not the last
    // line of this value, so it stays as written.
    expect(shown.startsWith(`${ROUTE_SHOWN}\n\`\`\`\nxxx`)).toBe(true);
    expect(shown.endsWith('…')).toBe(true);
    expect(route.querySelector('.trace-input-route-truncated')?.textContent).toBe(
      'Truncated: showing the first 4000 characters. The full value is in raw args.',
    );
    // raw args still carries the whole value, byte for byte.
    const raw = container.querySelector('details.trace-input-raw pre')!.textContent;
    expect(raw).toBe(JSON.stringify(input, null, 2));
    expect(raw!.length).toBeGreaterThan(3 * 1024 * 1024);
  });

  it('localises the truncation marker', () => {
    renderBlock({ question: QUESTION, kg_context: 'z'.repeat(ROUTING_CONTEXT_MAX_CHARS + 10) });
    act(() => setLang('ja'));
    expect(container.querySelector('.trace-input-route-truncated')?.textContent).toBe(
      '省略表示：先頭の 4000 文字のみ表示しています。全文は raw args にあります。',
    );
  });

  it('renders CRLF and lone CR as real line breaks in the display copy only', () => {
    const crlf = ROUTE.replace(/\n/g, '\r\n');
    const input = { question: QUESTION, kg_context: `${crlf}\r` };
    renderBlock(input);
    const route = container.querySelector<HTMLDetailsElement>('details.trace-input-route')!;
    const shown = route.querySelector('pre')!.textContent!;
    expect(shown).toBe(ROUTE_SHOWN);
    expect(shown).not.toContain('\r');
    expect(route.dataset.truncated).toBeUndefined();
    expect(route.querySelector('.trace-input-route-truncated')).toBeNull();
    // raw args keeps the original CRLF (as JSON escapes).
    const raw = container.querySelector('details.trace-input-raw pre')!.textContent!;
    expect(raw).toBe(JSON.stringify(input, null, 2));
    expect(raw).toContain('\\r\\n');
    // A lone CR (old Mac line ending) is a line break too.
    renderBlock({ question: QUESTION, kg_context: 'target_system: erp\rjoin_rule: none' });
    expect(container.querySelector('.trace-input-route pre')?.textContent).toBe(
      'target_system: erp\njoin_rule: none',
    );
  });

  it('shows no block for whitespace-only or non-string kg_context, and raw args still renders', () => {
    const values: unknown[] = ['', '   ', '\r\n\t \r\n', '\u00a0\u2003', 42, true, null, ['ROUTE:'], { target_system: 'erp' }];
    for (const kg_context of values) {
      const input = { question: QUESTION, kg_context };
      expect(routingContextOf(input)).toBeNull();
      renderBlock(input);
      expect(container.querySelector('.trace-input-route')).toBeNull();
      expect(container.querySelector('details.trace-input-raw pre')?.textContent).toBe(
        JSON.stringify(input, null, 2),
      );
    }
  });

  it('bounds the expanded block with a themed max-block-size and scrolls (CSS contract)', () => {
    const dir = path.dirname(fileURLToPath(import.meta.url));
    const css = readFileSync(path.resolve(dir, '../chat/tool-activity.css'), 'utf-8');
    const base = readFileSync(path.resolve(dir, '../styles/base.css'), 'utf-8');
    const rule = css.match(/\.trace-input-route pre\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(rule).toMatch(/max-block-size:\s*var\(--trace-block-max\b/);
    expect(rule).toMatch(/overflow:\s*auto/);
    expect(base).toMatch(/:root\s*\{[^}]*--trace-block-max:\s*[\d.]+rem/);
  });
});

describe('<ToolInputBlock/> routing context', () => {
  it('renders kg_context as a separate, collapsed "Routing context" block with real line breaks', () => {
    renderBlock({ question: QUESTION, kg_context: ROUTE });
    const route = container.querySelector<HTMLDetailsElement>('details.trace-input-route');
    expect(route).not.toBeNull();
    expect(route!.open).toBe(false);
    expect(route!.querySelector('summary')?.textContent).toBe('Routing context');
    const pre = route!.querySelector('pre')!;
    // The ROUTE block's own line breaks, not JSON `\n` escapes, and no
    // literal fence lines.
    expect(pre.textContent).toBe(ROUTE_SHOWN);
    expect(pre.textContent).toContain('\njoin_rule: J1 mes.work_orders.lot_id -> erp.batches.lot_number');
    expect(pre.textContent).not.toContain('\\n');
    expect(pre.textContent).not.toContain('```');
    // A separate expander from raw args, placed before it.
    const expanders = [...container.querySelectorAll('details')].map((d) => d.className);
    expect(expanders).toEqual(['trace-input-route', 'trace-input-raw']);
  });

  it('renders no routing block for a call without kg_context (and with one beside it, does)', () => {
    renderBlock({ question: QUESTION });
    expect(container.querySelector('.trace-input-route')).toBeNull();
    renderBlock({ question: QUESTION, kg_context: '  \n ' });
    expect(container.querySelector('.trace-input-route')).toBeNull();
    // Positive control in the same test: the case-insensitive key is found.
    renderBlock({ question: QUESTION, KG_CONTEXT: ROUTE });
    expect(container.querySelector('.trace-input-route pre')?.textContent).toBe(ROUTE_SHOWN);
  });

  it('keeps raw args and the input summary exactly as before', () => {
    const input = { question: QUESTION, kg_context: ROUTE };
    renderBlock(input);
    const raw = container.querySelector<HTMLDetailsElement>('details.trace-input-raw')!;
    expect(raw.querySelector('summary')?.textContent).toBe('raw args');
    expect(raw.querySelector('pre')?.textContent).toBe(JSON.stringify(input, null, 2));
    expect(container.querySelector('.trace-input')?.textContent).toBe(`input: ${QUESTION}`);
    // The routing block is the only addition.
    expect(container.querySelectorAll('.trace-input-route')).toHaveLength(1);
  });

  it('follows the language picker', () => {
    renderBlock({ question: QUESTION, kg_context: ROUTE });
    act(() => setLang('de'));
    expect(container.querySelector('.trace-input-route summary')?.textContent).toBe('Routing-Kontext');
  });
});

describe('routing context in the chat activity line', () => {
  let store: ThreadStore;

  beforeEach(() => {
    store = createThreadStore();
  });

  it("shows the call's ROUTE block inside its trace row, only for the call that carries one", () => {
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', {
        tool: 'erp___query_erp',
        tool_id: 'a',
        input: { question: QUESTION, kg_context: ROUTE },
      });
      store.applyEvent('tool_result', { tool: 'erp___query_erp', tool_id: 'a', summary: '2 rows' });
      store.applyEvent('tool_start', { tool: 'cmms___query_cmms', tool_id: 'b', input: { question: 'open orders' } });
      store.applyEvent('tool_result', { tool: 'cmms___query_cmms', tool_id: 'b', summary: '1 row' });
      store.applyEvent('done', { status: 'complete' });
    });
    act(() => root.render(<ChatThread store={store} />));
    const withRoute = container.querySelector('.tool-activity .tool-trace[data-tool-id="a"]')!;
    expect(withRoute.querySelector('.trace-input-route pre')?.textContent).toBe(ROUTE_SHOWN);
    expect(withRoute.querySelector('.trace-input-raw pre')?.textContent).toBe(
      JSON.stringify({ question: QUESTION, kg_context: ROUTE }, null, 2),
    );
    const without = container.querySelector('.tool-activity .tool-trace[data-tool-id="b"]')!;
    expect(without.querySelector('.trace-input-raw')).not.toBeNull();
    expect(without.querySelector('.trace-input-route')).toBeNull();
  });
});

/* The ROUTE block the gateway merged in from the route seed is streamed on the
 * call's tool_result as `route_context`: it is what the system was sent. The
 * Routing context shows it, says that some lines came from the knowledge
 * graph, and raw args keeps the model's own arguments. */
describe('routing context the system was sent (tool_result route_context)', () => {
  const SENT_LINES = [
    'target_system: line-log',
    'translated_identifiers: not given',
    'canonical_concept: lot-run',
    'scope: not given',
    'join_rule: jp-lr-1 line-log.runs.run_code -> order-book.consignment_lines.lot_ref',
  ];
  const SENT = ['```ROUTE:', ...SENT_LINES, '```'].join('\n');
  const SENT_SHOWN = SENT_LINES.join('\n');
  const ADDED = 'Some lines come from the knowledge graph, not the model. Raw args shows what the model wrote.';
  let store: ThreadStore;

  beforeEach(() => {
    store = createThreadStore();
  });

  const runCall = (input: unknown, routeContext?: string) =>
    act(() => {
      store.startTurn('q');
      store.applyEvent('tool_start', { tool: 'line-log___query_line_log', tool_id: 'c', input });
      store.applyEvent('tool_result', {
        tool: 'line-log___query_line_log',
        tool_id: 'c',
        summary: '1 row',
        ...(routeContext === undefined ? {} : { route_context: routeContext }),
      });
      store.applyEvent('done', { status: 'complete' });
    });

  it('shows the sent context for a call whose model arguments had none; raw args stays the model input', () => {
    const input = { question: QUESTION };
    runCall(input, SENT);
    act(() => root.render(<ChatThread store={store} />));
    const row = container.querySelector('.tool-activity .tool-trace[data-tool-id="c"]')!;
    expect(row.querySelector('.trace-input-route pre')?.textContent).toBe(SENT_SHOWN);
    expect(row.querySelector('.trace-input-route-added')?.textContent).toBe(ADDED);
    expect(row.querySelector('.trace-input-raw pre')?.textContent).toBe(JSON.stringify(input, null, 2));
  });

  it('prefers the sent context over the model kg_context, and notes only a real difference', () => {
    renderBlockWith({ question: QUESTION, kg_context: ROUTE }, SENT);
    expect(container.querySelector('.trace-input-route pre')?.textContent).toBe(SENT_SHOWN);
    expect(container.querySelector('.trace-input-route-added')).not.toBeNull();
    renderBlockWith({ question: QUESTION, kg_context: `\n${SENT}\n` }, SENT);
    expect(container.querySelector('.trace-input-route pre')?.textContent).toBe(SENT_SHOWN);
    expect(container.querySelector('.trace-input-route-added')).toBeNull();
  });

  it('falls back to the model kg_context without a sent one, as before', () => {
    runCall({ question: QUESTION, kg_context: ROUTE });
    act(() => root.render(<ChatThread store={store} />));
    const row = container.querySelector('.tool-activity .tool-trace[data-tool-id="c"]')!;
    expect(row.querySelector('.trace-input-route pre')?.textContent).toBe(ROUTE_SHOWN);
    expect(row.querySelector('.trace-input-route-added')).toBeNull();
    renderBlockWith({ question: QUESTION }, '   ');
    expect(container.querySelector('.trace-input-route')).toBeNull();
  });

  it('localises the note', () => {
    renderBlockWith({ question: QUESTION }, SENT);
    act(() => setLang('de'));
    expect(container.querySelector('.trace-input-route-added')?.textContent).toBe(
      'Einige Zeilen stammen aus dem Wissensgraphen, nicht vom Modell. Unter raw args steht, was das Modell geschrieben hat.',
    );
  });

  const renderBlockWith = (input: unknown, routeContext: string) =>
    act(() =>
      root.render(<ToolInputBlock input={input} tool="line-log___query_line_log" routeContext={routeContext} />),
    );
});

/* The routing block is fenced at its source (an opening fence line with an
 * info string, and a closing fence line). The display copy drops those two
 * lines only; raw args keeps the value as sent. */
describe('routing context fence lines', () => {
  const LINES = ['target_system: order-book', 'join_rule: none'];
  const BODY = LINES.join('\n');
  const shownFor = (kg_context: unknown) => routingContextOf({ question: QUESTION, kg_context })?.text;

  it('drops a leading fence line with or without an info string, and the trailing fence line', () => {
    expect(shownFor(['```ROUTE:', ...LINES, '```'].join('\n'))).toBe(BODY);
    expect(shownFor(['```', ...LINES, '```'].join('\n'))).toBe(BODY);
    expect(shownFor(['```text extra words', ...LINES, '```'].join('\n'))).toBe(BODY);
    // A longer fence closes with at least as many backticks.
    expect(shownFor(['````ROUTE:', ...LINES, '`````'].join('\n'))).toBe(BODY);
    // CRLF line endings and outer whitespace around the fences.
    expect(shownFor(`  \r\n\`\`\`ROUTE:\r\n${LINES.join('\r\n')}\r\n  \`\`\`  \r\n`)).toBe(BODY);
    // Blank lines just inside the fences are outer whitespace of the shown copy.
    expect(shownFor(['```ROUTE:', '', ...LINES, '', '```'].join('\n'))).toBe(BODY);
  });

  it('leaves unfenced text and backticks inside the text as written', () => {
    expect(shownFor(BODY)).toBe(BODY);
    const inline = 'target_system: order-book\njoin_rule: `a.b` -> ```c.d```';
    expect(shownFor(inline)).toBe(inline);
    expect(shownFor('```one line with ``` inside```')).toBe('```one line with ``` inside```');
    // Only the outer pair goes: a fenced snippet inside the block stays.
    const inner = ['```ROUTE:', 'scope: x', '```sql', 'SELECT 1', '```', 'join_rule: none', '```'].join('\n');
    expect(shownFor(inner)).toBe(['scope: x', '```sql', 'SELECT 1', '```', 'join_rule: none'].join('\n'));
  });

  it('drops an opening fence whose closer is missing, and keeps a closer with no opener', () => {
    expect(shownFor(['```ROUTE:', ...LINES].join('\n'))).toBe(BODY);
    expect(shownFor([...LINES, '```'].join('\n'))).toBe([...LINES, '```'].join('\n'));
    // A closer shorter than the opener does not close it.
    expect(shownFor(['````', ...LINES, '```'].join('\n'))).toBe([...LINES, '```'].join('\n'));
    // Two backticks are not a fence.
    expect(shownFor(['``x', ...LINES, '``'].join('\n'))).toBe(['``x', ...LINES, '``'].join('\n'));
  });

  it('shows no block for a value that is only fence lines, falling back to the model kg_context', () => {
    expect(shownFor('```ROUTE:\n```')).toBeUndefined();
    expect(shownFor('```')).toBeUndefined();
    const own = ['```ROUTE:', ...LINES, '```'].join('\n');
    expect(routingContextOf({ kg_context: own }, '```ROUTE:\n\n```')?.text).toBe(BODY);
  });

  it('drops a tilde fence the same way, closed only by tildes', () => {
    expect(shownFor(['~~~ROUTE:', ...LINES, '~~~'].join('\n'))).toBe(BODY);
    expect(shownFor(['~~~~', ...LINES, '~~~~~'].join('\n'))).toBe(BODY);
    // A backtick line does not close a tilde fence, nor a tilde line a backtick one.
    expect(shownFor(['~~~', ...LINES, '```'].join('\n'))).toBe([...LINES, '```'].join('\n'));
    expect(shownFor(['```', ...LINES, '~~~'].join('\n'))).toBe([...LINES, '~~~'].join('\n'));
    // A tilde fence's info string may hold backticks.
    expect(shownFor(['~~~ `x`', ...LINES, '~~~'].join('\n'))).toBe(BODY);
  });

  it('claims no knowledge-graph lines when the sent value shows nothing', () => {
    const own = ['```ROUTE:', ...LINES, '```'].join('\n');
    for (const sent of ['```ROUTE:\n\n```', '```ROUTE:', '~~~']) {
      expect(routingContextOf({ kg_context: own }, sent)?.text).toBe(BODY);
      expect(routingContextAdded({ kg_context: own }, sent)).toBe(false);
    }
    // A sent value that shows lines still counts when it differs.
    const merged = ['```ROUTE:', ...LINES, 'canonical_concept: lot', '```'].join('\n');
    expect(routingContextAdded({ kg_context: own }, merged)).toBe(true);
  });

  it('caps the shown copy after the opening fence line is dropped', () => {
    const long = '```ROUTE:\n' + 'y'.repeat(ROUTING_CONTEXT_MAX_CHARS) + '\n```';
    expect(routingContextOf({ kg_context: long })).toEqual({
      text: 'y'.repeat(ROUTING_CONTEXT_MAX_CHARS),
      truncated: false,
    });
    expect(routingContextOf({ kg_context: long.replace('\n```', 'z\n```') })).toEqual({
      text: 'y'.repeat(ROUTING_CONTEXT_MAX_CHARS),
      truncated: true,
    });
  });

  it('renders the fenced value without its fence lines; raw args keeps them', () => {
    const value = ['```ROUTE:', ...LINES, '```'].join('\n');
    const input = { question: QUESTION, kg_context: value };
    renderBlock(input);
    expect(container.querySelector('.trace-input-route pre')?.textContent).toBe(BODY);
    const raw = container.querySelector('details.trace-input-raw pre')!.textContent!;
    expect(raw).toBe(JSON.stringify(input, null, 2));
    expect(raw).toContain('```ROUTE:\\n');
    expect(raw).toContain('\\n```"');
    // The expander keeps its accessible name.
    expect(container.querySelector('.trace-input-route summary')?.textContent).toBe('Routing context');
  });
});
