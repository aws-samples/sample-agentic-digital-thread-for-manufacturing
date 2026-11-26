// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* AdminPage — the admin console page.
 *
 * Contractual DOM (tests/e2e/test_admin.py):
 *   - nav.tab-bar with button[data-tab=tab-agents|tab-settings|tab-demoscript|tab-thread|tab-vocabulary|tab-context|tab-evals|tab-conversations]
 *   - clicking a tab activates the matching #<tab> .tab-panel (`.active`) with
 *     no JS errors; hash-driven restore via history.replaceState('#'+tabId)
 *   - #access-denied gate off /api/auth/me is_admin
 *   - quick-links (#link-highbyte #link-erp-dashboard #link-console-start
 *     #link-i3x-highbyte #link-i3x-sitewise) stay hidden while unconfigured;
 *     i3x links mint a one-shot URL on click (/api/admin/i3x-explorer-link/…)
 *   - the Thread tab mounts <ThreadPanel/> on first activation (the old
 *     initAdminThread timing): #thread-loading appears, then leaves the DOM
 *     (count 0) once the overview resolves
 *   - the AgentCore Metrics tab hosts <AgentsFleet containerId="agents-grid"/>
 *     (agents-shared.js loadAgentsInto('agents-grid')), mounted once at page init. */
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { useMutation, useQueries } from '@tanstack/react-query';

import { ApiError } from '../../api/client';
import type { QuickLinks } from '../../api/types/admin';
import { useAuth } from '../../contexts/AuthContext';
import { useBranding } from '../../contexts/BrandingContext';
import { useTheme } from '../../hooks/useTheme';
import { useI18n } from '../../lib/i18n';
import { AgentsFleet } from '../../shared/agents';
import { ThreadPanel } from '../../shared/thread';
import { push as pushToast } from '../../shared/ui/toast-store';
import { adminBootstrapQueryOptions, mintI3xLinkMutationOptions } from './admin-queries';
import { useWorkshopStory } from '../../features/workshop/useWorkshopStory';
import { DEFAULT_ADMIN_TAB, restoreTabFromHash, workshopTabFallback, type AdminTabId } from './tabs/admin-logic';
import BusinessContextTab from './tabs/BusinessContextTab';
import ConversationsTab from './tabs/ConversationsTab';
import DemoScriptTab from './tabs/DemoScriptTab';
import EvalsTab from './tabs/EvalsTab';
import SettingsTab from './tabs/SettingsTab';
import VocabularyTab from './tabs/VocabularyTab';
import { WorkshopTabButton, WorkshopTabSection } from './tabs/workshop/WorkshopTabGate';

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

// Shared external-link icon used by every quick-link (verbatim path data).
function QuickLinkIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}

