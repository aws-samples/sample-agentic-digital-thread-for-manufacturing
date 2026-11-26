// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Calendar rules section (Settings tab) against agents/calendar_policy_routes.py.
 *
 * Pins:
 * - the stored policy hydrates both plain-language choices, the day start and
 *   the site rows (mutation: ignoring the GET);
 * - Save POSTs the whole policy, drops an empty override and blank rows, and
 *   shows the saved state (mutation: wrong body shape);
 * - a 400 lists every problem the server named (mutation: swallowing errors);
 * - "Remove calendar rules" POSTs /clear and returns the form to no policy. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import CalendarPolicySection, { policyBody } from '../pages/admin/tabs/settings/CalendarPolicySection';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const STORED = {
  bare_date: 'utc_day',
  relative_days: 'site_day',
  site_day_start: '06:30',
  sites: [
    { site: 'site-a', timezone: 'America/New_York' },
    { site: 'site-b', timezone: 'Europe/London', day_start: '07:00' },
  ],
  updated_by: 'steward@example.com',
  updated_at: '2026-10-15T00:00:00+00:00',
};

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Bad Request',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

interface Call {
  url: string;
  method: string;
  body: unknown;
}

function stubFetch(handler: (url: string, method: string, body: unknown) => [unknown, number?]): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const body = init?.body ? (JSON.parse(String(init.body)) as unknown) : undefined;
      calls.push({ url, method, body });
      const [payload, status] = handler(url, method, body);
      return response(payload, status ?? 200);
    }),
  );
  return calls;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function render() {
  await act(async () => {
    root.render(<CalendarPolicySection />);
  });
  await flush();
}

async function click(id: string) {
  await act(async () => {
    document.getElementById(id)!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await flush();
}

const value = (id: string) => (document.getElementById(id) as HTMLInputElement).value;

describe('calendar rules section', () => {
  it('hydrates the form from the stored policy', async () => {
    stubFetch(() => [{ policy: STORED }]);
    await render();
    expect(value('calendar-bare-date')).toBe('utc_day');
    expect(value('calendar-relative-days')).toBe('site_day');
    expect(value('calendar-day-start')).toBe('06:30');
    const rows = container.querySelectorAll('[data-testid="calendar-site-row"]');
    expect(rows).toHaveLength(2);
    expect(container.textContent).toContain('When someone gives a date with no time, use:');
    expect(container.textContent).toContain("The site's working day starts at:");
    expect(container.textContent).toContain('Last changed by steward@example.com');
  });

  it('saves the whole policy and shows the saved state', async () => {
    const calls = stubFetch((_url, method, body) =>
      method === 'POST' ? [{ status: 'ok', policy: body }] : [{ policy: STORED }],
    );
    await render();
    await click('calendar-add-site');
    await click('calendar-policy-save');
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.url).toBe('/api/admin/calendar-policy');
    expect(post.body).toEqual({
      bare_date: 'utc_day',
      relative_days: 'site_day',
      site_day_start: '06:30',
      sites: STORED.sites,
    });
    expect(document.getElementById('calendar-policy-status')!.textContent).toContain('Saved');
  });

  it('lists every problem a 400 names', async () => {
    stubFetch((_url, method) =>
      method === 'POST'
        ? [{ error: 'invalid_policy', errors: ['bare_date must be one of utc_day, site_day', 'x'] }, 400]
        : [{ policy: null }],
    );
    await render();
    await click('calendar-policy-save');
    const items = document.querySelectorAll('#calendar-policy-errors li');
    expect(Array.from(items).map((li) => li.textContent)).toEqual([
      'bare_date must be one of utc_day, site_day',
      'x',
    ]);
  });

  it('removes the rules and returns to no policy', async () => {
    const calls = stubFetch((url) => (url.endsWith('/clear') ? [{ status: 'cleared' }] : [{ policy: STORED }]));
    await render();
    await click('calendar-policy-clear');
    expect(calls.some((c) => c.method === 'POST' && c.url === '/api/admin/calendar-policy/clear')).toBe(true);
    expect(value('calendar-relative-days')).toBe('utc_day');
    expect(container.querySelectorAll('[data-testid="calendar-site-row"]')).toHaveLength(0);
    expect(document.getElementById('calendar-policy-clear')).toBeNull();
  });
});

describe('policyBody', () => {
  it('drops blank rows and an empty override, and defaults the day start', () => {
    expect(
      policyBody('site_day', 'site_day', '', [
        { key: 1, site: ' site-a ', timezone: 'UTC', dayStart: '' },
        { key: 2, site: '', timezone: '', dayStart: '' },
      ]),
    ).toEqual({
      bare_date: 'site_day',
      relative_days: 'site_day',
      site_day_start: '00:00',
      sites: [{ site: 'site-a', timezone: 'UTC' }],
    });
  });
});
