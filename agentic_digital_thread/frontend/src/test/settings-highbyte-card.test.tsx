// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Settings tab: the HighByte MCP Token card follows /api/admin/quick-links.
 *
 * - no HighByte URL (a deployment without HighByte seeds the "placeholder"
 *   sentinel, which the server normalizes to "") hides the card;
 * - a HighByte URL shows it, and a configured token says so;
 * - a quick-links failure or a still-loading quick-links keeps the card, so an
 *   outage never removes the form;
 * - the LLM section carries no region-prefix hint (models come from a list). */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { BrandingProvider } from '../contexts/BrandingContext';
import SettingsTab from '../pages/admin/tabs/SettingsTab';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MODELS = [{ id: 'model-a', label: 'Model A' }];

let quickLinks: { status: number; body: unknown } | 'pending';

function getResponses(): Record<string, unknown> {
  return {
    '/api/admin/settings': { status: 'ok', demo_enabled: false },
    '/api/admin/models': { orchestrator: MODELS, agent: MODELS },
    '/api/admin/feedback-exploration': {
      feedback_exploration_enabled: false,
      source: 'default',
      env_kill_active: false,
    },
    '/api/admin/live-updates': { live_updates_enabled: false, interval_seconds: 120 },
    '/api/admin/llm-settings': { orchestrator_model: 'model-a', agent_model: 'model-a' },
    '/api/admin/concept-packs': { packs: [] },
  };
}

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Error',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

let container: HTMLDivElement;
let root: Root;
let consoleErrors: string[];

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  consoleErrors = [];
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    consoleErrors.push(args.map(String).join(' '));
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (method !== 'GET') throw new Error(`unexpected ${method}: ${url}`);
      if (url === '/api/admin/quick-links') {
        if (quickLinks === 'pending') return new Promise<Response>(() => undefined);
        return response(quickLinks.body, quickLinks.status);
      }
      const responses = getResponses();
      return response(url in responses ? responses[url] : {});
    }),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // Every render below settles its queries inside act(), and nothing in this
  // file should log an error at all (an act warning included).
  expect(consoleErrors).toEqual([]);
});

/** Render, then let the queries resolve inside act() (one macrotask per round,
 * so every state update lands inside an act scope) until only `stillFetching`
 * queries are in flight and the form has hydrated. */
async function renderTab(client = createQueryClient(), stillFetching = 0) {
  client.setQueryData(queryKeys.branding(), { company: 'TestCo', tagline: 'Tagline' });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <BrandingProvider updateTitle={false}>
          <SettingsTab onDemoEnabledChange={() => undefined} onBrandingSaved={() => undefined} />
        </BrandingProvider>
      </QueryClientProvider>,
    );
  });
  const settled = () =>
    (document.getElementById('settings-orch-model') as HTMLSelectElement | null)?.value === 'model-a' &&
    client.isFetching() === stillFetching;
  for (let round = 0; round < 50 && !settled(); round += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
  expect(settled()).toBe(true);
}

const tokenInput = () => document.getElementById('settings-highbyte-token');

describe('HighByte MCP Token card', () => {
  it('is hidden when the deployment has no HighByte', async () => {
    quickLinks = { status: 200, body: { highbyte: '', highbyte_i3x_token: false } };
    await renderTab();
    await vi.waitFor(() => expect(tokenInput()).toBeNull());
    expect(container.textContent).not.toContain('HighByte MCP Token');
  });

  it('is shown when HighByte is deployed, and says when a token is already set', async () => {
    quickLinks = {
      status: 200,
      body: { highbyte: 'https://highbyte.example.com', highbyte_i3x_token: true },
    };
    await renderTab();
    await vi.waitFor(() =>
      expect(document.getElementById('settings-highbyte-token-set')?.textContent).toBe(
        'A token is already set.',
      ),
    );
    expect(tokenInput()).not.toBeNull();
  });

  it('has no "already set" note when HighByte is deployed without a token', async () => {
    quickLinks = { status: 200, body: { highbyte: 'https://highbyte.example.com', highbyte_i3x_token: false } };
    await renderTab();
    await vi.waitFor(() => expect(tokenInput()).not.toBeNull());
    expect(document.getElementById('settings-highbyte-token-set')).toBeNull();
  });

  it('stays visible when quick-links fails', async () => {
    quickLinks = { status: 500, body: { detail: 'boom' } };
    const client = createQueryClient();
    client.setDefaultOptions({ queries: { retry: false } });
    await renderTab(client);
    expect(client.getQueryState(queryKeys.admin.quickLinks())?.status).toBe('error');
    expect(tokenInput()).not.toBeNull();
  });

  it('stays visible while quick-links is still loading', async () => {
    quickLinks = 'pending';
    const client = createQueryClient();
    await renderTab(client, 1);
    expect(client.getQueryState(queryKeys.admin.quickLinks())?.status).toBe('pending');
    expect(tokenInput()).not.toBeNull();
  });
});

describe('LLM Configuration copy', () => {
  it('drops the region-prefix hint and describes how a save applies', async () => {
    quickLinks = { status: 200, body: { highbyte: '' } };
    await renderTab();
    expect(container.textContent).not.toContain('region-prefixed');
    expect(container.textContent).toContain(
      'Saving switches the running agents over; an orchestrator change takes about a minute.',
    );
  });
});
