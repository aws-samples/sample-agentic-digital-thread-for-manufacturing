// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* API types — Neptune knowledge-graph endpoints.
 *
 * Derived from the client usage (loadNeptuneGraph,
 * filterNeptuneGraph, the neighbors tap handler, requestKgAiAnalysis) with the
 * FastAPI routers as cross-check. */

/** One node of GET /api/neptune/graph. */
export interface NeptuneNode {
  id: string;
  label: string;
  /** Node type, e.g. 'System' | 'SchemaEntry' | 'Concept' | 'JoinPath' | … */
  type: string;
  props?: Record<string, unknown>;
}

/** One edge of GET /api/neptune/graph. */
export interface NeptuneEdge {
  source: string;
  target: string;
  label: string;
}

/** GET /api/neptune/graph */
export interface NeptuneGraphResponse {
  nodes: NeptuneNode[];
  edges: NeptuneEdge[];
  /** Node labels the view leaves out (Field, Property, OnboardingLog) and how
   * many the graph holds; null for a label whose count could not be read. */
  not_drawn?: Record<string, number | null>;
  /** Public error text (category message + reference) when the read failed. */
  error?: string;
}

/** The center node of GET /api/neptune/node/{id}/neighbors. */
export interface NeptuneNodeDetail {
  labels?: string[];
  properties?: Record<string, unknown>;
}

/** Outgoing neighbor edge (center → target). */
export interface NeptuneNeighborOut {
  target_id: string;
  target_name: string;
  target_label: string;
  relationship: string;
}

/** Incoming neighbor edge (source → center). */
export interface NeptuneNeighborIn {
  source_id: string;
  source_name: string;
  source_label: string;
  relationship: string;
}

/** GET /api/neptune/node/{id}/neighbors */
export interface NeptuneNeighborsResponse {
  node?: NeptuneNodeDetail;
  outgoing?: NeptuneNeighborOut[];
  incoming?: NeptuneNeighborIn[];
}

/** POST /api/kg/analyze request body. */
export interface KgAnalyzeRequest {
  node_type: string;
  node_name: string;
  properties: string;
}

/** POST /api/kg/analyze response. */
export interface KgAnalyzeResponse {
  analysis?: string;
  /** Public error text (category message + reference) when analysis failed. */
  error?: string;
}
