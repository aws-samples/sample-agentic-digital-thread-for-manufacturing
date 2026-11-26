// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — class-sectioned review panel (plan Design decision 6, task 1.3).
 * Pins: the pure sectionOf classification (everything rule-grade → mapping so
 * no class can vanish from review), the fixed section DOM order mapping →
 * annotations → insights → drift, consequence-desc ordering WITHIN the
 * mapping section, the insights <details> collapsed by default, and empty
 * sections rendering nothing (no empty headers). sl-cards.test.tsx fixture
 * idiom. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import { proposalsStore } from '../views/architecture/proposals';
import { ProposalsList } from '../views/architecture/ProposalsPanel';
import { sectionOf, SL_TYPE_META } from '../views/architecture/SlCards';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
  proposalsStore.set({
    phase: 'idle',
    mappings: [],
    error: '',
    statusFilter: 'pending',
    optimistic: {},
    ruleLifecycle: {},
    guidancePhase: 'idle',
    guidance: [],
    guidanceError: '',
    edit: null,
  });
});

function render(el: React.ReactElement) {
  act(() => {
    root.render(el);
  });
}

const base: ProposedMapping = {
  mapping_id: 'm-1',
  proposed_type: 'value_mapping',
  status: 'pending',
  confidence: '0.8',
  from_system: 'erp',
  from_field: 'orders.status',
  from_value: 'SHIPPED',
  to_system: 'mes',
  to_field: 'runs.state',
  to_value: 'DONE',
};

/** Mixed fixture covering all four sections, deliberately interleaved so the
 * section partition (not the input order) determines the DOM layout. */
const mixed: ProposedMapping[] = [
  { ...base, mapping_id: 'drift-1', proposed_type: 'schema_drift' },
  { ...base, mapping_id: 'insight-1', proposed_type: 'insight', query_context: 'a finding' },
  { ...base, mapping_id: 'vm-low', consequence: 1.0 },
  { ...base, mapping_id: 'anno-1', proposed_type: 'field_annotation', query_context: 'a note' },
  { ...base, mapping_id: 'jp-high', proposed_type: 'join_path', consequence: 9.0 },
  { ...base, mapping_id: 'xw-mid', proposed_type: 'crosswalk', consequence: 5.0 },
];

function setReady(mappings: ProposedMapping[]) {
  proposalsStore.set({ ...proposalsStore.get(), phase: 'ready', mappings });
}

describe('sectionOf (pure classification — Design decision 6)', () => {
  it('routes annotation/insight/drift to their sections', () => {
    expect(sectionOf({ ...base, proposed_type: 'field_annotation' })).toBe('annotation');
    expect(sectionOf({ ...base, proposed_type: 'insight' })).toBe('insight');
    expect(sectionOf({ ...base, proposed_type: 'schema_drift' })).toBe('drift');
  });

  it('EVERYTHING else is rule-grade mapping — no class can vanish from review', () => {
    for (const t of [
      'value_mapping',
      'crosswalk',
      'join_path',
      'identity_rule',
      'correlation',
      'concept_mapping',
      'field_concept_mint',
      'some_unknown_future_type',
    ]) {
      expect(sectionOf({ ...base, proposed_type: t })).toBe('mapping');
    }
    // Legacy rows without a proposed_type default to value_mapping → mapping.
    expect(sectionOf({ ...base, proposed_type: undefined })).toBe('mapping');
  });

  it('SL_TYPE_META carries the schema_drift entry', () => {
    expect(SL_TYPE_META.schema_drift).toEqual({ label: 'Schema Drift', color: '#b91c1c' });
  });
});

