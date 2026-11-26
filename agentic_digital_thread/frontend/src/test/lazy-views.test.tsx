// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import AppShell from '../pages/app/AppShell';

type OnboardStatus = {
  data?: { setup_complete: boolean };
  error: unknown;
  isError: boolean;
  isSuccess: boolean;
};

const PENDING: OnboardStatus = { data: undefined, error: null, isError: false, isSuccess: false };
const COMPLETE: OnboardStatus = {
  data: { setup_complete: true },
  error: null,
  isError: false,
  isSuccess: true,
};
const INCOMPLETE: OnboardStatus = {
  data: { setup_complete: false },
  error: null,
  isError: false,
  isSuccess: true,
};
const FORBIDDEN: OnboardStatus = {
  data: undefined,
  error: new ApiError(403, '/api/onboard/status', 'Onboarding requires the integrator or Admin role'),
  isError: true,
  isSuccess: false,
};
const FAILED: OnboardStatus = {
  data: undefined,
  error: new Error('status unreachable'),
  isError: true,
  isSuccess: false,
};

const lazyState = vi.hoisted(() => {
  return {
    architectureThrows: false,
    onboardImports: 0,
    threadMounts: 0,
    dashboardMounts: 0,
    // Typed wide so a test can swap in pending / error / incomplete states.
    onboardStatus: {
      data: { setup_complete: true },
      error: null,
      isError: false,
      isSuccess: true,
    } as OnboardStatus,
    showToast: (_message: string) => {},
  };
});

