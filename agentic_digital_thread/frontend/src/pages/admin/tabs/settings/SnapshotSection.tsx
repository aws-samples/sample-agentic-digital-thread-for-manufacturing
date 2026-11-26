// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Snapshot section — the Settings-tab surface for the operational graph
 * snapshot tooling (agents/graph_snapshot_routes.py):
 *
 *  - EXPORT walks the paginated GET /api/admin/graph/snapshot?cursor=...
 *    chunks and assembles the file CLIENT-SIDE, mirroring the CLI
 *    (scripts/graph_snapshot.py::cmd_export + agents/graph_snapshot.py::
 *    assemble_snapshot) — edge endpoints become file-local positional
 *    indexes, gids never land in the downloaded file. Download is a Blob
 *    anchor (no innerHTML — zero-sink policy).
 *  - IMPORT is DESTRUCTIVE (wipe-and-restore): the button stays disabled
 *    until the operator types the literal confirm token (the DisconnectModal
 *    type-to-confirm pattern), then POSTs and polls the background job.
 *    The job result carries a reconcile report (husk check) rendered as a
 *    table; unhealthy (husk) rows are highlighted and unlock "Repair now".
 *  - RECONCILE "Check system health" runs the read-only check on demand;
 *    repair POSTs {repair:true} and polls to done, re-rendering report_after.
 *
 * All transport goes through requestJson (same-origin, cookie auth); every
 * failure is surfaced inline, never swallowed. */
import { useEffect, useRef, useState, type ChangeEvent, type CSSProperties } from 'react';

import { ApiError, requestJson } from '../../../../api/client';

/** Snapshot file format — must match agents/graph_snapshot.py FORMAT_VERSION. */
export const SNAPSHOT_FORMAT = 'adt-graph-snapshot/1';
/** Literal confirmation token the import body must carry, verbatim. */
export const CONFIRM_TOKEN = 'wipe-and-restore';

const SNAPSHOT_URL = '/api/admin/graph/snapshot';
const POLL_MS = 2000;
/** Same ceiling as the CLI's IMPORT_TIMEOUT_SECONDS (45 min). */
const POLL_TIMEOUT_MS = 45 * 60 * 1000;

// ── backend contract shapes ──────────────────────────────────────────────────

interface NodeItem {
  gid: string | number;
  labels: string[];
  props: Record<string, unknown>;
}

interface EdgeItem {
  type: string;
  from_gid: string | number;
  to_gid: string | number;
  props?: Record<string, unknown>;
}

interface SnapshotChunk {
  kind: 'nodes' | 'edges';
  items: unknown[];
  next_cursor?: string | null;
}

export interface GraphSnapshot {
  format: string;
  exported_at: string;
  node_count: number;
  edge_count: number;
  nodes: { labels: string[]; props: Record<string, unknown> }[];
  edges: { type: string; from: number; to: number; props: Record<string, unknown> }[];
}

export interface ReconcileRow {
  system_id?: string;
  runtime_ok?: boolean;
  target_ok?: boolean;
  healthy?: boolean;
}

interface ImportJobResult {
  reconcile?: ReconcileRow[];
  reconcile_error?: string;
}

interface RepairJobResult {
  repaired?: string[];
  healthy?: string[];
  report_after?: ReconcileRow[];
}

interface JobStatus<R> {
  state: 'none' | 'running' | 'done' | 'error';
  result?: R;
  error?: string;
}

// ── pure helpers (module-level; mirror the CLI assembly faithfully) ──────────

/** Mirror of agents/graph_snapshot.py::assemble_snapshot — edge endpoints
 * become FILE-LOCAL positional indexes into `nodes` (never an assumed `id`
 * property); an edge referencing an unknown gid means the graph mutated
 * between pages and the export must be re-run. */
