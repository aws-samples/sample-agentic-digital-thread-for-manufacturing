// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Settings tab — feedback-exploration toggle (plan A15).
 *
 * Pins (each kills a named mutation):
 * - env-kill renders the checkbox DISABLED with an explanatory note naming
 *   the env var (mutation: dropping the `disabled` wiring or the note);
 * - under env-kill, Save SKIPS the feedback-exploration PUT entirely
 *   (mutation: removing the skip would fire a 409 write attempt);
 * - without a kill the checkbox is live, provenance hint shows for
 *   admin-flag source, and Save sends the PUT with the toggled value
 *   (mutation: save not joined into saveSettings / wrong body key). */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { BrandingProvider } from '../contexts/BrandingContext';
import SettingsTab from '../pages/admin/tabs/SettingsTab';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GET_RESPONSES: Record<string, unknown> = {
  '/api/admin/settings': { status: 'ok', demo_enabled: false },
  '/api/admin/models': { orchestrator: [], agent: [] },
  '/api/admin/live-updates': { status: 'ok', live_updates_enabled: true },
  '/api/admin/llm-settings': { orchestrator_model: '', agent_model: '' },
  '/api/admin/concept-packs': { packs: [] },
};

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
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

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

function stubFetch(feedbackExplorationState: unknown): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (method === 'GET') {
      if (url === '/api/admin/feedback-exploration') return response(feedbackExplorationState);
      if (url in GET_RESPONSES) return response(GET_RESPONSES[url]);
      throw new Error(`unexpected GET: ${url}`);
    }
    // Every mutation confirms with the shape its requireOk expects.
    if (url === '/api/admin/settings') return response({ status: 'ok', demo_enabled: false });
    if (url === '/api/admin/live-updates') return response({ status: 'ok', live_updates_enabled: true });
    if (url === '/api/admin/branding') return response({ status: 'ok' });
    if (url === '/api/admin/feedback-exploration') {
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
      return response({
        status: 'ok',
        feedback_exploration_enabled: body.feedback_exploration_enabled === true,
        source: 'admin-flag',
        env_kill_active: false,
      });
    }
    throw new Error(`unexpected ${method}: ${url}`);
  }));
  return calls;
}

async function renderTab(seedFeedbackExploration?: unknown) {
  const client = createQueryClient();
  client.setQueryData(queryKeys.branding(), { company: 'AnyCompany', tagline: 'Tagline' });
  // Seeding the cache (the authMe/branding idiom) makes the hydrate-once
  // effect run at mount — no race between the fetch landing and the click.
  if (seedFeedbackExploration !== undefined) {
    client.setQueryData(queryKeys.admin.feedbackExploration(), seedFeedbackExploration);
  }
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <BrandingProvider updateTitle={false}>
          <SettingsTab onDemoEnabledChange={() => undefined} onBrandingSaved={() => undefined} />
        </BrandingProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() =>
    expect(document.getElementById('settings-feedback-exploration-enabled')).toBeTruthy(),
  );
  return client;
}

function checkbox(): HTMLInputElement {
  return document.getElementById('settings-feedback-exploration-enabled') as HTMLInputElement;
}

async function clickSave() {
  await act(async () => {
    document.getElementById('settings-save-btn')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

describe('settings feedback-exploration toggle', () => {
  it('disables the checkbox and explains the env kill switch when active', async () => {
    stubFetch({ feedback_exploration_enabled: false, source: 'env-kill', env_kill_active: true });
    await renderTab();
    await vi.waitFor(() => expect(checkbox().disabled).toBe(true));
    const note = document.getElementById('settings-feedback-exploration-kill-note')!;
    expect(note.textContent).toContain('FEEDBACK_EXPLORATION_ENABLED');
    expect(note.textContent).toContain('cannot override');
    expect(document.getElementById('settings-feedback-exploration-provenance')).toBeNull();
  });

  it('skips the feedback-exploration PUT on Save while the env kill is active', async () => {
    const calls = stubFetch({ feedback_exploration_enabled: false, source: 'env-kill', env_kill_active: true });
    await renderTab();
    await vi.waitFor(() => expect(checkbox().disabled).toBe(true));
    await clickSave();
    await vi.waitFor(() =>
      expect(calls.some((c) => c.url === '/api/admin/settings' && c.method === 'PUT')).toBe(true),
    );
    expect(
      calls.filter((c) => c.url === '/api/admin/feedback-exploration' && c.method !== 'GET'),
    ).toHaveLength(0);
  });

  it('shows the admin-flag provenance hint and keeps the checkbox live without a kill', async () => {
    stubFetch({ feedback_exploration_enabled: true, source: 'admin-flag', env_kill_active: false });
    await renderTab();
    await vi.waitFor(() => expect(checkbox().checked).toBe(true));
    expect(checkbox().disabled).toBe(false);
    expect(document.getElementById('settings-feedback-exploration-provenance')).toBeTruthy();
    expect(document.getElementById('settings-feedback-exploration-kill-note')).toBeNull();
  });

  it('sends the PUT with the toggled value when Save is clicked', async () => {
    const state = { feedback_exploration_enabled: false, source: 'default', env_kill_active: false };
    const calls = stubFetch(state);
    await renderTab(state);
    expect(checkbox().disabled).toBe(false);
    expect(checkbox().checked).toBe(false);
    // default source: no provenance hint, no kill note
    expect(document.getElementById('settings-feedback-exploration-provenance')).toBeNull();
    expect(document.getElementById('settings-feedback-exploration-kill-note')).toBeNull();

    await act(async () => {
      checkbox().click();
      await Promise.resolve();
    });
    expect(checkbox().checked).toBe(true);
    await clickSave();
    await vi.waitFor(() => {
      const put = calls.find(
        (c) => c.url === '/api/admin/feedback-exploration' && c.method === 'PUT',
      );
      expect(put).toBeTruthy();
      expect(put!.body).toEqual({ feedback_exploration_enabled: true });
    });
  });
});
