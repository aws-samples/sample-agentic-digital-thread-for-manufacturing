// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chat thread store — reducers mirroring runAskTurn's per-event DOM mutations
 * (chat/useAskStream.ts) 1:1 over the ChatTurn model (chat/turn-model.ts).
 * Wired since jsx-migration W3.1: runAskTurn dispatches here and BOTH the
 * sidebar (#messages) and the pop-out modal (#modal-messages) render the one
 * `chatThreadStore` via <ChatThread/> (the innerHTML-mirroring hack is gone).
 *
 * Built on the W1.1 external-store helper (src/lib/store.ts createStore):
 * reducers mutate the active turn in place, then publish a fresh top-level
 * snapshot object so useSyncExternalStore/useStore subscribers re-render.
 *
 * ── Per-event semantics (SSE event → useAskStream DOM mutation → reducer) ──
 *
 * tool_start
 *   DOM: flushStreamNow(); toolCallCount++; (a pill completes on its OWN
 *        tool_result — parallel batches must not green siblings early);
 *        reasoningBetweenTools flushed to handleArchEvent('reasoning');
 *        segments.onToolBoundary() (run PRESERVED as candidate answer
 *        segment); new `.tool-pill.inline` — KG calls with a derivable
 *        question get `KG: <question>` + `data-kgq="1"`, else getToolLabel;
 *        new `.tool-trace` with the REAL input args stashed as `_toolInput`
 *        and renderToolInputBlock html; non-explore_graph tools push a
 *        numbered `_queriedSystems` citation; handleArchEvent('tool_start').
 *   Reducer: flush pending token notify; toolCallCount++; scratch.reasoningBetweenTools kept for the Wave-3
 *        transport to flush to arch-bridge (arch events stay transport-side);
 *        segments.onToolBoundary(); pills.set + traces.set (toolInput stash —
 *        rendered by <ToolInputBlock/>); citations.push for non-explore_graph.
 *
 * tool_result
 *   DOM: trace.innerHTML = sanitize(renderToolInputBlock(stashed ?? input) +
 *        `<div class="trace-result">` + mdParse(summary) + `</div>`);
 *        trace `.visible`; explore_graph pill relabelled via
 *        neptunePillLabel(formatToolInput(tool, args).summary + result)
 *        UNLESS `data-kgq` (actual-question label kept); analysis counters
 *        (explore_graph result heuristic / get_system_schema context /
 *        guided system calls); handleArchEvent('tool_result'); citation
 *        `findingNodeId` linked from window._lastFindingId.
 *   Reducer: trace.{toolInput,resultMd,visible}; same relabel guard on
 *        pill.kgq; same scratch counters; findingNodeId is an optional
 *        reducer arg (Wave-3 transport passes window._lastFindingId).
 *        Rendering is JSX at render time (model stays DOM/HTML-free).
 *
 * tool_end
 *   DOM: activePill `.done`, activePill = null; handleArchEvent('tool_end').
 *   Reducer: pills[activePillId].done = true; activePillId = null.
 *
 * token
 *   DOM: segments.pushToken(text); reasoningBetweenTools += text; stream-md
 *        re-render coalesced to one per animation frame (requestAnimationFrame).
 *   Reducer: same pushes; subscriber notify coalesced per animation frame
 *        (falls back to synchronous notify without rAF, e.g. under node).
 *
 * metadata
 *   DOM: streamMB → handleArchEvent only (no thread DOM); intelligence_summary
 *        → window._lastIntelligenceSummary + buildIntelligenceHtml;
 *        agent_analysis → buildAgentAnalysisHtml (both rendered on done).
 *   Reducer: scratch.{lastIntelligenceSummary,lastAgentAnalysis} —
 *        turn-scoped structured data instead of window globals/html strings.
 *
 * done
 *   DOM: flushStreamNow(); thinking indicator removed; ONE collapsible Agent
 *        Analysis panel assembled (buildAgentAnalysisPanel → outerHTML);
 *        responseText = segments.finalize() → parseAnswerBlocks (proposal
 *        strip + followups/actions); activePill `.done`; reasoning container
 *        folded into `<details class="tool-calls-details">` with summary
 *        `Reasoning (N tool call[s])` and the live stream-md removed; final
 *        answer rendered (chart-placeholder extract → mdParse → sanitize →
 *        `[N]`→cite-badge replace → chart reinject → collapseResponseSections);
 *        followup chips, numbered Sources row, feedback row, messageId/
 *        userMessage/assistantResponse datasets; modal auto-open (>10 chars).
 *   Reducer: streaming = false; analysisPanel = buildAgentAnalysisPanelData
 *        (structured inputs — <AgentAnalysisPanel/> renders JSX at render
 *        time); answerMd/followups/actions
 *        from finalize + parseAnswerBlocks; active pill done;
 *        reasoningCollapsed = true when any reasoning content existed
 *        (mirrors "reasoningDiv was created"); messageId = 'msg-' + Date.now().
 *        Citation-link/chart/collapse/chips/sources/feedback are RENDER
 *        concerns over answerMd + citations (<AnswerBody/>).
 *
 * error (SSE `event: error` and the non-abort catch path)
 *   DOM: thinking removed; contentDiv.innerHTML REPLACED with
 *        `<p style="color: var(--red);">Error: …</p>`.
 *   Reducer: streaming = false; error = PLAIN message text (renderer shows
 *        the red error paragraph as JSX BELOW the partial reasoning/pills,
 *        which are kept, plus an "Ask again" control).
 *
 * abort (AbortError catch path — Stop / '+ New')
 *   DOM: quiet `.response-canceled` marker ('⏹ Response canceled by user')
 *        appended UNDER the partial content (no red banner).
 *   Reducer: streaming = false; canceled = true (partial segments/pills kept;
 *        running pills settle as `stopped` with no verdict); error/errorRef
 *        cleared (a Stop is never shown as an error). Later tool_start and
 *        token events for a finished turn are dropped.
 */
import type {
  DoneEventData,
  ErrorEventData,
  MetadataEventData,
  ToolEndEventData,
  ToolResultEventData,
  ToolStartEventData,
  TokenEventData,
} from '../api/types/chat';
import { isExploreGraphTool, kgPillLabel, kgQuestionText } from '../lib/kg-label';
import { classifyToolResultDetailed } from '../lib/arch-store';
import { buildAgentAnalysisPanelData } from './analysis-panel';
import { isCitedTool } from './provenance';
import { parseAnswerBlocks } from '../shared/thread/answer-blocks';
import { createStore } from '../lib/store';
import { rafPair } from '../lib/raf';
import {
  formatToolInput,
  getCitationLabel,
  getSystemLabel,
  getToolLabel,
  neptunePillLabel,
} from './tool-labels';
import { createTurn, type ChatTurn, type Citation } from './turn-model';

export interface ThreadState {
  /** Rehydrated plain messages: one entry per plain `.message` div (user:
   * textContent; assistant: markdown with its control blocks parsed). A
   * reopened session's questions rebuild as `turns`; this holds only what
   * precedes the first question. Rendered by <ChatThread/> BEFORE the turns. */
  history: HistoryMessage[];
  turns: ChatTurn[];
}

/** One rehydrated message (no pills/traces/feedback — plain content only). */
export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
}

