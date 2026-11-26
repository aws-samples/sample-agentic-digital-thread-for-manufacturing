// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Data detail modal (openDataModal / closeDataModal / sendModalFollowup /
 * saveModalAsWord / showToast) plus the #data-modal-* markup.
 *
 * jsx-migration W2.4: the modal is STORE-DRIVEN. <DataModal/> (mounted once
 * in the app shell) renders from `dataModalStore`
 * ({open, title, contentMd, conversation, followupTurns});
 * the exported imperative API (openDataModal(title, detail) — a window
 * contract registered by AppShell) dispatches store actions instead of
 * poking the DOM by id. Markdown renders through <Md/> (react-markdown —
 * zero-innerHTML: the legacy `raw` pre-sanitized-HTML path had no remaining
 * callers and was removed), and the Word export builds its file payload from
 * the store content via the mdToSafeHtml string pipeline (a DOWNLOAD payload,
 * not a DOM sink; interpolated titles are escHtml-escaped).
 *
 * Follow-up questions stream over the shared postSse transport and REUSE the
 * shared Conversation Session id (ADR-0005). Chart blocks render in sandboxed
 * srcdoc iframes that load Chart.js by absolute URL
 * (`${origin}/js/vendor/chart.umd.min.js`, which the build copies from the pinned
 * npm Chart.js into dist/js/vendor/). */

import { useEffect, useMemo, useRef } from 'react';
import { createPortal } from 'react-dom';

import { postSse } from '../../api/sse';
import { AnswerSegments } from '../../chat/answer-segments';
import { splitChartBlocks, stripChartBlocks, stripChartBlocksForStream } from '../../chat/charts';
import { ChartIframe } from '../../chat/ChartIframe';
import { escHtml, mdToSafeHtml } from '../../chat/md';
import { displayZoneName } from '../../lib/answer-times';
import { t as translate, useI18n } from '../../lib/i18n';
import { wrapResponseLang } from '../../lib/i18n-lang';
import { getSessionId } from '../../lib/session';
import { rafPair } from '../../lib/raf';
import { createStore, useStore } from '../../lib/store';
import { Md } from '../ui/Md';
import { ModalSurface } from '../ui/ModalSurface';
import { push } from '../ui/toast-store';
import { FollowupChips, parseAnswerBlocks } from './answer-blocks';

// Window augmentation for _lastDisplayText lives in the single shared
// declaration (src/chat/globals.d.ts) — no duplicate here.

/** A single turn in the modal follow-up conversation context. */
interface ModalTurn {
  role: 'user' | 'assistant';
  text: string;
}

/** One rendered follow-up exchange inside the modal body. */
interface FollowupTurn {
  id: number;
  question: string;
  /** Clean (control-blocks-stripped) markdown streamed so far. */
  answerMd: string;
  status: 'loading' | 'streaming' | 'done' | 'error';
  error?: string;
  followups: string[];
  actions: string[];
}

export interface DataModalState {
  open: boolean;
  title: string;
  /** Markdown content (the openDataModal path). */
  contentMd: string | null;
  /** Follow-up context turns (window._modalConversation). */
  conversation: ModalTurn[];
  followupTurns: FollowupTurn[];
  /** Plain-text sections shown below the markdown (``telemetrySections``):
   * rendered as text nodes, never parsed as markdown or for chart tags. */
  textSections?: ModalTextSection[];
}

/** One plain-text section of the modal: an optional label and its lines. */
export interface ModalTextSection {
  label: string;
  lines: string[];
}

/** The optional third argument of ``openDataModal``: the Architecture
 * drill-down passes its call details (arch-store ``InvModalExtras``); only
 * the typed telemetry lines are read here. */
export interface DataModalExtras {
  kind?: string;
  calls?: ReadonlyArray<{ telemetryCoverage?: readonly string[]; sampleLine?: string }>;
}

/** The drill-down calls' stored telemetry lines as text sections: for each
 * call with any, its coverage lines then its sample line, labelled
 * ``Request N`` (the markdown's own heading) when the modal lists a system's
 * calls. The lines come as typed fields, so they are shown exactly as read:
 * a read identifier holding markdown or a chart-block tag stays text. */
