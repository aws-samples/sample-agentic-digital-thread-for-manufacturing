// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Fired-vs-loaded metric copy (T1.6 / G3): the intelligence-summary popover
 * wording must DISTINGUISH "used in this answer" (rules_applied — fired)
 * from "loaded at planning" (planned_with — loaded), and both surfaces must
 * read the ONE shared constant (lib/metric-copy.ts — reused verbatim by the
 * T3.5 results scorecard). Copy only; no state. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { AgentAnalysisPanel, buildAgentAnalysisPanelData } from '../chat/analysis-panel';
import { PLANNED_WITH_TIP, RULES_APPLIED_TIP } from '../lib/metric-copy';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe('metric-copy constants (T1.6 fired-vs-loaded)', () => {
  it('both copy strings are present (non-empty)', () => {
    expect(RULES_APPLIED_TIP.length).toBeGreaterThan(0);
    expect(PLANNED_WITH_TIP.length).toBeGreaterThan(0);
  });

  it('the two strings are distinct — they measure different things', () => {
    expect(RULES_APPLIED_TIP).not.toBe(PLANNED_WITH_TIP);
  });

  it('rules_applied copy says "used in this answer" (fired)', () => {
    expect(RULES_APPLIED_TIP.toLowerCase()).toContain('used in this answer');
  });

  it('planned_with copy says "loaded at planning" (loaded ≠ used)', () => {
    expect(PLANNED_WITH_TIP.toLowerCase()).toContain('loaded at planning');
    // The loaded-side copy must point at the fired-side wording so a reader
    // seeing planned_with move while rules_applied stays empty is told the
    // two measure different things.
    expect(PLANNED_WITH_TIP.toLowerCase()).toContain('used in this answer');
  });
});

describe('analysis panel uses the shared copy as popover titles', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('renders both tooltips from lib/metric-copy on the KG contribution parts', () => {
    const data = buildAgentAnalysisPanelData(
      [],
      { toolCallCount: 1, graphLookupCount: 0, emptyGraphLookups: 0 },
      {
        total_tool_calls: 1,
        rules_applied: [{ id: 'r1', label: 'Line id crosswalk' }],
        planned_with: { systems: 2, value_mappings: 12, authority_rules: 1 },
      },
      null,
    )!;
    act(() => root.render(<AgentAnalysisPanel data={data} />));
    const titles = Array.from(container.querySelectorAll('[title]')).map(
      (el) => el.getAttribute('title'),
    );
    expect(titles).toContain(RULES_APPLIED_TIP);
    expect(titles).toContain(PLANNED_WITH_TIP);
  });
});
