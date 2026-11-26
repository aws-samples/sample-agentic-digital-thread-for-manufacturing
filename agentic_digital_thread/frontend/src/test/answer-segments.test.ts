// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Task 2.1 — answer-segment selection wiring (src/chat/answer-segments.ts).
 *
 * Verifies the AnswerSegments accumulator (the `_answerSegments` bookkeeping
 * of the chat send turn) is wired to the VERBATIM
 * lib/analysis-util.js selectFinalAnswer: tool_start boundaries preserve the
 * current text run as a candidate segment, and `done` reconstructs the final
 * answer as the longest run + any trailing epilogue. */
import { describe, expect, it } from 'vitest';

import { AnswerSegments, chartFingerprint, selectFinalAnswer } from '../chat/answer-segments';

describe('selectFinalAnswer bridge (verbatim lib/analysis-util.js)', () => {
  it('is wired to the global published by the IIFE', () => {
    // The side-effect import in answer-segments.ts must have executed the IIFE.
    expect(typeof (globalThis as Record<string, unknown>).selectFinalAnswer).toBe('function');
  });

  it('picks the longest run and appends the trailing epilogue', () => {
    const report = 'Here is the FULL report. '.repeat(10);
    const out = selectFinalAnswer(['Let me check MES…', report, 'Saved the correlation.']);
    expect(out.startsWith(report.trim().slice(0, 30))).toBe(true);
    expect(out).toContain('Saved the correlation.');
    expect(out).not.toContain('Let me check MES…');
  });

  it('returns empty string for no usable segments', () => {
    expect(selectFinalAnswer([])).toBe('');
    expect(selectFinalAnswer(['   ', ''])).toBe('');
  });
});

describe('AnswerSegments (stream wiring)', () => {
  it('keeps a single uninterrupted run as the answer', () => {
    const seg = new AnswerSegments();
    seg.pushToken('Hello ');
    seg.pushToken('world.');
    expect(seg.finalize()).toBe('Hello world.');
  });

  it('PRESERVES the pre-tool run: report before a trailing housekeeping tool call', () => {
    // The P0 "missing answer" bug: the model writes the full report FIRST,
    // then makes a trailing tool call, then emits only a short epilogue.
    const seg = new AnswerSegments();
    const report = 'FULL REPORT: quality dipped on Line 2 during shift 3. '.repeat(5);
    seg.pushToken(report);
    seg.onToolBoundary(); // save_correlation tool call
    seg.pushToken('Correlation saved.');
    const out = seg.finalize();
    expect(out).toContain('FULL REPORT');
    expect(out).toContain('Correlation saved.');
  });

  it('drops inter-tool reasoning runs BEFORE the longest run', () => {
    const seg = new AnswerSegments();
    seg.pushToken('Now I will query MES…');
    seg.onToolBoundary();
    const answer = 'The final answer with all the detail the user asked for. '.repeat(4);
    seg.pushToken(answer);
    const out = seg.finalize();
    expect(out).not.toContain('Now I will query MES…');
    expect(out).toContain('final answer with all the detail');
  });

  it('resets the live streamingText at each tool boundary (no cumulative dupes)', () => {
    const seg = new AnswerSegments();
    seg.pushToken('run one');
    expect(seg.streamingText).toBe('run one');
    seg.onToolBoundary();
    expect(seg.streamingText).toBe('');
    seg.pushToken('run two');
    expect(seg.streamingText).toBe('run two');
  });

  it('ignores whitespace-only runs at boundaries', () => {
    const seg = new AnswerSegments();
    seg.pushToken('   \n');
    seg.onToolBoundary();
    seg.pushToken('Real answer.');
    expect(seg.finalize()).toBe('Real answer.');
  });

  it('exposes completed runs for the reasoning-trace render (W3.1)', () => {
    const seg = new AnswerSegments();
    seg.pushToken('run one');
    seg.onToolBoundary();
    expect(seg.completedRuns).toEqual(['run one']);
    seg.pushToken('run two');
    expect(seg.completedRuns).toEqual(['run one']); // live run not included
    seg.finalize();
    expect(seg.completedRuns).toEqual(['run one', 'run two']); // trailing pushed
  });

  it('done with tokens still pending pushes the trailing run', () => {
    const seg = new AnswerSegments();
    seg.pushToken('short');
    seg.onToolBoundary();
    seg.pushToken('This trailing run is by far the longest run of the turn.');
    expect(seg.finalize()).toBe('This trailing run is by far the longest run of the turn.');
  });
});

