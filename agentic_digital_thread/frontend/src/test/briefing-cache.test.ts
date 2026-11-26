// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the role-keyed AI Operations Briefing cache (loading-state audit):
 * a generated briefing is cached per role, revisits within the ~5-min TTL
 * serve the cached content instantly with NO regeneration (briefings are
 * expensive LLM calls), switching roles generates per role and switching BACK
 * re-serves from cache, TTL expiry regenerates, an explicit user refresh
 * (force) bypasses the cache, and the single-flight guard still holds. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  _resetBriefingCache,
  BRIEFING_CACHE_TTL_MS,
  BRIEFING_PROMPT,
  BRIEFING_SESSION_ID,
  briefingPromptForRole,
  DEFAULT_BRIEFING_ROLE,
  requestOpsBriefing,
} from '../views/dashboard/briefing';
import { dashboardStore, patchBriefing } from '../views/dashboard/store';

vi.mock('../api/sse', () => ({
  postSse: vi.fn(),
  openEventSource: vi.fn(),
}));

import { postSse } from '../api/sse';

const postSseMock = vi.mocked(postSse);

const BRIEFING_TEXT =
  '- Line status: All lines running.\n' +
  '- Quality: Pass rate at 98 percent.\n' +
  '- Maintenance: One work order overdue.\n' +
  '- Order risk: Order 42 at risk.\n' +
  '- Action: Inspect line 2 today.';

/** Make postSse deliver a successful 'done' briefing. */
function mockGeneration(text = BRIEFING_TEXT): void {
  postSseMock.mockImplementation(async (_url, _body, onEvent) => {
    onEvent('done', { text });
  });
}

function resetBriefingSlice(): void {
  patchBriefing({ phase: 'init', bullets: [], blockMd: '', analyzing: false, timestamp: '' });
}

