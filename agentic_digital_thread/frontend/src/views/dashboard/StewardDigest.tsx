// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Steward digest (ADR-0052 B5) — compact "Recent learning" section rendered
 * INSIDE the Operations Summary card (steward feedback 2026-08-06: the old
 * full-width card was "too technical … can't tell what I'm supposed to care
 * about"). Design rules:
 *
 *   - PLAIN LANGUAGE first: every pending proposal renders as a sentence a
 *     non-engineer can read ("Match asset name in System A with equipment
 *     id in System B"); the raw claim/score live behind a per-row
 *     <details> expander, not on the surface.
 *   - Artifact ids (ir-/jp-…) never render bare — they humanize to their kind
 *     and system ("Identity rule · System A — 342 uses") inside the
 *     technical expander.
 *   - Small: three rows, one count line, one optional stale line. Everything
 *     else (artifacts, quarantine/staging counters, the D3 proposal accept
 *     rate, cost trend) sits behind one "Technical details" expander.
 *
 * Data: ONE fast JSON GET on mount (/api/admin/learning/digest — 60 s server
 * cache). Non-admins get a 403: ANY failure renders null (best-effort,
 * never a broken section). Zero innerHTML: server strings render as JSX text. */
import { useEffect, useState } from 'react';

import { requestJson } from '../../api/client';
import type {
  DigestPrecision,
  DigestPrecisionEntry,
  LearningDigest,
  WorthReviewItem,
} from '../../api/types/learning';
import { requestArchTab } from '../architecture/arch-tab-bridge';
import { systemLabel } from './store';

/** R3 provenance-tier plain-language verb (0 user/steward, 1 agent-evidenced,
 * 2 LLM). Every worth_review row is PENDING by construction, so none of these
 * may sound like an acceptance status. */
const TIER_VERBS = [
  'Steward-defined:',
  'Proposed (with evidence):',
  'Proposed:',
] as const;

function tierVerb(tier: number): string {
  return TIER_VERBS[tier] ?? TIER_VERBS[2];
}

/** Artifact-id prefixes → human kind (graph artifact families). */
const ARTIFACT_KINDS: Record<string, string> = {
  ir: 'Identity rule',
  jp: 'Join path',
  vm: 'Value mapping',
  co: 'Concept overlay',
};

/** "equipment[id]" → "equipment id"; "asset_name" → "asset name". */
function fieldLabel(raw: string): string {
  return raw.replace(/\[(\w+)\]$/, ' $1').replace(/_/g, ' ');
}

/** One side of a claim: "system.some.path.field[sub]" → {system, field}.
 * The FIRST dot segment is the system id; the LAST is the field. */
function parseSide(side: string): { system: string; field: string } | null {
  const parts = side.trim().split('.');
  if (parts.length < 2) return null;
  const last = parts[parts.length - 1];
  return { system: parts[0], field: fieldLabel(last) };
}

/** Plain-language rendering of a proposal claim. Falls back to the raw claim
 * when the shape is unrecognized — never hides information it cannot parse. */
export function humanizeClaim(claim: string): string {
  const m = claim.match(/^(\w+):\s*(.+?)\s*\u2194\s*(.+)$/);
  if (!m) return claim;
  const [, kind, leftRaw, rightRaw] = m;
  const left = parseSide(leftRaw);
  const right = parseSide(rightRaw);
  if (!left || !right) return claim;
  const a = `${left.field} in ${systemLabel(left.system)}`;
  const b = `${right.field} in ${systemLabel(right.system)}`;
  if (kind === 'value_mapping') return `Match ${a} with ${b}`;
  if (kind === 'crosswalk') return `Translate ${a} to ${b}`;
  if (kind === 'identity_rule') return `Treat ${a} as the same as ${b}`;
  return claim;
}

/** "ir-some-system-663de5…" → "Identity rule · <system name>".
 * Unrecognized ids render as-is (honest fallback). */
export function humanizeArtifact(artifactId: string): string {
  const m = artifactId.match(/^([a-z]{2})-(.+)-[0-9a-f]{8,}$/);
  if (!m) return artifactId;
  const kind = ARTIFACT_KINDS[m[1]];
  return kind ? `${kind} · ${systemLabel(m[2])}` : artifactId;
}

/** Navigate to the Architecture review list (#proposed-mappings-panel lives
 * on the 'proposed' tab). requestArchTab stashes the request if the lazy
 * view has not mounted yet (the arch-tab-bridge seam). */
function goToReviewList(): void {
  window.switchView?.('architecture');
  requestArchTab('proposed');
}

/** Pinned E7 scope copy (ADR-0053 measurement honesty). */
const COST_TREND_TIP =
  'Measured orchestrator model usage only — system-agent model calls are not included. No cost estimate is derived.';

/** Pinned D3 scope copy (ADR-0063 — computed from decisions, report-only). */
const PRECISION_TIP =
  'Computed from your own accept and reject decisions — a quality signal about the machine\u2019s proposals. It never changes what gets filed or accepted.';

/** Engine method id → sentence-case words ("hierarchy_position" →
 * "Hierarchy position"). Ids only, no domain vocabulary. */
export function humanizeMethod(method: string): string {
  const words = method.replace(/[_-]+/g, ' ').trim();
  if (!words) return method;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Whole-percent string for a 0–1 accept rate; null when the rate key is
 * ABSENT — too few decisions to judge is not a fabricated 0%. */
function ratePercent(entry: DigestPrecisionEntry): string | null {
  const rate = entry.rate;
  if (typeof rate !== 'number' || !Number.isFinite(rate)) return null;
  return `${Math.round(rate * 100)}%`;
}

/** The honest count note used wherever a rate is absent. */
function tooFewNote(entry: DigestPrecisionEntry): string {
  return `too few decisions yet to judge (${entry.decisions} so far)`;
}

/** Turn-weighted average tokens/answer over the digest's cost_trend days.
 * Null when the key is absent, days are empty, or no day carries usable
 * measured numbers (honest absence — never a fabricated zero). */
function costTrendAvg(digest: LearningDigest): number | null {
  const days = digest.cost_trend?.days;
  if (!Array.isArray(days) || days.length === 0) return null;
  let turns = 0;
  let tokens = 0;
  for (const d of days) {
    const t = d?.turns;
    const avg = d?.avg_total_tokens;
    if (
      typeof t === 'number' && Number.isFinite(t) && t > 0 &&
      typeof avg === 'number' && Number.isFinite(avg) && avg >= 0
    ) {
      turns += t;
      tokens += t * avg;
    }
  }
  return turns > 0 ? Math.round(tokens / turns) : null;
}

/** One pending proposal as a sentence; the raw claim + score behind a native
 * <details> so the technical form is one click away, never the surface. */
function LearningRow({ item }: { item: WorthReviewItem }) {
  return (
    <details className="steward-digest-row" data-mapping-id={item.mapping_id}>
      <summary
        style={{ cursor: 'pointer', fontSize: '0.78rem', color: 'var(--text2)', padding: '0.15rem 0' }}
      >
        <span className="steward-digest-tier" style={{ color: 'var(--muted)' }}>
          {tierVerb(item.provenance_tier)}
        </span>{' '}
        {humanizeClaim(item.claim)}
      </summary>
      <div
        className="steward-digest-row-tech"
        style={{
          fontSize: '0.7rem',
          color: 'var(--muted)',
          fontFamily: "'IBM Plex Mono',monospace",
          padding: '0.1rem 0 0.25rem 1rem',
          overflowWrap: 'anywhere',
        }}
      >
        {item.claim} · consequence {item.consequence.toFixed(1)}
      </div>
    </details>
  );
}

/** D3 miner precision in plain language (ADR-0063). Renders nothing when the
 * server omitted the block (unavailable); when the rate is absent it renders
 * ONLY the honest count note — never a fabricated percentage. */
function PrecisionBlock({ precision }: { precision?: DigestPrecision }) {
  if (!precision) return null;
  const overall = ratePercent(precision);
  return (
    <div
      className="steward-digest-precision"
      title={PRECISION_TIP}
      style={{ marginTop: '0.25rem', fontSize: '0.7rem', color: 'var(--muted)', cursor: 'help' }}
    >
      <div className="steward-digest-precision-overall">
        {overall === null
          ? `Machine-proposed learnings: ${tooFewNote(precision)}`
          : `Of your last ${precision.decisions} decisions on machine-proposed learnings, ${overall} were accepted`}
      </div>
      {Object.entries(precision.by_method).map(([method, entry]) => (
        <div
          key={method}
          className="steward-digest-precision-method"
          style={{ paddingLeft: '0.75rem' }}
        >
          {humanizeMethod(method)}:{' '}
          {ratePercent(entry) === null
            ? tooFewNote(entry)
            : `${ratePercent(entry)} of ${entry.decisions} accepted`}
        </div>
      ))}
    </div>
  );
}

/** The collapsed technical block: humanized top artifacts + lifecycle
 * counters + cost trend. Everything the old card showed at full height. */
function TechnicalDetails({ digest }: { digest: LearningDigest }) {
  const avg = costTrendAvg(digest);
  return (
    <details className="steward-digest-technical" style={{ marginTop: '0.25rem' }}>
      <summary style={{ cursor: 'pointer', fontSize: '0.7rem', color: 'var(--muted)' }}>
        Technical details
      </summary>
      {digest.top_artifacts.length > 0 && (
        <div style={{ marginTop: '0.25rem' }}>
          <div style={{ fontSize: '0.68rem', color: 'var(--muted)', fontWeight: 600 }}>
            Most-used learned rules (30d)
          </div>
          {digest.top_artifacts.map((a) => (
            <div
              key={a.artifact_id}
              className="steward-digest-artifact"
              title={a.artifact_id}
              style={{ fontSize: '0.72rem', color: 'var(--text2)', padding: '0.1rem 0' }}
            >
              {humanizeArtifact(a.artifact_id)} — {a.usage_30d} use
              {a.usage_30d === 1 ? '' : 's'} · {a.evidence_classes.join(', ')}
            </div>
          ))}
        </div>
      )}
      <div
        className="steward-digest-stats"
        style={{ marginTop: '0.25rem', fontSize: '0.7rem', color: 'var(--muted)' }}
      >
        {digest.expired_unreviewed} expired unreviewed ·{' '}
        {digest.quarantine.pending_quarantined} quarantined · {digest.staging.rows} staged
      </div>
      <PrecisionBlock precision={digest.precision} />
      {avg !== null && (
        <div
          className="steward-digest-cost-trend"
          title={COST_TREND_TIP}
          style={{ fontSize: '0.7rem', color: 'var(--muted)', cursor: 'help' }}
        >
          Avg tokens/answer (7d): {avg.toLocaleString()} · orchestrator model only
        </div>
      )}
    </details>
  );
}

function DigestBody({ digest }: { digest: LearningDigest }) {
  return (
    <div id="steward-digest-content" style={{ fontSize: '0.8rem', color: 'var(--text2)' }}>
      {digest.worth_review.map((item) => (
        <LearningRow key={item.mapping_id} item={item} />
      ))}
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline', marginTop: '0.2rem' }}>
        <button
          type="button"
          className="steward-digest-review-link"
          onClick={goToReviewList}
          style={{
            background: 'none',
            border: 'none',
            padding: 0,
            cursor: 'pointer',
            color: 'var(--accent)',
            fontSize: '0.72rem',
            fontWeight: 600,
          }}
        >
          {digest.pending_total} pending proposal{digest.pending_total === 1 ? '' : 's'} — review
        </button>
        {(digest.stale_system_refs ?? 0) > 0 && (
          <span
            className="steward-digest-stale"
            style={{ fontSize: '0.68rem', color: 'var(--muted)' }}
            title="Proposals naming systems that are no longer onboarded — unreviewable until those systems return or the rows are retired by a steward."
          >
            +{digest.stale_system_refs} for systems no longer onboarded
          </span>
        )}
      </div>
      <TechnicalDetails digest={digest} />
    </div>
  );
}

/** Compact "Recent learning" section for the Operations Summary card. Renders
 * null until the digest loads (or forever, for non-admins/failures). */
export function StewardDigest() {
  const [digest, setDigest] = useState<LearningDigest | null>(null);
  useEffect(() => {
    let cancelled = false;
    requestJson<LearningDigest>('/api/admin/learning/digest')
      .then((d) => {
        if (!cancelled && d) setDigest(d);
      })
      .catch(() => {
        /* 403 (non-admin) or any failure: section stays hidden — best-effort */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  if (!digest) return null;
  return (
    <div
      id="steward-digest-card"
      style={{ marginTop: '0.6rem', paddingTop: '0.5rem', borderTop: '1px solid var(--border)' }}
    >
      <div
        className="steward-digest-heading"
        style={{ fontSize: '0.72rem', fontWeight: 600, color: 'var(--muted)', marginBottom: '0.15rem' }}
      >
        Recent learning
      </div>
      <DigestBody digest={digest} />
    </div>
  );
}