describe('chart survival across tool calls', () => {
  const TAG_A = '<chart-block type="line" title="Motor Temperature" datasets=\'[{"data":[1,2]}]\'/>';
  const TAG_A_SHIFTED =
    '<chart-block type="line" title="Motor Temperature" datasets=\'[{"data":[3,4]}]\'/>';
  const TAG_B = '<chart-block type="bar" title="Downtime by Shift" datasets=\'[]\'/>';
  const LONG = 'The full outage report with all the detail the user asked for. '.repeat(8);
  const tags = (s: string) => s.match(/<chart-block\b[^>]*\/>/g) || [];

  it('acceptance 1: a tag streamed in run 2 of 4 appears exactly once when a longer later run wins', () => {
    const seg = new AnswerSegments();
    seg.pushToken('Checking telemetry…');
    seg.onToolBoundary();
    seg.pushToken(`Here is the chart.\n\n${TAG_A}\n`);
    seg.onToolBoundary();
    seg.pushToken('Now the maintenance side…');
    seg.onToolBoundary();
    seg.pushToken(LONG);
    const out = seg.finalize();
    expect(tags(out)).toEqual([TAG_A]);
    // Reattached ABOVE the narrative ("the charts above").
    expect(out.indexOf(TAG_A)).toBeLessThan(out.indexOf('The full outage report'));
    expect(out).toContain(LONG.trim());
  });

  it('acceptance 2 (H2): same-title tags with different data BOTH render (distinct windows)', () => {
    const seg = new AnswerSegments();
    seg.pushToken(`Batch tool output:\n${TAG_A}\n`);
    seg.onToolBoundary();
    seg.pushToken(`Chart tool output:\n${TAG_A_SHIFTED}\n`);
    seg.onToolBoundary();
    seg.pushToken(LONG);
    const out = seg.finalize();
    // Title alone dropped same-title different-window charts ("compare last
    // hour vs yesterday") — the content-hash fingerprint keeps both.
    expect(tags(out)).toEqual([TAG_A, TAG_A_SHIFTED]);
  });

  it('acceptance 2b: a byte-identical re-render still dedups to one chart', () => {
    const seg = new AnswerSegments();
    seg.pushToken(`Batch tool output:\n${TAG_A}\n`);
    seg.onToolBoundary();
    seg.pushToken(`Chart tool output:\n${TAG_A}\n`);
    seg.onToolBoundary();
    seg.pushToken(LONG);
    const out = seg.finalize();
    expect(tags(out)).toEqual([TAG_A]);
  });

  it('acceptance 3: a tag already inside the selected answer is not duplicated', () => {
    const seg = new AnswerSegments();
    seg.pushToken('short run');
    seg.onToolBoundary();
    seg.pushToken(`${LONG}\n\n${TAG_A}\n`); // chart tool was the LAST call — tag survives selection
    const out = seg.finalize();
    expect(tags(out)).toEqual([TAG_A]);
    expect(out.startsWith('<chart-block')).toBe(false); // no prepend happened
  });

  it('acceptance 4: no-chart streams are byte-identical to the plain reconstruction', () => {
    const seg = new AnswerSegments();
    seg.pushToken('short run');
    seg.onToolBoundary();
    seg.pushToken(LONG);
    expect(seg.finalize()).toBe(selectFinalAnswer(['short run', LONG]));
  });

  it('distinct-title tags from different runs are BOTH reattached, in stream order', () => {
    const seg = new AnswerSegments();
    seg.pushToken(`${TAG_A}\n`);
    seg.onToolBoundary();
    seg.pushToken(`${TAG_B}\n`);
    seg.onToolBoundary();
    seg.pushToken(LONG);
    const out = seg.finalize();
    expect(tags(out)).toEqual([TAG_A, TAG_B]);
  });

  it('harvests a tag split across token events (matches over the accumulated run)', () => {
    const seg = new AnswerSegments();
    const cut = Math.floor(TAG_A.length / 2);
    seg.pushToken(TAG_A.slice(0, cut));
    seg.pushToken(TAG_A.slice(cut));
    seg.onToolBoundary();
    seg.pushToken(LONG);
    expect(tags(seg.finalize())).toEqual([TAG_A]);
  });

  it('does not re-harvest the same tag on subsequent tokens of the same run', () => {
    const seg = new AnswerSegments();
    seg.pushToken(`${TAG_A}\n`);
    seg.pushToken('trailing prose in the same run');
    seg.onToolBoundary();
    seg.pushToken(LONG);
    expect(tags(seg.finalize())).toEqual([TAG_A]);
  });
});

