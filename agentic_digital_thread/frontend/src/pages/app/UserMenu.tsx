// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* UserMenu — the consolidated header dropdown
 * (#user-menu-trigger/#user-dropdown).
 *
 * The top-right header controls (language select, theme toggle, admin link,
 * sign-out) live in a labeled dropdown behind a person-circle trigger. Element
 * ids are CONTRACTUAL (e2e suite + ported CSS): #user-menu-trigger
 * #user-dropdown #lang-select #user-display #admin-link #logout-btn #theme-btn
 * #theme-mode-label.
 *
 * Behavior (user-menu.js): trigger click toggles; Escape closes and returns
 * focus to the trigger; outside click closes; clicks INSIDE the panel keep it
 * open so the language <select> and theme toggle stay usable without
 * reopening. aria-expanded mirrors state; the panel is anchored right:0 (CSS)
 * so it never pushes content or overflows the viewport.
 *
 * The controls reuse the SPA's existing handlers rather than duplicating
 * logic: useTheme (theme class + localStorage), i18n setLang (persists
 * adt-lang via i18n-lang + notifies the chrome-translation store), useAuth
 * (visibility of admin link / sign-out / display name). The theme-mode
 * readout derives from useTheme state — no DOM reads needed.
 *
 * Labels render through t() (lib/i18n.ts) — the menu.* keys the
 * legacy index.html marked with data-i18n; useI18n re-renders them on
 * language change. Control titles WITHOUT a legacy data-i18n-title hook
 * ("AI Response Language", "Toggle light/dark mode", …) stay literal. */
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { ApiError, requestJson } from '../../api/client';
import { shortDisplayName, useAuth } from '../../contexts/AuthContext';
import { useTheme } from '../../hooks/useTheme';
import { setLang, useI18n } from '../../lib/i18n';
import { SUPPORTED_LANGS } from '../../lib/i18n-lang';
import './user-menu.css';

export default function UserMenu() {
  const [open, setOpen] = useState(false);
  const [logoutError, setLogoutError] = useState('');
  const queryClient = useQueryClient();
  // Chrome-translation store (localStorage 'adt-lang'): `lang` drives both the
  // select value and, through useI18n's subscription, the t() label renders.
  const { lang, t } = useI18n();
  const { user } = useAuth();
  const { isLight, toggleTheme } = useTheme();
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  // Escape closes (returning focus to the trigger); outside click closes.
  // Clicks inside the panel or on the trigger are ignored here — the trigger's
  // own onClick handles the toggle, and in-panel clicks keep the menu open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (
        t &&
        !dropdownRef.current?.contains(t) &&
        !triggerRef.current?.contains(t)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('click', onClick);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('click', onClick);
    };
  }, [open]);

  const logout = async () => {
    setLogoutError('');
    try {
      // Edge-auth deploys return { redirect: '/signout' } so the browser hits
      // the Authorization@Edge sign-out behavior (authoritative hosted-UI
      // sign-out); otherwise fall back to the login form as before.
      const resp = await requestJson<{ status?: string; redirect?: string }>('/api/auth/logout', { method: 'POST' });
      await queryClient.cancelQueries();
      queryClient.clear();
      window.location.replace(resp?.redirect ?? '/login.html');
    } catch (error) {
      const detail = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
      setLogoutError('Sign out failed: ' + detail);
    }
  };

  return (
    <>
      <button
        id="user-menu-trigger"
        className="user-menu-trigger"
        type="button"
        ref={triggerRef}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="user-dropdown"
        title={t('menu.accountMenu')}
        onClick={() => setOpen((o) => !o)}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" />
          <circle cx="12" cy="10" r="3" />
          <path d="M6.17 18.6a6 6 0 0 1 11.66 0" />
        </svg>
        <span
          className="user-menu-name"
          id="user-display"
          style={{ display: user ? '' : 'none' }}
        >
          {user ? shortDisplayName(user) : ''}
        </span>
        <svg className="user-menu-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      <div
        id="user-dropdown"
        className="user-dropdown"
        role="menu"
        aria-labelledby="user-menu-trigger"
        ref={dropdownRef}
        hidden={!open}
      >
        <div className="user-dropdown-section">{t('menu.preferences')}</div>
        <div className="user-dropdown-row" role="none">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10" />
            <path d="M2 12h20" />
            <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
          </svg>
          <span className="user-dropdown-label">{t('menu.language')}</span>
          <select
            id="lang-select"
            className="lang-select"
            title="AI Response Language"
            value={SUPPORTED_LANGS.some((l) => l.value === lang) ? lang : 'en'}
            onChange={(e) => setLang(e.target.value)}
          >
            {SUPPORTED_LANGS.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
        </div>
        <button
          id="theme-btn"
          className="user-dropdown-item"
          role="menuitem"
          onClick={toggleTheme}
          title="Toggle light/dark mode"
        >
          <svg id="theme-icon-dark" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: isLight ? 'none' : '' }}>
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
          </svg>
          <svg id="theme-icon-light" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ display: isLight ? '' : 'none' }}>
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
          <span className="user-dropdown-label">{t('menu.theme')}</span>
          <span className="user-dropdown-meta" id="theme-mode-label">
            {isLight ? t('menu.light') : t('menu.dark')}
          </span>
        </button>
        <div className="user-dropdown-divider" />
        <div className="user-dropdown-section">{t('menu.account')}</div>
        {/* New tab: leaving the app page cancels a running answer and drops
            the chat, and the workshop sends attendees to Admin between
            questions. */}
        <a
          id="admin-link"
          href="/admin.html"
          target="_blank"
          rel="noopener"
          className="user-dropdown-item"
          role="menuitem"
          style={{ display: user?.is_admin ? '' : 'none' }}
          title="Admin Panel"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          <span className="user-dropdown-label">{t('menu.adminPanel')}</span>
        </a>
        <button
          id="logout-btn"
          className="user-dropdown-item"
          role="menuitem"
          onClick={() => void logout()}
          style={{ display: user ? '' : 'none' }}
          title="Sign out"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" />
            <polyline points="16 17 21 12 16 7" />
            <line x1="21" y1="12" x2="9" y2="12" />
          </svg>
          <span className="user-dropdown-label">{t('menu.signOut')}</span>
        </button>
        {logoutError && (
          <div className="user-dropdown-section" role="alert">
            {logoutError}
          </div>
        )}
      </div>
    </>
  );
}
