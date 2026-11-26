// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <InvestigationTrace/> — Wave-6 W6-F6 pins (Task A3, contract-first;
 * prose-markdown relaxation 2026-08-06).
 *
 * - no fetch on mount; exactly ONE fetch on first expand; re-toggle never
 *   re-fetches;
 * - empty queryRef ⇒ the honest line with ZERO fetches;
 * - rejected fetch (404/403/network) ⇒ the same honest line — never a
 *   spinner residue;
 * - loaded: question, step order (seq), reasoning paragraphs, tool
 *   name/status/duration, honest truncation lines, the steps_dropped line,
 *   final answer;
 * - PROSE (reasoning / final answer) renders as CONSTRAINED markdown via
 *   react-markdown (bold, tables, small headers) — react elements only,
 *   zero innerHTML: raw HTML in prose NEVER becomes live DOM (no
 *   rehype-raw ⇒ dropped inert, same posture as the answer <Md/> path),
 *   markdown links render as plain text spans (never anchors), markdown
 *   images render as alt text (never <img>);
 * - DATA (tool input/output) and the question stay LITERAL text — hostile
 *   strings render as literal text in the <pre> blocks, which also carry
 *   copy buttons that copy the EXACT untruncated block text. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InvestigationTrace } from '../chat/InvestigationTrace';
import { jsonResponse } from './fetch-stub';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let fetchFn: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  window.currentSessionId = 'session-test-w6';
  fetchFn = vi.fn();
  vi.stubGlobal('fetch', fetchFn);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  delete window.currentSessionId;
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const QUERY_REF = 'q-0123456789ab';

function traceDetail(over: Record<string, unknown> = {}) {
  return {
    query_ref: QUERY_REF,
    session_id: 'session-test-w6',
    system_id: 'erp',
    origin: 'system-agent',
    question: 'How many open orders reference part B-2847-X?',
    kg_context_attached: false,
    steps: [
      {
        seq: 1,
        reasoning: 'Checking the orders table first.',
        tool_name: 'execute_sql',
        tool_input: '{"sql":"SELECT part_no FROM orders"}',
        input_bytes: 37,
        tool_output: '[{"part_no":"B-2847-X"}]',
        output_bytes: 24,
        status: 'success',
        duration_ms: 42,
        started_at: '2026-08-04T18:00:00Z',
      },
      {
        seq: 2,
        reasoning: '',
        tool_name: 'execute_sql',
        tool_input: '{"sql":"SELECT count(*) FROM order_lines"}',
        input_bytes: 43,
        tool_output: '[{"count":7}]',
        output_bytes: 13,
        status: 'success',
        duration_ms: 11,
        started_at: '2026-08-04T18:00:01Z',
      },
    ],
    steps_dropped: 0,
    final_answer: 'Seven open orders reference that part.',
    final_answer_bytes: 38,
    captured_at: '2026-08-04T18:00:02Z',
    ...over,
  };
}

async function expand() {
  const details = container.querySelector<HTMLDetailsElement>(
    'details.evidence-investigation',
  )!;
  expect(details).not.toBeNull();
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event('toggle', { bubbles: true }));
  });
}

async function collapseThenExpand() {
  const details = container.querySelector<HTMLDetailsElement>(
    'details.evidence-investigation',
  )!;
  await act(async () => {
    details.open = false;
    details.dispatchEvent(new Event('toggle', { bubbles: true }));
  });
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event('toggle', { bubbles: true }));
  });
}

describe('<InvestigationTrace/> fetch discipline', () => {
  it('never fetches on mount; fetches once on first expand; re-toggle does not re-fetch', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, traceDetail()));
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    expect(fetchFn).not.toHaveBeenCalled();

    await expand();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const url = String(fetchFn.mock.calls[0][0]);
    expect(url).toContain(`/api/admin/agents/traces/investigations/${QUERY_REF}`);
    expect(url).toContain('session_id=session-test-w6');

    await collapseThenExpand();
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('empty queryRef renders the honest line with zero fetches', async () => {
    render(<InvestigationTrace queryRef="" />);
    await expand();
    expect(fetchFn).not.toHaveBeenCalled();
    expect(container.textContent).toContain(
      'Investigation trace not available for this claim',
    );
  });

  it.each([[404], [403], [500]])('HTTP %d collapses to the honest line', async (status) => {
    fetchFn.mockResolvedValue(jsonResponse(status, { detail: 'nope' }));
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();
    expect(container.textContent).toContain(
      'Investigation trace not available for this claim',
    );
    expect(container.textContent).not.toContain('Loading');
  });

  it('network failure collapses to the honest line', async () => {
    fetchFn.mockRejectedValue(new TypeError('network down'));
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();
    expect(container.textContent).toContain(
      'Investigation trace not available for this claim',
    );
  });
});

