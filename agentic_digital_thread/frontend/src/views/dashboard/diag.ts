// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Diagnostic mode (S5.4 steering) — initDiagMode / toggleDiag.
 * body.diag reveals the per-card/panel "why didn't
 * this render" traces (.diag-note) and the presence-hidden panels
 * (.diag-reveal). Enabled by ?diag=1 (or #...&diag) or toggleDiag().
 *
 * `toggleDiag` MUST live on window: the e2e suite calls it via
 * page.evaluate('toggleDiag()'). Registered at module import time so it is
 * available as soon as the dashboard bundle loads. */

declare global {
  interface Window {
    toggleDiag?: () => boolean;
  }
}

/** Enable diagnostic mode from ?diag=1 / #...diag on boot. */
export function initDiagMode(): void {
  try {
    const on = /(?:^|[?&#])diag=?1?(?:[&#]|$)/.test(location.search + location.hash);
    if (on) document.body.classList.add('diag');
  } catch {
    /* no-op */
  }
}

export function toggleDiag(): boolean {
  document.body.classList.toggle('diag');
  return document.body.classList.contains('diag');
}

// Window-global registration (e2e contract) — module import side effect.
if (typeof window !== 'undefined') {
  window.toggleDiag = toggleDiag;
}
