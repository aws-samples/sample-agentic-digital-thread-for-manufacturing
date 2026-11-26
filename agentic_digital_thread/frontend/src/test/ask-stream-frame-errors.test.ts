// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* runAskTurn over the REAL postSse: a failure in our own code while handling
 * one stream frame must not read as "The connection dropped".
 *
 * On a test event four chat turns ended with that line at the first step of
 * the first question after a new system was connected; asking again seconds
 * later worked, and the retries quoted a turn the server had finished. A JS
 * exception inside an event handler rejected postSse, and every rejection
 * was reported as a dropped connection. These pins:
 *  - a handler (investigation-graph bridge or store reducer) that throws on
 *    one frame is logged with the event type, the later frames still arrive
 *    and the turn settles on its done frame (no stuck spinner, no error line);
 *  - a frame whose data is not JSON is skipped, not fatal;
 *  - a done/error frame whose store reducer throws (even after it ended
 *    streaming) leaves a NOTIFIED error on the turn, never a silent
 *    half-settled answer;
 *  - a body that ends with no done/error frame, and a real transport failure,
 *    still show the connection-dropped line, each with its own console tag. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../chat/arch-bridge', () => ({
  handleArchEvent: vi.fn(),
  resetArchitectureState: vi.fn(),
}));

import { handleArchEvent } from '../chat/arch-bridge';
import { chatThreadStore, runAskTurn } from '../chat/useAskStream';

const archMock = vi.mocked(handleArchEvent);
const DROPPED =
  'The connection dropped before the answer arrived. Ask again. If it happens twice, ask for help.';
const NOT_SHOWN =
  'The answer could not be shown. Ask again. If it happens twice, ask for help.';
const deps = { plant: 'all', showThread: () => {}, onAnswerReady: () => {} };

function frames(lines: string[]): Response {
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

const TURN = [
  'event: tool_start',
  'data: {"tool": "plant1___query_records", "tool_id": "t1", "input": {"question": "count"}}',
  '',
  'event: tool_result',
  'data: {"tool": "plant1___query_records", "tool_id": "t1", "input": "3 rows", "summary": "3 rows"}',
  '',
  'event: token',
  'data: {"text": "Three records failed."}',
  '',
  'event: done',
  'data: {}',
  '',
];

let consoleSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  const ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  chatThreadStore.clear();
  archMock.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function serve(lines: string[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => frames(lines)),
  );
}

function lastAssistant() {
  return chatThreadStore.getState().turns.at(-1)!.assistant;
}

