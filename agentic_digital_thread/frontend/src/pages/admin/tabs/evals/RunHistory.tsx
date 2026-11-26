// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Run history table — newest-first summaries from GET /api/admin/evals/runs.
 * Every cell is a literal text node (report data is DATA, never markdown).
 *
 * Each row carries its provenance: run_path (where the run was driven from)
 * and golden_set_source (ADR-0064 D3 — "demo set" vs "custom set", with the
 * sha and question count beside it). The two sources are never presented as
 * comparable. */
import type { CSSProperties } from 'react';

import type { EvalRunSummary } from '../../../../api/types/admin';
import {
  goldenSourceLabel,
  goldenSourceOf,
  questionCountText,
  startedText,
  suiteText,
  verdictBadgeClass,
  type GoldenSetSource,
} from './evals-logic';

const runPathBadgeStyle: CSSProperties = {
  padding: '0.15rem 0.45rem',
  borderRadius: 3,
  fontSize: '0.7rem',
  fontWeight: 600,
  background: 'var(--surface2)',
  color: 'var(--accent)',
  fontFamily: "'IBM Plex Mono',monospace",
};

/** run_path chip — "server" (internal chat client) vs "cli" (CloudFront);
 * their latencies are incomparable, so the path rides on every row. */
export function RunPathBadge({ runPath }: { runPath: string | undefined }) {
  return (
    <span className="evals-runpath-badge" style={runPathBadgeStyle}>
      {runPath || '—'}
    </span>
  );
}

/** golden_set_source chip (ADR-0064 D3) — a steward-uploaded "custom set" is
 * visually distinct from the packaged "demo set", and a row recorded before
 * the server stamped provenance shows an em dash rather than a guess. The two
 * sources are NEVER comparable: different questions over different systems. */
export function GoldenSourceBadge({ source }: { source: GoldenSetSource }) {
  return (
    <span
      className={`evals-goldensource-badge ${source}`}
      data-golden-source={source}
      style={{
        ...runPathBadgeStyle,
        color: source === 'uploaded' ? 'var(--amber, #fbbf24)' : 'var(--muted)',
      }}
    >
      {goldenSourceLabel(source)}
    </span>
  );
}

function statusBadgeClass(status: string | undefined): string {
  if (status === 'complete') return verdictBadgeClass('pass');
  if (status === 'failed') return verdictBadgeClass('fail');
  return 'badge';
}

const metaStyle: CSSProperties = {
  fontSize: '0.68rem',
  color: 'var(--muted)',
  marginTop: '0.2rem',
  fontFamily: "'IBM Plex Mono',monospace",
};

const noteStyle: CSSProperties = {
  fontSize: '0.7rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginTop: '0.4rem',
};

export default function RunHistory({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: EvalRunSummary[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
}) {
  if (!runs.length) {
    return (
      <div id="evals-history-empty" style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>
        No eval runs yet — run the full golden suite to record the first one.
      </div>
    );
  }
  return (
    <div className="table-wrap">
      <table id="evals-history-table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Status</th>
            <th>Path</th>
            <th>Set</th>
            <th>Result</th>
            <th>Variant</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.run_id}
              data-run-id={run.run_id}
              onClick={() => onSelect(run.run_id)}
              style={{
                cursor: 'pointer',
                background: run.run_id === selectedRunId ? 'var(--surface2)' : undefined,
              }}
            >
              <td className="mono">{startedText(run.started_at)}</td>
              <td>
                <span className={statusBadgeClass(run.status)}>{run.status ?? '—'}</span>
              </td>
              <td>
                <RunPathBadge runPath={run.run_path} />
              </td>
              <td>
                <GoldenSourceBadge source={goldenSourceOf(run)} />
                <div className="evals-goldenset-meta" style={metaStyle}>
                  {[run.golden_set_sha || '', questionCountText(run)].filter(Boolean).join(' · ')
                    || '—'}
                </div>
              </td>
              <td>{run.status === 'running' ? '…' : suiteText(run.suite)}</td>
              <td>{run.variant || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div id="evals-history-note" style={noteStyle}>
        A custom-set run and a demo-set run are never comparable — different questions over
        different systems. Custom-set rows carry no answer excerpts: answer text is not persisted,
        and the report expires after 7 days.
      </div>
    </div>
  );
}
