// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Onboard store tests (jsx-migration W5.1) — pin the flow-store behaviors the
 * e2e suite depends on: the mapping-checkpoint confirm gate (.ob-confirm-btn
 * disabled semantics), Skip All Unmapped enabling it, obCollapseStep folding,
 * the _obSavedFlow snapshot restore, and the catalog store's connection-string
 * read the wizard consumes. No DOM — pure store state. */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  appendCheckpoint,
  appendLoading,
  appendMsg,
  appendToolPill,
  collapseStep,
  flowStore,
  getEntry,
  mappingConfirmBlocked,
  markLastToolPillDone,
  openDetail,
  rejectRule,
  removeEntry,
  resetToWelcome,
  restoreSaved,
  setCheckpointContent,
  skipAllUnmapped,
  skipRow,
  addRowConcept,
  removeRowTag,
  startWizard,
  updateLoadingLabel,
  type FlowEntry,
  type MappingRowVm,
} from '../views/onboard/flow-store';
import {
  catalogStore,
  getConnString,
  setBusy,
  setConnValue,
} from '../views/onboard/catalog-store';
import { ob, type ObStepContext } from '../views/onboard/state';

const noopCtx = (): ObStepContext => ({
  step: 'map',
  systemId: 'cmms',
  data: {},
  history: [],
});

const mappingRows = (): MappingRowVm[] => [
  { field: 'a.wo', tags: [{ concept: 'work-order', confidence: 0.9 }], review: false },
  { field: 'a.x', tags: [], review: true },
  { field: 'a.y', tags: [], review: true },
];

function addMappingCheckpoint(onConfirm: (id: number) => void = () => {}): number {
  return appendCheckpoint({
    title: 'CHECKPOINT — Confirm Mappings',
    content: { kind: 'mappingTable', rows: mappingRows() },
    confirmLabel: 'Confirm Mappings →',
    confirmDisabled: true,
    showSkipAll: true,
    stepContext: noopCtx(),
    onConfirm,
  });
}

function cpEntry(id: number): Extract<FlowEntry, { kind: 'checkpoint' }> {
  const e = getEntry(id);
  if (!e || e.kind !== 'checkpoint') throw new Error('not a checkpoint');
  return e;
}

beforeEach(() => {
  resetToWelcome();
  startWizard();
  ob.active = null;
});

describe('mapping checkpoint confirm gate (React state, not DOM)', () => {
  it('starts disabled with unmapped rows; Skip All Unmapped enables it', () => {
    const id = addMappingCheckpoint();
    expect(cpEntry(id).confirmDisabled).toBe(true);
    skipAllUnmapped(id);
    const e = cpEntry(id);
    expect(e.confirmDisabled).toBe(false);
    // skipped rows carry a skip tag and leave review state
    if (e.content.kind !== 'mappingTable') throw new Error('content');
    expect(e.content.rows[1].tags[0]).toMatchObject({ concept: 'skip', skip: true });
    expect(e.content.rows[1].review).toBe(false);
    // the already-mapped row is untouched
    expect(e.content.rows[0].tags[0].concept).toBe('work-order');
  });

  it('skipRow / addRowConcept / removeRowTag re-derive the gate', () => {
    const id = addMappingCheckpoint();
    skipRow(id, 1);
    expect(cpEntry(id).confirmDisabled).toBe(true); // row 2 still unmapped
    addRowConcept(id, 2, 'batch');
    expect(cpEntry(id).confirmDisabled).toBe(false);
    const e = cpEntry(id);
    if (e.content.kind !== 'mappingTable') throw new Error('content');
    expect(e.content.rows[2].tags[0]).toMatchObject({
      concept: 'batch',
      confidence: 0.5,
      manual: true,
    });
    // removing the only tag disables again
    removeRowTag(id, 2, 0);
    removeRowTag(id, 1, 0);
    expect(cpEntry(id).confirmDisabled).toBe(true);
  });

  it('setCheckpointContent re-derives the gate for re-run results', () => {
    const id = addMappingCheckpoint();
    setCheckpointContent(id, {
      kind: 'mappingTable',
      rows: [
        { field: 'a', tags: [{ concept: 'batch', confidence: 1 }], review: false },
      ],
    });
    expect(cpEntry(id).confirmDisabled).toBe(false);
  });

  it('keeps Confirm disabled when a re-run returns zero rows', () => {
    const id = addMappingCheckpoint();
    skipAllUnmapped(id);
    expect(cpEntry(id).confirmDisabled).toBe(false);
    setCheckpointContent(id, { kind: 'mappingTable', rows: [] });
    expect(cpEntry(id).confirmDisabled).toBe(true);
    expect(mappingConfirmBlocked([])).toBe(true);
    // Skip All on an empty table cannot enable it either.
    skipAllUnmapped(id);
    expect(cpEntry(id).confirmDisabled).toBe(true);
  });
});

