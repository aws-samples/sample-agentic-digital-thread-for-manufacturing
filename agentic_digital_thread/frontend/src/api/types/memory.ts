// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for agents/memory_routes.py — derived from the memory-panel and
 * session-menu client usage. */

import type { ProvenanceMetadata } from '../../chat/provenance';

/** One conversation session (GET /api/memory/sessions). The id/timestamp field
 * names vary across backends — session-menu.js probes them in this order. */
export interface MemorySession {
  sessionId?: string;
  session_id?: string;
  id?: string;
  label?: string;
  createdAt?: string | number;
  timestamp?: string | number;
  last_updated?: string | number;
  created_at?: string | number;
}

export interface SessionsResponse {
  sessions?: MemorySession[];
}

/** One ordered piece of a stored turn (agents/memory_turns.py display_blocks):
 * the same parts a live answer streams. A tool result carries the name of the
 * call it answers and the live trace's one-line summary. */
export type MemoryTurnBlock =
  | { type: 'text'; text: string }
  | { type: 'tool_use'; tool_id: string; tool: string; input?: unknown }
  | {
      type: 'tool_result';
      tool_id: string;
      tool: string;
      status?: string;
      summary?: string;
      /** The ROUTE context the gateway sent this system call (kept app-side). */
      route_context?: string;
      /** The telemetry sample line the system runtime stored for this call,
       * shown below `summary` (display only). */
      sample_line?: string;
      /** The telemetry-coverage lines the system runtime stored for this
       * call, shown above `summary` (display only, never classified). */
      telemetry_coverage?: string[];
      /** The chart tags the live stream injected right after this result
       * (complete `<chart-block .../>` tags, read from the stored result),
       * only those the server recorded the stream sending. Absent when there
       * were none and on a gated turn. */
      charts?: string[];
    };

/** One event turn (GET /api/memory/events?session_id=). Tool turns arrive as
 * concise 'Tool call: <name>' / 'Tool result' content labels; `blocks` (absent
 * on older servers and under the enforce integrity gate) holds the turn's
 * ordered pieces for the Recent sessions reload. */
export interface MemoryEvent {
  role?: string;
  content?: string;
  timestamp?: string | number;
  blocks?: MemoryTurnBlock[];
  /** On a seeded question only: the Knowledge Graph step's call and result,
   * which the live stream showed before the turn's first stored call. The
   * server rebuilds them from the stored seed (memory_turns.seed_step_blocks). */
  prelude?: MemoryTurnBlock[];
  /** On an answered question only: the `provenance` metadata frame its live
   * stream sent, kept by the server for this actor and session
   * (agents/turn_evidence_store.py). The Sources chips open their evidence
   * cards from it. Absent when nothing is returned for it: a session stored
   * before the server kept frames, one past its keep window, or a frame kept
   * from an ungated stream when the reload shows the turn gated. */
  provenance?: ProvenanceMetadata;
  /** On a question only: true when its turn's stored results held charts the
   * reload does not all replay (no record of what its stream sent, a gated
   * turn, or the response's chart byte bound). The answer says so. */
  charts_not_shown?: boolean;
}

export interface MemoryEventsResponse {
  messages?: MemoryEvent[];
  events?: MemoryEvent[];
}

/** Semantic memory tabs (GET /api/memory/semantic?session_id&type). */
export type SemanticType = 'facts' | 'summaries' | 'preferences';

export interface SemanticTopic {
  name?: string;
  body?: string;
}

/** Structured payload per strategy: userPreference / summary. */
export interface SemanticStructured {
  preference?: string;
  categories?: string[];
  context?: string;
  overview?: string;
  topics?: SemanticTopic[];
}

export interface SemanticItem {
  /** AgentCore memoryRecordId — present when the backend can address the
   * record individually (enables DELETE /api/memory/record). */
  id?: string;
  content?: string;
  text?: string;
  timestamp?: string | number;
  createdAt?: string | number;
  structured?: SemanticStructured;
}

export interface SemanticResponse {
  items?: SemanticItem[];
}
