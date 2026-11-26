// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure Evals-tab logic — kept DOM-free so it is unit-testable under the node
 * vitest environment (the admin-logic.ts convention). All report fields are
 * DATA: these helpers format strings for literal text nodes only.
 *
 * Also owns the ADR-0064 provenance/refusal vocabulary: a run's golden-set
 * source ("demo set" vs "custom set", never conflated), and the distinct
 * message for every upload refusal. Nothing here ever holds, echoes or
 * reconstructs uploaded file content. */

import { ApiError } from '../../../../api/client';
import type { AgentInfo } from '../../../../api/types/agents';
import type { EvalRunSummary, EvalSuiteTotals } from '../../../../api/types/admin';

/** The first run currently executing, if any (the list is newest-first). */
export function activeRunOf(runs: EvalRunSummary[] | undefined): EvalRunSummary | null {
  return (runs ?? []).find((run) => run.status === 'running') ?? null;
}

/** Verdict chip class — reuses the admin.css .badge palette (pos/neg). */
export function verdictBadgeClass(verdict: string | undefined): string {
  if (verdict === 'pass') return 'badge pos';
  if (verdict === 'fail') return 'badge neg';
  return 'badge';
}

/** "current/total" while progress is known, else ''. */
export function progressText(progress: { current?: number; total?: number } | undefined): string {
  if (!progress || progress.current == null || progress.total == null) return '';
  return `${progress.current}/${progress.total}`;
}

/** "P pass · F fail · S skipped / T" (em dash when the block is absent). */
export function suiteText(suite: EvalSuiteTotals | undefined): string {
  if (!suite || suite.total == null) return '—';
  return `${suite.pass ?? 0} pass · ${suite.fail ?? 0} fail · ${suite.skipped ?? 0} skipped / ${suite.total}`;
}