export function telemetrySections(extras?: DataModalExtras): ModalTextSection[] {
  const calls = Array.isArray(extras?.calls) ? extras.calls : [];
  const sections: ModalTextSection[] = [];
  calls.forEach((call, i) => {
    const coverage = Array.isArray(call?.telemetryCoverage) ? call.telemetryCoverage : [];
    const lines = [...coverage, ...(call?.sampleLine ? [call.sampleLine] : [])].filter(
      (l): l is string => typeof l === 'string' && !!l.trim(),
    );
    if (lines.length) {
      sections.push({ label: extras?.kind === 'inv-node' ? `Request ${i + 1}` : '', lines });
    }
  });
  return sections;
}

const INITIAL_STATE: DataModalState = {
  open: false,
  title: '',
  contentMd: null,
  conversation: [],
  followupTurns: [],
};

/** Exported for tests; components subscribe via useStore. */
export const dataModalStore = createStore<DataModalState>(INITIAL_STATE);

/** '+ New' / session-rehydrate reset — clears the follow-up conversation
 * context (jsx-migration W3.1: replaces the legacy
 * `window._modalConversation = []` assignment; the defineProperty bridge is
 * gone — the store is the single source of truth). */
export function resetModalConversation(): void {
  dataModalStore.update((s) => ({ ...s, conversation: [] }));
}

// ── Toast compatibility dispatcher (showToast) ───────────────────────────────
/** Preserve the window/module contract while routing notices through the
 * app-level JSX ToastRegion. This function must never create DOM nodes. */
export function showToast(msg: string): void {
  push('info', msg);
}

// ── Chart-block → sandboxed iframe ───────────────────────────────────────────
// The extraction + srcdoc-iframe pipeline is the shared one in chat/charts.ts
// (byte-equivalent srcdoc template). The modal passes preserveExisting:false to
// keep the modal's overwrite-defaults delta.

// ── openDataModal / closeDataModal ────────────────────────────────────────────
// The legacy `raw` (pre-sanitized HTML) variant had no remaining callers and
// was removed in the zero-innerHTML migration — content is markdown only.
export function openDataModal(title: string, content: string, extras?: DataModalExtras): void {
  dataModalStore.update((s) => ({
    ...s,
    open: true,
    title,
    contentMd: content,
    textSections: telemetrySections(extras),
    // The old innerHTML overwrite discarded any appended follow-up DOM on
    // every open — reset the rendered follow-ups to match.
    followupTurns: [],
    // A new query result resets the follow-up conversation context. Follow-ups
    // REUSE the shared Conversation Session id so the modal thread continues
    // the same session as the main chat, instead of minting a fresh one per
    // result.
    conversation: [
      { role: 'user', text: title },
      { role: 'assistant', text: content.slice(0, 3000) }, // Cap context to avoid token bloat
    ],
  }));
}

export function closeDataModal(): void {
  dataModalStore.update((s) => (s.open ? { ...s, open: false } : s));
  // Collapse all persona details back to just headers
  document.querySelectorAll('.persona-details[open]').forEach((d) => d.removeAttribute('open'));
  // Closing the reading modal returns the user to the INTACT chat thread — it must
  // NOT wipe #messages or destroy the session. The trace window stays visible so
  // the conversation (and any follow-ups) remain available for continued turns.
}

// ── Word export (saveModalAsWord / saveChatModalAsWord) ──────────────────────
/** Assemble the modal's export HTML from the STORE (not the live DOM):
 * the markdown rendered through the shared sanitizer plus each follow-up
 * exchange. This is a DOWNLOAD-file payload (never inserted into the page);
 * chart-block tags are stripped (the export stylesheet hid iframes anyway). */
/** The modal's plain-text sections (``textSections``) for the Word export,
 * as on screen: a heading, then each section's label and lines in a
 * ``<pre>``. Every piece is ``escHtml()``-escaped text, never markdown or
 * chart parsing, so a read identifier holding either stays literal. */
function textSectionsHtml(sections: ModalTextSection[] | undefined): string {
  if (!sections?.length) return '';
  const body = sections
    .map(
      (section) =>
        (section.label ? `<h4>${escHtml(section.label)}</h4>` : '') +
        `<pre style="white-space:pre-wrap;">${escHtml(section.lines.join('\n'))}</pre>`,
    )
    .join('');
  // Security-reviewed (html-in-template-string): DOWNLOAD-file payload only
  // (never inserted into the page); every interpolation is escHtml()-escaped.
  // nosemgrep
  return `<section><h3>${escHtml(translate('chat.telemetryRead'))}</h3>${body}</section>`;
}

