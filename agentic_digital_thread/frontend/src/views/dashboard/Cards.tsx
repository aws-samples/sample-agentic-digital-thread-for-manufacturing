// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Resolver-driven KPI cards (S2.3/S4.2/S5.3/S5.4, ADR 0018) — React half of
 * the old cards.ts (jsx-migration W2.1). renderCards/renderCard became the
 * store reducer (store.upsertCards); this file renders the store slices:
 * <KpiRow/> (the .kpi-row tiles) and <GaugeCards/> (gauge cards inside
 * #metrics-oee). Stable `#kpi-card-<id>` nodes come from React keys over
 * cardOrder (create-or-update in place, same as the old getElementById
 * branch). Every class/id/hint string is byte-identical to the old DOM
 * builders ('.kpi-card-hint' → "choose to drill in" / "choose for breakdown",
 * '.kpi-target' as its OWN block after .kpi-value, '.kpi-asof', the
 * '.uncovered-strip' roll-up (uncovered cards never render as
 * tiles), '.kpi-updating'/'.kpi-audit', '.diag-note').
 * No server strings ever reach markup unescaped (all text via JSX). */
import { useEffect, useState } from 'react';

import { requestJson } from '../../api/client';
import type { CardMetric, DashboardCard } from '../../api/types/dashboard';
import { rafPair } from '../../lib/raf';
import { useStore } from '../../lib/store';
import {
  bandColorVar,
  fmtCardValue,
  friendlyMetricError,
  gaugeColor,
  gaugePercent,
  gaugeStatusClass,
  gaugeSubLabel,
  isSpeedOfTargetCard,
  oeeComponentsPercent,
  oeeDisclosureText,
  siteChipLabels,
  thresholdColor,
  toPercent,
} from './lib';
import { openDrill } from './sections';
import {
  dashboardStore,
  deriveLineStateSummary,
  patchModal,
  type CardMeta,
} from './store';

/** Issue 5 — OEE drill-in: open the shared modal with the labeled breakdown
 * (body rendered by DetailModal's OeeDrillBody). */
export function openOeeDrill(card: DashboardCard): void {
  patchModal({
    open: true,
    title: card.title || 'Overall Equipment Effectiveness',
    sub: oeeDisclosureText(card.metrics),
    key: null,
    lineage: null,
    content: { kind: 'oee', card },
  });
}

/** Honest wording for a RESOLVED-but-withheld metric (no boundary `error`):
 * each honesty status says what it means and names what is missing via the
 * server's `detail` — never the generic "could not be resolved". Unknown
 * statuses return '' so the caller keeps the historical Unavailable text. */
const WITHHELD_LEADS: Record<string, string> = {
  'no-data': 'No data — the definition resolved but returned no samples',
  'invariant-violation': 'Withheld — the value fails a declared invariant',
  'unresolved-predicate': 'Withheld — a filter concept has no bound field',
  'ambiguous-binding': 'Withheld — the concept binds to several columns; a steward must choose one',
  'unresolved-input': 'Withheld — an input metric does not resolve',
  invalid: 'Not resolved — the definition does not validate',
};

function withheldDiagMsg(metric: CardMetric, card: DashboardCard): string {
  const lead = WITHHELD_LEADS[metric.status || ''];
  if (!lead) return '';
  const id = metric.metric_id || card.card_id || '';
  return lead + (id ? ' (metric "' + id + '")' : '') + (metric.detail ? ': ' + metric.detail : '') + '.';
}

/** Per-card diagnostic trace: WHY a card is not showing real data (body.diag)
 * — logic of the old applyCardDiag, 1:1. */
function cardDiagMsg(card: DashboardCard, metric: CardMetric | null): string {
  if (card.status === 'uncovered') {
    const miss = (card.missing_concepts || []).join(', ');
    return (
      'Not rendered — no onboarded system maps ' +
      (miss || 'the required concept') +
      (card.suggestion ? ' · ' + card.suggestion : '') +
      '.'
    );
  }
  if (metric && metric.status && metric.status !== 'ok') {
    const withheld = metric.error ? '' : withheldDiagMsg(metric, card);
    if (withheld) return withheld;
    return (
      'Unavailable — metric "' +
      (metric.metric_id || card.card_id) +
      '" ' +
      friendlyMetricError(metric.error) +
      '.'
    );
  }
  if (
    card.viz_type === 'gauge' &&
    !(card.metrics || []).some((m) => m && m.status === 'ok')
  ) {
    return 'Gauge unavailable — no component metric resolved.';
  }
  if (!card.drill && (card.metrics || []).some((m) => m && m.status === 'ok')) {
    return (
      'No drill target — this metric did not resolve to a physical (system, table), ' +
      'so there is no entity table to open.'
    );
  }
  return '';
}

/** Per-card "as of HH:MM" from the metric's resolve timestamp (applyAsOf). */
function asOfLabel(metric: CardMetric | null): string {
  const iso = metric && metric.status === 'ok' && metric.lineage && metric.lineage.executed_at;
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return 'as of ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}

// ── S4.2 coverage gating — compact "Awaiting data" strip ──────────────────────
// An uncovered card is a STRUCTURAL gap (no onboarded system maps a required
// concept). It is no longer rendered as a full dead KPI tile: all uncovered /
// needs-data cards collapse into ONE compact strip at the bottom of the KPI
// row — "Awaiting data: <names> — onboard a system mapping these concepts to
// light them up" — with one chip per card whose title tooltip lists the needed
// concept ids (server-supplied `missing_concepts`). Fully payload-driven (no
// hardcoded names); working cards keep their position/size. The registry lives
// in the store (`uncovered`, updated per card upsert — full snapshot AND the
// card_update ripple). All text via JSX — a server-controlled title/concept id
// can't inject markup (XSS-safe, mirrors the legacy pure-DOM note).
export function UncoveredStrip() {
  const uncovered = useStore(dashboardStore, (s) => s.uncovered);
  // Chip detail popover (steward feedback 2026-08-06): a chip click explains
  // WHAT data is missing and what would light it up, without a placeholder
  // card in the grid. Plain fixed-position overlay; Esc/backdrop closes.
  const [detailId, setDetailId] = useState<string | null>(null);
  // Investigate: fires the coverage-gap check scoped to no system (a gap is
  // about a CONCEPT, satisfiable by any onboarded system — never guessed from
  // whichever card happens to be showing it) and polls to completion. Per-chip
  // state so investigating one item never disturbs another's popover.
  const [investigateState, setInvestigateState] = useState<
    Record<string, 'idle' | 'running' | 'done' | 'error'>
  >({});
  const [investigateResult, setInvestigateResult] = useState<
    Record<string, { findings: number; filed: number } | string>
  >({});
  const ids = Object.keys(uncovered);
  if (!ids.length) return null;
  // Three wording buckets (Task 2B): structural gaps ("Awaiting data" — no
  // system maps the concept, actionable by onboarding), covered-but-empty
  // ("No data yet" — mapped, the source returned zero samples), and
  // steward-choice withholds ("Needs a steward choice"). Absent kind
  // = structural (back-compat).
  const structural = ids
    .filter((id) => uncovered[id].kind !== 'no-data' && uncovered[id].kind !== 'steward-choice')
    .sort();
  const noData = ids.filter((id) => uncovered[id].kind === 'no-data').sort();
  // Every metric withheld until a steward chooses (ambiguous-binding /
  // unresolved-predicate): openable chips whose detail view carries the
  // server's own reason, candidate columns included.
  const stewardChoice = ids.filter((id) => uncovered[id].kind === 'steward-choice').sort();
  const detail = detailId ? uncovered[detailId] : null;
  const detailIsChoice = !!detail && detail.kind === 'steward-choice';

  async function investigate(id: string): Promise<void> {
    setInvestigateState((s) => ({ ...s, [id]: 'running' }));
    try {
      const started = await requestJson<{ job_id: string }>(
        '/api/admin/learning/coverage-gap-check',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        },
      );
      // Fast poll — this is a bounded, cached-schema check (seconds, not the
      // drift check's re-discovery latency); a handful of short polls suffice.
      for (let i = 0; i < 20; i += 1) {
        await new Promise((r) => setTimeout(r, 500));
        const job = await requestJson<{
          status: string;
          result?: { findings: number; filed: number };
          error?: string;
        }>(`/api/admin/learning/coverage-gap-check/${started.job_id}`);
        if (job.status === 'done') {
          setInvestigateState((s) => ({ ...s, [id]: 'done' }));
          setInvestigateResult((r) => ({ ...r, [id]: job.result || { findings: 0, filed: 0 } }));
          return;
        }
        if (job.status === 'error') {
          setInvestigateState((s) => ({ ...s, [id]: 'error' }));
          setInvestigateResult((r) => ({ ...r, [id]: job.error || 'failed' }));
          return;
        }
      }
      setInvestigateState((s) => ({ ...s, [id]: 'error' }));
      setInvestigateResult((r) => ({ ...r, [id]: 'timed out — try again shortly' }));
    } catch {
      setInvestigateState((s) => ({ ...s, [id]: 'error' }));
      setInvestigateResult((r) => ({ ...r, [id]: 'request failed' }));
    }
  }

  return (
    <div id="uncovered-strip" className="uncovered-strip">
      {structural.length > 0 && (
        <>
          <span className="uncovered-strip-label">Awaiting data:</span>
          {structural.map((id) => {
            const entry = uncovered[id];
            return (
              <button
                key={id}
                type="button"
                className="uncovered-strip-chip"
                style={{ cursor: 'pointer', font: 'inherit' }}
                onClick={() => setDetailId(id)}
              >
                {entry.title || id}
              </button>
            );
          })}
          <span className="uncovered-strip-tail">
            {'(choose an item to see what data it needs)'}
          </span>
        </>
      )}
      {noData.length > 0 && (
        <>
          <span className="uncovered-strip-label uncovered-strip-label-nodata">
            No data yet:
          </span>
          {noData.map((id) => {
            const entry = uncovered[id];
            return (
              <span
                key={id}
                className="uncovered-strip-chip"
                title="Mapped, but the source returned no rows yet"
              >
                {entry.title || id}
              </span>
            );
          })}
        </>
      )}
      {stewardChoice.length > 0 && (
        <>
          <span className="uncovered-strip-label uncovered-strip-label-steward">
            Needs a steward choice:
          </span>
          {stewardChoice.map((id) => {
            const entry = uncovered[id];
            return (
              <button
                key={id}
                type="button"
                className="uncovered-strip-chip"
                style={{ cursor: 'pointer', font: 'inherit' }}
                onClick={() => setDetailId(id)}
              >
                {entry.title || id}
              </button>
            );
          })}
          <span className="uncovered-strip-tail">
            {'(choose an item to see the choice it needs)'}
          </span>
        </>
      )}
      {detail && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={
            detailIsChoice
              ? `Why ${detail.title} is withheld`
              : `What ${detail.title} is waiting on`
          }
          onClick={() => setDetailId(null)}
          onKeyDown={(e) => e.key === 'Escape' && setDetailId(null)}
          style={{
            position: 'fixed', inset: 0, zIndex: 60,
            background: 'rgba(0,0,0,0.35)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'var(--bg, #fff)', color: 'var(--text2, #333)',
              borderRadius: 8, padding: '1rem 1.25rem', maxWidth: 420,
              boxShadow: '0 8px 30px rgba(0,0,0,0.25)', fontSize: '0.85rem',
            }}
          >
            <div style={{ fontWeight: 600, marginBottom: '0.4rem' }}>{detail.title}</div>
            {detailIsChoice ? (
              <div className="uncovered-strip-reasons">
                {(detail.reasons || []).map((reason, i) => (
                  <div key={i} className="uncovered-strip-reason">
                    {withheldDiagMsg(
                      { metric_id: reason.metric_id, status: reason.status, detail: reason.detail },
                      { card_id: detailId || undefined },
                    ) || 'Withheld until a steward makes a choice.'}
                  </div>
                ))}
              </div>
            ) : (
              <div>
                {detail.missing.filter(Boolean).length
                  ? 'Waiting for data — no onboarded system maps '
                    + detail.missing.filter(Boolean).join(', ')
                    + ' yet. Onboard (or extend) a system that provides this concept to light it up.'
                  : 'Waiting for data — the required concept is not mapped by an onboarded system yet.'}
              </div>
            )}
            {!detailIsChoice && (() => {
              const state = investigateState[detailId!] || 'idle';
              const result = investigateResult[detailId!];
              return (
                <div style={{ marginTop: '0.6rem' }}>
                  {state === 'idle' && (
                    <button
                      type="button"
                      onClick={() => void investigate(detailId!)}
                      title="Check the connected systems' schemas for a candidate field that already looks like this concept"
                      style={{
                        padding: '0.25rem 0.75rem', marginRight: '0.5rem', cursor: 'pointer',
                        border: '1px solid var(--accent, #2563eb)', borderRadius: 4,
                        background: 'none', color: 'var(--accent, #2563eb)', font: 'inherit',
                      }}
                    >
                      Think this should be mapped? Investigate
                    </button>
                  )}
                  {state === 'running' && (
                    <span style={{ color: 'var(--muted)' }}>Checking connected systems…</span>
                  )}
                  {state === 'done' && typeof result === 'object' && (
                    <span style={{ color: 'var(--muted)' }}>
                      {result.filed > 0
                        ? `Found ${result.filed} candidate field${result.filed === 1 ? '' : 's'} — `
                          + 'see Recent learning below for review.'
                        : 'No obvious candidate field found in the connected systems\u2019 '
                          + 'schemas — this concept likely needs a new data source.'}
                    </span>
                  )}
                  {state === 'error' && (
                    <span style={{ color: 'var(--muted)' }}>
                      Could not complete the check ({typeof result === 'string' ? result : 'failed'}).
                    </span>
                  )}
                </div>
              );
            })()}
            <button
              type="button"
              onClick={() => setDetailId(null)}
              style={{
                marginTop: '0.75rem', padding: '0.25rem 0.75rem', cursor: 'pointer',
                border: '1px solid var(--border, #ccc)', borderRadius: 4, background: 'none',
                color: 'inherit', font: 'inherit',
              }}
            >
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Multi-site chips (Task 2B) ────────────────────────────────────────────────

/** Compact per-site chips under the KPI value / gauge center — rendered only
 * when the metric carries a `sites` breakdown with MORE than one entry (Task
 * 2A conditional key; a single-site deployment renders exactly as before).
 * Payload-driven (site label from the payload, no literals); all text via
 * JSX. Gauge chips normalize per-site values to percentages (same
 * toPercent contract as the center). */
function SiteChips({
  metric,
  domId,
  gauge,
}: {
  metric: CardMetric | null | undefined;
  domId: string;
  gauge?: boolean;
}) {
  const sites =
    metric && Array.isArray(metric.sites) ? metric.sites.filter((s) => !!s) : [];
  if (sites.length < 2) return null;
  const labels = siteChipLabels(sites);
  return (
    <div id={domId + '-sites'} className="kpi-site-chips">
      {sites.map((s, i) => {
        const label = labels[i];
        let valTxt: string;
        if (s.status && s.status !== 'ok') {
          valTxt = '\u2014';
        } else if (gauge) {
          const pct = toPercent(s.value);
          valTxt = pct === null ? '\u2014' : fmtCardValue(pct) + '%';
        } else if (s.value === null || s.value === undefined) {
          valTxt = '\u2014';
        } else {
          valTxt = fmtCardValue(s.value);
        }
        return (
          <span
            key={(s.system_id || s.site || '') + ':' + i}
            className="kpi-site-chip"
            title={s.system_name || s.system_id || undefined}
          >
            {(label ? label + ' ' : '') + valTxt}
          </span>
        );
      })}
    </div>
  );
}

// ── OEE gauge (S5.3 W2, ADR 0029 §1) ─────────────────────────────────────────

/** SVG circular gauge (track + value arc + centered % text). Hand-built SVG —
 * NOT Chart.js (matching dashboard.js; Chart.js is only used by the sandboxed
 * chart iframes in chat/thread). */
function GaugeSvg({ pct, color }: { pct: number | null; color: string }) {
  const size = 120;
  const stroke = 12;
  const r = (size - stroke) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const C = 2 * Math.PI * r;
  const clamped = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <svg
      className="gauge-svg"
      viewBox={'0 0 ' + size + ' ' + size}
      width={size}
      height={size}
      role="img"
    >
      <circle className="gauge-track" cx={cx} cy={cy} r={r} fill="none" strokeWidth={stroke} />
      {pct !== null && (
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={String(C)}
          strokeDashoffset={String(C * (1 - clamped / 100))}
          transform={'rotate(-90 ' + cx + ' ' + cy + ')'}
          style={{ stroke: color }}
        />
      )}
      <text
        className="gauge-center"
        x={cx}
        y={cy}
        textAnchor="middle"
        dominantBaseline="central"
        style={{ fill: color }}
      >
        {pct !== null ? fmtCardValue(pct) + '%' : '\u2014'}
      </text>
    </svg>
  );
}

/** One sub-reading row (availability/performance/quality) beneath the gauge.
 * UNIT-AWARE: only genuinely ratio-shaped readings render as percentages —
 * a units/min or hours sub-reading shows its own unit ("0 units/min" was
 * previously rendered "0%", which is meaningless). */
function GaugeSub({ metric }: { metric: CardMetric | null | undefined }) {
  const unit = (metric && metric.unit) || '';
  const isRatio = unit === 'ratio' || unit === '%' || unit === '';
  const pct = isRatio ? gaugePercent(metric) : null;
  const raw =
    !isRatio && metric && metric.status === 'ok' && typeof metric.value === 'number'
      ? fmtCardValue(metric.value) + ' ' + unit
      : null;
  return (
    <div className="gauge-sub">
      <div className="gauge-sub-label">{gaugeSubLabel(metric)}</div>
      {pct !== null ? (
        <div className="gauge-sub-value">{fmtCardValue(pct) + '%'}</div>
      ) : raw !== null ? (
        <div className="gauge-sub-value">{raw}</div>
      ) : (
        <div className="gauge-sub-value muted">{'\u2014'}</div>
      )}
    </div>
  );
}

function GaugeBody({ card, domId }: { card: DashboardCard; domId: string }) {
  const metrics = Array.isArray(card.metrics) ? card.metrics : [];
  const primary = metrics.length ? metrics[0] : null;
  const pct = gaugePercent(primary);
  const color = gaugeColor(pct);
  // OEE center honesty (Task 1D): when the resolver satisfied the composite
  // from a stored system value (prefer_stored_hit), the center is
  // SYSTEM-REPORTED — it will never equal A×P×Q of the sub-readings, so tag
  // it. Independently, when all three sub-readings resolve, reconcile the
  // client-side A×P×Q product against the center and surface the divergence
  // when it exceeds 2 points (presence-driven — no backend dependency).
  const systemReported =
    !!(primary && primary.lineage) && primary.lineage.prefer_stored_hit === true;
  const componentsPct = oeeComponentsPercent(metrics);
  const showReconcile =
    pct !== null && componentsPct !== null && Math.abs(componentsPct - pct) > 2;
  return (
    <div id={domId + '-gauge'} className="gauge-body">
      <div className="gauge-center-col">
        <GaugeSvg pct={pct} color={color} />
        {systemReported && (
          <div id={domId + '-gauge-tag'} className="gauge-center-tag muted">
            system-reported
          </div>
        )}
        {showReconcile && (
          <div
            id={domId + '-gauge-reconcile'}
            className="gauge-reconcile muted"
            title="Availability × Performance × Quality computed from the sub-readings — shown when it diverges from the center value (which a source system reported directly)"
          >
            {'A\u00d7P\u00d7Q: ' + componentsPct.toFixed(1) + '%'}
          </div>
        )}
        <SiteChips metric={primary} domId={domId + '-gauge'} gauge />
      </div>
      <div className="gauge-subs">
        {metrics.slice(1, 4).map((m, i) => (
          <GaugeSub key={i} metric={m} />
        ))}
      </div>
    </div>
  );
}

// ── The card ──────────────────────────────────────────────────────────────────

export function KpiCard({ card, meta }: { card: DashboardCard; meta: CardMeta | undefined }) {
  const cardId = card.card_id || '';
  const domId = 'kpi-card-' + cardId;
  const [highlighted, setHighlighted] = useState(false);
  const metric = card.metrics && card.metrics.length ? card.metrics[0] : null;

  // (Uncovered cards never reach here — store.upsertOne routes them to the
  // "Awaiting data" strip registry — so this renders only
  // gauge/kpi/unavailable states.)
  /** Container/drill routing keys off viz_type alone (old renderCard/applyDrill
   * ran independent of the uncovered status branch). */
  const isGaugeViz = card.viz_type === 'gauge';
  const isGauge = isGaugeViz;
  const isOkKpi = !isGauge && card.viz_type === 'kpi' && !!metric && metric.status === 'ok';

  // className + title, per the old renderCard branches.
  let base: string;
  let titleAttr: string | undefined;
  if (isGauge) {
    base = 'kpi-card kpi-card-gauge ' + gaugeStatusClass(gaugePercent(metric));
    if (metric && metric.error) titleAttr = metric.error;
  } else if (isOkKpi) {
    base = 'kpi-card status-good';
  } else {
    base = 'kpi-card status-warning';
    if (metric && metric.error) titleAttr = 'Unavailable — ' + friendlyMetricError(metric.error);
  }

  // Drill affordance (applyDrill): gauges get the labeled OEE breakdown;
  // cards with a server drill target open the entity-table modal.
  const hasDrill = !!(card.drill && card.drill.table);
  const drillable = isGaugeViz || hasDrill;
  const onClick = isGaugeViz
    ? () => openOeeDrill(card)
    : hasDrill
      ? () => {
          const lineage =
            (card.metrics && card.metrics[0] && card.metrics[0].lineage) || null;
          void openDrill(
            card.drill!.system_id,
            card.drill!.table,
            card.drill!.entity_label || card.title,
            lineage,
          );
        }
      : undefined;
  const hintText = isGaugeViz ? 'choose for breakdown' : hasDrill ? 'choose to drill in' : null;

  // Context tag (applyContext): system · site · cell under the title.
  const ctxParts: string[] = [];
  if (card.context) {
    if (card.context.system) ctxParts.push(card.context.system);
    if (card.context.site) ctxParts.push(card.context.site);
    if (card.context.cell) ctxParts.push(card.context.cell);
  }

  const diagMsg = cardDiagMsg(card, metric);
  const asOf = asOfLabel(metric);
  const bandColor = isOkKpi ? thresholdColor(card.thresholds, metric!.value) : null;
  const showTarget =
    isOkKpi && typeof card.target === 'number' && isFinite(card.target);

  // Zero-with-samples idle annotation (Task 2B): a metric that is honestly
  // ZERO over a real sample set (`ok`, value 0, sample_count > 0 — Task 2A
  // sample honesty) is annotated when the line-state slice's headline shows
  // nothing running. Generic cross-slice rule — presence-driven off the
  // payload + headline, no metric-id literals.
  const lineState = useStore(dashboardStore, (s) => s.lineState);
  const headline = deriveLineStateSummary(lineState);
  const zeroWithSamples =
    isOkKpi &&
    metric!.value === 0 &&
    typeof metric!.sample_count === 'number' &&
    metric!.sample_count > 0;
  const showIdleNote =
    zeroWithSamples &&
    !!headline &&
    headline.running === 0 &&
    headline.idle + headline.down > 0;
  const displayTitle = isSpeedOfTargetCard(card)
    ? 'Machine Speed (of target)'
    : card.title || cardId;

  // S3.2 highlight pulse — remove the phase immediately, then restore it on
  // the next paint. This replays the class without a synchronous layout read.
  const highlightSeq = meta && meta.highlightSeq;
  useEffect(() => {
    if (!highlightSeq) return;
    setHighlighted(false);
    const frame = rafPair();
    const frameId = frame.schedule(() => setHighlighted(true));
    return () => frame.cancel(frameId);
  }, [highlightSeq]);

  return (
    <div
      id={domId}
      className={
        base +
        ' card-enter' +
        (drillable ? ' drillable' : '') +
        (highlighted ? ' highlight' : '')
      }
      title={titleAttr}
      onClick={onClick}
      style={{ position: 'relative' }}
    >
      <svg
        className="kpi-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <path d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10" />
      </svg>
      <div className="kpi-title">{displayTitle}</div>
      {ctxParts.length > 0 && (
        <div id={domId + '-ctx'} className="kpi-context">
          {ctxParts.join(' \u00b7 ')}
        </div>
      )}
      {isGauge ? (
        <div className="kpi-value" id={domId + '-value'} style={{ display: 'none' }} />
      ) : isOkKpi ? (
        <div
          className="kpi-value"
          id={domId + '-value'}
          style={bandColor ? { color: bandColorVar(bandColor) } : undefined}
        >
          {fmtCardValue(metric!.value)}
          {metric!.unit ? (
            <>
              {' '}
              <span className="muted">{metric!.unit}</span>
            </>
          ) : null}
        </div>
      ) : (
        // Fail-soft: unavailable metric OR unknown/reserved viz_type — never blank.
        <div className="kpi-value" id={domId + '-value'}>
          <span className="muted">Unavailable</span>
        </div>
      )}
      {showIdleNote && (
        <div id={domId + '-idle-note'} className="kpi-idle-note muted">
          all lines idle/changeover
        </div>
      )}
      {!isGauge && <SiteChips metric={metric} domId={domId} />}
      {showTarget && (
        <div id={domId + '-target'} className="kpi-target">
          {'target ' +
            fmtCardValue(card.target) +
            (metric && metric.unit ? ' ' + metric.unit : '')}
        </div>
      )}
      {isGauge && <GaugeBody card={card} domId={domId} />}
      {hintText && (
        <div id={domId + '-hint'} className="kpi-card-hint">
          {hintText}
        </div>
      )}
      {diagMsg && (
        <div id={domId + '-diag'} className="diag-note">
          {diagMsg}
        </div>
      )}
      {/* Corner status (top-right, absolutely positioned — never shifts the
          card body): 'updating…' REPLACES the as-of text while a re-derivation
          ripple is in flight, then the as-of timestamp returns. */}
      {(asOf || (meta && meta.updating)) && (
        <div
          id={domId + '-asof'}
          className={meta && meta.updating ? 'kpi-updating' : 'kpi-asof'}
          style={{
            position: 'absolute',
            top: '0.6rem',
            right: '0.75rem',
            fontSize: '0.65rem',
            color: 'var(--muted)',
          }}
        >
          {meta && meta.updating ? 'updating\u2026' : asOf}
        </div>
      )}
      {meta && meta.audit && <div className="kpi-audit">{meta.audit}</div>}
    </div>
  );
}

// ── Containers ────────────────────────────────────────────────────────────────

/** The .kpi-row tiles (every non-gauge card, in first-seen order). */
export function KpiRow() {
  const cardOrder = useStore(dashboardStore, (s) => s.cardOrder);
  const cards = useStore(dashboardStore, (s) => s.cards);
  const cardMeta = useStore(dashboardStore, (s) => s.cardMeta);
  return (
    <div className="kpi-row">
      {cardOrder
        .filter((id) => cards[id] && cards[id].viz_type !== 'gauge')
        .map((id) => (
          <KpiCard key={id} card={cards[id]} meta={cardMeta[id]} />
        ))}
    </div>
  );
}

/** Gauge cards — rendered inside #metrics-oee (S5.4 dedicated metrics row). */
export function GaugeCards() {
  const cardOrder = useStore(dashboardStore, (s) => s.cardOrder);
  const cards = useStore(dashboardStore, (s) => s.cards);
  const cardMeta = useStore(dashboardStore, (s) => s.cardMeta);
  return (
    <>
      {cardOrder
        .filter((id) => cards[id] && cards[id].viz_type === 'gauge')
        .map((id) => (
          <KpiCard key={id} card={cards[id]} meta={cardMeta[id]} />
        ))}
    </>
  );
}
