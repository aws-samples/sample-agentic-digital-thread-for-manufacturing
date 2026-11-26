// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests — pure per-claim provenance helpers (src/chat/provenance.ts).
 * Mirrors the frozen D1 wire contract emitted by agents/provenance.py:
 * sparse records are the NORM (report-only default), so honest-absence
 * behavior (fallback chip label, 'unverified' badge, empty sentences) is
 * pinned as hard as the enriched paths. */
import { describe, expect, it } from 'vitest';

import {
  chipLabel,
  recordForCitation,
  resultShapeSentence,
  scopeSentences,
  verificationBadge,
  type ProvenanceRecord,
} from '../chat/provenance';

/** A fully enriched D1 record (every field the server can populate). */
function fullRecord(overrides: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 3,
    tool_id: 'toolu_abc123',
    source_system: 'erp',
    tool_name: 'erp___query_erp',
    entity: { table: 'work_orders', columns: ['status', 'due_date'] },
    scope: {
      filters: ['status is one of open, blocked'],
      window: { start: '2026-07-01T00:00:00Z', end: '2026-08-01T00:00:00Z' },
    },
    query_ref: 'q-3f9c2b81ab04',
    result_shape: { rows: 42, truncated: false, partial_reason: '' },
    kg_route: {
      rules_applied: [{ id: 'subj-1', label: 'ERP work-order id mapping' }],
      value_mappings: [],
    },
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'partial',
    ...overrides,
  };
}

/** A sparse record — only the D1-required keys, honest-empty enrichment
 * (the report-only norm; matches the server's empty spellings). */
function sparseRecord(overrides: Partial<ProvenanceRecord> = {}): ProvenanceRecord {
  return {
    citation_num: 1,
    tool_id: 'toolu_sparse',
    source_system: 'erp',
    tool_name: 'erp___query_erp',
    entity: { table: '', columns: [] },
    scope: { filters: [], window: null },
    query_ref: 'q-000000000000',
    result_shape: { rows: null, truncated: false, partial_reason: '' },
    kg_route: { rules_applied: [], value_mappings: [] },
    captured_at: '2026-08-03T19:00:00Z',
    verification: 'unverified',
    ...overrides,
  };
}

describe('recordForCitation (D2 dual binding)', () => {
  const records = [
    sparseRecord({ citation_num: 1, tool_id: 'toolu_a' }),
    sparseRecord({ citation_num: 2, tool_id: 'toolu_b' }),
  ];

  it('binds by tool_id first (primary), even when nums disagree', () => {
    // Deliberately mismatched num — tool_id must win.
    const hit = recordForCitation(records, { num: 1, toolId: 'toolu_b' });
    expect(hit?.tool_id).toBe('toolu_b');
    expect(hit?.citation_num).toBe(2);
  });

  it('falls back to citation_num when no toolId is supplied', () => {
    const hit = recordForCitation(records, { num: 2 });
    expect(hit?.tool_id).toBe('toolu_b');
  });

  it('falls back to citation_num when the toolId matches nothing', () => {
    const hit = recordForCitation(records, { num: 1, toolId: 'toolu_zzz' });
    expect(hit?.tool_id).toBe('toolu_a');
  });

  it('returns null when nothing binds, and for empty/missing records', () => {
    expect(recordForCitation(records, { num: 9 })).toBeNull();
    expect(recordForCitation([], { num: 1 })).toBeNull();
    expect(recordForCitation(null, { num: 1 })).toBeNull();
    expect(recordForCitation(undefined, { num: 1 })).toBeNull();
  });
});

