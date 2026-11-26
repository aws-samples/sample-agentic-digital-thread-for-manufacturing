// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* AI Operations Briefing card — React half of the old briefing.ts DOM writes
 * (jsx-migration W2.1). Renders #ai-briefing-card / #ai-briefing-content /
 * #briefing-timestamp (CONTRACTUAL: #ai-briefing-card visible on the default
 * view) from the store's briefing slice. The three old sanitized-markdown
 * innerHTML sinks (mdInline title/detail, mdBlock fallback) render through
 * the shared <Md/> component (same marked+DOMPurify semantics). */
import { useI18n } from '../../lib/i18n';
import { useStore } from '../../lib/store';
import { Md } from '../../shared/ui/Md';
import { BRIEFING_PROMPT, requestOpsBriefing } from './briefing';
import { dashboardStore } from './store';

function BriefingContent() {
  const briefing = useStore(dashboardStore, (s) => s.briefing);
  switch (briefing.phase) {
    case 'init':
      return (
        <div style={{ color: 'var(--muted)', fontStyle: 'italic' }}>
          Initializing briefing...
        </div>
      );
    case 'placeholder':
      return (
        <div style={{ color: 'var(--muted)', fontStyle: 'italic', fontSize: '0.78rem' }}>
          Analyzing...
        </div>
      );
    case 'unavailable':
      return (
        <div style={{ color: 'var(--muted)', fontSize: '0.78rem', fontStyle: 'italic' }}>
          Briefing unavailable
        </div>
      );
    case 'block':
      return <Md text={briefing.blockMd} />;
    case 'bullets':
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
          {briefing.bullets.map((b, i) => (
            <div
              key={i}
              className="briefing-bullet"
              style={{
                display: 'flex',
                gap: '0.5rem',
                padding: '0.35rem 0',
                borderBottom: '1px solid var(--border)',
              }}
            >
              <div
                style={{
                  width: 3,
                  minWidth: 3,
                  borderRadius: 2,
                  background: b.color,
                  marginTop: '0.15rem',
                }}
              />
              <div style={{ fontSize: '0.8rem', lineHeight: 1.5, color: 'var(--text2)' }}>
                {b.title ? (
                  <>
                    <span style={{ fontWeight: 600, color: 'var(--text)' }}>
                      <Md text={b.title} inline />
                    </span>
                    {' — '}
                  </>
                ) : null}
                <Md text={b.detail} inline />
              </div>
            </div>
          ))}
          {briefing.notes.length > 0 ? (
            // Every non-card line after the first card, as ONE markdown block:
            // a caveat, a footnote over several lines, a sixth bullet.
            <div
              className="briefing-note"
              style={{
                fontSize: '0.72rem', lineHeight: 1.5, color: 'var(--muted)',
                padding: '0.35rem 0 0 0.6rem',
              }}
            >
              <Md text={briefing.notes.join('\n')} />
            </div>
          ) : null}
        </div>
      );
    default:
      return null;
  }
}

function BriefingTitle({ analyzing }: { analyzing: boolean }) {
  const { t } = useI18n();
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M12 2a10 10 0 1 0 10 10" />
        <path d="M12 6v6l4 2" />
        <circle cx="18" cy="6" r="3" fill="currentColor" stroke="none" opacity="0.4" />
      </svg>
      <span
        className="briefing-title-tooltip"
        data-tooltip={`Agent prompt: ${BRIEFING_PROMPT}`}
        style={{ cursor: 'help', borderBottom: '1px dashed var(--muted)' }}
      >
        {t('dash.aiBriefing')}
      </span>
      <span
        id="briefing-analyzing"
        style={{
          fontSize: '0.65rem', color: 'var(--muted)', fontStyle: 'italic',
          marginLeft: '0.5rem', display: analyzing ? '' : 'none',
        }}
      >
        analyzing...
      </span>
    </span>
  );
}

function BriefingMeta() {
  const lifecycle = useStore(dashboardStore, (s) => s.briefing.lifecycle);
  const timestamp = useStore(dashboardStore, (s) => s.briefing.timestamp);
  const mono = { fontSize: '0.65rem', color: 'var(--muted)',
    fontFamily: "'IBM Plex Mono',monospace" } as const;
  return (
    <>
      <span id="briefing-lifecycle" title="Automatic briefing lifecycle" style={mono}>
        {lifecycle === 'active' ? 'auto: scheduled' : 'auto: paused'}
      </span>
      <span id="briefing-timestamp" style={mono}>{timestamp}</span>
    </>
  );
}

function RefreshBriefingButton({ analyzing }: { analyzing: boolean }) {
  return (
    <button
      onClick={() => void requestOpsBriefing({ force: true })}
      title="Refresh briefing"
      style={{
        background: 'none', border: 'none', cursor: 'pointer',
        color: 'var(--muted)', padding: '0.1rem 0.25rem', borderRadius: 3,
        display: 'flex', alignItems: 'center', opacity: 0.7,
      }}
      onMouseOver={(e) => {
        e.currentTarget.style.opacity = '1';
        e.currentTarget.style.color = 'var(--accent)';
      }}
      onMouseOut={(e) => {
        e.currentTarget.style.opacity = '0.7';
        e.currentTarget.style.color = 'var(--muted)';
      }}
    >
      <svg
        width="13" height="13" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
        strokeLinejoin="round"
        style={{ animation: analyzing ? 'spin 1s linear infinite' : '' }}
      >
        <polyline points="23 4 23 10 17 10" />
        <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
      </svg>
    </button>
  );
}

function BriefingHeader() {
  const analyzing = useStore(dashboardStore, (s) => s.briefing.analyzing);
  return (
    <div
      className="ai-insights-title"
      style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
    >
      <BriefingTitle analyzing={analyzing} />
      <BriefingMeta />
      <RefreshBriefingButton analyzing={analyzing} />
    </div>
  );
}

export function AiBriefing() {
  return (
    <div
      className="ai-insights"
      id="ai-briefing-card"
      style={{ display: 'flex', flexDirection: 'column' }}
    >
      <BriefingHeader />
      <div
        id="ai-briefing-content"
        style={{
          flex: 1, fontSize: '0.82rem', lineHeight: 1.6, color: 'var(--text2)',
          textRendering: 'optimizeLegibility', WebkitFontSmoothing: 'antialiased',
        }}
      >
        <BriefingContent />
      </div>
    </div>
  );
}
