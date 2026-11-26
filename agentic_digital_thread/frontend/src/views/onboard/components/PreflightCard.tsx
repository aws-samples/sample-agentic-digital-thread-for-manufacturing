// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <PreflightBody/> (E1, ADR-0053) — the preflight checkpoint card body:
 * verdict badge (ready=green / degraded=amber / blocked=red), the blocked
 * banner naming the probe reason enum + the docs reference, the connection
 * probe row, the per-table sample-read list (collapsed beyond 8 rows),
 * completeness counts, and warnings VERBATIM. Pure JSX over the pinned
 * PreflightResult contract — no HTML-string sinks. */
import type {
  PreflightResult,
  PreflightTableCheck,
} from '../../../api/types/onboard';

/** Repo documentation reference for blocked verdicts. The app serves no
 * documentation route, so this renders as a visible path, not a dead link. */
export const PREFLIGHT_DOCS_PATH = 'docs/cross-account-onboarding.md';

/** Table rows shown before the rest collapses into a <details> fold. */
export const PREFLIGHT_TABLE_FOLD = 8;

const VERDICT_META: Record<string, { label: string; color: string }> = {
  ready: { label: '✓ Ready', color: 'var(--green)' },
  degraded: { label: '⚠ Degraded', color: 'var(--amber)' },
  blocked: { label: '✗ Blocked', color: 'var(--red)' },
};

function VerdictBadge({ verdict }: { verdict?: string }) {
  const meta = VERDICT_META[verdict || ''] || {
    label: verdict || 'unknown',
    color: 'var(--muted)',
  };
  return (
    <span
      className="preflight-verdict"
      data-verdict={verdict || 'unknown'}
      style={{
        display: 'inline-block',
        padding: '0.15rem 0.6rem',
        borderRadius: 12,
        fontSize: '0.8rem',
        fontWeight: 600,
        color: meta.color,
        border: `1px solid ${meta.color}`,
        marginBottom: '0.5rem',
      }}
    >
      {meta.label}
    </span>
  );
}

/** Prominent honest banner for a blocked verdict — names the probe reason
 * ENUM verbatim, keeps the actionable detail, and points at the docs. */
function BlockedBanner({ result }: { result: PreflightResult }) {
  const conn = result.connection || {};
  return (
    <div
      className="preflight-blocked-banner"
      role="alert"
      style={{
        padding: '0.6rem 0.75rem',
        marginBottom: '0.5rem',
        borderRadius: 6,
        background: 'rgba(248,81,73,0.1)',
        border: '1px solid var(--red)',
        color: 'var(--red)',
        fontSize: '0.85rem',
      }}
    >
      <strong>Preflight blocked — {conn.reason || 'connection check failed'}.</strong>{' '}
      Discovery will not start until the connection is fixed.
      {conn.detail ? <div style={{ marginTop: '0.35rem' }}>{conn.detail}</div> : null}
      <div style={{ marginTop: '0.35rem' }}>
        See <code>{PREFLIGHT_DOCS_PATH}</code> in the repository documentation.
      </div>
    </div>
  );
}

function ConnectionRow({ result }: { result: PreflightResult }) {
  const conn = result.connection || {};
  const pass = conn.status === 'pass';
  return (
    <div className="preflight-connection" style={{ fontSize: '0.82rem', marginBottom: '0.4rem' }}>
      Connection:{' '}
      <span style={{ color: pass ? 'var(--green)' : 'var(--red)' }}>
        {pass ? '✓ pass' : '✗ fail'}
      </span>
      {typeof conn.latency_ms === 'number' ? ` · ${conn.latency_ms} ms` : ''}
      {!pass && conn.reason ? ` — ${conn.reason}` : ''}
      {!pass && conn.detail ? (
        <div style={{ color: 'var(--muted)', marginTop: '0.2rem' }}>{conn.detail}</div>
      ) : null}
    </div>
  );
}

function CompletenessRow({ result }: { result: PreflightResult }) {
  const c = result.completeness;
  if (!c) return null;
  const physical = String(c.physical_columns || '');
  return (
    <div className="preflight-completeness" style={{ fontSize: '0.8rem', color: 'var(--muted)', marginBottom: '0.4rem' }}>
      {c.tables_visible ?? 0} tables visible · {c.tables_sample_read ?? 0} sample-read ·{' '}
      {c.columns_discovered ?? 0} columns discovered
      {physical && physical !== 'n/a' ? (
        <span style={physical.startsWith('skipped') ? { color: 'var(--amber)' } : undefined}>
          {' '}
          · physical columns: {physical}
        </span>
      ) : null}
    </div>
  );
}

function TableRow({ t }: { t: PreflightTableCheck }) {
  const status = t.sample_read || 'skipped';
  const color =
    status === 'ok' ? 'var(--green)' : status === 'failed' ? 'var(--red)' : 'var(--muted)';
  const mark = status === 'ok' ? '✓' : status === 'failed' ? '✗' : '○';
  return (
    <tr className="preflight-table-row" data-sample-read={status}>
      <td>{t.name || ''}</td>
      <td style={{ color }}>
        {mark} {status}
        {t.detail ? <span style={{ color: 'var(--muted)' }}> — {t.detail}</span> : null}
      </td>
    </tr>
  );
}

/** Per-table sample-read list; rows beyond PREFLIGHT_TABLE_FOLD collapse into
 * a <details> fold so a wide source stays scannable. */
function TablesList({ tables }: { tables: PreflightTableCheck[] }) {
  if (tables.length === 0) return null;
  const shown = tables.slice(0, PREFLIGHT_TABLE_FOLD);
  const folded = tables.slice(PREFLIGHT_TABLE_FOLD);
  return (
    <>
      <table className="ob-table preflight-tables">
        <thead>
          <tr>
            <th>Table</th>
            <th>Sample read</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((t, i) => (
            <TableRow key={i} t={t} />
          ))}
        </tbody>
      </table>
      {folded.length > 0 && (
        <details className="preflight-tables-fold">
          <summary style={{ cursor: 'pointer', fontSize: '0.8rem', color: 'var(--muted)' }}>
            {folded.length} more tables
          </summary>
          <table className="ob-table preflight-tables">
            <tbody>
              {folded.map((t, i) => (
                <TableRow key={i} t={t} />
              ))}
            </tbody>
          </table>
        </details>
      )}
    </>
  );
}

function Warnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <ul className="preflight-warnings" style={{ margin: '0.4rem 0 0', paddingLeft: '1.1rem' }}>
      {warnings.map((w, i) => (
        <li key={i} style={{ color: 'var(--amber)', fontSize: '0.82rem' }}>
          ⚠ {w}
        </li>
      ))}
    </ul>
  );
}

export function PreflightBody({ result }: { result: PreflightResult }) {
  return (
    <div className="preflight-card">
      <VerdictBadge verdict={result.verdict} />
      {result.verdict === 'blocked' && <BlockedBanner result={result} />}
      <ConnectionRow result={result} />
      <CompletenessRow result={result} />
      <TablesList tables={result.tables || []} />
      <Warnings warnings={result.warnings || []} />
    </div>
  );
}
