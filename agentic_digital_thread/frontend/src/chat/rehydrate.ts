// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Session rehydration flow — extracted from
 * ChatSidebar so the wipe-guard semantics are unit-testable.
 *
 * CHAT-PERSISTENCE BUG (2026-07): the old flow adopted the session id and
 * then unconditionally applied whatever /api/memory/events returned — an
 * EMPTY result (AgentCore memory persistence lags the live turn; or every
 * event was tool noise) called setHistory([]) and WIPED the in-progress
 * conversation, leaving the investigation graph full but the chat panel
 * blank. Clicking the ACTIVE session row in the memory panel (it renders as
 * a button) or any recent-session row in the '+ New' menu could trigger it.
 *
 * Fixed semantics:
 * - The fetched history is built FIRST; nothing mutates until we decide.
 * - Empty history + non-empty live thread → NO-OP: keep the live state,
 *   console.warn, do NOT adopt the session id, do NOT reset the modal.
 * - Session adoption never silently discards an in-progress thread: with
 *   live content, adoptSessionId/resetModal run only when non-empty history
 *   actually replaces the thread. With NOTHING to lose, the session is
 *   adopted up front (legacy behavior — switching to a session whose events
 *   are unavailable still moves the conversation context).
 * - The store's setHistory carries the same empty-overwrite guard as
 *   defense in depth (thread-store.ts). */
import { requestJson } from '../api/client';
import type { MemoryEvent, MemoryEventsResponse, MemoryTurnBlock } from '../api/types/memory';
import type { ProvenanceMetadata } from './provenance';
import { createThreadStore, type HistoryMessage, type ThreadStore } from './thread-store';
import type { ChatTurn } from './turn-model';

const TOOL_NOISE_RE = /^(Tool call|Tool result)/;
/** One complete chart tag and nothing else (the tag shape the server's
 * chart pipeline and AnswerSegments both match). */
const ONE_CHART_TAG_RE = /^<chart-block\b[^>]*\/>$/;

/** A tool result's `charts` as the live stream injected them: complete chart
 * tags only, anything else dropped. */
function replayableCharts(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((tag): tag is string => typeof tag === 'string' && ONE_CHART_TAG_RE.test(tag))
    : [];
}

/** A question's kept `provenance` frame, or null when it has none (or the
 * value is not a frame with a records list, which the reducer would ignore). */
function keptFrame(value: unknown): ProvenanceMetadata | null {
  const frame = value as ProvenanceMetadata | null | undefined;
  return frame && typeof frame === 'object' && Array.isArray(frame.records) && frame.records.length
    ? frame
    : null;
}

export interface RehydratedThread {
  /** Assistant prose that precedes the first question (the read window cut
   * its turn off): plain messages, rendered before the turns. */
  history: HistoryMessage[];
  /** One finished turn per stored question. */
  turns: ChatTurn[];
}

/** Rebuild a reopened session the way it rendered live (Recent sessions
 * reload bug, seen on a Workshop Studio test event: answer above the
 * question, no Reasoning fold, no Sources, raw ---FOLLOWUPS--- and proposal
 * paragraphs). The events (oldest first, from GET /api/memory/events) are
 * REPLAYED through the live thread-store reducers: each question starts a
 * turn, its stored text / tool calls / tool results become token /
 * tool_start / tool_result events, and `done` runs the same answer selection
 * and parseAnswerBlocks a live answer gets. So the tool pills, the Reasoning
 * fold, the numbered Sources and the follow-up chips come from the same
 * code. A seeded question's `prelude` (its Knowledge Graph step, rebuilt by
 * the server from the stored seed) replays right after its turn starts, as
 * the live stream sent it before any stored call. An answered question's
 * `provenance` (the metadata frame its live stream sent, kept by the server)
 * replays right before the turn's `done`, as the live stream sent it, so the
 * Sources chips open the same evidence cards; a turn without one is marked
 * `evidenceNotKept`. A tool result's `charts` (the tags the live stream
 * injected after it that the server recorded it sending, rebuilt from the
 * stored result) replay right after it as the same token text, so
 * AnswerSegments places them above the answer as it did live; a question
 * marked `charts_not_shown` sets `chartsNotShown`. Events from an older server
 * (no `blocks`) replay their text, with the 'Tool call' / 'Tool result'
 * labels dropped as noise. */