describe('chipLabel (Tier-1 honesty)', () => {
  it('enriched record: source_system + table + compact window', () => {
    expect(chipLabel(fullRecord(), 'ERP')).toBe(
      'erp · work_orders · 2026-07-01 to 2026-08-01',
    );
  });

  it('table without window, and window without table', () => {
    expect(chipLabel(fullRecord({ scope: { filters: [], window: null } }), 'ERP'))
      .toBe('erp · work_orders');
    expect(
      chipLabel(fullRecord({ entity: { table: '', columns: [] } }), 'ERP'),
    ).toBe('erp · 2026-07-01 to 2026-08-01');
  });

  it('one-sided windows render since/until', () => {
    const since = fullRecord({
      entity: { table: '', columns: [] },
      scope: { filters: [], window: { start: '2026-07-01T00:00:00Z', end: '' } },
    });
    expect(chipLabel(since, 'ERP')).toBe('erp · since 2026-07-01');
    const until = fullRecord({
      entity: { table: '', columns: [] },
      scope: { filters: [], window: { start: '', end: '2026-08-01T00:00:00Z' } },
    });
    expect(chipLabel(until, 'ERP')).toBe('erp · until 2026-08-01');
  });

  it('sparse record (no table, no window) returns the fallback VERBATIM', () => {
    expect(chipLabel(sparseRecord(), 'MES\nMilwaukee')).toBe('MES\nMilwaukee');
  });

  it('null record returns the fallback verbatim', () => {
    expect(chipLabel(null, 'Knowledge Graph')).toBe('Knowledge Graph');
  });

  it('empty source_system with enrichment heads with the fallback', () => {
    const rec = fullRecord({ source_system: '' });
    expect(chipLabel(rec, 'ERP')).toBe('ERP · work_orders · 2026-07-01 to 2026-08-01');
  });
});

describe('scopeSentences (Tier-2 plain language)', () => {
  it('renders filters + window + queried-at from a full record', () => {
    expect(scopeSentences(fullRecord())).toEqual([
      'rows where status is one of open, blocked',
      'data observed 2026-07-01 to 2026-08-01',
      'queried at 2026-08-03T19:00:00Z',
    ]);
  });

  it('multiple filters each get a sentence; empties are skipped', () => {
    const rec = fullRecord({
      scope: { filters: ['a = 1', '', 'b in (2, 3)'], window: null },
    });
    const sentences = scopeSentences(rec);
    expect(sentences).toContain('rows where a = 1');
    expect(sentences).toContain('rows where b in (2, 3)');
    expect(sentences.filter((s) => s.startsWith('rows where'))).toHaveLength(2);
  });

  it('sparse record yields only the queried-at line; null yields []', () => {
    expect(scopeSentences(sparseRecord())).toEqual([
      'queried at 2026-08-03T19:00:00Z',
    ]);
    expect(scopeSentences(null)).toEqual([]);
  });

  it('hostile filter strings pass through as plain strings (text nodes)', () => {
    const hostile = '<img src=x onerror=alert(1)> AND col = SELECT 1';
    const rec = fullRecord({ scope: { filters: [hostile], window: null } });
    // Pure string pipeline — no interpretation, no markup handling; the
    // consumer renders this as a React text node (sink policy).
    expect(scopeSentences(rec)[0]).toBe(`rows where ${hostile}`);
  });
});

describe('resultShapeSentence (truncation honesty)', () => {
  it.each([
    ['row_limit', 'result was capped at the row limit'],
    ['byte_limit', 'result was capped at the size limit'],
    ['cell_limit', 'result was capped at the cell limit'],
    ['column_limit', 'result was capped at the column limit'],
  ])('%s renders its capped wording', (reason, wording) => {
    const rec = fullRecord({
      result_shape: { rows: 50, truncated: true, partial_reason: reason },
    });
    expect(resultShapeSentence(rec)).toBe(
      `first 50 rows of a larger result — ${wording}`,
    );
  });

  it('unknown partial_reason still reads as capped (generic wording)', () => {
    const rec = fullRecord({
      result_shape: { rows: 5, truncated: true, partial_reason: 'novel_cap' },
    });
    expect(resultShapeSentence(rec)).toBe(
      'first 5 rows of a larger result — result was capped',
    );
  });

  it('truncated without a row count is honest about partiality', () => {
    const rec = fullRecord({
      result_shape: { rows: null, truncated: true, partial_reason: 'byte_limit' },
    });
    expect(resultShapeSentence(rec)).toBe(
      'partial result — result was capped at the size limit',
    );
  });

  it('a partial_reason alone (truncated false) still reads as capped', () => {
    const rec = fullRecord({
      result_shape: { rows: 10, truncated: false, partial_reason: 'row_limit' },
    });
    expect(resultShapeSentence(rec)).toBe(
      'first 10 rows of a larger result — result was capped at the row limit',
    );
  });

  it('un-capped results state the count; zero and one are exact', () => {
    expect(resultShapeSentence(fullRecord())).toBe('42 rows returned');
    expect(
      resultShapeSentence(
        fullRecord({ result_shape: { rows: 1, truncated: false, partial_reason: '' } }),
      ),
    ).toBe('1 row returned');
    expect(
      resultShapeSentence(
        fullRecord({ result_shape: { rows: 0, truncated: false, partial_reason: '' } }),
      ),
    ).toBe('0 rows returned');
  });

  it('no captured shape ⇒ empty string (honest absence), incl. null record', () => {
    expect(resultShapeSentence(sparseRecord())).toBe('');
    expect(resultShapeSentence(null)).toBe('');
    const noShape = { ...fullRecord() };
    delete noShape.result_shape;
    expect(resultShapeSentence(noShape)).toBe('');
  });
});

