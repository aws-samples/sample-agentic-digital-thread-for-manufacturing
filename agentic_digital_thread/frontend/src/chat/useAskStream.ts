// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* /api/ask streaming turn — the TRANSPORT half of the chat send flow,
 * on top of the Wave-1 postSse transport (src/api/sse.ts).
 *
 * jsx-migration W3.1: this module no longer builds thread DOM. Each SSE event
 * dispatches into the chat thread store (chat/thread-store.ts — reducers
 * mirroring the old per-event DOM mutations 1:1), and <ChatThread/> renders
 * the store into BOTH the sidebar (#messages) and the pop-out modal
 * (#modal-messages). What stays here, by contract:
 * - One AbortController per send; Stop / '+ New'-cancel aborts the fetch or
 *   the stream read, surfacing as an AbortError which is stored as a quiet
 *   `.response-canceled` marker (no red banner).
 * - handleArchEvent call ORDER per SSE event (investigation-graph fidelity):
 *   user_msg → [reasoning →] tool_start → tool_result → tool_end →
 *   metadata/intelligence_summary → done({}) … done(data). The reasoning
 *   run flush reads scratch.reasoningBetweenTools BEFORE the tool_start
 *   reducer resets it; the tool_result citation link passes
 *   window._lastFindingId through AFTER the architecture handler minted it.
 * - The error sites (SSE `event: error`, the non-abort catch, and a clean
 *   end of the body with no terminal frame) store PLAIN message text on the
 *   turn; <AssistantTurn/> renders the single red error paragraph as JSX
 *   (python textual contract — tests/unit/test_agent_analysis_panel.py
 *   counts the red error-paragraph site across the chat surface). Once the
 *   controller is aborted (Stop), error frames are ignored.
 * - Our own failures are not a dropped connection: a frame or handler that
 *   fails is logged ("[ask] handler failed <type>", "[ask] frame skipped")
 *   and the stream keeps reading (postSse frameErrorTag), and every
 *   investigation-graph call goes through archEvent, so the graph view can
 *   never stop the chat turn from settling. The connection-dropped line is
 *   left for a transport failure ("[ask] stream failed:") and a body with no
 *   terminal frame ("[ask] stream ended without a done or error frame"). A
 *   done/error frame counts as settled only after its store reducer has run;
 *   a reducer that throws ("[ask] terminal handler failed") forces a notified
 *   error on the turn, even if it had already ended streaming.
 * - Stream perf (plan §6): token dispatches are rAF-coalesced by the store —
 *   one subscriber notify per animation frame, not per token — and
 *   <ChatThread/> memoizes per turn so only the streaming leaf re-renders. */
import { useCallback, useRef, useState } from 'react';

import { postSse } from '../api/sse';
import type {
  AskRequest,
  DoneEventData,
  ErrorEventData,
  MetadataEventData,
  ToolResultEventData,
} from '../api/types/chat';
import { clientTimeContext } from '../lib/answer-times';
import { getSessionId } from '../lib/session';
import { t } from '../lib/i18n';
import { wrapResponseLang } from '../lib/i18n-lang';
import { handleArchEvent } from './arch-bridge';
import { isSubstantiveAnswer } from './auto-open';
import { noteSessionSystem } from './session-systems';
import { createThreadStore } from './thread-store';

/** THE chat thread store — one Conversation Session thread, rendered by
 * <ChatThread/> in the sidebar and (when open) the pop-out modal. */
export const chatThreadStore = createThreadStore();

/** Per-send options. `retry` is set ONLY by "Ask again" on the chat's last
 * turn (chat/thread-clicks.ts): the server then leaves the attempt the user
 * never saw out of the history this turn reads. */
export interface AskOptions {
  retry?: boolean;
}

/** The /api/ask request body. An ordinary send carries no `retry` key at all. */
export function askRequestBody(question: string, plant: string, retry?: boolean): AskRequest {
  return {
    question: wrapResponseLang(question),
    session_id: getSessionId(),
    plant,
    // Temporal context for the agent (server-validated; invalid values
    // degrade to UTC-only). Lets relative phrases ("today", "last 24h",
    // "this month") resolve against the user's clock.
    ...clientTimeContext(),
    ...(retry === true ? { retry: true } : {}),
  };
}

/** handleArchEvent for one chat event: the investigation graph is a side
 * view, so a failure there is logged with the event type and never stops the
 * chat turn from updating or settling. */
function archEvent(type: string, data: unknown): void {
  try {
    handleArchEvent(type, data);
  } catch (err) {
    console.error('[ask] handler failed', type, err);
  }
}

/** Settle a turn still marked streaming after the body ended (postSse
 * resolved). Each case has its own console tag so a captured console tells
 * them apart: a Stop that raced the final read is canceled; a body with no
 * done/error frame is a dropped connection; a terminal frame whose reducer
 * completed but left the turn open (should not happen) is our own bug. */
function settleOpenTurn(turnId: string, controller: AbortController, settled: boolean): void {
  const active = chatThreadStore.getState().turns.at(-1);
  if (!active || active.id !== turnId || !active.assistant.streaming) return;
  archEvent('done', {});
  if (controller.signal.aborted) {
    chatThreadStore.abortTurn();
    return;
  }
  console.error(
    settled
      ? '[ask] turn did not settle after its terminal frame'
      : '[ask] stream ended without a done or error frame',
  );
  chatThreadStore.errorTurn(t(settled ? 'chat.answerNotShown' : 'chat.connectionDropped'));
}

/** A terminal (done/error) frame whose store reducer threw: the reducer may
 * already have ended streaming before it failed, so the turn is FORCED into
 * a notified error state rather than left looking settled with a partial or
 * blank answer. Only this turn; a '+ New' that cleared the thread wins. */
function forceTurnError(turnId: string, text: string): void {
  const active = chatThreadStore.getState().turns.at(-1);
  if (!active || active.id !== turnId) return;
  try {
    chatThreadStore.errorTurn(text);
  } catch (err) {
    console.error('[ask] could not show the turn error', err);
  }
}

/** Run a terminal frame's store reducer; the caller records the frame as
 * settled only after this returns. A reducer that throws is logged and the
 * turn is forced into the error state (settled, as an error). */
function settleTerminal(turnId: string, type: string, apply: () => void, failText: string): void {
  try {
    apply();
  } catch (err) {
    console.error('[ask] terminal handler failed', type, err);
    forceTurnError(turnId, failText);
  }
}

export interface AskTurnDeps {
  /** #plant-select value (dashboard-owned control) or 'all'. */
  plant: string;
  /** Show trace window / hide persona panel. */
  showThread: () => void;
  /** End-of-turn notify hook (fired on `done` when the settled answer is
   * substantive — chat/auto-open.ts isSubstantiveAnswer). NEVER opens the
   * modal itself: the caller decides toast-or-nothing; the full-screen modal
   * opens only from explicit user actions (the expand affordance, the header
   * pop-out, or the toast click). */
  onAnswerReady: () => void;
}

/** The active (last) turn's assistant state, or null. */
function activeAssistant() {
  const { turns } = chatThreadStore.getState();
  return turns.length ? turns[turns.length - 1].assistant : null;
}

/** Run one complete /api/ask turn. Resolves after the store has settled the
 * turn (done/error/abort); the caller owns button/loading state + memory
 * refresh. `options.retry` marks an "Ask again" on the last turn. */
export async function runAskTurn(
  question: string,
  controller: AbortController,
  deps: AskTurnDeps,
  options: AskOptions = {},
): Promise<void> {
  deps.showThread();

  // User message (window._nextUserDisplayText: demo/dashboard override).
  const displayText = window._nextUserDisplayText || question;
  window._lastDisplayText = displayText;
  window._nextUserDisplayText = null;

  // Notify architecture view of new question
  archEvent('user_msg', { question: displayText });

  const turnId = chatThreadStore.startTurn(question, displayText);
  // Set only AFTER a terminal frame's store reducer has run (settleTerminal).
  let terminalSettled = false;

  const onEvent = (eventType: string, raw: unknown): void => {
    if (eventType === 'tool_start') {
      // Flush accumulated reasoning text to the architecture trace BEFORE the
      // reducer resets the run (arch order: reasoning → tool_start).
      const reasoning = activeAssistant()?.scratch.reasoningBetweenTools ?? '';
      if (reasoning.trim()) {
        archEvent('reasoning', { text: reasoning.trim() });
      }
      chatThreadStore.applyEvent(eventType, raw);
      // G3: accumulate the canonical system ids used this session from
      // gateway tool-name prefixes ("<system_id>___<tool>") — feeds the
      // save-as-prompt required-systems prefill.
      noteSessionSystem((raw as { tool?: string }).tool);
      archEvent('tool_start', raw);
    } else if (eventType === 'tool_result') {
      const data = raw as ToolResultEventData;
      archEvent('tool_result', data);
      // Link finding node to citation (findings are minted by the architecture
      // view's richer handler, which reports the id via window._lastFindingId).
      let findingNodeId: string | null = null;
      if (data.tool !== 'explore_graph' && window._lastFindingId) {
        findingNodeId = window._lastFindingId;
        window._lastFindingId = null;
      }
      chatThreadStore.applyEvent(eventType, raw, findingNodeId);
    } else if (eventType === 'tool_end') {
      chatThreadStore.applyEvent(eventType, raw);
      archEvent('tool_end', raw);
    } else if (eventType === 'token') {
      // rAF-coalesced inside the store: one notify per animation frame.
      chatThreadStore.applyEvent(eventType, raw);
    } else if (eventType === 'metadata') {
      const data = raw as MetadataEventData;
      // Display stream payload size in the architecture trace
      if (data.streamMB !== undefined) {
        archEvent('metadata', data);
      }
      if (data.intelligence_summary) {
        window._lastIntelligenceSummary = data.intelligence_summary;
        archEvent('intelligence_summary', data.intelligence_summary);
      }
      if (data.agent_analysis) {
        window._lastAgentAnalysis = data.agent_analysis;
      }
      // intelligence/agent-analysis html is built turn-scoped by the reducer
      // (rendered inside the unified collapsible panel on `done`).
      chatThreadStore.applyEvent(eventType, raw);
    } else if (eventType === 'done') {
      const data = raw as DoneEventData;
      archEvent('done', {});
      // Reducer: analysis panel inputs, answer reconstruction
      // (segments.finalize → parseAnswerBlocks), reasoning collapse,
      // messageId — the render-time transforms live in <AssistantTurn/>.
      settleTerminal(
        turnId,
        eventType,
        () => chatThreadStore.applyEvent(eventType, raw),
        t('chat.answerNotShown'),
      );
      terminalSettled = true;
      // End-of-turn auto-open RETIRED (2026-08-06): never open the modal
      // here. Notify the caller that a substantive answer settled — it shows
      // the "response ready" toast only when no in-app surface has the
      // answer (chat/auto-open.ts); the expand affordance under the thread
      // is the explicit path to the full-screen modal.
      if (isSubstantiveAnswer(activeAssistant()?.answerMd)) {
        deps.onAnswerReady();
      }
      archEvent('done', data);
    } else if (eventType === 'reasoning') {
      // Task 1H (consumes Task 1I's additive `event: reasoning` SSE type —
      // backend heartbeats / reasoning deltas between tool batches): route
      // into the architecture trace ("Reasoning" decision entries). Order-
      // independent contract: an older backend simply never emits these.
      const text = String((raw as { text?: unknown })?.text ?? '');
      if (text.trim()) archEvent('reasoning', { text: text.trim() });
    } else if (eventType === 'error') {
      // A Stop aborts the fetch, but a frame already in the read buffer can
      // still be delivered: once the user has stopped the turn, an error
      // frame must not paint a red line on it (the AbortError path settles
      // the turn as canceled and the arch lifecycle as done).
      if (controller.signal.aborted) return;
      const data = raw as ErrorEventData;
      // Task 1H: settle the investigation lifecycle on the error path too —
      // without this the arch store's investigationActive flag (idle-but-live
      // question pulse) would stick after a failed turn.
      archEvent('done', {});
      // Plain message text — <AssistantTurn/> renders the red error
      // paragraph as JSX (escaped by construction).
      const text = String(data.text);
      settleTerminal(
        turnId,
        eventType,
        () => chatThreadStore.errorTurn(text, data.correlation_id),
        text,
      );
      terminalSettled = true;
    }
  };

  try {
    // frameErrorTag: a frame or handler failure in our own code is logged
    // ("[ask] handler failed <type>") and the stream keeps reading, so it
    // never reads as a dropped connection (sse.ts).
    await postSse('/api/ask', askRequestBody(question, deps.plant, options.retry), onEvent, {
      signal: controller.signal,
      frameErrorTag: '[ask]',
    });
    // postSse resolves on ANY clean end of the body. Without a terminal
    // `done`/`error` frame (a proxy or task closing the stream, a 401
    // redirect) the turn would stay "Working..." forever while the button
    // flips back to Send: settle it here.
    settleOpenTurn(turnId, controller, terminalSettled);
  } catch (error) {
    const err = error as { name?: string; message?: string };
    // Task 1H: any transport-level end of the stream settles the arch
    // lifecycle (HUD rows → stopped, flowing edges cleared,
    // investigationActive → false) so the live pulse can never stick —
    // covers BOTH the non-abort failure and a user Stop (abort).
    archEvent('done', {});
    if (err && err.name === 'AbortError') {
      // Quiet, explicit cancellation marker under the partial answer. A
      // '+ New' reset clears the store BEFORE this async catch runs, so the
      // marker is skipped exactly like the old contentDiv.isConnected guard.
      chatThreadStore.abortTurn();
    } else {
      // Transport failures ("POST /api/ask → 504", "Failed to fetch", a
      // network error or a JS exception) are developer text: the raw message
      // goes to the console only and the attendee reads ONE fixed line.
      console.error('[ask] stream failed:', err?.message ?? error);
      chatThreadStore.errorTurn(t('chat.connectionDropped'));
    }
  }
}

export interface UseAskStreamDeps {
  getPlant: () => string;
  showThread: () => void;
  /** See AskTurnDeps.onAnswerReady — notify only, never a modal open. */
  onAnswerReady: () => void;
  /** Called in the finally of every turn (refreshMemory(true)). */
  onTurnSettled: () => void;
}

/** Send↔Stop lifecycle — port of the isStreaming / window._askController /
 * _setComposerStreaming(...) plumbing. The composer buttons stay ENABLED while
 * streaming and click through to stop(); labels are derived from isStreaming. */
export function useAskStream(deps: UseAskStreamDeps) {
  const [isStreaming, setIsStreaming] = useState(false);
  const streamingRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const depsRef = useRef(deps);
  depsRef.current = deps;

  const stopStreaming = useCallback(() => {
    try {
      controllerRef.current?.abort();
    } catch {
      /* already settled */
    }
  }, []);

  const sendMessage = useCallback(async (question: string, options?: AskOptions) => {
    const d = depsRef.current;
    if (!question || streamingRef.current) return;
    streamingRef.current = true;
    setIsStreaming(true);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      await runAskTurn(
        question,
        controller,
        {
          plant: d.getPlant(),
          showThread: d.showThread,
          onAnswerReady: d.onAnswerReady,
        },
        options,
      );
    } finally {
      streamingRef.current = false;
      setIsStreaming(false);
      controllerRef.current = null;
      d.onTurnSettled();
    }
  }, []);

  return { isStreaming, isStreamingRef: streamingRef, sendMessage, stopStreaming };
}
