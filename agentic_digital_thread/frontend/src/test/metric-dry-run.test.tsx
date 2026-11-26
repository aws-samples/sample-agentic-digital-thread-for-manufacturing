// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — ADR-0080 D5: the dry-run preview of a PENDING metric definition.
 *
 *  1. "Preview value" lazily opens a frame labelled "Preview — not accepted"
 *     with computed_at + window, rendering the dashboard's OWN KpiCard;
 *  2. companion ids ride `with=`; the fetch helper builds resolve=1&with=…;
 *  3. honesty statuses surface verbatim (no-data / unresolved-input / …) and
 *     the cost line is measured-only (counts + reported bytes, no dollars);
 *  4. close discards, re-open and Recompute refetch — nothing persisted;
 *  5. a failed preview is an alert that says nothing was accepted;
 *  6. on a pending proposal card the accept action is UNCHANGED (one door);
 *  7. no HTML-string sinks in the new sources.
 * All against injected fetchers / a stubbed fetch — no network. */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { fileURLToPath, URL as NodeURL } from 'node:url';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ProposedMapping } from '../api/types/proposals';
import type { MetricDefinitionDryRun, MetricDefinitionPreview } from '../api/types/vocabulary';
import { fetchMetricDryRun } from '../pages/admin/metric-queries';
import { costText, previewCard } from '../views/architecture/MetricDryRunFrame';
import { MetricDryRunAction, parseCompanionIds } from '../views/architecture/metric-dry-run';
import { SlCard, SlCardBody } from '../views/architecture/SlCards';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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

function dryRun(over: Partial<MetricDefinitionDryRun> = {}): MetricDefinitionDryRun {
  return {
    preview: true,
    metric_id: 'widget-count',
    label: 'Widget Count',
    unit: 'widgets',
    status: 'ok',
    value: 42,
    sample_count: 42,
    system_id: 'sys-a',
    lineage: { kind: 'direct' },
    window: '24h',
    computed_at: '2026-09-25T12:00:00Z',
    cost: { query_count: 1, systems: [{ system_id: 'sys-a', query_count: 1, scanned_bytes: 2048 }] },
    ...over,
  };
}

function fetcher(block: MetricDefinitionDryRun | Error) {
  return vi.fn(async (): Promise<MetricDefinitionPreview> => {
    if (block instanceof Error) throw block;
    return { dry_run: block };
  });
}

async function render(el: React.ReactElement) {
  await act(async () => {
    root.render(el);
  });
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).click();
  });
}

async function openPreview() {
  await click(container.querySelector('.sl-metric-dryrun-open'));
  await vi.waitFor(() => expect(container.querySelector('.sl-metric-dryrun')).toBeTruthy());
  await vi.waitFor(() =>
    expect(container.querySelector('.sl-metric-dryrun-busy')).toBeNull(),
  );
}

const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;

