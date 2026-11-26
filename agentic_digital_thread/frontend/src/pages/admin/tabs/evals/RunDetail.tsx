// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Per-run detail — GET /api/admin/evals/runs/{run_id}. Polls every 5 s while
 * the run executes (evalRunQueryOptions), then renders the full report:
 * per-question verdict matrix + honesty blocks + the informational trend
 * block. EVERY report field renders as a literal text node — report data is
 * DATA, never markdown (repo sink policy: zero HTML-string sinks).
 *
 * A custom-set (uploaded) report carries `answer_text_redacted` — its answer
 * text was never persisted (ADR-0064 D5), so the excerpt block is replaced by
 * an explicit note rather than left as an empty gap. */
import { useEffect, type CSSProperties } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError } from '../../../../api/client';
import { queryKeys } from '../../../../api/query-keys';
import type {
  EvalReport,
  EvalReportQuestion,
  EvalTrendBlock,
} from '../../../../api/types/admin';
import { evalRunQueryOptions } from '../../admin-queries';
import {
  goldenSourceOf,
  isAnswerTextRedacted,
  latencyText,
  progressText,
  questionCountText,
  suiteText,
  verdictBadgeClass,
} from './evals-logic';
import { GoldenSourceBadge, RunPathBadge } from './RunHistory';

const mutedStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const failListStyle: CSSProperties = {
  margin: '0.25rem 0 0',
  paddingLeft: '1rem',
  color: 'var(--red, #f87171)',
  fontSize: '0.72rem',
};
const honestyStyle: CSSProperties = {
  fontSize: '0.7rem',
  color: 'var(--muted)',
  fontFamily: "'IBM Plex Mono',monospace",
};

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

function HonestyBlock({ question }: { question: EvalReportQuestion }) {
  const absence = question.honesty?.absence_markers_matched ?? [];
  const fabrication = question.honesty?.fabrication_flags ?? [];
  if (!absence.length && !fabrication.length) return null;
  return (
    <div className="evals-honesty" style={honestyStyle}>
      {absence.length > 0 && <div>absence markers: {absence.join(', ')}</div>}
      {fabrication.length > 0 && <div>fabrication flags: {fabrication.join(', ')}</div>}
    </div>
  );
}

function QuestionRow({
  question,
  answerTextRedacted,
}: {
  question: EvalReportQuestion;
  answerTextRedacted: boolean;
}) {
  return (
    <tr data-question-id={question.id}>
      <td className="mono">{question.id}</td>
      <td>
        <span className={'evals-verdict-chip ' + verdictBadgeClass(question.verdict)}>
          {question.verdict ?? '—'}
        </span>
      </td>
      <td className="mono">{latencyText(question.elapsed_seconds)}</td>
      <td className="mono">
        {question.systems_queried ?? '—'} / {question.neptune_queries ?? '—'}
      </td>
      <td>
        {question.skip_reason ? <span style={mutedStyle}>{question.skip_reason}</span> : null}
        {(question.failures ?? []).length > 0 && (
          <ul className="evals-failures" style={failListStyle}>
            {(question.failures ?? []).map((failure, i) => (
              <li key={i}>{failure}</li>
            ))}
          </ul>
        )}
        {question.answer_excerpt ? (
          <div className="evals-answer-excerpt" style={{ ...mutedStyle, marginTop: '0.25rem' }}>
            {question.answer_excerpt}
          </div>
        ) : answerTextRedacted ? (
          // An explicit note, never an empty gap: for a custom-set run the
          // answer text was never persisted (ADR-0064 D5), which is a
          // different fact from "the answer was empty".
          <div className="evals-answer-redacted" style={{ ...mutedStyle, marginTop: '0.25rem' }}>
            answer text not persisted (custom-set run)
          </div>
        ) : null}
        <HonestyBlock question={question} />
      </td>
    </tr>
  );
}

/** The informational A/B trend block — clearly labeled: it NEVER gates
 * (ADR-0053 D-E2; metric drift on a live LLM path flakes run-to-run). */
