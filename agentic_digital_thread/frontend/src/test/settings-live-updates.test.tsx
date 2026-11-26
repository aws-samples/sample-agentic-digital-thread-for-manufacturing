// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Settings tab — live-updates refresh interval.
 *
 * Pins:
 * - the interval select hydrates from GET /api/admin/live-updates
 *   (interval_seconds) and is enabled only while Enable Live Updates is on;
 * - Save sends the interval alongside the flag through the SAME route
 *   (POST body { live_updates_enabled, interval_seconds });
 * - a non-preset effective value (e.g. env-configured) renders as an
 *   "(current)" option instead of being silently snapped to a preset. */
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
  '/api/admin/feedback-exploration': {
    feedback_exploration_enabled: false,
    source: 'default',
    env_kill_active: false,
  },
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

function stubFetch(liveUpdatesState: Record<string, unknown>): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (method === 'GET') {
      if (url === '/api/admin/live-updates') return response(liveUpdatesState);
      if (url in GET_RESPONSES) return response(GET_RESPONSES[url]);
      throw new Error(`unexpected GET: ${url}`);
    }
    if (url === '/api/admin/settings') return response({ status: 'ok', demo_enabled: false });
    if (url === '/api/admin/live-updates') {
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
      return response({
        status: 'ok',
        live_updates_enabled: body.live_updates_enabled === true,
        interval_seconds: body.interval_seconds ?? 120,
      });
    }
    if (url === '/api/admin/branding') return response({ status: 'ok' });
    if (url === '/api/admin/feedback-exploration') {
      return response({
        status: 'ok',
        feedback_exploration_enabled: false,
        source: 'default',
        env_kill_active: false,
      });
    }
    throw new Error(`unexpected ${method}: ${url}`);
  }));
  return calls;
}

async function renderTab(liveUpdatesState: Record<string, unknown>) {
  const calls = stubFetch(liveUpdatesState);
  const client = createQueryClient();
  client.setQueryData(queryKeys.branding(), { company: 'TestCo', tagline: 'Tagline' });
  // Seed the cache (the hydrate-once idiom) so the mount-time effect runs
  // without racing the fetch.
  client.setQueryData(queryKeys.admin.liveUpdates(), liveUpdatesState);
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
    expect(document.getElementById('settings-live-updates-interval')).toBeTruthy(),
  );
  return calls;
}

function checkbox(): HTMLInputElement {
  return document.getElementById('settings-live-updates-enabled') as HTMLInputElement;
}

function intervalSelect(): HTMLSelectElement {
  return document.getElementById('settings-live-updates-interval') as HTMLSelectElement;
}

async function clickSave() {
  await act(async () => {
    document.getElementById('settings-save-btn')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

function setSelectValue(el: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

describe('settings live-updates interval', () => {
  it('hydrates the select from the effective interval and enables it while live updates are on', async () => {
    await renderTab({ status: 'ok', live_updates_enabled: true, interval_seconds: 300 });
    await vi.waitFor(() => expect(checkbox().checked).toBe(true));
    expect(intervalSelect().disabled).toBe(false);
    expect(intervalSelect().value).toBe('300');
  });

  it('disables the select while live updates are off', async () => {
    await renderTab({ status: 'ok', live_updates_enabled: false, interval_seconds: 120 });
    expect(checkbox().checked).toBe(false);
    expect(intervalSelect().disabled).toBe(true);
    // Turning the checkbox on re-enables the cadence choice.
    await act(async () => {
      checkbox().click();
      await Promise.resolve();
    });
    expect(intervalSelect().disabled).toBe(false);
  });

  it('sends the chosen interval alongside the flag through the same route on Save', async () => {
    const calls = await renderTab({ status: 'ok', live_updates_enabled: true, interval_seconds: 120 });
    await vi.waitFor(() => expect(checkbox().checked).toBe(true));
    setSelectValue(intervalSelect(), '900');
    await clickSave();
    await vi.waitFor(() => {
      const post = calls.find(
        (c) => c.url === '/api/admin/live-updates' && c.method === 'POST',
      );
      expect(post).toBeTruthy();
      expect(post!.body).toEqual({ live_updates_enabled: true, interval_seconds: 900 });
    });
  });

  it('renders a non-preset effective value as a "(current)" option', async () => {
    await renderTab({ status: 'ok', live_updates_enabled: true, interval_seconds: 45 });
    await vi.waitFor(() => expect(intervalSelect().value).toBe('45'));
    const labels = [...intervalSelect().options].map((o) => o.textContent);
    expect(labels).toContain('45s (current)');
    expect(labels).toContain('2m (default)');
  });
});