describe('runAskTurn — our own frame failures', () => {
  it('a graph-bridge handler that throws on tool_start leaves the turn answered', async () => {
    const boom = new Error('graph handler bug');
    archMock.mockImplementation((type: string) => {
      if (type === 'tool_start') throw boom;
    });
    serve(TURN);
    await expect(runAskTurn('How many records failed?', new AbortController(), deps)).resolves.toBeUndefined();
    const a = lastAssistant();
    expect(a.streaming).toBe(false);
    expect(a.error).toBeNull();
    expect(a.answerMd).toContain('Three records failed.');
    expect(consoleSpy).toHaveBeenCalledWith('[ask] handler failed', 'tool_start', boom);
    expect(consoleSpy).not.toHaveBeenCalledWith('[ask] stream failed:', expect.anything());
  });

  it('a store reducer that throws on one frame still lets done settle the turn', async () => {
    const boom = new Error('reducer bug');
    const real = chatThreadStore.applyEvent;
    vi.spyOn(chatThreadStore, 'applyEvent').mockImplementation((type, raw, finding) => {
      if (type === 'tool_result') throw boom;
      return real(type, raw, finding);
    });
    serve(TURN);
    await runAskTurn('How many records failed?', new AbortController(), deps);
    const a = lastAssistant();
    expect(a.streaming).toBe(false);
    expect(a.error).toBeNull();
    expect(a.answerMd).toContain('Three records failed.');
    expect(consoleSpy).toHaveBeenCalledWith('[ask] handler failed', 'tool_result', boom);
  });

  it('a frame that is not JSON is skipped and the turn still answers', async () => {
    serve([
      'event: token',
      'data: {"text": "Three "}',
      'data: not-json',
      'data: {"text": "records failed."}',
      '',
      'event: done',
      'data: {}',
      '',
    ]);
    await runAskTurn('How many records failed?', new AbortController(), deps);
    const a = lastAssistant();
    expect(a.error).toBeNull();
    expect(a.answerMd).toContain('Three records failed.');
    expect(consoleSpy).toHaveBeenCalledWith(
      '[ask] frame skipped (not JSON)',
      'token',
      expect.any(String),
    );
  });

  it('a body that ends with no done or error frame is still a dropped connection', async () => {
    serve(['event: token', 'data: {"text": "partial"}', '']);
    await runAskTurn('How many records failed?', new AbortController(), deps);
    expect(lastAssistant().error).toBe(DROPPED);
    expect(consoleSpy).toHaveBeenCalledWith('[ask] stream ended without a done or error frame');
  });

  it('a real transport failure is still a dropped connection with its own tag', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await runAskTurn('How many records failed?', new AbortController(), deps);
    expect(lastAssistant().error).toBe(DROPPED);
    expect(consoleSpy).toHaveBeenCalledWith('[ask] stream failed:', 'Failed to fetch');
  });

  it('a done frame whose own handler throws never leaves a spinner', async () => {
    const boom = new Error('done reducer bug');
    const real = chatThreadStore.applyEvent;
    vi.spyOn(chatThreadStore, 'applyEvent').mockImplementation((type, raw, finding) => {
      if (type === 'done') throw boom;
      return real(type, raw, finding);
    });
    serve(TURN);
    await runAskTurn('How many records failed?', new AbortController(), deps);
    const a = lastAssistant();
    expect(a.streaming).toBe(false);
    expect(a.error).toBe(NOT_SHOWN);
    expect(consoleSpy).toHaveBeenCalledWith('[ask] terminal handler failed', 'done', boom);
    expect(consoleSpy).not.toHaveBeenCalledWith('[ask] stream ended without a done or error frame');
  });

  it('a done reducer that ends streaming and then throws still shows a notified error', async () => {
    const boom = new Error('done reducer bug after the flag');
    const real = chatThreadStore.applyEvent;
    vi.spyOn(chatThreadStore, 'applyEvent').mockImplementation((type, raw, finding) => {
      if (type === 'done') {
        chatThreadStore.getState().turns.at(-1)!.assistant.streaming = false;
        throw boom;
      }
      return real(type, raw, finding);
    });
    const seenErrors: (string | null)[] = [];
    const unsubscribe = chatThreadStore.subscribe(() => {
      seenErrors.push(chatThreadStore.getState().turns.at(-1)?.assistant.error ?? null);
    });
    serve(TURN);
    await runAskTurn('How many records failed?', new AbortController(), deps);
    unsubscribe();
    const a = lastAssistant();
    expect(a.streaming).toBe(false);
    expect(a.error).toBe(NOT_SHOWN);
    expect(seenErrors).toContain(NOT_SHOWN); // subscribers were told, not just the state
    expect(consoleSpy).toHaveBeenCalledWith('[ask] terminal handler failed', 'done', boom);
  });

  it('an error-frame reducer that ends streaming and then throws still shows the error', async () => {
    const real = chatThreadStore.errorTurn;
    let calls = 0;
    vi.spyOn(chatThreadStore, 'errorTurn').mockImplementation((message, ref) => {
      calls += 1;
      if (calls === 1) {
        chatThreadStore.getState().turns.at(-1)!.assistant.streaming = false;
        throw new Error('error reducer bug');
      }
      return real(message, ref);
    });
    serve([
      'event: error',
      'data: {"text": "Query failed. Retry in a moment.", "correlation_id": "s-1"}',
      '',
      'event: done',
      'data: {}',
      '',
    ]);
    await runAskTurn('How many records failed?', new AbortController(), deps);
    const a = lastAssistant();
    expect(a.streaming).toBe(false);
    expect(a.error).toBe('Query failed. Retry in a moment.');
    expect(consoleSpy).toHaveBeenCalledWith(
      '[ask] terminal handler failed',
      'error',
      expect.any(Error),
    );
  });
});
