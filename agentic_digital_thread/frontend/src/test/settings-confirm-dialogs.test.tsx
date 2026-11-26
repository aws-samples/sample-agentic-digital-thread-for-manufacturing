// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Settings tab — consequential actions ask through the app dialog
 * (askConfirm, shared/ui/confirm-store.ts), never the native confirm():
 *
 * - Save Models with a changed orchestrator model asks "Restart the
 *   orchestrator?"; declining sends nothing;
 * - a changed agent model asks "Change the agent model?" AFTER the
 *   orchestrator dialog, and only both confirms send the PUT;
 * - Uninstall on an installed concept pack asks with a danger-toned
 *   "Uninstall pack"; declining sends nothing, confirming POSTs uninstall. */
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import { BrandingProvider } from '../contexts/BrandingContext';
import SettingsTab from '../pages/admin/tabs/SettingsTab';
import { answerConfirm, confirmDialog, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const MODELS = [
  { id: 'model-a', label: 'Model A' },
  { id: 'model-b', label: 'Model B' },
];

const GET_RESPONSES: Record<string, unknown> = {
  '/api/admin/settings': { status: 'ok', demo_enabled: false },
  '/api/admin/models': { orchestrator: MODELS, agent: MODELS },
  '/api/admin/feedback-exploration': {
    feedback_exploration_enabled: false,
    source: 'default',
    env_kill_active: false,
  },
  '/api/admin/live-updates': { live_updates_enabled: false, interval_seconds: 120 },
  '/api/admin/llm-settings': { orchestrator_model: 'model-a', agent_model: 'model-a' },
  '/api/admin/concept-packs': {
    packs: [{ id: 'aerospace', name: 'Aerospace', is_installed: true, concepts_installed: 12 }],
  },
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

interface FetchCall {
  url: string;
  method: string;
}

let container: HTMLDivElement;
let root: Root;
let calls: FetchCall[];

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  calls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      calls.push({ url, method });
      if (method === 'GET') {
        if (url in GET_RESPONSES) return response(GET_RESPONSES[url]);
        return response({});
      }
      if (url === '/api/admin/llm-settings') {
        return response({
          status: 'ok',
          restart_required: true,
          reonboard_required: true,
          agent_runtime_refresh: { ok: ['erp'], failed: ['mes'], skipped: ['plm'] },
        });
      }
      if (url === '/api/admin/concept-packs/aerospace/uninstall') {
        return response({ status: 'ok', deleted: 10, preserved_due_to_mappings: 2 });
      }
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

async function renderTab() {
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
    expect((document.getElementById('settings-orch-model') as HTMLSelectElement).value).toBe('model-a');
    expect(document.querySelector('[data-pack-id="aerospace"]')).toBeTruthy();
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

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).click();
  });
}

const writes = () => calls.filter((c) => c.method !== 'GET');

describe('Save Models', () => {
  it('asks before restarting the orchestrator; declining sends nothing', async () => {
    const nativeConfirm = vi.fn(() => true);
    vi.stubGlobal('confirm', nativeConfirm);
    await renderTab();
    await choose('settings-orch-model', 'model-b');
    await click(document.getElementById('settings-llm-btn'));

    expect(confirmText()).toEqual({
      title: 'Restart the orchestrator?',
      body: 'The orchestrator runtime will be restarted. This takes about 60 seconds.',
      buttons: ['Cancel', 'Restart runtime'],
    });
    expect(confirmDialog()!.querySelector('.confirm-danger-btn')).toBeNull();
    await answerConfirm('cancel');
    expect(confirmDialog()).toBeNull();
    expect(writes()).toEqual([]);
    expect(nativeConfirm).not.toHaveBeenCalled();
  });

  it('asks about the agent model after the orchestrator, and saves only when both are confirmed', async () => {
    await renderTab();
    await choose('settings-orch-model', 'model-b');
    await choose('settings-agent-model', 'model-b');
    await click(document.getElementById('settings-llm-btn'));

    expect(confirmText().title).toBe('Restart the orchestrator?');
    await answerConfirm('confirm');
    expect(confirmText()).toEqual({
      title: 'Change the agent model?',
      body: "Each running system agent switches to the new model in place. If one can't be switched, you'll be asked to re-onboard it.",
      buttons: ['Cancel', 'Change agent model'],
    });
    expect(writes()).toEqual([]);
    await answerConfirm('confirm');

    await vi.waitFor(() =>
      expect(writes()).toEqual([{ url: '/api/admin/llm-settings', method: 'PUT' }]),
    );
    await vi.waitFor(() =>
      expect(document.getElementById('settings-llm-status')!.textContent).toContain('orchestrator restarting'),
    );
    // The systems the in-place switch could not move are named.
    expect(document.getElementById('settings-llm-status')!.textContent).toContain(
      're-onboard mes, plm for the new agent model',
    );
  });

  it('declining the agent-model dialog sends nothing even after confirming the restart', async () => {
    await renderTab();
    await choose('settings-orch-model', 'model-b');
    await choose('settings-agent-model', 'model-b');
    await click(document.getElementById('settings-llm-btn'));
    await answerConfirm('confirm');
    await answerConfirm('cancel');
    expect(confirmDialog()).toBeNull();
    expect(writes()).toEqual([]);
  });
});

describe('concept pack uninstall', () => {
  it('asks with a danger-toned "Uninstall pack"; declining sends nothing', async () => {
    await renderTab();
    await click(document.querySelector('[data-pack-id="aerospace"]'));
    expect(confirmText()).toEqual({
      title: 'Uninstall the aerospace concept pack?',
      body: 'Concepts that are mapped to system fields will be preserved.',
      buttons: ['Cancel', 'Uninstall pack'],
    });
    expect(confirmDialog()!.querySelector('.confirm-danger-btn')?.textContent).toBe('Uninstall pack');
    await answerConfirm('cancel');
    expect(writes()).toEqual([]);
  });

  it('confirming POSTs the uninstall and reports the preserved count', async () => {
    await renderTab();
    await click(document.querySelector('[data-pack-id="aerospace"]'));
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(writes()).toEqual([{ url: '/api/admin/concept-packs/aerospace/uninstall', method: 'POST' }]),
    );
    await vi.waitFor(() =>
      expect(document.getElementById('concept-packs-status')!.textContent).toContain('2 preserved'),
    );
  });
});
