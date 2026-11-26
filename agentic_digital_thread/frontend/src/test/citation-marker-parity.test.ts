// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Client and server parity for where the chat links a citation marker.
 *
 * ONE shared case list, agentic_digital_thread/tests/fixtures/
 * citation_marker_cases.json, is read here (buildAnswerModel: chart tags
 * split out, then injectCitationLinks) AND by
 * tests/unit/test_citation_marker_parity.py (agents/citation_markers.py
 * linked_markers, the grammar the server removes earlier answers' markers
 * by). A change on either side that the other does not mirror fails one of
 * the two lanes. Each linked marker is written {{N}} or {{E<k>}}; a linked
 * group's chips come out space-separated, {{1}} {{2}}. The same cases also
 * check that the footer's earlier chips (citedEarlierNums, which reads the
 * whole answer) count exactly the earlier labels the inline links link. */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { buildAnswerModel } from '../chat/answer-sections';
import { citedEarlierNums } from '../chat/citations';
import type { Citation } from '../chat/turn-model';

interface MarkerCase {
  name: string;
  text: string;
  current: number[];
  earlier: number[];
  linked: string;
}

const FIXTURE = path.resolve(process.cwd(), '..', 'tests', 'fixtures', 'citation_marker_cases.json');
const { cases } = JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: MarkerCase[] };

function citation(num: number, earlier: boolean): Citation {
  return {
    tool: 'sys-a___query_sys_a',
    label: 'Source',
    nodeId: `${earlier ? 'earlier' : 'current'}-${num}`,
    num,
    findingNodeId: null,
    ...(earlier ? { earlier: true } : {}),
  };
}

const LINK_RE = /\[(E?\d+)\]\(<#cite-[^>]*> "[^"]*"\)/g;

function linked(c: MarkerCase): string {
  const cites = [...c.current.map((n) => citation(n, false)), ...c.earlier.map((n) => citation(n, true))];
  const model = buildAnswerModel(c.text, cites);
  expect(model.sections).toEqual([]);
  return model.intro
    .map((p) => (p.kind === 'md' ? p.text.replace(LINK_RE, '{{$1}}') : `<chart-block${p.attrs}>`))
    .join('');
}

describe('citation markers: shared parity fixture', () => {
  it('loads a non-trivial case list', () => {
    expect(cases.length).toBeGreaterThan(30);
    expect(cases.some((c) => c.linked.includes('{{E'))).toBe(true);
    expect(cases.some((c) => c.linked === c.text && c.current.length > 0)).toBe(true);
  });

  for (const c of cases) {
    it(c.name, () => {
      expect(linked(c)).toBe(c.linked);
    });
  }
});

/** The earlier labels a case links, in label order. */
function linkedEarlier(c: MarkerCase): number[] {
  const labels = [...c.linked.matchAll(/\{\{E(\d+)\}\}/g)].map((m) => Number(m[1]));
  return [...new Set(labels)].sort((a, b) => a - b);
}

describe('the footer counts the earlier labels the inline links link', () => {
  // citedEarlierNums decides the footer's "earlier in this chat" chips. It
  // reads the raw answer, so it must walk the same chart-free, outside-code
  // stretches as the inline links (review 2026-10-29: a grouped label in
  // code put a false earlier source in the footer).
  for (const c of cases) {
    it(c.name, () => {
      const cites = [...c.current.map((n) => citation(n, false)), ...c.earlier.map((n) => citation(n, true))];
      const counted = [...citedEarlierNums(cites, c.text)].sort((a, b) => a - b);
      expect(counted).toEqual(linkedEarlier(c));
    });
  }
});
