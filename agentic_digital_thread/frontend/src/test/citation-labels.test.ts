// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Sources-row labels for knowledge-graph gateway calls.
 *
 * Workshop walk 2026-10-03: a root-cause answer listed three Sources chips
 * that all read "Digital Thread" (the capitalised gateway target id), which
 * told the reader nothing. A graph-target call now reads "Knowledge graph ·
 * <what it did>". Numbering is untouched: the same calls are numbered in the
 * same order (provenance D2), only the label text changes. */
import { describe, expect, it } from 'vitest';

import { createThreadStore } from '../chat/thread-store';
import { getCitationLabel } from '../chat/tool-labels';

describe('getCitationLabel', () => {
  it('names knowledge-graph gateway calls by what they did', () => {
    expect(getCitationLabel('digital-thread___get_system_schema')).toBe('Knowledge graph · system schema');
    expect(getCitationLabel('digital-thread___verify_relationship')).toBe('Knowledge graph · verify relationship');
    expect(getCitationLabel('digital-thread___save_value_mapping')).toBe('Knowledge graph · save value mapping');
    expect(getCitationLabel('digital-thread___save_correlation')).toBe('Knowledge graph · save correlation');
    expect(getCitationLabel('digital-thread___explore_graph')).toBe('Knowledge graph · query');
  });

  it('treats a prefixed explore_graph on any target as a graph query', () => {
    expect(getCitationLabel('kg___explore_graph')).toBe('Knowledge graph · query');
  });

  it('never reads "Digital Thread"', () => {
    for (const op of ['get_system_schema', 'save_correlation', 'some_new_tool']) {
      expect(getCitationLabel(`digital-thread___${op}`)).not.toMatch(/digital thread/i);
    }
  });

  it('names system-agent sources through the shared resolver', () => {
    expect(getCitationLabel('erp___query_erp')).toBe('ERP');
    expect(getCitationLabel('cmms___query_cmms')).toBe('CMMS');
    expect(getCitationLabel('query_mes')).toBe('MES');
    expect(getCitationLabel('get_system_schema')).toBe('Get Schema');
  });
});

describe('thread store Sources row with graph gateway calls', () => {
  it('labels graph calls distinctly and keeps the numbering order', () => {
    const store = createThreadStore();
    store.startTurn('why did the lot fail?');
    const calls = [
      'digital-thread___get_system_schema',
      'erp___query_erp',
      'digital-thread___verify_relationship',
      'cmms___query_cmms',
      'digital-thread___save_correlation',
    ];
    calls.forEach((tool, i) => {
      store.applyEvent('tool_start', { tool, tool_id: `t${i}`, input: {} });
      store.applyEvent('tool_result', { tool, tool_id: `t${i}`, input: 'ok', summary: 'rows' });
    });
    store.applyEvent('done', {});
    const { turns } = store.getState();
    const cites = turns[turns.length - 1].assistant.citations;
    expect(cites.map((c) => [c.num, c.tool, c.label])).toEqual([
      [1, 'digital-thread___get_system_schema', 'Knowledge graph · system schema'],
      [2, 'erp___query_erp', 'ERP'],
      [3, 'digital-thread___verify_relationship', 'Knowledge graph · verify relationship'],
      [4, 'cmms___query_cmms', 'CMMS'],
      [5, 'digital-thread___save_correlation', 'Knowledge graph · save correlation'],
    ]);
  });
});