type Listener = () => void;

export interface ThreadStore {
  /** useSyncExternalStore-shaped snapshot (new object identity per notify;
   * turn internals are mutated in place — Wave-3 components subscribe to the
   * store, they must not rely on turn-object identity for memoization). */
  getState: () => ThreadState;
  subscribe: (listener: Listener) => () => void;
  /** New user turn (addMessage user + assistant container). Returns turn id. */
  startTurn: (question: string, displayText?: string | null) => string;
  /** Dispatch one SSE event to the ACTIVE (last) turn. `findingNodeId` is the
   * tool_result citation link (window._lastFindingId, passed by the Wave-3
   * transport after handleArchEvent). */
  applyEvent: (eventType: string, data: unknown, findingNodeId?: string | null) => void;
  /** AbortError path — quiet canceled marker under the partial answer. */
  abortTurn: () => void;
  /** Non-abort catch path — same red-paragraph semantics as `event: error`.
   * The transport passes the PLAIN error message text (see turn-model.ts
   * AssistantState.error — the paragraph renders as JSX). */
  errorTurn: (message: string, ref?: string) => void;
  /** Session rehydration: replaces the WHOLE thread with the reopened
   * session: `turns` rebuilt through the live reducers (chat/rehydrate.ts
   * buildRehydrateThread) and, before them, any plain `messages` that
   * belong to no question (the read window cut their turn off).
   * WIPE GUARD: an EMPTY rehydrate over a non-empty live thread is a no-op
   * (console.warn); only clear() ('+ New') may empty a live conversation. */
  setHistory: (messages: HistoryMessage[], turns?: ChatTurn[]) => void;
  /** Tool-pill click — toggle the matching trace's `.visible` class (the old
   * delegated classList.toggle; store-backed so both mounted <ChatThread/>
   * surfaces stay in sync). */
  toggleTrace: (turnId: string, toolId: string) => void;
  /** Open (num) / close (null) the Tier-2 evidence card for a citation on
   * the ACTIVE turn (thread-clicks dispatches here in Task 2.4; the card
   * mounts off AssistantState.openEvidenceNum). Store-backed so both
   * mounted <ChatThread/> surfaces stay in sync. */
  openEvidence: (num: number | null) => void;
  /** '+ New' — clears the thread (messagesEl.innerHTML = '' equivalent). */
  clear: () => void;
}

