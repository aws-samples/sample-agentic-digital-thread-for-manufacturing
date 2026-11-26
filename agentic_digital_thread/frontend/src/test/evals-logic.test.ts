// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests for the pure Evals-tab logic
 * (pages/admin/tabs/evals/evals-logic.ts). Node environment — no DOM. */
import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import type { EvalRunSummary } from '../api/types/admin';
import {
  activeRunOf,
  goldenSourceLabel,
  goldenSourceOf,
  isAnswerTextRedacted,
  latencyText,
  managedEvalRows,
  progressText,
  questionCountText,
  scorePctText,
  startedText,
  suiteText,
  uploadRefusalOf,
  verdictBadgeClass,
} from '../pages/admin/tabs/evals/evals-logic';

describe('activeRunOf', () => {
  const runs: EvalRunSummary[] = [
    { run_id: 'r3', status: 'running' },
    { run_id: 'r2', status: 'complete' },
    { run_id: 'r1', status: 'failed' },
  ];
  it('finds the running run', () => {
    expect(activeRunOf(runs)?.run_id).toBe('r3');
  });
  it('returns null when no run is executing', () => {
    expect(activeRunOf(runs.slice(1))).toBeNull();
    expect(activeRunOf([])).toBeNull();
    expect(activeRunOf(undefined)).toBeNull();
  });
});

describe('verdictBadgeClass', () => {
  it('maps pass/fail to the admin.css badge palette', () => {
    expect(verdictBadgeClass('pass')).toBe('badge pos');
    expect(verdictBadgeClass('fail')).toBe('badge neg');
  });
  it('leaves skipped/unknown neutral', () => {
    expect(verdictBadgeClass('skipped')).toBe('badge');
    expect(verdictBadgeClass(undefined)).toBe('badge');
  });
});

describe('progressText', () => {
  it('renders current/total', () => {
    expect(progressText({ current: 3, total: 24 })).toBe('3/24');
    expect(progressText({ current: 0, total: 24 })).toBe('0/24');
  });
  it('is empty while progress is unknown', () => {
    expect(progressText(undefined)).toBe('');
    expect(progressText({})).toBe('');
    expect(progressText({ current: 3 })).toBe('');
  });
});

describe('suiteText', () => {
  it('renders pass/fail/skipped over total', () => {
    expect(suiteText({ pass: 12, fail: 2, skipped: 1, total: 15 })).toBe(
      '12 pass · 2 fail · 1 skipped / 15',
    );
  });
  it('zero-fills missing counters but em-dashes a missing block', () => {
    expect(suiteText({ total: 3 })).toBe('0 pass · 0 fail · 0 skipped / 3');
    expect(suiteText(undefined)).toBe('—');
    expect(suiteText({})).toBe('—');
  });
});

describe('startedText / latencyText', () => {
  it('formats a valid ISO timestamp and em-dashes garbage', () => {
    expect(startedText('2026-08-09T10:00:00Z')).not.toBe('—');
    expect(startedText('not-a-date')).toBe('—');
    expect(startedText(null)).toBe('—');
    expect(startedText(undefined)).toBe('—');
  });
  it('latency: honest-data rule — null is unmeasured, never 0', () => {
    expect(latencyText(12.3)).toBe('12.3s');
    expect(latencyText(0)).toBe('0s');
    expect(latencyText(null)).toBe('—');
    expect(latencyText(undefined)).toBe('—');
  });
});

describe('managedEvalRows (fleet payload eval_scores flatten)', () => {
  it('flattens agents with scores and skips agents without', () => {
    const rows = managedEvalRows([
      {
        display_name: 'Orchestrator',
        has_eval: true,
        eval_scores: {
          Correctness: { avg: 0.87, count: 42 },
          Helpfulness: { avg: 0.91 },
        },
      },
      { name: 'erp-agent' }, // no eval block — omitted
      {
        runtime_name: 'adt_mes',
        eval_scores: { Correctness: { avg: Number.NaN as unknown as number } },
      },
    ]);
    expect(rows).toEqual([
      { agent: 'Orchestrator', metric: 'Correctness', avg: 0.87, count: 42 },
      { agent: 'Orchestrator', metric: 'Helpfulness', avg: 0.91, count: undefined },
      { agent: 'adt_mes', metric: 'Correctness', avg: Number.NaN, count: undefined },
    ]);
  });
  it('handles empty/absent fleets', () => {
    expect(managedEvalRows([])).toEqual([]);
    expect(managedEvalRows(undefined)).toEqual([]);
  });
  it('carries the figure check counts, and a figure check with no pass rate yet', () => {
    const rows = managedEvalRows([
      {
        display_name: 'Orchestrator',
        eval_scores: {
          FigureCheck: { avg: 0.75, count: 8, no_figures: 5, unchecked: 1 },
        },
      },
      {
        display_name: 'Orchestrator B',
        eval_scores: { FigureCheck: { avg: null, count: 0, no_figures: 3, unchecked: 0 } },
      },
      { display_name: 'Orchestrator C', eval_scores: { Correctness: { avg: null } } },
    ]);
    expect(rows).toEqual([
      { agent: 'Orchestrator', metric: 'FigureCheck', avg: 0.75, count: 8, noFigures: 5, unchecked: 1 },
      { agent: 'Orchestrator B', metric: 'FigureCheck', avg: null, count: 0, noFigures: 3, unchecked: 0 },
    ]);
  });
});

