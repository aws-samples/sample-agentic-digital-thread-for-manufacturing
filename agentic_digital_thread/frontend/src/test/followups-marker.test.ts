// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The ONE tolerant FOLLOWUPS marker matcher (src/chat/followups-marker.ts)
 * and every split/harvest site that consumes it. Rehearsal leak: the model
 * emitted a formatting variant of `---FOLLOWUPS---` (`---\nFOLLOWUPS\n---`,
 * `**FOLLOWUPS**`, `## FOLLOWUPS`, `FOLLOWUPS:`), the exact-marker split
 * missed it, and the answer body rendered a bare `FOLLOWUPS` paragraph plus
 * the raw `Q1:`/`A1:` lines. */
import { describe, expect, it } from 'vitest';

import { AnswerSegments } from '../chat/answer-segments';
import { createThreadStore } from '../chat/thread-store';
import { findFollowupsMarker, splitFollowups, FOLLOWUPS_MARKER } from '../chat/followups-marker';
import { parseAnswerBlocks, stripControlBlocksForStream } from '../shared/thread/answer-blocks';
import { cleanBriefingText } from '../views/dashboard/lib';

const BODY = 'Line 2 ran at 92% OEE yesterday.';
const CHIPS = 'Q1: What about line 3?\nA1: Schedule PM';

/** Marker variants observed / expected from the model. */
const VARIANTS: Record<string, string> = {
  canonical: '---FOLLOWUPS---',
  'rules around (blank-separated)': '---\n\nFOLLOWUPS\n\n---',
  'rules around (tight)': '---\nFOLLOWUPS\n---',
  bold: '**FOLLOWUPS**',
  'bold with colon': '**FOLLOWUPS:**',
  heading: '## FOLLOWUPS',
  'heading h3 colon': '### FOLLOWUPS:',
  colon: 'FOLLOWUPS:',
  bare: 'FOLLOWUPS',
};

describe('findFollowupsMarker / splitFollowups', () => {
  it('exports the canonical protocol marker', () => {
    expect(FOLLOWUPS_MARKER).toBe('---FOLLOWUPS---');
  });

  for (const [name, marker] of Object.entries(VARIANTS)) {
    it(`matches the ${name} variant and leaves no label in the body`, () => {
      const { body, block } = splitFollowups(`${BODY}\n\n${marker}\n${CHIPS}\n`);
      expect(body.trim()).toBe(BODY);
      expect(block).not.toBeNull();
      expect(block).toContain('Q1: What about line 3?');
      expect(block).not.toMatch(/FOLLOWUPS/);
    });
  }

  it('still matches the canonical marker mid-line (reserved protocol token)', () => {
    const { body, block } = splitFollowups(`${BODY} ---FOLLOWUPS---\nQ1: x`);
    expect(body.trim()).toBe(BODY);
    expect(block).toContain('Q1: x');
  });

  it('first marker wins', () => {
    const hit = findFollowupsMarker(`a\nFOLLOWUPS:\nQ1: b\n---FOLLOWUPS---\nQ2: c`);
    expect(hit?.start).toBe(2);
  });

  it('never matches prose that mentions followups mid-sentence', () => {
    for (const prose of [
      'We discussed the FOLLOWUPS from the audit yesterday.',
      'The followups: none were filed.',
      'FOLLOWUPS were scheduled for Tuesday.',
      '## FOLLOWUPS from the audit',
      '- FOLLOWUPS',
    ]) {
      expect(findFollowupsMarker(`${BODY}\n${prose}\nMore text.`)).toBeNull();
    }
  });

  it('returns the whole text and a null block when there is no marker', () => {
    expect(splitFollowups(BODY)).toEqual({ body: BODY, block: null });
    expect(splitFollowups('')).toEqual({ body: '', block: null });
  });
});

describe('parseAnswerBlocks — tolerant split', () => {
  for (const [name, marker] of Object.entries(VARIANTS)) {
    it(`splits chips on the ${name} variant`, () => {
      const r = parseAnswerBlocks(`${BODY}\n\n${marker}\n${CHIPS}\n`);
      expect(r.answer).toBe(BODY);
      expect(r.followups).toEqual(['What about line 3?']);
      expect(r.actions).toEqual(['Schedule PM']);
    });

    it(`an empty ${name} block yields no chips and no leftover label`, () => {
      const r = parseAnswerBlocks(`${BODY}\n\n${marker}\n`);
      expect(r.answer).toBe(BODY);
      expect(r.followups).toEqual([]);
      expect(r.actions).toEqual([]);
    });
  }

  it('keeps prose mentioning followups intact', () => {
    const text = `${BODY}\nThe FOLLOWUPS from the audit are closed.`;
    expect(parseAnswerBlocks(text).answer).toBe(text);
  });
});

describe('stripControlBlocksForStream — tolerant cut', () => {
  for (const [name, marker] of Object.entries(VARIANTS)) {
    it(`cuts an opened ${name} block mid-stream`, () => {
      const out = stripControlBlocksForStream(`${BODY}\n\n${marker}\nQ1: partial qu`);
      expect(out.trim()).toBe(BODY);
    });
  }
});

describe('AnswerSegments.reattachFollowups — tolerant harvest', () => {
  for (const [name, marker] of Object.entries(VARIANTS)) {
    it(`harvests a ${name} block from a discarded short run`, () => {
      const s = new AnswerSegments();
      s.pushToken('This is the long main answer run that wins the selection. '.repeat(5));
      s.onToolBoundary();
      s.pushToken(`${marker}\n${CHIPS}`);
      const r = parseAnswerBlocks(s.finalize());
      expect(r.followups).toEqual(['What about line 3?']);
      expect(r.answer).not.toMatch(/FOLLOWUPS/);
    });
  }

  it('does not re-append when the selected answer already carries a variant', () => {
    const s = new AnswerSegments();
    s.pushToken('Main answer. '.repeat(30) + '\n**FOLLOWUPS**\nQ1: One?');
    expect(s.finalize().match(/FOLLOWUPS/g)?.length).toBe(1);
  });
});

describe('thread-store done — tolerant chips', () => {
  it('splits a `---\\nFOLLOWUPS\\n---` variant into chips', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.applyEvent('token', { text: `${BODY}\n\n---\nFOLLOWUPS\n---\n${CHIPS}\n` });
    store.applyEvent('done', {});
    const { turns } = store.getState();
    const a = turns[turns.length - 1].assistant;
    expect(a.answerMd).toBe(BODY);
    expect(a.followups).toEqual(['What about line 3?']);
    expect(a.actions).toEqual(['Schedule PM']);
  });
});

describe('cleanBriefingText — tolerant strip', () => {
  for (const [name, marker] of Object.entries(VARIANTS)) {
    it(`strips a ${name} block from the briefing`, () => {
      expect(cleanBriefingText(`${BODY}\n${marker}\n${CHIPS}`)).toBe(BODY);
    });
  }
});
