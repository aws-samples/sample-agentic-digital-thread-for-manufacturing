// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Click-to-drill detail modal (S5.3 W3 / S5.4) — React half of the old
 * sections.ts + the drill tables from cards.ts/lineState.ts (jsx-migration
 * W2.1). The overlay/modal skeleton ids are CONTRACTUAL
 * (#detail-modal-overlay #detail-modal-title #detail-modal-sub
 * #detail-modal-body .detail-modal-close, overlay `.open` class toggle;
 * '.oee-drill-table' with the Measurement/Value/Source system header). All
 * body content is data → JSX (no server strings in markup). An open section
 * drill live-refreshes automatically: SectionTable reads sections[modal.key]
 * from the store, so a new targeted /api/dashboard/sections response re-renders it
 * (the old renderEntitySections refill). */
import { useState } from 'react';
import { createPortal } from 'react-dom';

import type {
  DashboardCard,
  DimensionPayload,
  EntitySection,
  Lineage,
} from '../../api/types/dashboard';
import { useStore } from '../../lib/store';
import { ModalSurface } from '../../shared/ui/ModalSurface';
import {
  bucketMachineState,
  fmtCardValue,
  friendlyMetricError,
  gaugePercent,
  gaugeSubLabel,
} from './lib';
import { submitCorrection } from './sections';
import {
  isLineStateMachinesEnvelope,
  type LineStateMachinesEnvelope,
} from './lineState';
import { closeModal, dashboardStore, systemLabel, type ModalState } from './store';

// ── Section table (buildSectionTable) ─────────────────────────────────────────

