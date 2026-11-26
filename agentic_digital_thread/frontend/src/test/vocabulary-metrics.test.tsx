// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — the metric-definition admin surface (ADR-0065, Wave 4 task 4A):
 *
 *  1. the four row statuses (code / accepted / pending / shadowed) all render;
 *  2. a shadowed row shows the store's REASON verbatim;
 *  3. the propose form pre-checks locally, then surfaces the server's 400
 *     field message VERBATIM;
 *  4. the review-queue formula-diff expander renders the computed preview as
 *     structured elements (before/after formula + changed fields);
 *  5. ONE DOOR — the metrics panel exposes NO accept/reject affordance;
 *  6. no HTML-string sinks in the new sources (repo sink policy).
 *
 * All against a mocked fetch — no network. Generic metric/concept ids only. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
// Node's URL explicitly: under @vitest-environment jsdom the global URL is
// jsdom's implementation, which fileURLToPath does not accept.
import { fileURLToPath, URL as NodeURL } from 'node:url';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { MetricDefinitionPreview } from '../api/types/vocabulary';
import MetricsPanel from '../pages/admin/tabs/vocabulary/MetricsPanel';
import {
  metricDraftToInput,
  preflightMetricDraft,
  type MetricDraft,
} from '../pages/admin/tabs/vocabulary/MetricDefinitionModal';
import {
  familySummary,
  MetricDiffExpander,
  metricValueText,
} from '../views/architecture/proposal-families';
import { SlCardBody } from '../views/architecture/SlCards';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const METRICS_URL = '/api/admin/vocabulary/metrics';
const PROPOSE_URL = '/api/admin/vocabulary/metrics/propose';

/** One row per visible status, in the server's own ordering. */
const METRICS_BODY = {
  total: 4,
  metrics: [
    {
      id: 'alpha-rate',
      label: 'Alpha Rate',
      kind: 'direct',
      unit: 'pct',
      formula: 'avg(concept:alpha_reading)',
      definition: { id: 'alpha-rate', kind: 'direct' },
      source: 'code',
      status: 'code',
      editable: false,
    },
    {
      id: 'beta-ratio',
      label: 'Beta Ratio',
      kind: 'derived',
      unit: 'ratio',
      formula: 'ratio(alpha-rate, gamma-count)',
      definition: { id: 'beta-ratio', kind: 'derived' },
      source: 'graph',
      status: 'accepted',
      editable: true,
    },
    {
      id: 'gamma-count',
      label: 'Gamma Count',
      kind: 'direct',
      unit: '',
      formula: 'count(concept:gamma_event)',
      definition: { id: 'gamma-count', kind: 'direct' },
      source: 'user',
      status: 'pending',
      editable: true,
      mapping_id: 'map-gamma-1',
    },
    {
      id: 'delta-index',
      label: '',
      kind: '',
      unit: '',
      formula: '',
      definition: null,
      source: 'graph',
      status: 'shadowed',
      editable: false,
      reason: 'id collides with a code-defined metric',
    },
  ],
};

const PREVIEW: MetricDefinitionPreview = {
  current: { id: 'beta-ratio', kind: 'derived' },
  proposed: { id: 'beta-ratio', kind: 'derived' },
  diff: {
    is_new: false,
    added: { unit: 'ratio' },
    removed: {},
    changed: { label: { from: 'Beta Ratio', to: 'Beta Ratio v2' } },
    changed_fields: ['label', 'unit'],
    formula: {
      from: 'ratio(alpha-rate, gamma-count)',
      to: 'ratio(alpha-rate, delta-index)',
    },
  },
  validation: { valid: true, error: '' },
  collision: false,
};

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Bad Request',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

type FetchCall = { url: string; init?: RequestInit };

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const result = handler(url, init);
      if (!result) throw new Error(`unexpected URL: ${url} (${init?.method || 'GET'})`);
      return result;
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

