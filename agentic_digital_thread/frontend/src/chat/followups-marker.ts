// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The ONE tolerant ---FOLLOWUPS--- marker matcher.
 *
 * The orchestrator is prompted to emit the exact protocol line
 * `---FOLLOWUPS---` before its Q1:/A1: suggestion lines, but models drift
 * into formatting variants (`---\nFOLLOWUPS\n---`, `**FOLLOWUPS**`,
 * `## FOLLOWUPS`, `FOLLOWUPS:`). An exact-string split missed those and the
 * answer body rendered a bare `FOLLOWUPS` paragraph plus the raw chip lines.
 *
 * Every split/harvest site (parseAnswerBlocks, stripControlBlocksForStream,
 * AnswerSegments.reattachFollowups, cleanBriefingText) goes through
 * findFollowupsMarker — never a second regex. Mirror of
 * agents/eval_core.py::strip_followups (the scoring boundary); parity is
 * pinned by ONE shared case list, tests/fixtures/followups_marker_cases.json,
 * read by both vitest and pytest.
 *
 * Matches:
 *  - the canonical `---FOLLOWUPS---` token anywhere (reserved protocol);
 *  - a LINE that is only the word FOLLOWUPS, optionally wrapped in dashes /
 *    asterisks / underscores, prefixed by heading hashes, suffixed by a
 *    colon — plus an optional `---` rule directly above it (blank lines
 *    allowed between), so the rule does not survive as a trailing <hr>.
 * Prose mentioning followups mid-sentence never matches (line-anchored). */

export const FOLLOWUPS_MARKER = '---FOLLOWUPS---';

const RULE = String.raw`[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*\n(?:[ \t]*\n)*`;
// Wrapping dashes/asterisks must hug the word so a bullet ("- FOLLOWUPS")
// stays prose.
const LABEL = String.raw`[ \t]*(?:#{1,6}[ \t]*)?[-*_]*FOLLOWUPS[-*_]*[ \t]*:?[-*_]*[ \t]*`;
const MARKER_RE = new RegExp(
  String.raw`(?:^|\n)(?:${RULE})?${LABEL}(?=\n|$)|---FOLLOWUPS---`,
);

export interface FollowupsMarkerHit {
  /** Index where the marker region (incl. a directly-preceding rule) starts. */
  start: number;
  /** Index just past the marker line — the chip block begins here. */
  end: number;
}

/** Locate the FIRST FOLLOWUPS marker in `text`, or null when absent. */
export function findFollowupsMarker(text: string | null | undefined): FollowupsMarkerHit | null {
  const m = MARKER_RE.exec(text || '');
  if (!m) return null;
  // The line alternatives consume their leading newline; keep it in the body
  // (the pre-tolerant exact split left it there too).
  const lead = m[0].startsWith('\n') ? 1 : 0;
  return { start: m.index + lead, end: m.index + m[0].length };
}

/** Split `text` at the first FOLLOWUPS marker: `body` is everything before
 * it (untrimmed), `block` the chip payload after the marker line — null
 * when there is no marker (body is then the whole text). */
export function splitFollowups(text: string | null | undefined): {
  body: string;
  block: string | null;
} {
  const src = text || '';
  const hit = findFollowupsMarker(src);
  if (!hit) return { body: src, block: null };
  return { body: src.slice(0, hit.start), block: src.slice(hit.end) };
}