function modalContentHtml(s: DataModalState): string {
  // localTimes: UTC answer times show in the viewer's zone, as on screen.
  const md = (text: string) => mdToSafeHtml(text, { localTimes: true });
  let html = s.contentMd != null ? md(stripChartBlocks(s.contentMd)) : '';
  html += textSectionsHtml(s.textSections);
  for (const t of s.followupTurns) {
    html +=
      '<div style="margin-top:1rem;border-top:1px solid var(--border);padding-top:0.75rem;">' +
      // Security-reviewed (html-in-template-string): DOWNLOAD-file payload only
      // (never inserted into the page); question is escHtml()-escaped and the
      // answer runs through the shared mdToSafeHtml sanitizer (ADR-sanctioned
      // Word-export path — the project's sink policy).
      // nosemgrep
      `<div style="font-size:0.75rem;color:var(--accent);margin-bottom:0.5rem;font-weight:600;">Follow-up: ${escHtml(t.question)}</div>` +
      // nosemgrep
      `<div class="followup-answer">${md(t.answerMd)}</div>` +
      '</div>';
  }
  return html;
}

export function saveModalAsWord(opts?: { title?: string; content?: string }): void {
  opts = opts || {};
  // Content source is configurable so ONE exporter serves both the (legacy) data
  // modal and the pop-out chat modal. Defaults read the data-modal STORE;
  // callers may pass an explicit title/content instead.
  const state = dataModalStore.get();
  const rawTitle = opts.title != null ? opts.title : state.title || 'Report';
  // Use a clean short title — truncate at 60 chars and strip SQL/markdown noise
  const title = rawTitle.length > 60 ? rawTitle.slice(0, 60).replace(/\*\*/g, '').trim() + '…' : rawTitle;
  const content = opts.content != null ? opts.content : modalContentHtml(state);
  const date = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  // Both export sources show answer times in the viewer's zone (the store
  // path through mdToSafeHtml localTimes, the chat path from the rendered
  // page), so the file says which zone; each time's title keeps its UTC text.
  const zone = displayZoneName();
  // escHtml on the title interpolations: the download-file payload
  // previously interpolated the title unescaped.
  const html = `
    <html xmlns:o="urn:schemas-microsoft-com:office:office"
          xmlns:w="urn:schemas-microsoft-com:office:word"
          xmlns="http://www.w3.org/TR/REC-html40">
    <head><meta charset="utf-8">
    <title>${escHtml(title)}</title>
    <!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View></w:WordDocument></xml><![endif]-->
    <style>
      body { font-family: Calibri, Arial, sans-serif; font-size: 11pt; color: #000; margin: 2cm; }
      h1 { font-size: 16pt; color: #1a1a2e; border-bottom: 2px solid #1a1a2e; padding-bottom: 4pt; }
      h2 { font-size: 13pt; color: #1a1a2e; margin-top: 14pt; }
      h3 { font-size: 11pt; color: #333; }
      table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 10pt; }
      th { background: #1a1a2e; color: #fff; padding: 5pt 8pt; text-align: left; border: 1px solid #ccc; }
      td { padding: 4pt 8pt; border: 1px solid #ccc; }
      tr:nth-child(even) td { background: #f5f5f5; }
      .date { color: #666; font-size: 10pt; margin-bottom: 12pt; }
      iframe { display: none; }
    </style>
    </head><body>
    <h1>${escHtml(title)}</h1>
    <div class="date">Generated: ${date} · Times shown in ${escHtml(zone)}</div>
    ${content}
    </body></html>`;
  const blob = new Blob(['\ufeff', html], { type: 'application/msword' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = title.replace(/[^a-z0-9]/gi, '_').toLowerCase() + '_' + new Date().toISOString().slice(0, 10) + '.doc';
  a.click();
  URL.revokeObjectURL(url);
}

// Export the pop-out chat modal's conversation as Word — reuses the single
// saveModalAsWord exporter, serializing the rendered #modal-messages thread
// with XMLSerializer (a read-only serialization of <ChatThread/> output for
// the document download — no innerHTML/outerHTML access).
export function saveChatModalAsWord(): void {
  const el = document.getElementById('modal-messages');
  let content = '';
  if (el) {
    const ser = new XMLSerializer();
    content = Array.from(el.childNodes)
      .map((n) => ser.serializeToString(n))
      .join('');
  }
  const title = (typeof window !== 'undefined' && window._lastDisplayText) || 'Conversation';
  saveModalAsWord({ title, content });
}

// ── Follow-up chat in data modal ──────────────────────────────────────────────
// Clicking a follow-up chip inside the reading modal asks it as the next
// follow-up (stays in the modal thread) instead of resetting the main chat.
export function askModalFollowup(question: string): void {
  const input = document.getElementById('data-modal-chat-input') as HTMLInputElement | null;
  if (input) input.value = question;
  void sendModalFollowup();
}

let followupSeq = 0;

/** Patch one follow-up turn by id (replaces the turns array so subscribers
 * re-render). Token-cadence callers coalesce through the rAF scheduler in
 * sendModalFollowup — react-markdown re-parses are too heavy per token. */
function patchFollowup(id: number, patch: Partial<FollowupTurn>): void {
  dataModalStore.update((s) => ({
    ...s,
    followupTurns: s.followupTurns.map((t) => (t.id === id ? { ...t, ...patch } : t)),
  }));
}

export async function sendModalFollowup(): Promise<void> {
  const input = document.getElementById('data-modal-chat-input') as HTMLInputElement | null;
  if (!input) return;
  const question = input.value.trim();
  if (!question) return;
  input.value = '';

  // Build context from previous conversation
  const conversation = dataModalStore.get().conversation;
  let contextPrompt = question;
  if (conversation.length > 0) {
    const history = conversation.map((m) => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.text}`).join('\n\n');
    contextPrompt = `CONVERSATION CONTEXT (same session — user is asking a follow-up):\n${history}\n\nFOLLOW-UP QUESTION: ${question}`;
  }

  // Append the loading turn ("Thinking" dots until the first token arrives).
  const id = ++followupSeq;
  dataModalStore.update((s) => ({
    ...s,
    followupTurns: [
      ...s.followupTurns,
      { id, question, answerMd: '', status: 'loading', followups: [], actions: [] },
    ],
  }));

  // Reuse the main conversation session so follow-ups continue the same
  // Conversation Session (shared memory/thread) rather than minting a new one.
  const sessionId = getSessionId();

  // The answer is rebuilt from the text runs the way the main chat does it
  // (AnswerSegments): a tool call ends a run, the answer is chosen from the
  // runs after the last call that reads data, and a write-back call
  // (save_correlation and friends) does not end the answer. The live
  // preview shows the run in progress.
  const segments = new AnswerSegments();

  // rAF-coalesce the per-token store patch (a react-markdown
  // re-parse per token is ~15x the old marked cost and O(n²) over the
  // answer; the chat thread store already coalesces this way). One patch
  // per animation frame; the final done/error patch supersedes any pending.
  const raf = rafPair();
  let pendingFrame: number | null = null;
  let pendingLiveMd: string | null = null;
  const flushLivePatch = () => {
    pendingFrame = null;
    if (pendingLiveMd != null) {
      patchFollowup(id, { status: 'streaming', answerMd: pendingLiveMd });
      pendingLiveMd = null;
    }
  };
  const cancelLivePatch = () => {
    raf.cancel(pendingFrame);
    pendingFrame = null;
    pendingLiveMd = null;
  };

  try {
    await postSse(
      '/api/ask',
      {
        // Follow-ups honor the selected AI-response language — the
        // directive is appended to the outgoing prompt only
        // (sendModalFollowup → wrapResponseLang); UI copies stay unwrapped.
        question: wrapResponseLang(contextPrompt),
        session_id: sessionId,
        plant: (document.getElementById('plant-select') as HTMLSelectElement | null)?.value || 'all',
      },
      (eventType, data) => {
        if (eventType === 'tool_start') {
          // A tool call ends the current run; its name decides whether the
          // runs before it can still be the answer.
          const tool = (data as { tool?: unknown }).tool;
          segments.onToolBoundary(typeof tool === 'string' ? tool : undefined);
        } else if (eventType === 'token') {
          segments.pushToken((data as { text?: string }).text ?? '');
          // Live preview of the run in progress (control blocks stripped),
          // coalesced to one store patch per animation frame.
          pendingLiveMd = parseAnswerBlocks(segments.streamingText).answer;
          if (pendingFrame === null) pendingFrame = raf.schedule(flushLivePatch);
        }
      },
    );

    cancelLivePatch();

    // Final render — strip control blocks, then render the clean answer + chips.
    const parsed = parseAnswerBlocks(segments.finalize());
    patchFollowup(id, {
      status: 'done',
      answerMd: parsed.answer,
      followups: parsed.followups,
      actions: parsed.actions,
    });

    // Store the CLEAN answer (no control blocks) in conversation history so the
    // next follow-up's context stays compact and free of leaked markers.
    dataModalStore.update((s) => ({
      ...s,
      conversation: [
        ...s.conversation,
        { role: 'user', text: question },
        { role: 'assistant', text: parsed.answer.slice(0, 3000) },
      ],
    }));
  } catch (error) {
    cancelLivePatch();
    patchFollowup(id, {
      status: 'error',
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

// ── Body content components ───────────────────────────────────────────────────

/** Markdown content: chart-block tags are split out of the markdown at the
 * STRING level (charts.ts splitChartBlocks) and rendered as sandboxed
 * <ChartIframe/>s interleaved with <Md/> chunks (preserveExisting=false —
 * the modal's overwrite-defaults variant). */
function ModalMdContent({ md, className }: { md: string; className?: string }) {
  const parts = useMemo(() => splitChartBlocks(md), [md]);
  return (
    <div className={className}>
      {parts.map((p, i) =>
        p.kind === 'md' ? (
          <Md key={`md-${i}`} bare text={p.text} />
        ) : (
          <ChartIframe key={`chart-${i}`} attrs={p.attrs} preserveExisting={false} />
        ),
      )}
    </div>
  );
}

function FollowupTurnView({ turn }: { turn: FollowupTurn }) {
  if (turn.status === 'loading' || turn.status === 'error') {
    // The pre-stream indicator box (#modal-followup-loading); the
    // error state renders in the same box instead of the answer container.
    // The asked question is echoed above the indicator — the composer was
    // cleared on send, so without it the user has no trace of what is
    // in flight.
    return (
      <div
        style={{
          marginTop: '1rem',
          padding: '0.75rem',
          background: 'var(--surface2)',
          borderRadius: 8,
          color: 'var(--muted)',
          fontSize: '0.85rem',
        }}
      >
        <div style={{ fontSize: '0.75rem', color: 'var(--accent)', marginBottom: '0.5rem', fontWeight: 600 }}>
          Follow-up: {turn.question}
        </div>
        {turn.status === 'error' ? (
          <span style={{ color: 'var(--red)' }}>Error: {turn.error}</span>
        ) : (
          // Animated three-dot indicator (parity with the thread's
          // .thinking-inline): the dot-bounce keyframes target `.loading-dots
          // span` (dashboard.css:537) — a childless .loading-dots rendered as
          // static text, indistinguishable from a stall.
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '0.4rem' }}>
            <span className="loading-dots" style={{ display: 'inline-flex', gap: 3 }}>
              <span />
              <span />
              <span />
            </span>
            Thinking
          </span>
        )}
      </div>
    );
  }
  const hasChips = turn.status === 'done' && (turn.followups.length > 0 || turn.actions.length > 0);
  return (
    <div style={{ marginTop: '1rem', borderTop: '1px solid var(--border)', paddingTop: '0.75rem' }}>
      <div style={{ fontSize: '0.75rem', color: 'var(--accent)', marginBottom: '0.5rem', fontWeight: 600 }}>
        Follow-up: {turn.question}
      </div>
      {turn.status === 'done' ? (
        // Done answers route through the SAME chart pipeline as the modal
        // body (react-markdown would otherwise render a
        // `<chart-block .../>` tag as permanently visible escaped text; the
        // old DOMPurify path stripped it invisibly, this now RENDERS the
        // chart, preserveExisting=false like the rest of the modal).
        <ModalMdContent md={turn.answerMd} className="followup-answer" />
      ) : (
        // Streaming preview: hide complete chart tags AND the partial tail
        // of one still being streamed (parity with the old sanitized path).
        <Md className="followup-answer" text={stripChartBlocksForStream(turn.answerMd)} />
      )}
      {hasChips ? (
        // The ONE chip row (answer-blocks.tsx <FollowupChips/>: real
        // buttons, same classes + data-question contract) with a direct
        // per-chip handler.
        <FollowupChips
          followups={turn.followups}
          actions={turn.actions}
          onAsk={(q) => askModalFollowup(q)}
        />
      ) : null}
    </div>
  );
}

