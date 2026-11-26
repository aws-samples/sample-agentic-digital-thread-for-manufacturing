// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure findings-ledger logic — DOM-free so it stays unit-testable under the
 * node vitest environment (the evals-logic.ts / admin-logic.ts convention).
 *
 * Two honesty rules live here, not in the component (ADR-0062 D6, ADR-0053):
 *   - usage is MEASURED TOKENS OR ABSENT: usageText() returns null when the
 *     server sent no measured block, and the panel then renders NO element at
 *     all. There is no zero, no estimate, and nothing denominated in money;
 *   - the 7-day generated detail is customer-derived, so a rejected steward
 *     note and a refused proposal filing map to FIXED copy keyed by the
 *     server's pinned reason — the rejected value is never echoed back.
 */
import { ApiError } from '../../../../api/client';
import type {
  AuditBudgetBlock,
  AuditRunSummary,
  EvalMeasuredUsage,
  FindingIndexEntry,
  FindingRow,
  FindingsDigest,
  FindingsImportResult,
  FindingsLedgerResponse,
} from '../../../../api/types/admin';

/** The pinned 7-value taxonomy (agents/audit_core.py TAXONOMY), in the order
 * the ledger and the digest present it. Extending it is a deliberate edit on
 * both sides. */
export const FINDING_TAXONOMY = [
  'data_gap',
  'mapping_gap',
  'schema_drift',
  'prompt_gap',
  'code_bug',
  'ux_gap',
  'golden_candidate',
] as const;

export type FindingTaxonomy = (typeof FINDING_TAXONOMY)[number];

const TAXONOMY_LABELS: Record<string, string> = {
  data_gap: 'Data gap',
  mapping_gap: 'Mapping gap',
  schema_drift: 'Schema drift',
  prompt_gap: 'Prompt gap',
  code_bug: 'Code bug',
  ux_gap: 'UX gap',
  golden_candidate: 'Golden candidate',
};

/** Steward-note ceiling — mirrors eval_findings.NOTE_MAX_CHARS so the input is
 * bounded in the browser too (the server bound is what holds). */
export const NOTE_MAX_CHARS = 500;

/** Human label for a taxonomy value; an unknown value renders verbatim. */
export function taxonomyLabel(taxonomy: string | undefined): string {
  if (!taxonomy) return '—';
  return TAXONOMY_LABELS[taxonomy] ?? taxonomy;
}

export interface LedgerGroup {
  taxonomy: string;
  label: string;
  count: number;
  entries: FindingIndexEntry[];
}

/** Taxonomy-grouped ledger rows in the pinned order; empty groups are omitted
 * (a taxonomy with no findings is not a row, it is an absence). */
export function ledgerGroups(ledger: FindingsLedgerResponse | undefined): LedgerGroup[] {
  const groups = ledger?.groups ?? {};
  const out: LedgerGroup[] = [];
  for (const taxonomy of FINDING_TAXONOMY) {
    const entries = groups[taxonomy] ?? [];
    if (!entries.length) continue;
    out.push({ taxonomy, label: taxonomyLabel(taxonomy), count: entries.length, entries });
  }
  return out;
}

export interface DigestCount {
  taxonomy: string;
  label: string;
  count: number;
}

/** Non-zero digest counts in the pinned taxonomy order. These are MEASURED
 * counts of persisted rows — unlike usage, a zero here is meaningful, it is
 * just not worth a chip. */
export function digestCounts(digest: FindingsDigest | undefined): DigestCount[] {
  const counts = digest?.taxonomy_counts ?? {};
  const out: DigestCount[] = [];
  for (const taxonomy of FINDING_TAXONOMY) {
    const count = counts[taxonomy];
    if (typeof count !== 'number' || count <= 0) continue;
    out.push({ taxonomy, label: taxonomyLabel(taxonomy), count });
  }
  return out;
}

/** MEASURED token text, or null when the server sent no measured usage.
 * null means "render nothing" — never a zero, never a money figure. */
export function usageText(usage: EvalMeasuredUsage | undefined | null): string | null {
  if (!usage) return null;
  const { input_tokens: input, output_tokens: output } = usage;
  if (typeof input !== 'number' || typeof output !== 'number') return null;
  if (!Number.isFinite(input) || !Number.isFinite(output)) return null;
  return `measured tokens: ${input} in · ${output} out`;
}

/** "3 of 10 annotation slots claimed today (UTC 2026-08-18)", or null when the
 * server did not report a budget — an unreported budget is not a zero one. */
export function budgetText(budget: AuditBudgetBlock | undefined | null): string | null {
  if (!budget) return null;
  const { claimed, cap, day_key: dayKey } = budget;
  if (typeof claimed !== 'number' || typeof cap !== 'number') return null;
  const day = dayKey ? ` (UTC ${dayKey})` : '';
  return `${claimed} of ${cap} annotation slots claimed today${day}`;
}

