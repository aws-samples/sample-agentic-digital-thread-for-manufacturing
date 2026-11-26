// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Knowledge-graph question labels — a PORT-SIDE wrapper that composes the
 * verbatim-contract-tested describeToolInput (lib/architecture-util.js, loaded
 * as a global by the side-effect import below) into short human-readable
 * question text for KG tool pills, investigation-trace entries, and the KG
 * graph-node label/tooltip.
 *
 * Kept separate so architecture-util.js stays a self-contained
 * CJS-compatible IIFE (tests/unit/test_architecture_util.py). */
import './architecture-util.js';

const _g = globalThis as Record<string, unknown>;

function _describeToolInput(tool: string, input: unknown): string {
  const fn = _g.describeToolInput;
  try {
    return typeof fn === 'function'
      ? String((fn as (t: string, i: unknown) => string)(tool, input) || '')
      : '';
  } catch {
    return '';
  }
}

function _truncate(s: string, n: number): string {
  const t = s.replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n).trimEnd() + '…' : t;
}

/** True for a knowledge-graph exploration call, in either the local
 * ("explore_graph") or gateway ("digital-thread___explore_graph") shape. */
export function isExploreGraphTool(tool: unknown): boolean {
  if (!tool || typeof tool !== 'string') return false;
  const half = tool.includes('___') ? tool.split('___')[1] : tool;
  return half === 'explore_graph';
}

/** Short human-readable question/summary of a KG call's input args.
 *
 * Priority: an explicit natural-language field (question/prompt/…) from the
 * tool_start input, else the Englishized Cypher from describeToolInput with
 * the boilerplate ("Looked up … in the knowledge graph") compacted away.
 * Returns '' when nothing meaningful can be derived. */
export function kgQuestionText(tool: string, input: unknown): string {
  if (input && typeof input === 'object' && !Array.isArray(input)) {
    const obj = input as Record<string, unknown>;
    const lower: Record<string, string> = {};
    for (const k of Object.keys(obj)) lower[k.toLowerCase()] = k;
    for (const key of ['question', 'sub_question', 'subquestion', 'prompt', 'text']) {
      const actual = lower[key];
      const v = actual != null ? obj[actual] : undefined;
      if (typeof v === 'string' && v.trim()) return _truncate(v, 90);
    }
  }
  const desc = _describeToolInput(tool, input);
  if (!desc || /^Explored the knowledge graph$/i.test(desc)) return '';
  // "Looked up System and Asset nodes in the knowledge graph matching “X”"
  // → "System and Asset nodes matching “X”" (the KG context is implied by
  // the pill's KG prefix / the Knowledge Graph node itself).
  const compact = desc
    .replace(/^Looked up\s+/i, '')
    .replace(/\s+in the knowledge graph/i, '');
  return _truncate(compact, 90);
}

/** Chat tool-pill label for a KG call — "KG: <question>", falling back to the
 * original generic label when no question can be derived. */
export function kgPillLabel(tool: string, input: unknown): string {
  const q = kgQuestionText(tool, input);
  return q ? `KG: ${q}` : 'Querying knowledge graph';
}