describe('MetricDryRunAction — the preview frame', () => {
  it('renders the dashboard card inside a labelled preview frame', async () => {
    const onFetch = fetcher(dryRun());
    await render(<MetricDryRunAction mappingId="prop-1" onFetch={onFetch} />);
    expect(container.querySelector('.sl-metric-dryrun')).toBeNull(); // nothing until asked
    await openPreview();

    expect(onFetch).toHaveBeenCalledWith('prop-1', []);
    const frame = container.querySelector('.sl-metric-dryrun')!;
    expect(frame.getAttribute('aria-label')).toBe('Preview — not accepted');
    expect(frame.querySelector('.sl-metric-dryrun-label')?.textContent).toBe(
      'Preview — not accepted',
    );
    const meta = frame.querySelector('.sl-metric-dryrun-meta')?.textContent ?? '';
    expect(meta).toContain('2026-09-25T12:00:00Z');
    expect(meta).toContain('window: 24h');
    // The SAME KpiCard the dashboard renders.
    const card = frame.querySelector('#kpi-card-preview-widget-count');
    expect(card?.className).toContain('kpi-card');
    expect(card?.querySelector('.kpi-title')?.textContent).toBe('Widget Count');
    expect(card?.querySelector('.kpi-value')?.textContent).toContain('42');
    expect(frame.querySelector('.sl-metric-dryrun-cost')?.textContent).toBe(
      // System named through the shared resolver (no catalog here: the
      // id-derived fallback).
      'Measured cost: 1 value query (Sys A: 1, 2048 bytes scanned).',
    );
  });

  it('forwards companion ids and surfaces a named unresolved input', async () => {
    const onFetch = fetcher(
      dryRun({
        status: 'unresolved-input',
        value: null,
        detail: "input metric 'widget-total' resolves to neither a served metric",
        missing_inputs: ['widget-total'],
        cost: { query_count: 0, systems: [] },
      }),
    );
    await render(<MetricDryRunAction mappingId="prop-1" onFetch={onFetch} />);
    const input = container.querySelector<HTMLInputElement>('.sl-metric-dryrun-with')!;
    await act(async () => {
      setInputValue.call(input, ' prop-2, prop-2 ,prop-3');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await openPreview();

    expect(onFetch).toHaveBeenCalledWith('prop-1', ['prop-2', 'prop-3']);
    const status = container.querySelector('.sl-metric-dryrun-status');
    expect(status?.getAttribute('data-status')).toBe('unresolved-input');
    expect(status?.textContent).toContain('missing: widget-total');
    expect(container.querySelector('.diag-note')?.textContent).toContain(
      'Withheld — an input metric does not resolve',
    );
    expect(container.querySelector('.sl-metric-dryrun-cost')?.textContent).toBe(
      'Measured cost: nothing executed.',
    );
  });

  it('shows no-data honestly rather than as a failure', async () => {
    await render(
      <MetricDryRunAction
        mappingId="prop-1"
        onFetch={fetcher(dryRun({ status: 'no-data', value: null, sample_count: 0 }))}
      />,
    );
    await openPreview();
    expect(container.querySelector('.diag-note')?.textContent).toContain(
      'No data — the definition resolved but returned no samples',
    );
  });

  it('names an ambiguous binding and its candidates (ADR-0076)', async () => {
    const detail =
      "concept 'widget-kind' is bound to 2 distinct columns on system 'sys-a' " +
      "(widget_events: 'kind_code', 'parent.kind_code')";
    await render(
      <MetricDryRunAction
        mappingId="prop-1"
        onFetch={fetcher(
          dryRun({ status: 'ambiguous-binding', value: null, sample_count: null, detail }),
        )}
      />,
    );
    await openPreview();
    const note = container.querySelector('.diag-note')?.textContent ?? '';
    expect(note).toContain(
      'Withheld — the concept binds to several columns; a steward must choose one',
    );
    expect(note).toContain("'parent.kind_code'");
  });

  it('close discards; re-open and Recompute recompute', async () => {
    const onFetch = fetcher(dryRun());
    await render(<MetricDryRunAction mappingId="prop-1" onFetch={onFetch} />);
    await openPreview();
    await click(container.querySelector('.sl-metric-dryrun-recompute'));
    await vi.waitFor(() => expect(onFetch).toHaveBeenCalledTimes(2));
    await click(container.querySelector('.sl-metric-dryrun-close'));
    expect(container.querySelector('.sl-metric-dryrun')).toBeNull();
    await openPreview();
    expect(onFetch).toHaveBeenCalledTimes(3);
  });

  it('reports a failed preview without accepting anything', async () => {
    await render(<MetricDryRunAction mappingId="prop-1" onFetch={fetcher(new Error('boom'))} />);
    await openPreview();
    const alert = container.querySelector('.sl-metric-dryrun-error');
    expect(alert?.getAttribute('role')).toBe('alert');
    expect(alert?.textContent).toContain('boom');
    expect(alert?.textContent).toContain('Nothing was accepted');
  });
});

describe('dry-run helpers', () => {
  it('builds resolve=1 and with= on the preview URL', async () => {
    const calls: string[] = [];
    const inits: (RequestInit | undefined)[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(url);
        inits.push(init);
        return new Response(JSON.stringify({ dry_run: dryRun() }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }),
    );
    await fetchMetricDryRun('prop 1', ['a', 'b']);
    await fetchMetricDryRun('p2');
    expect(calls[0]).toBe('/api/admin/vocabulary/metrics/prop%201/preview?resolve=1&with=a%2Cb');
    expect(calls[1]).toBe('/api/admin/vocabulary/metrics/p2/preview?resolve=1');
    // The browser HTTP cache may never answer a dry run (recompute = re-resolve).
    expect(inits.map((i) => i?.cache)).toEqual(['no-store', 'no-store']);
  });

  it('cost text is measured-only and card mapping is presentation-only', () => {
    expect(costText({ query_count: 2, systems: [{ system_id: 's', query_count: 2 }] })).toBe(
      'Measured cost: 2 value queries (S: 2).',
    );
    expect(costText(undefined)).not.toMatch(/\$|usd|dollar|estimat/i);
    const card = previewCard(dryRun());
    expect(card).toMatchObject({ card_id: 'preview-widget-count', viz_type: 'kpi', drill: null });
    expect(parseCompanionIds(' a, ,b,a ')).toEqual(['a', 'b']);
  });
});

describe('pending metric proposal — one door', () => {
  const metricProposal: ProposedMapping = {
    mapping_id: 'prop-1',
    proposed_type: 'metric_definition',
    status: 'pending',
    to_value: 'widget-count',
  };

  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{}', { status: 404 })),
    );
  });

  it('offers the preview action on a pending proposal body only', async () => {
    await render(<SlCardBody m={metricProposal} />);
    expect(container.querySelector('.sl-metric-dryrun-open')?.textContent).toBe('Preview value');
    await render(<SlCardBody m={{ ...metricProposal, status: 'accepted' }} />);
    expect(container.querySelector('.sl-metric-dryrun-open')).toBeNull();
  });

  it('leaves the accept action unchanged and the preview never accepts', async () => {
    const onAccept = vi.fn();
    await render(<SlCard m={metricProposal} onAccept={onAccept} onReject={vi.fn()} />);
    const accept = container.querySelector<HTMLButtonElement>('.sl-accept');
    expect(accept).not.toBeNull();
    expect(accept?.disabled).toBe(false);
    const previewControls = [...container.querySelectorAll('.sl-metric-dryrun-action button')];
    expect(previewControls.map((b) => b.textContent)).toEqual(['Preview value']);
    expect(onAccept).not.toHaveBeenCalled();
  });
});

describe('sink policy — the dry-run sources', () => {
  for (const rel of ['views/architecture/MetricDryRunFrame.tsx', 'views/architecture/metric-dry-run.tsx']) {
    it(`${rel} has no HTML-string sink`, () => {
      const source = readFileSync(fileURLToPath(new NodeURL(`../${rel}`, import.meta.url)), 'utf-8');
      for (const sink of ['dangerouslySetInnerHTML', 'innerHTML', 'insertAdjacentHTML', 'outerHTML']) {
        expect(source, `${rel} must not use ${sink}`).not.toContain(sink);
      }
    });
  }
});