describe('<InvestigationTrace/> loaded rendering', () => {
  it('renders question, ordered steps, reasoning, tool header, final answer', async () => {
    fetchFn.mockResolvedValue(jsonResponse(200, traceDetail()));
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    expect(container.textContent).toContain(
      'Asked: How many open orders reference part B-2847-X?',
    );
    expect(container.textContent).toContain('Checking the orders table first.');
    const headers = [...container.querySelectorAll('.investigation-tool-header')].map(
      (n) => n.textContent,
    );
    expect(headers).toEqual(['execute_sql · success · 42 ms', 'execute_sql · success · 11 ms']);
    const inputs = [...container.querySelectorAll('.investigation-tool-input')].map(
      (n) => n.textContent,
    );
    expect(inputs).toEqual([
      '{"sql":"SELECT part_no FROM orders"}',
      '{"sql":"SELECT count(*) FROM order_lines"}',
    ]);
    expect(container.textContent).toContain(
      'Responded: Seven open orders reference that part.',
    );
    // no dropped-steps line when steps_dropped is 0
    expect(container.querySelector('.investigation-dropped')).toBeNull();
  });

  it('renders honest truncation lines and the steps_dropped line', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          steps_dropped: 3,
          final_answer_truncated: true,
          final_answer_bytes: 9001,
          steps: [
            {
              seq: 4,
              reasoning: 'r',
              tool_name: 't',
              tool_input: 'in',
              input_truncated: true,
              input_bytes: 5000,
              tool_output: 'out',
              output_truncated: true,
              output_bytes: 70000,
              status: 'success',
              duration_ms: null,
              started_at: '',
            },
          ],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    expect(container.textContent).toContain(
      'first 3 steps omitted to stay within storage bounds',
    );
    expect(container.textContent).toContain('input truncated — 5000 bytes total');
    expect(container.textContent).toContain('output truncated — 70000 bytes total');
    expect(container.textContent).toContain('answer truncated — 9001 bytes total');
    // null duration renders no "ms" fragment in the header
    const header = container.querySelector('.investigation-tool-header')!;
    expect(header.textContent).toBe('t · success');
  });

  it('omits non-string fields defensively instead of rendering them', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          question: 42,
          final_answer: { evil: true },
          steps: [{ seq: 1, reasoning: null, tool_name: 7, tool_input: [], tool_output: {} }],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();
    expect(container.querySelector('.investigation-question')).toBeNull();
    expect(container.querySelector('.investigation-answer')).toBeNull();
    expect(container.textContent).not.toContain('42');
    expect(container.textContent).not.toContain('evil');
  });
});

describe('<InvestigationTrace/> hostile-string containment (zero-innerHTML)', () => {
  const IMG = '<img src=x onerror=alert(1)>';
  const SCRIPT = '<script>alert(2)</script>';
  const JSLINK = '[x](javascript:alert(3))';

  it('renders hostile strings in DATA fields (question, tool input/output) as literal text', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          question: `Q ${IMG}`,
          steps: [
            {
              seq: 1,
              reasoning: '',
              tool_name: 'execute_sql',
              tool_input: `I ${SCRIPT}`,
              input_bytes: 10,
              tool_output: `O ${IMG}`,
              output_bytes: 10,
              status: 'success',
              duration_ms: 1,
              started_at: '',
            },
          ],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    // no elements were ever created from the strings
    expect(container.querySelector('img,script,a')).toBeNull();
    // the literal text IS present
    expect(container.textContent).toContain(`Q ${IMG}`);
    expect(container.querySelector('.investigation-tool-input')!.textContent).toBe(`I ${SCRIPT}`);
    expect(container.querySelector('.investigation-tool-output')!.textContent).toBe(`O ${IMG}`);
  });

  it('raw HTML in PROSE (reasoning/answer) NEVER becomes live DOM', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          final_answer: `before ${SCRIPT} after`,
          steps: [
            {
              seq: 1,
              reasoning: `start ${IMG} end`,
              tool_name: 't',
              tool_input: '',
              tool_output: '',
              status: 'success',
              duration_ms: 1,
              started_at: '',
            },
          ],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    // the hostile markup never becomes elements (react-markdown has no
    // rehype-raw: raw HTML nodes are dropped inert — the <Md/> posture)
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    // the surrounding prose still renders
    expect(container.querySelector('.investigation-reasoning')!.textContent).toContain('start');
    expect(container.querySelector('.investigation-reasoning')!.textContent).toContain('end');
    expect(container.textContent).toContain('before');
    expect(container.textContent).toContain('after');
  });

  it('markdown links and images in PROSE never become anchors or images', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          final_answer: `See ${JSLINK} and ![evil](https://x.example/p.png)`,
          steps: [],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    expect(container.querySelector('a')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    // the link text renders as plain text; the image renders as alt text
    const answer = container.querySelector('.investigation-answer-prose')!;
    expect(answer.textContent).toContain('x');
    expect(answer.textContent).toContain('evil');
  });
});

