// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Cited-system ID derivation for the feedback POST.
 *
 * `cited_systems` must carry system IDS — the same unit the backend diffs
 * against (`metrics_registry.systems_from_tool_names`) — never display
 * labels (getSystemLabel output, which can contain newlines). Pins here:
 * the derivation shape (gateway prefix → id, `_`→`-`, distinct, sorted,
 * non-prefixed skipped) and the AssistantTurn wiring (source-level: the
 * FeedbackRow prop derives from citation TOOL names via citedSystemIds,
 * and label-mapping never reappears). The cross-language parity pin — the
 * REAL js function against the REAL python derivation on one fixture —
 * lives in tests/unit/test_feedback_cited_systems_parity.py. */
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { citedSystemIds } from '../chat/cited-systems';

describe('citedSystemIds — feedback cited_systems unit (H1)', () => {
  it('derives ids from gateway-prefixed tool names (_ → -, distinct, sorted)', () => {
    expect(
      citedSystemIds([
        'mes_mil___query_mes_mil',
        'erp___query_erp',
        'mes_mil___get_schema',
        'lake-athena___athena_run_query',
      ]),
    ).toEqual(['erp', 'lake-athena', 'mes-mil']);
  });

  it('skips non-prefixed (orchestrator-own) tools — same rule as the backend', () => {
    expect(citedSystemIds(['explore_graph', 'query_mes', 'get_system_schema'])).toEqual([]);
  });

  it('lowercases defensively — output for gateway-shaped names is id-shaped', () => {
    // The helper MIRRORS the backend derivation (no extra filtering — the
    // server drops non-id-shaped entries); for gateway-conventional tool
    // names the output is lowercase ids, never label text.
    const ids = citedSystemIds(['MES_MIL___q', 'erp___q']);
    expect(ids).toEqual(['erp', 'mes-mil']);
    for (const id of ids) expect(id).toMatch(/^[a-z0-9-]+$/);
  });

  it('tolerates junk input', () => {
    expect(citedSystemIds([])).toEqual([]);
    expect(citedSystemIds([null as unknown as string, 42 as unknown as string])).toEqual([]);
  });
});

describe('AnswerFooter wiring (source pin) — ids, never labels', () => {
  const src = readFileSync(
    path.resolve(process.cwd(), 'src/chat/AnswerFooter.tsx'),
    'utf-8',
  );

  it('citedSystems derives from citation TOOL names via citedSystemIds', () => {
    // `shown`: this turn's citations plus the earlier ([E<k>]) ones the
    // answer cites — the chips the footer renders.
    expect(src).toContain('const shown = [...current, ...earlier];');
    expect(src).toContain('citedSystems={citedSystemIds(shown.map((c) => c.tool))}');
  });

  it('label-sending never reappears on the citedSystems prop', () => {
    expect(src).not.toMatch(/citedSystems=\{[^}]*\.label/);
  });
});
