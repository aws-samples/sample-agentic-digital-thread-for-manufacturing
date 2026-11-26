// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests for the pure admin-page logic (pages/admin/tabs/admin-logic.ts,
 * Task 3.1). Node environment — no DOM. */
import { describe, expect, it } from 'vitest';

import {
  ADMIN_TAB_IDS,
  buildModelOptions,
  DEFAULT_ADMIN_TAB,
  dashboardKpiText,
  demoScriptErrorHtml,
  escapeHtml,
  feedbackRowsHtml,
  feedbackUrl,
  guardrailRowsHtml,
  modelValueToSave,
  NOT_OFFERED_NOTE,
  replaceDemoScriptCompany,
  restoreTabFromHash,
  truncate,
  workshopTabFallback,
} from '../pages/admin/tabs/admin-logic';

describe('escapeHtml (admin.js verbatim)', () => {
  it('escapes the five HTML special characters', () => {
    expect(escapeHtml(`<img src="x" onerror='p'> & more`)).toBe(
      '&lt;img src=&quot;x&quot; onerror=&#39;p&#39;&gt; &amp; more',
    );
  });
  it('stringifies non-strings', () => {
    expect(escapeHtml(42)).toBe('42');
  });
});

describe('truncate (admin.js verbatim)', () => {
  it('returns an em dash for empty/nullish', () => {
    expect(truncate('', 5)).toBe('—');
    expect(truncate(null, 5)).toBe('—');
    expect(truncate(undefined, 5)).toBe('—');
  });
  it('passes short strings through', () => {
    expect(truncate('abc', 5)).toBe('abc');
  });
  it('truncates long strings with a single-char ellipsis', () => {
    expect(truncate('abcdefgh', 5)).toBe('abcde…');
  });
});

describe('restoreTabFromHash (admin.js hash-restore IIFE)', () => {
  it('restores any known tab when demo is on and the workshop is live', () => {
    for (const id of ADMIN_TAB_IDS) {
      expect(restoreTabFromHash('#' + id, true, 'live')).toBe(id);
    }
  });
  it('skips tab-workshop unless its section is (or may be) rendered (gate)', () => {
    // Frozen/demo deploy, 403/404: no workshop section → the default tab stays.
    expect(restoreTabFromHash('#tab-workshop', true, 'hidden')).toBeNull();
    expect(restoreTabFromHash('#tab-workshop', true)).toBeNull();
    // Gate not answered yet: restore; workshopTabFallback leaves it if it settles hidden.
    expect(restoreTabFromHash('#tab-workshop', false, 'pending')).toBe('tab-workshop');
    expect(restoreTabFromHash('#tab-workshop', false, 'live')).toBe('tab-workshop');
    // The workshop gate never blocks another tab.
    expect(restoreTabFromHash('#tab-evals', false, 'hidden')).toBe('tab-evals');
  });
  it('accepts hashes without the # prefix', () => {
    expect(restoreTabFromHash('tab-settings', false)).toBe('tab-settings');
  });
  it('skips tab-demoscript when demo is off (gate)', () => {
    expect(restoreTabFromHash('#tab-demoscript', false)).toBeNull();
    expect(restoreTabFromHash('#tab-demoscript', true)).toBe('tab-demoscript');
  });
  it('activates tab-evals from its hash (Evals admin tab)', () => {
    expect(ADMIN_TAB_IDS).toContain('tab-evals');
    expect(restoreTabFromHash('#tab-evals', true)).toBe('tab-evals');
    // The demo gate applies ONLY to tab-demoscript — evals restores demo-off too.
    expect(restoreTabFromHash('#tab-evals', false)).toBe('tab-evals');
  });
  it('ignores unknown/empty hashes', () => {
    expect(restoreTabFromHash('', true)).toBeNull();
    expect(restoreTabFromHash('#', true)).toBeNull();
    expect(restoreTabFromHash('#tab-feedback', true)).toBeNull(); // no panel/button in the static markup
    expect(restoreTabFromHash('#nonsense', true)).toBeNull();
  });
});

describe('workshopTabFallback (#tab-workshop on a deploy without the section)', () => {
  it('falls back to the default tab only when tab-workshop is active and the gate settled hidden', () => {
    expect(DEFAULT_ADMIN_TAB).toBe('tab-agents');
    expect(workshopTabFallback('tab-workshop', 'hidden')).toBe('tab-agents');
    expect(workshopTabFallback('tab-workshop', 'pending')).toBeNull();
    expect(workshopTabFallback('tab-workshop', 'live')).toBeNull();
    expect(workshopTabFallback('tab-evals', 'hidden')).toBeNull();
  });
});

