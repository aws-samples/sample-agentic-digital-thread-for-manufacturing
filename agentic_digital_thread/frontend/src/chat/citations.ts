// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Citation-badge pre-transform (zero-innerHTML migration).
 *
 * The old pipeline replaced `[N]` with an anchor HTML string AFTER
 * sanitization. Markdown now renders as React elements, so the transform
 * moved to the STRING level: `[N]` becomes a markdown link
 * `[N](<#cite-TARGET> "label")`, and the shared <Md/> `a` override renders
 * any `#cite-*` link as the `.cite-badge` anchor with `data-cite-target`
 * (same class/data-attribute contract — the delegated thread click handler
 * keeps firing).
 *
 * Behavior deltas vs the post-HTML string replace (all deliberate):
 * - `[N]` inside fenced code blocks stays literal text (the old replace
 *   injected a live anchor into <code> content);
 * - `[N]` inside INLINE code spans stays literal text too (the
 *   markdown-level replace would otherwise leave raw link markup visible
 *   inside the rendered <code>);
 * - `[N](` — an existing markdown link with that text — is left alone
 *   (negative lookahead), where the old HTML-level replace never saw it.
 *
 * A grouped marker (`[1, 2]`, `[1,2]`, `[E1, E2]`, `[1, E1]`) is linked as
 * one chip per member, in order, when EVERY member names a citation of the
 * answer; a group with any other member stays exactly as written (an interval
 * `[4, 7]` is not a citation). Review 2026-10-29: an answer that grouped its
 * citations showed plain text, and a follow-up citing only earlier results
 * that way had no Sources row.
 *
 * Two shapes are never a citation, whatever their members (review
 * 2026-10-29): a run of groups right after an ASCII letter or digit, `_`, `]`
 * or `)` (an index `sensor[1]`, `a[1, 2]`, `m[1][2]`, `f(x)[1]`, a
 * reference-link label `[text][1]`), and a footnote definition, a run that
 * starts its line and is followed by `:` (`[1]: text`). The footer's earlier
 * chips (citedEarlierNums) read the answer through the same walk as the
 * inline links, so a label in code, a fence or a chart tag is counted by
 * neither.
 *
 * The server keeps a copy of this grammar (agents/citation_markers.py) to
 * find the citation markers of an earlier answer; one shared case list
 * (tests/fixtures/citation_marker_cases.json, run by
 * test/citation-marker-parity.test.ts and its Python twin) pins the two. */
import { splitChartBlocks } from './charts';
import type { Citation } from './turn-model';

/** A fence line: optional indent, then a run of three or more backticks or
 * tildes. */
const FENCE_RE = /^\s*(`{3,}|~{3,})/;

/** True when `line` closes a block opened by the run `fence`: a run of the
 * same character at least as long, and nothing else on the line
 * (CommonMark). A four-backtick block may hold a three-backtick line, and a
 * tilde block a backtick line (review 2026-10-29). */
function closesFence(line: string, fence: string): boolean {
  const m = FENCE_RE.exec(line);
  return (
    m !== null &&
    m[1][0] === fence[0] &&
    m[1].length >= fence.length &&
    line.slice(m[0].length).trim() === ''
  );
}

/** Apply `fn` to every line OUTSIDE fenced code blocks (``` / ~~~). */
function mapOutsideFences(md: string, fn: (line: string) => string): string {
  let fence: string | null = null;
  return md
    .split('\n')
    .map((line) => {
      if (fence !== null) {
        if (closesFence(line, fence)) fence = null;
        return line;
      }
      const opened = FENCE_RE.exec(line);
      if (opened) {
        fence = opened[1];
        return line;
      }
      return fn(line);
    })
    .join('\n');
}

/** Inline code spans (`` `…` ``, backtick runs matched by an equal-length
 * closing run — the CommonMark rule). Applied per line so a `[N]` inside a
 * code span stays literal (the replace previously produced raw
 * link markup INSIDE the rendered <code>). */