export function buildRehydrateThread(items: MemoryEvent[]): RehydratedThread {
  const history: HistoryMessage[] = [];
  const scratch = createThreadStore();
  let open = false;
  let started = new Set<string>();
  // The open turn's kept provenance frame, and per finished turn whether it had one.
  let evidence: ProvenanceMetadata | null = null;
  const kept: boolean[] = [];
  // Whether the open turn's question was marked charts_not_shown, per finished turn.
  let chartsMarked = false;
  const chartsNotShown: boolean[] = [];

  const finish = () => {
    if (open) {
      // The live stream sends the frame just before `done`; so does the replay.
      if (evidence) scratch.applyEvent('metadata', { provenance: evidence });
      kept.push(evidence !== null);
      chartsNotShown.push(chartsMarked);
      scratch.applyEvent('done', {});
    }
    open = false;
    evidence = null;
    chartsMarked = false;
  };
  // Text runs of separate messages with no tool call between them would
  // otherwise run together; a paragraph break keeps them apart.
  const pushText = (text: string) => {
    if (!text) return;
    const turn = scratch.getState().turns.at(-1);
    const current = turn?.assistant.segments.streamingText ?? '';
    scratch.applyEvent('token', { text: current.trim() ? `\n\n${text}` : text });
  };
  const replay = (b: MemoryTurnBlock) => {
    if (b.type === 'text') {
      pushText(b.text);
    } else if (b.type === 'tool_use') {
      if (b.tool_id) started.add(b.tool_id);
      // A gated turn's calls carry no input, as their live tool_start did.
      scratch.applyEvent('tool_start', {
        tool: b.tool,
        tool_id: b.tool_id || undefined,
        input: 'input' in b ? b.input : null,
      });
    } else if (b.type === 'tool_result' && started.has(b.tool_id)) {
      // A result whose call is not in this turn (cut off) has nothing to settle.
      scratch.applyEvent('tool_result', {
        tool: b.tool,
        tool_id: b.tool_id,
        summary: b.summary || '',
        ...(typeof b.route_context === 'string' ? { route_context: b.route_context } : {}),
        ...(typeof b.sample_line === 'string' ? { sample_line: b.sample_line } : {}),
        ...(Array.isArray(b.telemetry_coverage) ? { telemetry_coverage: b.telemetry_coverage } : {}),
      });
      scratch.applyEvent('tool_end', { tool: b.tool, tool_id: b.tool_id });
      // The charts the live stream injected after this result, as the same
      // text (the runtime's chart wrapper), so AnswerSegments places them.
      const charts = replayableCharts(b.charts);
      if (charts.length) scratch.applyEvent('token', { text: `\n\n${charts.join('\n\n')}\n\n` });
    }
  };

  for (const ev of items) {
    const content = ev.content || '';
    const blocks = Array.isArray(ev.blocks) ? ev.blocks : null;
    const noise = TOOL_NOISE_RE.test(content.trim());
    const toolResults = !!blocks?.some((b) => b.type === 'tool_result');
    const question = ev.role !== 'assistant' && !toolResults && !(blocks === null && noise);
    if (question) {
      if (!content.trim()) continue;
      finish();
      scratch.startTurn(content);
      started = new Set();
      open = true;
      // The Knowledge Graph step the live stream showed before the turn's
      // first stored call (absent on older servers and unseeded turns).
      if (Array.isArray(ev.prelude)) ev.prelude.forEach(replay);
      evidence = keptFrame(ev.provenance);
      chartsMarked = ev.charts_not_shown === true;
    } else if (!open) {
      if (ev.role !== 'assistant') continue;
      const text = blocks
        ? blocks.flatMap((b) => (b.type === 'text' ? [b.text] : [])).join('\n\n')
        : noise
          ? ''
          : content;
      if (text.trim()) history.push({ role: 'assistant', content: text });
    } else if (blocks) {
      blocks.forEach(replay);
    } else if (!noise) {
      pushText(content);
    }
  }
  finish();

  const turns = scratch.getState().turns;
  turns.forEach((turn, i) => {
    // Without its kept frame the answer's Sources open no evidence card;
    // the footer says so (AnswerFooter) instead of leaving a dead chip.
    turn.assistant.evidenceNotKept = !kept[i];
    // Its stored results held charts the reload does not all replay; the
    // footer says so (AnswerFooter).
    turn.assistant.chartsNotShown = chartsNotShown[i] === true;
  });
  for (const turn of turns) {
    // Feedback rates the answer it was shown for; a reload has no id for the
    // original answer, so it offers no thumbs (as the plain reload did).
    turn.assistant.messageId = null;
    // The replay happened in one tick: its clock readings would show every
    // call as 0.0s. The stored turn has no call timing, so show none.
    for (const trace of turn.assistant.traces.values()) {
      trace.startedAt = undefined;
      trace.elapsedMs = undefined;
      trace.serverStartTs = undefined;
      trace.serverEndTs = undefined;
    }
  }
  return { history, turns };
}

export interface RehydrateDeps {
  store: Pick<ThreadStore, 'getState' | 'setHistory'>;
  /** lib/session adoptSessionId — switch window.currentSessionId. */
  adoptSession: (sid: string) => void;
  /** data-modal resetModalConversation — clears the modal follow-up thread. */
  resetModal: () => void;
  /** ChatSidebar setThreadVisible(true). */
  showThread: () => void;
  /** MemoryPanel refresh — runs on every outcome (legacy finally). */
  refreshMemory: () => void;
  /** Optional visible-error hook owned by the caller. */
  onError?: (error: unknown) => void;
}

/** Open a prior session. Resolves true when the thread was replaced, false
 * when the rehydrate was a guarded no-op (or the events fetch failed with a
 * live thread present). */
export async function rehydrateSession(sid: string, deps: RehydrateDeps): Promise<boolean> {
  if (!sid) return false;
  const live = deps.store.getState();
  const hasLive = live.turns.length > 0 || live.history.length > 0;
  if (!hasLive) {
    // Nothing to lose — adopt up front (legacy semantics: the session is
    // adopted even if the events fetch fails below).
    deps.adoptSession(sid);
    deps.resetModal();
  }
  try {
    const data = await requestJson<MemoryEventsResponse>(
      `/api/memory/events?session_id=${encodeURIComponent(sid)}`,
    );
    const thread = buildRehydrateThread(data.messages || data.events || []);
    if (thread.history.length === 0 && thread.turns.length === 0 && hasLive) {
      console.warn(
        `[chat] rehydrate(${sid}) returned no history — keeping the live thread (session not adopted)`,
      );
      return false;
    }
    if (hasLive) {
      deps.adoptSession(sid);
      deps.resetModal();
    }
    deps.store.setHistory(thread.history, thread.turns);
    deps.showThread();
    return true;
  } catch (error) {
    deps.onError?.(error);
    return false;
  } finally {
    deps.refreshMemory();
  }
}