/** One-line summary of an annotator run: trigger · status · start time. */
export function auditRunText(run: AuditRunSummary): string {
  const started = run.started_at ? new Date(run.started_at) : null;
  const when = started && !Number.isNaN(started.getTime()) ? started.toLocaleString() : '—';
  return `${run.trigger ?? 'unknown trigger'} · ${run.status ?? 'unknown status'} · ${when}`;
}

/** Honest import counters — every count the server reported, including the
 * ones that mean data was thrown away (nothing is rounded up or hidden). */
export function importSummaryLines(result: FindingsImportResult | undefined): string[] {
  if (!result) return [];
  const lines: string[] = [];
  if (typeof result.written === 'number') {
    lines.push(`${result.written} findings recorded (minimized rows)`);
  }
  if (typeof result.deleted === 'number') {
    lines.push(`${result.deleted} prior generated rows replaced for this report`);
  }
  if (typeof result.discarded_invalid === 'number') {
    lines.push(`${result.discarded_invalid} findings discarded as invalid (taxonomy or evidence refs did not check out)`);
  }
  for (const [reason, count] of Object.entries(result.rejected_fields ?? {})) {
    lines.push(`${count} generated field(s) dropped before storage — reason: ${reason}`);
  }
  if (typeof result.usage_bumped === 'number') {
    lines.push(`${result.usage_bumped} usage-ledger rows recorded`);
  }
  return lines;
}

/** The server's pinned rejection reason for a 400, when it sent one. The
 * response detail is a {reason, detail} envelope of server literals. */
function rejectionReason(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  try {
    const parsed: unknown = JSON.parse(error.detail);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const reason = (parsed as { reason?: unknown }).reason;
      if (typeof reason === 'string' && reason) return reason;
    }
  } catch {
    // Not an envelope — fall through to the fixed generic copy.
  }
  return null;
}

const NOTE_REJECTION_COPY: Record<string, string> = {
  invalid_input: 'Note refused: it must be text.',
  too_long: `Note refused: keep it under ${NOTE_MAX_CHARS} characters.`,
  secret_like_content:
    'Note refused: it looks like it carries a secret (key, token or password shape).',
  fence_delimiter: 'Note refused: it contains a prompt-fence delimiter.',
  invalid_status: 'Annotation refused: unknown status.',
  invalid_annotated_by: 'Annotation refused: the steward id was not id-shaped.',
};

/** FIXED copy for a refused annotation — keyed by the server's reason so the
 * rejected note is never echoed back into the DOM. */
export function annotateRejectionMessage(error: unknown): string {
  const reason = rejectionReason(error);
  if (reason && NOTE_REJECTION_COPY[reason]) return NOTE_REJECTION_COPY[reason];
  if (reason) return `Annotation refused (${reason}).`;
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return 'Annotation refused by the server.';
  }
  return 'Annotation could not be saved.';
}

export type ProposalConflictKind = 'detail-expired' | 'no-mapping' | 'other';

export interface ProposalConflict {
  kind: ProposalConflictKind;
  message: string;
}

/** The two 409s are DISTINCT states and must read that way (ADR-0062 D6):
 * an aged-out 7-day detail is not the same as a finding that never suggested
 * a mapping. */
export function proposalConflict(error: unknown): ProposalConflict {
  const detail = error instanceof ApiError ? error.detail : '';
  if (error instanceof ApiError && error.status === 409) {
    if (detail.includes('detail expired')) {
      return {
        kind: 'detail-expired',
        message:
          'Detail expired — the customer-derived detail is kept 7 days, and the mapping lived there. Re-import the findings to file this one.',
      };
    }
    if (detail.includes('no suggested mapping')) {
      return {
        kind: 'no-mapping',
        message: 'This finding carries no suggested mapping — there is nothing to file.',
      };
    }
  }
  if (error instanceof ApiError) return { kind: 'other', message: detail };
  return { kind: 'other', message: error instanceof Error ? error.message : String(error) };
}

/** True when the resolved finding carries a mapping a steward could file.
 * Only the 7-day detail row holds mapping VALUES, so an expired detail is
 * never fileable. */
export function hasSuggestedMapping(row: FindingRow | undefined): boolean {
  if (!row || row.detail_expired) return false;
  const mapping = row.detail?.suggested_mapping;
  if (!mapping || typeof mapping !== 'object') return false;
  return Object.keys(mapping).length > 0;
}

/** Ordered {key, value} pairs of a suggested mapping, for literal rendering. */
export function mappingRows(row: FindingRow | undefined): { key: string; value: string }[] {
  const mapping = row?.detail?.suggested_mapping ?? {};
  return Object.entries(mapping)
    .filter(([, value]) => typeof value === 'string' && value !== '')
    .map(([key, value]) => ({ key, value: String(value) }));
}

/** Locale timestamp, em dash when absent/invalid (the evals-logic rule). */
export function createdAtText(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}