const CODE_SPAN_RE = /(`+)(.+?)\1/g;

/** Apply `fn` to the segments of a line OUTSIDE inline code spans;
 * `lineStart` is true for the segment the line starts with. */
function mapOutsideCodeSpans(
  line: string,
  fn: (segment: string, lineStart: boolean) => string,
): string {
  CODE_SPAN_RE.lastIndex = 0;
  let out = '';
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = CODE_SPAN_RE.exec(line)) !== null) {
    out += fn(line.slice(last, m.index), last === 0) + m[0];
    last = m.index + m[0].length;
  }
  return out + fn(line.slice(last), last === 0);
}

/** Escape a string for a double-quoted markdown link title. */
function escapeLinkTitle(s: string): string {
  return s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/** The `#cite-*` markdown link for one citation, shown as `text`. */
function citeLink(s: Citation, text: string): string {
  const target = s.findingNodeId || s.nodeId;
  return `[${text}](<#cite-${encodeURIComponent(target)}> "${escapeLinkTitle(s.label)}")`;
}

/** A citation marker group: `[` one or more members separated by commas
 * (spaces around a comma allowed) `]`, not followed by `(` (a markdown link).
 * A member is a number or an earlier label `E<k>` of one or two digits, the
 * fixed label the runtime gives a result an EARLIER question in this chat
 * returned (agents/citation_ordinals.py). A FIXED regex: no citation value is
 * ever interpolated into a pattern. Global, so use it only with
 * `String.replace` or `matchAll`, neither of which keeps its `lastIndex`. */
export const CITATION_GROUP_RE = /\[((?:E\d{1,2}|\d+)(?: *, *(?:E\d{1,2}|\d+))*)\](?!\()/g;

/** Groups written side by side, `[1][2]`: the unit `isRefused` judges.
 * Fixed and global, like `CITATION_GROUP_RE`. */
const CITATION_RUN_RE = /(?:\[(?:E\d{1,2}|\d+)(?: *, *(?:E\d{1,2}|\d+))*\](?!\())+/g;

/** A run right after one of these belongs to the text before it. ASCII
 * only, so a marker right after a letter of another script still links. */
const ATTACHED_RE = /[A-Za-z0-9_\])]/;

/** What may come before a footnote definition on its line. */
const INDENT_RE = /^[ \t]*$/;

/** True when the run `run` at `offset` of `segment` is never a citation:
 * it follows an attached character, or it is a footnote definition (it
 * starts its line and `:` follows it). Read within the segment, as the
 * server reads it (agents/citation_markers.py `_refused`). */
function isRefused(segment: string, offset: number, run: string, lineStart: boolean): boolean {
  if (offset > 0 && ATTACHED_RE.test(segment[offset - 1])) return true;
  return (
    lineStart && segment[offset + run.length] === ':' && INDENT_RE.test(segment.slice(0, offset))
  );
}

interface Numbered {
  num: number;
  earlier?: boolean;
}

/** This turn's citations by number (the first wins, as the old per-citation
 * replace did) and its earlier citations by label number (the last wins). */
function indexCitations<C extends Numbered>(
  citations: readonly C[],
): { current: Map<number, C>; earlier: Map<number, C> } {
  const current = new Map<number, C>();
  const earlier = new Map<number, C>();
  for (const s of citations) {
    if (s.earlier) earlier.set(s.num, s);
    else if (!current.has(s.num)) current.set(s.num, s);
  }
  return { current, earlier };
}

/** The citations a group's members (`CITATION_GROUP_RE` group 1) name, in
 * order, or null when any member names none: a number this turn never gave
 * out or does not print that way (`[01]` is not `[1]`), or an earlier label
 * the turn does not have (`E01` reads as `E1`). A bare number never names an
 * earlier citation. */
function resolveGroup<C extends Numbered>(
  members: string,
  current: ReadonlyMap<number, C>,
  earlier: ReadonlyMap<number, C>,
): C[] | null {
  const named: C[] = [];
  for (const raw of members.split(',')) {
    const m = raw.trim();
    let s: C | undefined;
    if (m[0] === 'E') s = earlier.get(Number(m.slice(1)));
    else if (String(Number(m)) === m) s = current.get(Number(m));
    if (!s) return null;
    named.push(s);
  }
  return named;
}

/** The one walk the inline links and the footer's earlier chips share:
 * every group of `md` (one markdown part, chart tags already split out)
 * outside fenced blocks, fence lines and inline code spans, in a run that is
 * not refused, whose every member names a citation. `fn` gets the citations
 * it names, in order, and returns the group's replacement. */
function mapCitedGroups<C extends Numbered>(
  md: string,
  citations: readonly C[],
  fn: (named: C[]) => string,
): string {
  const { current, earlier } = indexCitations(citations);
  const groups = (whole: string, members: string) => {
    const named = resolveGroup(members, current, earlier);
    return named ? fn(named) : whole;
  };
  return mapOutsideFences(md, (line) =>
    mapOutsideCodeSpans(line, (seg, lineStart) =>
      seg.replace(CITATION_RUN_RE, (run: string, offset: number) =>
        isRefused(seg, offset, run, lineStart) ? run : run.replace(CITATION_GROUP_RE, groups),
      ),
    ),
  );
}

/** The label numbers of the earlier citations `text` (a whole answer, chart
 * tags included) cites, alone or in a group the inline links expand: the
 * footer's earlier chips (provenance.ts citedEarlierCitations). It reads the
 * answer through the same walk as the inline links (`mapCitedGroups` over
 * each markdown part between chart tags, as buildAnswerModel splits them),
 * so a label in code, a fence or a chart tag is never counted. */
export function citedEarlierNums<C extends Numbered>(
  citations: readonly C[],
  text: string,
): Set<number> {
  const cited = new Set<number>();
  for (const part of splitChartBlocks(text)) {
    if (part.kind !== 'md') continue;
    mapCitedGroups(part.text, citations, (named) => {
      for (const s of named) if (s.earlier) cited.add(s.num);
      return '';
    });
  }
  return cited;
}

/** Replace each citation marker with `#cite-*` markdown links, one per
 * member of a group, separated by a space. The angle-bracket destination
 * keeps arbitrary target ids valid; the target is percent-encoded here and
 * decoded by the <Md/> anchor override. This turn's citations link `[N]`;
 * earlier citations (`earlier: true`) link their `[E<k>]` label, never
 * `[N]`. */
export function injectCitationLinks(md: string, citations: readonly Citation[]): string {
  if (!citations.length) return md;
  return mapCitedGroups(md, citations, (named) =>
    named.map((s) => citeLink(s, s.earlier ? `E${s.num}` : String(s.num))).join(' '),
  );
}
