// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Typed bridge over src/lib/analysis-util.js (a CJS-compatible
 * IIFE that publishes buildDistributionSegments / selectFinalAnswer /
 * DISTRIBUTION_PALETTE onto the global object — window in the browser,
 * globalThis under node/vitest). The side-effect import below executes the
 * IIFE; the wrappers add types without modifying the plain-JS file (it is
 * also node-required directly by the python unit lane). */
import '../lib/analysis-util.js';

import { findFollowupsMarker } from './followups-marker';

export interface DistributionSegment {
  label: string;
  count: number;
  pct: number;
  color: string;
}

export interface ToolSeqEntry {
  tool: string;
  label: string;
  type: string;
  guided: boolean;
  summary: string;
}

type AnalysisGlobals = {
  buildDistributionSegments?: (
    toolSequence: unknown,
    neptuneCount: number,
    palette?: string[],
  ) => DistributionSegment[];
  selectFinalAnswer?: (segments: string[], start?: number) => string;
  isWriteBackTool?: (name: string | undefined) => boolean;
  DISTRIBUTION_PALETTE?: string[];
};

const g = globalThis as AnalysisGlobals;

/** Reconstruct the final answer from per-turn text runs — longest run + any
 * trailing epilogue (see analysis-util.js for the P0 rationale), from `start`
 * on when given (the run after the last tool call that read data). */
export function selectFinalAnswer(segments: string[], start?: number): string {
  if (typeof g.selectFinalAnswer === 'function') return g.selectFinalAnswer(segments, start);
  // Degraded fallback (typeof-guard): keep the last run.
  return segments.length ? segments[segments.length - 1] : '';
}

/** True for a tool that writes back what the turn learned (analysis-util.js):
 * it does not end the answer. Every other tool call reads data. */
export function isWriteBackTool(name: string | undefined): boolean {
  if (typeof g.isWriteBackTool === 'function') return g.isWriteBackTool(name);
  return false;
}

/** Composition of a query's tool calls across sources (distribution bar). */
export function buildDistributionSegments(
  toolSequence: ToolSeqEntry[],
  neptuneCount: number,
): DistributionSegment[] {
  if (typeof g.buildDistributionSegments === 'function') {
    return g.buildDistributionSegments(toolSequence, neptuneCount);
  }
  return [];
}

/* ── Answer-segment accumulation ─────────────────────────────────────────
 * The `_answerSegments` bookkeeping of the chat send turn: the
 * orchestrator streams the answer in text runs separated by tool calls. Each
 * tool_start ends the current run (PRESERVED as a candidate answer segment —
 * never discarded); `done` pushes the trailing run and reconstructs via
 * selectFinalAnswer. Extracted as a pure class so the wiring is unit-testable
 * offline (src/test/answer-segments.test.ts). */

/* ── Chart survival across tool calls ─────────────────────────────────────
 * The backend ChartRedactionHook injects `<chart-block .../>` tags into the
 * token stream exactly ONCE, right after the chart-producing tool returns —
 * usually a SHORT middle run that selectFinalAnswer discards (health query:
 * chart at step 9 of 15 → no charts; outage query: chart last → charts.
 * 2026-07-20 capture analysis). Harvest tags into their own accumulator that
 * survives run boundaries; finalize() reattaches any tag missing from the
 * selected answer, deduped by chartFingerprint. The fingerprint is the TITLE
 * plus a CONTENT HASH of the whole tag: title alone dropped
 * same-title charts over different windows ("compare last hour vs yesterday"
 * lost the second chart — titles carry no window, the rendered labels/
 * datasets do). Byte-identical re-renders (the batch tool AND the chart tool
 * emitting the same chart) still collapse; retype is structurally impossible
 * (the hook withholds chart data from the model). */
const CHART_TAG_RE = /<chart-block\b[^>]*\/>/g;
const CHART_TITLE_RE = /\btitle=(?:"([^"]*)"|'([^']*)')/;

