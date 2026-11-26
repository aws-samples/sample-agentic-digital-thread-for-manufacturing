// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Evals admin tab — runs the SAME golden lane the CLI runs, server-side
 * (agents/eval_core.py through agents/eval_routes.py). It COMPLEMENTS the CLI
 * lanes (scripts/eval_answers.py / smoke_prompts.py), never replaces them.
 *
 * Pinned invariants (ADR-0064 for the upload affordance):
 *   - run-ALL-only: NO subset controls of any kind (no count, no category, no
 *     per-question selection) on EITHER run affordance — the packaged demo
 *     suite and an uploaded customer set both run whole. The optional variant
 *     LABEL and the golden-set file picker are the only inputs;
 *   - both run buttons are askConfirm()-gated (a full run is minutes and
 *     billable), and one run executes at a time (server-enforced 409);
 *   - an uploaded golden set is transient: read at submit time, sent as raw
 *     bytes, and never put in component state, the query cache, or web
 *     storage. Its report is answer-text redacted with a 7-day horizon;
 *   - report data is DATA: every field renders as a literal text node — zero
 *     HTML-string sinks (repo sink policy).
 *
 * Same conventions as tabs/VocabularyTab.tsx: the host keeps this mounted
 * after first activation, TanStack Query owns server freshness, local state
 * owns the form. */
import { useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../api/client';
import { EVAL_META } from '../../../shared/agents';
import { askConfirm } from '../../../shared/ui/confirm-store';
import { fleetQueryOptions } from '../../../shared/agents/agent-queries';
import { evalRunsQueryOptions, startEvalRunMutationOptions } from '../admin-queries';
import { activeRunOf, managedEvalRows, samplesText, scorePctText } from './evals/evals-logic';
import FindingsLedger from './evals/FindingsLedger';
import RunDetail from './evals/RunDetail';
import RunHistory from './evals/RunHistory';
import ReviewQueue from './evals/ReviewQueue';
import UploadedGoldenRun from './evals/UploadedGoldenRun';

const h3Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 };
const h4Style: CSSProperties = { margin: '0 0 0.5rem', fontSize: '0.85rem', fontWeight: 600 };
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const runBtnStyle: CSSProperties = {
  background: 'var(--accent)',
  color: '#000',
  fontWeight: 600,
  padding: '0.4rem 1rem',
  border: 'none',
  borderRadius: 4,
  fontSize: '0.8rem',
  cursor: 'pointer',
};
const inputStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '0.35rem 0.6rem',
  fontSize: '0.8rem',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const hrStyle: CSSProperties = {
  border: 'none',
  borderTop: '1px solid var(--border)',
  margin: '2rem 0',
};

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

interface ColoredStatus {
  text: string;
  color?: string;
}

/** Managed AgentCore Evaluations — REUSES the fleet query (GET
 * /api/admin/agents; queryKeys.admin.fleet(24)): the evaluation block is
 * already served there, so no new endpoint exists for this section. */