describe('<InvestigationTrace/> constrained prose markdown', () => {
  it('renders bold and newline-separated paragraphs in reasoning', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          steps: [
            {
              seq: 1,
              reasoning: 'First **important** line.\n\nSecond paragraph.',
              tool_name: 't',
              tool_input: '',
              tool_output: '',
              status: 'success',
              duration_ms: 1,
              started_at: '',
            },
          ],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    const reasoning = container.querySelector('.investigation-reasoning')!;
    const strong = reasoning.querySelector('strong')!;
    expect(strong).not.toBeNull();
    expect(strong.textContent).toBe('important');
    expect(reasoning.querySelectorAll('p')).toHaveLength(2);
  });

  it('renders a GFM table with compact borders in the final answer', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          final_answer: '| part | qty |\n| --- | --- |\n| B-2847 | 7 |',
          steps: [],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    const table = container.querySelector('.investigation-answer-prose table')!;
    expect(table).not.toBeNull();
    expect(table.textContent).toContain('B-2847');
    const td = table.querySelector('td')!;
    expect(td.style.border).toContain('var(--border)');
  });

  it('headers stay SMALL — near body size, subtle ascending steps only', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          steps: [
            {
              seq: 1,
              reasoning: '# Top\n\n## Sub\n\nbody',
              tool_name: 't',
              tool_input: '',
              tool_output: '',
              status: 'success',
              duration_ms: 1,
              started_at: '',
            },
          ],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    const h1 = container.querySelector<HTMLElement>('.investigation-reasoning h1')!;
    const h2 = container.querySelector<HTMLElement>('.investigation-reasoning h2')!;
    expect(h1.style.fontSize).toBe('1.12em');
    expect(h2.style.fontSize).toBe('1.09em');
  });

  it('tool input/output stay LITERAL — markdown inside DATA is never rendered', async () => {
    fetchFn.mockResolvedValue(
      jsonResponse(
        200,
        traceDetail({
          steps: [
            {
              seq: 1,
              reasoning: '',
              tool_name: 't',
              tool_input: '**not bold** | a | b |',
              tool_output: '# not a header',
              status: 'success',
              duration_ms: 1,
              started_at: '',
            },
          ],
        }),
      ),
    );
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    const input = container.querySelector('.investigation-tool-input')!;
    const output = container.querySelector('.investigation-tool-output')!;
    expect(input.textContent).toBe('**not bold** | a | b |');
    expect(output.textContent).toBe('# not a header');
    expect(input.querySelector('strong,table')).toBeNull();
    expect(output.querySelector('h1')).toBeNull();
  });
});

describe('<InvestigationTrace/> copy buttons on tool input/output', () => {
  afterEach(() => {
    Object.defineProperty(window.navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    });
  });

  it('each tool input/output <pre> carries a copy button copying its EXACT text', async () => {
    const writeText = vi.fn((_t: string) => Promise.resolve());
    Object.defineProperty(window.navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    fetchFn.mockResolvedValue(jsonResponse(200, traceDetail()));
    render(<InvestigationTrace queryRef={QUERY_REF} />);
    await expand();

    const inputBtn = container.querySelector<HTMLButtonElement>(
      'button.adt-copy-btn[aria-label="Copy tool input"]',
    )!;
    const outputBtn = container.querySelector<HTMLButtonElement>(
      'button.adt-copy-btn[aria-label="Copy tool output"]',
    )!;
    expect(inputBtn).not.toBeNull();
    expect(outputBtn).not.toBeNull();

    await act(async () => {
      inputBtn.click();
    });
    await act(async () => {
      outputBtn.click();
    });
    expect(writeText.mock.calls.map((c) => c[0])).toEqual([
      '{"sql":"SELECT part_no FROM orders"}',
      '[{"part_no":"B-2847-X"}]',
    ]);
  });
});
