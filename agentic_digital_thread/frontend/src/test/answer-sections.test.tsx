// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the string-level answer pipeline (zero-innerHTML migration):
 * buildAnswerModel (chat/answer-sections.ts — the port of the old DOM
 * collapseResponseSections semantics + chart split + citation links) and the
 * rendered <AnswerBody/> structure (same .response-section classes /
 * default-open rules as the DOM post-processor, plus toggle persistence
 * across re-renders). Edge cases mined from the old collapse tests. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { buildAnswerModel, EMPTY_SECTION_TEXT } from '../chat/answer-sections';
import { AnswerBody } from '../chat/AnswerBody';
import { injectCitationLinks } from '../chat/citations';
import {
  buildChartSrcdoc,
  splitChartBlocks,
  stripChartBlocks,
  stripChartBlocksForStream,
} from '../chat/charts';
import type { Citation } from '../chat/turn-model';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PAD = 'x'.repeat(700); // >600 chars so the length gate passes

/** Old-fixture shape: intro + three h2 sections (padded past the gate). */
const SECTIONED =
  `Intro paragraph.\n\n## First *section*\n\nBody one. ${PAD}\n\n## Second\n\nBody two.\n\n- item\n\n## Third\n\nBody three.`;

const NO_CITES: Citation[] = [];

describe('buildAnswerModel — collapse gates (old collapseResponseSections)', () => {
  it('does not section short responses', () => {
    const model = buildAnswerModel('Intro\n\n## A\n\nbody\n\n## B\n\nbody', NO_CITES);
    expect(model.sections).toEqual([]);
    expect(model.intro.length).toBeGreaterThan(0);
  });

  it('does not section with fewer than two h2 headings', () => {
    const model = buildAnswerModel(`Intro ${PAD}\n\n## Only\n\nBody.`, NO_CITES);
    expect(model.sections).toEqual([]);
  });

  it('ignores ## lines inside code fences for gating and splitting', () => {
    const md = `Intro ${PAD}\n\n\`\`\`\n## not a heading\n## also not\n\`\`\`\n\n## Real\n\nbody`;
    const model = buildAnswerModel(md, NO_CITES);
    expect(model.sections).toEqual([]); // only ONE real h2
  });

  it('splits intro + sections with the old default-open rule (intro present)', () => {
    const model = buildAnswerModel(SECTIONED, NO_CITES);
    expect(model.intro.length).toBe(1);
    expect(model.sections.map((s) => s.heading)).toEqual(['First *section*', 'Second', 'Third']);
    // groups = [intro, First, Second, Third] → idx <= 1 → only First opens.
    expect(model.sections.map((s) => s.defaultOpen)).toEqual([true, false, false]);
  });

  it('opens the first TWO sections when there is no intro (chat.js parity)', () => {
    const md = `## First\n\nBody one. ${PAD}\n\n## Second\n\nBody two.\n\n## Third\n\nBody three.`;
    const model = buildAnswerModel(md, NO_CITES);
    expect(model.intro).toEqual([]);
    expect(model.sections.map((s) => s.defaultOpen)).toEqual([true, true, false]);
  });

  it('keeps chart parts inside the section where they appear', () => {
    const md = `Intro. ${PAD}\n\n## A\n\ntext\n<chart-block type='line' labels='[1]' datasets='[]'/>\nmore\n\n## B\n\nbody`;
    const model = buildAnswerModel(md, NO_CITES);
    const a = model.sections[0];
    expect(a.parts.some((p) => p.kind === 'chart')).toBe(true);
    expect(model.sections[1].parts.every((p) => p.kind === 'md')).toBe(true);
  });

  it('strips ATX closing sequences from summaries and honors 1-3 space indents', () => {
    // `## Foo ##` renders as h2 "Foo" (remark strips the closing run) and an
    // indented `  ## Bar` is still a valid h2 — the gate/split must agree.
    const md = `Intro. ${PAD}\n\n## First ##\n\nbody one\n\n  ## Second\n\nbody two`;
    const model = buildAnswerModel(md, NO_CITES);
    expect(model.sections.map((s) => s.heading)).toEqual(['First', 'Second']);
  });

  it('keeps a non-whitespace-separated trailing # in the heading (CommonMark)', () => {
    const md = `Intro. ${PAD}\n\n## Line #4\n\nbody\n\n## Bay C#\n\nbody`;
    const model = buildAnswerModel(md, NO_CITES);
    expect(model.sections.map((s) => s.heading)).toEqual(['Line #4', 'Bay C#']);
  });
});

