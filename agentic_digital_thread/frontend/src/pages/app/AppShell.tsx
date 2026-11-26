// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* AppShell — the app chrome: header (logo, nav,
 * user-menu), hash-driven view switching, the four view containers, and the
 * brand footer. Element ids and classes are CONTRACTUAL (e2e suite + ported
 * CSS): #brand-logo #dashboard-btn #architecture-btn #threads-btn #onboard-btn
 * #demo-btn, and #<view>-view containers with `.view.active`
 * semantics. The remaining header controls (#lang-select #user-display
 * #admin-link #logout-btn #theme-btn) live in the consolidated UserMenu
 * dropdown (see UserMenu.tsx).
 *
 * Task 3.2 integration pass — view bodies are mounted here with the old
 * lazy-init-on-first-activation semantics (dashboard.js switchView): a view's
 * component mounts on its FIRST activation and stays mounted afterwards, so
 * per-view state (cytoscape graphs, fleet data, wizard progress) survives
 * switches exactly like the old always-in-DOM markup. Dashboard/Architecture
 * additionally take `active` to (dis)connect their SSE / rebuild the system
 * map per activation.
 *
 * Composition: the view/activation controller lives in useViewRouting (view
 * state, the visited set, switchView, window.switchView, hashchange, and the
 * boot fresh-install redirect); this file is the presentational chrome plus the
 * load-time global registrations the old <script> tags performed.
 * The shell still owns:
 * - the demo gate for #demo-btn (/api/demo/status, fail-safe hidden);
 * - load-time registrations: refreshSuggestions (onboard.js:318), the thread.js
 *   bare globals, and the /api/version badge fill (architecture.js init).
 *
 * Shell reference queries delegate JSON transport to requestJson in
 * api/reference-queries.ts; AppShell never bypasses that checked layer. */
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useRef } from 'react';

import { ApiError } from '../../api/client';
import { useDemoStatusQuery } from '../../api/reference-queries';
import { fetchSystems } from '../../api/systems-query';
import { ChatSidebar } from '../../chat';
import { useBranding } from '../../contexts/BrandingContext';
// Design C: start the attendee's plant clock once on first sign-in (live only).
import { useAutoPlaceClock } from '../../features/workshop/useAutoPlaceClock';
import { VIEWS, type ViewName } from '../../lib/hash-view';
import { useI18n } from '../../lib/i18n';
import { fillVersionBadge } from '../../lib/version-badge';
import { ConfirmHost } from '../../shared/ui/ConfirmHost';
import { ToastRegion } from '../../shared/ui/ToastRegion';
import { ViewErrorBoundary } from '../../shared/ui/ViewErrorBoundary';
import {
  closeDataModal,
  DataModal,
  openDataModal,
  saveChatModalAsWord,
  showToast,
  ThreadPanel,
} from '../../shared/thread';
import type { DataModalExtras } from '../../shared/thread/data-modal';
import { DashboardView } from '../../views/dashboard';
// Shared persona-suggestions builder (old onboard.js refreshSuggestions) —
// registered on window and run once at boot, like the old script load did.
import { refreshSuggestions } from '../../views/onboard/chat';
// Consolidated top-right header controls: language, theme, admin
// link, sign-out — behind the labeled #user-menu-trigger dropdown.
import NotificationBell from './NotificationBell';
// Read-only workshop plant clock (ADR-0091) — self-gating, renders nothing off-live.
import PlantClockChip from './PlantClockChip';
import UserMenu from './UserMenu';
import { useViewRouting } from './useViewRouting';

const LazyArchitectureView = lazy(() =>
  import('../../views/architecture').then((module) => ({ default: module.ArchitectureView })),
);
const LazyOnboardView = lazy(() =>
  import('../../views/onboard').then((module) => ({ default: module.OnboardView })),
);

function ViewSkeleton({ label }: { label: string }) {
  return (
    <div className="view-loading skeleton-shimmer" role="status" aria-label={`Loading ${label}`}>
      <span>Loading {label}…</span>
    </div>
  );
}

/** `.view.active` semantics from switchView() — CONTRACTUAL class names. */
function viewClass(view: ViewName, self: ViewName): string {
  return `view${view === self ? ' active view-enter' : ''}`;
}