export function assembleSnapshot(nodeItems: NodeItem[], edgeItems: EdgeItem[]): GraphSnapshot {
  const indexByGid = new Map<string | number, number>();
  nodeItems.forEach((item, i) => indexByGid.set(item.gid, i));
  const nodes = nodeItems.map((item) => ({ labels: item.labels, props: item.props }));
  const edges = edgeItems.map((item) => {
    const from = indexByGid.get(item.from_gid);
    const to = indexByGid.get(item.to_gid);
    if (from === undefined || to === undefined) {
      const missing = from === undefined ? item.from_gid : item.to_gid;
      throw new Error(
        `edge references unknown node id ${String(missing)} — graph changed during export; re-run the export`,
      );
    }
    return { type: item.type, from, to, props: item.props ?? {} };
  });
  return {
    format: SNAPSHOT_FORMAT,
    exported_at: new Date().toISOString(),
    node_count: nodes.length,
    edge_count: edges.length,
    nodes,
    edges,
  };
}

/** Mirror of scripts/graph_snapshot.py::cmd_export's walk: start at cursor "",
 * route each chunk's items by kind (anything not "nodes" is edges), and stop
 * when next_cursor is null/absent. */
async function walkExport(
  onProgress: (nodes: number, edges: number) => void,
): Promise<{ nodeItems: NodeItem[]; edgeItems: EdgeItem[] }> {
  const nodeItems: NodeItem[] = [];
  const edgeItems: EdgeItem[] = [];
  let cursor: string | null = '';
  while (cursor !== null) {
    const chunk: SnapshotChunk = await requestJson<SnapshotChunk>(
      `${SNAPSHOT_URL}?cursor=${encodeURIComponent(cursor)}`,
    );
    if (chunk.kind === 'nodes') nodeItems.push(...(chunk.items as NodeItem[]));
    else edgeItems.push(...(chunk.items as EdgeItem[]));
    onProgress(nodeItems.length, edgeItems.length);
    cursor = chunk.next_cursor ?? null;
  }
  return { nodeItems, edgeItems };
}