function TrendBlock({ trend }: { trend: EvalTrendBlock }) {
  const suite = trend.suite ?? {};
  const interesting = (trend.questions ?? []).filter(
    (q) => q.transition === 'regression' || q.transition === 'improvement',
  );
  return (
    <div id="evals-trend" style={{ marginTop: '1rem' }}>
      <h4 style={{ margin: '0 0 0.25rem', fontSize: '0.85rem' }}>
        Trend vs previous run{' '}
        <span className="badge" style={{ background: 'var(--surface2)', color: 'var(--muted)' }}>
          informational — never gates
        </span>
      </h4>
      <div style={mutedStyle}>
        pass {suite.pass_previous ?? '—'} → {suite.pass_current ?? '—'} · regressions{' '}
        {suite.regressions ?? 0} · improvements {suite.improvements ?? 0}
        {trend.comparable === false ? ' · golden sets differ — not comparable' : ''}
      </div>
      {interesting.length > 0 && (
        <ul style={{ ...mutedStyle, margin: '0.25rem 0 0', paddingLeft: '1rem' }}>
          {interesting.map((q) => (
            <li key={q.id}>
              {q.id}: {q.previous_verdict ?? '—'} → {q.verdict ?? '—'} ({q.transition})
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ReportView({ report }: { report: EvalReport }) {
  const source = goldenSourceOf(report);
  const redacted = isAnswerTextRedacted(report);
  return (
    <div id="evals-report">
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
        <RunPathBadge runPath={report.run_path} />
        <GoldenSourceBadge source={source} />
        <span style={mutedStyle}>{suiteText(report.suite)}</span>
        {report.variant ? <span style={mutedStyle}>variant: {report.variant}</span> : null}
        {report.golden_set_sha ? (
          <span className="mono" style={mutedStyle}>
            goldens {report.golden_set_sha}
          </span>
        ) : null}
        {questionCountText(report) ? (
          <span className="mono" style={mutedStyle}>
            {questionCountText(report)}
          </span>
        ) : null}
      </div>
      {redacted ? (
        <div id="evals-report-redaction-note" style={{ ...mutedStyle, marginBottom: '0.5rem' }}>
          Answer text was not persisted for this custom-set run — verdicts, question ids and
          deterministic failures are kept, and this report expires 7 days after the run. For
          answer-level debugging, run the same set through the CLI lane
          (scripts/eval_answers.py, then scripts/audit_answer.py) over a report you hold locally.
        </div>
      ) : null}
      <div className="table-wrap">
        <table id="evals-detail-table">
          <thead>
            <tr>
              <th>Question</th>
              <th>Verdict</th>
              <th>Latency</th>
              <th>Systems / Neptune</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {(report.questions ?? []).map((question) => (
              <QuestionRow
                key={question.id}
                question={question}
                answerTextRedacted={redacted}
              />
            ))}
          </tbody>
        </table>
      </div>
      {report.trend ? <TrendBlock trend={report.trend} /> : null}
    </div>
  );
}

export default function RunDetail({ runId }: { runId: string }) {
  const queryClient = useQueryClient();
  const detailQuery = useQuery(evalRunQueryOptions(runId));
  const status = detailQuery.data?.status;

  // A run finishing changes its history row (status + suite counts) — refresh
  // the list once the poll observes the transition.
  useEffect(() => {
    if (status === 'complete' || status === 'failed') {
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.evalRuns() });
    }
  }, [status, queryClient]);

  if (detailQuery.isPending) {
    return <div style={mutedStyle}>Loading run…</div>;
  }
  if (detailQuery.isError) {
    return (
      <div role="alert" style={{ ...mutedStyle, color: 'var(--red, #f87171)' }}>
        Run unavailable: {errorDetail(detailQuery.error)}{' '}
        <button type="button" onClick={() => void detailQuery.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const detail = detailQuery.data;
  if (detail.status === 'running') {
    return (
      <div id="evals-progress" style={mutedStyle}>
        Run in progress — question {progressText(detail.progress) || '…'} (updates every 5 s).
      </div>
    );
  }
  if (detail.status === 'failed') {
    return (
      <div role="alert" style={{ ...mutedStyle, color: 'var(--red, #f87171)' }}>
        Run failed: {detail.error || 'no error detail recorded'}
      </div>
    );
  }
  if (!detail.report) {
    return <div style={mutedStyle}>Run complete, but no report was persisted.</div>;
  }
  return <ReportView report={detail.report} />;
}
