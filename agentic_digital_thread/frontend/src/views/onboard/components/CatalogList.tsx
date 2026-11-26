// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <CatalogList/> + <SysCard/> (jsx-migration W5.1) — the system catalog
 * rendered from the catalog store. e2e contracts preserved verbatim:
 * #card-<sys>, #btn-<sys>, the `onboarded` card class, and
 * .sys-card-disconnect-btn (test_onboarding.py). Admin-only controls:
 * Test/Retest, the inline Edit-connection form with the read-only masked
 * "Current connection" summary, and the Athena scan-budget editor.
 * Server-provided strings render as React text (the textContent XSS
 * discipline of the port); secrets are never fetched or rendered. */
import { useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, requestJson } from '../../../api/client';
import type { CatalogSystem, ConnField } from '../../../api/types/onboard';
import { useStore } from '../../../lib/store';
import { EmptyState, ErrorState, LoadingState, RefreshingOverlay } from '../../../shared/ui/AsyncStates';
import {
  obLoadCatalog,
  obTestConnection,
  showConnectNewForm,
  showSystemDetail,
  submitEditConnection,
} from '../catalog';
import {
  catalogStore,
  setConnValue,
  sysId,
  toggleConnOpen,
  toggleEditOpen,
  type CatalogState,
} from '../catalog-store';
import {
  obDisconnectSystem,
  obGuardStart,
  obIsOnboarding,
  obRemoveAgain,
  obShowActiveOnboarding,
  removingStore,
} from '../flow';
import {
  markRemovalNoticesAnnounced,
  pendingRemovalNotices,
  removalNoticeStore,
} from '../removal-store';
import { useCatalogQuery } from '../onboard-queries';
import { obCheckAdmin, secretArnSourceOffered } from '../state';
import {
  buildScanBudgetPayload,
  canBeSecretSourced,
  isSecretSourced,
  obEditConnFields,
  obFieldOptions,
  obPrefill,
} from '../payloads';
import { obTypedConnSummary } from '../util';
import { Dots } from './Checkpoint';

// Type icons (verbatim path data from the ported markup).
const ICONS: Record<string, React.ReactNode> = {
  ERP: (
    <svg className="sys-card-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <ellipse cx="10" cy="5" rx="7" ry="3" />
      <path d="M3 5v5c0 1.66 3.13 3 7 3s7-1.34 7-3V5" />
      <path d="M3 10v5c0 1.66 3.13 3 7 3s7-1.34 7-3v-5" />
    </svg>
  ),
  MES: (
    <svg className="sys-card-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="6" width="16" height="10" rx="1.5" />
      <path d="M6 6V4a2 2 0 014 0v2" />
      <circle cx="10" cy="11" r="2" />
    </svg>
  ),
  CMMS: (
    <svg className="sys-card-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l.77-.77a4 4 0 01-5.94 5.94l-4.91 4.91a1.5 1.5 0 01-2.12-2.12l4.91-4.91A4 4 0 0116.24 7.07l-.77.77a1 1 0 01-1.4 0l-1.6-1.6a1 1 0 000-1.4l.23-.24z" />
    </svg>
  ),
  PLM: (
    <svg className="sys-card-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M10 2l7 4v8l-7 4-7-4V6z" />
      <path d="M10 2v14M3 6l7 4 7-4" />
    </svg>
  ),
  IoT: (
    <svg className="sys-card-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M5 10a5 5 0 0110 0" />
      <path d="M2 10a8 8 0 0116 0" />
      <circle cx="10" cy="10" r="1.5" fill="currentColor" />
    </svg>
  ),
};
ICONS.Historian = ICONS.IoT;

const DEFAULT_ICON = (
  <svg className="sys-card-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
    <circle cx="10" cy="10" r="7" />
    <path d="M10 7v3l2 2" />
  </svg>
);

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '0.4rem',
  background: 'var(--bg)',
  border: '1px solid var(--border)',
  borderRadius: 4,
  color: 'var(--text)',
  fontSize: '0.8rem',
  boxSizing: 'border-box',
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: '0.7rem',
  color: 'var(--muted)',
  marginTop: '0.5rem',
  marginBottom: '0.15rem',
};

const blockStyle: React.CSSProperties = {
  paddingBottom: '0.5rem',
  marginBottom: '0.5rem',
  borderBottom: '1px solid var(--border)',
};

const blockTitleStyle: React.CSSProperties = {
  fontSize: '0.7rem',
  color: 'var(--muted)',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  marginBottom: '0.25rem',
};

