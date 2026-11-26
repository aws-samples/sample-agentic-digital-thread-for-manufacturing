// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Fixture tests for the /api/ask POST-SSE parser (src/api/sse.ts).
 *
 * The fixture reproduces the real event grammar streamed by /api/ask
 * (event:/data: line pairs; token, tool_start, tool_result, done) and the
 * chunking hazards the parser loop handles: chunks that split a line
 * mid-prefix, mid-JSON, and multiple events in one chunk. */
import { describe, expect, it, vi } from 'vitest';

import { postSse, SseLineParser } from '../api/sse';

type Captured = { event: string; data: unknown };

function capture(): { events: Captured[]; onEvent: (e: string, d: unknown) => void } {
  const events: Captured[] = [];
  return { events, onEvent: (event, data) => events.push({ event, data }) };
}

/** The canonical /api/ask stream fixture: a token run, a tool call with its
 * result, another token run, then done. */
const FIXTURE_LINES = [
  'event: token',
  'data: {"text": "Checking "}',
  '',
  'event: token',
  'data: {"text": "the line…"}',
  '',
  'event: tool_start',
  'data: {"tool": "query_mes", "tool_id": "t1", "input": {"question": "line 2 status"}}',
  '',
  'event: tool_result',
  'data: {"tool": "query_mes", "tool_id": "t1", "input": "RESULT text", "summary": "3 rows"}',
  '',
  'event: token',
  'data: {"text": "Line 2 is running."}',
  '',
  'event: done',
  'data: {"status": "complete"}',
  '',
];
const FIXTURE = FIXTURE_LINES.join('\n');

const EXPECTED: Captured[] = [
  { event: 'token', data: { text: 'Checking ' } },
  { event: 'token', data: { text: 'the line…' } },
  {
    event: 'tool_start',
    data: { tool: 'query_mes', tool_id: 't1', input: { question: 'line 2 status' } },
  },
  {
    event: 'tool_result',
    data: { tool: 'query_mes', tool_id: 't1', input: 'RESULT text', summary: '3 rows' },
  },
  { event: 'token', data: { text: 'Line 2 is running.' } },
  { event: 'done', data: { status: 'complete' } },
];

describe('SseLineParser', () => {
  it('parses the full fixture fed as one chunk', () => {
    const { events, onEvent } = capture();
    new SseLineParser(onEvent).feed(FIXTURE);
    expect(events).toEqual(EXPECTED);
  });

  it('re-assembles events split across arbitrary chunk boundaries', () => {
    // Slice the stream into awkward 7-char chunks — splits land mid-prefix
    // ("even|t: "), mid-JSON, and mid-multibyte-adjacent positions.
    const { events, onEvent } = capture();
    const parser = new SseLineParser(onEvent);
    for (let i = 0; i < FIXTURE.length; i += 7) {
      parser.feed(FIXTURE.slice(i, i + 7));
    }
    expect(events).toEqual(EXPECTED);
  });

  it('keeps a trailing partial line buffered until completed', () => {
    const { events, onEvent } = capture();
    const parser = new SseLineParser(onEvent);
    parser.feed('event: token\ndata: {"text": "hel');
    expect(events).toEqual([]); // data line not terminated yet
    parser.feed('lo"}\n');
    expect(events).toEqual([{ event: 'token', data: { text: 'hello' } }]);
  });

  it('applies the current event type to subsequent data lines (chat.js semantics)', () => {
    const { events, onEvent } = capture();
    const parser = new SseLineParser(onEvent);
    parser.feed('event: token\ndata: {"text": "a"}\ndata: {"text": "b"}\n');
    expect(events).toEqual([
      { event: 'token', data: { text: 'a' } },
      { event: 'token', data: { text: 'b' } },
    ]);
  });

  it('ignores data lines before any event line, blanks, and unknown lines', () => {
    const { events, onEvent } = capture();
    const parser = new SseLineParser(onEvent);
    parser.feed('data: {"orphan": true}\n: comment\n\nevent: done\ndata: {}\n');
    expect(events).toEqual([{ event: 'done', data: {} }]);
  });
});

describe('postSse', () => {
  function streamResponse(text: string, chunkSize = 11): Response {
    const encoder = new TextEncoder();
    const bytes = encoder.encode(text);
    let offset = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (offset >= bytes.length) {
          controller.close();
          return;
        }
        controller.enqueue(bytes.slice(offset, offset + chunkSize));
        offset += chunkSize;
      },
    });
    return new Response(stream, { status: 200 });
  }

  it('streams the fixture through the injected fetch and fires all events', async () => {
    const { events, onEvent } = capture();
    const fetchFn = vi.fn(async () => streamResponse(FIXTURE));

    await postSse('/api/ask', { question: 'q', session_id: 's1', plant: 'all' }, onEvent, {
      fetchFn,
    });

    expect(events).toEqual(EXPECTED);
    expect(fetchFn).toHaveBeenCalledWith(
      '/api/ask',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: 'q', session_id: 's1', plant: 'all' }),
      }),
    );
  });

  it('passes the AbortSignal through to fetch (Stop button wiring)', async () => {
    const controller = new AbortController();
    const fetchFn = vi.fn(async (_url: string, options: RequestInit) => {
      expect(options.signal).toBe(controller.signal);
      return streamResponse(FIXTURE);
    });

    const { onEvent } = capture();
    await postSse('/api/ask', {}, onEvent, { fetchFn, signal: controller.signal });
    expect(fetchFn).toHaveBeenCalledOnce();
  });

  it('returns without reading when authFetch signalled a 401 redirect (null)', async () => {
    const { events, onEvent } = capture();
    const fetchFn = vi.fn(async () => null);
    await expect(postSse('/api/ask', {}, onEvent, { fetchFn })).resolves.toBeUndefined();
    expect(events).toEqual([]);
  });

  it('rejects non-2xx responses before reading the body or feeding events', async () => {
    const { events, onEvent } = capture();
    const getReader = vi.fn();
    const fetchFn = vi.fn(async () =>
      ({
        ok: false,
        status: 503,
        body: { getReader },
      }) as unknown as Response,
    );

    await expect(postSse('/api/ask', {}, onEvent, { fetchFn })).rejects.toThrow(
      'POST /api/ask → 503',
    );
    expect(getReader).not.toHaveBeenCalled();
    expect(events).toEqual([]);
  });
});

