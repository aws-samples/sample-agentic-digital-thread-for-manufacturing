// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Calendar rules section — the Settings-tab form for the calendar policy
 * (agents/calendar_policy_routes.py):
 *
 *  - two plain-language choices: what a date with no time means, and what
 *    today / yesterday / this week / the past N days mean (the UTC calendar
 *    day or the site's working day);
 *  - the time the site's working day starts;
 *  - each site's time zone (IANA name), with an optional day-start override.
 *
 * GET /api/admin/calendar-policy hydrates the form once; Save POSTs the whole
 * policy (a 400 lists every problem inline); "Remove calendar rules" POSTs
 * /clear, back to no policy. All transport goes through requestJson
 * (same-origin, cookie auth); every failure is surfaced inline. */
import { useEffect, useRef, useState, type CSSProperties } from 'react';

import { ApiError, requestJson } from '../../../../api/client';

const POLICY_URL = '/api/admin/calendar-policy';

export type CalendarRule = 'utc_day' | 'site_day';

export interface SiteZone {
  site: string;
  timezone: string;
  day_start?: string;
}

export interface CalendarPolicy {
  bare_date: CalendarRule;
  relative_days: CalendarRule;
  site_day_start: string;
  sites: SiteZone[];
  updated_by?: string;
  updated_at?: string;
}

interface PolicyRead {
  policy: CalendarPolicy | null;
}

export interface SiteRow {
  key: number;
  site: string;
  timezone: string;
  dayStart: string;
}

export const RULE_CHOICES: ReadonlyArray<{ value: CalendarRule; label: string }> = [
  { value: 'utc_day', label: 'the UTC calendar day' },
  { value: 'site_day', label: "the site's working day" },
];

/** The POST body for the form's current values (empty override dropped). */
export function policyBody(
  bareDate: CalendarRule,
  relativeDays: CalendarRule,
  dayStart: string,
  rows: SiteRow[],
): CalendarPolicy {
  return {
    bare_date: bareDate,
    relative_days: relativeDays,
    site_day_start: dayStart || '00:00',
    sites: rows
      .filter((row) => row.site.trim() || row.timezone.trim())
      .map((row) => ({
        site: row.site.trim(),
        timezone: row.timezone.trim(),
        ...(row.dayStart ? { day_start: row.dayStart } : {}),
      })),
  };
}

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
const labelStyle: CSSProperties = {
  display: 'block',
  fontSize: '0.75rem',
  color: 'var(--muted)',
  marginBottom: '0.35rem',
};
const helpStyle: CSSProperties = {
  fontSize: '0.72rem',
  color: 'var(--muted)',
  lineHeight: 1.5,
  marginBottom: '1rem',
};
const statusStyle: CSSProperties = { fontSize: '0.75rem', color: 'var(--muted)' };
const errorStyle: CSSProperties = { ...statusStyle, color: 'var(--red, #f87171)' };
const secondaryButtonStyle: CSSProperties = {
  background: 'var(--surface2)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  padding: '0.35rem 0.85rem',
  borderRadius: 4,
  fontSize: '0.78rem',
  cursor: 'pointer',
};

function errorList(error: unknown): string[] {
  if (error instanceof ApiError) {
    const payload = error.payload as { errors?: unknown } | undefined;
    if (payload && Array.isArray(payload.errors)) return payload.errors.map(String);
    return [error.detail];
  }
  return [error instanceof Error ? error.message : String(error)];
}

export default function CalendarPolicySection() {
  const [bareDate, setBareDate] = useState<CalendarRule>('utc_day');
  const [relativeDays, setRelativeDays] = useState<CalendarRule>('utc_day');
  const [dayStart, setDayStart] = useState('00:00');
  const [rows, setRows] = useState<SiteRow[]>([]);
  const [stored, setStored] = useState<CalendarPolicy | null>(null);
  const [status, setStatus] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const nextKey = useRef(0);

  const hydrate = (policy: CalendarPolicy | null) => {
    setStored(policy);
    setBareDate(policy?.bare_date ?? 'utc_day');
    setRelativeDays(policy?.relative_days ?? 'utc_day');
    setDayStart(policy?.site_day_start ?? '00:00');
    setRows(
      (policy?.sites ?? []).map((site) => ({
        key: nextKey.current++,
        site: site.site,
        timezone: site.timezone,
        dayStart: site.day_start ?? '',
      })),
    );
  };

  useEffect(() => {
    let alive = true;
    requestJson<PolicyRead>(POLICY_URL)
      .then((read) => {
        if (alive) hydrate(read.policy);
      })
      .catch((error: unknown) => {
        if (alive) setErrors(errorList(error));
      });
    return () => {
      alive = false;
    };
  }, []);

  const updateRow = (key: number, change: Partial<SiteRow>) =>
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...change } : row)));

  const save = async () => {
    setBusy(true);
    setErrors([]);
    setStatus('Saving…');
    try {
      const result = await requestJson<{ policy: CalendarPolicy }>(POLICY_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(policyBody(bareDate, relativeDays, dayStart, rows)),
      });
      hydrate(result.policy);
      setStatus('Saved. The assistant uses the new rules within a minute.');
    } catch (error) {
      setStatus('');
      setErrors(errorList(error));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setErrors([]);
    try {
      await requestJson(`${POLICY_URL}/clear`, { method: 'POST' });
      hydrate(null);
      setStatus('Calendar rules removed.');
    } catch (error) {
      setErrors(errorList(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div id="calendar-policy-section">
      <h3 style={{ margin: '0 0 0.5rem', fontSize: '0.95rem', fontWeight: 600 }}>Calendar rules</h3>
      <p style={helpStyle}>
        How the assistant reads the dates in a question. It works out the exact time span for
        each date before it looks anything up, so every answer uses the same span. Without these
        rules it decides each time.
      </p>
      <div style={{ marginBottom: '1rem' }}>
        <label htmlFor="calendar-bare-date" style={labelStyle}>
          When someone gives a date with no time, use:
        </label>
        <select
          id="calendar-bare-date"
          style={inputStyle}
          value={bareDate}
          onChange={(e) => setBareDate(e.target.value as CalendarRule)}
        >
          {RULE_CHOICES.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ marginBottom: '1rem' }}>
        <label htmlFor="calendar-relative-days" style={labelStyle}>
          For today, yesterday, this week and the past few days, use:
        </label>
        <select
          id="calendar-relative-days"
          style={inputStyle}
          value={relativeDays}
          onChange={(e) => setRelativeDays(e.target.value as CalendarRule)}
        >
          {RULE_CHOICES.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ marginBottom: '1rem' }}>
        <label htmlFor="calendar-day-start" style={labelStyle}>
          The site&apos;s working day starts at:
        </label>
        <input
          id="calendar-day-start"
          type="time"
          style={inputStyle}
          value={dayStart}
          onChange={(e) => setDayStart(e.target.value)}
        />
      </div>
      <fieldset style={{ border: 'none', padding: 0, margin: '0 0 1rem' }}>
        <legend style={labelStyle}>
          Each site&apos;s time zone (a name such as America/New_York or Europe/London). A
          working day needs at least one site.
        </legend>
        {rows.map((row, index) => (
          <div
            key={row.key}
            data-testid="calendar-site-row"
            style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr 0.8fr auto', gap: '0.5rem', marginBottom: '0.5rem' }}
          >
            <input
              aria-label={`Site ${index + 1} name`}
              style={inputStyle}
              placeholder="Site name"
              value={row.site}
              onChange={(e) => updateRow(row.key, { site: e.target.value })}
            />
            <input
              aria-label={`Site ${index + 1} time zone`}
              style={inputStyle}
              placeholder="Time zone"
              value={row.timezone}
              onChange={(e) => updateRow(row.key, { timezone: e.target.value })}
            />
            <input
              aria-label={`Site ${index + 1} working day start (optional)`}
              type="time"
              style={inputStyle}
              value={row.dayStart}
              onChange={(e) => updateRow(row.key, { dayStart: e.target.value })}
            />
            <button
              type="button"
              style={secondaryButtonStyle}
              aria-label={`Remove site ${index + 1}`}
              onClick={() => setRows((current) => current.filter((r) => r.key !== row.key))}
            >
              Remove
            </button>
          </div>
        ))}
        <button
          id="calendar-add-site"
          type="button"
          style={secondaryButtonStyle}
          onClick={() =>
            setRows((current) => [...current, { key: nextKey.current++, site: '', timezone: '', dayStart: '' }])
          }
        >
          Add site
        </button>
      </fieldset>
      {errors.length > 0 && (
        <ul id="calendar-policy-errors" role="alert" style={{ ...errorStyle, margin: '0 0 1rem', paddingLeft: '1.1rem' }}>
          {errors.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
        <button id="calendar-policy-save" className="btn-primary" disabled={busy} onClick={() => void save()}>
          Save calendar rules
        </button>
        {stored && (
          <button
            id="calendar-policy-clear"
            type="button"
            style={secondaryButtonStyle}
            disabled={busy}
            onClick={() => void clear()}
          >
            Remove calendar rules
          </button>
        )}
        <span id="calendar-policy-status" role="status" aria-live="polite" style={statusStyle}>
          {status}
        </span>
      </div>
      {stored?.updated_by && (
        <p id="calendar-policy-updated" style={{ ...helpStyle, margin: '0.6rem 0 0' }}>
          Last changed by {stored.updated_by}
          {stored.updated_at ? ` at ${stored.updated_at}` : ''}.
        </p>
      )}
    </div>
  );
}
