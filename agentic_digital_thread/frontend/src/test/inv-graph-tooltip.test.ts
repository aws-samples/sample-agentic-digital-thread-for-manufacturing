// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Investigation graph's hover tooltip shows a node's detail as plain
 * text (arch-store invTooltipText), never its raw markdown.
 *
 * Live walk (2026-10-28): hovering a data node showed "Based on the query
 * results, there are **no vibration sensor readings ... / - *". The tooltip
 * is wired in initArchitecture; this drives that handler with a stubbed
 * cytoscape instance and checks what tippy is given. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Handler = (evt: unknown) => void;

const { handlers, tippyCalls } = vi.hoisted(() => ({
  handlers: [] as Array<{ event: string; selector: string; fn: Handler }>,
  tippyCalls: [] as Array<Record<string, unknown>>,
}));

vi.mock('cytoscape', () => {
  const chain: Record<string, unknown> = {};
  for (const name of ['removeClass', 'addClass', 'not', 'forEach']) chain[name] = () => chain;
  const cytoscape = Object.assign(
    vi.fn(() => ({
      on: (event: string, selectorOrFn: string | Handler, fn?: Handler) => {
        if (typeof selectorOrFn === 'function') handlers.push({ event, selector: '', fn: selectorOrFn });
        else handlers.push({ event, selector: selectorOrFn, fn: fn as Handler });
      },
      destroy: vi.fn(),
      destroyed: () => false,
      elements: () => chain,
      nodes: () => chain,
      edges: () => chain,
      container: () => null,
    })),
    { use: vi.fn() },
  );
  return { default: cytoscape };
});
vi.mock('cytoscape-cola', () => ({ default: {} }));
vi.mock('cytoscape-popper', () => ({ default: {} }));
vi.mock('tippy.js', () => ({
  default: vi.fn((_el: unknown, opts: Record<string, unknown>) => {
    tippyCalls.push(opts);
    return { show: vi.fn(), destroy: vi.fn() };
  }),
}));

import { invTooltipText } from '../lib/arch-store';
import { initArchitecture } from '../views/architecture/inv-graph';

const REPLY = [
  'Based on the query results, there are **no vibration sensor readings available for the asset** in that window.',
  '',
  'The system indicates that:',
  '- **Requested window:** 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z',
  '- *',
].join('\n');

function hover(detail: string): void {
  const h = handlers.find((x) => x.event === 'mouseover' && x.selector.includes('finding'))!;
  h.fn({
    target: {
      data: (key: string) => (key === 'detail' ? detail : undefined),
      popperRef: () => ({ getBoundingClientRect: () => ({}) }),
    },
  });
}

function unhover(): void {
  for (const h of handlers.filter((x) => x.event === 'mouseout')) h.fn({ target: {} });
}

beforeEach(() => {
  handlers.length = 0;
  tippyCalls.length = 0;
  document.body.replaceChildren();
  const host = document.createElement('div');
  host.id = 'cy-inv-graph';
  document.body.appendChild(host);
  initArchitecture();
});

afterEach(() => {
  unhover();
});

describe('Investigation graph tooltip', () => {
  it('is given the detail as plain text, not its markdown', () => {
    hover(REPLY);
    expect(tippyCalls).toHaveLength(1);
    const content = String(tippyCalls[0].content);
    expect(content).toBe(invTooltipText(REPLY));
    expect(content).not.toMatch(/\*|^\s*[-+]\s/m);
    expect(content.split('\n')).toEqual([
      'Based on the query results, there are no vibration sensor readings available for the asset in that window.',
      'The system indicates that:',
      'Requested window: 2026-10-20T00:00:00Z to 2026-10-20T01:00:00Z',
    ]);
  });

  it('keeps its line breaks visible: the tooltip theme shows them', () => {
    hover(REPLY);
    expect(String(tippyCalls[0].theme).split(' ')).toEqual(expect.arrayContaining(['cyto', 'cyto-lines']));
  });

  it('shows no tooltip for a detail of markdown structure only', () => {
    hover('## Result\n\n---');
    expect(tippyCalls).toHaveLength(0);
  });

  it('shows a table-only detail as its cells, and a code-only detail as its code', () => {
    // Review: both had no tooltip, although the detail holds the result.
    hover('| batch | line items |\n|---|---|\n| B-1 | 45 |');
    expect(tippyCalls).toHaveLength(1);
    expect(String(tippyCalls[0].content)).toBe('batch · line items\nB-1 · 45');
    unhover();
    hover('```\nSELECT 1\n```');
    expect(tippyCalls).toHaveLength(2);
    expect(String(tippyCalls[1].content)).toBe('SELECT 1');
  });
});