/** Load-time registrations the old <script> tags performed on every page load:
 * persona suggestions (onboard.js:318), the thread.js bare globals
 * (openDataModal/showToast/... — consumed via guarded window lookups by
 * inv-graph node clicks, chat cite badges, SessionMenu toasts, and the chat
 * modal's Word export), and the #version-badge fill (architecture.js init —
 * ArchitectureView repeats it on mount, idempotent). */
function useShellGlobals(queryClient: QueryClient): void {
  useEffect(() => {
    window.refreshSuggestions = refreshSuggestions;
    window.openDataModal = (title?: string, detail?: string, extras?: DataModalExtras) =>
      openDataModal(title ?? '', detail ?? '', extras);
    window.closeDataModal = closeDataModal;
    window.showToast = showToast;
    window.saveChatModalAsWord = saveChatModalAsWord;
    void refreshSuggestions();
    fillVersionBadge(queryClient);
    // Warm the system catalog on every view (not just the dashboard), so the
    // chat and the other surfaces name systems by their catalog display name
    // (lib/system-names.ts). Same query key as the dashboard: one request.
    fetchSystems(queryClient).catch(() => {});
  }, [queryClient]);
}

/** Demo status is shared and periodically refreshed by Query. Unknown/error
 * remains fail-safe hidden; errors are reported once without changing view. */
function useDemoGate(): boolean {
  const demoStatus = useDemoStatusQuery();
  const errorShown = useRef(false);
  const { error, isError } = demoStatus;

  useEffect(() => {
    if (!isError || errorShown.current) return;
    errorShown.current = true;
    const detail =
      error instanceof ApiError
        ? error.detail
        : error instanceof Error
          ? error.message
          : String(error);
    showToast('Demo controls unavailable: ' + detail);
  }, [error, isError]);

  return demoStatus.data?.enabled === true;
}

/** #demo-btn — gated by /api/demo/status (shell state). handleDemoBtn is
 * registered by the dashboard port's registerDemoGlobals(); the old markup used
 * inline onmousedown="handleDemoBtn(event)". */
function DemoButton({ enabled }: { enabled: boolean }) {
  return (
    <button
      id="demo-btn"
      onMouseDown={(e) => {
        (window as unknown as { handleDemoBtn?: (ev: Event) => void }).handleDemoBtn?.(
          e.nativeEvent,
        );
      }}
      style={{
        display: enabled ? '' : 'none',
        background: 'var(--accent)',
        // Match the shared Send/button treatment (.btn-send): var(--bg)
        // text — white in light mode, dark in dark mode.
        color: 'var(--bg)',
        fontWeight: 600,
        borderRadius: 4,
        padding: '0.25rem 0.75rem',
        marginRight: '0.75rem',
      }}
    >
      ▶ Demo
    </button>
  );
}

interface AppHeaderProps {
  view: ViewName;
  switchView: (next: ViewName) => void;
  demoEnabled: boolean;
  company: string | undefined;
  brandingLoaded: boolean;
}

function AppHeader({ view, switchView, demoEnabled, company, brandingLoaded }: AppHeaderProps) {
  // Chrome i18n: nav labels render via t(`nav.<view>`) — the same
  // keys the legacy index.html marked with data-i18n; re-renders on setLang.
  const { t } = useI18n();
  return (
    <header>
      <div
        className="logo"
        id="brand-logo"
        onClick={() => switchView('dashboard')}
        style={{ cursor: 'pointer', visibility: brandingLoaded ? 'visible' : 'hidden' }}
      >
        {company ? company.toUpperCase() : 'AGENTIC DIGITAL THREAD'}
      </div>
      <div className="nav">
        {VIEWS.map((v) => (
          <button
            key={v}
            id={`${v}-btn`}
            className={view === v ? 'active' : ''}
            aria-current={view === v ? 'page' : undefined}
            onClick={() => switchView(v)}
          >
            {t(`nav.${v}`)}
          </button>
        ))}
      </div>
      <div className="user-menu" id="user-menu">
        <DemoButton enabled={demoEnabled} />
        {/* Consolidated dropdown: #user-menu-trigger +
            #user-dropdown carry the language select, theme toggle, admin
            link and sign-out. Demo stays outside as in the spec. */}
        <PlantClockChip />
        <NotificationBell />
        <UserMenu />
      </div>
    </header>
  );
}

