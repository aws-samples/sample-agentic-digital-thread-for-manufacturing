// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Demo Script tab — loadDemoScript(). The host
 * mounts this component on first activation and keeps it mounted, so switching
 * tabs does not reload the markdown or reset its local state.
 *
 * jsx-migration W2.4: the markdown renders through the shared <Md/> component
 * (with applyDemoScriptStyles as its post-process pass) and the friendly
 * error copy is a JSX element — no innerHTML sinks. The markdown is fetched
 * from /demo-script.md (a public/ passthrough) and the placeholder company
 * name is swapped for the live branding. */
import { useEffect, useState } from 'react';

import { useBranding } from '../../../contexts/BrandingContext';
import { Md } from '../../../shared/ui/Md';
import { applyDemoScriptStyles, replaceDemoScriptCompany } from './admin-logic';

/** loadDemoScript() friendly error copy (admin-logic demoScriptErrorHtml) as
 * a JSX element — the error message renders as escaped text. */
function DemoScriptError({ message }: { message: string }) {
  return (
    <div style={{ color: 'var(--text2)', padding: '1rem 0' }}>
      {/not found/i.test(message) ? (
        <>
          No demo script is configured for this deployment. Add{' '}
          <code>agentic_digital_thread/frontend/public/demo-script.md</code> to populate this tab.
        </>
      ) : (
        <>Failed to load demo script: {message}</>
      )}
    </div>
  );
}

export default function DemoScriptTab() {
  const { branding, loaded: brandingLoaded, error: brandingError } = useBranding();
  const [md, setMd] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!brandingLoaded) return;

    // Scroll panel to top (verbatim: the section is the scroll container).
    const panel = document.getElementById('tab-demoscript');
    if (panel) panel.scrollTop = 0;

    let cancelled = false;
    (async () => {
      try {
        // BrandingContext is the sole requestJson owner for /api/branding.
        // This component deliberately retains only the checked raw-text fetch.
        const companyName = branding?.company || 'Agentic Digital Thread';
        const resp = await fetch('/demo-script.md');
        if (!resp.ok) throw new Error('Not found');
        const text = replaceDemoScriptCompany(await resp.text(), companyName);
        if (!cancelled) setMd(text);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [brandingLoaded, branding]);

  return (
    <div
      id="demoscript-content"
      style={{ color: 'var(--text)', lineHeight: 1.7, fontSize: '0.9rem', maxWidth: 860 }}
    >
      {brandingError && (
        <div role="status" style={{ color: 'var(--muted)', fontSize: '0.75rem', marginBottom: '0.75rem' }}>
          Branding unavailable; using the default company name: {brandingError}
        </div>
      )}
      {error != null ? (
        <DemoScriptError message={error} />
      ) : md != null ? (
        // applyDemoScriptStyles is the original post-render inline-style pass
        // (idempotent: it assigns cssText per selector on each run).
        <Md text={md} postProcess={applyDemoScriptStyles} />
      ) : (
        <div style={{ color: 'var(--muted)', fontSize: '0.8rem' }}>Loading demo script…</div>
      )}
    </div>
  );
}