function ManagedEvalsSection() {
  const fleetQuery = useQuery(fleetQueryOptions(24));
  const rows = managedEvalRows(fleetQuery.data?.agents);
  return (
    <div id="evals-managed-section">
      <h4 style={h4Style}>Managed AgentCore Evaluations</h4>
      <p style={sectionHelpStyle}>
        Continuous sampled scoring from Amazon Bedrock AgentCore Evaluations — it complements the
        golden lane above and never replaces it. Scores lag by roughly hours and sample about 10%
        of traffic, so treat them as a trend line, not a per-run verdict.
      </p>
      {fleetQuery.isPending && <div style={statusStyle}>Loading evaluation scores…</div>}
      {fleetQuery.isError && (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
          Evaluation scores unavailable: {errorDetail(fleetQuery.error)}
        </div>
      )}
      {fleetQuery.isSuccess && rows.length === 0 && (
        <div style={statusStyle}>No managed evaluation scores yet (scores lag by hours).</div>
      )}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table id="evals-managed-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Metric</th>
                <th>Avg</th>
                <th>Samples</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.agent}\u0000${row.metric}`}>
                  <td>{row.agent}</td>
                  <td title={EVAL_META[row.metric]?.desc}>
                    {EVAL_META[row.metric]?.label ?? row.metric}
                  </td>
                  <td className="mono">{row.avg === null ? '—' : scorePctText(row.avg)}</td>
                  <td className="mono">{samplesText(row)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function EvalsTab() {
  const queryClient = useQueryClient();
  const runsQuery = useQuery(evalRunsQueryOptions());
  const startMutation = useMutation(startEvalRunMutationOptions(queryClient));
  const [variant, setVariant] = useState('');
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [startStatus, setStartStatus] = useState<ColoredStatus>({ text: '' });

  const runs = runsQuery.data?.runs ?? [];
  const activeRun = activeRunOf(runs);
  const runDisabled = !!activeRun || startMutation.isPending;
  // The active run's live progress renders in RunDetail; follow it by default.
  const detailRunId = selectedRunId ?? activeRun?.run_id ?? null;

  const startRun = async () => {
    // askConfirm()-gated (the steward-action convention): a full golden run
    // is minutes long and billable — never start one on a stray click.
    const run = await askConfirm({
      key: 'evals:run-full',
      title: 'Run the full golden suite?',
      body: 'This asks every golden question against the live orchestrator. It takes minutes and incurs model cost.',
      confirmLabel: 'Run full suite',
    });
    if (!run) return;
    setStartStatus({ text: 'Starting run…', color: 'var(--muted)' });
    try {
      const result = await startMutation.mutateAsync(variant);
      setStartStatus({ text: 'Run started.', color: 'var(--green, #4ade80)' });
      if (result.run_id) setSelectedRunId(result.run_id);
    } catch (error) {
      // A 409 means a run is already active or the golden set is missing —
      // surface the server's detail verbatim.
      setStartStatus({ text: 'Error: ' + errorDetail(error), color: 'var(--red, #f87171)' });
    }
  };

  return (
    <div style={{ maxWidth: 960, padding: '1.5rem' }}>
      <h3 style={h3Style}>Evaluations</h3>
      <p style={sectionHelpStyle}>
        Runs the full golden answer-evaluation suite server-side — the same computed scoring as the
        CLI lane (scoring is deterministic; the model never produces a verdict). Runs are always
        the COMPLETE suite: partial runs can flip the cross-system proof, so no subset selection
        exists. On environments without seeded demo data, goldens marked requires_seed will
        honestly FAIL — that is expected, not a platform fault. The variant label below is an
        optional tag on whichever run you start; it is persisted with the run, so keep customer
        identity out of it.
      </p>

      <div id="evals-run-form" style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
        <input
          id="evals-variant-input"
          type="text"
          placeholder="Variant label (optional)"
          value={variant}
          maxLength={80}
          style={inputStyle}
          onChange={(e) => setVariant(e.target.value)}
        />
        <button
          id="evals-run-btn"
          type="button"
          style={{ ...runBtnStyle, opacity: runDisabled ? 0.5 : 1, cursor: runDisabled ? 'not-allowed' : 'pointer' }}
          disabled={runDisabled}
          onClick={() => void startRun()}
        >
          Run full golden suite
        </button>
        <span id="evals-run-status" style={{ ...statusStyle, color: startStatus.color ?? statusStyle.color }}>
          {activeRun ? 'A run is active — wait for it to finish.' : startStatus.text}
        </span>
      </div>

      {/* The SECOND run affordance (ADR-0064) — beside the packaged run, never
        * replacing it, and with no subset control of its own either. One run
        * executes at a time, so an active run disables both. */}
      <UploadedGoldenRun
        variant={variant}
        disabled={runDisabled}
        onStarted={(runId) => setSelectedRunId(runId)}
      />

      {runsQuery.isPending && <div style={statusStyle}>Loading run history…</div>}
      {runsQuery.isError && (
        <div role="alert" style={{ ...statusStyle, color: 'var(--red, #f87171)' }}>
          Run history unavailable: {errorDetail(runsQuery.error)}{' '}
          <button type="button" onClick={() => void runsQuery.refetch()}>
            Retry
          </button>
        </div>
      )}
      {runsQuery.isSuccess && (
        <RunHistory runs={runs} selectedRunId={detailRunId} onSelect={setSelectedRunId} />
      )}

      {detailRunId && (
        <div id="evals-detail" style={{ marginTop: '1.5rem' }}>
          <RunDetail key={detailRunId} runId={detailRunId} />
        </div>
      )}

      <hr style={hrStyle} />
      <ReviewQueue />

      <hr style={hrStyle} />
      <FindingsLedger />

      <hr style={hrStyle} />
      <ManagedEvalsSection />
    </div>
  );
}