describe('scorePctText', () => {
  it('renders a [0,1] average as a rounded percent', () => {
    expect(scorePctText(0.87)).toBe('87%');
    expect(scorePctText(1)).toBe('100%');
    expect(scorePctText(0)).toBe('0%');
  });
});

describe('golden-set provenance (ADR-0064 D3)', () => {
  it('normalizes the two known sources and never guesses a third', () => {
    expect(goldenSourceOf({ golden_set_source: 'uploaded' })).toBe('uploaded');
    expect(goldenSourceOf({ golden_set_source: 'packaged' })).toBe('packaged');
    // A row recorded before the server stamped provenance, or anything else,
    // is 'unknown' — an absent field is not a value (honest-data rule).
    expect(goldenSourceOf({})).toBe('unknown');
    expect(goldenSourceOf({ golden_set_source: '' })).toBe('unknown');
    expect(goldenSourceOf({ golden_set_source: 'UPLOADED' })).toBe('unknown');
    expect(goldenSourceOf({ golden_set_source: 7 })).toBe('unknown');
    expect(goldenSourceOf(undefined)).toBe('unknown');
    expect(goldenSourceOf(null)).toBe('unknown');
  });

  it('labels a custom set distinctly from a demo set, and never shares a label', () => {
    expect(goldenSourceLabel('uploaded')).toBe('custom set');
    expect(goldenSourceLabel('packaged')).toBe('demo set');
    expect(goldenSourceLabel('unknown')).toBe('—');
    // The two sources are never comparable, so their labels never collide.
    expect(goldenSourceLabel('uploaded')).not.toBe(goldenSourceLabel('packaged'));
  });

  it('renders a stamped question count and stays silent without one', () => {
    expect(questionCountText({ question_count: 17 })).toBe('17 questions');
    expect(questionCountText({ question_count: 1 })).toBe('1 question');
    expect(questionCountText({ question_count: 0 })).toBe('0 questions');
    expect(questionCountText({})).toBe('');
    expect(questionCountText({ question_count: '17' })).toBe('');
    expect(questionCountText(undefined)).toBe('');
  });

  it('reads answer_text_redacted strictly (only true means redacted)', () => {
    expect(isAnswerTextRedacted({ answer_text_redacted: true })).toBe(true);
    expect(isAnswerTextRedacted({ answer_text_redacted: false })).toBe(false);
    expect(isAnswerTextRedacted({ answer_text_redacted: 'true' })).toBe(false);
    expect(isAnswerTextRedacted({})).toBe(false);
    expect(isAnswerTextRedacted(undefined)).toBe(false);
  });
});

describe('uploadRefusalOf (ADR-0064 D6 — every refusal keeps its own message)', () => {
  function refusal(status: number, detail: unknown) {
    const text = typeof detail === 'string' ? detail : JSON.stringify(detail);
    return uploadRefusalOf(new ApiError(status, '/api/admin/evals/run-upload', text));
  }

  it('gives each server reason its own distinct message', () => {
    const messages = [
      refusal(413, { reason: 'payload_too_large', detail: 'too big' }).message,
      refusal(422, { reason: 'invalid_json', detail: 'not utf-8' }).message,
      refusal(422, { reason: 'invalid_golden_set', detail: 'duplicate id' }).message,
      refusal(422, { reason: 'too_many_questions', detail: '61 > 40' }).message,
      refusal(422, { reason: 'unknown_query_params', detail: 'count' }).message,
      refusal(400, { reason: 'secret_like_content', detail: 'secret-shaped' }).message,
      refusal(409, 'eval run already active').message,
      refusal(403, 'admin required').message,
    ];
    expect(new Set(messages).size).toBe(messages.length);
    expect(messages.every((message) => message.length > 0)).toBe(true);
    expect(messages[0]).toContain('256 KiB');
    expect(messages[1]).toContain('byte-order mark');
    expect(messages[3]).toContain('cap is 40');
    expect(messages[4]).toContain('no subset selection');
    expect(messages[6]).toContain('one golden run executes at a time');
    expect(messages[7]).toContain('Steward (admin) access');
  });

  it('surfaces the computed sha so a BOM/non-UTF-8 file can be reconciled', () => {
    expect(refusal(422, { reason: 'invalid_json', detail: 'not utf-8', sha: 'deadbeef1234' }).sha)
      .toBe('deadbeef1234');
    // …also when the server folds it into the detail text only.
    expect(refusal(422, { reason: 'invalid_json', detail: 'sha 0123456789ab' }).sha).toBe(
      '0123456789ab',
    );
    expect(refusal(422, { reason: 'invalid_json', detail: 'golden_set_sha absent' }).sha).toBe('');
  });

  it('keeps the server detail verbatim and falls back to it for an unknown reason', () => {
    const parsed = refusal(422, { reason: 'brand_new_reason', detail: 'something specific' });
    expect(parsed.reason).toBe('brand_new_reason');
    expect(parsed.detail).toBe('something specific');
    // No copy exists for a reason this client has never seen — the server's own
    // words are shown rather than a made-up explanation.
    expect(parsed.message).toBe('something specific');
  });

  it('handles a plain-text detail and a non-ApiError failure', () => {
    expect(uploadRefusalOf(new Error('network down')).detail).toBe('network down');
    expect(uploadRefusalOf(new Error('network down')).message).toBe('network down');
    expect(uploadRefusalOf('boom').detail).toBe('boom');
  });
});
