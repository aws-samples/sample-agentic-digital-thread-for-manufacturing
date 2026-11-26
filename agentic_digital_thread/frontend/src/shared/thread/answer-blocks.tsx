// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Answer control-block parsing (parseAnswerBlocks).
 * The orchestrator appends machine-readable control blocks
 * to its prose answer: ---VALUE-MAPPING-PROPOSAL---/---CORRELATION-PROPOSAL---
 * (stripped from display) and a trailing ---FOLLOWUPS--- block of `Q1:`/`A1:`
 * lines (rendered as chips).
 *
 * CANONICAL module (Task 3.2 dedupe): both chat surfaces (src/chat) and the
 * thread/data-modal consume this single implementation — the former
 * src/chat/control-blocks.ts copy was removed so the two never drift.
 * The FOLLOWUPS split goes through the ONE tolerant matcher
 * (chat/followups-marker.ts). */

import { findFollowupsMarker, splitFollowups } from '../../chat/followups-marker';
import { useI18n } from '../../lib/i18n';

/* The proposal protocol's markers, matched tolerantly: two or more dashes,
 * optional spaces inside the marker, hyphen or space between the words. The
 * model writes `--VALUE-MAPPING-PROPOSAL--`, `---- CORRELATION PROPOSAL ----`
 * and `--- END-PROPOSAL ---` often enough that an exact three-dash match let
 * the raw block through (briefing bullets 2026-09-14; event 26 module 3,
 * 2026-10-04, where the closing sentinel arrived with no opener and the
 * literal line rendered under the refusal). The opener also arrives with its
 * dashes on the line above, as a horizontal rule (`---` then
 * `CORRELATION-PROPOSAL` alone on the next line; module 5 walk, 2026-10-27), so
 * a marker alone on its line opens the block too, taking the rule line with it.
 * Same shapes as agents/public_answer.py _PROPOSAL_OPEN_SRC. */
const PROPOSAL_NAME_SRC = '(?:VALUE[- ]MAPPING|CORRELATION)[- ]PROPOSAL';
const PROPOSAL_OPEN_SRC =
  `(?:-{2,}[ \\t]*${PROPOSAL_NAME_SRC}[ \\t]*-{2,}` +
  `|(?:^|\\n)(?:[ \\t]*-{3,}[ \\t]*\\n)?[ \\t]*${PROPOSAL_NAME_SRC}[ \\t]*(?=\\n|$))`;
const PROPOSAL_END_SRC = '-{2,}[ \\t]*END[- ]PROPOSAL[ \\t]*-{2,}';
const PROPOSAL_BLOCK_RE = new RegExp(`${PROPOSAL_OPEN_SRC}[\\s\\S]*?${PROPOSAL_END_SRC}`, 'g');
const ORPHAN_END_RE = new RegExp(`^[ \\t]*${PROPOSAL_END_SRC}[ \\t]*$\\n?`, 'gm');
const PROPOSAL_OPEN_RE = new RegExp(PROPOSAL_OPEN_SRC);
/** The self-learning counter line, bare or inside exactly one matching wrapper
 * from this list (a saved briefing, 2026-10-17, wrote it in italics). The same
 * list and line rule as agents/public_answer.py CANDIDATE_PAIR_WRAPPERS, which
 * the server applies to every streamed answer; a Python test pins the two lists
 * equal. Longest first. */
const CANDIDATE_COUNTER_WRAPPERS = ['**', '__', '*', '_'] as const;
const CANDIDATE_COUNTER_RE = new RegExp(
  '^[ \\t]*(' +
    CANDIDATE_COUNTER_WRAPPERS.map((w) => w.replace(/[*_]/g, '\\$&')).join('|') +
    '|)additional_candidate_pairs_observed:[ \\t]*\\d+[ \\t]*\\1[ \\t]*$\\n?',
  'gmi',
);

/** Remove every COMPLETE proposal block, any closing sentinel left on its own
 * line, and the self-learning counter line (`additional_candidate_pairs_observed: N`),
 * all machine payload the orchestrator appends for the accept/reject cards. */
export function stripProposalBlocks(text: string): string {
  return (text || '')
    .replace(PROPOSAL_BLOCK_RE, '')
    .replace(ORPHAN_END_RE, '')
    .replace(CANDIDATE_COUNTER_RE, '');
}

/** Cut from an OPENED proposal marker (terminator not present) to the end:
 * everything after it is machine payload. Identity when none is open. */