/** One section's table (or a muted note). Reused by the drill modal. */
export function SectionTable({ section }: { section: EntitySection | undefined | null }) {
  if (!section) {
    return (
      <div className="detail-modal-empty">
        {'No drill-down table for this metric\u2019s source \u2014 its source is either ' +
          'non-tabular (e.g. an IoT SiteWise historian, which has no rows to browse) or its ' +
          'table isn\u2019t entity-mapped for drill-down.'}
      </div>
    );
  }
  if (section.status === 'error') {
    return (
      <div className="detail-modal-empty">
        {section.error ||
          'Section unavailable for ' +
            (section.entity_label || section.table || 'this entity')}
      </div>
    );
  }
  const columns = Array.isArray(section.columns) ? section.columns : [];
  const rows = Array.isArray(section.rows) ? section.rows : [];
  if (!columns.length || !rows.length) {
    return <div className="detail-modal-empty">No rows.</div>;
  }
  return (
    <div className="table-container">
      <table>
        <thead>
          <tr>
            {columns.map((col, i) => (
              <th key={i}>{(col && (col.label || col.column)) || ''}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              {columns.map((col, ci) => {
                const cell = Array.isArray(row)
                  ? row[ci]
                  : row
                    ? (row as Record<string, unknown>)[col.column || '']
                    : undefined;
                return <td key={ci}>{cell === null || cell === undefined ? '' : String(cell)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Lineage (lineageNode + _correctControl) ───────────────────────────────────

interface LineageOpts {
  correctable?: boolean;
}

/** What to tell the person after a correction POST: a steward's correction
 * applies at once, anyone else's is filed for a steward to review, and a
 * plain-dict error carries the server's own message. */
function correctionStatusText(result: unknown): string {
  const r = (result ?? {}) as { status?: string; error?: string };
  if (r.error) return 'Correction failed: ' + r.error;
  if (r.status === 'pending') return 'Sent for review. A steward has to accept it before the mapping changes.';
  if (r.status === 'accepted') return 'Mapping corrected.';
  return 'Correction failed: the server did not confirm it.';
}

/** Opt-in "correct this mapping" affordance (Fork F) — only on a direct node
 * with a known (system, concept), and only when the caller opts in. */
function CorrectControl({ lin }: { lin: Lineage }) {
  const [open, setOpen] = useState(false);
  const systemId = lin.authority!.system_id!;
  const concept = lin.concept!;
  const fromField =
    lin.mapping && lin.mapping.table
      ? lin.mapping.column
        ? lin.mapping.table + '.' + lin.mapping.column
        : lin.mapping.table
      : '';
  const [toField, setToField] = useState(fromField);
  const [status, setStatus] = useState('');
  return (
    <div className="lineage-correct">
      <button
        type="button"
        className="lineage-correct-toggle"
        onClick={() => setOpen((v) => !v)}
      >
        correct this mapping
      </button>
      <div className="lineage-correct-form" style={{ display: open ? '' : 'none' }}>
        <input
          type="text"
          className="lineage-correct-input"
          placeholder="table.column"
          value={toField}
          onChange={(e) => setToField(e.target.value)}
        />
        <button
          type="button"
          className="lineage-correct-submit"
          onClick={() => {
            const to = toField.trim();
            if (!to) return;
            setStatus('Saving…');
            submitCorrection({
              system_id: systemId,
              concept,
              to_field: to,
              from_field: fromField,
            })
              .then((result) => setStatus(correctionStatusText(result)))
              .catch((err) => {
                console.error('correct-mapping', err);
                setStatus('Correction failed: ' + (err as Error).message);
              });
          }}
        >
          submit
        </button>
        {status && (
          <div className="lineage-correct-status" role="status">
            {status}
          </div>
        )}
      </div>
    </div>
  );
}

/** One lineage record; recurses over inputs for derived lineage. */
export function LineageNode({
  lin,
  opts,
}: {
  lin: Lineage | null | undefined;
  opts: LineageOpts;
}) {
  if (!lin) return <div className="lineage-node" />;
  if (lin.kind === 'derived') {
    const inputs = Array.isArray(lin.inputs) ? lin.inputs : [];
    return (
      <div className="lineage-node">
        <div className="lineage-derived">
          {(lin.metric_id || 'derived') + ' = ' + (lin.op || '?') + '('}
        </div>
        {inputs.map((child, i) => (
          <div key={i} style={{ marginLeft: 12 }}>
            <LineageNode lin={child} opts={opts} />
          </div>
        ))}
        <div>{')'}</div>
      </div>
    );
  }
  // direct: metric_id → system_id → table.column
  const parts: string[] = [];
  if (lin.metric_id) parts.push(lin.metric_id);
  if (lin.authority && lin.authority.system_id) parts.push(lin.authority.system_id);
  if (lin.mapping && lin.mapping.table) {
    parts.push(
      lin.mapping.column ? lin.mapping.table + '.' + lin.mapping.column : lin.mapping.table,
    );
  }
  const correctable = !!(
    opts &&
    opts.correctable &&
    lin.authority &&
    lin.authority.system_id &&
    lin.concept
  );
  return (
    <div className="lineage-node">
      <div className="lineage-direct">{parts.join(' \u2192 ')}</div>
      {correctable && <CorrectControl lin={lin} />}
    </div>
  );
}

/** COLLAPSED "Lineage" section tucked at the bottom of a section drill. */
function LineageDetails({ lineage }: { lineage: Lineage }) {
  return (
    <details className="drill-lineage">
      <summary>Lineage</summary>
      <LineageNode lin={lineage} opts={{}} />
    </details>
  );
}

// ── OEE drill (openOeeDrill body) ─────────────────────────────────────────────

/** Issue 5 — labeled breakdown of the OEE composite. */
export function OeeDrillBody({ card }: { card: DashboardCard }) {
  const metrics = Array.isArray(card.metrics) ? card.metrics : [];
  if (!metrics.length) {
    return <div className="detail-modal-empty">No OEE components resolved.</div>;
  }
  return (
    <>
      <div className="table-container">
        <table className="oee-drill-table">
          <thead>
            <tr>
              <th>Measurement</th>
              <th>Value</th>
              <th>Source system</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m, i) => {
              if (!m) return null;
              const pct = gaugePercent(m);
              const source = systemLabel(m.system_id);
              return (
                <tr key={i} className={i === 0 ? 'oee-drill-composite' : undefined}>
                  <td>
                    {i === 0
                      ? m.label || 'OEE'
                      : gaugeSubLabel(m) || m.label || m.metric_id || ''}
                  </td>
                  {pct !== null ? (
                    <td>{fmtCardValue(pct) + '%'}</td>
                  ) : (
                    <td className="muted">{'\u2014'}</td>
                  )}
                  {i === 0 ? (
                    <td className="muted">derived from the readings below</td>
                  ) : source ? (
                    <td>{source}</td>
                  ) : (
                    <td className="muted" title="source not reported">
                      {'\u2014'}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="oee-drill-note">
        {'OEE is the product of Availability, Performance, and Quality. ' +
          'Each contributing measurement is shown with the system that supplied it.'}
      </div>
    </>
  );
}

// ── Line State drill tables (buildMachineStateTable / _buildStateDistributionTable) ──

/** Per-machine state table (Machine · Current State · Bucket). Fail-soft. */
export function MachineStateTable({ payload }: { payload: DimensionPayload | null | undefined }) {
  if (!payload || payload.status === 'error') {
    return (
      <div className="detail-modal-empty">
        {'Machine-level state unavailable' +
          (payload && payload.error ? ' \u2014 ' + friendlyMetricError(payload.error) : '') +
          '.'}
      </div>
    );
  }
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  if (!rows.length) {
    return <div className="detail-modal-empty">No machines reported a current state.</div>;
  }
  return (
    <div className="table-container">
      <table>
        <thead>
          <tr>
            <th>Machine</th>
            <th>Current State</th>
            <th>Bucket</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => {
            if (!Array.isArray(row)) return null;
            const machine = row[0];
            const state = row[1];
            const bucket = bucketMachineState(state);
            return (
              <tr key={ri}>
                <td>{machine === null || machine === undefined ? '' : String(machine)}</td>
                <td>{state === null || state === undefined ? '' : String(state)}</td>
                <td className={'ls-bucket-' + bucket}>{bucket}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Fallback table: current-state distribution (State · Machines · Bucket). */
export function StateDistributionTable({
  payload,
}: {
  payload: DimensionPayload | null | undefined;
}) {
  if (!payload || payload.status === 'error') {
    return (
      <div className="detail-modal-empty">
        {'Line State unavailable' +
          (payload && payload.error ? ' \u2014 ' + friendlyMetricError(payload.error) : '') +
          '.'}
      </div>
    );
  }
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  if (!rows.length) {
    return <div className="detail-modal-empty">No machine states reported.</div>;
  }
  return (
    <div className="table-container">
      <table>
        <thead>
          <tr>
            <th>State</th>
            <th>Machines</th>
            <th>Bucket</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => {
            if (!Array.isArray(row)) return null;
            const state = row[0];
            const count = row[1];
            const bucket = bucketMachineState(state);
            return (
              <tr key={ri}>
                <td>{state === null || state === undefined ? '' : String(state)}</td>
                <td>{count === null || count === undefined ? '' : String(count)}</td>
                <td className={'ls-bucket-' + bucket}>{bucket}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Fan-out drill body (Task 2C): per-system/site groups ─────────────────────

/** Grouped Line State drill (GET /api/dashboard/line-state/machines): one
 * group per covering system — per-machine rows where the system maps
 * event-time (`kind:'machines'`), per-state counts with the honest caption
 * where it does not (`kind:'distribution'`). All text via JSX (XSS-safe). */
export function LineStateSystemsBody({ envelope }: { envelope: LineStateMachinesEnvelope }) {
  const systems = Array.isArray(envelope.systems) ? envelope.systems : [];
  if (envelope.status === 'error') {
    return (
      <div className="detail-modal-empty">
        {'Machine-level state unavailable' +
          (envelope.error ? ' \u2014 ' + friendlyMetricError(envelope.error) : '') +
          '.'}
      </div>
    );
  }
  if (!systems.length) {
    return (
      <div className="detail-modal-empty">
        No onboarded system reports machine state.
      </div>
    );
  }
  return (
    <>
      {systems.map((sys, i) => {
        const name = sys.name || sys.system_id || 'system';
        const label = name + (sys.site ? ' \u00b7 ' + sys.site : '');
        const payload: DimensionPayload = {
          status: 'ok',
          rows: Array.isArray(sys.rows) ? sys.rows : [],
        };
        return (
          <div className="ls-drill-group" key={sys.system_id || i}>
            <div className="ls-drill-group-title">{label}</div>
            {sys.kind === 'machines' ? (
              <MachineStateTable payload={payload} />
            ) : (
              <>
                <StateDistributionTable payload={payload} />
                <div className="ls-drill-caption muted">
                  {'per-machine detail needs an event-time mapping on ' + name}
                </div>
              </>
            )}
          </div>
        );
      })}
    </>
  );
}

// ── The modal ─────────────────────────────────────────────────────────────────

function ModalBody({
  modal,
  sections,
}: {
  modal: ModalState;
  sections: Record<string, EntitySection>;
}) {
  const content = modal.content;
  if (!content) return null;
  switch (content.kind) {
    case 'loading':
      return <div className="detail-modal-empty">{content.label}</div>;
    case 'error':
      return <div className="detail-modal-empty">{content.message}</div>;
    case 'section': {
      const section = modal.key ? sections[modal.key] : undefined;
      return (
        <>
          <SectionTable section={section} />
          {modal.lineage && <LineageDetails lineage={modal.lineage} />}
        </>
      );
    }
    case 'oee':
      return <OeeDrillBody card={content.card} />;
    case 'machines':
      // Task 2C: the fan-out envelope rides the same content kind — route on
      // shape (systems[] present) so the legacy single-system payload still
      // renders the plain machines table (404-fallback path).
      return isLineStateMachinesEnvelope(content.payload) ? (
        <LineStateSystemsBody envelope={content.payload} />
      ) : (
        <MachineStateTable payload={content.payload} />
      );
    case 'distribution':
      return <StateDistributionTable payload={content.payload} />;
    default:
      return null;
  }
}

/** Click-to-drill detail modal — entity table renders here on card click.
 * The body portal keeps the fixed overlay outside the animated dashboard view,
 * whose overflow/transform would otherwise clip it to the dashboard pane. */
export function DetailModal() {
  const modal = useStore(dashboardStore, (s) => s.modal);
  const sections = useStore(dashboardStore, (s) => s.sections);
  if (!modal.open || typeof document === 'undefined') return null;
  return createPortal(
    <ModalSurface
      className="detail-modal-overlay open"
      id="detail-modal-overlay"
      open={modal.open}
      onClose={closeModal}
      closeOnOverlayClick
      labelledBy="detail-modal-title"
      initialFocus={() => document.querySelector<HTMLElement>('.detail-modal-close')}
    >
      <div className="detail-modal">
        <div className="detail-modal-header">
          <div>
            <div className="detail-modal-title" id="detail-modal-title">
              {modal.title}
            </div>
            <div className="detail-modal-sub" id="detail-modal-sub">
              {modal.sub}
            </div>
          </div>
          <button type="button" className="detail-modal-close" onClick={closeModal}>
            ✕ Close
          </button>
        </div>
        <div className="detail-modal-body" id="detail-modal-body">
          <ModalBody modal={modal} sections={sections} />
        </div>
      </div>
    </ModalSurface>,
    document.body,
  );
}