/** The modal's plain-text sections (``telemetrySections``), as text nodes. */
function ModalTextSections({ sections }: { sections: ModalTextSection[] }) {
  const { t } = useI18n();
  return (
    <section className="modal-telemetry" aria-label={t('chat.telemetryRead')}>
      <h3>{t('chat.telemetryRead')}</h3>
      {sections.map((section, i) => (
        <div key={i} className="modal-telemetry-section">
          {section.label ? <h4>{section.label}</h4> : null}
          <pre className="modal-telemetry-lines">{section.lines.join('\n')}</pre>
        </div>
      ))}
    </section>
  );
}

function DataModalBody() {
  const contentMd = useStore(dataModalStore, (s) => s.contentMd);
  const textSections = useStore(dataModalStore, (s) => s.textSections);
  const followupTurns = useStore(dataModalStore, (s) => s.followupTurns);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Follow streamed follow-ups inside the bounded content region. The modal
  // shell keeps its header and composer visible while this body scrolls.
  useEffect(() => {
    if (followupTurns.length === 0) return;
    const scroller = bodyRef.current;
    if (!scroller) return;
    scroller.scrollTop = scroller.scrollHeight;
  }, [followupTurns]);

  return (
    <div id="data-modal-content" className="data-modal-body" ref={bodyRef}>
      {contentMd != null ? <ModalMdContent md={contentMd} /> : null}
      {textSections?.length ? <ModalTextSections sections={textSections} /> : null}
      {followupTurns.map((t) => (
        <FollowupTurnView key={t.id} turn={t} />
      ))}
    </div>
  );
}

