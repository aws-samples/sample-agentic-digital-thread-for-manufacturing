// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Demo Mode confirm dialog — the only demo-specific UI left after the
 * uniformity refactor: the demo now narrates through the NORMAL chat
 * surfaces (chat sidebar + investigation graph; see views/dashboard/demo.ts),
 * so the former #demo-overlay pager (question card, splitIntoPages text
 * pages, progress sweep) is deleted. What remains is the "Leave demo mode?"
 * pause-confirm, rendered from the store's demo slice. */
import { useEffect, useRef } from 'react';

import { useStore } from '../../lib/store';
import { demoConfirmLeave, demoConfirmStay } from './demo';
import { dashboardStore } from './store';

const CONFIRM_BTN_BASE: React.CSSProperties = {
  padding: '0.4rem 1rem',
  borderRadius: 6,
  fontSize: '0.85rem',
  cursor: 'pointer',
};

/** "Leave demo mode?" confirm — shown when a background click / Escape /
 * Space lands during an active demo (demo.ts pauses the run while this is
 * up). Stay resumes; Leave takes the existing stop path. The document
 * capture handler in demo.ts only touches mousedown/keydown, so these
 * buttons' 'click' events land normally; Escape while open = Stay (also
 * handled there). */
function DemoConfirmDialog() {
  const stayRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    stayRef.current?.focus();
  }, []);

  return (
    <div
      id="demo-confirm-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="demo-confirm-title"
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.5)',
        zIndex: 10000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '2rem',
      }}
    >
      <div
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 12,
          padding: '1.25rem 1.5rem',
          maxWidth: 360,
          width: '100%',
        }}
      >
        <div
          id="demo-confirm-title"
          style={{ fontSize: '1rem', fontWeight: 600, color: 'var(--text)' }}
        >
          Leave demo mode?
        </div>
        <div
          style={{
            fontSize: '0.8rem',
            color: 'var(--muted)',
            margin: '0.5rem 0 1rem',
          }}
        >
          The demo is paused. Leaving returns you to the dashboard.
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem' }}>
          <button
            ref={stayRef}
            type="button"
            id="demo-confirm-stay"
            onClick={demoConfirmStay}
            style={{
              ...CONFIRM_BTN_BASE,
              background: 'var(--accent)',
              color: 'var(--bg)',
              border: '1px solid var(--accent)',
            }}
          >
            Stay
          </button>
          <button
            type="button"
            id="demo-confirm-leave"
            onClick={demoConfirmLeave}
            style={{
              ...CONFIRM_BTN_BASE,
              background: 'var(--surface2)',
              color: 'var(--text)',
              border: '1px solid var(--border)',
            }}
          >
            Leave
          </button>
        </div>
      </div>
    </div>
  );
}

export function DemoModal() {
  const demo = useStore(dashboardStore, (s) => s.demo);
  return demo.confirmVisible ? <DemoConfirmDialog /> : null;
}
