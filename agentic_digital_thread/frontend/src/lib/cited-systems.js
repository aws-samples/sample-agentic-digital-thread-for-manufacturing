// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Agentic Digital Thread — cited-system ID derivation for feedback.
 *
 * The feedback POST's `cited_systems` MUST carry system IDS, never display
 * labels (display labels — getSystemLabel output, which can contain
 * newlines — were diffed server-side against lowercase system ids from
 * `metrics_registry.systems_from_tool_names`, manufacturing bogus divergence
 * on essentially every exploration). This helper is the FRONTEND HALF of
 * that derivation and mirrors the backend one token-for-token:
 * gateway-prefixed tool names only (`<system>___<tool>`), prefix with `_`
 * replaced by `-`, distinct, sorted; non-prefixed (orchestrator-own) tools
 * do not count. Lowercasing is a defensive extra — gateway ids are already
 * lowercase, and the server normalizes to lowercase before diffing.
 *
 * Kept in its own plain-JS module (the analysis-util.js pattern) so the
 * python unit lane can node-require the REAL function and pin it against
 * the REAL backend derivation on the same fixture
 * (tests/unit/test_feedback_cited_systems_parity.py — the cross-derivation
 * pin that kills reintroducing label-sending on either side).
 */
(function (global) {
  'use strict';

  /**
   * Distinct owning-system ids from gateway-prefixed tool names.
   *
   * @param {Array<string>} toolNames raw tool names from the turn's citations
   * @returns {Array<string>} sorted distinct system ids (e.g. ['erp', 'mes-mil'])
   */
  function citedSystemIds(toolNames) {
    var seen = Object.create(null);
    var list = Array.isArray(toolNames) ? toolNames : [];
    for (var i = 0; i < list.length; i++) {
      var name = list[i];
      if (typeof name !== 'string' || name.indexOf('___') === -1) continue;
      var id = name.split('___')[0].replace(/_/g, '-').toLowerCase();
      if (!id) continue;
      seen[id] = true;
    }
    return Object.keys(seen).sort();
  }

  global.citedSystemIds = citedSystemIds;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { citedSystemIds: citedSystemIds };
  }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
