// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* The Verify summary says what the check counted. A system with no SQL
 * schema (MCP, REST, SiteWise, Athena, Tulip) is checked in the
 * knowledge graph and counts no rows, so its summary must not claim
 * "0 rows accessible"; a PostgreSQL check counts tables and rows. Both the
 * wizard's line and the chat's verify card use the same summary. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { ObFlow } from '../views/onboard/components/ObFlow';
import type { VerifyResponse } from '../api/types/onboard';
import { resetAgentTestRuns } from '../views/onboard/agent-test';
import { verifySummary, verifySummaryLine } from '../views/onboard/verify-summary';
import { appendChatCheckpoint, resetToWelcome, startWizard } from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GRAPH: VerifyResponse = {
  system_id: 'zeta-hub',
  verified: true,
  check: 'knowledge_graph',
  connection_type: 'REST API',
  rows_counted: false,
  tables_accessible: 4,
  fields: 29,
};

describe('verifySummary', () => {
  it('names the graph check and claims no row count for a non-SQL system', () => {
    const s = verifySummary(GRAPH);
    expect(s.ok).toBe(true);
    expect(s.text).toBe(
      "Onboarded in the knowledge graph with 4 entities and 29 fields. Rows aren't counted for REST API systems. The agent test reads through the system's own tool.",
    );
    expect(s.text).not.toMatch(/rows accessible/);
  });

  it('uses singular nouns and still passes an onboarded system with no entities', () => {
    expect(verifySummary({ ...GRAPH, tables_accessible: 1, fields: 1 }).text).toContain(
      'with 1 entity and 1 field.',
    );
    const empty = verifySummary({ ...GRAPH, tables_accessible: 0, fields: 0 });
    expect(empty.ok).toBe(true);
    expect(empty.text).toMatch(/^Onboarded in the knowledge graph\. Rows aren't counted/);
  });

  it('says what failed when the graph check is an issue', () => {
    const s = verifySummary({ ...GRAPH, verified: false, error: "System 'zeta-hub' not found in Neptune" });
    expect(s.ok).toBe(false);
    expect(s.text).toBe("System 'zeta-hub' not found in Neptune.");
  });

  it('counts tables and rows for a database check', () => {
    const s = verifySummary({
      system_id: 'omega',
      verified: true,
      check: 'database',
      rows_counted: true,
      tables_accessible: 2,
      total_rows: 1234,
    });
    expect(s).toEqual({ ok: true, text: '2 tables, 1,234 rows accessible.' });
    expect(verifySummary({ tables_accessible: 1, total_rows: 1, rows_counted: true }).text).toBe(
      '1 table, 1 row accessible.',
    );
  });

  it('reads an older server answer (no verified flag) by its table count', () => {
    expect(verifySummary({ tables_accessible: 3, total_rows: 9 })).toEqual({
      ok: true,
      text: '3 tables, 9 rows accessible.',
    });
    expect(verifySummary({ tables_accessible: 0, total_rows: 0 }).ok).toBe(false);
  });

  it('builds the wizard line from the same summary', () => {
    expect(verifySummaryLine(GRAPH)).toBe(
      "**✓ Verification passed** — Onboarded in the knowledge graph with 4 entities and 29 fields. Rows aren't counted for REST API systems. The agent test reads through the system's own tool.",
    );
    expect(verifySummaryLine({ tables_accessible: 0, total_rows: 0 })).toBe(
      '**✗ Verification issue** — 0 tables, 0 rows accessible.',
    );
  });
});

describe('chat verify card', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.useFakeTimers();
    resetAgentTestRuns();
    resetToWelcome();
    startWizard();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    resetToWelcome();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('shows the graph check without a row count', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    appendChatCheckpoint('verify', GRAPH);
    act(() => root.render(<ObFlow />));
    expect(container.textContent).toContain('Verification Passed');
    expect(container.textContent).toContain(
      'Onboarded in the knowledge graph with 4 entities and 29 fields.',
    );
    expect(container.textContent).not.toContain('rows accessible');
  });
});
