// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the steward story GET (/api/admin/workshop/story) is skipped when
 * /api/demo/status reports workshop mode off, so a demo deploy logs no 404 on
 * every page load. The surfaces read 'hidden' / 'off' as they did on the 404.
 * Workshop mode on, or a status that failed or lacks the field, keeps the GET. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { AuthProvider } from '../contexts/AuthContext';
import { useWorkshopMode, useWorkshopStory } from '../features/workshop/useWorkshopStory';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORY_URL = '/api/admin/workshop/story';
const STATUS_URL = '/api/demo/status';

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

function stubFetch(status: { body: unknown; code?: number }): string[] {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      if (url === STATUS_URL) return response(status.body, status.code ?? 200);
      if (url === STORY_URL) return response({ story: 'frozen', clock: null, warnings: [] });
      return response({ detail: 'not stubbed' }, 404);
    }),
  );
  return calls;
}

let container: HTMLDivElement;
let root: Root;
let seen: { gate?: string; mode?: string };

function Probe() {
  seen = { gate: useWorkshopStory().gate, mode: useWorkshopMode() };
  return null;
}

beforeEach(() => {
  seen = {};
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderProbe() {
  const client = createQueryClient();
  client.setQueryDefaults(queryKeys.demoStatus(), { retryDelay: 0 });
  client.setQueryData(queryKeys.authMe(), { user_id: 'admin', email: 'admin@example.com', is_admin: true });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </QueryClientProvider>,
    );
  });
  for (let i = 0; i < 10; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
  }
}

const gets = (calls: string[], url: string) => calls.filter((u) => u === url).length;

describe('workshop story GET follows /api/demo/status', () => {
  it('skips the story GET when workshop mode is off', async () => {
    const calls = stubFetch({ body: { enabled: true, workshop: false } });
    await renderProbe();
    expect(gets(calls, STATUS_URL)).toBeGreaterThan(0);
    expect(gets(calls, STORY_URL)).toBe(0);
    expect(seen).toEqual({ gate: 'hidden', mode: 'off' });
  });

  it('reads the story when workshop mode is on', async () => {
    const calls = stubFetch({ body: { enabled: false, workshop: true } });
    await renderProbe();
    expect(gets(calls, STORY_URL)).toBe(1);
    expect(seen).toEqual({ gate: 'hidden', mode: 'on' });
  });

  it('falls back to the story GET when the status fails or has no workshop field', async () => {
    for (const status of [{ body: { detail: 'x' }, code: 500 }, { body: { enabled: true } }]) {
      const calls = stubFetch(status);
      await renderProbe();
      expect(gets(calls, STORY_URL)).toBe(1);
      act(() => root.unmount());
      root = createRoot(container);
      vi.unstubAllGlobals();
    }
  });
});
