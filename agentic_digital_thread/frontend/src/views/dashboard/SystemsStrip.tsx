// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Connected Systems bar + empty-state hero — React half of the old systems.ts
 * (jsx-migration W2.1). #systems-bar / #systems-chips render the connected
 * systems as chips (same classes/structure; all server names via JSX text);
 * <EmptyHero/> is the old lazily-created #dash-empty-hero (rendered only once
 * loadSystems reports zero connected systems — store.showEmptyHero). */
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import { dashboardStore } from './store';
import { switchToOnboard } from './systems';

export function SystemsStrip() {
  const { t } = useI18n();
  const systems = useStore(dashboardStore, (s) => s.systems);
  const connected = (systems || []).filter((s) => s.status === 'connected');
  return (
    <div className="systems-bar" id="systems-bar">
      <span className="systems-bar-label">{t('dash.connectedSystems')}</span>
      <div className="systems-bar-chips" id="systems-chips">
        {systems === null ? null : connected.length === 0 ? (
          <span className="system-chip muted" onClick={() => switchToOnboard()}>
            No systems connected — onboard one
          </span>
        ) : (
          connected.map((s, i) => (
            <span key={s.id || i} className="system-chip">
              <span
                className="sys-dot alive"
                style={{
                  background: 'var(--green)',
                  animation: 'pulse-dot 2s ease-in-out infinite',
                }}
              />
              <span style={{ color: 'var(--text2)' }}>{`${s.type}: ${s.name}`}</span>
            </span>
          ))
        )}
      </div>
    </div>
  );
}

/** The "No systems connected yet" hero (old dash-empty-hero, prepended into
 * #dashboard-view → rendered first in the DashboardView fragment). */
export function EmptyHero() {
  const show = useStore(dashboardStore, (s) => s.showEmptyHero);
  if (!show) return null;
  return (
    <div
      id="dash-empty-hero"
      style={{ textAlign: 'center', padding: '4rem 2rem', color: 'var(--muted)' }}
    >
      <div style={{ fontSize: '1.1rem', color: 'var(--text)', marginBottom: '0.5rem' }}>
        No systems connected yet
      </div>
      <div style={{ marginBottom: '1.5rem' }}>
        <a
          href="#"
          style={{ color: 'var(--accent)' }}
          onClick={(e) => {
            e.preventDefault();
            switchToOnboard();
          }}
        >
          Onboard a system
        </a>
        {' to see live data here.'}
      </div>
    </div>
  );
}
