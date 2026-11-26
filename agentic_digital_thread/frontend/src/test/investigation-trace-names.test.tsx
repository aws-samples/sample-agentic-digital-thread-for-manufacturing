// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <InvestigationTrace/>: graph names in the final answer show as text.
 *
 * The trace route returns the stored answer as written plus
 * `final_answer_names`, the graph names of its ids
 * (agents_trace_routes._with_answer_names). The card applies them after the
 * markdown is parsed (chat/trace-id-names.ts). The cases are shared with the
 * server test that pins the route (tests/unit/test_investigation_trace_card_names.py).
 *
 * For each case the real card is rendered twice, from the route body
 * without names and with them: the named answer must render with exactly
 * the stored answer's elements (no added link, time, emphasis, code,
 * heading, list or table cell), show every expected string as text and none
 * of the absent ones. A `chat_trace` case also shows exactly the text the
 * chat trace names the answer to, so both views name ids one way. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InvestigationTrace } from '../chat/InvestigationTrace';
import { jsonResponse } from './fetch-stub';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface NameCase {
  id: string;
  answer: string;
  names: Record<string, string>;
  shown: string[];
  absent?: string[];
  chat_trace?: string;
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CASES: NameCase[] = JSON.parse(
  readFileSync(path.join(HERE, 'fixtures', 'investigation-trace-names-cases.json'), 'utf8'),
).cases;
const QUERY_REF = 'q-0123456789ab';

beforeEach(() => {
  window.currentSessionId = 'session-test-names';
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.currentSessionId;
});

interface Rendered {
  tags: string[];
  text: string;
}

/** The card's rendered final answer for one route body: the tag of every
 * element under the answer prose, in document order, and its text. */
async function renderedAnswer(answer: string, names?: Record<string, string>): Promise<Rendered> {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      jsonResponse(200, {
        query_ref: QUERY_REF,
        session_id: 'session-test-names',
        system_id: 'sys-kappa',
        origin: 'system-agent',
        question: 'How did the inlet pressure move?',
        steps: [],
        steps_dropped: 0,
        final_answer: answer,
        ...(names ? { final_answer_names: names } : {}),
        captured_at: '2026-10-27T15:00:00Z',
      }),
    ),
  );
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(<InvestigationTrace queryRef={QUERY_REF} />));
  const details = container.querySelector<HTMLDetailsElement>('details.evidence-investigation')!;
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event('toggle', { bubbles: true }));
  });
  const prose = container.querySelector('.investigation-answer-prose')!;
  expect(prose).not.toBeNull();
  const out = {
    tags: [...prose.querySelectorAll('*')].map((el) => el.tagName.toLowerCase()),
    text: prose.textContent ?? '',
  };
  act(() => root.unmount());
  container.remove();
  return out;
}

describe('<InvestigationTrace/> names from the graph show as text', () => {
  it.each(CASES.map((c) => [c.id, c] as const))(
    '%s: the named answer shows the name and adds no element',
    async (_id, c) => {
      const stored = await renderedAnswer(c.answer);
      const named = await renderedAnswer(c.answer, c.names);
      expect(named.tags).toEqual(stored.tags);
      for (const s of c.shown) expect(named.text).toContain(s);
      for (const s of c.absent ?? []) expect(named.text).not.toContain(s);
      if (c.chat_trace !== undefined) expect(named.text).toBe(c.chat_trace);
    },
  );

  it('the shape check sees the link and time an inserted name used to add', async () => {
    // Detector sanity: names written into the answer TEXT before parsing (the
    // previous design) become a link (the trace renders <a> as a span) and a
    // localized <time>, so the comparison above would catch them.
    const c = CASES.find((x) => x.id === 'url-name')!;
    const stored = await renderedAnswer(c.answer);
    const asText = await renderedAnswer(c.chat_trace!);
    expect(asText.tags).not.toEqual(stored.tags);
    const t = CASES.find((x) => x.id === 'utc-time-name')!;
    const timeAsText = await renderedAnswer(t.chat_trace!);
    expect(timeAsText.tags).toContain('time');
  });

  it('a malformed names field is ignored, never rendered', async () => {
    const c = CASES.find((x) => x.id === 'plain-name')!;
    const stored = await renderedAnswer(c.answer);
    for (const bad of [
      ['not', 'a', 'map'],
      { 'not-an-id': 'Test Rig 4' },
      { [Object.keys(c.names)[0]]: 42 },
    ]) {
      const named = await renderedAnswer(c.answer, bad as unknown as Record<string, string>);
      expect(named).toEqual(stored);
    }
  });
});