/** Locale timestamp for a run's started_at (em dash when absent/invalid). */
export function startedText(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

/** "12.3s" (em dash when unmeasured — honest-data rule: null ≠ 0). */
export function latencyText(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number') return '—';
  return `${seconds}s`;
}

// ── Golden-set provenance (ADR-0064 D3) ──────────────────────────────────────
// The server stamps `golden_set_source` / `question_count` on every run summary
// and `golden_set_source` / `answer_text_redacted` on every report. These
// helpers take `unknown` on purpose: the wire fields are additive, and the
// shared api/types/admin.ts declarations land through a separate change — so a
// record is read structurally here and no call site changes when they arrive.

/** Where a run's questions came from. `unknown` is a row recorded BEFORE the
 * server stamped provenance — it is never guessed into a source. */
export type GoldenSetSource = 'packaged' | 'uploaded' | 'unknown';

function stringField(record: unknown, key: string): string | undefined {
  if (!record || typeof record !== 'object') return undefined;
  const value = (record as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : undefined;
}

function numberField(record: unknown, key: string): number | undefined {
  if (!record || typeof record !== 'object') return undefined;
  const value = (record as Record<string, unknown>)[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/** The run's golden-set source, normalized. Anything the server did not stamp
 * as one of the two known sources is 'unknown' (honest-data rule: an absent
 * field is not a value). */
export function goldenSourceOf(record: unknown): GoldenSetSource {
  const source = stringField(record, 'golden_set_source');
  if (source === 'uploaded') return 'uploaded';
  if (source === 'packaged') return 'packaged';
  return 'unknown';
}

/** Badge copy for a source. A steward-uploaded run is a "custom set" and the
 * packaged demo golden is the "demo set" — the two are never comparable
 * (different questions over different systems), so they never share a badge. */
export function goldenSourceLabel(source: GoldenSetSource): string {
  if (source === 'uploaded') return 'custom set';
  if (source === 'packaged') return 'demo set';
  return '—';
}

/** "17 questions" from a stamped `question_count`, else '' (never guessed from
 * the suite totals — a partially recorded run would lie). */
export function questionCountText(record: unknown): string {
  const count = numberField(record, 'question_count');
  if (count == null) return '';
  return `${count} ${count === 1 ? 'question' : 'questions'}`;
}

/** True when the report says its answer text was NOT persisted (uploaded runs,
 * ADR-0064 D5). Drives the explicit note that replaces the excerpt block. */
export function isAnswerTextRedacted(record: unknown): boolean {
  if (!record || typeof record !== 'object') return false;
  return (record as Record<string, unknown>).answer_text_redacted === true;
}

// ── Uploaded-run refusals (ADR-0064 D6) ──────────────────────────────────────
// Every refusal gets its OWN message: a steward must be able to tell a size
// refusal from an encoding refusal from a schema refusal without guessing. The
// server never echoes file content in a detail, and nothing here reconstructs
// any — these helpers only ever produce strings for literal text nodes.

/** A parsed upload refusal: the server's reason id, the message the tab shows,
 * the server's own detail, and the computed sha when the server reported one. */
export interface UploadRefusal {
  reason: string;
  message: string;
  detail: string;
  sha: string;
}

/** sha256[:12] — the digest `eval_core.golden_bytes_sha` returns. */
const SHA12 = /\b[0-9a-f]{12}\b/;

const REFUSAL_COPY: Record<string, string> = {
  payload_too_large:
    'That file is over the 256 KiB upload ceiling. The server refused it before parsing anything.',
  invalid_json:
    'The server could not read that file as UTF-8 JSON. A byte-order mark (BOM) or a non-UTF-8 encoding is the usual cause — re-save the file as UTF-8 without a BOM and try again.',
  invalid_golden_set:
    'The file is valid JSON but not a valid golden set. The server names the offending key or question id below and never quotes your question text.',
  too_many_questions:
    'That set has more questions than one server-side run can cover (the cap is 40, derived from the run deadline). Run the full set through the CLI lane instead — a truncated run is never offered.',
  unknown_query_params:
    'The server rejected an unexpected query parameter. Only the variant label is accepted: there is no subset selection on either run affordance.',
  secret_like_content:
    'The file looks like it carries a credential, so the server refused it before the questions could reach the model. The refusal never echoes the content.',
};

const STATUS_COPY: Record<number, string> = {
  403: 'Steward (admin) access is required to run an uploaded golden set.',
  409: 'A run is already active — one golden run executes at a time, and a custom-set run and a demo-set run can never overlap.',
  413: REFUSAL_COPY.payload_too_large,
};

/** Parse an upload refusal into its reason id, its distinct message, the
 * server's verbatim detail, and the sha the server computed over the bytes it
 * received (shown so a steward can reconcile against their own file — the
 * BOM/non-UTF-8 case is exactly where a naive local hash diverges). */
export function uploadRefusalOf(error: unknown): UploadRefusal {
  const status = error instanceof ApiError ? error.status : 0;
  const raw = error instanceof ApiError
    ? error.detail
    : error instanceof Error ? error.message : String(error);
  let reason = '';
  let detail = raw;
  let sha = '';
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const fields = parsed as Record<string, unknown>;
      reason = typeof fields.reason === 'string' ? fields.reason : '';
      const inner = typeof fields.detail === 'string' ? fields.detail : '';
      detail = inner || reason || raw;
      for (const key of ['sha', 'golden_set_sha'] as const) {
        if (typeof fields[key] === 'string' && SHA12.test(fields[key] as string)) {
          sha = fields[key] as string;
        }
      }
    }
  } catch {
    // A plain-text detail (403, a transport failure) is already the message.
  }
  if (!sha) sha = SHA12.exec(detail)?.[0] ?? '';
  const message = REFUSAL_COPY[reason] || STATUS_COPY[status] || detail;
  return { reason, message, detail, sha };
}

/** One managed-evaluation score row derived from the fleet payload. */
export interface ManagedEvalRow {
  agent: string;
  metric: string;
  /** `null`: the figure check has sampled only answers it had nothing to check. */
  avg: number | null;
  count?: number;
  noFigures?: number;
  unchecked?: number;
}

/** Flatten the fleet payload's eval_scores blocks (GET /api/admin/agents) into
 * displayable rows — agents without scores are omitted, and a score with no
 * average is kept only when it carries the figure check's counts. */
export function managedEvalRows(agents: AgentInfo[] | undefined): ManagedEvalRow[] {
  const rows: ManagedEvalRow[] = [];
  for (const agent of agents ?? []) {
    const name = agent.display_name || agent.name || agent.runtime_name || 'unknown';
    for (const [metric, score] of Object.entries(agent.eval_scores ?? {})) {
      const counted = typeof score?.no_figures === 'number' || typeof score?.unchecked === 'number';
      if (typeof score?.avg !== 'number' && !(score?.avg === null && counted)) continue;
      rows.push({
        agent: name,
        metric,
        avg: score.avg,
        count: score.count,
        noFigures: score.no_figures,
        unchecked: score.unchecked,
      });
    }
  }
  return rows;
}

/** "8 · 5 no figures · 1 unchecked": the samples a row's average covers, then
 * the figure check's answers kept out of it. */
export function samplesText(row: ManagedEvalRow): string {
  const parts = [row.count === undefined ? '—' : String(row.count)];
  if (row.noFigures) parts.push(`${row.noFigures} no figures`);
  if (row.unchecked) parts.push(`${row.unchecked} unchecked`);
  return parts.join(' · ');
}

/** "87%" from a [0,1] eval-score average. */
export function scorePctText(avg: number): string {
  return `${Math.round(avg * 100)}%`;
}
