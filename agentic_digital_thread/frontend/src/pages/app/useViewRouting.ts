// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* useViewRouting — the shell's view/activation controller, extracted from
 * AppShell so the chrome components stay presentational.
 *
 * It owns THREE things and nothing else:
 *  - which view is shown (`view`), hash-driven (dashboard.js:14-16 / 59-102);
 *  - which views have been REACHED (`visited`) — the lazy-init set that mounts
 *    a view body on first activation and keeps it mounted afterwards, so
 *    per-view state survives switches like the old always-in-DOM markup;
 *  - the boot fresh-install redirect (dashboard.js:9-26).
 *
 * The dashboard is special in one way only: its MARKUP mounts eagerly (see
 * AppShell), but its entry in `visited` still gates the `active` prop, i.e. the
 * recurring SSE/panel/briefing cost. A no-hash boot therefore starts INACTIVE
 * until the shared onboarding-status query proves the dashboard is the landing
 * view — a session that only passes through `/` en route to admin.html or a
 * deep link never pays it (2026-08-24 single-task CPU saturation).
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../../api/client';
import { useOnboardStatusQuery } from '../../api/reference-queries';
import { isViewName, readHashView, writeHashView, type ViewName } from '../../lib/hash-view';
import { showToast } from '../../shared/thread';

export interface ViewRouting {
  /** The shown view — drives `.view.active` and the nav button state. */
  view: ViewName;
  /** Views that have been REACHED; gates lazy mounts and dashboard `active`. */
  visited: ReadonlySet<ViewName>;
  /** switchView (dashboard.js:59-102) — also exposed as window.switchView. */
  switchView: (next: ViewName) => void;
}

/** Human-readable detail for a failed reference query, without leaking shapes. */
function toastDetail(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** Mirror the chat-sidebar auto-collapse on Onboard / expand on Dashboard
 * (dashboard.js switchView tail) — routed through the ChatSidebar-owned toggle
 * so its React state stays the single source of truth. */
function syncChatSidebar(next: ViewName): void {
  const sidebar = document.getElementById('chat-sidebar');
  if (!sidebar) return;
  const collapsed = sidebar.classList.contains('collapsed');
  if (next === 'onboard' && !collapsed) window.toggleChatSidebar?.();
  else if (next === 'dashboard' && collapsed) window.toggleChatSidebar?.();
}

/** The boot fresh-install redirect AND the default-landing activation release.
 *
 * Driven only by a RESOLVED shared status query: a boot hash always wins, and a
 * later user-selected hash is never overwritten. On a no-hash boot the dashboard
 * renders (skeleton attached, every contractual id intact) but stays INACTIVE
 * until this resolves, because until then the shell does not know whether it is
 * about to redirect to Onboard. A status ERROR fails OPEN to the existing
 * dashboard landing, so availability is unchanged. */
function useBootLanding(
  bootHash: ViewName | null,
  markVisited: (next: ViewName) => void,
  switchView: (next: ViewName) => void,
): void {
  const status = useOnboardStatusQuery();
  const handled = useRef(false);
  const errorShown = useRef(false);
  const { data, error, isError, isSuccess } = status;

  useEffect(() => {
    if (handled.current) return;
    if (bootHash || readHashView()) {
      handled.current = true;
      return;
    }
    if (isSuccess) {
      handled.current = true;
      if (data?.setup_complete === false) switchView('onboard');
      else markVisited('dashboard');
      return;
    }
    if (isError) {
      handled.current = true;
      markVisited('dashboard');
      if (errorShown.current) return;
      errorShown.current = true;
      // /api/onboard/status is integrator/Admin-gated: a 403 is the EXPECTED
      // answer for every ordinary signed-in user (live-found 2026-09-25 on the
      // first external-IdP sign-in), not a failure. Land on the dashboard
      // silently — the same rule PromptLibrary applies to the same endpoint.
      if (error instanceof ApiError && error.status === 403) return;
      showToast('Could not check onboarding status: ' + toastDetail(error));
    }
  }, [bootHash, data, error, isError, isSuccess, markVisited, switchView]);
}

/** Fragment navigation (e.g. the bell's `/#onboard/questions` link): an in-page
 * anchor click fires hashchange (writeHashView's replaceState never does, so
 * this cannot loop). Activate the encoded view WITHOUT rewriting the hash — a
 * sub-path like `onboard/questions` must survive for the in-view surface that
 * reads it (lib/hash-view.ts readHashSubview). */
function useHashNavigation(
  setView: (next: ViewName) => void,
  markVisited: (next: ViewName) => void,
): void {
  useEffect(() => {
    const onHashChange = () => {
      const v = readHashView();
      if (!v) return;
      setView(v);
      markVisited(v);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, [markVisited, setView]);
}

export function useViewRouting(): ViewRouting {
  const [view, setView] = useState<ViewName>(() => readHashView() ?? 'dashboard');
  const [visited, setVisited] = useState<ReadonlySet<ViewName>>(() => {
    const h = readHashView();
    return new Set<ViewName>(h ? [h] : []);
  });
  const bootHash = useRef(readHashView()).current;

  // Mark a view as reached: mounts a lazy body and, for the dashboard, releases
  // its recurring work. Idempotent and identity-stable so a repeat call causes
  // no re-render (and never remounts an already-mounted view body).
  const markVisited = useCallback((next: ViewName) => {
    setVisited((prev) => (prev.has(next) ? prev : new Set(prev).add(next)));
  }, []);

  const switchView = useCallback(
    (next: ViewName) => {
      setView(next);
      markVisited(next);
      writeHashView(next);
      syncChatSidebar(next);
    },
    [markVisited],
  );

  // The cross-module contract: architecture proposal cards, dashboard
  // chips/heroes and injected intelligence-summary links all call
  // window.switchView('<view>') (guarded lookups / inline onclick strings).
  useEffect(() => {
    window.switchView = (v: string) => {
      if (isViewName(v)) switchView(v);
    };
  }, [switchView]);

  useHashNavigation(setView, markVisited);
  useBootLanding(bootHash, markVisited, switchView);

  return { view, visited, switchView };
}