// rafPair (rAF + synchronous node fallback) moved to ../lib/raf — shared with
// the data-modal follow-up stream's token coalescing.

/** A server epoch-ms stamp, or undefined (older backends omit `ts`). */
const finiteTs = (ts: unknown): number | undefined =>
  typeof ts === 'number' && Number.isFinite(ts) && ts > 0 ? ts : undefined;

/** At most this many earlier ([E<k>]) results, as the runtime sends
 * (agents/citation_ordinals.py MAX_EARLIER_RESULTS). */
export const MAX_EARLIER_SOURCES = 8;
const EARLIER_LABEL = /^E([1-9]\d?)$/;

/** `intelligence_summary.earlier_sources` → earlier Citations, validated:
 * a `{label: 'E<k>', tool}` entry with k 1-99 and a tool name, first per
 * label, capped. Null when the key is absent or not a list (nothing to
 * change); [] when it is a list with nothing valid. */
export function earlierCitations(raw: unknown): Citation[] | null {
  if (!Array.isArray(raw)) return null;
  const out: Citation[] = [];
  for (const entry of raw) {
    if (out.length >= MAX_EARLIER_SOURCES) break;
    const e = entry as { label?: unknown; tool?: unknown } | null;
    const m = typeof e?.label === 'string' ? EARLIER_LABEL.exec(e.label) : null;
    const tool = typeof e?.tool === 'string' ? e.tool.slice(0, 200) : '';
    if (!m || !tool || out.some((c) => c.num === Number(m[1]))) continue;
    out.push({
      tool,
      label: getCitationLabel(tool),
      nodeId: `system-${tool}`,
      num: Number(m[1]),
      findingNodeId: null,
      earlier: true,
    });
  }
  return out;
}