describe('postSse frameErrorTag (our own frame/handler failures are not a dropped stream)', () => {
  function streamOf(lines: string[]): Response {
    const bytes = new TextEncoder().encode(lines.join('\n'));
    return new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(bytes);
          controller.close();
        },
      }),
      { status: 200 },
    );
  }

  const LINES = [
    'event: token',
    'data: {"text": "one"}',
    '',
    'event: tool_start',
    'data: {"tool": "plant1___query_records", "tool_id": "t1"}',
    '',
    'event: token',
    'data: {"text": "two"}',
    '',
    'event: done',
    'data: {}',
    '',
  ];

  it('logs a handler that throws on one frame and keeps reading to done', async () => {
    const seen: string[] = [];
    const boom = new Error('handler bug');
    const onEvent = (type: string) => {
      seen.push(type);
      if (type === 'tool_start') throw boom;
    };
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      await expect(
        postSse('/api/ask', {}, onEvent, {
          fetchFn: async () => streamOf(LINES),
          frameErrorTag: '[ask]',
        }),
      ).resolves.toBeUndefined();
      expect(seen).toEqual(['token', 'tool_start', 'token', 'done']);
      expect(consoleSpy).toHaveBeenCalledWith('[ask] handler failed', 'tool_start', boom);
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('skips a frame whose data is not JSON and delivers the frames around it', async () => {
    const { events, onEvent } = capture();
    const lines = [
      'event: token',
      'data: {"text": "before"}',
      'data: not-json',
      'data: {"text": "after"}',
      '',
      'event: done',
      'data: {}',
      '',
    ];
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      await postSse('/api/ask', {}, onEvent, {
        fetchFn: async () => streamOf(lines),
        frameErrorTag: '[ask]',
      });
      expect(events).toEqual([
        { event: 'token', data: { text: 'before' } },
        { event: 'token', data: { text: 'after' } },
        { event: 'done', data: {} },
      ]);
      expect(consoleSpy).toHaveBeenCalledTimes(1);
      const [tag, type] = consoleSpy.mock.calls[0];
      expect(tag).toBe('[ask] frame skipped (not JSON)');
      expect(type).toBe('token');
      // The frame's own text never reaches the console.
      expect(JSON.stringify(consoleSpy.mock.calls)).not.toContain('not-json');
    } finally {
      consoleSpy.mockRestore();
    }
  });

  it('without the tag keeps the old contract: the first failure ends the stream', async () => {
    const onEvent = (type: string) => {
      if (type === 'tool_start') throw new Error('handler bug');
    };
    await expect(
      postSse('/api/ask', {}, onEvent, { fetchFn: async () => streamOf(LINES) }),
    ).rejects.toThrow('handler bug');
  });
});

describe('SseLineParser singleDataPerEvent (the /api/onboard/chat grammar)', () => {
  // The onboard-chat grammar resets eventType = null after each handled data
  // line: one `event: ` line is consumed by exactly ONE `data: ` line, and
  // stray data lines without a fresh event line are dropped.
  it('fires one event per event/data pair and drops trailing data lines', () => {
    const events: Captured[] = [];
    const parser = new SseLineParser((event, data) => events.push({ event, data }), {
      singleDataPerEvent: true,
    });
    parser.feed(
      [
        'event: token',
        'data: {"text": "a"}',
        'data: {"text": "DROPPED — no fresh event line"}',
        'event: token',
        'data: {"text": "b"}',
        '',
      ].join('\n'),
    );
    expect(events).toEqual([
      { event: 'token', data: { text: 'a' } },
      { event: 'token', data: { text: 'b' } },
    ]);
  });

  it('default (multi-data) grammar still applies every data line to the current event', () => {
    const events: Captured[] = [];
    const parser = new SseLineParser((event, data) => events.push({ event, data }));
    parser.feed(['event: token', 'data: {"text": "a"}', 'data: {"text": "b"}', ''].join('\n'));
    expect(events).toEqual([
      { event: 'token', data: { text: 'a' } },
      { event: 'token', data: { text: 'b' } },
    ]);
  });

  it('re-assembles onboard events split across chunk boundaries', () => {
    const stream = [
      'event: progress',
      'data: {"step": 1}',
      '',
      'event: checkpoint',
      'data: {"step": "discover", "result": {"tables": []}}',
      '',
      'event: credential_request',
      'data: {"fields": [{"name": "password"}], "system_id": "erp"}',
      '',
      'event: done',
      'data: {"mutations": [], "action": null}',
      '',
    ].join('\n');
    const events: Captured[] = [];
    const parser = new SseLineParser((event, data) => events.push({ event, data }), {
      singleDataPerEvent: true,
    });
    for (let i = 0; i < stream.length; i += 7) parser.feed(stream.slice(i, i + 7));
    expect(events.map((e) => e.event)).toEqual([
      'progress',
      'checkpoint',
      'credential_request',
      'done',
    ]);
    expect(events[0].data).toEqual({ step: 1 });
    expect(events[2].data).toEqual({ fields: [{ name: 'password' }], system_id: 'erp' });
  });
});
