// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Every surface that renders answer markdown shows a UTC time in the viewer's
 * zone: the shared <Md/> component (chat answer, streaming, follow-ups, data
 * modal, dashboard briefing and pinned cards) and the investigation trace's
 * prose. The model writes `YYYY-MM-DD HH:MM UTC` or an ISO instant with Z;
 * the page renders a <time> whose text is local and whose title keeps the
 * UTC source. Code, link text and times without a date stay as written. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { InvestigationTrace } from '../chat/InvestigationTrace';
import { setDisplayTimeZone } from '../lib/answer-times';
import { Md } from '../shared/ui/Md';
import { jsonResponse } from './fetch-stub';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  setDisplayTimeZone('America/Denver', 'en-US');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  setDisplayTimeZone(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

function times(): { text: string | null; dateTime: string | null; title: string | null }[] {
  return [...container.querySelectorAll('time')].map((t) => ({
    text: t.textContent,
    dateTime: t.getAttribute('datetime'),
    title: t.getAttribute('title'),
  }));
}

describe('<Md/> shows UTC answer times in the viewer zone', () => {
  it('converts the answer form in a paragraph', () => {
    render(<Md text="Window queried: 2026-10-15 06:00 UTC → 2026-10-15 14:17 UTC." />);
    expect(times()).toEqual([
      { text: '2026-10-15 00:00 MDT', dateTime: '2026-10-15T06:00Z', title: '2026-10-15 06:00 UTC' },
      { text: '2026-10-15 08:17 MDT', dateTime: '2026-10-15T14:17Z', title: '2026-10-15 14:17 UTC' },
    ]);
    expect(container.textContent).toBe('Window queried: 2026-10-15 00:00 MDT → 2026-10-15 08:17 MDT.');
  });

  it('converts an ISO instant and crosses midnight into the previous local day', () => {
    render(<Md text="Last reading at 2026-10-16T03:30:15Z (spindle)." />);
    expect(times()).toEqual([
      { text: '2026-10-15 21:30:15 MDT', dateTime: '2026-10-16T03:30:15Z', title: '2026-10-16T03:30:15Z' },
    ]);
  });

  it('pins a half-hour zone and the daylight saving change', () => {
    setDisplayTimeZone('Asia/Kolkata', 'en-US');
    render(<Md text="At 2026-10-15 14:17 UTC." />);
    expect(times()[0].text).toBe('2026-10-15 19:47 GMT+5:30');
    setDisplayTimeZone('America/Denver', 'en-US');
    render(<Md text="Before 2026-11-01 07:59 UTC, after 2026-11-01 08:00 UTC." />);
    expect(times().map((t) => t.text)).toEqual(['2026-11-01 01:59 MDT', '2026-11-01 01:00 MST']);
  });

  it('converts a time inside a table cell and inside bold text', () => {
    const md = '| Order | Raised |\n|---|---|\n| WO-1 | 2026-10-15 14:17 UTC |\n\n**Due 2026-10-16 12:00 UTC**';
    render(<Md text={md} />);
    expect(container.querySelector('td time')?.textContent).toBe('2026-10-15 08:17 MDT');
    expect(container.querySelector('strong time')?.textContent).toBe('2026-10-16 06:00 MDT');
  });

  it('converts in inline mode (answer section headings)', () => {
    render(<Md inline text="Shift ending 2026-10-15 14:17 UTC" />);
    expect(container.querySelector('span time')?.textContent).toBe('2026-10-15 08:17 MDT');
  });

  it('leaves code spans and fenced code untouched', () => {
    render(<Md text={'Use `2026-10-15 14:17 UTC` here.\n\n```\nend_iso: 2026-10-15T14:17:00Z\n```'} />);
    expect(times()).toEqual([]);
    expect(container.querySelector('p code')?.textContent).toBe('2026-10-15 14:17 UTC');
    expect(container.querySelector('pre code')?.textContent).toContain('2026-10-15T14:17:00Z');
  });

  it('leaves link text untouched, including citation badges', () => {
    render(
      <Md text={'See [2026-10-15 14:17 UTC](https://example.com/run) and [1](<#cite-system-erp> "2026-10-15 14:17 UTC").'} />,
    );
    expect(times()).toEqual([]);
    expect(container.querySelector('a[href="https://example.com/run"]')?.textContent).toBe('2026-10-15 14:17 UTC');
    expect(container.querySelector('a.cite-badge')?.textContent).toBe('1');
  });

  it('leaves a time with no date and a date with no time alone', () => {
    render(<Md text="The alarm fired at 14:17 UTC on 2026-10-15." />);
    expect(times()).toEqual([]);
    expect(container.textContent).toBe('The alarm fired at 14:17 UTC on 2026-10-15.');
  });
});

describe('<Md/> keeps a stored local time and its UTC audit value as one value', () => {
  it('leaves a parenthetical UTC stamp after a zoned local time as written', () => {
    // Answers written before the change gave local time with UTC in parentheses.
    render(<Md text="Window end 2026-10-15 08:17 MDT (2026-10-15 14:17 UTC), then the next run." />);
    expect(times()).toEqual([]);
    expect(container.textContent).toBe('Window end 2026-10-15 08:17 MDT (2026-10-15 14:17 UTC), then the next run.');
  });

  it('leaves 12-hour and offset forms of the pair alone too', () => {
    render(<Md text={'Oct 15, 8:17 AM MDT (2026-10-15T14:17Z) and 08:17 -06:00 (2026-10-15 14:17 UTC)'} />);
    expect(times()).toEqual([]);
  });

  it('still converts a parenthetical UTC stamp that follows no local time', () => {
    render(<Md text="Last reading (2026-10-15 14:17 UTC) and a UTC time 14:17 UTC (2026-10-15 14:17 UTC)." />);
    expect(times().map((t) => t.text)).toEqual(['2026-10-15 08:17 MDT', '2026-10-15 08:17 MDT']);
  });
});

describe('<InvestigationTrace/> prose shows UTC times in the viewer zone', () => {
  it('converts the system agent final answer and reasoning', async () => {
    const fetchFn = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        query_ref: 'q-0123456789ab',
        session_id: 'session-test-tz',
        system_id: 'erp',
        origin: 'system-agent',
        question: 'Readings between 2026-10-15T06:00Z and 2026-10-15T14:17Z',
        kg_context_attached: false,
        steps: [
          {
            seq: 1,
            reasoning: 'Reading from 2026-10-15T06:00:00Z.',
            tool_name: 'execute_sql',
            tool_input: '{"start":"2026-10-15T06:00:00Z"}',
            input_bytes: 33,
            tool_output: '[]',
            output_bytes: 2,
            status: 'success',
            duration_ms: 5,
          },
        ],
        steps_dropped: 0,
        final_answer: 'Last reading 2026-10-15 14:17 UTC.',
        final_answer_bytes: 34,
      }),
    );
    vi.stubGlobal('fetch', fetchFn);
    window.currentSessionId = 'session-test-tz';
    render(<InvestigationTrace queryRef="q-0123456789ab" />);
    const details = container.querySelector<HTMLDetailsElement>('details.evidence-investigation')!;
    await act(async () => {
      details.open = true;
      details.dispatchEvent(new Event('toggle', { bubbles: true }));
    });
    expect(times().map((t) => t.text)).toEqual(['2026-10-15 00:00:00 MDT', '2026-10-15 08:17 MDT']);
    // The question and tool payloads are literal DATA and stay as sent.
    expect(container.textContent).toContain('Asked: Readings between 2026-10-15T06:00Z and 2026-10-15T14:17Z');
    expect(container.querySelector('.investigation-tool-input')?.textContent).toBe('{"start":"2026-10-15T06:00:00Z"}');
    delete window.currentSessionId;
  });
});
