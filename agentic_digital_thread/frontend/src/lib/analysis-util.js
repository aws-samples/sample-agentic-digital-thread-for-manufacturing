// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Agentic Digital Thread — Agent Analysis distribution helper.
 *
 * Pure, DOM-free logic behind the "How this answer was assembled" bar in the
 * chat Agent Analysis panel. Kept in its own module so it can be unit-tested
 * under node (see tests/unit/test_analysis_util.py) as well as executed as a
 * side-effect import (chat/answer-segments.ts) publishing browser globals.
 */
(function (global) {
  'use strict';

  // Neutral / positive chart palette — mirrors the COLORS array reused across
  // the chat, thread and dashboard surfaces. Intentionally NO red-for-low: this is
  // a composition ("how the answer was assembled"), not a pass/fail score.
  var DISTRIBUTION_PALETTE = ['#58a6ff', '#00d8ff', '#3fb950', '#d29922', '#bc8cff', '#f78166'];

  /**
   * Build the composition of a query's tool calls across sources.
   *
   * One segment for the Knowledge Graph (explore_graph lookups) and one per
   * distinct system label queried. Segment share is proportional to that
   * source's fraction of total tool calls. Pure: no DOM, no globals, no side
   * effects.
   *
   * @param {Array<{label:string}>} toolSequence per-system tool calls (client tracked)
   * @param {number} neptuneCount                knowledge-graph lookup count
   * @param {Array<string>} [palette]            hex colors to cycle (defaults to neutral palette)
   * @returns {Array<{label:string,count:number,pct:number,color:string}>}
   *   Knowledge Graph first (when present), then systems by descending count.
   *   Percentages are whole numbers. Returns [] when there are no tool calls.
   */
  function buildDistributionSegments(toolSequence, neptuneCount, palette) {
    var colors = (palette && palette.length) ? palette : DISTRIBUTION_PALETTE;
    var seq = Array.isArray(toolSequence) ? toolSequence : [];
    var graph = Number(neptuneCount) > 0 ? Math.floor(Number(neptuneCount)) : 0;

    // Group system calls by their display label (whitespace-normalised).
    var bySystem = Object.create(null);
    seq.forEach(function (t) {
      var label = (t && t.label != null) ? String(t.label).replace(/\s+/g, ' ').trim() : '';
      if (!label) label = 'System';
      bySystem[label] = (bySystem[label] || 0) + 1;
    });

    // Knowledge Graph anchors the composition first; systems follow, ordered by
    // descending count (ties broken alphabetically for a stable render).
    var sysSegs = Object.keys(bySystem)
      .map(function (label) { return { label: label, count: bySystem[label] }; })
      .sort(function (a, b) {
        return b.count - a.count || a.label.localeCompare(b.label);
      });

    var ordered = [];
    if (graph > 0) ordered.push({ label: 'Knowledge Graph', count: graph });
    ordered = ordered.concat(sysSegs);

    var total = ordered.reduce(function (sum, s) { return sum + s.count; }, 0);
    if (total <= 0) return [];

    return ordered.map(function (s, i) {
      return {
        label: s.label,
        count: s.count,
        pct: Math.round((s.count / total) * 100),
        color: colors[i % colors.length],
      };
    });
  }

  /**
   * Reconstruct the assistant's final answer from the per-turn text segments.
   *
   * The orchestrator streams text in runs separated by tool calls. The final
   * answer is NOT simply "the text after the last tool call": the model often
   * writes the full report FIRST, then makes a trailing housekeeping tool call
   * (e.g. save_correlation), then emits only a short epilogue. Keeping just the
   * last run would drop the entire report (the P0 "missing answer" bug).
   *
   * Heuristic: the real answer is the LONGEST text run the model produced;
   * anything AFTER it (a short epilogue following a trailing tool call) is
   * appended. Runs BEFORE it are inter-tool reasoning ("Now I'll query MES…")
   * and are dropped — they remain visible in the collapsed Reasoning trace.
   * Pure: no DOM, no globals, no side effects.
   *
   * `start` (optional) is the index of the first run that may be the answer:
   * the caller passes the run after the last tool call that read data
   * (see isWriteBackTool), so planning text written before that call is not
   * kept even when it is longer than the answer. Runs from `start` on are
   * selected as above; when none of them has text, every run is.
   *
   * @param {Array<string>} segments completed text runs, in stream order
   * @param {number} [start] index of the first run that may be the answer
   * @returns {string} the reconstructed final answer (may be empty)
   */
  function selectFinalAnswer(segments, start) {
    var raw = (Array.isArray(segments) ? segments : [])
      .map(function (s) { return s == null ? '' : String(s); });
    var nonBlank = function (s) { return s.trim().length > 0; };
    var from = typeof start === 'number' && start > 0 ? Math.floor(start) : 0;
    var segs = raw.slice(from).filter(nonBlank);
    if (segs.length === 0) segs = raw.filter(nonBlank);
    if (segs.length === 0) return '';
    var maxIdx = 0;
    for (var i = 1; i < segs.length; i++) {
      if (segs[i].length > segs[maxIdx].length) maxIdx = i;
    }
    return segs.slice(maxIdx).join('\n\n').trim();
  }

  /** Tools that write back what the turn learned (a correlation, a value
   * mapping, a metric definition proposal). The model calls them AFTER it has
   * written its answer, so they do not end the answer: the report before one
   * and the short epilogue after it are both kept. Every other tool call
   * reads data, and text written before it is planning. */
  var WRITE_BACK_TOOLS = ['save_correlation', 'save_value_mapping', 'propose_metric_definition'];

  /** True for a write-back tool, bare or gateway-prefixed (`target___name`). */
  function isWriteBackTool(name) {
    var tool = name == null ? '' : String(name);
    var cut = tool.lastIndexOf('___');
    if (cut !== -1) tool = tool.slice(cut + 3);
    return WRITE_BACK_TOOLS.indexOf(tool) !== -1;
  }

  global.buildDistributionSegments = buildDistributionSegments;
  global.DISTRIBUTION_PALETTE = DISTRIBUTION_PALETTE;
  global.selectFinalAnswer = selectFinalAnswer;
  global.isWriteBackTool = isWriteBackTool;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      buildDistributionSegments: buildDistributionSegments,
      DISTRIBUTION_PALETTE: DISTRIBUTION_PALETTE,
      selectFinalAnswer: selectFinalAnswer,
      isWriteBackTool: isWriteBackTool,
    };
  }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
