// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Typed bridge over src/lib/cited-systems.js (a CJS-compatible IIFE that
 * publishes citedSystemIds onto the global object — the answer-segments.ts
 * pattern). The side-effect import executes the IIFE; the wrapper adds types
 * without modifying the plain-JS file (it is also node-required directly by
 * the python unit lane's cross-derivation pin,
 * tests/unit/test_feedback_cited_systems_parity.py). */
import '../lib/cited-systems.js';

type CitedSystemsGlobals = {
  citedSystemIds?: (toolNames: readonly string[]) => string[];
};

const g = globalThis as CitedSystemsGlobals;

/** Distinct owning-system IDS from the turn's citation tool names — the
 * feedback POST's `cited_systems` unit (ids, NEVER display
 * labels; mirrors the backend `systems_from_tool_names` derivation). */
export function citedSystemIds(toolNames: readonly string[]): string[] {
  if (typeof g.citedSystemIds === 'function') return g.citedSystemIds(toolNames);
  // Degraded fallback (typeof-guard): send nothing rather than labels.
  return [];
}
