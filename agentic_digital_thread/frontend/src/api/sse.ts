// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* SSE transport — two flavors used by the app:
 *
 * 1. POST-SSE (`postSse` / `SseLineParser`): `/api/ask` streams SSE over a
 *    fetch()ed POST response body. The parser is a hand-rolled loop: accumulate decoded
 *    chunks into a buffer, split on '\n' keeping the trailing partial line,
 *    then interpret `event: <type>` / `data: <json>` line pairs. Event types
 *    include `token`, `tool_start`, `tool_result`, `tool_end`, `done`.
 *
 * 2. Native EventSource (`openEventSource`): GET streams such as
 *    `/api/dashboard/stream?plant=` — thin wrapper adding JSON parsing and
 *    per-event-type handler registration. */
import { authFetch } from './client';

export type SseEventHandler = (eventType: string, data: unknown) => void;

/** Incremental parser for the `event:`/`data:` line grammar of /api/ask.
 *
 * Behavior contract:
 * - the buffer keeps the trailing partial line across feed() calls
 *   (`lines.pop()`), so events split across network chunks re-assemble;
 * - an `event: ` line sets the current event type; it applies to every
 *   subsequent `data: ` line until the next `event: ` line;
 * - `data: ` lines seen before any `event: ` line are ignored;
 * - other lines (blank separators, comments) are ignored.
 *
 * With `singleDataPerEvent` (the /api/onboard/chat
 * grammar): each `event: ` line is consumed by exactly ONE `data: ` line —
 * the event type resets to null after firing, so further `data: ` lines are
 * ignored until the next `event: ` line.
 *
 * With `frameErrorTag` (the /api/ask chat turn): a `data: ` line that is not
 * JSON is skipped and a handler that throws is logged with its event type
 * (`<tag> frame skipped (not JSON)` / `<tag> handler failed`); reading goes
 * on either way, so a bug in our own code never ends the stream and never
 * reads as a dropped connection. Without it the first failure throws out of
 * feed() (and rejects postSse), as before. */
export interface SseLineParserOptions {
  singleDataPerEvent?: boolean;
  frameErrorTag?: string;
}

export class SseLineParser {
  private buffer = '';
  private eventType: string | null = null;

  constructor(
    private readonly onEvent: SseEventHandler,
    private readonly options: SseLineParserOptions = {},
  ) {}

  /** Feed a decoded text chunk; fires onEvent for each completed event line pair. */
  feed(chunk: string): void {
    this.buffer += chunk;
    const lines = this.buffer.split('\n');
    this.buffer = lines.pop() ?? '';

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        this.eventType = line.slice(7);
      } else if (line.startsWith('data: ') && this.eventType) {
        this.dispatch(this.eventType, line.slice(6));
        if (this.options.singleDataPerEvent) this.eventType = null;
      }
    }
  }

  /** Parse one frame's data and hand it to onEvent (see frameErrorTag). The
   * console line names the event type and the frame size, never its text. */
  private dispatch(eventType: string, raw: string): void {
    const tag = this.options.frameErrorTag;
    if (tag === undefined) {
      this.onEvent(eventType, JSON.parse(raw) as unknown);
      return;
    }
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch {
      console.error(`${tag} frame skipped (not JSON)`, eventType, `${raw.length} chars`);
      return;
    }
    try {
      this.onEvent(eventType, data);
    } catch (err) {
      console.error(`${tag} handler failed`, eventType, err);
    }
  }
}

export interface PostSseOptions {
  /** Wired to the Stop button — aborting rejects the read loop with AbortError. */
  signal?: AbortSignal;
  /** Injectable fetch for tests; defaults to the cookie-session authFetch. */
  fetchFn?: (url: string, options: RequestInit) => Promise<Response | null>;
  /** Use the /api/onboard/chat grammar (one data line per event line). */
  singleDataPerEvent?: boolean;
  /** Log-and-continue on a bad frame or a throwing handler (SseLineParser). */
  frameErrorTag?: string;
}

/** POST `body` as JSON to `url` and stream the SSE response through `onEvent`.
 *
 * Resolves when the stream ends; rejects with AbortError when the signal
 * aborts mid-read (caught for the Stop button). Returns without
 * reading when authFetch initiated a 401 login redirect. */
export async function postSse(
  url: string,
  body: unknown,
  onEvent: SseEventHandler,
  options: PostSseOptions = {},
): Promise<void> {
  const doFetch = options.fetchFn ?? authFetch;

  const response = await doFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
  });
  if (!response) return; // 401 → login redirect already initiated
  if (!response.ok) throw new Error(`POST ${url} → ${response.status}`);
  if (!response.body) throw new Error(`POST ${url} → ${response.status}: no response body`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseLineParser(onEvent, {
    singleDataPerEvent: options.singleDataPerEvent,
    frameErrorTag: options.frameErrorTag,
  });

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parser.feed(decoder.decode(value, { stream: true }));
  }
}

/** Per-event-type handlers for a native EventSource stream. Values receive the
 * JSON-parsed `data` payload (or the raw string when it isn't valid JSON). */
export type EventSourceHandlers = Record<string, (data: unknown) => void>;

function parseData(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/** Thin EventSource wrapper for GET-SSE endpoints (e.g. /api/dashboard/stream).
 * Caller owns the returned EventSource and must close() it on teardown. */
export function openEventSource(
  url: string,
  handlers: EventSourceHandlers,
  onError?: (e: Event) => void,
): EventSource {
  const es = new EventSource(url);
  for (const [type, handler] of Object.entries(handlers)) {
    if (type === 'message') {
      es.onmessage = (ev) => handler(parseData(ev.data));
    } else {
      es.addEventListener(type, (ev) => handler(parseData((ev as MessageEvent).data)));
    }
  }
  if (onError) es.onerror = onError;
  return es;
}