export default function AdminPage() {
  const { t } = useI18n();
  const { user, loading } = useAuth();
  const { branding, loaded: brandingLoaded } = useBranding();
  const { isLight, toggleTheme } = useTheme();

  const isAdmin = !!user?.is_admin;

  const [activeTab, setActiveTab] = useState<AdminTabId>(DEFAULT_ADMIN_TAB);
  // Demo Script tab gate: hidden until the gate RESOLVES true (null = not
  // resolved yet). Showing during null flashed a button that then vanished
  // for base installs on every page load (observed live) — hide-until-known
  // is the honest default; the button appears once settings confirm it.
  const [demoEnabled, setDemoEnabled] = useState<boolean | null>(null);
  const [settingsMounted, setSettingsMounted] = useState(false);
  const [demoScriptMounted, setDemoScriptMounted] = useState(false);
  const [threadMounted, setThreadMounted] = useState(false);
  const [vocabularyMounted, setVocabularyMounted] = useState(false);
  const [contextMounted, setContextMounted] = useState(false);
  const [evalsMounted, setEvalsMounted] = useState(false);
  const [conversationsMounted, setConversationsMounted] = useState(false);
  // Branding-save override for logo/footer (saveSettings updated them in place;
  // it also reset the footer to text-only, dropping the version span).
  const [companyOverride, setCompanyOverride] = useState<string | null>(null);
  const [versionSuppressed, setVersionSuppressed] = useState(false);

  // All eight admin domains become enabled in the same render after auth.
  // requestJson transport is encapsulated by admin-queries; AdminPage owns
  // authorization, query observation, and visible retry UI only.
  // The settings tab observes the same keys, so opening it never starts a
  // second request while these entries remain fresh.
  const [settingsQuery, , , , , , quickLinksQuery, versionQuery] = useQueries({
    queries: adminBootstrapQueryOptions(isAdmin),
  });
  const mintLinkMutation = useMutation(mintI3xLinkMutationOptions());

  const company = companyOverride ?? branding?.company ?? '';
  const links: QuickLinks | undefined = quickLinksQuery.data;
  const version = versionSuppressed ? '' : versionQuery.data?.version || '';

  // ── switchAdminTab (admin.js:11-26) ─────────────────────────────────────────
  const activateTab = (tabId: AdminTabId) => {
    setActiveTab(tabId);
    history.replaceState(null, '', '#' + tabId);
    if (tabId === 'tab-settings') setSettingsMounted(true);
    if (tabId === 'tab-demoscript') setDemoScriptMounted(true);
    if (tabId === 'tab-thread') setThreadMounted(true);
    if (tabId === 'tab-vocabulary') setVocabularyMounted(true);
    if (tabId === 'tab-context') setContextMounted(true);
    if (tabId === 'tab-evals') setEvalsMounted(true);
    if (tabId === 'tab-conversations') setConversationsMounted(true);
  };
  const activateTabRef = useRef(activateTab);
  activateTabRef.current = activateTab;
  // The Workshop tab's gate (the SHARED story query; 'pending' until it answers).
  const workshopGate = useWorkshopStory().gate;
  const workshopGateRef = useRef(workshopGate);
  workshopGateRef.current = workshopGate;

  // ── initDemoGate + hash restore (admin.js:37-64) — admin only ───────────────
  const gateResolvedRef = useRef(false);
  useEffect(() => {
    if (!isAdmin) {
      gateResolvedRef.current = false;
      setDemoEnabled(null);
      return;
    }
    if (settingsQuery.data) {
      setDemoEnabled(settingsQuery.data.demo_enabled === true);
    } else if (settingsQuery.isError) {
      setDemoEnabled(false);
    }
    if (gateResolvedRef.current || (!settingsQuery.isSuccess && !settingsQuery.isError)) return;
    gateResolvedRef.current = true;
    const enabled = settingsQuery.data?.demo_enabled === true;
    const restored = restoreTabFromHash(window.location.hash, enabled, workshopGateRef.current);
    if (restored) activateTabRef.current(restored);
  }, [isAdmin, settingsQuery.data, settingsQuery.isError, settingsQuery.isSuccess]);

  // #tab-workshop on a deploy that renders no workshop section (frozen/demo
  // story, 403/404 — or the gate later settles hidden): fall back to the
  // default tab instead of showing an unrendered panel.
  useEffect(() => {
    const fallback = workshopTabFallback(activeTab, workshopGate);
    if (fallback) activateTabRef.current(fallback);
  }, [activeTab, workshopGate]);

  // In-page #tab-* links (e.g. the Demo Script's "Admin → AgentCore Metrics"
  // anchor) only change the hash when already on admin.html — follow them.
  // activateTab's replaceState rewrites the same hash, which never re-fires
  // hashchange, so this cannot loop.
  useEffect(() => {
    if (!isAdmin) return;
    const onHashChange = () => {
      const target = restoreTabFromHash(window.location.hash, demoEnabled === true, workshopGate);
      if (target) activateTabRef.current(target);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, [isAdmin, demoEnabled, workshopGate]);

  // ── Branding header/footer + /api/version badge + title (auth-init) ────────
  useEffect(() => {
    if (!brandingLoaded) return;
    const c = branding?.company || 'Agentic Digital Thread';
    document.title = 'Admin — ' + c;
  }, [brandingLoaded, branding]);

  const bootstrapErrors = [
    settingsQuery.isError ? 'Admin settings unavailable: ' + errorDetail(settingsQuery.error) : '',
    quickLinksQuery.isError ? 'Quick links unavailable: ' + errorDetail(quickLinksQuery.error) : '',
    versionQuery.isError ? 'Version unavailable: ' + errorDetail(versionQuery.error) : '',
    mintLinkMutation.isError ? 'i3X explorer unavailable: ' + errorDetail(mintLinkMutation.error) : '',
  ].filter(Boolean);

  const retryBootstrapErrors = () => {
    if (settingsQuery.isError) void settingsQuery.refetch();
    if (quickLinksQuery.isError) void quickLinksQuery.refetch();
    if (versionQuery.isError) void versionQuery.refetch();
    mintLinkMutation.reset();
  };

  // i3X Explorer links — mint a one-shot URL with the backend-held bearer token.
  const mintI3xLink = async (e: ReactMouseEvent, which: 'highbyte' | 'sitewise') => {
    e.preventDefault();
    try {
      const result = await mintLinkMutation.mutateAsync(which);
      if (result.status === 'ok' && result.href) {
        window.open(result.href, '_blank', 'noopener');
      } else {
        pushToast('error', 'Could not open i3X explorer: ' + (result.message || 'unknown error'), {
          urgent: true,
        });
      }
    } catch (error) {
      pushToast('error', 'Could not open i3X explorer: ' + errorDetail(error), { urgent: true });
    }
  };

  if (loading) return null;

  const denied = !isAdmin;
  // The i3X Explorer links show only when the server has the explorer build.
  const i3xBuilt = links?.i3x_explorer === true;
  const showI3xHighbyte = i3xBuilt && !!(links?.highbyte && links.highbyte_i3x_token);
  const showI3xSitewise = i3xBuilt && !!(links?.sitewise_i3x_url && links.sitewise_i3x_token);

  const panelCls = (id: AdminTabId) => 'tab-panel' + (activeTab === id ? ' active' : '');
  const btnCls = (id: AdminTabId) => (activeTab === id ? 'active' : undefined);

  return (
    <>
      <div id="access-denied" style={denied ? { display: 'block' } : undefined}>
        <svg
          width="32"
          height="32"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--red)"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
        </svg>
        <span>{t('admin.accessDenied')}</span>
      </div>

      <div id="main-content" className="admin-mobile-safe" style={denied ? { display: 'none' } : undefined}>
        <header>
          <a href="/" className="logo" id="brand-logo" style={{ textDecoration: 'none' }}>
            {company ? company.toUpperCase() : 'AGENTIC DIGITAL THREAD'}
          </a>
          <div className="nav" aria-label="Admin context">
            <span className="admin-badge">{t('admin.badge')}</span>
          </div>
          <div className="user-menu" id="admin-user-menu">
            <a href="/" className="back-link">
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="19" y1="12" x2="5" y2="12" />
                <polyline points="12 19 5 12 12 5" />
              </svg>
              {t('admin.backToApp')}
            </a>
            <button
              className="user-menu-btn"
              onClick={toggleTheme}
              title={t('admin.toggleTheme')}
              aria-label={t('admin.toggleTheme')}
            >
            <svg
              id="adm-theme-dark"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ display: isLight ? 'none' : undefined }}
            >
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
            <svg
              id="adm-theme-light"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ display: isLight ? undefined : 'none' }}
            >
              <circle cx="12" cy="12" r="5" />
              <line x1="12" y1="1" x2="12" y2="3" />
              <line x1="12" y1="21" x2="12" y2="23" />
              <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
              <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
              <line x1="1" y1="12" x2="3" y2="12" />
              <line x1="21" y1="12" x2="23" y2="12" />
              <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
              <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
            </svg>
            </button>
          </div>
        </header>

        <nav className="tab-bar">
          <button className={btnCls('tab-agents')} data-tab="tab-agents" onClick={() => activateTab('tab-agents')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
            </svg>
            {t('admin.tabAgents')}
          </button>
          <button
            className={btnCls('tab-demoscript')}
            data-tab="tab-demoscript"
            style={{ display: demoEnabled === true ? undefined : 'none' }}
            onClick={() => activateTab('tab-demoscript')}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
            {t('admin.tabDemoScript')}
          </button>
          <button className={btnCls('tab-thread')} data-tab="tab-thread" onClick={() => activateTab('tab-thread')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="5" r="2" />
              <circle cx="5" cy="19" r="2" />
              <circle cx="19" cy="19" r="2" />
              <line x1="12" y1="7" x2="5" y2="17" />
              <line x1="12" y1="7" x2="19" y2="17" />
            </svg>
            {t('admin.tabThread')}
          </button>
          <button className={btnCls('tab-vocabulary')} data-tab="tab-vocabulary" onClick={() => activateTab('tab-vocabulary')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M2 3h6a4 4 0 014 4v14a3 3 0 00-3-3H2z" />
              <path d="M22 3h-6a4 4 0 00-4 4v14a3 3 0 013-3h7z" />
            </svg>
            {t('admin.tabVocabulary')}
          </button>
          <button className={btnCls('tab-context')} data-tab="tab-context" onClick={() => activateTab('tab-context')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="8" y1="13" x2="16" y2="13" />
              <line x1="8" y1="17" x2="13" y2="17" />
            </svg>
            {t('admin.tabContext')}
          </button>
          <button className={btnCls('tab-evals')} data-tab="tab-evals" onClick={() => activateTab('tab-evals')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 11l3 3L22 4" />
              <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
            </svg>
            {t('admin.tabEvals')}
          </button>
          <button
            className={btnCls('tab-conversations')}
            data-tab="tab-conversations"
            onClick={() => activateTab('tab-conversations')}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
              <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
            </svg>
            {t('admin.tabConversations')}
          </button>
          <WorkshopTabButton active={activeTab === 'tab-workshop'} onActivate={() => activateTab('tab-workshop')} />
          {/* Settings sits FAR RIGHT of the tab buttons (quick-link anchors follow). */}
          <button className={btnCls('tab-settings')} data-tab="tab-settings" onClick={() => activateTab('tab-settings')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.07 4.93a10 10 0 010 14.14M4.93 4.93a10 10 0 000 14.14" />
            </svg>
            {t('admin.tabSettings')}
          </button>
          <a
            id="link-highbyte"
            href={links?.highbyte ? links.highbyte + '/ui/#/' : '#'}
            target="_blank"
            rel="noopener"
            className="quick-link"
            style={{ display: links?.highbyte ? undefined : 'none' }}
          >
            <QuickLinkIcon />
            HighByte
          </a>
          <a
            id="link-erp-dashboard"
            href={links?.erp_dashboard || '#'}
            target="_blank"
            rel="noopener"
            className="quick-link"
            style={{ display: links?.erp_dashboard ? undefined : 'none' }}
          >
            <QuickLinkIcon />
            ERP Dashboard
          </a>
          {/* Optional AWS Quick desktop URL; hidden until
              /adt/quick-desktop-url is configured in this environment */}
          <a
            id="link-quick-desktop"
            href={links?.quick_desktop || '#'}
            target="_blank"
            rel="noopener"
            className="quick-link"
            style={{ display: links?.quick_desktop ? undefined : 'none' }}
            title="AWS Quick Desktop Kiosk (browser via DCV)"
          >
            <QuickLinkIcon />
            Quick Desktop
          </a>
          {/* href is populated at runtime from the CONSOLE_START_URL env var; hidden
              until configured so it never renders as a dead '#' link */}
          <a
            id="link-console-start"
            href={links?.console_start || '#'}
            target="_blank"
            rel="noopener"
            className="quick-link"
            style={{ display: links?.console_start ? undefined : 'none' }}
            title="AWS Identity Center Portal — Bedrock & SiteWise console access"
          >
            <QuickLinkIcon />
            AWS Console
          </a>
          <a
            id="link-i3x-highbyte"
            href="#"
            target="_blank"
            rel="noopener"
            className="quick-link"
            style={{ display: showI3xHighbyte ? undefined : 'none' }}
            onClick={(e) => void mintI3xLink(e, 'highbyte')}
          >
            <QuickLinkIcon />
            i3X Explorer · HighByte
          </a>
          <a
            id="link-i3x-sitewise"
            href="#"
            target="_blank"
            rel="noopener"
            className="quick-link"
            style={{ display: showI3xSitewise ? undefined : 'none' }}
            onClick={(e) => void mintI3xLink(e, 'sitewise')}
          >
            <QuickLinkIcon />
            i3X Explorer · SiteWise
          </a>
          {bootstrapErrors.length > 0 && (
            <span id="quick-links-error" className="quick-link" role="status">
              {bootstrapErrors.join(' · ')}{' '}
              <button type="button" onClick={retryBootstrapErrors}>Retry</button>
            </span>
          )}
        </nav>

        {/* Agent Dashboard */}
        <section id="tab-agents" className={panelCls('tab-agents')}>
          <div id="agents-grid">
            {isAdmin && <AgentsFleet containerId="agents-grid" />}
          </div>
        </section>

        {/* Settings — first activation mounts once; Query cache owns freshness. */}
        <section id="tab-settings" className={panelCls('tab-settings')}>
          {settingsMounted && (
            <SettingsTab
              onDemoEnabledChange={setDemoEnabled}
              onBrandingSaved={(c) => {
                setCompanyOverride(c);
                // saveSettings() reset the footer to text-only (dropping the
                // version span) — kept verbatim.
                setVersionSuppressed(true);
              }}
            />
          )}
        </section>

        {/* Demo Script mounts once, preserving markdown and scroll state across tabs. */}
        <section id="tab-demoscript" className={panelCls('tab-demoscript')}>
          {demoScriptMounted ? (
            <DemoScriptTab />
          ) : (
            <div
              id="demoscript-content"
              style={{ color: 'var(--text)', lineHeight: 1.7, fontSize: '0.9rem', maxWidth: 860 }}
            >
              <div style={{ color: 'var(--muted)', fontSize: '0.8rem' }}>Loading demo script…</div>
            </div>
          )}
        </section>

        {/* Thread Tab Panel — <ThreadPanel/> mounts on first activation (the
            old initAdminThread timing) and loads the overview on mount. */}
        <section id="tab-thread" className={panelCls('tab-thread')}>
          {threadMounted && <ThreadPanel />}
        </section>

        {/* Vocabulary — mounts on first activation (the settingsMounted idiom);
            Query cache owns concept-list freshness afterwards. */}
        <section id="tab-vocabulary" className={panelCls('tab-vocabulary')}>
          {vocabularyMounted && <VocabularyTab />}
        </section>

        <section id="tab-context" className={panelCls('tab-context')}>
          {contextMounted && <BusinessContextTab />}
        </section>

        {/* Evals — mounts on first activation (the tab-vocabulary idiom);
            Query cache owns run-history freshness afterwards. */}
        <section id="tab-evals" className={panelCls('tab-evals')}>
          {evalsMounted && <EvalsTab />}
        </section>
        {/* Conversations — mounts on first activation; every user's web-chat
            questions and answers (admin only, GET /api/admin/conversations). */}
        <section id="tab-conversations" className={panelCls('tab-conversations')}>
          {conversationsMounted && <ConversationsTab />}
        </section>
        <WorkshopTabSection active={activeTab === 'tab-workshop'} />

        <footer id="admin-footer">
          Admin Panel — {company || 'Agentic Digital Thread'}
          {version && (
            <span
              style={{
                marginLeft: '1rem',
                fontSize: '0.65rem',
                color: 'var(--muted)',
                fontFamily: "'IBM Plex Mono',monospace",
                opacity: 0.6,
              }}
            >
              {version}
            </span>
          )}
        </footer>
      </div>
    </>
  );
}
