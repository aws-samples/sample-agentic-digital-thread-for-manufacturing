// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* jsx-migration W2.3 — the intelligence-summary block appended to
 * #response-area (lib/arch-store.ts onIntelligenceSummary) replaced its
 * inline onclick="switchView(...)" anchor with a data-nav="self-learning"
 * anchor plus a listener the appending module registers itself (CSP-safe,
 * same anchor convention as chat/thread-clicks.ts).
 *
 * jsdom: onIntelligenceSummary early-returns without a document. */
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

describe('intelligence-summary Self-Learning anchor (data-nav, no inline onclick)', () => {
  let responseEl: HTMLDivElement;
  let tabBtn: HTMLButtonElement;

  beforeEach(() => {
    responseEl = document.createElement('div');
    responseEl.id = 'response-area';
    document.body.appendChild(responseEl);
    tabBtn = document.createElement('button');
    tabBtn.setAttribute('data-arch-tab', 'proposed');
    document.body.appendChild(tabBtn);
  });

  afterEach(() => {
    responseEl.remove();
    tabBtn.remove();
    delete (window as { switchView?: unknown }).switchView;
  });

  it('renders the anchor with data-nav and clicking navigates (switchView + proposed tab)', () => {
    const switchView = vi.fn();
    window.switchView = switchView;
    const tabClick = vi.fn();
    tabBtn.addEventListener('click', tabClick);

    const store = createArchStore();
    store.handleArchEvent('intelligence_summary', {
      total_tool_calls: 3,
      neptune_queries: 1,
      system_queries: 2,
      graph_coverage_pct: 33,
      wasted_calls: 0,
      rules_applied: [{ id: 'n1', label: 'ERP order_id ↔ MES work_order' }],
      new_proposed_mappings: 1,
    });

    const anchor = responseEl.querySelector<HTMLAnchorElement>('a[data-nav="self-learning"]');
    expect(anchor).not.toBeNull();
    expect(anchor!.textContent).toContain('Review in Self-Learning Discoveries');
    // No inline handler strings anywhere in the appended block.
    expect(responseEl.innerHTML).not.toContain('onclick=');

    anchor!.click();
    expect(switchView).toHaveBeenCalledWith('architecture');
    expect(tabClick).toHaveBeenCalledTimes(1);
  });

  it('omits the anchor when there are no new proposed mappings', () => {
    const store = createArchStore();
    store.handleArchEvent('intelligence_summary', {
      total_tool_calls: 2,
      neptune_queries: 1,
      system_queries: 1,
      graph_coverage_pct: 50,
      wasted_calls: 0,
      rules_applied: [],
      new_proposed_mappings: 0,
    });
    expect(responseEl.querySelector('a[data-nav="self-learning"]')).toBeNull();
    // The intelligence card itself still renders.
    expect(responseEl.textContent).toContain('Query Intelligence');
  });

  it('renders rules-applied count with an escaped expandable list; no % deterministic', () => {
    const store = createArchStore();
    const hostile = '<img src=x onerror=alert(1)> & "quotes"';
    store.handleArchEvent('intelligence_summary', {
      total_tool_calls: 4,
      neptune_queries: 2,
      system_queries: 2,
      graph_coverage_pct: 50,
      wasted_calls: 2,
      rules_applied: [{ id: 'n1', label: hostile }, { id: 'n2' }],
      new_proposed_mappings: 0,
    });
    expect(responseEl.textContent).toContain('Learned rules applied: 2');
    // Labels are text nodes — the hostile label never becomes markup.
    expect(responseEl.querySelector('img')).toBeNull();
    expect(responseEl.textContent).toContain(hostile);
    // Missing label falls back to the id.
    expect(responseEl.textContent).toContain('n2');
    expect(responseEl.querySelector('details')).not.toBeNull();
    // New neutral lines; the judged metric is gone.
    expect(responseEl.textContent).toContain('Graph coverage: 2/4 knowledge-graph queries');
    expect(responseEl.textContent).toContain('Exploration steps with no matching data: 2');
    expect(responseEl.textContent).not.toContain('% deterministic');
    expect(responseEl.textContent).not.toContain('Graph-guided steps');
  });

  it('deploy-skew guard: an OLD payload (no new fields) renders without NaN/undefined', () => {
    const store = createArchStore();
    store.handleArchEvent('intelligence_summary', {
      total_tool_calls: 2,
      neptune_queries: 1,
      system_queries: 1,
      new_proposed_mappings: 0,
    });
    expect(responseEl.textContent).toContain('Learned rules applied: 0');
    // No graph_coverage_pct field → no coverage line; wasted 0 → no wasted line.
    expect(responseEl.textContent).not.toContain('Graph coverage:');
    expect(responseEl.textContent).not.toContain('Exploration steps');
    expect(responseEl.textContent).not.toContain('undefined');
    expect(responseEl.textContent).not.toContain('NaN');
  });
});
