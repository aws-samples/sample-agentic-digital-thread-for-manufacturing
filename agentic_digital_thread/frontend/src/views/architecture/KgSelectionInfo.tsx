// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* KG selection-details panel for the Knowledge Graph tab (jsx-migration W6).
 *
 * Replaces the tap handler's imperative #kg-selection-info selPanel.innerHTML=
 * write in neptune-graph.ts — which interpolated graph-sourced labels and
 * property values UNESCAPED — with a state-driven component over
 * kgSelectionStore. All graph data now renders as JSX text (React-escaped).
 * Markup/inline styles kept byte-comparable to the old string build. */
import { useStore } from '../../lib/store';
import { kgSelectionStore } from './neptune-graph';

export function KgSelectionInfo() {
  const sel = useStore(kgSelectionStore);
  return (
    <div
      id="kg-selection-info"
      style={{ fontSize: '0.8rem', color: 'var(--text2)', lineHeight: 1.6 }}
    >
      {sel === null ? (
        <em style={{ color: 'var(--muted)' }}>Choose a node to see details</em>
      ) : (
        <>
          <div style={{ marginBottom: '0.5rem' }}>
            <strong style={{ color: 'var(--accent)' }}>[{sel.label}]</strong> {sel.name}
          </div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text2)' }}>
            {sel.props.map(([k, v]) => (
              <div key={k}>
                <span style={{ color: 'var(--muted)' }}>{k}:</span> {v}
              </div>
            ))}
          </div>
          <div style={{ marginTop: '0.5rem', fontSize: '0.7rem', color: 'var(--muted)' }}>
            Outgoing: {sel.outgoing} edges | Incoming: {sel.incoming} edges
          </div>
        </>
      )}
    </div>
  );
}
