// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Frontend ↔ scoring-boundary parity for the tolerant FOLLOWUPS marker.
 *
 * ONE shared case list — agentic_digital_thread/tests/fixtures/
 * followups_marker_cases.json — is read here (findFollowupsMarker /
 * splitFollowups) AND by tests/unit/test_followups_marker_parity.py
 * (agents/eval_core.py::_FOLLOWUPS_RE / strip_followups). A regex change on
 * either side that the other does not mirror fails one of the two lanes. */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { findFollowupsMarker, splitFollowups } from '../chat/followups-marker';

interface MarkerCase {
  name: string;
  text: string;
  match: boolean;
  body: string;
  block: string | null;
}

const FIXTURE = path.resolve(process.cwd(), '..', 'tests', 'fixtures', 'followups_marker_cases.json');
const { cases } = JSON.parse(readFileSync(FIXTURE, 'utf8')) as { cases: MarkerCase[] };

describe('followups marker — shared parity fixture', () => {
  it('loads a non-trivial case list with matches and non-matches', () => {
    expect(cases.length).toBeGreaterThan(20);
    expect(cases.some((c) => c.match)).toBe(true);
    expect(cases.some((c) => !c.match)).toBe(true);
  });

  for (const c of cases) {
    it(c.name, () => {
      expect(findFollowupsMarker(c.text) !== null).toBe(c.match);
      const { body, block } = splitFollowups(c.text);
      expect(body.trimEnd()).toBe(c.body);
      expect(block).toBe(c.block);
    });
  }
});
