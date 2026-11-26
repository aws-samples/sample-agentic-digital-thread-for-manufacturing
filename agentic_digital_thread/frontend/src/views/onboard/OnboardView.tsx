// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* OnboardView (jsx-migration W5.1) — the Onboard view is now fully
 * store-driven React: <CatalogList/> renders the catalog from the catalog
 * store, <ObFlow/> renders #onboard-flow from the flow store (welcome /
 * wizard / detail screens), <ObModals/> renders the overlays, and
 *
 * Contractual ids/classes (e2e suite tests/e2e/test_onboarding.py + CSS):
 * #catalog-list, #card-<id>, #btn-<id>, .ob-confirm-btn,
 * .sys-card-disconnect-btn, #onboard-flow, #ob-welcome-chat.
 *
 * Theme persistence is deliberately NOT ported here — the app shell owns it
 * (onboard.js's toggleTheme/restore block is superseded by useTheme). */
import { useEffect, useState } from 'react';

import { useWorkshopMode } from '../../features/workshop/useWorkshopStory';
import { useI18n } from '../../lib/i18n';
import { obLoadCatalog } from './catalog';
import { obWelcomeChat } from './chat';
import { CatalogList } from './components/CatalogList';
import { ObFlow } from './components/ObFlow';
import { ObModals } from './components/ObModals';
import { obResetAll } from './flow';
import { registerOnboardGlobals } from './globals';
import { useRuntimeQuestionsDeepLink } from './runtime-questions-link';
import { loadConcepts } from './state';

/* Init-once guard (module-level so React StrictMode's double effect and any
 * remount don't re-fire the boot fetches — matches the old page's single
 * script initialization per load). */
let initialized = false;

export default function OnboardView() {
  const { t } = useI18n();
  const [catalogOpen, setCatalogOpen] = useState(false);
  /* Reset All disconnects every system and reseeds the sample databases. A
   * workshop story runs on the systems its seed onboarded at deploy, and the
   * shared attendee login is an admin, so the button is offered only once the
   * account is known NOT to run a workshop story (hidden while still
   * unknown, so it never flashes). Each card's Remove system stays, but on
   * the same gate a seeded system's removal keeps its registration: the
   * dialogs get this reading and drop the "also forget" checkbox for a
   * system the user did not connect. A
   * facilitator's full reset: docs/workshop-mode.md#reset-all. */
  const workshopMode = useWorkshopMode();
  const showResetAll = workshopMode === 'off';

  useEffect(() => {
    if (initialized) return;
    initialized = true;
    registerOnboardGlobals();
    loadConcepts(); // top-of-script /api/onboard/concepts fetch in onboard.js
    obWelcomeChat(); // DOMContentLoaded wiring in onboard.js (seed chips)
    void obLoadCatalog(); // switchView('onboard') → obLoadCatalog() in the old app
  }, []);
  // Bell deep link (/#onboard/questions) → runtime questions IN the chat.
  useRuntimeQuestionsDeepLink();

  useEffect(() => {
    if (!catalogOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCatalogOpen(false);
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [catalogOpen]);

  return (
    <div className={`onboard-layout${catalogOpen ? ' catalog-open' : ''}`}>
      <button
        type="button"
        className="catalog-toggle"
        aria-controls="system-catalog-drawer"
        aria-expanded={catalogOpen}
        onClick={() => setCatalogOpen((open) => !open)}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <line x1="8" y1="6" x2="21" y2="6" />
          <line x1="8" y1="12" x2="21" y2="12" />
          <line x1="8" y1="18" x2="21" y2="18" />
          <line x1="3" y1="6" x2="3.01" y2="6" />
          <line x1="3" y1="12" x2="3.01" y2="12" />
          <line x1="3" y1="18" x2="3.01" y2="18" />
        </svg>
        Systems
      </button>
      {catalogOpen && (
        <button
          type="button"
          className="catalog-backdrop"
          aria-label="Close systems catalog"
          onClick={() => setCatalogOpen(false)}
        />
      )}
      {/* The catalog and flow stay mounted when the mobile drawer closes so
          active onboarding state and in-flight wizard work are preserved. */}
      <aside className="system-catalog" id="system-catalog-drawer" aria-label="Systems">
        <div className="catalog-header">
          <h3>{t('onboard.systemCatalog')}</h3>
          <div className="catalog-header-actions">
            {showResetAll && (
              <button className="reset-btn" onClick={() => obResetAll()}>
                {t('onboard.resetAll')}
              </button>
            )}
            <button
              type="button"
              className="catalog-close"
              aria-label="Close systems catalog"
              onClick={() => setCatalogOpen(false)}
            >
              ✕
            </button>
          </div>
        </div>
        <div id="catalog-list">
          <CatalogList onNavigate={() => setCatalogOpen(false)} />
        </div>
      </aside>
      {/* Right: Onboarding Flow — rendered from the flow store and never
          unmounted by the mobile catalog toggle. */}
      <ObFlow />
      <ObModals workshopMode={workshopMode} />
    </div>
  );
}