/* Dashboard MARKUP mounts EAGERLY: in the original all of its markup is static
 * in index.html (attached from first paint even when the boot lands on Onboard),
 * and architecture.js's DOMContentLoaded calls switchView('dashboard') before
 * the fresh-install status check resolves. The e2e suite relies on
 * "#dashboard-view visible ⇒ skeleton attached" (#ops-summary-card,
 * #entity-sections, #ls-systems-badge, #quality-by-line-panel) and on boot-time
 * initDiagMode (?diag=1 → body.diag at first paint).
 *
 * The RECURRING work behind `active` is deferred to a PROVEN first activation
 * (2026-08-24 single-task CPU saturation) — see useViewRouting. A session that
 * only passes through `/` en route to admin.html or a deep link never connects
 * the SSE (which schedules seven panel reads), observes systems, loads pinned
 * cards or arms the briefing turn. The node is never unmounted, so state and
 * every contractual id survive.
 *
 * Threads is imported eagerly (shared/thread is already in the entry chunk for
 * DataModal); the `visited` gate defers MOUNT (and thus data fetching) to first
 * activation. The index Agents view moved to the admin page only (same shared
 * AgentsFleet component). */
interface LeftPanelProps {
  view: ViewName;
  visited: ReadonlySet<ViewName>;
  switchView: (next: ViewName) => void;
}

function LeftPanel({ view, visited, switchView }: LeftPanelProps) {
  return (
    <div className="left-panel">
      <div id="dashboard-view" className={viewClass(view, 'dashboard')}>
        <DashboardView active={view === 'dashboard' && visited.has('dashboard')} />
      </div>
      <div id="threads-view" className={viewClass(view, 'threads')}>
        {visited.has('threads') && (
          <ViewErrorBoundary viewName="Threads">
            <ThreadPanel onNavigateOnboard={() => switchView('onboard')} />
          </ViewErrorBoundary>
        )}
      </div>
      <div id="architecture-view" className={viewClass(view, 'architecture')}>
        {visited.has('architecture') && (
          <ViewErrorBoundary viewName="Architecture">
            <Suspense fallback={<ViewSkeleton label="Architecture" />}>
              <LazyArchitectureView active={view === 'architecture'} />
            </Suspense>
          </ViewErrorBoundary>
        )}
      </div>
      <div id="onboard-view" className={viewClass(view, 'onboard')}>
        {visited.has('onboard') && (
          <ViewErrorBoundary viewName="Onboard">
            <Suspense fallback={<ViewSkeleton label="Onboard" />}>
              <LazyOnboardView />
            </Suspense>
          </ViewErrorBoundary>
        )}
      </div>
    </div>
  );
}

function BrandFooter({
  company,
  tagline,
  brandingLoaded,
}: {
  company: string | undefined;
  tagline: string | undefined;
  brandingLoaded: boolean;
}) {
  return (
    <footer id="brand-footer" style={{ visibility: brandingLoaded ? 'visible' : 'hidden' }}>
      {company ? `${company} — ${tagline ?? ''}` : 'Agentic Digital Thread — Manufacturing Intelligence'}
      <span
        id="version-badge"
        style={{
          marginLeft: '1rem',
          fontSize: '0.65rem',
          color: 'var(--muted)',
          fontFamily: "'IBM Plex Mono',monospace",
          opacity: 0.6,
        }}
      >
        v...
      </span>
    </footer>
  );
}

export default function AppShell() {
  const { view, visited, switchView } = useViewRouting();
  const demoEnabled = useDemoGate();
  const { branding, loaded: brandingLoaded } = useBranding();
  useShellGlobals(useQueryClient());
  useAutoPlaceClock();

  const company = branding?.company;

  return (
    <>
      <AppHeader
        view={view}
        switchView={switchView}
        demoEnabled={demoEnabled}
        company={company}
        brandingLoaded={brandingLoaded}
      />

      <div className="main">
        <LeftPanel view={view} visited={visited} switchView={switchView} />
        {/* Chat surface (#resize-handle / #chat-sidebar / #memory-sidebar /
            .sidebar-toggles + pop-out modal) — mounted once at shell level. */}
        <ChatSidebar />
      </div>

      {/* Data detail modal + toast host (thread.js) — mounted once; ported
          dashboard/chat modules call the imperative openDataModal() API.
          ConfirmHost renders askConfirm() prompts (shared/ui/confirm-store). */}
      <DataModal />
      <ToastRegion />
      <ConfirmHost />

      <BrandFooter
        company={company}
        tagline={branding?.tagline}
        brandingLoaded={brandingLoaded}
      />
    </>
  );
}
