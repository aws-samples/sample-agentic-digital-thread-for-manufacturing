// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Types for the learning admin routes (agents/learning_routes.py,
 * ADR-0052) — the /api/admin/learning/* contract. Task 3.2 extends this
 * file with the try-it job shapes. */

/** One worth-review entry of GET /api/admin/learning/digest — a pending
 * proposal ranked by consequence (evidence strength × blast radius,
 * the shared proposal_scoring formula). */
export interface WorthReviewItem {
  mapping_id: string;
  /** Human-readable claim line (type + endpoint fields). */
  claim: string;
  consequence: number;
  /** 0 = user/steward, 1 = accepted proposal, 2 = LLM/unknown (R3 tiers). */
  provenance_tier: number;
}

/** One top-artifact entry — usage-ledger aggregate over the 30d window. */
export interface DigestArtifact {
  artifact_id: string;
  kind: string;
  usage_30d: number;
  /** B3 evidence classes; pre-B3 rows fold under "tool-text". */
  evidence_classes: string[];
}

/** One precision entry (overall or per method) of the D3 block. The decision
 * COUNT is always present; ``rate`` is ABSENT below the server's minimum
 * sample size — too few decisions to judge is not a zero (ADR-0063 D3). */
export interface DigestPrecisionEntry {
  decisions: number;
  accepted: number;
  /** accepted / (accepted + rejected), 0–1. ABSENT below min_decisions. */
  rate?: number;
}

/** GET /api/admin/learning/digest → precision (ADR-0063 D3). Computed from
 * persisted steward decisions over a bounded window — REPORT-ONLY, it gates
 * nothing. ABSENT from the degraded shape (key-absent ≠ measured 0). */
export interface DigestPrecision extends DigestPrecisionEntry {
  window_days: number;
  min_decisions: number;
  by_method: Record<string, DigestPrecisionEntry>;
}

/** GET /api/admin/learning/digest (admin-gated; 403 for non-admins). */
export interface LearningDigest {
  worth_review: WorthReviewItem[];
  pending_total: number;
  /** Pending rows naming systems that are no longer onboarded — unreviewable,
   * so excluded from pending_total/worth_review and counted here instead.
   * ABSENT when system liveness could not be checked (key-absent ≠ measured 0). */
  stale_system_refs?: number;
  /** Insight findings that hit the 30d pending TTL without review. */
  expired_unreviewed: number;
  top_artifacts: DigestArtifact[];
  quarantine: { pending_quarantined: number };
  /** ADR-0051 P5 staging buffer — reported separately, never pending. */
  staging: { rows: number; distinct_subjects: number };
  /** D3 additive (ADR-0063) — accept rate on machine-proposed learnings over
   * a bounded window, per method and overall. Report-only: it gates nothing.
   * ABSENT when the digest degraded (key-absent ≠ measured 0). */
  precision?: DigestPrecision;
  /** E7 additive (ADR-0053) — day-bucketed orchestrator-token trend from the
   * cost ledger. ABSENT when the ledger is unavailable — never fabricated
   * zeros (key-absent ≠ measured 0). */
  cost_trend?: {
    days: { date: string; turns: number; avg_total_tokens: number }[];
    scope: string;
  };
}

/* ── Try-it validation (B2, tasks 3.1/3.2 — the pinned tryit contract) ───
 * POST /api/admin/learning/tryit runs ONE representative question twice —
 * baseline route seed vs seed with the candidate artifact injected — as an
 * async JobStore job polled by fast GETs (CloudFront 120 s cap forbids a
 * blocking POST). The honest replacement for the removed
 * tool_calls_before/after fiction: the diff reports what each run DID. */

/** One rule the run actually applied (id + human label). */
export interface TryItRuleRef {
  id: string;
  label: string;
}

/** One run's observed behavior (baseline or candidate — same shape). */
export interface TryItRun {
  systems_queried: string[];
  rules_applied: TryItRuleRef[];
  translation_steps: string[];
  /** ≤400 chars of the answer text. */
  answer_excerpt: string;
  elapsed_seconds: number;
  /** X-4: whether the candidate artifact was actually injected into this
   * run's route seed (always false for the baseline run). */
  injected: boolean;
}

/** Baseline→candidate routing delta computed server-side. Rule deltas are
 * {id,label} objects (the same TryItRuleRef the runs report) — X-1 pinned;
 * system deltas stay plain system-id strings. */
export interface TryItDiff {
  systems_added: string[];
  systems_removed: string[];
  rules_added: TryItRuleRef[];
  rules_removed: TryItRuleRef[];
  /** Honest server annotation — e.g. "candidate not injectable in this
   * environment" when the candidate run could not inject (X-4). */
  note?: string;
}

/** The completed job's result payload. */
export interface TryItResult {
  /** The test question actually asked — the steward-supplied override or
   * the proposal row's query_context (the UI renders it verbatim above the
   * run columns so the steward sees what was asked). */
  question: string;
  baseline: TryItRun;
  candidate: TryItRun;
  diff: TryItDiff;
  /** X-4: false when the candidate could not be injected — the comparison
   * then does NOT reflect the mapping's effect and the UI must say so. */
  candidate_injected: boolean;
}

/** GET /api/admin/learning/tryit/{job_id}. */
export interface TryItJob {
  status: 'running' | 'done' | 'error';
  /** The resolved test question (steward override or query_context) —
   * present on every job state so a poll can show what was asked. */
  question?: string;
  error?: string;
  /** X-5: sanitized human-readable failure detail (plain text) — preferred
   * over the generic copy when present. */
  error_detail?: string;
  result?: TryItResult;
}

/** POST /api/admin/learning/tryit → 202 (409 = one already running). */
export interface TryItLaunch {
  job_id: string;
}
