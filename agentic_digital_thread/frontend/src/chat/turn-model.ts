// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Chat turn data model — the React-state shape that replaces the imperative
 * per-turn DOM built by `runAskTurn` (chat/useAskStream.ts). Model only
 * (jsx-migration W1.2): NOTHING is wired yet — useAskStream keeps building
 * DOM until Wave 3 swaps it to dispatch into the thread store
 * (chat/thread-store.ts) and render via <ChatThread/>/<AssistantTurn/>.
 *
 * Field ↔ DOM correspondence (source of truth: useAskStream.ts):
 *
 * - `user.text`     — the DISPLAYED user message (window._nextUserDisplayText
 *                     override or the question); also the bookmark button's
 *                     `dataset.rawContent` stash.
 * - `user.raw`      — the question actually POSTed to /api/ask
 *                     (`messageDiv.dataset.userMessage` on done).
 * - `assistant.segments` — the AnswerSegments accumulator (answer-segments.ts,
 *                     shared with useAskStream): text runs separated by tool
 *                     calls, PRESERVED across tool boundaries; `done`
 *                     reconstructs via selectFinalAnswer.
 * - `assistant.pills` — Map<toolId, ToolPill>, insertion-ordered like the
 *                     `.tool-pill.inline` spans in the reasoning container.
 *                     `kgq` mirrors `pill.dataset.kgq` (KG question label set
 *                     at tool_start must survive the tool_result relabel);
 *                     `done` mirrors `.done`; `segBoundary` records how many
 *                     completed text runs preceded the pill so Wave 3 can
 *                     reconstruct the run/pill interleaving of the old DOM.
 * - `assistant.traces` — Map<toolId, ToolTrace>, one `.tool-trace` div each.
 *                     `toolInput` is the `_toolInput` expando stash (the REAL
 *                     call args, only available on tool_start — tool_result's
 *                     `input` field carries the RESULT); `resultMd` is
 *                     tool_result's summary markdown. The render composes
 *                     <ToolInputBlock/> + a `.trace-result` <Md/> — all JSX,
 *                     so this model stays DOM- and HTML-free.
 * - `assistant.reasoningCollapsed` — done's fold of the reasoning container
 *                     into `<details class="tool-calls-details">`; the summary
 *                     is tool-activity.ts summarizeReasoning(pills, traces).
 * - `assistant.answerMd` — the reconstructed final answer MARKDOWN after
 *                     parseAnswerBlocks stripping (proposal blocks +
 *                     ---FOLLOWUPS---). Chart-block splitting, citation-link
 *                     injection and section collapse are STRING-level
 *                     render transforms over this markdown (<AnswerBody/> →
 *                     answer-sections.ts / citations.ts / charts.ts).
 * - `assistant.citations` — the `_queriedSystems` array: one numbered entry
 *                     per non-explore_graph tool_start, `findingNodeId`
 *                     attached from the architecture view on tool_result.
 * - `assistant.followups` / `assistant.actions` — parseAnswerBlocks chips.
 * - `assistant.analysisPanel` — structured inputs of <AgentAnalysisPanel/>
 *                     (analysis-panel.tsx), assembled on done by
 *                     buildAgentAnalysisPanelData — data only, rendered as
 *                     JSX at render time.
 * - `assistant.canceled` — the AbortError path's quiet
 *                     `.response-canceled` marker ('⏹ Response canceled by
 *                     user') appended UNDER the partial content.
 * - `assistant.error` — the `event: error` / non-abort catch path; a red error
 *                     paragraph renders BELOW whatever reasoning and tool
 *                     pills the turn already streamed (they stay visible).
 * - `assistant.streaming` — the per-turn `.thinking-inline` indicator: true
 *                     from startTurn until done/error/abort.
 * - `assistant.toolCallCount` / `messageId` — feed the Reasoning summary label
 *                     and the feedback row's `dataset.messageId`.
 */
import type { IntelligenceSummary, AgentAnalysis } from '../api/types/chat';
import type { CallOutcome } from '../lib/arch-store';
import type { AgentAnalysisPanelData } from './analysis-panel';
import type { GraphQueryRef, ProvenanceRecord } from './provenance';
import { AnswerSegments } from './answer-segments';

/** One `.tool-pill.inline` span. */
export interface ToolPill {
  toolId: string;
  tool: string;
  label: string;
  /** `pill.dataset.kgq` — actual-question KG label set at tool_start; guards
   * against the tool_result explore_graph relabel clobbering it. */
  kgq: boolean;
  /** `.done` class (previous pill on tool_start; active pill on
   * tool_end/done). */
  done: boolean;
  /** Set when the turn ended (a Stop or an error frame) while this call was
   * still running: it is settled (`done`) but has no result and no verdict. */
  stopped?: boolean;
  /** The server repeated this call's tool_id within the turn: a provenance
   * tool_id match is ambiguous, so it never primary-binds (provenance.ts). */
  ambiguousWireId?: boolean;
  /** Completed text runs at creation time (run/pill interleave for Wave 3). */
  segBoundary: number;
}

/** One `.tool-trace` div. */
export interface ToolTrace {
  toolId: string;
  tool: string;
  /** The server's tool_id for this call (the Map key differs only when the
   * same id repeated and the key was uniquified). */
  wireId?: string;
  /** The `_toolInput` expando: REAL call args stashed on tool_start (updated
   * on tool_result if the stash was empty — tool_result's `input` carries the
   * RESULT). Rendered by <ToolInputBlock/> at render time. */
  toolInput: unknown;
  /** tool_result's `route_context`: the ROUTE context the system was sent,
   * when the gateway merged route-seed facts into the model's arguments. */
  routeContext?: string;
  /** tool_result's `summary` markdown (`.trace-result`); '' until then. */
  resultMd: string;
  /** tool_result's `sample_line`: the telemetry sample line the system
   * runtime stored for the call, shown as its own line below `resultMd`.
   * Display only: the outcome is classified from `resultMd` alone. */
  sampleLine?: string;
  /** tool_result's `telemetry_coverage`: the telemetry-coverage lines the
   * system runtime stored for the call, shown above `resultMd`. Display
   * only, like `sampleLine`. */
  coverageLines?: string[];
  /** `.visible` class — set when tool_result lands. */
  visible: boolean;
  /** Wall-clock ms at tool_start (activity-line duration; never sent anywhere). */
  startedAt?: number;
  /** Wall-clock ms when this call's OWN tool_result landed. Absent until then,
   * so "no result yet" stays distinct from "result with an empty summary". */
  resultAt?: number;
  /** Server emit stamps (`ts`, epoch ms) of tool_start / tool_result. The
   * group span prefers these: browser receipt times bunch up when the wire
   * delivers a start and its result in one burst. */
  serverStartTs?: number;
  serverEndTs?: number;
  /** Call duration: the server's `elapsed_ms` first, the receipt-clock
   * pairing (resultAt − startedAt) as the fallback (same order as the HUD). */
  elapsedMs?: number;
  /** classifyToolResultDetailed verdict, computed once at tool_result with
   * the HUD's arguments (lib/arch-store.ts — the ONE outcome fixture). */
  outcome?: CallOutcome;
  /** WHY a non-success outcome was chosen ('' for success). */
  outcomeReason?: string;
}

/** One `_queriedSystems` entry (numbered citation → sources row + badges). */
export interface Citation {
  tool: string;
  label: string;
  nodeId: string;
  num: number;
  findingNodeId: string | null;
  /** A result an EARLIER question in this chat returned, labelled `[E<num>]`
   * (from `intelligence_summary.earlier_sources`): it has no call in this
   * turn, so no pill, trace or provenance record, and never a `[N]` badge.
   * Absent on every citation counted from this turn's own calls. */
  earlier?: boolean;
}

/** Inputs of the <AgentAnalysisPanel/> render (assembled on done by
 * buildAgentAnalysisPanelData — structured DATA, no HTML strings). */
export type { AgentAnalysisPanelData as AnalysisPanelData } from './analysis-panel';

/** Reducer-internal scratch — the closure variables of runAskTurn that do not
 * render directly but feed the done-time analysis panel + arch trace. */
export interface TurnScratch {
  toolCallSequence: Array<{
    tool: string;
    label: string;
    type: string;
    guided: boolean;
    summary: string;
  }>;
  lastNeptuneHadResults: boolean;
  hadSchemaContext: boolean;
  systemCallsSinceNeptune: number;
  graphLookupCount: number;
  emptyGraphLookups: number;
  citationCounter: number;
  /** Token text since the last tool boundary (arch 'reasoning' flushes —
   * Wave-3 transport reads + clears it when dispatching to arch-bridge). */
  reasoningBetweenTools: string;
  /** Whether the reasoning container would exist in the old DOM (any pill or
   * any streamed token) — gates the done-collapse. */
  sawReasoningContent: boolean;
  /** Completed non-whitespace text runs so far (mirrors AnswerSegments'
   * private list length; feeds ToolPill.segBoundary). */
  segmentCount: number;
  /** Key of the call whose tool_result landed last — the server emits that
   * call's name-only tool_end right after it. Cleared by tool_end. */
  lastResultKey: string | null;
  /** metadata handlers (window._lastIntelligenceSummary / _lastAgentAnalysis
   * equivalents, scoped to the turn — structured data, rendered on done). */
  lastIntelligenceSummary: IntelligenceSummary | null;
  lastAgentAnalysis: AgentAnalysis | null;
}

export interface AssistantState {
  segments: AnswerSegments;
  pills: Map<string, ToolPill>;
  traces: Map<string, ToolTrace>;
  reasoningCollapsed: boolean;
  toolCallCount: number;
  /** Final answer markdown (post parseAnswerBlocks); null while streaming. */
  answerMd: string | null;
  citations: Citation[];
  /** metadata.provenance records for this turn (frozen contract D1; the
   * store copies `data.provenance.records` when the additive key arrives).
   * EMPTY ([]) when the stream never carried the key — absence is the
   * legacy default and changes nothing (Tier-1 chips fall back to today's
   * labels via chat/provenance.ts helpers). Storage only in this model —
   * rendering is Task 2.4's wiring. */
  provenance: ProvenanceRecord[];
  /** Additive top-level `graph_queries` rows for this turn (uncited-but-
   * redeemable Tier-3 handles — refs + tool names only; the store copies
   * `data.provenance.graph_queries` when the additive key arrives). EMPTY
   * ([]) when the stream never carried the key — absence is the legacy
   * default and renders nothing (EvidenceCard's Routing-queries list). */
  graphQueries: GraphQueryRef[];
  /** UI state: citation number whose Tier-2 evidence card is open (null =
   * no card). Set via the store's openEvidence action; consumed by the
   * Task 2.4 <EvidenceCard/> mount. */
  openEvidenceNum: number | null;
  /** A reopened answer the server returned no `provenance` frame for (stored
   * before it kept frames, past its keep window, or kept from an ungated
   * stream for a reload now shown gated): its Sources chips can open no
   * evidence card, and the footer says so. Set only by the Recent sessions
   * reload (chat/rehydrate.ts); false on every live turn. */
  evidenceNotKept: boolean;
  /** A reopened answer the server marked `charts_not_shown`: its turn's
   * stored results held charts the reload does not all replay, and the
   * footer says so. Set only by the Recent sessions reload; false on every
   * live turn. */
  chartsNotShown: boolean;
  followups: string[];
  actions: string[];
  analysisPanel: AgentAnalysisPanelData | null;
  canceled: boolean;
  /** Non-null means the turn failed: <AssistantTurn/> renders the red error
   * paragraph BELOW the reasoning/pills already streamed, plus an "Ask
   * again" control. PLAIN TEXT error message (escaped by construction). */
  error: string | null;
  /** Correlation/reference id from `event: error` (correlation_id) — rendered
   * as a muted "Reference for support:" line so a user can quote it. */
  errorRef: string | null;
  streaming: boolean;
  /** 'msg-' + Date.now() assigned on done (feedback payload key). */
  messageId: string | null;
  /** The `activePill` closure variable (toolId of the running pill). */
  activePillId: string | null;
  scratch: TurnScratch;
}

export interface ChatTurn {
  id: string;
  /** Mutation counter — bumped by every thread-store reducer that touches the
   * turn. Turn internals mutate in place (the store publishes fresh top-level
   * snapshots only), so <ChatThread/> memoizes per-turn on (turn, rev) instead
   * of object identity (jsx-migration W3.1). */
  rev: number;
  /** Epoch ms when the turn was sent — drives the elapsed time on the
   * stream-position "Working..." indicator (client clock only). */
  startedAt: number;
  user: { text: string; raw: string };
  assistant: AssistantState;
}

let _turnSeq = 0;

/** New turn — `question` is the POSTed text, `displayText` the
 * window._nextUserDisplayText override (defaults to question). */
export function createTurn(question: string, displayText?: string | null): ChatTurn {
  return {
    id: `turn-${Date.now()}-${++_turnSeq}`,
    rev: 0,
    startedAt: Date.now(),
    user: { text: displayText || question, raw: question },
    assistant: {
      segments: new AnswerSegments(),
      pills: new Map(),
      traces: new Map(),
      reasoningCollapsed: false,
      toolCallCount: 0,
      answerMd: null,
      citations: [],
      provenance: [],
      graphQueries: [],
      openEvidenceNum: null,
      evidenceNotKept: false,
      chartsNotShown: false,
      followups: [],
      actions: [],
      analysisPanel: null,
      canceled: false,
      error: null,
      errorRef: null,
      streaming: true,
      messageId: null,
      activePillId: null,
      scratch: {
        toolCallSequence: [],
        lastNeptuneHadResults: false,
        hadSchemaContext: false,
        systemCallsSinceNeptune: 0,
        graphLookupCount: 0,
        emptyGraphLookups: 0,
        citationCounter: 0,
        reasoningBetweenTools: '',
        sawReasoningContent: false,
        segmentCount: 0,
        lastResultKey: null,
        lastIntelligenceSummary: null,
        lastAgentAnalysis: null,
      },
    },
  };
}
