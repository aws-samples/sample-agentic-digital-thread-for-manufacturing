// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — frontend half of the tool-result outcome parity pin.
 *
 * classifyToolResult (HUD / trace / graph outcome) and the server's
 * query_intelligence.is_failed_result read the SAME fixture
 * (fixtures/tool-result-outcome-cases.json; the server half is
 * tests/unit/test_tool_result_outcome_parity.py).
 *
 * Evidence: a live ERP answer — eight successful queries, an honest "no
 * suppliers linked to batches with quality failures" conclusion — rendered a
 * red 'failed' node because "does not exist" / "no results" appeared deep in
 * its prose. Markers now count only in the result's envelope head, and every
 * non-success outcome carries a human-readable reason. */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import {
  buildInvCallModal,
  classifyToolResult,
  classifyToolResultDetailed,
  envelopeHead,
  ENVELOPE_HEAD_CHARS,
  isTransientError,
  type ArchNode,
} from '../lib/arch-store';
import { withReason } from '../views/architecture/ToolHud';
import { installArchStoreDisposal, trackedArchStore as createArchStore } from './arch-store-tracking';

installArchStoreDisposal();

type Case = { id: string; text: string; ui: string; server: boolean; divergent?: boolean };

const FIXTURE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  'fixtures/tool-result-outcome-cases.json',
);
const CASES = (JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: Case[] }).cases;
const ERP = CASES.find((c) => c.id === 'erp-honest-absence-verbatim')!;

describe('shared outcome fixture (parity with query_intelligence.is_failed_result)', () => {
  it.each(CASES.map((c) => [c.id, c] as const))('%s', (_id, c) => {
    expect(classifyToolResult(c.text)).toBe(c.ui);
    // Where the UI and server verdicts differ, the fixture must declare it.
    if (['failed', 'empty'].includes(c.ui) !== c.server) expect(c.divergent).toBe(true);
  });

  it('pins the live ERP answer as success at its observed 43.1s duration', () => {
    expect(classifyToolResult(ERP.text, 43_100, undefined, 'erp___query_erp')).toBe('success');
    expect(isTransientError(ERP.text)).toBe(false);
  });
});

describe('envelopeHead', () => {
  it('is the first ENVELOPE_HEAD_CHARS characters of the trimmed text', () => {
    expect(envelopeHead('  a\nb  ')).toBe('a\nb');
    const long = 'first\n' + 'x'.repeat(500);
    expect(envelopeHead(long)).toBe(long.slice(0, ENVELOPE_HEAD_CHARS));
  });

  it('transient needles still fire on genuine short envelopes', () => {
    expect(isTransientError('McpException - MCP invocation failed: is not reachable')).toBe(true);
    expect(isTransientError('COLUMN_NOT_FOUND: line 1:8')).toBe(true);
    expect(isTransientError('Query error: boom')).toBe(true);
  });
});

describe('classifyToolResultDetailed reasons', () => {
  it('names the matched marker for failed and empty envelopes', () => {
    expect(classifyToolResultDetailed('Error: connection refused')).toEqual({
      outcome: 'failed',
      reason: 'the result opens with the error marker "Error:"',
    });
    expect(classifyToolResultDetailed('No records found.').reason).toContain('"No records"');
    expect(classifyToolResultDetailed('').reason).toBe('the tool returned an empty result');
    expect(classifyToolResultDetailed('3 records found\n| a |').reason).toBe('');
  });

  it('explains timeouts and learning-write statuses', () => {
    const t = classifyToolResultDetailed('Error: gateway gave up', 61_000);
    expect(t.outcome).toBe('timeout');
    expect(t.reason).toContain('after 61s');
    const w = classifyToolResultDetailed('x', 10, { status: 'rejected' }, 'kg___save_value_mapping');
    expect(w).toEqual({ outcome: 'failed', reason: 'the learning write reported status "rejected"' });
  });
});

describe('failure reason reaches the trace, HUD and call modal', () => {
  it('a failed call carries its reason on the HUD row, trace row, and modal', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'q' });
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { question: 'x' } });
    store.handleArchEvent('tool_result', {
      tool: 'erp___query_erp',
      summary: 'Error: relation "lots" does not exist',
      input: {},
    });
    const st = store.getState();
    const hud = st.hud.filter((h) => h.kind === 'tool').slice(-1)[0]!;
    expect(hud.outcome).toBe('failed');
    expect(hud.outcomeReason).toContain('"Error:"');
    expect(withReason(hud.errorLine, hud.outcomeReason)).toContain('Why: the result opens with');
    const row = st.trace.find((t) => t.outcome === 'failed')!;
    expect(row.outcomeReason).toContain('"Error:"');
    const errNode = st.nodes.find((n) => n.outcome === 'failed') as ArchNode;
    expect(buildInvCallModal(errNode)).toContain('**Outcome:** failed — classified because');
  });

  it('the live ERP answer renders as a normal finding, not a failed node', () => {
    const store = createArchStore();
    store.handleArchEvent('user_msg', { question: 'suppliers linked to failed batches' });
    store.handleArchEvent('tool_start', { tool: 'erp___query_erp', input: { question: 'x' } });
    store.handleArchEvent('tool_result', { tool: 'erp___query_erp', summary: ERP.text, input: {}, elapsed_ms: 43_100 });
    const st = store.getState();
    expect(st.nodes.some((n) => n.outcome === 'failed')).toBe(false);
    expect(st.hud.filter((h) => h.kind === 'tool').slice(-1)[0]!.outcome).toBe('success');
    expect(st.trace.some((t) => t.outcome)).toBe(false);
  });

  it('withReason composes titles and bare reasons', () => {
    expect(withReason(undefined, undefined)).toBeUndefined();
    expect(withReason('Call failed', undefined)).toBe('Call failed');
    expect(withReason(undefined, 'r')).toBe('Why: r');
  });
});
