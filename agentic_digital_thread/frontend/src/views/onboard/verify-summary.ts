// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Verify step's summary (POST /api/onboard/verify, agents/onboard.py
 * onboard_verify), shared by the wizard's line and the chat's verify card.
 *
 * It says what the check counted. A database check (PostgreSQL) lists tables
 * and totals rows. Every other connection type is checked in the knowledge
 * graph (`check: knowledge_graph`, `rows_counted: false`): it reports the
 * entities and fields provision wrote and never a row total it didn't count.
 * An answer from an older server (no `verified`) passes on its table count. */
import type { VerifyResponse } from '../../api/types/onboard';

function counted(n: number, one: string, many: string): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
}

/** `ok` (passed or an issue) and the plain sentence(s) after the verdict. */
export function verifySummary(result: VerifyResponse): { ok: boolean; text: string } {
  const tables = Number(result.tables_accessible || 0);
  const ok = typeof result.verified === 'boolean' ? result.verified : tables > 0;
  if (result.rows_counted === false) {
    if (!ok) {
      const reason = String(result.error || 'The system is not onboarded in the knowledge graph');
      return { ok, text: /[.!?]$/.test(reason) ? reason : `${reason}.` };
    }
    const fields = Number(result.fields || 0);
    const what =
      tables > 0
        ? ` with ${counted(tables, 'entity', 'entities')}${fields > 0 ? ` and ${counted(fields, 'field', 'fields')}` : ''}`
        : '';
    const ctype = result.connection_type ? `${result.connection_type} systems` : 'this type of system';
    return {
      ok,
      text: `Onboarded in the knowledge graph${what}. Rows aren't counted for ${ctype}. The agent test reads through the system's own tool.`,
    };
  }
  const rows = Number(result.total_rows || 0);
  return {
    ok,
    text: `${counted(tables, 'table', 'tables')}, ${counted(rows, 'row', 'rows')} accessible.`,
  };
}

/** The wizard's markdown line: verdict, then the summary. */
export function verifySummaryLine(result: VerifyResponse): string {
  const { ok, text } = verifySummary(result);
  return `**${ok ? '✓ Verification passed' : '✗ Verification issue'}** — ${text}`;
}