/* A call's chip says "no data" when it reported none, while its record can
 * count the rows its captured evidence holds (another query the same call
 * ran, in the cases seen). The sentence states both facts, so they do not
 * read as a contradiction, and claims no cause the client cannot see. */
describe('resultShapeSentence when the asked lookup returned no rows', () => {
  const shape = (rows: number | null, truncated = false, partial_reason = '') =>
    fullRecord({ result_shape: { rows, truncated, partial_reason } });

  it('says the source reported no data and its captured evidence holds the counted rows', () => {
    expect(resultShapeSentence(shape(50), 'empty')).toBe(
      'This source reported no data, but its captured evidence holds 50 rows.',
    );
    expect(resultShapeSentence(shape(1), 'empty')).toBe(
      'This source reported no data, but its captured evidence holds 1 row.',
    );
    expect(resultShapeSentence(shape(30, true, 'row_limit'), 'empty')).toBe(
      'This source reported no data, but its captured evidence holds the first 30 rows of a larger result.',
    );
  });

  it('keeps every other case as it was', () => {
    // No rows counted: nothing to reconcile.
    expect(resultShapeSentence(shape(0), 'empty')).toBe('0 rows returned');
    expect(resultShapeSentence(shape(null), 'empty')).toBe('');
    expect(resultShapeSentence(shape(null, true, 'byte_limit'), 'empty')).toBe(
      'partial result — result was capped at the size limit',
    );
    expect(resultShapeSentence(null, 'empty')).toBe('');
    // Other outcomes, and no outcome, keep the plain count.
    for (const outcome of [null, undefined, 'failed', 'timeout'] as const) {
      expect(resultShapeSentence(shape(50), outcome)).toBe('50 rows returned');
    }
    expect(resultShapeSentence(shape(30, true, 'row_limit'), 'failed')).toBe(
      'first 30 rows of a larger result — result was capped at the row limit',
    );
  });
});

describe('verificationBadge (D4 honest absence)', () => {
  it('passes through the exact wire values', () => {
    expect(verificationBadge(fullRecord({ verification: 'verified' }))).toBe('verified');
    expect(verificationBadge(fullRecord({ verification: 'partial' }))).toBe('partial');
    expect(verificationBadge(fullRecord({ verification: 'unverified' }))).toBe(
      'unverified',
    );
  });

  it('null record ⇒ unverified (missing record is never upgraded)', () => {
    expect(verificationBadge(null)).toBe('unverified');
  });

  it('unknown/hostile wire values normalize to unverified, never trusted', () => {
    expect(verificationBadge(fullRecord({ verification: 'VERIFIED' }))).toBe(
      'unverified',
    );
    expect(verificationBadge(fullRecord({ verification: 'totally-legit' }))).toBe(
      'unverified',
    );
    expect(verificationBadge(fullRecord({ verification: '' }))).toBe('unverified');
  });
});