describe('identity-rules checkpoint per-row reject', () => {
  const rules = () => [
    { concept: 'cost', source_field: 'a.unit_cost', target_system: 'erp', target_field: 'b.list_price' },
    { concept: 'work-order', source_field: 'a.wo_number', target_system: 'erp', target_field: 'b.order_ref' },
  ];

  it('rejectRule drops the row from the table AND from the step data provision reads', () => {
    const stepCtx: ObStepContext = {
      step: 'resolve',
      systemId: 'cmms',
      data: { rules: rules() },
      history: [],
    };
    let confirmedWith: unknown[] | null = null;
    const id = appendCheckpoint({
      title: 'CHECKPOINT — Identity Rules',
      content: { kind: 'rules', rules: rules(), extras: null },
      confirmLabel: 'Confirm & Provision →',
      stepContext: stepCtx,
      // Same contract as wizard.ts: onConfirm hands stepCtx.data.rules to provision.
      onConfirm: () => {
        confirmedWith = stepCtx.data.rules as unknown[];
      },
    });
    rejectRule(id, 0);
    const e = cpEntry(id);
    if (e.content.kind !== 'rules') throw new Error('content');
    expect(e.content.rules.map((r) => r.concept)).toEqual(['work-order']);
    expect((stepCtx.data.rules as { concept: string }[]).map((r) => r.concept)).toEqual([
      'work-order',
    ]);
    e.onConfirm(id);
    expect((confirmedWith as unknown as { concept: string }[]).map((r) => r.concept)).toEqual([
      'work-order',
    ]);
  });

  it('rejectRule is a no-op on non-rules checkpoints', () => {
    const id = addMappingCheckpoint();
    rejectRule(id, 0);
    const e = cpEntry(id);
    if (e.content.kind !== 'mappingTable') throw new Error('content');
    expect(e.content.rows).toHaveLength(3);
  });
});

describe('collapseStep folding', () => {
  it('wraps message-like entries into one collapsed group, in place', () => {
    appendMsg('analysis');
    const cp = addMappingCheckpoint();
    appendToolPill('t1', 'Discovering schema');
    collapseStep('Discover — CMMS');
    const entries = flowStore.get().entries;
    expect(entries).toHaveLength(1);
    expect(entries[0].kind).toBe('collapsed');
    const section = entries[0] as Extract<FlowEntry, { kind: 'collapsed' }>;
    expect(section.label).toBe('Discover — CMMS');
    expect(section.children.map((c) => c.kind)).toEqual([
      'msg',
      'checkpoint',
      'toolPill',
    ]);
    expect(section.children[1].id).toBe(cp);
  });
});

describe('loading + tool pill entries', () => {
  it('loading label updates (batch progress) and removal', () => {
    const id = appendLoading('Analyzing field semantics');
    updateLoadingLabel(id, 'Mapping fields — batch 2/5 (10 mappings so far)');
    const e = getEntry(id);
    expect(e && e.kind === 'loading' && e.label).toContain('batch 2/5');
    removeEntry(id);
    expect(getEntry(id)).toBeUndefined();
  });

  it('markLastToolPillDone marks only the last not-done pill', () => {
    appendToolPill('a', 'A');
    appendToolPill('b', 'B');
    markLastToolPillDone();
    const pills = flowStore
      .get()
      .entries.filter((e): e is Extract<FlowEntry, { kind: 'toolPill' }> => e.kind === 'toolPill');
    expect(pills.map((p) => p.done)).toEqual([false, true]);
    markLastToolPillDone();
    const pills2 = flowStore
      .get()
      .entries.filter((e): e is Extract<FlowEntry, { kind: 'toolPill' }> => e.kind === 'toolPill');
    expect(pills2.map((p) => p.done)).toEqual([true, true]);
  });
});

describe('_obSavedFlow snapshot-as-store-state (detail back-nav)', () => {
  it('openDetail snapshots the flow; restoreSaved brings it back', () => {
    appendMsg('hello');
    const before = flowStore.get().entries;
    openDetail({ id: 'cmms', name: 'CMMS' });
    expect(flowStore.get().screen).toBe('detail');
    expect(restoreSaved()).toBe(true);
    const s = flowStore.get();
    expect(s.screen).toBe('flow');
    expect(s.mode).toBe('wizard');
    expect(s.entries).toEqual(before);
    expect(s.saved).toBeNull();
    // nothing saved → restore reports false (caller falls back to obReset)
    expect(restoreSaved()).toBe(false);
  });
});

describe('catalog store: wizard UI state', () => {
  beforeEach(() => {
    catalogStore.set({
      connValues: {},
      connOpen: {},
      testResults: {},
      editOpen: {},
      busy: {},
      connectNewOpen: false,
      connectNewBusy: false,
      connectNewError: null,
    });
  });

  it('getConnString prefers the edited input value over the caller catalog value', () => {
    expect(getConnString('cmms', 'pg://catalog')).toBe('pg://catalog');
    setConnValue('cmms', 'pg://edited');
    expect(getConnString('cmms', 'pg://catalog')).toBe('pg://edited');
    expect(getConnString('other', 'fallback')).toBe('fallback');
  });

  it('retains per-card busy state without duplicating Query-owned systems', () => {
    setBusy('cmms', true);
    expect(catalogStore.get().busy.cmms).toBe(true);
    expect(catalogStore.get()).not.toHaveProperty('systems');
    expect(catalogStore.get()).not.toHaveProperty('phase');
  });
});