describe('feedbackUrl (loadFeedback)', () => {
  it("omits the query param for 'all'", () => {
    expect(feedbackUrl('all')).toBe('/api/admin/feedback');
  });
  it('appends ?sentiment= otherwise', () => {
    expect(feedbackUrl('positive')).toBe('/api/admin/feedback?sentiment=positive');
    expect(feedbackUrl('negative')).toBe('/api/admin/feedback?sentiment=negative');
  });
});

describe('feedbackRowsHtml (loadFeedback rows)', () => {
  it('renders the no-records row for an empty list', () => {
    expect(feedbackRowsHtml([])).toContain('No records');
  });
  it('renders sentiment badges and escapes/truncates fields', () => {
    const html = feedbackRowsHtml([
      {
        timestamp: '2026-07-13T10:00:00Z',
        user_id: 'u'.repeat(30),
        sentiment: 'positive',
        user_message: '<b>hi</b>',
        user_comment: '',
      },
      { sentiment: 'negative', user_message: 'meh' },
    ]);
    expect(html).toContain('badge pos');
    expect(html).toContain('badge neg');
    expect(html).toContain('&lt;b&gt;hi&lt;/b&gt;');
    expect(html).toContain('u'.repeat(20) + '…'); // user_id truncated at 20
    expect(html).toContain('<span style="color:var(--muted)">—</span>'); // empty comment
  });
});

describe('guardrailRowsHtml (loadGuardrails rows)', () => {
  it('renders the no-violations row for an empty list', () => {
    expect(guardrailRowsHtml([])).toContain('No violations');
  });
  it('renders source/action columns with em-dash fallbacks and escaping', () => {
    const html = guardrailRowsHtml([
      { user_id: 'user-1', source: '<input>', content_preview: 'x'.repeat(80) },
    ]);
    expect(html).toContain('&lt;input&gt;');
    expect(html).toContain('x'.repeat(70) + '…'); // content_preview truncated at 70
    expect(html).toContain('<td>—</td>'); // missing action
  });
});

describe('dashboardKpiText (loadDashboard)', () => {
  it('maps present values, rounding the average', () => {
    expect(
      dashboardKpiText({
        total_requests: 12,
        total_tokens: 3456,
        unique_users: 4,
        avg_tokens_per_request: 287.6,
        period_days: 30,
      }),
    ).toEqual({
      requests: '12',
      tokens: '3456',
      users: '4',
      avg: '288',
      period: 'Last 30 days',
    });
  });
  it('falls back to em dashes / empty period for missing values', () => {
    expect(dashboardKpiText({})).toEqual({
      requests: '—',
      tokens: '—',
      users: '—',
      avg: '—',
      period: '',
    });
  });
});