vi.mock('../api/reference-queries', () => ({
  useDemoStatusQuery: () => ({ data: { enabled: false }, isError: false }),
  demoStatusQueryOptions: () => ({
    queryKey: ['demo', 'status'],
    queryFn: async () => ({ enabled: false, workshop: false }),
  }),
  useOnboardStatusQuery: () => lazyState.onboardStatus,
}));
vi.mock('../chat', () => ({ ChatSidebar: () => <aside id="chat-sidebar" /> }));
vi.mock('../contexts/BrandingContext', () => ({
  useBranding: () => ({ branding: { company: 'Test' }, loaded: true }),
}));
vi.mock('../lib/i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }));
vi.mock('../lib/version-badge', () => ({ fillVersionBadge: vi.fn() }));
vi.mock('../pages/app/UserMenu', () => ({ default: () => <div id="user-menu-stub" /> }));
vi.mock('../shared/thread', () => ({
  closeDataModal: vi.fn(),
  DataModal: () => null,
  openDataModal: vi.fn(),
  saveChatModalAsWord: vi.fn(),
  showToast: (message: string) => lazyState.showToast(message),
  ThreadPanel: () => {
    lazyState.threadMounts += 1;
    return <div data-testid="threads-ready">Threads ready</div>;
  },
}));
vi.mock('../shared/ui/ToastRegion', () => ({ ToastRegion: () => null }));
vi.mock('../views/architecture', () => ({
  ArchitectureView: () => {
    if (lazyState.architectureThrows) throw new Error('architecture chunk failed');
    return <div data-testid="architecture-ready">Architecture ready</div>;
  },
}));
vi.mock('../views/dashboard', () => ({
  // The eagerly mounted skeleton is CONTRACTUAL; only the `active` prop (which
  // gates the SSE/panel/briefing work) is expected to change.
  DashboardView: ({ active }: { active?: boolean }) => {
    lazyState.dashboardMounts += 1;
    return (
      <div data-testid="dashboard-eager" data-active={String(active === true)}>
        Dashboard eager
      </div>
    );
  },
}));
vi.mock('../views/onboard', () => {
  lazyState.onboardImports += 1;
  return { OnboardView: () => <div>Onboard ready</div> };
});
vi.mock('../views/onboard/chat', () => ({ refreshSuggestions: vi.fn(async () => {}) }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let client: QueryClient;

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  lazyState.architectureThrows = false;
  lazyState.dashboardMounts = 0;
  lazyState.threadMounts = 0;
  lazyState.onboardStatus = COMPLETE;
  lazyState.showToast = () => {};
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

/** Render (or re-render, preserving shell state) with the CURRENT stub status. */
function renderShell() {
  act(() => {
    root.render(
      <QueryClientProvider client={client}>
        <AppShell />
      </QueryClientProvider>,
    );
  });
}

function dashboardNode(): HTMLElement | null {
  return container.querySelector('[data-testid="dashboard-eager"]');
}

function dashboardActive(): string | null | undefined {
  return dashboardNode()?.getAttribute('data-active');
}

function clickNav(view: 'dashboard' | 'threads' | 'architecture' | 'onboard') {
  act(() => {
    (container.querySelector(`#${view}-btn`) as HTMLButtonElement).click();
  });
}

describe('<AppShell/> lazy views', () => {
  it('keeps Dashboard eager, defers Threads mount to first visit, preserves visits, and isolates view errors', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderShell();

    expect(dashboardNode()).not.toBeNull();
    expect(lazyState.threadMounts).toBe(0);
    expect(lazyState.onboardImports).toBe(0);

    clickNav('threads');
    expect(lazyState.threadMounts).toBe(1);
    expect(container.querySelector('[data-testid="threads-ready"]')).not.toBeNull();

    clickNav('dashboard');
    expect(container.querySelector('[data-testid="threads-ready"]')).not.toBeNull();
    expect(container.querySelector('#threads-view')?.classList.contains('active')).toBe(false);

    lazyState.architectureThrows = true;
    clickNav('architecture');
    await vi.waitFor(() => {
      expect(container.querySelector('[aria-label="Architecture unavailable"]')).not.toBeNull();
    });
    expect(dashboardNode()).not.toBeNull();

    lazyState.architectureThrows = false;
    await act(async () => {
      (container.querySelector('.view-error-actions button') as HTMLButtonElement).click();
    });
    expect(container.querySelector('[data-testid="architecture-ready"]')).not.toBeNull();
    expect(consoleError).toHaveBeenCalled();
  });
});

/* Dashboard boot cost follows a PROVEN first activation (2026-08-24 CPU
 * saturation): the skeleton still mounts eagerly — every contractual id and the
 * `#dashboard-view visible ⇒ skeleton attached` e2e contract are untouched —
 * but `active` (which arms the SSE connection, the seven panel loaders and the
 * briefing turn) stays false until the shell KNOWS the dashboard is the shown
 * view. A browser test that logs in at `/` and immediately navigates to
 * admin.html or `#onboard/questions` therefore pays none of it. */
describe('<AppShell/> dashboard activation', () => {
  it('renders the dashboard skeleton INACTIVE while the boot status is unresolved', () => {
    lazyState.onboardStatus = PENDING;
    renderShell();

    expect(dashboardNode()).not.toBeNull();
    expect(container.querySelector('#dashboard-view')?.classList.contains('active')).toBe(true);
    expect(dashboardActive()).toBe('false');
  });

  it('activates the dashboard once the boot status confirms the default landing', () => {
    lazyState.onboardStatus = PENDING;
    renderShell();
    expect(dashboardActive()).toBe('false');

    lazyState.onboardStatus = COMPLETE;
    renderShell();

    expect(dashboardActive()).toBe('true');
  });

  it('activates the dashboard when the boot status FAILS (availability fail-open) and reports once', () => {
    const toasts: string[] = [];
    lazyState.showToast = (message) => toasts.push(message);
    lazyState.onboardStatus = FAILED;
    renderShell();

    expect(dashboardActive()).toBe('true');
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toContain('Could not check onboarding status');
  });

  it('activates the dashboard SILENTLY when the boot status is 403 (ordinary user, not a failure)', () => {
    const toasts: string[] = [];
    lazyState.showToast = (message) => toasts.push(message);
    lazyState.onboardStatus = FORBIDDEN;
    renderShell();

    expect(dashboardActive()).toBe('true');
    expect(toasts).toHaveLength(0);
  });

  it('never activates the dashboard when a boot hash selects another view', () => {
    window.history.replaceState({}, '', '/#onboard/questions');
    lazyState.onboardStatus = PENDING;
    renderShell();

    expect(dashboardNode()).not.toBeNull();
    expect(dashboardActive()).toBe('false');

    // Even after the shared status query resolves, a boot hash keeps ownership.
    lazyState.onboardStatus = COMPLETE;
    renderShell();
    expect(dashboardActive()).toBe('false');
    expect(container.querySelector('#dashboard-view')?.classList.contains('active')).toBe(false);
  });

  it('keeps the dashboard inactive when the boot status redirects to onboarding', () => {
    lazyState.onboardStatus = INCOMPLETE;
    renderShell();

    expect(container.querySelector('#onboard-view')?.classList.contains('active')).toBe(true);
    expect(dashboardActive()).toBe('false');
  });

  it('activates on an explicit user switch and keeps the SAME mounted node afterwards', () => {
    lazyState.onboardStatus = PENDING;
    renderShell();
    const before = dashboardNode();
    expect(dashboardActive()).toBe('false');

    clickNav('dashboard');
    expect(dashboardActive()).toBe('true');
    expect(dashboardNode()).toBe(before);

    clickNav('threads');
    expect(dashboardActive()).toBe('false');
    clickNav('dashboard');
    expect(dashboardActive()).toBe('true');
    // Still the ORIGINAL node — deferring activation must never remount the
    // dashboard (per-view state and the contractual skeleton survive).
    expect(dashboardNode()).toBe(before);
  });

  it('activates on a dashboard hashchange', () => {
    lazyState.onboardStatus = PENDING;
    renderShell();
    expect(dashboardActive()).toBe('false');

    act(() => {
      window.history.replaceState({}, '', '/#dashboard');
      window.dispatchEvent(new Event('hashchange'));
    });

    expect(dashboardActive()).toBe('true');
  });

  it('activates when window.switchView is called from a cross-module shim', () => {
    lazyState.onboardStatus = PENDING;
    renderShell();
    expect(dashboardActive()).toBe('false');

    act(() => {
      window.switchView?.('dashboard');
    });

    expect(dashboardActive()).toBe('true');
  });

  it('boots ACTIVE when the initial hash already selects the dashboard', () => {
    window.history.replaceState({}, '', '/#dashboard');
    lazyState.onboardStatus = PENDING;
    renderShell();

    expect(dashboardActive()).toBe('true');
  });
});