// ── Modal markup (static/index.html #data-modal-overlay) ─────────────────────
/** Mount ONCE in the app shell. All ids/classes are contractual. */
export function DataModal() {
  const open = useStore(dataModalStore, (s) => s.open);
  const title = useStore(dataModalStore, (s) => s.title);
  // Chrome i18n: the follow-up placeholder + Send carry legacy
  // data-i18n hooks (chat.followupPlaceholder / chat.send in index.html:517,520).
  const { t } = useI18n();
  const content = (
      <div className="data-modal">
        <div className="data-modal-header">
          <h3 id="data-modal-title">{title}</h3>
          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
            <button
              type="button"
              onClick={() => saveModalAsWord()}
              title="Save as Word document"
              style={{
                background: 'var(--surface)',
                border: '1px solid var(--border)',
                color: 'var(--text)',
                padding: '0.3rem 0.7rem',
                borderRadius: 4,
                fontSize: '0.75rem',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '0.3rem',
              }}
            >
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <line x1="16" y1="13" x2="8" y2="13" />
                <line x1="16" y1="17" x2="8" y2="17" />
                <polyline points="10 9 9 9 8 9" />
              </svg>
              Word
            </button>
            <button type="button" onClick={closeDataModal}>
              ✕
            </button>
          </div>
        </div>
        <DataModalBody />
        {/* Follow-up chat input stays outside the independently scrolling
            .data-modal-body content region. */}
        <div id="data-modal-followup" className="data-modal-followup">
          <input
            id="data-modal-chat-input"
            type="text"
            placeholder={t('chat.followupPlaceholder')}
            style={{
              flex: 1,
              background: 'var(--surface2)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              padding: '0.5rem 0.75rem',
              color: 'var(--text)',
              fontSize: '0.85rem',
              fontFamily: 'inherit',
            }}
            onKeyPress={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void sendModalFollowup();
              }
            }}
          />
          {/* Visual treatment comes from the SHARED .btn-send rule
              (dashboard.css — same source as the chat panel's Send). */}
          <button
            type="button"
            className="btn-send"
            onClick={() => void sendModalFollowup()}
            style={{
              padding: '0.5rem 1rem',
              fontSize: '0.8rem',
              whiteSpace: 'nowrap',
            }}
          >
            {t('chat.send')}
          </button>
        </div>
      </div>
  );
  if (typeof document === 'undefined') return null;
  return createPortal(
    <ModalSurface
      id="data-modal-overlay"
      className="data-modal-overlay"
      style={{ display: open ? 'flex' : 'none' }}
      open={open}
      keepMounted
      onClose={closeDataModal}
      closeOnOverlayClick
      labelledBy="data-modal-title"
      initialFocus={() => document.getElementById('data-modal-chat-input')}
    >
      {content}
    </ModalSurface>,
    document.body,
  );
}