describe('buildModelOptions (_fillModelSelect + _selectModelValue)', () => {
  const catalog = [
    { id: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
    { id: 'us.amazon.nova-lite-v1:0', label: 'Nova Lite', available: false },
  ];
  it('annotates region-unavailable models but keeps them selectable', () => {
    const opts = buildModelOptions(catalog, '');
    expect(opts).toEqual([
      { value: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
      { value: 'us.amazon.nova-lite-v1:0', label: 'Nova Lite (not enabled in region)' },
    ]);
  });
  it('appends a "(current)" option when the saved value is not in the catalog', () => {
    const opts = buildModelOptions(catalog, 'eu.custom.model-v9');
    expect(opts[opts.length - 1]).toEqual({
      value: 'eu.custom.model-v9',
      label: 'eu.custom.model-v9 (current)',
    });
  });
  it('does not duplicate a current value the catalog already includes', () => {
    const opts = buildModelOptions(catalog, 'us.anthropic.claude-sonnet-4-6');
    expect(opts).toHaveLength(2);
  });
  it('renders the list-price hint when the entry carries pricing fields', () => {
    const priced = [
      {
        id: 'us.anthropic.claude-opus-4-8',
        label: 'Claude Opus 4.8',
        input_per_1k: 0.005,
        output_per_1k: 0.025,
      },
      {
        id: 'us.amazon.nova-lite-v1:0',
        label: 'Nova Lite v1',
        input_per_1k: 0.00006,
        output_per_1k: 0.00024,
        available: false,
      },
    ];
    const opts = buildModelOptions(priced, '');
    expect(opts[0].label).toBe('Claude Opus 4.8 · $5/$25 per MTok');
    // Availability annotation composes AFTER the price hint.
    expect(opts[1].label).toBe('Nova Lite v1 · $0.06/$0.24 per MTok (not enabled in region)');
  });
  it('omits the price hint when pricing fields are absent (absence ≠ free)', () => {
    const opts = buildModelOptions([{ id: 'x.model', label: 'Some Model' }], '');
    expect(opts[0].label).toBe('Some Model');
  });
});

describe('buildModelOptions under a deployment allowlist (ALLOWED_MODELS)', () => {
  const catalog = [
    { id: 'us.anthropic.claude-sonnet-5', label: 'Claude Sonnet 5', allowed: true },
    { id: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6', allowed: false },
    { id: 'us.amazon.nova-lite-v1:0', label: 'Nova Lite', available: false, allowed: true },
    { id: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro', available: false, allowed: false },
  ];
  it('greys out (never hides) models the deployment does not offer', () => {
    const opts = buildModelOptions(catalog, '', true);
    expect(opts).toEqual([
      { value: 'us.anthropic.claude-sonnet-5', label: 'Claude Sonnet 5' },
      {
        value: 'us.anthropic.claude-sonnet-4-6',
        label: 'Claude Sonnet 4.6' + NOT_OFFERED_NOTE,
        disabled: true,
      },
      // Allowed but not enabled in the region keeps today's annotation.
      { value: 'us.amazon.nova-lite-v1:0', label: 'Nova Lite (not enabled in region)' },
      { value: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro' + NOT_OFFERED_NOTE, disabled: true },
    ]);
    expect(NOT_OFFERED_NOTE).toBe(' (not offered in this deployment)');
  });
  it('keeps a disallowed configured model as the shown value, disabled', () => {
    const opts = buildModelOptions(catalog, 'us.anthropic.claude-sonnet-4-6', true);
    expect(opts).toHaveLength(4);
    expect(opts.find((o) => o.value === 'us.anthropic.claude-sonnet-4-6')?.disabled).toBe(true);
  });
  it('marks an off-catalog configured model as not offered when an allowlist is active', () => {
    const opts = buildModelOptions(catalog, 'eu.custom.model-v9', true);
    expect(opts[opts.length - 1]).toEqual({
      value: 'eu.custom.model-v9',
      label: 'eu.custom.model-v9 (current)' + NOT_OFFERED_NOTE,
      disabled: true,
    });
  });
  it('leaves everything selectable when no allowlist is configured', () => {
    const unrestricted = [
      { id: 'a', label: 'A', allowed: true },
      { id: 'b', label: 'B', allowed: true },
    ];
    const opts = buildModelOptions(unrestricted, 'eu.custom.model-v9', false);
    expect(opts.some((o) => o.disabled)).toBe(false);
    expect(opts[opts.length - 1].label).toBe('eu.custom.model-v9 (current)');
  });
  it('treats a catalog without allowlist fields (the unset server shape) as unrestricted', () => {
    // Unset ALLOWED_MODELS: no per-entry `allowed`, no `allowlist_active`.
    const unsetShape = [
      { id: 'a', label: 'A', available: true, default: true },
      { id: 'b', label: 'B', available: false, default: false },
    ];
    const opts = buildModelOptions(unsetShape, 'eu.custom.model-v9');
    expect(opts).toEqual([
      { value: 'a', label: 'A' },
      { value: 'b', label: 'B (not enabled in region)' },
      { value: 'eu.custom.model-v9', label: 'eu.custom.model-v9 (current)' },
    ]);
  });
});

describe('modelValueToSave', () => {
  const opts = [
    { value: 'ok', label: 'OK' },
    { value: 'no', label: 'No', disabled: true },
  ];
  it('saves a selectable value (trimmed) as is', () => {
    expect(modelValueToSave(opts, ' ok ')).toBe('ok');
    expect(modelValueToSave(opts, 'not-listed')).toBe('not-listed');
  });
  it('saves a disabled (not offered) value as "" so that role is left unchanged', () => {
    expect(modelValueToSave(opts, 'no')).toBe('');
  });
});

describe('demo-script helpers (loadDemoScript)', () => {
  it('replaces every placeholder company occurrence', () => {
    expect(
      replaceDemoScriptCompany(
        '# Agentic Digital Thread\nWelcome to Agentic Digital Thread.',
        'AnyCompany',
      ),
    ).toBe('# AnyCompany\nWelcome to AnyCompany.');
  });
  it('renders the friendly not-found message', () => {
    expect(demoScriptErrorHtml('Not found')).toContain('No demo script is configured');
  });
  it('renders the generic failure message otherwise', () => {
    expect(demoScriptErrorHtml('boom')).toContain('Failed to load demo script: boom');
  });
  it('escapes markup in the error message', () => {
    const html = demoScriptErrorHtml('<img src=x onerror=alert(1)>');
    expect(html).not.toContain('<img');
    expect(html).toContain('Failed to load demo script: &lt;img src=x onerror=alert(1)&gt;');
  });
});