describe('chartFingerprint', () => {
  it('keys on title (double or single quotes) PLUS a content hash (H2)', () => {
    const fpDouble = chartFingerprint('<chart-block title="X" data=\'[1]\'/>');
    const fpSingle = chartFingerprint("<chart-block title='X' data='[2]'/>");
    expect(fpDouble.startsWith('title:X|h:')).toBe(true);
    expect(fpSingle.startsWith('title:X|h:')).toBe(true);
    // Same title, different data ⇒ DIFFERENT fingerprints (distinct windows
    // must not dedup against each other).
    expect(fpDouble).not.toBe(fpSingle);
    // Byte-identical tags ⇒ identical fingerprints.
    expect(chartFingerprint('<chart-block title="X" data=\'[1]\'/>')).toBe(fpDouble);
  });

  it('untitled tags still get stable content-hash fingerprints', () => {
    const untitled = `<chart-block type="line" datasets='[${'1,'.repeat(100)}1]'/>`;
    expect(chartFingerprint(untitled)).toBe(chartFingerprint(untitled));
    expect(chartFingerprint(untitled)).not.toBe(
      chartFingerprint('<chart-block title="" x="1"/>'),
    );
  });
});

describe('reattachFollowups (chips-lost-to-run-selection fix)', () => {
  it('appends a FOLLOWUPS block from a discarded short run', async () => {
    const { AnswerSegments } = await import('../chat/answer-segments');
    const s = new AnswerSegments();
    s.pushToken('This is the long main answer run with lots of prose that wins selection. '.repeat(5));
    s.onToolBoundary();
    s.pushToken('---FOLLOWUPS---\nQ1: What next?\nA1: Check the line.');
    const finalMd = s.finalize();
    expect(finalMd).toContain('---FOLLOWUPS---');
    expect(finalMd).toContain('Q1: What next?');
    expect(finalMd.indexOf('---FOLLOWUPS---')).toBeGreaterThan(finalMd.indexOf('long main answer'));
  });

  it('does not duplicate when the selected answer already carries the block', async () => {
    const { AnswerSegments } = await import('../chat/answer-segments');
    const s = new AnswerSegments();
    s.pushToken('Main answer. '.repeat(30) + '\n---FOLLOWUPS---\nQ1: One?');
    const finalMd = s.finalize();
    expect(finalMd.match(/---FOLLOWUPS---/g)?.length).toBe(1);
  });

  it('takes the LAST followups block when several runs carry one', async () => {
    const { AnswerSegments } = await import('../chat/answer-segments');
    const s = new AnswerSegments();
    s.pushToken('---FOLLOWUPS---\nQ1: stale early block');
    s.onToolBoundary();
    s.pushToken('The very long real answer text run winning the selection. '.repeat(6));
    s.onToolBoundary();
    s.pushToken('---FOLLOWUPS---\nQ1: fresh final block');
    const finalMd = s.finalize();
    expect(finalMd).toContain('fresh final block');
    expect(finalMd).not.toContain('stale early block');
  });
});

describe('the answer starts after the last tool call that reads data', () => {
  // A longer planning run before the last data read, then a shorter real
  // answer: longest-run selection kept the planning run.
  const PLANNING =
    'The shipment lines came back, so next I will price them by joining each line to its order line. '.repeat(3);
  const ANSWER = '## Customers\n\n| Customer | Units |\n|---|---|\n| A | 110 |\n\nTotal: 110 units [2]';
  const REPORT = 'FULL REPORT: four customers received parts from the lot. '.repeat(5);

  it('keeps a short answer after a longer planning run', () => {
    expect(PLANNING.length).toBeGreaterThan(ANSWER.length);
    const seg = new AnswerSegments();
    seg.pushToken(PLANNING);
    seg.onToolBoundary('erp___query_erp');
    seg.pushToken(ANSWER);
    expect(seg.finalize()).toBe(ANSWER);
  });

  it.each(['save_correlation', 'digital-thread___save_correlation',
    'digital-thread___save_value_mapping', 'propose_metric_definition'])(
    'a %s call after the report keeps the report and its epilogue', (tool) => {
      const seg = new AnswerSegments();
      seg.pushToken('Now I will query ERP.');
      seg.onToolBoundary('erp___query_erp');
      seg.pushToken(REPORT);
      seg.onToolBoundary(tool);
      seg.pushToken('Correlation saved.');
      expect(seg.finalize()).toBe(`${REPORT}\n\nCorrelation saved.`.trim());
    });

  it('keeps every completed run for the reasoning trace', () => {
    const seg = new AnswerSegments();
    seg.pushToken(PLANNING);
    seg.onToolBoundary('erp___query_erp');
    seg.pushToken(ANSWER);
    seg.finalize();
    expect(seg.completedRuns).toEqual([PLANNING, ANSWER]);
  });

  it('falls back to every run when no text follows the last read', () => {
    const seg = new AnswerSegments();
    seg.pushToken(REPORT);
    seg.onToolBoundary('erp___query_erp');
    expect(seg.finalize()).toBe(REPORT.trim());
  });
});
