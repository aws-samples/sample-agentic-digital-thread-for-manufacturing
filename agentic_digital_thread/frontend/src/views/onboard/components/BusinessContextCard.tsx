// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <BusinessContextCard/> — steward-editable per-system business context
 * (ADR-0054, contract C4a). GET on mount, PUT on save. The context text is
 * UNTRUSTED user-authored data: it renders ONLY as a controlled textarea
 * value (React text) — never markdown, never an HTML sink. The save line
 * reports the best-effort runtime refresh honestly (ok/failed/skipped);
 * pinned 400 reasons surface as readable errors via ApiError.detail. */
import { useEffect, useState } from 'react';

import { ApiError, requestJson } from '../../../api/client';
import type {
  BusinessContextResponse,
  BusinessContextSaveResponse,
} from '../../../api/types/onboard';
import { BusinessContextAssist } from './BusinessContextAssist';

export const BUSINESS_CONTEXT_MAX_LEN = 2000;

const GREEN = 'var(--green, #4ade80)';
const RED = 'var(--red, #f87171)';
const AMBER = 'var(--amber, #fbbf24)';
const MUTED = 'var(--muted)';

type ResultLine = { color: string; text: string };

/** T1.5 pre-write warning — a business-context save ROLLS the affected system
 * runtimes (secret-delivered payload), so the steward must see the cost
 * before the write is issued. The count comes from the REAL affected-system
 * set (the batched coordinator dedupes; this card affects exactly the one
 * system it edits, pack installs pass many) — never a hardcoded number. */
export function rollWarningText(affectedSystems: string[]): string {
  const n = affectedSystems.length;
  return (
    `Saving will restart ${n} system agent${n === 1 ? '' : 's'} (~2 min each); ` +
    'answers may briefly degrade while ' +
    `${n === 1 ? 'it restarts' : 'they restart'}.`
  );
}

/** Honest save line per the C4a `runtime_refresh` outcome. `failed` is a
 * visible warning: the durable value saved but the running agent did NOT
 * refresh (it picks the value up on its next restart/re-provision). When the
 * T1.5 coordinator's `readiness` block is present it refines the ok line —
 * READY alone is not proof the runtime serves traffic (ADR-0056), so the
 * verified/unverified/still-rolling distinction is surfaced honestly. */
export function saveResultLine(d: BusinessContextSaveResponse): ResultLine {
  if (d.status !== 'ok') return { color: RED, text: 'Save failed' };
  if (d.runtime_refresh === 'ok') {
    const entry = d.readiness ? Object.values(d.readiness)[0] : undefined;
    if (entry?.readiness === 'ready_verified') {
      return { color: GREEN, text: 'Saved ✓ — agent restarted and verified serving.' };
    }
    if (entry?.readiness === 'ready_unverified') {
      return {
        color: AMBER,
        text:
          'Saved ✓ — agent restarted (READY), but the warm-up check did not ' +
          'complete; the first answer may be slow.',
      };
    }
    if (entry?.readiness === 'not_ready') {
      return {
        color: AMBER,
        text:
          'Saved — agent restart still in progress (~2 min); answers may ' +
          'briefly degrade until it is back.',
      };
    }
    return { color: GREEN, text: 'Saved ✓ — agents refresh in ~1–2 min.' };
  }
  if (d.runtime_refresh === 'failed') {
    return {
      color: AMBER,
      text:
        'Saved, but the runtime refresh failed — the value is stored durably; ' +
        'the running agent was NOT updated and will pick it up on its next ' +
        'restart or re-provision.',
    };
  }
  return {
    color: MUTED,
    text: 'Saved. Runtime refresh skipped (no cloud agent runtime to update).',
  };
}

/** Readable error line for a failed PUT. The pinned C4a 400 reasons
 * (invalid_input | too_long | secret_like_content) arrive as the server's
 * `detail` string on ApiError; the secret-shaped rejection additionally
 * explains WHY secrets are banned from this field. */