export function createThreadStore(): ThreadStore {
  const store = createStore<ThreadState>({ history: [], turns: [] });
  const raf = rafPair();
  let pendingFrame: number | null = null;

  const notify = () => {
    // Fresh top-level identity so useSyncExternalStore re-renders (turn
    // internals are mutated in place — see getState doc).
    const s = store.get();
    store.set({ history: s.history, turns: s.turns });
  };

  /** flushStreamNow() equivalent — collapse a pending token notify. */
  const flushTokenNotify = () => {
    if (pendingFrame != null) {
      raf.cancel(pendingFrame);
      pendingFrame = null;
      notify();
    }
  };

  const activeTurn = (): ChatTurn | null => {
    const { turns } = store.get();
    return turns.length ? turns[turns.length - 1] : null;
  };

  const onToolStart = (turn: ChatTurn, data: ToolStartEventData) => {
    const a = turn.assistant;
    flushTokenNotify();
    // NOTE (parallel-batch honesty): a new tool_start does NOT mark the
    // previous pill done — that serial-era shortcut greened every pill of a
    // parallel fan-out the moment its SIBLING started (user-observed: all six
    // system pills green while the graph honestly pulsed in-flight). A pill
    // completes on its OWN tool_result (onToolResult) — tool_end/done still
    // finalize the active pill as the stream-tail fallback.
    a.toolCallCount++;
    a.scratch.sawReasoningContent = true;
    // reasoningBetweenTools is flushed to arch-bridge by the Wave-3 transport
    // (handleArchEvent('reasoning')); the reducer only resets the run.
    a.scratch.reasoningBetweenTools = '';
    // A tool call ends the current text run — PRESERVE it as a candidate
    // answer segment (never discard).
    const hadRun = a.segments.streamingText.trim().length > 0;
    a.segments.onToolBoundary(typeof data.tool === 'string' ? data.tool : undefined);
    if (hadRun) a.scratch.segmentCount++;
    // Pills/traces are Map-keyed: prefer the server's tool_id; without one,
    // uniquify per call (`tool#N`, N = toolCallCount) — a bare `tool` key made
    // duplicate calls to the same tool OVERWRITE the earlier pill/trace, where
    // the legacy UI appended fresh DOM per call. A
    // REPEATED tool_id is uniquified the same way (`id#N`); results bind back
    // through the trace's `wireId` (first unresolved occurrence).
    const wireId = data.tool_id || undefined;
    // An earlier call in this turn already used this wire id: both
    // occurrences become ambiguous for provenance (no primary binding).
    const sameWire = wireId
      ? [...a.traces.values()].filter((t) => t.wireId === wireId).map((t) => t.toolId)
      : [];
    let toolId = wireId && !a.pills.has(wireId) ? wireId : '';
    for (let n = a.toolCallCount; !toolId || a.pills.has(toolId); n++) {
      toolId = `${wireId || data.tool}#${n}`;
    }
    for (const key of sameWire) {
      const earlier = a.pills.get(key);
      if (earlier) earlier.ambiguousWireId = true;
    }
    // KG calls show the actual question (data-kgq) so the tool_result relabel
    // does not clobber it with a generic "Querying …" heading.
    const kgq = !!(isExploreGraphTool(data.tool) && kgQuestionText(data.tool, data.input));
    a.pills.set(toolId, {
      toolId,
      tool: data.tool,
      label: kgq ? kgPillLabel(data.tool, data.input) : getToolLabel(data.tool),
      kgq,
      done: false,
      segBoundary: a.scratch.segmentCount,
      ambiguousWireId: sameWire.length > 0 || undefined,
    });
    a.traces.set(toolId, {
      toolId,
      tool: data.tool,
      wireId,
      toolInput: data.input, // the `_toolInput` stash (REAL call args)
      resultMd: '',
      visible: false,
      startedAt: Date.now(),
      serverStartTs: finiteTs(data.ts),
    });
    a.activePillId = toolId;
    // Citation numbering rides the ONE D2 counting predicate
    // (chat/provenance.ts isCitedTool — exact `!== 'explore_graph'` test).
    // This turn's citations stay ahead of any earlier ([E<k>]) ones.
    if (isCitedTool(data.tool)) {
      a.scratch.citationCounter++;
      const firstEarlier = a.citations.findIndex((c) => c.earlier);
      a.citations.splice(firstEarlier < 0 ? a.citations.length : firstEarlier, 0, {
        tool: data.tool,
        label: getCitationLabel(data.tool),
        nodeId: `system-${data.tool}`,
        num: a.scratch.citationCounter,
        findingNodeId: null,
      });
    }
  };

  const onToolResult = (
    turn: ChatTurn,
    data: ToolResultEventData,
    findingNodeId?: string | null,
  ) => {
    const a = turn.assistant;
    // Without a tool_id, resolve to the LAST trace recorded for this tool —
    // tool_start uniquifies no-tool_id keys as `tool#N`, and the latest
    // call is the one this result belongs to. With a
    // tool_id, bind to the FIRST unresolved occurrence of that wire id (a
    // repeated id was uniquified at tool_start).
    let toolId = data.tool_id || data.tool;
    if (!data.tool_id) {
      for (const [key, t] of a.traces) if (t.tool === data.tool) toolId = key;
    } else {
      for (const [key, t] of a.traces) {
        if (t.wireId === data.tool_id && t.resultAt == null) {
          toolId = key;
          break;
        }
      }
    }
    a.scratch.lastResultKey = toolId;
    const trace = a.traces.get(toolId);
    // The pill's OWN result is its completion signal (see onToolStart note).
    const ownPill = a.pills.get(toolId);
    if (ownPill) ownPill.done = true;
    if (trace) {
      // Prefer the real input args stashed on tool_start (tool_result's
      // `input` carries the RESULT, not the args).
      const stashed = trace.toolInput;
      const toolArgs = stashed !== undefined && stashed !== null ? stashed : data.input;
      trace.toolInput = toolArgs;
      if (typeof data.route_context === 'string' && data.route_context.trim()) {
        trace.routeContext = data.route_context;
      }
      trace.resultMd = data.summary || '';
      // The stored telemetry lines ride beside the summary, never in it: the
      // outcome below is classified from the summary alone, so an id in a
      // line that reads like an error marker cannot fail a successful call,
      // and coverage lines in front cannot push a failure out of the head.
      trace.sampleLine =
        typeof data.sample_line === 'string' && data.sample_line.trim() ? data.sample_line : undefined;
      const coverage = Array.isArray(data.telemetry_coverage)
        ? data.telemetry_coverage.filter((l): l is string => typeof l === 'string' && !!l.trim())
        : [];
      trace.coverageLines = coverage.length ? coverage : undefined;
      trace.visible = true;
      // Timing + honest outcome for the activity line, classified ONCE here
      // with the same arguments the HUD uses (server elapsed first).
      const now = Date.now();
      const serverElapsed =
        typeof data.elapsed_ms === 'number' && Number.isFinite(data.elapsed_ms) && data.elapsed_ms >= 0
          ? data.elapsed_ms
          : undefined;
      trace.resultAt = now;
      trace.serverEndTs = finiteTs(data.ts);
      trace.elapsedMs =
        serverElapsed ?? (trace.startedAt != null ? Math.max(0, now - trace.startedAt) : undefined);
      const classified = classifyToolResultDetailed(
        trace.resultMd,
        trace.elapsedMs,
        data.input,
        data.tool,
      );
      trace.outcome = classified.outcome;
      trace.outcomeReason = classified.reason;
      // Update the Neptune pill label based on what was actually queried.
      if (data.tool === 'explore_graph') {
        const resultStr =
          typeof data.input === 'string'
            ? data.input
            : data.input && typeof data.input === 'object'
              ? JSON.stringify(data.input)
              : '';
        const labelSrc = (
          formatToolInput(data.tool, toolArgs).summary + ' ' + resultStr
        ).toLowerCase();
        if (labelSrc.trim()) {
          const pill = a.pills.get(toolId);
          // Keep the actual-question label set at tool_start (data-kgq).
          if (pill && !pill.kgq) pill.label = neptunePillLabel(labelSrc);
        }
      }
    }
    // Agent Analysis: track whether Neptune returned useful results.
    const s = a.scratch;
    if (data.tool === 'explore_graph') {
      const summary = data.summary || '';
      s.lastNeptuneHadResults =
        !!summary &&
        !summary.includes('No matching') &&
        !summary.includes('No results') &&
        summary.length > 20;
      s.systemCallsSinceNeptune = 0;
      s.graphLookupCount++;
      if (!s.lastNeptuneHadResults) s.emptyGraphLookups++;
    } else if (data.tool === 'get_system_schema') {
      // Schema calls provide structured context — subsequent queries are informed.
      s.hadSchemaContext = true;
    } else {
      // System query — was it guided by Neptune OR schema data?
      s.systemCallsSinceNeptune++;
      const isGuided = s.hadSchemaContext || (s.lastNeptuneHadResults && s.systemCallsSinceNeptune <= 5);
      s.toolCallSequence.push({
        tool: data.tool,
        label: getSystemLabel(data.tool),
        type: 'system',
        guided: isGuided,
        summary: (data.summary || '').slice(0, 80),
      });
    }
    // Link finding node to citation (window._lastFindingId in the old code —
    // the Wave-3 transport passes it through after handleArchEvent).
    if (data.tool !== 'explore_graph' && findingNodeId) {
      const cite = [...a.citations]
        .reverse()
        .find((c) => c.tool === data.tool && !c.findingNodeId && !c.earlier);
      if (cite) cite.findingNodeId = findingNodeId;
    }
  };

  const onToolEnd = (turn: ChatTurn, data: ToolEndEventData) => {
    const a = turn.assistant;
    // The server emits tool_end right AFTER its call's tool_result and with
    // only the tool name (agents/chat_client.py), so it belongs to the call
    // whose result just landed. Settling the most recently STARTED call
    // instead finished a still-running sibling of a parallel batch.
    const key = a.scratch.lastResultKey;
    a.scratch.lastResultKey = null;
    let target = key ? a.pills.get(key) : undefined;
    if (target && data.tool && target.tool !== data.tool) target = undefined;
    if (target && data.tool_id && a.traces.get(target.toolId)?.wireId !== data.tool_id) target = undefined;
    // Legacy stream-tail fallback (no result preceded this tool_end): the
    // named call, else the oldest call of that tool still running, else the
    // active pill.
    if (!target && data.tool_id) {
      target = [...a.pills.values()].find(
        (p) => !p.done && a.traces.get(p.toolId)?.wireId === data.tool_id,
      );
    }
    if (!target && data.tool) target = [...a.pills.values()].find((p) => !p.done && p.tool === data.tool);
    if (!target && !data.tool && a.activePillId) target = a.pills.get(a.activePillId);
    if (target) {
      target.done = true;
      if (a.activePillId === target.toolId) a.activePillId = null;
    }
  };

  const onToken = (turn: ChatTurn, data: TokenEventData) => {
    const a = turn.assistant;
    a.segments.pushToken(data.text);
    a.scratch.reasoningBetweenTools += data.text;
    if (data.text) a.scratch.sawReasoningContent = true;
  };

  const onMetadata = (turn: ChatTurn, data: MetadataEventData) => {
    const s = turn.assistant.scratch;
    // streamMB → architecture trace only (Wave-3 transport); no thread state.
    // Structured data only — the panel renders as JSX on done
    // (analysis-panel.tsx <AgentAnalysisPanel/>).
    if (data.intelligence_summary) {
      s.lastIntelligenceSummary = data.intelligence_summary;
      // Results an earlier question returned, labelled [E<k>] by the
      // runtime: replace any earlier entries (last frame wins); an absent
      // key leaves the citations exactly as they were.
      const earlier = earlierCitations(data.intelligence_summary.earlier_sources);
      if (earlier) {
        const a = turn.assistant;
        a.citations = [...a.citations.filter((c) => !c.earlier), ...earlier];
      }
    }
    if (data.agent_analysis) {
      s.lastAgentAnalysis = data.agent_analysis;
    }
    // metadata.provenance — additive per-claim records (frozen contract D1).
    // Guarded on the key's presence WITH records: a legacy stream (no key)
    // takes neither branch, so `provenance` stays [] and nothing downstream
    // changes (legacy-client proof). Storage only — chips/card render in
    // Task 2.4.
    if (data.provenance?.records) {
      turn.assistant.provenance = data.provenance.records;
    }
    // Additive top-level graph_queries — uncited-but-redeemable Tier-3
    // handles (refs + tool names only; may arrive with EMPTY records: in
    // default Mode A the bare explore_graph is never cited). Same allowlist
    // posture: absent/empty key ⇒ untouched (legacy default []); a later
    // frame REPLACES (last-write-wins, matching records).
    if (data.provenance?.graph_queries?.length) {
      turn.assistant.graphQueries = data.provenance.graph_queries;
    }
  };

  const onDone = (turn: ChatTurn, _data: DoneEventData) => {
    const a = turn.assistant;
    const s = a.scratch;
    flushTokenNotify();
    a.streaming = false;
    // ── Agent Analysis — ONE collapsible panel (collapsed by default) ──
    // Structured data only; buildAgentAnalysisPanelData returns null when
    // there is nothing to show (analysis-panel.tsx renders it on done).
    a.analysisPanel = buildAgentAnalysisPanelData(
      s.toolCallSequence,
      {
        toolCallCount: a.toolCallCount,
        graphLookupCount: s.graphLookupCount,
        emptyGraphLookups: s.emptyGraphLookups,
      },
      s.lastIntelligenceSummary,
      s.lastAgentAnalysis,
    );
    s.lastAgentAnalysis = null;
    // Reconstruct the final answer from the per-turn text runs, then strip
    // proposal blocks + split off the ---FOLLOWUPS--- chips.
    const parsed = parseAnswerBlocks(a.segments.finalize());
    a.answerMd = parsed.answer;
    a.followups = parsed.followups;
    a.actions = parsed.actions;
    // Stranded-pill hardening: the stream is over — no call
    // can still be in flight. Sweep ALL pills done, not just the active one:
    // a pill whose tool_result/tool_end never arrived would pulse forever.
    // Own-result semantics DURING streaming are unchanged (onToolResult).
    for (const pill of a.pills.values()) pill.done = true;
    a.activePillId = null;
    // Collapse the reasoning container into <details> (only if it existed).
    if (s.sawReasoningContent) a.reasoningCollapsed = true;
    a.messageId = 'msg-' + Date.now();
  };

  /** The stream ended without a result for these calls (Stop, or an error
   * frame): settle them as STOPPED, never as a verdict. */
  const settleRunningAsStopped = (turn: ChatTurn) => {
    for (const pill of turn.assistant.pills.values()) {
      if (!pill.done) pill.stopped = true;
      pill.done = true;
    }
    turn.assistant.activePillId = null;
  };

  const onError = (turn: ChatTurn, text: string, ref?: string) => {
    const a = turn.assistant;
    flushTokenNotify();
    a.streaming = false;
    // An error ends the turn, so a call still running has no result coming:
    // without this its activity line would pulse under the red error.
    settleRunningAsStopped(turn);
    a.error = text;
    a.errorRef = ref?.trim() ? ref : null;
  };

  return {
    getState: store.get,
    subscribe: store.subscribe,
    startTurn: (question, displayText) => {
      const turn = createTurn(question, displayText);
      store.get().turns.push(turn);
      notify();
      return turn.id;
    },
    applyEvent: (eventType, raw, findingNodeId) => {
      const turn = activeTurn();
      if (!turn) return;
      // A finished turn (done / canceled / error) takes no NEW work: a late
      // tool_start would add a call nothing can ever settle (an activity line
      // pulsing forever under a closed answer), and a late token would grow
      // text after the answer was finalized. Results/metadata may still land.
      if (!turn.assistant.streaming && (eventType === 'tool_start' || eventType === 'token')) return;
      turn.rev++;
      if (eventType === 'tool_start') {
        onToolStart(turn, raw as ToolStartEventData);
        notify();
      } else if (eventType === 'tool_result') {
        onToolResult(turn, raw as ToolResultEventData, findingNodeId);
        notify();
      } else if (eventType === 'tool_end') {
        onToolEnd(turn, raw as ToolEndEventData);
        notify();
      } else if (eventType === 'token') {
        onToken(turn, raw as TokenEventData);
        // rAF-coalesced notify: one re-render per animation frame, not per
        // token (useAskStream's pendingFrame/renderStreamingText batching).
        if (pendingFrame == null) {
          pendingFrame = raf.schedule(() => {
            pendingFrame = null;
            notify();
          });
        }
      } else if (eventType === 'metadata') {
        onMetadata(turn, raw as MetadataEventData);
        notify();
      } else if (eventType === 'done') {
        onDone(turn, raw as DoneEventData);
        notify();
      } else if (eventType === 'error') {
        const data = raw as ErrorEventData;
        onError(turn, data.text, data.correlation_id);
        notify();
      }
    },
    abortTurn: () => {
      const turn = activeTurn();
      if (!turn) return;
      flushTokenNotify();
      turn.rev++;
      turn.assistant.streaming = false;
      turn.assistant.canceled = true;
      // The stream is gone, so nothing is still in flight: settle running
      // pills as STOPPED (no verdict — their own result never came) so an
      // activity line never pulses under a canceled turn, and never reads
      // as a finished "Queried …" either.
      settleRunningAsStopped(turn);
      // A user Stop is never an error, even when an error frame raced the
      // abort: the turn shows only the quiet canceled marker.
      turn.assistant.error = null;
      turn.assistant.errorRef = null;
      notify();
    },
    errorTurn: (message, ref) => {
      const turn = activeTurn();
      if (!turn) return;
      turn.rev++;
      onError(turn, message, ref);
      notify();
    },
    setHistory: (messages, turns = []) => {
      // Wipe guard (chat-persistence bug): a rehydrate that fetched EMPTY
      // history must never destroy a non-empty live thread. The only caller
      // is session rehydration — an empty result there means the server-side
      // memory events have not persisted yet (or were all tool noise), NOT
      // that the user asked to clear the conversation ('+ New' → clear()).
      if (messages.length === 0 && turns.length === 0) {
        const s = store.get();
        if (s.turns.length > 0 || s.history.length > 0) {
          console.warn(
            '[thread-store] setHistory([]) ignored — would wipe a non-empty live thread',
          );
          return;
        }
      }
      flushTokenNotify();
      store.set({ history: messages, turns: [...turns] });
    },
    toggleTrace: (turnId, toolId) => {
      const { turns } = store.get();
      const turn = turns.find((t) => t.id === turnId);
      const trace = turn?.assistant.traces.get(toolId);
      if (!turn || !trace) return;
      turn.rev++;
      trace.visible = !trace.visible;
      notify();
    },
    openEvidence: (num) => {
      const turn = activeTurn();
      if (!turn) return;
      if (turn.assistant.openEvidenceNum === num) return;
      turn.rev++;
      turn.assistant.openEvidenceNum = num;
      notify();
    },
    clear: () => {
      flushTokenNotify();
      store.set({ history: [], turns: [] });
    },
  };
}
