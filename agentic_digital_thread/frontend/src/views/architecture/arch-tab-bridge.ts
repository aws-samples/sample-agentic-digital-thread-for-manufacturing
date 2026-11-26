// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Cross-module arch-tab request bridge.
 *
 * `window.switchArchTab` is registered by ArchitectureView's mount effect —
 * but views lazy-mount on first activation (AppShell `visited` set), so a
 * SYNCHRONOUS call right after `switchView('architecture')` (dashboard demo
 * mode) finds the global undefined on the first visit and silently no-ops
 * (in the retired legacy UI, a classic script, switchArchTab existed from
 * page load). This tiny dependency-free module stashes the requested tab;
 * ArchitectureView consumes it at the end of its mount effect. */

let pendingTab: string | null = null;
let pendingFocusTab: string | null = null;

function archViewMounted(): boolean {
  return typeof (window as unknown as { switchArchTab?: unknown }).switchArchTab === 'function';
}

/** Switch the Architecture tab now if the view is mounted, else stash the
 * request for ArchitectureView's mount effect to apply. */
export function requestArchTab(tab: string): void {
  const fn = (window as unknown as { switchArchTab?: (t: string) => void }).switchArchTab;
  if (typeof fn === 'function') fn(tab);
  else pendingTab = tab;
}

/** Mount-time drain — returns the stashed tab (if any) exactly once. */
export function consumePendingArchTab(): string | null {
  const t = pendingTab;
  pendingTab = null;
  return t;
}

/** Focus an Architecture tab button one frame from now. The frame lets a
 * closing dialog restore its own focus first (useDialogFocus cleanup) and
 * the view switch commit, so this focus is the one that sticks. */
export function focusArchTabSoon(tab: string): void {
  requestAnimationFrame(() => {
    Array.from(document.querySelectorAll<HTMLElement>('.arch-tab[data-arch-tab]'))
      .find((btn) => btn.dataset.archTab === tab)
      ?.focus();
  });
}

/** Move keyboard focus to an Architecture tab: now (one frame) if the view
 * is mounted, else on ArchitectureView's mount. For a caller that just hid
 * the control that held focus (the chat's investigation button). */
export function requestArchTabFocus(tab: string): void {
  if (archViewMounted()) focusArchTabSoon(tab);
  else pendingFocusTab = tab;
}

/** Mount-time drain for requestArchTabFocus — returns it exactly once. */
export function consumePendingArchTabFocus(): string | null {
  const t = pendingFocusTab;
  pendingFocusTab = null;
  return t;
}