export function saveErrorLine(e: unknown): ResultLine {
  if (e instanceof ApiError && e.status === 403) {
    return { color: RED, text: 'Not saved — admin only.' };
  }
  if (e instanceof ApiError && e.status === 404) {
    return { color: RED, text: 'Not saved — unknown system.' };
  }
  if (e instanceof ApiError && e.status === 400) {
    // The secret-shape detail is pinned server-side ("secret-shaped pattern");
    // spell out WHY it is rejected: this text is persisted to the graph and
    // rendered into agent prompts, so a pasted credential would be stored and
    // exposed — it must be removed (and rotated if it was real).
    const why = /secret[- ]shaped/i.test(e.detail)
      ? ' Business context is stored durably and rendered into agent prompts,' +
        ' so credentials, keys, or tokens pasted here would be persisted and' +
        ' exposed — remove them (and rotate any real secret).'
      : '';
    return { color: RED, text: 'Not saved — ' + e.detail + why };
  }
  return { color: RED, text: 'Not saved — ' + (e as Error).message };
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'note'; text: string }
  | { kind: 'ready' };

/** Steward business-context editor card (admin-gated server-side; a 403 on
 * the initial GET collapses the card to an "Admin only" note). */
export function BusinessContextCard({ systemId }: { systemId: string }) {
  const [state, setState] = useState<LoadState>({ kind: 'loading' });
  const [text, setText] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [contextHash, setContextHash] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<ResultLine | null>(null);

  // The REAL affected-system set for this write (T1.5 pre-write warning):
  // a per-system business-context edit rolls exactly this system's runtime.
  const affectedSystems = [systemId];

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const note = (t: string) => {
        if (!cancelled) setState({ kind: 'note', text: t });
      };
      try {
        const d = await requestJson<BusinessContextResponse>(
          '/api/admin/business-context/' + encodeURIComponent(systemId),
        );
        if (cancelled) return;
        setText(typeof d.business_context === 'string' ? d.business_context : '');
        setEnabled(!!d.enabled);
        setContextHash(d.context_hash ?? null);
        setState({ kind: 'ready' });
      } catch (e) {
        if (e instanceof ApiError && e.status === 403) note('Admin only');
        else if (e instanceof ApiError && e.status === 404) note('Unknown system');
        else note('Business context unavailable: ' + (e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [systemId]);

  const save = async () => {
    setConfirming(false);
    setSaving(true);
    setResult({ color: MUTED, text: 'Saving…' });
    try {
      const d = await requestJson<BusinessContextSaveResponse>(
        '/api/admin/business-context/' + encodeURIComponent(systemId),
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          // Pinned C4a body shape — exactly these two keys.
          body: JSON.stringify({ business_context: text, enabled }),
        },
      );
      setResult(saveResultLine(d));
      if (d.status === 'ok') setContextHash(d.context_hash ?? null);
    } catch (e) {
      setResult(saveErrorLine(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      data-testid="business-context-card"
      style={{
        border: '1px solid var(--border)',
        borderRadius: 6,
        padding: '0.75rem',
        marginBottom: '1.25rem',
      }}
    >
      <div
        style={{
          fontSize: '0.7rem',
          color: MUTED,
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          marginBottom: '0.35rem',
        }}
      >
        Business context
      </div>
      {state.kind === 'loading' && <span style={{ color: MUTED }}>Loading…</span>}
      {state.kind === 'note' && <span style={{ color: MUTED }}>{state.text}</span>}
      {state.kind === 'ready' && (
        <>
          <div style={{ fontSize: '0.75rem', color: MUTED, marginBottom: '0.35rem' }}>
            Free-text business meaning for this system, folded into its agent's
            prompt as untrusted reference data. Never paste credentials.
          </div>
          {/* Intake hints (docs/domain-context-intake.md): the questions
              whose answers measurably improve agent behavior. Generic wording
              ONLY — engine neutrality applies to UI strings too. */}
          <details style={{ marginBottom: '0.45rem' }}>
            <summary
              style={{ fontSize: '0.72rem', color: MUTED, cursor: 'pointer' }}
            >
              What makes good context? Eight things worth stating
            </summary>
            <ol
              style={{
                fontSize: '0.72rem',
                color: MUTED,
                margin: '0.35rem 0 0.2rem',
                paddingLeft: '1.2rem',
                lineHeight: 1.5,
              }}
            >
              <li>The unit of work — and common terms that do NOT apply here.</li>
              <li>
                What lifecycle states mean — especially whether retries close old
                records as failed/cancelled bookkeeping.
              </li>
              <li>Which failure or exception codes are real problems vs workflow noise.</li>
              <li>Identifier shapes, and how they relate to other systems' identifiers.</li>
              <li>Concepts this system IS (or is NOT) the authority for.</li>
              <li>What the data does NOT cover (sites, columns never populated).</li>
              <li>How to query it well (e.g. aggregates over row-by-row reads).</li>
              <li>
                How each KPI is calculated — numerator, the exact denominator
                basis, and which records are excluded.
              </li>
            </ol>
          </details>
          <textarea
            aria-label="Business context"
            value={text}
            maxLength={BUSINESS_CONTEXT_MAX_LEN}
            onChange={(e) => setText(e.target.value.slice(0, BUSINESS_CONTEXT_MAX_LEN))}
            rows={14}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              background: 'var(--surface)',
              color: 'var(--text)',
              border: '1px solid var(--border)',
              borderRadius: 4,
              padding: '0.4rem',
              fontSize: '0.8rem',
              fontFamily: 'inherit',
              resize: 'vertical',
            }}
          />
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              marginTop: '0.35rem',
              fontSize: '0.75rem',
            }}
          >
            <span data-testid="bc-char-count" style={{ color: MUTED }}>
              {text.length} / {BUSINESS_CONTEXT_MAX_LEN}
            </span>
            <label style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              Enabled
            </label>
            <button
              className="onboard-btn"
              style={{ fontSize: '0.75rem' }}
              disabled={saving || confirming}
              onClick={() => {
                setResult(null);
                setConfirming(true);
              }}
            >
              Save
            </button>
            {contextHash && (
              <span style={{ color: MUTED, fontFamily: "'IBM Plex Mono',monospace" }}>
                v:{contextHash}
              </span>
            )}
          </div>
          {/* OPT-IN AI assist (collapsed to one button by default). Accepting
              a draft only replaces the textarea VALUE — the steward can hand-
              edit, and saving stays the Save flow above with its roll warning
              and the PUT's secret-shape gate. */}
          <BusinessContextAssist
            systemId={systemId}
            currentText={text}
            onUseDraft={(draft) => {
              setText(draft.slice(0, BUSINESS_CONTEXT_MAX_LEN));
              setResult(null);
            }}
          />
          {confirming && (
            <div
              role="alertdialog"
              aria-label="Runtime restart warning"
              data-testid="bc-roll-warning"
              style={{
                marginTop: '0.4rem',
                padding: '0.5rem',
                border: `1px solid ${AMBER}`,
                borderRadius: 4,
                fontSize: '0.75rem',
                color: AMBER,
              }}
            >
              <div>{rollWarningText(affectedSystems)}</div>
              <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.4rem' }}>
                <button
                  className="onboard-btn"
                  style={{ fontSize: '0.75rem' }}
                  onClick={() => void save()}
                >
                  Save and restart
                </button>
                <button
                  className="onboard-btn"
                  style={{ fontSize: '0.75rem' }}
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          {result && (
            <div
              role="status"
              data-testid="bc-save-result"
              style={{ marginTop: '0.4rem', fontSize: '0.75rem', color: result.color }}
            >
              {result.text}
            </div>
          )}
        </>
      )}
    </div>
  );
}
