// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Sanitized markdown + HTML escaping — mdToSafeHtml / escHtml
 * (marked + DOMPurify, pinned to the exact vendored
 * versions).
 *
 * NOTE (zero-innerHTML migration): this module is NO LONGER a DOM render
 * path. In-page markdown renders as React elements via the shared <Md/>
 * component (react-markdown). mdToSafeHtml is kept for:
 *  - the Word-export DOWNLOAD-file payload (shared/thread/data-modal.tsx) —
 *    a file blob, not a DOM sink;
 *  - the window.mdToSafeHtml contract (registered by ChatSidebar; e2e probes);
 *  - the python textual contract (tests/unit/test_agent_analysis_panel.py
 *    concatenates this file into its chat-surface fixture).
 * Do NOT feed its output into innerHTML/dangerouslySetInnerHTML — no such
 * sink exists in src/ anymore. */
import DOMPurify from 'dompurify';
import { Marked, marked, type Token, type Tokens } from 'marked';

import { escapeHtml as escHtml } from '../lib/esc';
import { utcStampsToTimeHtml } from '../lib/answer-times';

/** Escape untrusted text (LLM-authored fields, system labels) before it is
 * interpolated into an HTML STRING PAYLOAD (Word export). Re-exported legacy
 * name for the shared helper in src/lib/esc.ts (byte-identical output). */
export { escHtml };

type NoTimes = { adtNoTimes?: boolean };

// marked's own text escape (no re-encoding of an existing entity), so the
// export's text is escaped exactly as the shared renderer escapes it.
const MARKED_TEXT_ESCAPE_RE = /[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)/g;
const MARKED_TEXT_ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

function escapeText(text: string): string {
  return text.replace(MARKED_TEXT_ESCAPE_RE, (c) => MARKED_TEXT_ESCAPES[c]);
}

/** Mark every token inside a link or image so its text keeps the UTC time as
 * written (the label is the author's, as on screen). */
function markNoTimes(token: Token): void {
  (token as NoTimes).adtNoTimes = true;
  for (const child of ('tokens' in token && token.tokens) || []) markNoTimes(child);
}

/** A marked instance for the Word export: text tokens outside code and link
 * text show UTC answer times in the viewer's zone as `<time datetime title>`,
 * the same values the page shows (lib/answer-times.ts). Separate from the
 * shared `marked` so every other caller renders exactly as before. */
const exportMarked = new Marked({
  walkTokens(token) {
    if (token.type === 'link' || token.type === 'image') markNoTimes(token);
  },
  renderer: {
    text(token: Tokens.Text | Tokens.Escape) {
      if ('tokens' in token && token.tokens) return this.parser.parseInline(token.tokens);
      if ('escaped' in token && token.escaped) return token.text;
      const html = escapeText(token.text);
      return (token as NoTimes).adtNoTimes ? html : utcStampsToTimeHtml(html);
    },
  },
});

/** Render text as sanitized markdown; fall back to escaped plain text if the
 * markdown parser throws (the missing-vendor degradation path). With
 * `localTimes` (the Word export) UTC answer times become local `<time>`
 * elements, as on screen. */
export function mdToSafeHtml(text: unknown, opts?: { localTimes?: boolean }): string {
  const s = text == null ? '' : String(text);
  try {
    const html = (opts?.localTimes ? exportMarked : marked).parse(s) as string;
    return DOMPurify.sanitize(html);
  } catch {
    return DOMPurify.sanitize(escHtml(s));
  }
}

// sanitize() and mdParse() were removed post-migration (review cleanup):
// zero consumers remained once every in-page render path moved to <Md/>.