describe('ProposalsList: four ordered sections (mixed fixture)', () => {
  it('renders section DOM order mapping → annotations → insights → drift', () => {
    setReady(mixed);
    render(<ProposalsList />);

    const blocks = Array.from(
      container.querySelectorAll('#self-learning-cards > section, #self-learning-cards > details'),
    );
    expect(
      blocks.map((el) =>
        el.tagName === 'DETAILS' ? 'insights' : el.querySelector('.sl-section-head')!.textContent,
      ),
    ).toEqual(['Mapping decisions', 'Field annotations', 'insights', 'Schema drift']);

    // Every card is inside its section, in overall document order:
    // mapping cards first, then the annotation, insight, and drift cards.
    const ids = Array.from(container.querySelectorAll<HTMLElement>('.sl-card')).map(
      (card) => card.dataset.mappingId,
    );
    expect(ids).toEqual(['jp-high', 'xw-mid', 'vm-low', 'anno-1', 'insight-1', 'drift-1']);
  });

  it('mapping section is consequence-descending (existing sort composition)', () => {
    setReady(mixed);
    render(<ProposalsList />);
    const mappingIds = Array.from(
      container.querySelectorAll<HTMLElement>('.sl-section--mapping .sl-card'),
    ).map((card) => card.dataset.mappingId);
    expect(mappingIds).toEqual(['jp-high', 'xw-mid', 'vm-low']);
  });

  it('insights <details> is collapsed by default and contains the insight card', () => {
    setReady(mixed);
    render(<ProposalsList />);
    const details = container.querySelector<HTMLDetailsElement>('details.sl-insights-collapse')!;
    expect(details).not.toBeNull();
    expect(details.hasAttribute('open')).toBe(false);
    expect(details.querySelector('summary')!.textContent).toBe(
      'Insights — informational, no action needed',
    );
    expect(details.querySelector('.sl-card[data-mapping-id="insight-1"]')).not.toBeNull();
  });

  it('drift section carries the re-onboard hint text', () => {
    setReady(mixed);
    render(<ProposalsList />);
    const drift = container.querySelector('.sl-section--drift')!;
    expect(drift.querySelector('.sl-section-hint')!.textContent).toBe(
      'Resolves automatically when the system is re-onboarded.',
    );
    expect(drift.querySelector('.sl-card[data-mapping-id="drift-1"]')).not.toBeNull();
  });
});

describe('ProposalsList: empty sections render nothing', () => {
  it('an insights-only fixture renders no mapping/annotation/drift headers', () => {
    setReady([
      { ...base, mapping_id: 'insight-only', proposed_type: 'insight', query_context: 'x' },
    ]);
    render(<ProposalsList />);
    expect(container.querySelector('.sl-section--mapping')).toBeNull();
    expect(container.querySelector('.sl-section--annotation')).toBeNull();
    expect(container.querySelector('.sl-section--drift')).toBeNull();
    expect(container.querySelector('.sl-section-head')).toBeNull();
    const details = container.querySelector<HTMLDetailsElement>('details.sl-insights-collapse')!;
    expect(details.querySelector('.sl-card[data-mapping-id="insight-only"]')).not.toBeNull();
  });

  it('a mappings-only fixture renders neither the insights collapse nor other headers', () => {
    setReady([{ ...base, mapping_id: 'vm-only' }]);
    render(<ProposalsList />);
    expect(container.querySelector('details.sl-insights-collapse')).toBeNull();
    expect(container.querySelector('.sl-section--annotation')).toBeNull();
    expect(container.querySelector('.sl-section--drift')).toBeNull();
    const heads = Array.from(container.querySelectorAll('.sl-section-head')).map(
      (el) => el.textContent,
    );
    expect(heads).toEqual(['Mapping decisions']);
  });
});

describe('ProposalsList: sections apply WITHIN the selected status chip', () => {
  it('non-pending tabs are sectioned over the same filtered rows', () => {
    proposalsStore.set({
      ...proposalsStore.get(),
      phase: 'ready',
      statusFilter: 'accepted',
      mappings: [
        { ...base, mapping_id: 'pending-vm' },
        { ...base, mapping_id: 'accepted-vm', status: 'accepted', confidence: '0.9' },
        {
          ...base,
          mapping_id: 'accepted-insight',
          status: 'accepted',
          proposed_type: 'insight',
          query_context: 'y',
        },
      ],
    });
    render(<ProposalsList />);
    // Only accepted rows render, partitioned into their sections.
    const ids = Array.from(container.querySelectorAll<HTMLElement>('.sl-card')).map(
      (card) => card.dataset.mappingId,
    );
    expect(ids).toEqual(['accepted-vm', 'accepted-insight']);
    expect(
      container.querySelector('.sl-section--mapping .sl-card[data-mapping-id="accepted-vm"]'),
    ).not.toBeNull();
    expect(
      container.querySelector(
        'details.sl-insights-collapse .sl-card[data-mapping-id="accepted-insight"]',
      ),
    ).not.toBeNull();
  });
});
