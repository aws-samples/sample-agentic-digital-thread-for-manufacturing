// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* refreshSuggestions() fetch failure — the chat's suggestion chips must not
 * claim "No systems connected yet" when the real problem is that
 * /api/onboard/status (or /api/prompts) failed. Seen on a live deployment
 * 2026-11-16: the dashboard header listed two connected systems while the
 * orchestrator chat showed the no-systems chip, because the one boot-time
 * refreshSuggestions() call failed and nothing ever retried it.
 *
 * Pins: a failed boot read shows a "couldn't load" chip (not the no-systems
 * text), retries on its own, and the retry fills the real suggestions. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setLang } from '../lib/i18n';
import * as chat from '../views/onboard/chat';
import { refreshSuggestions } from '../views/onboard/chat';
import { PersonaGroups } from '../views/onboard/components/PersonaGroups';
import { getOnboardQueryClient } from '../views/onboard/onboard-queries';
import { setSuggestions, suggestionsStore } from '../views/onboard/suggestions-store';
import { getPromptQueryClient } from '../chat/prompt-queries';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STATUS_OK = {
  onboarded_systems: [
    { id: 'mes-1', name: 'Line MES', system_type: 'MES' },
    { id: 'historian-1', name: 'Line Historian', system_type: 'Historian' },
  ],
  suggested_questions: ['What is the machine speed on line 1?', 'Show active alarms'],
};

function json(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Service Unavailable',
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

let container: HTMLDivElement;
let root: Root;
let statusFails: number;
let failStatus: number;
let promptsStatus: number;

beforeEach(() => {
  vi.useFakeTimers();
  const ls = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => ls.get(k) ?? null,
    setItem: (k: string, v: string) => void ls.set(k, String(v)),
    removeItem: (k: string) => void ls.delete(k),
  });
  statusFails = 1;
  failStatus = 503;
  promptsStatus = 200;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (String(url).includes('/api/onboard/status')) {
        // Query's own retries are switched off below, so a failure reaches
        // refreshSuggestions() at once.
        if (statusFails > 0) {
          statusFails -= 1;
          return json(failStatus, { detail: 'failed' });
        }
        return json(200, STATUS_OK);
      }
      if (String(url).includes('/api/prompts')) {
        return promptsStatus === 200 ? json(200, []) : json(promptsStatus, { detail: 'denied' });
      }
      return json(200, {});
    }),
  );
  setLang('en');
  setSuggestions({ kind: 'loading' });
  getOnboardQueryClient().clear();
  getPromptQueryClient().clear();
  getOnboardQueryClient().setDefaultOptions({ queries: { retry: false } });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  // Optional so the suite fails on its assertions (not an import error) on
  // code without the retry.
  (chat as { _resetSuggestionsRetry?: () => void })._resetSuggestionsRetry?.();
  setSuggestions({ kind: 'loading' });
  getOnboardQueryClient().clear();
  getPromptQueryClient().clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('refreshSuggestions fetch failure', () => {
  it('shows a load-failure chip, not the no-systems text, when /api/onboard/status fails', async () => {
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('error');
    expect(container.textContent).not.toContain('No systems connected yet');
    expect(container.textContent).toContain("Couldn't load suggestions");
  });

  it('retries on its own and then shows the real suggestions', async () => {
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('error');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(suggestionsStore.get().kind).toBe('plain');
    expect(container.textContent).toContain('What is the machine speed on line 1?');
    expect(container.textContent).not.toContain("Couldn't load suggestions");
  });

  it('keeps showing suggestions it already has when a later refresh fails', async () => {
    statusFails = 0;
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('plain');
    statusFails = 1;
    getOnboardQueryClient().clear();
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('plain');
    expect(container.textContent).toContain('What is the machine speed on line 1?');
  });

  // A 403 means this user's role cannot read onboarding status (the route
  // needs the integrator or Admin group). Retrying won't change that, and
  // "retrying" would be untrue, so the chip says so and no retry is scheduled.
  it('shows a no-access chip and does not retry on 403', async () => {
    failStatus = 403;
    statusFails = 99;
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('forbidden');
    expect(container.textContent).toContain('need the Admin or integrator role');
    expect(container.textContent).not.toContain('retrying');
    expect(container.textContent).not.toContain('No systems connected yet');
    const fetchMock = vi.mocked(globalThis.fetch);
    const statusCalls = () =>
      fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/onboard/status')).length;
    const before = statusCalls();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000);
    });
    expect(statusCalls()).toBe(before);
    expect(suggestionsStore.get().kind).toBe('forbidden');
  });

  it('treats a 403 from /api/prompts as a load failure, not an onboarding-role problem', async () => {
    statusFails = 0;
    promptsStatus = 403;
    // Make the registry path reachable: status must not short-circuit.
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('error');
    expect(container.textContent).not.toContain('need the Admin or integrator role');
  });

  it('recovers from no-access when a later refresh (e.g. after a role change) succeeds', async () => {
    failStatus = 403;
    statusFails = 1;
    await act(async () => {
      root.render(<PersonaGroups />);
    });
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('forbidden');
    getOnboardQueryClient().clear();
    await act(async () => {
      await refreshSuggestions();
    });
    expect(suggestionsStore.get().kind).toBe('plain');
  });
});
