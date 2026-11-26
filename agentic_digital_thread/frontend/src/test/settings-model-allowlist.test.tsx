// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Settings tab under a deployment model allowlist (ALLOWED_MODELS):
 *
 * - models the deployment does not offer are listed, greyed out (disabled);
 * - a configured model outside the allowlist is still the shown value, so
 *   the page reports what is actually configured;
 * - saving leaves that role unchanged ('' in the PUT) instead of sending an
 *   id the server would refuse;
 * - with no allowed choice on either role, nothing is sent. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { BrandingProvider } from '../contexts/BrandingContext';
import SettingsTab from '../pages/admin/tabs/SettingsTab';
import { answerConfirm, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const MODELS = [
  { id: 'model-a', label: 'Model A', allowed: true },
  { id: 'model-b', label: 'Model B', allowed: true },
  { id: 'model-c', label: 'Model C', allowed: false },
];

let llmSettings: { orchestrator_model: string; agent_model: string };

function getResponses(): Record<string, unknown> {
  return {
    '/api/admin/settings': { status: 'ok', demo_enabled: false },
    '/api/admin/models': { orchestrator: MODELS, agent: MODELS, allowlist_active: true },
    '/api/admin/feedback-exploration': {
      feedback_exploration_enabled: false,
      source: 'default',
      env_kill_active: false,
    },
    '/api/admin/live-updates': { live_updates_enabled: false, interval_seconds: 120 },
    '/api/admin/llm-settings': llmSettings,
    '/api/admin/concept-packs': { packs: [] },
  };
}

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

interface FetchCall {
  url: string;
  method: string;
  body?: unknown;
}

let container: HTMLDivElement;
let root: Root;
let calls: FetchCall[];

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  calls = [];
  llmSettings = { orchestrator_model: 'model-c', agent_model: 'model-a' };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
      calls.push({ url, method, body });
      if (method === 'GET') {
        const responses = getResponses();
        return response(url in responses ? responses[url] : {});
      }
      if (url === '/api/admin/llm-settings') return response({ status: 'ok' });
      throw new Error(`unexpected ${method}: ${url}`);
    }),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderTab(expectedOrch: string) {
  const client = createQueryClient();
  client.setQueryData(queryKeys.branding(), { company: 'TestCo', tagline: 'Tagline' });
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
  await vi.waitFor(() => {
    const select = document.getElementById('settings-orch-model') as HTMLSelectElement;
    expect(select.options.length).toBe(3);
    expect(select.value).toBe(expectedOrch);
  });
}

const setSelectValue = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!;

async function choose(id: string, value: string) {
  await act(async () => {
    const select = document.getElementById(id) as HTMLSelectElement;
    setSelectValue.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

async function save() {
  await act(async () => {
    (document.getElementById('settings-llm-btn') as HTMLElement).click();
  });
}

const writes = () => calls.filter((c) => c.method !== 'GET');

function option(selectId: string, value: string): HTMLOptionElement {
  const select = document.getElementById(selectId) as HTMLSelectElement;
  return Array.from(select.options).find((o) => o.value === value)!;
}

describe('LLM settings under a model allowlist', () => {
  it('greys out models not offered and still shows a disallowed configured model', async () => {
    await renderTab('model-c');
    for (const id of ['settings-orch-model', 'settings-agent-model']) {
      expect(option(id, 'model-c').disabled).toBe(true);
      expect(option(id, 'model-c').textContent).toBe('Model C (not offered in this deployment)');
      expect(option(id, 'model-a').disabled).toBe(false);
      expect(option(id, 'model-b').disabled).toBe(false);
    }
    expect(container.textContent).toContain('This deployment limits the models you can choose');
  });

  it('saving leaves the disallowed role unchanged and sends only the allowed choice', async () => {
    await renderTab('model-c');
    await choose('settings-agent-model', 'model-b');
    await save();
    // No orchestrator restart prompt: that role is not being changed.
    expect(confirmText().title).toBe('Change the agent model?');
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(writes()).toEqual([
        {
          url: '/api/admin/llm-settings',
          method: 'PUT',
          body: { orchestrator_model: '', agent_model: 'model-b' },
        },
      ]),
    );
  });

  it('picking an allowed orchestrator model replaces the disallowed one', async () => {
    await renderTab('model-c');
    await choose('settings-orch-model', 'model-a');
    await save();
    expect(confirmText().title).toBe('Restart the orchestrator?');
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(writes()).toEqual([
        {
          url: '/api/admin/llm-settings',
          method: 'PUT',
          body: { orchestrator_model: 'model-a', agent_model: 'model-a' },
        },
      ]),
    );
  });

  it('sends nothing when neither role has an allowed model selected', async () => {
    llmSettings = { orchestrator_model: 'model-c', agent_model: 'model-c' };
    await renderTab('model-c');
    await save();
    expect(writes()).toEqual([]);
    expect(document.getElementById('settings-llm-status')!.textContent).toBe(
      'Choose a model offered in this deployment',
    );
  });
});