describe('injectCitationLinks (string-level cite pre-transform)', () => {
  const CITES: Citation[] = [
    { tool: 'query_mes', label: 'MES "Mil"', nodeId: 'system-query_mes', num: 1, findingNodeId: 'finding-7' },
  ];

  it('replaces [N] with a #cite markdown link targeting the finding node', () => {
    const out = injectCitationLinks('OEE is 92% [1].', CITES);
    expect(out).toBe('OEE is 92% [1](<#cite-finding-7> "MES \\"Mil\\"").');
  });

  it('falls back to the system node when there is no finding', () => {
    const cites = [{ ...CITES[0], findingNodeId: null }];
    const out = injectCitationLinks('See [1].', cites);
    expect(out).toContain('#cite-system-query_mes');
  });

  it('leaves [N] inside code fences untouched', () => {
    const out = injectCitationLinks('```\narr[1] = 2\n```\nAnd [1].', CITES);
    expect(out).toContain('arr[1] = 2');
    expect(out.match(/#cite-/g)).toHaveLength(1);
  });

  it('leaves existing markdown links [N](…) untouched', () => {
    const out = injectCitationLinks('[1](https://example.com) and [1]', CITES);
    expect(out).toContain('[1](https://example.com)');
    expect(out.match(/#cite-/g)).toHaveLength(1);
  });

  it('does not match [1] inside [12]', () => {
    const out = injectCitationLinks('See [12] here.', CITES);
    expect(out).toBe('See [12] here.');
  });

  it('leaves [N] inside INLINE code spans untouched', () => {
    const out = injectCitationLinks('Run `status [1]` then see [1].', CITES);
    expect(out).toContain('`status [1]`');
    expect(out.match(/#cite-/g)).toHaveLength(1);
  });

  it('handles multi-backtick code spans and replaces outside them', () => {
    const out = injectCitationLinks('`` a[1] `` and [1] and `b[1]`', CITES);
    expect(out).toContain('`` a[1] ``');
    expect(out).toContain('`b[1]`');
    expect(out.match(/#cite-/g)).toHaveLength(1);
  });
});

describe('splitChartBlocks / stripChartBlocks', () => {
  it('splits markdown around chart-block tags (attrs captured)', () => {
    const parts = splitChartBlocks("before\n<chart-block type='bar' labels='[]'/>\nafter");
    expect(parts.map((p) => p.kind)).toEqual(['md', 'chart', 'md']);
    expect(parts[1].kind === 'chart' && parts[1].attrs).toContain("type='bar'");
  });

  it('strips chart-block tags for the Word export payload', () => {
    expect(stripChartBlocks("a\n<chart-block type='bar'/>\nb")).toBe('a\n\nb');
  });

  it('stream-strip also hides a partial trailing chart tag', () => {
    expect(stripChartBlocksForStream("a\n<chart-block type='bar'/>\nb")).toBe('a\n\nb');
    expect(stripChartBlocksForStream("a\n<chart-block type='line' labels='[\"x")).toBe('a\n');
    expect(stripChartBlocksForStream('untouched text')).toBe('untouched text');
  });

  it('srcdoc escapes </script> inside the chart config JSON (review hardening)', () => {
    const srcdoc = buildChartSrcdoc({
      data: { datasets: [{ label: '</script><script>alert(1)</script>' }] },
    });
    // The payload cannot close the script context — `<` is \u003c-escaped;
    // the only literal </script> occurrences are the template's own two.
    expect(srcdoc).not.toContain('</script><script>alert(1)');
    expect(srcdoc).toContain('\\u003c/script>\\u003cscript>');
  });
});

describe('<AnswerBody/> rendered structure', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const render = (md: string, cites: Citation[] = NO_CITES) =>
    act(() => root.render(<AnswerBody answerMd={md} citations={cites} />));

  it('wraps each h2 section in <details class="response-section"> with the old classes', () => {
    render(SECTIONED);
    const details = [...container.querySelectorAll<HTMLDetailsElement>('details.response-section')];
    expect(details).toHaveLength(3);
    // Intro stays always-visible content before the sections.
    expect(container.textContent).toContain('Intro paragraph.');
    // Default-open: first section only (intro counts as group 0).
    expect(details.map((d) => d.open)).toEqual([true, false, false]);
    // Summary carries the heading's INLINE markdown (the old innerHTML copy).
    const summaries = [...container.querySelectorAll('summary.response-section-summary')];
    expect(summaries).toHaveLength(3);
    expect(summaries[0].querySelector('em')?.textContent).toBe('section');
    // Bodies carry the section content.
    const bodies = [...container.querySelectorAll('.response-section-body')];
    expect(bodies).toHaveLength(3);
    expect(bodies[1].querySelector('ul li')?.textContent).toBe('item');
    // No leftover h2s — headings moved into the summaries.
    expect(container.querySelectorAll('h2')).toHaveLength(0);
  });

  it('renders short answers un-sectioned', () => {
    render('Just a **short** answer.');
    expect(container.querySelector('details.response-section')).toBeNull();
    expect(container.querySelector('strong')?.textContent).toBe('short');
  });

  it('preserves the user toggle across re-renders (streaming rev bumps)', () => {
    render(SECTIONED);
    const second = [...container.querySelectorAll<HTMLDetailsElement>('details.response-section')][1];
    expect(second.open).toBe(false);
    // User opens the second section (native toggle event).
    act(() => {
      second.open = true;
      second.dispatchEvent(new Event('toggle', { bubbles: true }));
    });
    // A re-render with the same props must keep the user's state.
    render(SECTIONED);
    const after = [...container.querySelectorAll<HTMLDetailsElement>('details.response-section')][1];
    expect(after.open).toBe(true);
  });

  it('interleaves sandboxed chart iframes with markdown chunks', () => {
    const md = `Numbers below. ${PAD}\n<chart-block type='line' title='OEE' labels='["a","b"]' datasets='[{"label":"x","data":[1,2]}]'/>\nDone.`;
    render(md);
    const iframe = container.querySelector('iframe')!;
    expect(iframe).not.toBeNull();
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts');
    expect(iframe.getAttribute('srcdoc')).toContain('chart.umd.min.js');
    expect(container.textContent).toContain('Numbers below.');
    expect(container.textContent).toContain('Done.');
  });

  it('renders citation badges from [N] markers (delegated-click contract)', () => {
    const cites: Citation[] = [
      { tool: 'query_mes', label: 'MES', nodeId: 'system-query_mes', num: 1, findingNodeId: 'finding-7' },
    ];
    render('OEE is 92% [1].', cites);
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.dataset.citeTarget).toBe('finding-7');
    expect(badge.textContent).toBe('1');
    expect(badge.title).toBe('MES');
  });

  it('renders injected citations whose labels contain dashes, quotes, and newlines', () => {
    const cites: Citation[] = [
      {
        tool: 'query_mes',
        label: 'MES — Quality "Active"\nCedar Rapids',
        nodeId: 'system-query_mes',
        num: 4,
        findingNodeId: 'finding-18',
      },
    ];
    render('Cedar Rapids — Quality Failures Active Today 1 [4].', cites);
    const badge = container.querySelector<HTMLAnchorElement>('a.cite-badge')!;
    expect(badge).not.toBeNull();
    expect(badge.dataset.citeTarget).toBe('finding-18');
    expect(badge.textContent).toBe('4');
    expect(badge.title).toBe('MES — Quality "Active"\nCedar Rapids');
    expect(container.textContent).not.toContain('[4](<#cite-');
  });
});

describe('buildAnswerModel: a heading with nothing under it', () => {
  it('fills an empty section with a plain "nothing found" line', () => {
    const md = `Intro. ${PAD}\n\n## Work orders\n\nWO one.\n\n## Open shipments at risk\n\n## Sources\n\n[1] ERP`;
    const model = buildAnswerModel(md, NO_CITES);
    const empty = model.sections.find((s) => s.heading === 'Open shipments at risk');
    expect(empty?.parts).toEqual([{ kind: 'md', text: EMPTY_SECTION_TEXT }]);
    const full = model.sections.find((s) => s.heading === 'Work orders');
    expect(full?.parts.some((p) => p.kind === 'md' && p.text.includes('WO one.'))).toBe(true);
  });
});