// Admin-only: READ-ONLY masked summary of a system's CURRENT connection
// (host/port/scheme/base_url + a token presence indicator). Data comes from
// GET /api/admin/connection/{id}, masked/presence-only server-side — a secret
// VALUE is never fetched or rendered.
function CurrentConnection({ systemId }: { systemId: string }) {
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'note'; text: string }
    | {
        kind: 'ready';
        rows: [string, string][];
        hasSecret: boolean;
      }
  >({ kind: 'loading' });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const note = (text: string) => {
        if (!cancelled) setState({ kind: 'note', text });
      };
      try {
        const d = await requestJson<{
          host?: string;
          port?: number | string;
          scheme?: string;
          base_url?: string;
          workspace_id?: string;
          secret_arn?: string;
          credential_source?: string;
          has_secret?: boolean;
        }>('/api/admin/connection/' + encodeURIComponent(systemId));
        const rows: [string, string][] = [];
        const secretSourced = String(d.credential_source || '') === 'secret_arn';
        if (secretSourced) rows.push(['Credentials', 'Secrets Manager secret']);
        if (d.host) rows.push(['Host', String(d.host)]);
        if (d.port != null && d.port !== '') rows.push(['Port', String(d.port)]);
        if (d.scheme) rows.push(['Scheme', String(d.scheme)]);
        if (d.base_url) rows.push(['Base URL', String(d.base_url)]);
        if (d.workspace_id) rows.push(['Workspace ID', String(d.workspace_id)]);
        if (secretSourced && d.secret_arn) rows.push(['Secret ARN', String(d.secret_arn)]);
        if (!rows.length) rows.push(['Endpoint', '—']);
        if (!cancelled) setState({ kind: 'ready', rows, hasSecret: !!d.has_secret });
      } catch (e) {
        if (e instanceof ApiError && e.status === 403) note('Admin only');
        else if (e instanceof ApiError && e.status === 404) note('Unknown system');
        else note('Connection summary unavailable: ' + (e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [systemId]);

  return (
    <div style={blockStyle}>
      <div style={blockTitleStyle}>Current connection</div>
      <div style={{ fontSize: '0.78rem', color: 'var(--text)' }}>
        {state.kind === 'loading' && <Dots />}
        {state.kind === 'note' && (
          <span style={{ color: 'var(--muted)' }}>{state.text}</span>
        )}
        {state.kind === 'ready' && (
          <>
            {state.rows.map(([label, value]) => (
              <div key={label} style={{ display: 'flex', gap: '0.5rem', marginTop: '0.15rem' }}>
                <span style={{ color: 'var(--muted)', minWidth: '5.5rem', flex: '0 0 auto' }}>
                  {label}
                </span>
                <span style={{ wordBreak: 'break-all' }}>{value}</span>
              </div>
            ))}
            {/* Token/secret presence — a boolean indicator only, never a value. */}
            <div
              style={{
                marginTop: '0.35rem',
                fontSize: '0.75rem',
                color: state.hasSecret ? 'var(--green, #4ade80)' : 'var(--muted)',
              }}
            >
              {state.hasSecret ? 'Token configured ✓' : 'Token not set'}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// Per-system Athena scanned-bytes budget editor (admin-only; rendered only for
// Athena systems). GET shows the configured + effective budget; PUT sets it
// (blank/non-positive cleared client-side to null => fall back to env/default).
function ScanBudget({
  systemId,
  connectionType,
}: {
  systemId: string;
  connectionType?: string;
}) {
  const isAthena = /athena/i.test(connectionType || '');
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'note'; text: string }
    | { kind: 'ready'; effective: number | null; configured: number | null }
  >({ kind: 'loading' });
  const [value, setValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<{ color: string; text: string } | null>(null);

  useEffect(() => {
    if (!isAthena) return;
    let cancelled = false;
    (async () => {
      const note = (text: string) => {
        if (!cancelled) setState({ kind: 'note', text });
      };
      try {
        const d = await requestJson<{
          effective_max_scanned_bytes?: number | null;
          max_scanned_bytes?: number | null;
        }>('/api/admin/scan-budget/' + encodeURIComponent(systemId));
        if (!cancelled) {
          setState({
            kind: 'ready',
            effective: d.effective_max_scanned_bytes ?? null,
            configured: d.max_scanned_bytes ?? null,
          });
          if (d.max_scanned_bytes != null) setValue(String(d.max_scanned_bytes));
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 403) note('Admin only');
        else if (e instanceof ApiError && e.status === 404) note('Unknown system');
        else note('Scan budget unavailable: ' + (e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [systemId, isAthena]);

  if (!isAthena) return null; // budget is Athena-only

  const hintFor = (eff: number | null) =>
    eff == null
      ? 'No scan budget set — queries are not capped by the app (workgroup limits still apply).'
      : 'Effective cap: ' +
        String(eff) +
        ' bytes. A query scanning more is stopped with an actionable message.';

  const save = async () => {
    // Blank or non-positive => null (clear the per-system budget).
    const payload = buildScanBudgetPayload(value);
    setSaving(true);
    setResult({ color: 'var(--muted)', text: 'Saving…' });
    try {
      const pd = await requestJson<{
        status?: string;
        max_scanned_bytes?: number | null;
        detail?: string;
        message?: string;
      }>('/api/admin/scan-budget/' + encodeURIComponent(systemId), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (pd.status === 'ok') {
        setResult({
          color: 'var(--green, #4ade80)',
          text:
            pd.max_scanned_bytes == null
              ? 'Per-system budget cleared.'
              : 'Budget set to ' + String(pd.max_scanned_bytes) + ' bytes.',
        });
        setState({
          kind: 'ready',
          effective: pd.max_scanned_bytes ?? null,
          configured: pd.max_scanned_bytes ?? null,
        });
      } else {
        setResult({
          color: 'var(--red, #f87171)',
          text: pd.detail || pd.message || 'Save failed',
        });
      }
    } catch (err) {
      setResult({
        color: 'var(--red, #f87171)',
        text: 'Save failed: ' + (err as Error).message,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={blockStyle}>
      <div style={blockTitleStyle}>Athena scan budget</div>
      <div style={{ fontSize: '0.78rem', color: 'var(--text)' }}>
        {state.kind === 'loading' && <Dots />}
        {state.kind === 'note' && (
          <span style={{ color: 'var(--muted)' }}>{state.text}</span>
        )}
        {state.kind === 'ready' && (
          <>
            <div style={{ color: 'var(--muted)', marginBottom: '0.3rem' }}>
              {hintFor(state.effective)}
            </div>
            <div
              style={{
                display: 'flex',
                gap: '0.5rem',
                alignItems: 'center',
                marginTop: '0.25rem',
              }}
            >
              <input
                type="number"
                min={1}
                placeholder="bytes (blank = no per-system cap)"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                style={{ ...inputStyle, flex: 1, width: undefined }}
                onClick={(e) => e.stopPropagation()}
              />
              <button
                className="onboard-btn"
                style={{ fontSize: '0.75rem' }}
                disabled={saving}
                onClick={(e) => {
                  e.stopPropagation();
                  void save();
                }}
              >
                Save budget
              </button>
            </div>
            {result && (
              <span
                style={{
                  fontSize: '0.7rem',
                  display: 'block',
                  marginTop: '0.3rem',
                  color: result.color,
                }}
              >
                {result.text}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// Admin-only inline "Edit connection" form. Pre-filled non-secret values via
// controlled inputs (.value semantics — XSS-safe); the secret input is always
// blank ("leave blank to keep current"). A user MCP system also gets its
// "Authorization header" select (obEditConnFields), seeded from the server's
// stored scheme like every other non-secret field.
/** The secret-field keys GET /api/admin/connection/{id} reports as holding a
 * stored (non-empty) value. Anything else, or a malformed field, is unset. */
export function storedSecretKeys(secretFields: unknown): ReadonlySet<string> {
  if (!Array.isArray(secretFields)) return new Set();
  return new Set(secretFields.filter((k): k is string => typeof k === 'string' && k !== ''));
}

export function EditConnectionForm({ systemId, sys }: { systemId: string; sys: CatalogSystem }) {
  // The AUTHORITATIVE credential source comes from the current-connection GET
  // below; the catalog card's own marker may be stale (lost at provision or
  // restart). `undefined` = not yet known. A secret marker from EITHER source
  // picks the read-only form (fail toward read-only).
  const [credentialSource, setCredentialSource] = useState<string | undefined>(undefined);
  const [summaryFailed, setSummaryFailed] = useState(false);
  // A system that could take its credentials from a named secret shows no
  // editable field until the server has answered: the card says so, or the
  // deploy offers the secret source and this type supports it.
  const mayBeSecretSourced = canBeSecretSourced(
    sys,
    secretArnSourceOffered(String(sys.connection_type || '')),
  );
  const sourceUnknown = mayBeSecretSourced && credentialSource === undefined;
  const prefill = obPrefill(sys);
  const fields = useMemo(
    () => (sourceUnknown ? [] : obEditConnFields(sys, credentialSource)),
    [sys, credentialSource, sourceUnknown],
  );
  // Secret fields that hold a stored value, by key (GET's secret_fields). null
  // until the GET answers: a secret blob can exist with an empty client_id, so
  // the catalog's blanket has_secret flag must not badge every field.
  const [storedSecrets, setStoredSecrets] = useState<ReadonlySet<string> | null>(null);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const v: Record<string, string> = {};
    obEditConnFields(sys).forEach((f) => {
      v[f.key] = f.secret ? '' : prefill[f.key] || '';
    });
    return v;
  });
  // Seed the NON-SECRET fields from the node (the durable source) via the SAME
  // GET /api/admin/connection/{id} the "Current connection" panel reads. The
  // catalog obPrefill above is only a first paint / fallback — a stale or empty
  // catalog entry would otherwise leave Base URL / Plant Target blank even when
  // the node has them, and submitting a blank non-secret field OVERWRITES the
  // node with empty. Secret fields are never seeded (always "keep current").
  // The GET's credential_source is AUTHORITATIVE: it picks the read-only
  // secret-sourced form even when the catalog card lost the marker.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await requestJson<Record<string, unknown>>(
          '/api/admin/connection/' + encodeURIComponent(systemId),
        );
        if (cancelled) return;
        setStoredSecrets(storedSecretKeys(d.secret_fields));
        const source =
          String(d.credential_source || '') === 'secret_arn' || isSecretSourced(sys)
            ? 'secret_arn'
            : '';
        setCredentialSource(source);
        const effectiveFields = obEditConnFields(sys, source);
        setValues((prev) => {
          const next = { ...prev };
          effectiveFields.forEach((f) => {
            if (f.secret) return; // never prefill a secret value
            const v = d[f.key];
            if (v != null && v !== '' && !prev[f.key]) next[f.key] = String(v);
          });
          return next;
        });
      } catch {
        // Fall back to the catalog obPrefill already in state. A system that
        // may be secret-sourced stays locked: without the server's answer the
        // form can't tell which fields are safe to edit.
        if (!cancelled) setSummaryFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [systemId]);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<{ color: string; text: string } | null>(null);

  const save = async () => {
    setSaving(true);
    setResult({ color: 'var(--muted)', text: '…' });
    const r = await submitEditConnection(systemId, fields as ConnField[], values);
    setResult(r);
    setSaving(false);
  };

  return (
    <div
      style={{
        padding: '0.75rem',
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        marginTop: '0.5rem',
      }}
      // Form sits inside a (possibly clickable) onboarded card — don't bubble
      // to the card's showSystemDetail handler.
      onClick={(e) => e.stopPropagation()}
    >
      <CurrentConnection systemId={systemId} />
      <ScanBudget systemId={systemId} connectionType={sys.connection_type} />
      <div style={blockTitleStyle}>Edit connection</div>
      {sourceUnknown && (
        <div role="status" style={{ fontSize: '0.75rem', color: 'var(--muted)', marginBottom: '0.4rem' }}>
          {summaryFailed
            ? "Couldn't load the current connection, so it can't be edited right now. Close and reopen this form to try again."
            : "Checking where this connection's credentials come from…"}
        </div>
      )}
      {fields.some((f) => f.readOnly) && (
        <div style={{ fontSize: '0.75rem', color: 'var(--muted)', marginBottom: '0.4rem' }}>
          This connection's credentials come from a Secrets Manager secret. Its base URL
          and workspace are set by the secret and shown here read-only; to change them,
          update the secret.
        </div>
      )}
      {fields.map((f) => (
        <span key={f.key}>
          <label style={labelStyle}>
            {f.label}
            {f.secret && storedSecrets?.has(f.key) && (
              <span style={{ color: 'var(--green, #4ade80)', marginLeft: '0.4rem' }}>
                configured ✓
              </span>
            )}
          </label>
          {f.readOnly ? (
            <input
              style={inputStyle}
              data-field={f.key}
              type="text"
              readOnly
              aria-readonly="true"
              tabIndex={-1}
              value={values[f.key] || '—'}
            />
          ) : f.options ? (
            // A blank stored value means the first option (the server's
            // default), so the select shows it without sending it.
            <select
              style={inputStyle}
              data-field={f.key}
              value={(values[f.key] || '').trim() || f.options[0]}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
            >
              {obFieldOptions(f, values[f.key] || '').map((o) => (
                <option key={o.value} value={o.value} disabled={o.disabled}>
                  {o.label}
                </option>
              ))}
            </select>
          ) : (
            <input
              style={inputStyle}
              data-field={f.key}
              type={f.secret ? 'password' : 'text'}
              placeholder={f.secret ? 'leave blank to keep current' : f.placeholder}
              autoComplete={f.secret ? 'new-password' : undefined}
              value={values[f.key] || ''}
              onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
            />
          )}
        </span>
      ))}
      <div
        style={{
          marginTop: '0.75rem',
          display: 'flex',
          alignItems: 'center',
          gap: '0.5rem',
        }}
      >
        <button
          className="onboard-btn"
          style={{ fontSize: '0.75rem' }}
          disabled={saving || sourceUnknown}
          onClick={() => void save()}
        >
          {saving ? 'Saving…' : 'Save & Reconnect'}
        </button>
        <span
          className="sys-card-edit-result"
          style={{ fontSize: '0.7rem', color: result?.color }}
        >
          {saving ? <Dots /> : result?.text}
        </span>
      </div>
    </div>
  );
}

/** Move focus to a card's current primary action after "Remove again"
 * opened nothing and its button disappeared: Onboard on a registered card,
 * the card itself on an onboarded one (it opens the detail), else the first
 * enabled control, else the card. */
function focusCurrentAction(card: HTMLElement | null, sid: string): void {
  if (!card) return;
  const onboard = document.getElementById(`btn-${sid}`);
  if (onboard instanceof HTMLButtonElement && card.contains(onboard) && !onboard.disabled) {
    onboard.focus();
    return;
  }
  if (card.getAttribute('role') === 'button') {
    card.focus();
    return;
  }
  const first = Array.from(card.querySelectorAll<HTMLButtonElement>('button')).find(
    (b) => !b.disabled,
  );
  if (first) {
    first.focus();
    return;
  }
  card.tabIndex = -1;
  card.focus();
}

/** Card subtitle "TYPE · PLANT · CONNECTION", skipping blank segments so a
 * system without a plant does not read "CMMS · · REST API". */
export function sysCardMeta(s: Pick<CatalogSystem, 'system_type' | 'plant' | 'connection_type'>): string {
  return [s.system_type, s.plant, s.connection_type]
    .map((v) => String(v ?? '').trim())
    .filter(Boolean)
    .join('\u00a0 · \u00a0');
}

function SysCard({
  s,
  cat,
  isAdmin,
  onNavigate,
}: {
  s: CatalogSystem;
  cat: CatalogState;
  isAdmin: boolean;
  onNavigate?: () => void;
}) {
  const sid = sysId(s);
  const removingHere = useStore(removingStore, (st) => st.ids[sid]);
  const removingError = useStore(removingStore, (st) => st.errors[sid]);
  // A removal in flight: started from this tab (removingStore), or reported
  // by the server's catalog status `removing` (started from another tab, the
  // onboarding chat, or before this tab loaded). Either way the card offers
  // neither Remove nor Onboard until the catalog says the cascade ended.
  const removing = removingHere === 'running' || s.status === 'removing' ? 'running' : removingHere;
  // "Remove again" first re-reads the catalog (obRemoveAgain): `checking`
  // while it does. Its outcome when it opens nothing is said by the catalog's
  // live region (CatalogList), not here: an onboarded card is role="button".
  const [checking, setChecking] = useState(false);
  // A removal that started and did not finish (the server's removal_failed
  // state, which survives a restart, or this session's failed removal job):
  // the system is half-removed, so it opens no detail and offers no Test,
  // Edit or Onboard; its only action is to remove it again.
  const removalFailed =
    removing !== 'running' && (s.status === 'removal_failed' || removing === 'error');
  const done = s.status === 'onboarded' && !removalFailed;
  // Removals are serialized server-side (409); mirror that in the UI so the
  // steward never clicks into the rejection.
  const otherRemoving = useStore(removingStore, (st) =>
    Object.entries(st.ids).some(([id, v]) => id !== sid && v === 'running'),
  );
  const stype = s.system_type || '';
  const typedSummary = obTypedConnSummary(s);
  const connOpen = !!cat.connOpen[sid];
  const test = cat.testResults[sid];
  const editOpen = !!cat.editOpen[sid];
  const connId = 'conn-' + sid;
  const toggleId = 'conntoggle-' + sid;

  return (
    <div
      className={`sys-card${done ? ' onboarded' : ''}`}
      id={`card-${sid}`}
      data-type={stype}
      role={done ? 'button' : undefined}
      tabIndex={done ? 0 : undefined}
      style={done ? { cursor: 'pointer' } : undefined}
      onClick={
        done
          ? () => {
              void showSystemDetail(s);
              onNavigate?.();
            }
          : undefined
      }
      onKeyDown={
        done
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                void showSystemDetail(s);
                onNavigate?.();
              }
            }
          : undefined
      }
    >
      <div className="sys-card-header">
        {ICONS[stype] || DEFAULT_ICON}
        <div className="sys-card-name">{s.name}</div>
      </div>
      <div className="sys-card-meta">{sysCardMeta(s)}</div>
      {/* TYPED connectors (Athena / SiteWise (Direct)) carry their settings in
          structured connection_config — render a READ-ONLY non-secret summary
          (never external_id or secrets). Every other type keeps the editable
          connection_string input consumed by connection_resolver/obStart. */}
      <button
        className={`sys-card-conn-toggle${connOpen ? ' open' : ''}`}
        id={toggleId}
        onClick={(e) => {
          e.stopPropagation();
          toggleConnOpen(sid);
        }}
      >
        {typedSummary !== null ? 'connection' : 'connection string'}
      </button>
      {typedSummary !== null ? (
        <input
          className={`sys-card-conn${connOpen ? ' visible' : ''}`}
          id={connId}
          value={typedSummary}
          readOnly
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <input
          className={`sys-card-conn${connOpen ? ' visible' : ''}`}
          id={connId}
          value={cat.connValues[sid] ?? s.connection_string ?? ''}
          placeholder="connection string"
          onChange={(e) => setConnValue(sid, e.target.value)}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      <div className="sys-card-footer">
        {isAdmin && !removalFailed && (
          <button
            className="reset-btn sys-card-test-btn"
            style={{ fontSize: '0.7rem' }}
            disabled={test?.state === 'testing'}
            onClick={(e) => {
              e.stopPropagation();
              void obTestConnection(sid);
            }}
          >
            {test?.state === 'testing' ? 'Testing…' : test?.ran ? 'Retest' : 'Test'}
          </button>
        )}
        {isAdmin && !removalFailed && (
          <button
            className="reset-btn sys-card-edit-btn"
            style={{ fontSize: '0.7rem' }}
            onClick={(e) => {
              e.stopPropagation();
              toggleEditOpen(sid);
            }}
          >
            {editOpen ? 'Close' : 'Edit'}
          </button>
        )}
        {removing === 'running' ? (
          /* Removal cascade in flight (graph → runtime → catalog): keep BOTH
           * the remove and re-onboard actions off until the job confirms the
           * runtime + gateway target are gone — re-onboarding a still-DELETING
           * runtime strands a CREATE_FAILED husk. */
          <button
            className="reset-btn sys-card-disconnect-btn"
            style={{ fontSize: '0.7rem' }}
            disabled
          >
            Removing…
          </button>
        ) : removalFailed ? (
          <button
            className="reset-btn sys-card-disconnect-btn"
            style={{ fontSize: '0.7rem' }}
            disabled={otherRemoving}
            aria-disabled={checking || undefined}
            title={
              otherRemoving
                ? 'Another system is being removed — wait for it to finish'
                : 'Its earlier removal did not finish: remove it again to finish the removal'
            }
            onClick={(e) => {
              e.stopPropagation();
              // Acts on the system's current state, not this tab's copy:
              // obRemoveAgain reads the catalog from the server first and
              // opens the retry (in its recorded mode) only while the system
              // is still half-removed. Not disabled while it checks (a
              // disabled button can drop keyboard focus); a click then is
              // ignored.
              if (checking) return;
              setChecking(true);
              void obRemoveAgain(sid, s.name).finally(() => setChecking(false));
            }}
          >
            Remove again
          </button>
        ) : done ? (
          <button
            className="reset-btn sys-card-disconnect-btn"
            style={{ fontSize: '0.7rem' }}
            disabled={otherRemoving}
            title={
              otherRemoving
                ? 'Another system is being removed — wait for it to finish'
                : 'Remove this system: deletes its knowledge-graph data and agent; by default it returns to the catalog ready to onboard again'
            }
            onClick={(e) => {
              e.stopPropagation();
              obDisconnectSystem(sid, s.name, false, undefined, false,
                s.connection_origin === 'user');
            }}
          >
            Remove system
          </button>
        ) : (
          <>
            {obIsOnboarding(sid, !!cat.busy[sid]) ? (
              /* Its onboarding is running: the card says so and takes the
               * user back to that run's wizard (never a second start). */
              <button
                className="onboard-btn"
                id={`btn-${sid}`}
                title="Onboarding is in progress"
                onClick={(e) => {
                  e.stopPropagation();
                  obShowActiveOnboarding();
                  onNavigate?.();
                }}
              >
                Onboarding…
              </button>
            ) : (
              <button
                className="onboard-btn"
                id={`btn-${sid}`}
                onClick={(e) => {
                  e.stopPropagation();
                  obGuardStart(s);
                  onNavigate?.();
                }}
              >
                Onboard
              </button>
            )}
            {/* A user's own registration (never onboarded, or returned to
             * 'registered' by a default removal) can be FORGOTTEN: this removes
             * the catalog registration and schedules its own stored credentials
             * for deletion. Demo/workshop registrations (origin !== 'user') and
             * HighByte keep their shared secrets and offer no such action. */}
            {s.connection_origin === 'user' && sid !== 'highbyte' && (
              <button
                className="reset-btn sys-card-disconnect-btn"
                style={{ fontSize: '0.7rem' }}
                disabled={otherRemoving}
                aria-label={`Remove registration for ${s.name}`}
                title={
                  otherRemoving
                    ? 'Another system is being removed — wait for it to finish'
                    : 'Forget this registration: removes it from the catalog and schedules its stored credentials for deletion'
                }
                onClick={(e) => {
                  e.stopPropagation();
                  obDisconnectSystem(sid, s.name, true);
                }}
              >
                Remove registration
              </button>
            )}
          </>
        )}
        {removalFailed && (
          <span style={{ fontSize: '0.7rem', marginLeft: '0.5rem', color: 'var(--danger, #c0392b)' }}>
            Removal incomplete —{' '}
            {(removing === 'error' && removingError) ||
              'its earlier removal did not finish. Remove it again to finish.'}
          </span>
        )}
        {isAdmin && !removalFailed && (
          <span
            className="sys-card-test-result"
            id={`test-result-${sid}`}
            style={{
              fontSize: '0.7rem',
              marginLeft: '0.5rem',
              color: test?.state === 'testing' ? 'var(--muted)' : test?.color,
            }}
          >
            {test?.state === 'testing' ? <Dots /> : test?.state === 'done' ? test.text : null}
          </span>
        )}
      </div>
      {isAdmin && !removalFailed && (
        <div
          className="sys-card-edit"
          id={`edit-form-${sid}`}
          style={{ display: editOpen ? '' : 'none' }}
        >
          {editOpen && <EditConnectionForm systemId={sid} sys={s} />}
        </div>
      )}
    </div>
  );
}

const sectionHeader: React.CSSProperties = {
  fontSize: '0.7rem',
  color: 'var(--muted)',
  textTransform: 'uppercase',
  letterSpacing: '0.08em',
  padding: '0.5rem 0 0.25rem',
};

/** How long, and how many, earlier "Remove again" outcomes the catalog's live
 * region keeps next to a new one: long enough that a screen reader still
 * reads an outcome that a moment-later one would otherwise replace. */
const NOTICE_KEEP_MS = 15_000;
const NOTICE_KEEP_MAX = 2;

export function CatalogList({ onNavigate }: { onNavigate?: () => void } = {}) {
  const cat = useStore(catalogStore);
  const catalog = useCatalogQuery();
  const [isAdmin, setIsAdmin] = useState(false);
  // "Remove again" outcomes (removalNoticeStore) are said by ONE live region
  // here, outside every card (an onboarded card is role="button", whose
  // content is not exposed as a live region). It is always mounted with the
  // list and its text is set after mount. Every unsaid notice is said (two
  // cards can finish their checks in the same tick), and each is marked said
  // only once its text is in the region, so none is lost and a catalog mounted
  // later does not repeat one. A new outcome is added next to the few that
  // were said moments before it (see NOTICE_KEEP_MS / NOTICE_KEEP_MAX). Text
  // stays visible while the store keeps the notice (until the state it
  // describes changes or a new action on the system starts) and until newer
  // outcomes push it out. Focus moves only after the text is in the region.
  const notices = useStore(removalNoticeStore);
  const pending = pendingRemovalNotices(notices);
  const statusRef = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState<
    Array<{ sid: string; seq: number; text: string; focus: boolean; at: number }>
  >([]);
  // The region is rendered only once the catalog has loaded (or failed with
  // data in hand); nothing is taken from the store before then.
  const regionMounted = !catalog.isPending && !(catalog.isError && catalog.data === undefined);
  const pendingKey = pending.map(([, n]) => n.seq).join(',');
  useEffect(() => {
    if (!regionMounted || pending.length === 0) return;
    const at = Date.now();
    const fresh = pending.map(([sid, n]) => ({
      sid, seq: n.seq, text: n.text, focus: n.focus, at,
    }));
    const current = removalNoticeStore.get();
    // A new outcome is added next to what the region said moments ago (so a
    // screen reader that has not read it yet does not lose it), but only the
    // few most recent earlier outcomes, and only for a short while: the region
    // says what just happened, it is not a history.
    const kept = shown
      .filter(
        (e) =>
          current[e.sid]?.seq === e.seq &&
          !fresh.some((f) => f.sid === e.sid) &&
          at - e.at < NOTICE_KEEP_MS,
      )
      .slice(-NOTICE_KEEP_MAX);
    setShown([...kept, ...fresh]);
    markRemovalNoticesAnnounced(pending.map(([sid, n]) => [sid, n.seq]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingKey, regionMounted]);
  const focusTarget = [...shown].reverse().find((e) => e.focus);
  const newestShown = shown.length ? shown[shown.length - 1].seq : 0;
  useEffect(() => {
    if (!focusTarget) return;
    const card = document.getElementById(`card-${focusTarget.sid}`);
    if (card) focusCurrentAction(card, focusTarget.sid);
    else statusRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newestShown]);
  const regionText = shown
    .filter((e) => notices[e.sid]?.seq === e.seq)
    .map((e) => e.text)
    .join(' ');

  useEffect(() => {
    let mounted = true;
    void obCheckAdmin().then((admin) => {
      if (mounted) setIsAdmin(admin);
    });
    return () => {
      mounted = false;
    };
  }, []);

  if (catalog.isPending) {
    return <LoadingState label="Loading systems" className="ob-loading-catalog" />;
  }
  if (catalog.isError && catalog.data === undefined) {
    return (
      <ErrorState
        message={`Failed to load catalog: ${catalog.error.message}`}
        onRetry={() => void catalog.refetch()}
        className="ob-empty"
      />
    );
  }

  const systems = catalog.data || [];
  // A system whose removal is running (server status `removing`) or did not
  // finish stays with the onboarded ones (its card offers Removing… or only
  // Remove again); it is not available to onboard.
  const inFlight = (s: CatalogSystem) =>
    s.status === 'onboarded' || s.status === 'removing' || s.status === 'removal_failed';
  const connected = systems.filter(inFlight);
  const registered = systems.filter((s) => !inFlight(s));

  return (
    <>
      <button
        type="button"
        className="system-card connect-new"
        onClick={() => {
          showConnectNewForm();
          onNavigate?.();
        }}
        style={{
          width: '100%',
          border: '2px dashed var(--accent)',
          cursor: 'pointer',
          textAlign: 'center',
          padding: '1.25rem',
          marginBottom: '0.75rem',
          borderRadius: 8,
          background: 'var(--surface)',
          color: 'inherit',
          font: 'inherit',
        }}
      >
        <div style={{ fontSize: '1.5rem', marginBottom: '0.25rem' }}>+</div>
        <div style={{ fontWeight: 600, color: 'var(--accent)', fontSize: '0.85rem' }}>
          Connect New System
        </div>
        <div style={{ color: 'var(--muted)', fontSize: '0.7rem', marginTop: '0.25rem' }}>
          {/* Tulip added as a first-class connection type */}
          PostgreSQL · REST API · MCP Server · Amazon Athena · AWS IoT SiteWise
          (Direct / i3X) · Tulip (Tables API)
        </div>
      </button>
      <div
        ref={statusRef}
        role="status"
        id="ob-catalog-status"
        tabIndex={-1}
        style={{ fontSize: '0.7rem' }}
      >
        {regionText}
      </div>
      {catalog.isFetching && (
        <RefreshingOverlay label="Refreshing systems" className="ob-loading-catalog" />
      )}
      {catalog.isError && (
        <ErrorState
          message={`Failed to refresh catalog: ${catalog.error.message}`}
          onRetry={() => void catalog.refetch()}
          compact
          className="ob-empty"
        />
      )}
      {systems.length === 0 && (
        <EmptyState
          message="No systems are registered yet."
          actionLabel="Connect a system"
          onAction={() => {
            showConnectNewForm();
            onNavigate?.();
          }}
          className="ob-empty"
        />
      )}
      {connected.length > 0 && (
        <>
          <div style={{ ...sectionHeader, marginTop: '0.5rem' }}>Onboarded</div>
          {connected.map((s) => (
            <SysCard key={sysId(s)} s={s} cat={cat} isAdmin={isAdmin} onNavigate={onNavigate} />
          ))}
        </>
      )}
      {registered.length > 0 && (
        <>
          <div
            style={{
              ...sectionHeader,
              ...(connected.length
                ? { marginTop: '0.75rem', borderTop: '1px solid var(--border)' }
                : {}),
            }}
          >
            Registered
          </div>
          {registered.map((s) => (
            <SysCard key={sysId(s)} s={s} cat={cat} isAdmin={isAdmin} onNavigate={onNavigate} />
          ))}
        </>
      )}
    </>
  );
}

/** Retry helper for the error state (parity with the old manual reload). */
export function reloadCatalog(): void {
  void obLoadCatalog();
}