/** Blob-anchor download (no innerHTML). Filename carries the export date. */
function downloadSnapshot(snapshot: GraphSnapshot): void {
  const blob = new Blob([JSON.stringify(snapshot, null, 1)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `adt-graph-snapshot-${snapshot.exported_at.slice(0, 10)}.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** Client-side refusal of anything that is not a well-formed snapshot —
 * the same checks validate_snapshot applies server-side BEFORE any wipe. */
export function validateSnapshotShape(payload: unknown): GraphSnapshot {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('snapshot must be a JSON object');
  }
  const snap = payload as Record<string, unknown>;
  if (snap.format !== SNAPSHOT_FORMAT) {
    throw new Error(
      `unsupported snapshot format ${JSON.stringify(snap.format)} — expected "${SNAPSHOT_FORMAT}"`,
    );
  }
  if (!Array.isArray(snap.nodes) || !Array.isArray(snap.edges)) {
    throw new Error("snapshot 'nodes' and 'edges' must be arrays");
  }
  return snap as unknown as GraphSnapshot;
}

/** Poll a job-status route until it leaves `running` (first check immediate,
 * then every POLL_MS, bounded by the CLI's 45-min ceiling). `alive` lets an
 * unmounted component stop polling. */
async function pollJob<R>(
  statusUrl: string,
  alive: () => boolean,
  onTick: (state: string) => void,
): Promise<JobStatus<R>> {
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  for (;;) {
    const job = await requestJson<JobStatus<R>>(statusUrl);
    onTick(job.state);
    if (job.state !== 'running') return job;
    if (!alive()) throw new Error('polling stopped — the page was closed');
    if (Date.now() > deadline) throw new Error('job polling timed out');
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

function jsonInit(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

function errorDetail(error: unknown): string {
  return error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
}

// ── section styling (verbatim from SettingsTab's section idiom) ──────────────

const h3Style: CSSProperties = { margin: '0 0 1.25rem', fontSize: '0.95rem', fontWeight: 600 };
const sectionHelpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const helpTextStyle: CSSProperties = {
  margin: '0.4rem 0 0',
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.4,
};
const fieldLabelStyle: CSSProperties = {
  display: 'block',
  fontSize: '0.75rem',
  color: 'var(--muted)',
  marginBottom: '0.35rem',
};
const inputStyle: CSSProperties = {
  width: '100%',
  padding: '0.5rem 0.75rem',
  background: 'var(--surface2)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.85rem',
  boxSizing: 'border-box',
};
const monoInputStyle: CSSProperties = { ...inputStyle, fontFamily: 'monospace' };
const actionBtnStyle: CSSProperties = {
  background: 'var(--accent)',
  color: '#000',
  fontWeight: 600,
  padding: '0.4rem 1rem',
  border: 'none',
  borderRadius: 4,
  fontSize: '0.8rem',
  cursor: 'pointer',
};
const dangerBtnStyle: CSSProperties = {
  ...actionBtnStyle,
  background: 'var(--red, #f87171)',
  color: '#000',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const rowStyle: CSSProperties = { display: 'flex', alignItems: 'center', gap: '0.75rem' };
const tableStyle: CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: '0.75rem',
  marginTop: '0.75rem',
};
const cellStyle: CSSProperties = {
  padding: '0.35rem 0.5rem',
  borderBottom: '1px solid var(--border)',
  textAlign: 'left',
};
const huskRowStyle: CSSProperties = {
  background: 'color-mix(in srgb, var(--red, #f87171) 15%, transparent)',
  color: 'var(--red, #f87171)',
};

const GREEN = 'var(--green, #4ade80)';
const RED = 'var(--red, #f87171)';

interface ColoredStatus {
  text: string;
  color?: string;
}

// ── reconcile report table (shared by import result, check, and repair) ──────

function ReportTable({ rows }: { rows: ReconcileRow[] }) {
  if (!rows.length) {
    return (
      <p id="snapshot-report-empty" style={helpTextStyle}>
        No onboarded systems in the graph.
      </p>
    );
  }
  return (
    <table id="snapshot-report-table" style={tableStyle}>
      <thead>
        <tr>
          <th style={cellStyle}>System</th>
          <th style={cellStyle}>Runtime</th>
          <th style={cellStyle}>Gateway target</th>
          <th style={cellStyle}>Healthy</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, i) => {
          const husk = row.healthy !== true;
          return (
            <tr
              key={row.system_id ?? i}
              data-husk={husk ? 'true' : 'false'}
              style={husk ? huskRowStyle : undefined}
            >
              <td style={cellStyle}>{row.system_id ?? '(unknown)'}</td>
              <td style={cellStyle}>{row.runtime_ok ? 'ok' : 'missing'}</td>
              <td style={cellStyle}>{row.target_ok ? 'ok' : 'missing'}</td>
              <td style={cellStyle}>{husk ? 'HUSK' : 'healthy'}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ── the section ───────────────────────────────────────────────────────────────

export default function SnapshotSection() {
  // Export
  const [exportBusy, setExportBusy] = useState(false);
  const [exportStatus, setExportStatus] = useState<ColoredStatus>({ text: '' });
  // Import
  const [parsed, setParsed] = useState<{ name: string; snapshot: GraphSnapshot } | null>(null);
  const [confirmTyped, setConfirmTyped] = useState('');
  const [importBusy, setImportBusy] = useState(false);
  const [importStatus, setImportStatus] = useState<ColoredStatus>({ text: '' });
  // Reconcile report — one table shared by import result, health check, repair.
  const [report, setReport] = useState<ReconcileRow[] | null>(null);
  const [reconcileStatus, setReconcileStatus] = useState<ColoredStatus>({ text: '' });
  const [checkBusy, setCheckBusy] = useState(false);
  const [repairBusy, setRepairBusy] = useState(false);

  const aliveRef = useRef(true);
  useEffect(
    () => () => {
      aliveRef.current = false;
    },
    [],
  );
  const alive = () => aliveRef.current;

  const armed = confirmTyped.trim() === CONFIRM_TOKEN;
  const hasHusk = !!report?.some((row) => row.healthy !== true);

  const runExport = async () => {
    setExportBusy(true);
    setExportStatus({ text: 'Starting export…' });
    try {
      const { nodeItems, edgeItems } = await walkExport((n, e) =>
        setExportStatus({ text: `Fetched ${n} nodes / ${e} edges…` }),
      );
      const snapshot = assembleSnapshot(nodeItems, edgeItems);
      downloadSnapshot(snapshot);
      setExportStatus({
        text: `✓ Exported ${snapshot.node_count} nodes / ${snapshot.edge_count} edges`,
        color: GREEN,
      });
    } catch (error) {
      setExportStatus({ text: 'Export failed: ' + errorDetail(error), color: RED });
    } finally {
      setExportBusy(false);
    }
  };

  const onFilePicked = (event: ChangeEvent<HTMLInputElement>) => {
    setParsed(null);
    setConfirmTyped('');
    setImportStatus({ text: '' });
    const file = event.target.files?.[0];
    if (!file) return;
    // FileReader (not Blob.text()) — the ImportPanel idiom; works everywhere.
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const text = typeof reader.result === 'string' ? reader.result : '';
        const snapshot = validateSnapshotShape(JSON.parse(text) as unknown);
        setParsed({ name: file.name, snapshot });
      } catch (error) {
        setImportStatus({ text: 'Invalid snapshot file: ' + errorDetail(error), color: RED });
      }
    };
    reader.onerror = () =>
      setImportStatus({ text: 'Could not read the selected file.', color: RED });
    reader.readAsText(file);
  };

  const runImport = async () => {
    if (!parsed || !armed) return;
    setImportBusy(true);
    setReport(null);
    setReconcileStatus({ text: '' });
    setImportStatus({ text: 'Starting import…' });
    try {
      const resp = await requestJson<{ status?: string }>(
        `${SNAPSHOT_URL}/import`,
        jsonInit({ confirm: CONFIRM_TOKEN, snapshot: parsed.snapshot }),
      );
      if (resp.status !== 'started' && resp.status !== 'already-running') {
        throw new Error(`import did not start: ${JSON.stringify(resp)}`);
      }
      const job = await pollJob<ImportJobResult>(`${SNAPSHOT_URL}/import/status`, alive, (s) =>
        setImportStatus({ text: `Import ${s}…` }),
      );
      if (job.state !== 'done') {
        throw new Error(job.error || `import job ended in state "${job.state}"`);
      }
      setReport(Array.isArray(job.result?.reconcile) ? job.result.reconcile : []);
      const reconcileNote = job.result?.reconcile_error
        ? ` (reconcile check failed: ${job.result.reconcile_error})`
        : '';
      setImportStatus({ text: '✓ Import complete' + reconcileNote, color: GREEN });
    } catch (error) {
      setImportStatus({ text: 'Import failed: ' + errorDetail(error), color: RED });
    } finally {
      setImportBusy(false);
    }
  };

  const runCheck = async () => {
    setCheckBusy(true);
    setReconcileStatus({ text: 'Checking system health…' });
    try {
      const resp = await requestJson<{ report?: ReconcileRow[] }>(
        `${SNAPSHOT_URL}/reconcile`,
        jsonInit({ repair: false }),
      );
      const rows = Array.isArray(resp.report) ? resp.report : [];
      setReport(rows);
      const husks = rows.filter((row) => row.healthy !== true).length;
      setReconcileStatus(
        husks
          ? { text: `${husks} system(s) need repair`, color: RED }
          : { text: '✓ All systems healthy', color: GREEN },
      );
    } catch (error) {
      setReconcileStatus({ text: 'Health check failed: ' + errorDetail(error), color: RED });
    } finally {
      setCheckBusy(false);
    }
  };

  const runRepair = async () => {
    setRepairBusy(true);
    setReconcileStatus({ text: 'Starting repair…' });
    try {
      const resp = await requestJson<{ status?: string; report?: ReconcileRow[] }>(
        `${SNAPSHOT_URL}/reconcile`,
        jsonInit({ repair: true }),
      );
      if (Array.isArray(resp.report)) setReport(resp.report);
      if (resp.status !== 'started' && resp.status !== 'already-running') {
        throw new Error(`repair did not start: ${JSON.stringify(resp)}`);
      }
      const job = await pollJob<RepairJobResult>(`${SNAPSHOT_URL}/reconcile/status`, alive, (s) =>
        setReconcileStatus({ text: `Repair ${s}…` }),
      );
      if (job.state !== 'done') {
        throw new Error(job.error || `repair job ended in state "${job.state}"`);
      }
      setReport(Array.isArray(job.result?.report_after) ? job.result.report_after : []);
      const repaired = job.result?.repaired ?? [];
      setReconcileStatus({
        text: repaired.length ? `✓ Repaired: ${repaired.join(', ')}` : '✓ Repair finished — nothing to repair',
        color: GREEN,
      });
    } catch (error) {
      setReconcileStatus({ text: 'Repair failed: ' + errorDetail(error), color: RED });
    } finally {
      setRepairBusy(false);
    }
  };

  return (
    <div>
      <h3 style={h3Style}>Graph Snapshot</h3>
      <p style={sectionHelpStyle}>
        Operational backup and restore of the entire knowledge graph as a JSON file
        (format <code>{SNAPSHOT_FORMAT}</code>). Export is read-only; import <strong>wipes the
        graph</strong> and restores it from the file, then checks each onboarded system against
        its live agent runtime and gateway target.
      </p>

      {/* EXPORT */}
      <div style={{ ...rowStyle, marginBottom: '0.25rem' }}>
        <button
          id="snapshot-export-btn"
          style={actionBtnStyle}
          disabled={exportBusy}
          onClick={() => void runExport()}
        >
          Export graph snapshot
        </button>
        <span id="snapshot-export-status" role="status" style={{ ...statusStyle, color: exportStatus.color ?? statusStyle.color }}>
          {exportStatus.text}
        </span>
      </div>
      <p style={helpTextStyle}>
        Walks the paginated snapshot API and downloads the assembled file.
      </p>

      {/* IMPORT */}
      <div style={{ marginTop: '1.5rem' }}>
        <label style={fieldLabelStyle} htmlFor="snapshot-import-file">
          Restore from snapshot file
        </label>
        <input
          id="snapshot-import-file"
          type="file"
          accept=".json,application/json"
          style={{ ...inputStyle, padding: '0.35rem 0.5rem' }}
          onChange={onFilePicked}
        />
        {parsed && (
          <p id="snapshot-import-counts" style={helpTextStyle}>
            {parsed.name}: {parsed.snapshot.node_count ?? parsed.snapshot.nodes.length} nodes /{' '}
            {parsed.snapshot.edge_count ?? parsed.snapshot.edges.length} edges
          </p>
        )}
        {parsed && (
          <div style={{ marginTop: '0.75rem' }}>
            <p style={{ ...helpTextStyle, margin: '0 0 0.35rem' }}>
              Importing <strong>wipes the entire graph</strong> and restores it from this file.
              Type <strong>{CONFIRM_TOKEN}</strong> to confirm:
            </p>
            <input
              id="snapshot-import-confirm"
              type="text"
              autoComplete="off"
              placeholder={CONFIRM_TOKEN}
              style={{ ...monoInputStyle, marginBottom: '0.5rem' }}
              value={confirmTyped}
              onChange={(e) => setConfirmTyped(e.target.value)}
            />
            <div style={rowStyle}>
              <button
                id="snapshot-import-btn"
                style={dangerBtnStyle}
                disabled={importBusy || !armed}
                onClick={() => void runImport()}
              >
                Import (wipe and restore)
              </button>
            </div>
          </div>
        )}
        <span id="snapshot-import-status" role="status" style={{ ...statusStyle, display: 'block', marginTop: '0.5rem', color: importStatus.color ?? statusStyle.color }}>
          {importStatus.text}
        </span>
      </div>

      {/* RECONCILE / REPAIR */}
      <div style={{ marginTop: '1.5rem' }}>
        <div style={rowStyle}>
          <button
            id="snapshot-reconcile-btn"
            style={actionBtnStyle}
            disabled={checkBusy || repairBusy}
            onClick={() => void runCheck()}
          >
            Check system health
          </button>
          {hasHusk && (
            <button
              id="snapshot-repair-btn"
              style={dangerBtnStyle}
              disabled={repairBusy || importBusy}
              onClick={() => void runRepair()}
            >
              Repair now
            </button>
          )}
          <span id="snapshot-reconcile-status" role="status" style={{ ...statusStyle, color: reconcileStatus.color ?? statusStyle.color }}>
            {reconcileStatus.text}
          </span>
        </div>
        <p style={helpTextStyle}>
          Compares graph-onboarded systems against live AgentCore runtimes and gateway targets.
          A HUSK row means the graph says onboarded but the runtime or target is gone — repair
          re-provisions it.
        </p>
        {report !== null && <ReportTable rows={report} />}
      </div>
    </div>
  );
}
