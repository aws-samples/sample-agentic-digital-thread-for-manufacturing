// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* AI-analysis panel for the Knowledge Graph tab (jsx-migration W2.3).
 *
 * Replaces the imperative #kg-ai-analysis panel.innerHTML= writes in
 * neptune-graph.ts#requestKgAiAnalysis with a state-driven component over
 * kgAnalysisStore: idle → analyzing → ready | none | error. The `ready`
 * analysis is raw LLM markdown and renders through the shared sanitized
 * <Md/> sink (marked + DOMPurify) — the security contract covered by
 * src/test/kg-analysis-sanitize.test.ts. */
import { useStore } from '../../lib/store';
import { Md } from '../../shared/ui/Md';
import { kgAnalysisStore } from './neptune-graph';

const MUTED: React.CSSProperties = { color: 'var(--muted)' };

export function NeptuneAnalysisPanel() {
  const state = useStore(kgAnalysisStore);
  return (
    <div id="kg-ai-analysis" style={{ fontSize: '0.8rem', color: 'var(--text2)', lineHeight: 1.6 }}>
      {state.status === 'idle' && <em style={MUTED}>Select nodes to get AI explanation</em>}
      {state.status === 'analyzing' && <em style={MUTED}>Analyzing...</em>}
      {state.status === 'ready' && (
        <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          <Md text={state.analysis} />
        </div>
      )}
      {state.status === 'none' && <em style={MUTED}>No analysis available</em>}
      {state.status === 'error' && (
        <em style={MUTED}>
          Analysis unavailable{state.message ? `: ${state.message}` : ''}
        </em>
      )}
    </div>
  );
}
