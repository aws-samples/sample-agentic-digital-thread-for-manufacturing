// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Control-block parsing (src/shared/thread/answer-blocks.ts — canonical since
 * the Task-3.2 dedupe), the parseAnswerBlocks contract. */
import { describe, expect, it } from 'vitest';

import { parseAnswerBlocks } from '../shared/thread/answer-blocks';

describe('parseAnswerBlocks', () => {
  it('returns plain text untouched with no chips', () => {
    const r = parseAnswerBlocks('The line is running at 94% OEE.');
    expect(r.answer).toBe('The line is running at 94% OEE.');
    expect(r.followups).toEqual([]);
    expect(r.actions).toEqual([]);
  });

  it('handles empty / null-ish input', () => {
    expect(parseAnswerBlocks('')).toEqual({ answer: '', followups: [], actions: [] });
  });

  it('strips VALUE-MAPPING proposal blocks from the display answer', () => {
    const text =
      'Answer prose.\n---VALUE-MAPPING-PROPOSAL---\nfrom_system: erp\nto_system: mes\n---END-PROPOSAL---\nMore prose.';
    const r = parseAnswerBlocks(text);
    expect(r.answer).not.toContain('VALUE-MAPPING-PROPOSAL');
    expect(r.answer).not.toContain('from_system');
    expect(r.answer).toContain('Answer prose.');
    expect(r.answer).toContain('More prose.');
  });

  it('strips the additional_candidate_pairs_observed bookkeeping line', () => {
    const text =
      'Answer prose.\n---VALUE-MAPPING-PROPOSAL---\nz\n---END-PROPOSAL---\nadditional_candidate_pairs_observed: 1\nMore prose.';
    const r = parseAnswerBlocks(text);
    expect(r.answer).not.toContain('additional_candidate_pairs_observed');
    expect(r.answer).toContain('Answer prose.');
    expect(r.answer).toContain('More prose.');
    // Prose mentioning the phrase mid-sentence is NOT stripped (line-anchored).
    const prose = parseAnswerBlocks('We saw additional_candidate_pairs_observed: 1 in the data.');
    expect(prose.answer).toContain('additional_candidate_pairs_observed');
  });

  it('strips the bookkeeping line when the model wraps it in italics or bold', () => {
    // A saved briefing (2026-10-17) wrote `*additional_candidate_pairs_observed: 0*`.
    // Same wrapper list and line rule as agents/public_answer.py (pinned there).
    for (const line of [
      '*additional_candidate_pairs_observed: 0*',
      '_additional_candidate_pairs_observed: 3_',
      '**additional_candidate_pairs_observed: 2**',
      '__additional_candidate_pairs_observed: 5__',
      '*Additional_Candidate_Pairs_Observed: 4*',
    ]) {
      const r = parseAnswerBlocks(`Answer prose.\n\n${line}\nMore prose.`);
      expect(r.answer).not.toMatch(/additional_candidate_pairs_observed/i);
      expect(r.answer).toContain('More prose.');
    }
  });

  it.each([
    '*additional_candidate_pairs_observed: 1_',
    '**additional_candidate_pairs_observed: 1*',
    '***additional_candidate_pairs_observed: 1***',
    '*additional_candidate_pairs_observed: 1* and more',
    '* additional_candidate_pairs_observed: 1',
  ])('leaves %j alone: not exactly one wrapped record', (line) => {
    expect(parseAnswerBlocks(`${line}\nNext.`).answer).toContain(line);
  });

  it('a counter split over two lines is not a record', () => {
    const text = 'additional_candidate_pairs_observed:\n5\nNext.';
    expect(parseAnswerBlocks(text).answer).toContain('additional_candidate_pairs_observed:');
  });

  it('strips CORRELATION proposal blocks (multiple, non-greedy)', () => {
    const text =
      'A.\n---CORRELATION-PROPOSAL---\nx\n---END-PROPOSAL---\nB.\n---CORRELATION-PROPOSAL---\ny\n---END-PROPOSAL---\nC.';
    const r = parseAnswerBlocks(text);
    expect(r.answer).not.toContain('CORRELATION-PROPOSAL');
    expect(r.answer).toContain('A.');
    expect(r.answer).toContain('B.');
    expect(r.answer).toContain('C.');
  });

  it('splits a trailing FOLLOWUPS block into question and action chips', () => {
    const text =
      'Final answer here.\n\n---FOLLOWUPS---\nQ1: Which line had the most scrap?\nQ2: Show downtime by shift\nA1: Create a maintenance work order\n';
    const r = parseAnswerBlocks(text);
    expect(r.answer).toBe('Final answer here.');
    expect(r.followups).toEqual(['Which line had the most scrap?', 'Show downtime by shift']);
    expect(r.actions).toEqual(['Create a maintenance work order']);
  });

  it('strips proposals AND extracts followups in one pass', () => {
    const text =
      'Prose.\n---VALUE-MAPPING-PROPOSAL---\nz\n---END-PROPOSAL---\n---FOLLOWUPS---\nQ1: next?\nA1: act now';
    const r = parseAnswerBlocks(text);
    expect(r.answer).toBe('Prose.');
    expect(r.followups).toEqual(['next?']);
    expect(r.actions).toEqual(['act now']);
  });

  it('leaves text after FOLLOWUPS out of the answer (block is trailing)', () => {
    const text = 'Answer.\n---FOLLOWUPS---\nQ1: a\nQ2: b';
    const r = parseAnswerBlocks(text);
    expect(r.answer).toBe('Answer.');
    expect(r.followups).toHaveLength(2);
  });

  it('drops placeholder lines the model writes when it has nothing to suggest', () => {
    // Event 23 (2026-10-02): an answer whose block ended "A3: None" rendered a
    // literal "None" chip under Agent Analysis.
    const text =
      'Answer.\n---FOLLOWUPS---\nQ1: Real question?\nQ2: None\nQ3: N/A\nA1: none.\nA2: Hold the work order\nA3: -';
    const r = parseAnswerBlocks(text);
    expect(r.followups).toEqual(['Real question?']);
    expect(r.actions).toEqual(['Hold the work order']);
  });
});