/** djb2 string hash — tiny, deterministic, good enough for dedup keys. */
function tagHash(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/** Dedup fingerprint for a chart tag: title (either quote style — the server
 * emits `title="…"`) PLUS a hash of the full tag content, so same-title
 * charts over different windows stay distinct. */
export function chartFingerprint(tag: string): string {
  const m = tag.match(CHART_TITLE_RE);
  const title = m ? (m[1] ?? m[2]) : '';
  return `title:${title}|h:${tagHash(tag)}`;
}

export class AnswerSegments {
  private segments: string[] = [];
  private current = '';
  /** Complete chart tags harvested from token text, in stream order —
   * immune to the per-run reset (survives onToolBoundary). Exact-string
   * deduped at harvest; TITLE-deduped against the answer at finalize. */
  private harvestedCharts: string[] = [];
  private harvestedSet = new Set<string>();
  /** Index of the first run that may be the answer: the run after the last
   * tool call that read data. Runs before it are planning; they stay in
   * completedRuns for the reasoning trace. */
  private answerStart = 0;

  /** `event: token` — extend the current text run, harvesting any COMPLETE
   * chart tags the moment they stream in (matching over the accumulated run
   * keeps this correct when a tag is split across token events). */
  pushToken(text: string): void {
    this.current += text;
    if (this.current.indexOf('<chart-block') !== -1) {
      for (const tag of this.current.match(CHART_TAG_RE) || []) {
        if (this.harvestedSet.has(tag)) continue;
        this.harvestedSet.add(tag);
        this.harvestedCharts.push(tag);
      }
    }
  }

  /** Live text of the in-progress run (drives the stream-md display). */
  get streamingText(): string {
    return this.current;
  }

  /** Completed text runs (preserved at tool boundaries + the trailing run
   * pushed by finalize). Rendered as the intermediate `stream-md` blocks
   * inside the reasoning trace (jsx-migration W3.1 <AssistantTurn/>). */
  get completedRuns(): readonly string[] {
    return this.segments;
  }

  /** `event: tool_start` — a tool call ends the current run. Preserve it as a
   * candidate answer segment; reset so the live stream-md display does not
   * duplicate cumulatively. A call that reads data (any tool but a
   * write-back, isWriteBackTool) moves the answer start past every run so
   * far: text written before it was planning the call, however long. A
   * boundary without a tool name keeps the longest-run rule over all runs. */
  onToolBoundary(tool?: string): void {
    if (this.current.trim()) this.segments.push(this.current);
    this.current = '';
    if (tool && !isWriteBackTool(tool)) this.answerStart = this.segments.length;
  }

  /** `event: done` — push the trailing run and reconstruct the final answer
   * (longest run + any trailing epilogue), then reattach any harvested chart
   * tag the selection discarded. */
  finalize(): string {
    if (this.current.trim()) this.segments.push(this.current);
    this.current = '';
    return this.reattachCharts(
      this.reattachFollowups(selectFinalAnswer(this.segments, this.answerStart)),
    );
  }

  /** The orchestrator often emits the trailing ---FOLLOWUPS--- block as its
   * own SHORT run (after late tool calls / the answer run), which the
   * longest-run selection discards — chips then never render even though the
   * stream carried them (2026-07-23 field report: no chips AND no raw
   * markers, because the whole run was dropped). Mirror of the chart
   * harvest: when the selected answer lacks a FOLLOWUPS block, append the
   * LAST one found across all runs. parseAnswerBlocks then splits it into
   * chips exactly as if it had streamed inside the answer run. */
  private reattachFollowups(answer: string): string {
    if (findFollowupsMarker(answer)) return answer;
    for (let i = this.segments.length - 1; i >= 0; i--) {
      const hit = findFollowupsMarker(this.segments[i]);
      if (hit) {
        return `${answer}\n\n${this.segments[i].slice(hit.start)}`;
      }
    }
    return answer;
  }

  /** Prepend harvested chart tags missing from the selected answer, deduped
   * by TITLE fingerprint (chartFingerprint). Prepended ABOVE the answer text
   * — the narrative refers to "the charts above", and anything
   * appended after a trailing ---FOLLOWUPS--- block would be lost to the
   * parseAnswerBlocks chip split. No-chart streams return the answer
   * untouched (byte-identical to the behavior before chart reattachment). */
  private reattachCharts(answer: string): string {
    if (this.harvestedCharts.length === 0) return answer;
    const seen = new Set<string>();
    for (const tag of answer.match(CHART_TAG_RE) || []) {
      seen.add(chartFingerprint(tag));
    }
    const missing: string[] = [];
    for (const tag of this.harvestedCharts) {
      const fp = chartFingerprint(tag);
      if (seen.has(fp)) continue;
      seen.add(fp);
      missing.push(tag);
    }
    if (missing.length === 0) return answer;
    return `${missing.join('\n\n')}\n\n${answer}`;
  }
}