export function cutOpenedProposal(text: string): string {
  const opened = text.search(PROPOSAL_OPEN_RE);
  return opened === -1 ? text : text.slice(0, opened);
}

export interface ParsedAnswer {
  answer: string;
  followups: string[];
  actions: string[];
}

/** A `Qn:`/`An:` line that carries no suggestion: the model's "nothing here"
 * placeholders (`None`, `N/A`, `none.`, `-`). Rendered as a chip they read as
 * a bug (event 23, 2026-10-02: a literal "None" chip under Agent Analysis). */
const PLACEHOLDER_RE = /^\s*(?:none|n\/?a|nothing|null|-+|\.+)?\s*[.!]?\s*$/i;

function usableSuggestion(text: string): boolean {
  return !PLACEHOLDER_RE.test(text);
}

export function parseAnswerBlocks(text: string | null | undefined): ParsedAnswer {
  let answer = stripProposalBlocks(text || '');
  const followups: string[] = [];
  const actions: string[] = [];
  const { body, block } = splitFollowups(answer);
  if (block !== null) {
    answer = body.trim();
    for (const m of block.matchAll(/Q\d+:\s*(.+)/g)) {
      const q = m[1].trim();
      if (usableSuggestion(q)) followups.push(q);
    }
    for (const m of block.matchAll(/A\d+:\s*(.+)/g)) {
      const a = m[1].trim();
      if (usableSuggestion(a)) actions.push(a);
    }
  }
  return { answer, followups, actions };
}

/** Live-stream suppression of control blocks (UX: the raw
 * ---FOLLOWUPS---/proposal text must never flash in the chat while tokens
 * stream — the chips/cards render only at finalize).
 *
 * Three layers, cheapest last-wins:
 * 1. complete proposal blocks are removed (same regexes as parseAnswerBlocks);
 * 2. an OPENED control block (marker seen, terminator not yet streamed) is cut
 *    from the marker onward — everything after it is machine payload;
 * 3. a PARTIAL trailing marker (e.g. the stream ends mid-"---FOLLO") is cut so
 *    the marker never flickers into view token-by-token. A genuine markdown
 *    horizontal rule re-appears as soon as the next token disambiguates it.
 */
export function stripControlBlocksForStream(text: string): string {
  let out = stripProposalBlocks(text || '');
  // 2. opened-but-unterminated block: cut at the first surviving marker.
  const followups = findFollowupsMarker(out);
  if (followups) out = out.slice(0, followups.start);
  out = cutOpenedProposal(out);
  // 3. partial trailing marker: bare trailing dashes or a dash-prefixed
  //    uppercase run at the very end of the streamed text.
  out = out.replace(/(?:^|\n)-{1,3}[A-Z-]*$/, '');
  return out;
}

/** Follow-up chip row (buildFollowupChips → <FollowupChips/>,
 * jsx-migration W3.1). Chips are real `<button>`s (keyboard-reachable,
 * announced as controls) and always carry `data-question`; pass `onAsk` for
 * a direct per-chip handler (the data modal), or omit it and rely on a
 * delegated click handler reading `data-question` (the chat thread —
 * thread-clicks.ts). The FIRST chip is the main next step
 * (`.suggestion-chip-primary`): the first follow-up question when there is
 * one, else the first recommended action. Actions (`.action-chip`) read as
 * recommendations, not warnings, and their accessible name says so.
 * Renders null when there is nothing to show. */
export function FollowupChips({
  followups,
  actions,
  onAsk,
}: {
  followups?: string[];
  actions?: string[];
  onAsk?: (question: string) => void;
}) {
  const { t } = useI18n();
  const qs = followups || [];
  const as = actions || [];
  if (!qs.length && !as.length) return null;
  const chip = (text: string, kind: 'question' | 'action', primary: boolean) => (
    <button
      type="button"
      key={`${kind}:${text}`}
      className={`suggestion-chip${kind === 'action' ? ' action-chip' : ''}${
        primary ? ' suggestion-chip-primary' : ''
      }`}
      data-question={text}
      aria-label={kind === 'action' ? `${t('chat.actionPrefix')} ${text}` : undefined}
      onClick={onAsk ? () => onAsk(text) : undefined}
    >
      {text}
    </button>
  );
  return (
    <div className="followup-chips" role="group" aria-label={t('chat.followupsLabel')}>
      {qs.map((q, i) => chip(q, 'question', i === 0))}
      {as.map((a, i) => chip(a, 'action', !qs.length && i === 0))}
    </div>
  );
}
