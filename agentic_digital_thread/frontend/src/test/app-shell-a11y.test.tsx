// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../api/query-keys';
import AppShell from '../pages/app/AppShell';

const refreshSuggestionsMock = vi.hoisted(() => vi.fn(async () => {}));

vi.mock('../chat', () => ({ ChatSidebar: () => <aside id="chat-sidebar" /> }));
vi.mock('../pages/app/UserMenu', () => ({ default: () => <div id="user-menu-stub" /> }));
vi.mock('../shared/agents', () => ({ AgentsFleet: () => <div>Agents</div> }));
vi.mock('../shared/ui/ToastRegion', () => ({ ToastRegion: () => null }));
vi.mock('../shared/thread', () => ({
  closeDataModal: vi.fn(),
  DataModal: () => null,
  openDataModal: vi.fn(),
  saveChatModalAsWord: vi.fn(),
  showToast: vi.fn(),
  ThreadPanel: () => <div>Threads</div>,
}));
vi.mock('../views/architecture', () => ({ ArchitectureView: () => <div>Architecture</div> }));
vi.mock('../views/dashboard', () => ({ DashboardView: () => <div>Dashboard</div> }));
vi.mock('../views/onboard', () => ({ OnboardView: () => <div>Onboard</div> }));
vi.mock('../views/onboard/chat', () => ({ refreshSuggestions: refreshSuggestionsMock }));
vi.mock('../lib/i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }));
vi.mock('../lib/version-badge', () => ({ fillVersionBadge: vi.fn() }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

beforeEach(() => {
  window.history.replaceState({}, '', '/#architecture');
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/onboard/status') return jsonResponse({ setup_complete: true });
    if (url === '/api/demo/status') return jsonResponse({ enabled: false });
    throw new Error(`unexpected URL: ${url}`);
  }));
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

describe('<AppShell/> navigation accessibility', () => {
  it('marks exactly one current page as hash-driven views switch', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });
    await act(async () => {
      root.render(
        <QueryClientProvider client={client}>
          <AppShell />
        </QueryClientProvider>,
      );
    });
    await vi.waitFor(() => {
      expect(client.getQueryData(queryKeys.onboardStatus())).toEqual({ setup_complete: true });
    });

    const navButtons = Array.from(container.querySelectorAll<HTMLButtonElement>('.nav button'));
    expect(navButtons).toHaveLength(4);
    const expectCurrent = (buttonId: string, hash: string) => {
      const current = navButtons.filter((button) => button.getAttribute('aria-current') === 'page');
      expect(current).toHaveLength(1);
      expect(current[0].id).toBe(buttonId);
      expect(window.location.hash).toBe(hash);
      const activeView = container.querySelector<HTMLElement>('.view.active');
      expect(activeView?.classList.contains('view-enter')).toBe(true);
    };

    expectCurrent('architecture-btn', '#architecture');
    for (const view of ['dashboard', 'threads', 'onboard', 'architecture']) {
      await act(async () => {
        (container.querySelector(`#${view}-btn`) as HTMLButtonElement).click();
      });
      expectCurrent(`${view}-btn`, `#${view}`);
    }
  });
});