describe('stripControlBlocksForStream (live suppression)', () => {
  it('cuts an opened-but-unterminated FOLLOWUPS block mid-stream', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text = 'The answer is 42.\n\n---FOLLOWUPS---\nQ1: partial question th';
    expect(stripControlBlocksForStream(text)).toBe('The answer is 42.\n\n');
  });

  it('cuts an opened proposal block before END-PROPOSAL arrives', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text = 'Done.\n---VALUE-MAPPING-PROPOSAL---\nfrom_value: generic_a\nreason: streaming';
    expect(stripControlBlocksForStream(text)).toBe('Done.\n');
  });

  it('hides a partial trailing marker so it never flickers', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    expect(stripControlBlocksForStream('Answer text.\n---FOLLO')).toBe('Answer text.');
    expect(stripControlBlocksForStream('Answer text.\n--')).toBe('Answer text.');
  });

  it('removes complete proposal blocks and keeps surrounding prose', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text = 'Before.\n---CORRELATION-PROPOSAL---\nx: y\n---END-PROPOSAL---\nAfter.';
    expect(stripControlBlocksForStream(text)).toBe('Before.\n\nAfter.');
  });

  it('leaves ordinary prose and completed markdown rules alone', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text = 'Para one.\n\n---\n\nPara two continues here.';
    expect(stripControlBlocksForStream(text)).toBe(text);
  });

  /* Event 26 (2026-10-04), module 3 refusal: the model emitted the closing
   * sentinel with no opener and the literal "---END-PROPOSAL---" line rendered
   * under the refusal. A closing sentinel is machine payload on its own, and a
   * block written with two or four dashes is still the block. */
  it('drops an orphan END-PROPOSAL sentinel at finalize and mid-stream', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text = "I can't share that.\n\n---END-PROPOSAL---\n\nadditional_candidate_pairs_observed: 0\n";
    expect(parseAnswerBlocks(text).answer).toBe("I can't share that.\n\n\n");
    expect(stripControlBlocksForStream(text)).toBe("I can't share that.\n\n\n");
    expect(parseAnswerBlocks('Done.\n--- END-PROPOSAL ---\nMore.').answer).toBe('Done.\nMore.');
  });

  it('closes a proposal block whatever its dash count', async () => {
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text =
      'Answer.\n--VALUE-MAPPING-PROPOSAL--\nfrom_system: a\n--END-PROPOSAL--\nAfter.\n' +
      '---- CORRELATION PROPOSAL ----\nx\n----END-PROPOSAL----\nEnd.';
    expect(parseAnswerBlocks(text).answer).toBe('Answer.\n\nAfter.\n\nEnd.');
    expect(stripControlBlocksForStream(text)).toBe('Answer.\n\nAfter.\n\nEnd.');
    // Opened with a variant marker and not yet closed: cut from the marker.
    expect(stripControlBlocksForStream('Done.\n--CORRELATION-PROPOSAL--\nPattern: x')).toBe('Done.\n');
  });

  it('strips a block whose opener dashes sit on the line above as a rule', async () => {
    // Module 5 walk, 2026-10-27: `---` rule, then the bare marker on its own
    // line; only the closing line was removed and the Pattern body rendered.
    const { stripControlBlocksForStream } = await import('../shared/thread/answer-blocks');
    const text =
      'The deferral preceded the failure [3].\n\n---\nCORRELATION-PROPOSAL\n' +
      'Pattern: deferred PM correlates with bearing failures\nConfidence: high\n' +
      '---END-PROPOSAL---\n\n---FOLLOWUPS---\nQ1: Next?\n';
    const r = parseAnswerBlocks(text);
    expect(r.answer).toBe('The deferral preceded the failure [3].');
    expect(r.followups).toEqual(['Next?']);
    expect(stripControlBlocksForStream('Done.\n\n---\nCORRELATION-PROPOSAL\nPattern: x')).toBe('Done.\n');
    const prose = 'A CORRELATION-PROPOSAL card appears below.';
    expect(parseAnswerBlocks(prose).answer).toBe(prose);
  });
});