beforeEach(() => {
  vi.useFakeTimers();
  postSseMock.mockReset();
  _resetBriefingCache();
  resetBriefingSlice();
  (window as unknown as { isStreaming?: boolean }).isStreaming = false;
  // This node/jsdom combo ships a broken localStorage (getItem is not a
  // function) — stub the minimal surface wrapResponseLang reads.
  vi.stubGlobal('localStorage', {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('requestOpsBriefing — role-keyed cache', () => {
  it('generates on first call and renders the bullet content', async () => {
    mockGeneration();
    await requestOpsBriefing();
    expect(postSseMock).toHaveBeenCalledTimes(1);
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets');
    expect(b.bullets).toHaveLength(5);
    expect(b.analyzing).toBe(false);
    expect(b.timestamp).toContain('Updated');
  });

  it('CACHE HIT: a revisit within the TTL re-serves instantly with NO regeneration', async () => {
    mockGeneration();
    await requestOpsBriefing();
    // Simulate an unmount/remount wiping the rendered slice.
    resetBriefingSlice();
    vi.advanceTimersByTime(60_000); // well inside the TTL

    await requestOpsBriefing();

    expect(postSseMock).toHaveBeenCalledTimes(1); // no second LLM call
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets'); // cached content re-applied
    expect(b.bullets).toHaveLength(5);
  });

  it('EXPIRY: after the TTL the cached content still serves first, then regenerates', async () => {
    mockGeneration();
    await requestOpsBriefing();
    resetBriefingSlice();
    vi.advanceTimersByTime(BRIEFING_CACHE_TTL_MS + 1_000);

    let resolveGen!: () => void;
    postSseMock.mockImplementation(
      (_url, _body, onEvent) =>
        new Promise<void>((res) => {
          resolveGen = () => {
            onEvent('done', { text: BRIEFING_TEXT });
            res();
          };
        }),
    );

    const p = requestOpsBriefing();
    // Stale cached content applied synchronously BEFORE the regeneration lands.
    expect(dashboardStore.get().briefing.phase).toBe('bullets');
    expect(postSseMock).toHaveBeenCalledTimes(2);

    resolveGen();
    await p;
    expect(dashboardStore.get().briefing.analyzing).toBe(false);
  });

  it('EXPLICIT REFRESH: force bypasses a fresh cache', async () => {
    mockGeneration();
    await requestOpsBriefing();
    vi.advanceTimersByTime(10_000); // cache is fresh

    await requestOpsBriefing({ force: true });

    expect(postSseMock).toHaveBeenCalledTimes(2); // regenerated despite freshness
  });

  it('ROLE-KEYED: each role generates once; switching back re-serves from cache', async () => {
    mockGeneration();
    await requestOpsBriefing(); // default role → generation 1
    await requestOpsBriefing({ role: 'maintenance' }); // new role → generation 2
    expect(postSseMock).toHaveBeenCalledTimes(2);
    // The maintenance generation used the role-tailored prompt.
    const secondBody = postSseMock.mock.calls[1][1] as { question: string };
    expect(secondBody.question).toContain('maintenance');

    resetBriefingSlice();
    await requestOpsBriefing(); // back to default → cache hit
    expect(postSseMock).toHaveBeenCalledTimes(2);
    expect(dashboardStore.get().briefing.phase).toBe('bullets');

    await requestOpsBriefing({ role: 'maintenance' }); // back again → cache hit
    expect(postSseMock).toHaveBeenCalledTimes(2);
  });

  it('single-flight: concurrent calls share ONE generation', async () => {
    let resolveGen!: () => void;
    postSseMock.mockImplementation(
      (_url, _body, onEvent) =>
        new Promise<void>((res) => {
          resolveGen = () => {
            onEvent('done', { text: BRIEFING_TEXT });
            res();
          };
        }),
    );

    const p1 = requestOpsBriefing();
    const p2 = requestOpsBriefing(); // in-flight guard skips
    expect(postSseMock).toHaveBeenCalledTimes(1);

    resolveGen();
    await Promise.all([p1, p2]);
    expect(dashboardStore.get().briefing.phase).toBe('bullets');
  });

  it('the default role prompt is the classic fixed prompt, byte-identical', () => {
    expect(briefingPromptForRole(DEFAULT_BRIEFING_ROLE)).toBe(BRIEFING_PROMPT);
  });

  it('asks for an ordinary caveat inside its bullet, and allows a required footnote after', () => {
    expect(BRIEFING_PROMPT).toContain('5-bullet');
    // Scoped to ordinary caveats: the orchestrator's identifier-recovery rule
    // requires a footnote AFTER the answer, and the system prompt wins.
    expect(BRIEFING_PROMPT).toMatch(/ordinary caveat about a figure inside the bullet it qualifies/);
    expect(BRIEFING_PROMPT).toMatch(/nothing except a footnote your instructions require/);
    expect(BRIEFING_PROMPT).not.toMatch(/write nothing after the last bullet/);
    // ask.py reads the prompt out of this file as single-quoted pieces.
    expect(BRIEFING_PROMPT).not.toContain("'");
  });

  it('a recovery footnote after a rule is kept as a note under the five cards', async () => {
    mockGeneration(
      BRIEFING_TEXT +
        "\n\n---\n'A-17' isn't a CMMS identifier; matched it to 'B-17' via the shared serial number. " +
        'The mapping was proposed for review.',
    );
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.bullets).toHaveLength(5);
    expect(b.notes).toHaveLength(1);
    expect(b.notes[0]).toContain('proposed for review');
  });

  it('a caveat line after the five bullets is kept as a note, not a sixth card', async () => {
    mockGeneration(
      BRIEFING_TEXT +
        '\n\n*Note: the ERP system flagged its total order value as unverified, so it is left out.*',
    );
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets');
    expect(b.bullets).toHaveLength(5);
    expect(b.notes).toEqual([
      '*Note: the ERP system flagged its total order value as unverified, so it is left out.*',
    ]);
    // The cached copy carries the note too.
    patchBriefing({ phase: 'init', bullets: [], notes: [] });
    await requestOpsBriefing();
    expect(dashboardStore.get().briefing.notes).toHaveLength(1);
  });

  it('six bullets from the stream: five cards, the sixth and a footnote kept as notes', async () => {
    const events: Array<[string, unknown]> = [
      ['tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: {} }],
      ['tool_end', { tool: 'erp___query_erp', tool_id: 't1' }],
      ['token', { text: BRIEFING_TEXT + '\n- Extra: one more item.\n\n---\n' }],
      ['token', { text: "'A-17' isn't a CMMS identifier; matched it to 'B-17'.\nThe mapping was proposed for review." }],
      ['done', { text: '' }],
    ];
    postSseMock.mockImplementation(async (_url, _body, onEvent) => {
      for (const [type, data] of events) onEvent(type, data);
    });
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.bullets).toHaveLength(5);
    expect(b.notes).toEqual([
      '- Extra: one more item.',
      "'A-17' isn't a CMMS identifier; matched it to 'B-17'.",
      'The mapping was proposed for review.',
    ]);
  });
});

describe('briefing session hygiene (live-found on a customer deployment 2026-08-09)', () => {
  // The briefing once rode the USER'S chat session id: every 5-min refresh
  // became a turn in the user's conversation, so "what was my previous
  // question?" answered with the briefing prompt. Synthetic turns must use
  // a dedicated `briefing-` session (AUTOMATED_SESSION_PREFIXES) and
  // suppress memory writes.
  it('POSTs a dedicated briefing- session id, never the shared chat session', async () => {
    mockGeneration();
    await requestOpsBriefing();
    const body = postSseMock.mock.calls[0][1] as { session_id?: string };
    expect(body.session_id).toBe(BRIEFING_SESSION_ID);
    expect(body.session_id).toMatch(/^briefing-/);
  });

  it('suppresses memory writes for the synthetic turn', async () => {
    mockGeneration();
    await requestOpsBriefing();
    const body = postSseMock.mock.calls[0][1] as { write_memory?: boolean };
    expect(body.write_memory).toBe(false);
  });
});

describe('briefing answer from a streamed turn (same rule as the chat)', () => {
  // A normal /api/ask turn ends with an EMPTY done.text, so the briefing is
  // built from the token stream. Text written before a later data call was
  // the model drafting or planning; it must not render beside the final
  // bullets.
  const DRAFT = '- Line status: Line 3 looks down, still checking.\nLet me check maintenance next.\n';
  const PLANNING = 'Now I need to check the CMMS for open orders before I summarise the shift.';

  function mockStream(events: Array<[string, unknown]>): void {
    postSseMock.mockImplementation(async (_url, _body, onEvent) => {
      for (const [type, data] of events) onEvent(type, data);
    });
  }

  const dataCall = (id: string): Array<[string, unknown]> => [
    ['tool_start', { tool: 'cmms___query_cmms', tool_id: id, input: {} }],
    ['tool_result', { tool: 'cmms___query_cmms', tool_id: id, input: 'ok', summary: 'rows' }],
    ['tool_end', { tool: 'cmms___query_cmms', tool_id: id }],
  ];

  it('renders only the five final bullets, not a draft bullet from before a data call', async () => {
    mockStream([
      ['token', { text: DRAFT }],
      ...dataCall('t1'),
      ['token', { text: BRIEFING_TEXT.slice(0, 40) }],
      ['token', { text: BRIEFING_TEXT.slice(40) }],
      ['done', { text: '' }],
    ]);
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets');
    expect(b.bullets).toHaveLength(5);
    const rendered = b.bullets.map((x) => `${x.title} ${x.detail}`).join('\n');
    expect(rendered).not.toContain('still checking');
    expect(rendered).toContain('All lines running');
  });

  it.each([
    ['one card', '- **Line 3:** down since the night shift'],
    ['two cards', '- **Line 3:** down since the night shift\n- **Orders:** two at risk today'],
  ])('with %s, the block fallback starts at the first card, not at narration before it', async (_n, cards) => {
    const NARRATION = 'Looking across every plant system before I write this up.';
    const answer = `${NARRATION}\n\n---\n${cards}\n*Note:* the ERP order total is unverified.`;
    for (const events of [
      [['token', { text: answer }], ['done', { text: '' }]],
      [['done', { text: answer }]],
    ] as Array<Array<[string, unknown]>>) {
      _resetBriefingCache();
      resetBriefingSlice();
      mockStream(events);
      await requestOpsBriefing();
      const b = dashboardStore.get().briefing;
      expect(b.phase).toBe('block');
      expect(b.blockMd).not.toContain('Looking across');
      expect(b.blockMd).toContain('Line 3');
      expect(b.blockMd).toContain('the ERP order total is unverified');
    }
  });

  it('keeps planning text out of the block fallback', async () => {
    mockStream([
      ['token', { text: PLANNING }],
      ...dataCall('t1'),
      ['token', { text: 'All lines are running and no orders are at risk this shift.' }],
      ['done', { text: '' }],
    ]);
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('block');
    expect(b.blockMd).toBe('All lines are running and no orders are at risk this shift.');
  });

  it('keeps the bullets written before a write-back call', async () => {
    mockStream([
      ['token', { text: 'Let me check the lines.' }],
      ...dataCall('t1'),
      ['token', { text: BRIEFING_TEXT }],
      ['tool_start', { tool: 'digital-thread___save_correlation', tool_id: 't2', input: {} }],
      ['tool_end', { tool: 'digital-thread___save_correlation', tool_id: 't2' }],
      ['token', { text: 'Saved one pattern.' }],
      ['done', { text: '' }],
    ]);
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets');
    expect(b.bullets).toHaveLength(5);
    expect(b.bullets.map((x) => `${x.title} ${x.detail}`).join('\n')).toContain('All lines running');
  });

  it('a done event carrying the full text still wins over the token stream', async () => {
    mockStream([
      ['token', { text: DRAFT }],
      ...dataCall('t1'),
      ['token', { text: 'short tail' }],
      ['done', { text: BRIEFING_TEXT }],
    ]);
    await requestOpsBriefing();
    expect(dashboardStore.get().briefing.bullets).toHaveLength(5);
  });

  it('the rehearsal stream shape: eight data calls, then bullets, a note and follow-ups', async () => {
    // A rehearsal briefing (2026-10-15): every run before the last tool call
    // is empty, the final run holds the five bullets, a rule and a closing
    // note, then ---FOLLOWUPS--- Q/A; done.text is empty. The card showed the
    // follow-up lines and 0 bullets.
    const tools = ['explore_graph', 'mes-a___query_mes_a', 'mes-b___query_mes_b',
      'mes-c___query_mes_c', 'mes-d___query_mes_d', 'cmms___query_cmms',
      'cmms-a___query_cmms_a', 'erp___query_erp'];
    const events: Array<[string, unknown]> = [];
    tools.forEach((tool, i) => {
      events.push(['tool_start', { tool, tool_id: `t${i}`, input: {} }]);
      events.push(['tool_result', { tool, tool_id: `t${i}`, input: 'ok', summary: 'rows' }]);
      events.push(['tool_end', { tool, tool_id: `t${i}` }]);
    });
    const text =
      BRIEFING_TEXT +
      '\n\n---\n*Two of these point to the same cause.*\n\n' +
      '---FOLLOWUPS---\nQ1: Check the vibration trend?\nQ2: Which orders are late?\n' +
      'A1: Expedite the overdue PM.\nA2: Dispatch maintenance now.';
    for (let i = 0; i < text.length; i += 37) events.push(['token', { text: text.slice(i, i + 37) }]);
    events.push(['done', { text: '' }]);
    mockStream(events);
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets');
    expect(b.bullets).toHaveLength(5);
    const rendered = b.bullets.map((x) => `${x.title} ${x.detail}`).join('\n');
    expect(rendered).toContain('All lines running');
    expect(rendered).not.toMatch(/Q1|A1|vibration/);
  });

  it('renders all five bullets when a rule sits between them', async () => {
    const lines = BRIEFING_TEXT.split('\n');
    const text =
      lines.slice(0, 2).join('\n') + '\n---\n' + lines.slice(2).join('\n') +
      '\n\n---FOLLOWUPS---\nQ1: Check the trend?\nA1: Expedite the PM.';
    mockStream([
      ['tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: {} }],
      ['tool_end', { tool: 'erp___query_erp', tool_id: 't1' }],
      ['token', { text }],
      ['done', { text: '' }],
    ]);
    await requestOpsBriefing();
    const b = dashboardStore.get().briefing;
    expect(b.phase).toBe('bullets');
    expect(b.bullets).toHaveLength(5);
  });
});