async function render(element: ReactElement) {
  const client = createQueryClient();
  await act(async () => {
    root.render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
    await Promise.resolve();
  });
  return client;
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
const setTextareaValue = Object.getOwnPropertyDescriptor(
  HTMLTextAreaElement.prototype,
  'value',
)!.set!;

async function type(selector: string, value: string) {
  const field = container.querySelector(selector) as HTMLInputElement | HTMLTextAreaElement | null;
  expect(field, `missing field ${selector}`).not.toBeNull();
  await act(async () => {
    const setter = field instanceof HTMLTextAreaElement ? setTextareaValue : setInputValue;
    setter.call(field, value);
    (field as HTMLElement).dispatchEvent(new Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

/** Render the panel and wait for the metric table to arrive. The bare
 * vi.waitFor is the repo idiom for a TanStack query settling
 * (vocabulary-preview-retirement.test.tsx). */
async function renderMetricsPanel() {
  await render(<MetricsPanel />);
  await vi.waitFor(() => expect(container.querySelector('#vocab-metrics-table')).toBeTruthy());
}

function rowFor(metricId: string): HTMLTableRowElement {
  const row = container.querySelector(`tr[data-metric-id="${metricId}"]`);
  expect(row, `missing row for ${metricId}`).not.toBeNull();
  return row as HTMLTableRowElement;
}

describe('MetricsPanel — the four visible statuses', () => {
  it('renders one row per status with the status label', async () => {
    stubFetch((url) => (url === METRICS_URL ? response(METRICS_BODY) : undefined));
    await renderMetricsPanel();

    for (const [metricId, status] of [
      ['alpha-rate', 'code'],
      ['beta-ratio', 'accepted'],
      ['gamma-count', 'pending'],
      ['delta-index', 'shadowed'],
    ] as const) {
      const row = rowFor(metricId);
      expect(row.getAttribute('data-metric-status')).toBe(status);
      const badge = row.querySelector(`.vocab-metric-status--${status}`);
      expect(badge, `missing ${status} badge`).not.toBeNull();
      expect(badge?.textContent?.trim()).not.toBe('');
    }
    // The code row reads as read-only; the graph rows do not claim to be.
    expect(rowFor('alpha-rate').textContent).toContain('read-only');
    // ADR-0080: ONLY the pending row offers the read-only dry-run preview.
    expect(rowFor('gamma-count').querySelector('.sl-metric-dryrun-open')).not.toBeNull();
    for (const other of ['alpha-rate', 'beta-ratio', 'delta-index']) {
      expect(rowFor(other).querySelector('.sl-metric-dryrun-open')).toBeNull();
    }
    expect(rowFor('beta-ratio').textContent).not.toContain('read-only');
  });

  it('renders the shadowed reason verbatim', async () => {
    stubFetch((url) => (url === METRICS_URL ? response(METRICS_BODY) : undefined));
    await renderMetricsPanel();

    const reason = rowFor('delta-index').querySelector('.vocab-metric-reason');
    expect(reason).not.toBeNull();
    expect(reason?.textContent).toBe('id collides with a code-defined metric');
  });

  it('renders each row formula as text, and no accept/reject affordance exists', async () => {
    stubFetch((url) => (url === METRICS_URL ? response(METRICS_BODY) : undefined));
    await renderMetricsPanel();

    expect(rowFor('beta-ratio').querySelector('.vocab-metric-formula')?.textContent).toBe(
      'ratio(alpha-rate, gamma-count)',
    );

    // ONE DOOR: acceptance lives on the proposals queue. No control in this
    // panel may accept, reject, apply or approve anything.
    const controls = [...container.querySelectorAll('button, a, input[type="submit"]')];
    const offending = controls.filter((el) =>
      /accept|reject|approve|apply|deny/i.test(
        `${el.textContent ?? ''} ${el.className ?? ''} ${el.id ?? ''}`,
      ),
    );
    expect(offending.map((el) => el.textContent)).toEqual([]);
    expect(container.querySelector('.sl-accept')).toBeNull();
    expect(container.querySelector('.sl-reject')).toBeNull();
  });
});

describe('MetricDefinitionModal — client pre-check then the server verdict', () => {
  it('pre-checks the draft locally without a request', () => {
    const base: MetricDraft = {
      id: '',
      label: 'L',
      kind: 'direct',
      synonyms: '',
      unit: '',
      concept: 'alpha_reading',
      aggregation: 'avg',
      formula: '',
    };
    expect(preflightMetricDraft(base)).toBe('ID is required');
    expect(preflightMetricDraft({ ...base, id: 'Bad Id' })).toContain('lowercase');
    expect(preflightMetricDraft({ ...base, id: 'ok-id', label: '' })).toBe('Label is required');
    expect(preflightMetricDraft({ ...base, id: 'ok-id', concept: '' })).toContain('Concept is required');
    expect(preflightMetricDraft({ ...base, id: 'ok-id' })).toBe('');

    const derived: MetricDraft = { ...base, id: 'ok-id', kind: 'derived' };
    expect(preflightMetricDraft(derived)).toContain('Formula is required');
    expect(preflightMetricDraft({ ...derived, formula: '{not json' })).toContain('not valid JSON');
    expect(preflightMetricDraft({ ...derived, formula: '{"inputs": []}' })).toContain("missing an 'op'");
    expect(
      preflightMetricDraft({ ...derived, formula: '{"op": "ratio", "inputs": [{"metric": "a"}]}' }),
    ).toBe('');
  });

  it('omits blank optional fields and sends the kind-specific fields only', () => {
    const direct = metricDraftToInput({
      id: 'ok-id',
      label: ' Label ',
      kind: 'direct',
      synonyms: ' one , , two ',
      unit: '',
      concept: ' alpha_reading ',
      aggregation: 'avg',
      formula: '',
    });
    expect(direct).toEqual({
      id: 'ok-id',
      kind: 'direct',
      label: 'Label',
      synonyms: ['one', 'two'],
      concept: 'alpha_reading',
      aggregation: 'avg',
    });

    const derived = metricDraftToInput({
      id: 'ok-id',
      label: 'Label',
      kind: 'derived',
      synonyms: '',
      unit: 'pct',
      concept: 'ignored',
      aggregation: 'avg',
      formula: '{"op": "ratio", "inputs": [{"metric": "a"}, {"metric": "b"}]}',
    });
    expect(derived).toEqual({
      id: 'ok-id',
      kind: 'derived',
      label: 'Label',
      unit: 'pct',
      formula: { op: 'ratio', inputs: [{ metric: 'a' }, { metric: 'b' }] },
    });
  });

  it("surfaces the server's 400 field message verbatim", async () => {
    const serverMessage = "metric 'ok-id' field 'concept': unknown concept 'alpha_reading'";
    const calls = stubFetch((url, init) => {
      if (url === METRICS_URL) return response(METRICS_BODY);
      if (url === PROPOSE_URL && init?.method === 'POST') {
        return response({ error: serverMessage }, 400);
      }
      return undefined;
    });

    await renderMetricsPanel();
    await click(container.querySelector('#vocab-metric-propose-btn'));

    await type('#vocab-metric-id', 'ok-id');
    await type('#vocab-metric-label', 'Ok Label');
    await type('#vocab-metric-concept', 'alpha_reading');
    await click(container.querySelector('.vocab-metric-modal button[type="submit"]'));
    await settle();

    const alert = container.querySelector('.vocab-metric-error');
    expect(alert).not.toBeNull();
    expect(alert?.getAttribute('role')).toBe('alert');
    // VERBATIM — not reworded, not truncated, not replaced with a friendlier guess.
    expect(alert?.textContent).toBe(serverMessage);
    expect(calls.filter((call) => call.url === PROPOSE_URL)).toHaveLength(1);
  });

  it('blocks the request when the local pre-check fails', async () => {
    const calls = stubFetch((url) => (url === METRICS_URL ? response(METRICS_BODY) : undefined));
    await renderMetricsPanel();
    await click(container.querySelector('#vocab-metric-propose-btn'));
    await click(container.querySelector('.vocab-metric-modal button[type="submit"]'));
    await settle();

    expect(container.querySelector('.vocab-metric-error')?.textContent).toBe('ID is required');
    expect(calls.some((call) => call.url === PROPOSE_URL)).toBe(false);
  });

  it('reports the pending outcome and never offers to accept', async () => {
    stubFetch((url, init) => {
      if (url === METRICS_URL) return response(METRICS_BODY);
      if (url === PROPOSE_URL && init?.method === 'POST') {
        return response({ status: 'pending', mapping_id: 'map-new-1' });
      }
      return undefined;
    });
    await renderMetricsPanel();
    await click(container.querySelector('#vocab-metric-propose-btn'));
    await type('#vocab-metric-id', 'ok-id');
    await type('#vocab-metric-label', 'Ok Label');
    await type('#vocab-metric-concept', 'alpha_reading');
    await click(container.querySelector('.vocab-metric-modal button[type="submit"]'));
    await settle();

    const result = container.querySelector('.vocab-metric-result');
    expect(result?.textContent).toContain('pending');
    expect(container.querySelector('.vocab-metric-mapping-id')?.textContent).toContain('map-new-1');
    const buttons = [...container.querySelectorAll('.vocab-metric-modal button')];
    expect(buttons.filter((b) => /accept|reject/i.test(b.textContent ?? ''))).toEqual([]);
  });
});

describe('review-queue formula diff (preview route)', () => {
  it('labels a metric_definition family by its metric id', () => {
    expect(
      familySummary([
        {
          mapping_id: 'map-gamma-1',
          proposed_type: 'metric_definition',
          to_value: 'beta-ratio',
        } as never,
      ]),
    ).toBe('Metric Definition · beta-ratio');
  });

  it('renders values as text, never markup', () => {
    expect(metricValueText(null)).toBe('—');
    expect(metricValueText('')).toBe('—');
    expect(metricValueText(2)).toBe('2');
    expect(metricValueText(false)).toBe('false');
    expect(metricValueText({ op: 'ratio' })).toBe('{"op":"ratio"}');
  });

  it('renders the before/after formula and changed fields as structured elements', async () => {
    const onFetch = vi.fn(async () => PREVIEW);
    await render(<MetricDiffExpander mappingId="map-beta-1" onFetch={onFetch} />);

    const toggle = container.querySelector('.sl-metric-diff-toggle');
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    const regionId = toggle?.getAttribute('aria-controls') ?? '';
    expect(container.querySelector(`#${regionId}`)?.hasAttribute('hidden')).toBe(true);

    await click(toggle);
    await settle();

    expect(toggle?.getAttribute('aria-expanded')).toBe('true');
    expect(onFetch).toHaveBeenCalledWith('map-beta-1');
    expect(container.querySelector(`#${regionId}`)?.hasAttribute('hidden')).toBe(false);

    const from = container.querySelector('.sl-metric-formula-from code');
    const to = container.querySelector('.sl-metric-formula-to code');
    expect(from?.textContent).toBe('ratio(alpha-rate, gamma-count)');
    expect(to?.textContent).toBe('ratio(alpha-rate, delta-index)');

    const changed = container.querySelector('.sl-metric-field-changed');
    expect(changed?.textContent).toContain('label');
    expect(changed?.textContent).toContain('Beta Ratio');
    expect(changed?.textContent).toContain('Beta Ratio v2');
    expect(container.querySelector('.sl-metric-field-added')?.textContent).toContain('unit');
    expect(container.querySelector('.sl-metric-validation')?.textContent).toContain('Validates');

    // Collapsing again does not refetch — the preview is read once.
    await click(toggle);
    await click(toggle);
    await settle();
    expect(onFetch).toHaveBeenCalledTimes(1);
  });

  it('is_new renders "no current definition" instead of a before formula', async () => {
    const onFetch = vi.fn(async () => ({
      current: null,
      proposed: { id: 'new-metric' },
      diff: {
        is_new: true,
        added: { id: 'new-metric' },
        removed: {},
        changed: {},
        changed_fields: ['id'],
        formula: { from: null, to: 'sum(concept:alpha_reading)' },
      },
      validation: { valid: false, error: "field 'concept': unknown concept" },
      collision: true,
    }));
    await render(<MetricDiffExpander mappingId="map-new-1" onFetch={onFetch} />);
    await click(container.querySelector('.sl-metric-diff-toggle'));
    await settle();

    expect(container.querySelector('.sl-metric-formula-from em')?.textContent).toContain('new metric');
    expect(container.querySelector('.sl-metric-formula-from code')).toBeNull();
    expect(container.querySelector('.sl-metric-collision')?.getAttribute('role')).toBe('alert');
    const validation = container.querySelector('.sl-metric-validation');
    expect(validation?.getAttribute('role')).toBe('alert');
    expect(validation?.textContent).toContain("field 'concept': unknown concept");
  });

  it('a metric_definition proposal card body exposes the diff expander', async () => {
    stubFetch((url) =>
      url === '/api/admin/vocabulary/metrics/map-gamma-1/preview' ? response(PREVIEW) : undefined,
    );
    await render(
      <SlCardBody
        m={
          {
            mapping_id: 'map-gamma-1',
            proposed_type: 'metric_definition',
            to_value: 'gamma-count',
            status: 'pending',
          } as never
        }
      />,
    );
    expect(container.textContent).toContain('gamma-count');
    const toggle = container.querySelector('.sl-metric-diff-toggle');
    expect(toggle).not.toBeNull();

    await click(toggle);
    await settle();
    expect(container.querySelector('.sl-metric-formula-to code')?.textContent).toBe(
      'ratio(alpha-rate, delta-index)',
    );
  });

  it('reports an unavailable preview instead of accepting anything', async () => {
    const onFetch = vi.fn(async () => {
      throw new Error('boom');
    });
    await render(<MetricDiffExpander mappingId="map-beta-1" onFetch={onFetch} />);
    await click(container.querySelector('.sl-metric-diff-toggle'));
    await settle();
    const error = container.querySelector('.sl-metric-diff-error');
    expect(error?.getAttribute('role')).toBe('alert');
    expect(error?.textContent).toContain('Nothing was accepted');
  });
});

describe('sink policy — the new metric sources', () => {
  const SOURCES = [
    'pages/admin/tabs/vocabulary/MetricsPanel.tsx',
    'pages/admin/tabs/vocabulary/MetricDefinitionModal.tsx',
    'views/architecture/proposal-families.tsx',
  ];
  for (const rel of SOURCES) {
    it(`${rel} has no HTML-string sink`, () => {
      const source = readFileSync(
        fileURLToPath(new NodeURL(`../${rel}`, import.meta.url)),
        'utf-8',
      );
      for (const sink of [
        'dangerouslySetInnerHTML',
        'innerHTML',
        'insertAdjacentHTML',
        'outerHTML',
      ]) {
        expect(source, `${rel} must not use ${sink}`).not.toContain(sink);
      }
    });
  }
});
