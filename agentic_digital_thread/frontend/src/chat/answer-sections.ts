// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Collapsible answer sections — STRING-LEVEL port of the old DOM
 * post-processor (chat/collapse-sections.ts
 * collapseResponseSections). The answer markdown is split into sections at the same
 * `##` (h2) boundaries the DOM version used; <AnswerBody/> renders each as
 * <details class="response-section"> with the SAME classes/structure and the
 * SAME default-open rule (group index <= 1, counting the intro group).
 *
 * Gates (identical to the DOM version): at least two h2 headings AND a raw
 * answer of >= 600 characters, else the answer renders un-sectioned.
 *
 * Chart blocks are split out FIRST (charts.ts splitChartBlocks — the old
 * placeholder extraction), then citation links are injected per markdown
 * chunk (citations.ts), then sections are split — so chart JSON is never
 * touched by the citation transform (same order as the old pipeline). */
import { splitChartBlocks, type ChartSplitPart } from './charts';
import { injectCitationLinks } from './citations';
import type { Citation } from './turn-model';

export interface AnswerSection {
  /** The `##` heading's inline markdown (rendered into the <summary>). */
  heading: string;
  parts: ChartSplitPart[];
  /** Old rule: groups with index <= 1 start open (intro group included). */
  defaultOpen: boolean;
}

export interface AnswerModel {
  /** Always-visible parts before the first section — or the WHOLE answer
   * when the collapse gate is off (sections empty). */
  intro: ChartSplitPart[];
  sections: AnswerSection[];
}

/* ATX h2, tightened per review: up to 3 leading spaces are valid (remark
 * renders them as h2 — the gate/split must agree), and an ATX CLOSING
 * sequence (`## Foo ##`) is stripped from the captured heading (the old DOM
 * version read rendered textContent, which never contained it). The closing
 * run must be whitespace-separated (CommonMark) — `## Foo#` keeps its `#`. */
const H2_RE = /^ {0,3}##\s+(.*?)(?:\s+#+)?\s*$/;
const FENCE_RE = /^\s*(```|~~~)/;

/** Build the render model for a final answer: chart split → citation links →
 * section split (h2 boundaries outside code fences). */
/** Body shown under a section heading that has no content of its own. */
export const EMPTY_SECTION_TEXT = '_Nothing found for this section._';

export function buildAnswerModel(answerMd: string, citations: readonly Citation[]): AnswerModel {
  const parts: ChartSplitPart[] = splitChartBlocks(answerMd).map((p) =>
    p.kind === 'md' ? { kind: 'md', text: injectCitationLinks(p.text, citations) } : p,
  );

  // Collapse gate: >= 2 h2 headings (outside fences) and a long answer.
  let h2Count = 0;
  {
    let inFence = false;
    for (const p of parts) {
      if (p.kind !== 'md') continue;
      for (const line of p.text.split('\n')) {
        if (FENCE_RE.test(line)) inFence = !inFence;
        else if (!inFence && H2_RE.test(line)) h2Count++;
      }
    }
  }
  if (h2Count < 2 || answerMd.length < 600) return { intro: parts, sections: [] };

  // Split into intro + heading sections at h2 lines (outside fences).
  const intro: ChartSplitPart[] = [];
  const sections: AnswerSection[] = [];
  let current: AnswerSection | null = null;
  const push = (part: ChartSplitPart) => {
    if (current) current.parts.push(part);
    else intro.push(part);
  };

  let inFence = false;
  for (const p of parts) {
    if (p.kind !== 'md') {
      push(p);
      continue;
    }
    let buf: string[] = [];
    const flushBuf = () => {
      const text = buf.join('\n');
      if (text.trim()) push({ kind: 'md', text });
      buf = [];
    };
    for (const line of p.text.split('\n')) {
      if (FENCE_RE.test(line)) inFence = !inFence;
      const m = !inFence && !FENCE_RE.test(line) ? H2_RE.exec(line) : null;
      if (m) {
        flushBuf();
        current = { heading: m[1], parts: [], defaultOpen: false };
        sections.push(current);
      } else {
        buf.push(line);
      }
    }
    flushBuf();
  }

  // A heading the model wrote with nothing under it (event 29: "Open
  // Shipments at Risk" when the ERP returned no rows) would render as an
  // empty fold; say so instead of showing a blank section.
  for (const sec of sections) {
    if (sec.parts.length === 0) sec.parts.push({ kind: 'md', text: EMPTY_SECTION_TEXT });
  }

  // Default-open rule over the GROUPS sequence ([intro?] + sections): the
  // first two groups start open — so with an intro only the first section
  // opens; without one the first TWO sections open.
  const introExists = intro.length > 0;
  sections.forEach((sec, i) => {
    sec.defaultOpen = i + (introExists ? 1 : 0) <= 1;
  });

  return { intro, sections };
}
