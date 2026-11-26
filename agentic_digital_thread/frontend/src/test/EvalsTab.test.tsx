// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Evals admin tab: the pinned run-ALL-only invariant
 * (exactly one run affordance, NO subset-selection control), the askConfirm()
 * gate (the app dialog, driven through its real buttons), 409 detail
 * surfacing, active-run disable + progress, RunHistory +
 * RunDetail rendering across every verdict state (+ trend + run_path badge),
 * the sink-policy pin (hostile report strings stay inert literal text), and
 * the managed AgentCore Evaluations section off the fleet query — all against
 * a mocked fetch, no network. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { EvalRunDetail, EvalRunSummary } from '../api/types/admin';
import { startUploadedEvalRunMutationOptions } from '../pages/admin/admin-queries';
import EvalsTab from '../pages/admin/tabs/EvalsTab';
import { answerConfirm, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const RUNS: EvalRunSummary[] = [
  {
    run_id: 'run-2',
    started_at: '2026-08-09T12:00:00Z',
    finished_at: '2026-08-09T12:20:00Z',
    status: 'complete',
    run_path: 'server',
    variant: 'arm-b',
    suite: { pass: 1, fail: 1, skipped: 1, total: 3 },
    golden_set_sha: 'abc123def456',
  },
  {
    run_id: 'run-1',
    started_at: '2026-08-08T09:00:00Z',
    finished_at: null,
    status: 'failed',
    run_path: 'server',
    variant: null,
    suite: { pass: 0, fail: 0, skipped: 0, total: 0 },
    golden_set_sha: 'abc123def456',
  },
];

// Hostile strings — the sink-policy pin: report data renders as LITERAL text.
const HOSTILE_EXCERPT = '<img src=x onerror="window.__pwned=1"> **not markdown**';
const HOSTILE_FAILURE = '<script>window.__pwned2=1</script> missing required pattern';

const COMPLETE_DETAIL: EvalRunDetail = {
  status: 'complete',
  progress: { current: 3, total: 3 },
  report: {
    run_id: 'run-2',
    run_path: 'server',
    variant: 'arm-b',
    golden_set_sha: 'abc123def456',
    suite: { pass: 1, fail: 1, skipped: 1, total: 3 },
    questions: [
      {
        id: 'q-pass',
        session_id: 'eval-run-run-2-q1',
        verdict: 'pass',
        failures: [],
        systems_queried: 2,
        neptune_queries: 3,
        elapsed_seconds: 42.1,
        answer_excerpt: 'Batch B-2847 traces to…',
        honesty: { absence_markers_matched: [], fabrication_flags: [] },
        metrics: { total_tokens: 1200 },
      },
      {
        id: 'q-fail',
        session_id: 'eval-run-run-2-q2',
        verdict: 'fail',
        failures: [HOSTILE_FAILURE],
        systems_queried: 1,
        neptune_queries: 0,
        elapsed_seconds: 12.9,
        answer_excerpt: HOSTILE_EXCERPT,
        honesty: {
          absence_markers_matched: ['no data available'],
          fabrication_flags: ['quantified an absent metric'],
        },
        metrics: null,
      },
      {
        id: 'q-skip',
        session_id: null,
        verdict: 'skipped',
        skip_reason: 'requires_seed',
        failures: [],
        systems_queried: 0,
        neptune_queries: 0,
        elapsed_seconds: null,
        answer_excerpt: '',
        honesty: { absence_markers_matched: [], fabrication_flags: [] },
        metrics: null,
      },
    ],
    trend: {
      informational: true,
      comparable: true,
      questions: [
        {
          id: 'q-fail',
          status: 'compared',
          previous_verdict: 'pass',
          verdict: 'fail',
          transition: 'regression',
        },
      ],
      suite: { pass_previous: 2, pass_current: 1, regressions: 1, improvements: 0 },
    },
  },
};

const FLEET = {
  agents: [
    {
      display_name: 'Orchestrator',
      has_eval: true,
      eval_scores: {
        Correctness: { avg: 0.87, count: 42 },
        ToolSelectionAccuracy: { avg: 0.62, count: 10 },
      },
    },
    { name: 'erp-agent' },
  ],
};

function response(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    json: async () => body,
    text: async () => text,
  } as unknown as Response;
}

type FetchCall = { url: string; init?: RequestInit };

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const result = handler(url, init);
    if (result) return result;
    // The managed section's fleet query is always mounted — default it here
    // so per-test handlers stay focused on the surface under test.
    if (url.startsWith('/api/admin/agents') && (!init?.method || init.method === 'GET')) {
      return response({ agents: [] });
    }
    throw new Error(`unexpected URL: ${url} (${init?.method || 'GET'})`);
  }));
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

async function renderTab() {
  const client = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <EvalsTab />
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() => expect(container.querySelector('#evals-run-btn')).toBeTruthy());
  return client;
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;

async function type(el: HTMLInputElement, value: string) {
  await act(async () => {
    setInputValue.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

function runsHandler(runs: EvalRunSummary[]) {
  return (url: string, init?: RequestInit): Response | undefined => {
    if (url === '/api/admin/evals/runs' && (!init?.method || init.method === 'GET')) {
      return response({ runs });
    }
    return undefined;
  };
}

// ── Uploaded customer golden set (ADR-0064) ──────────────────────────────────
// The canary is the file's own question text: it must reach the /run-upload
// request body and NOTHING else — no other request, no web storage, no cache.
const CANARY = 'CANARY-CUSTOMER-QUESTION-TEXT';
const GOLDEN_TEXT = JSON.stringify({
  version: 1,
  questions: [{ id: 'cust-q1', text: CANARY, expect: { answer_must_match: ['x'] } }],
});

/** Attach a file to the picker (jsdom cannot set input.files directly) — the
 * evals-findings.test.tsx idiom. */
async function pickGoldenFile(text = GOLDEN_TEXT, name = 'my-goldens.json') {
  const input = container.querySelector<HTMLInputElement>('#evals-upload-file-input')!;
  const file = new File([text], name, { type: 'application/json' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** The request body as text — the route takes RAW bytes, never a JSON envelope. */
function bodyText(init: RequestInit | undefined): string {
  const body = init?.body;
  if (body instanceof ArrayBuffer) return new TextDecoder().decode(body);
  return typeof body === 'string' ? body : '';
}

function uploadHandler(status: number, payload: unknown) {
  return (url: string, init?: RequestInit): Response | undefined => {
    if (url.startsWith('/api/admin/evals/run-upload') && init?.method === 'POST') {
      return response(payload, status);
    }
    return undefined;
  };
}

/** Render, attach a golden file, confirm, and click "Run uploaded set". */
async function runUpload(
  handler: (url: string, init?: RequestInit) => Response | undefined,
  runs: EvalRunSummary[] = [],
) {
  const calls = stubFetch((url, init) => runsHandler(runs)(url, init) ?? handler(url, init));
  await renderTab();
  await pickGoldenFile();
  await click(container.querySelector('#evals-upload-run-btn'));
  await answerConfirm('confirm');
  return calls;
}

describe('run-ALL-only pin (UI side)', () => {
  it('offers ONE affordance per golden source and NO subset control on either', async () => {
    stubFetch(runsHandler([]));
    await renderTab();

    // The packaged demo suite: one confirm()-gated button.
    const formButtons = container.querySelectorAll('#evals-run-form button');
    expect(formButtons).toHaveLength(1);
    expect(formButtons[0].id).toBe('evals-run-btn');
    expect(formButtons[0].textContent).toContain('Run full golden suite');

    // An uploaded customer set: a SECOND affordance BESIDE it (ADR-0064 D3) —
    // the packaged button is still present and unchanged above.
    const uploadButtons = container.querySelectorAll('#evals-upload-run-form button');
    expect(uploadButtons).toHaveLength(1);
    expect(uploadButtons[0].id).toBe('evals-upload-run-btn');
    expect(uploadButtons[0].textContent).toContain('Run uploaded set');

    // NO subset-selection control of any kind, anywhere in the tab: no
    // dropdowns, checkboxes, radios, or numeric inputs (count/category/ids).
    expect(container.querySelectorAll('select')).toHaveLength(0);
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
    expect(container.querySelectorAll('input[type="radio"]')).toHaveLength(0);
    expect(container.querySelectorAll('input[type="number"]')).toHaveLength(0);

    // The ONLY inputs are the optional variant LABEL (a run tag, not a filter)
    // and the golden-set file picker — neither selects questions.
    const inputs = Array.from(container.querySelectorAll('input'));
    expect(inputs.map((input) => input.id)).toEqual([
      'evals-variant-input',
      'evals-upload-file-input',
    ]);
    expect(inputs[1].type).toBe('file');

    // No control names/ids ever mention subset keys.
    for (const el of Array.from(container.querySelectorAll('input, button, select, textarea'))) {
      const idName = `${el.id} ${(el as HTMLInputElement).name ?? ''}`;
      expect(idName).not.toMatch(/count|category|question[-_]?ids?\b/i);
    }
  });

  it('renders the honest requires_seed copy for unseeded environments', async () => {
    stubFetch(runsHandler([]));
    await renderTab();
    expect(container.textContent).toContain('requires_seed');
    expect(container.textContent).toContain('honestly FAIL');
  });
});

describe('starting a run', () => {
  it('is confirm-gated: a declined confirm sends NO request', async () => {
    const calls = stubFetch(runsHandler([]));
    await renderTab();

    await click(container.querySelector('#evals-run-btn'));
    // The app dialog names the cost; nothing is sent while it is open.
    expect(confirmText()).toEqual({
      title: 'Run the full golden suite?',
      body: 'This asks every golden question against the live orchestrator. It takes minutes and incurs model cost.',
      buttons: ['Cancel', 'Run full suite'],
    });
    expect(calls.some((c) => c.init?.method === 'POST')).toBe(false);
    await answerConfirm('cancel');
    expect(calls.some((c) => c.init?.method === 'POST')).toBe(false);
  });

  it('POSTs {} without a variant and {variant} with one; 202 selects the run', async () => {
    const calls = stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/run' && init?.method === 'POST') {
        return response({ run_id: 'run-9', status: 'running' }, 202);
      }
      if (url === '/api/admin/evals/runs/run-9') {
        return response({ status: 'running', progress: { current: 0, total: 3 } });
      }
      return undefined;
    });
    await renderTab();

    await click(container.querySelector('#evals-run-btn'));
    await answerConfirm('confirm');
    let post = calls.filter((c) => c.init?.method === 'POST');
    expect(post).toHaveLength(1);
    expect(JSON.parse(String(post[0].init!.body))).toEqual({});

    // The 202 run_id becomes the selected run — its progress renders.
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-progress')!.textContent).toContain('0/3'),
    );

    // With a variant label typed, the POST carries ONLY {variant}.
    await type(container.querySelector<HTMLInputElement>('#evals-variant-input')!, ' arm-b ');
    await click(container.querySelector('#evals-run-btn'));
    await answerConfirm('confirm');
    post = calls.filter((c) => c.init?.method === 'POST');
    expect(post).toHaveLength(2);
    expect(JSON.parse(String(post[1].init!.body))).toEqual({ variant: 'arm-b' });
  });

  it('surfaces the 409 detail verbatim', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/run' && init?.method === 'POST') {
        return response({ detail: 'eval run already active' }, 409);
      }
      return undefined;
    });
    await renderTab();

    await click(container.querySelector('#evals-run-btn'));
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-run-status')!.textContent).toContain(
        'eval run already active',
      ),
    );
  });

  it('disables the button and shows progress while a run is active', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler([
        { run_id: 'run-3', started_at: '2026-08-09T13:00:00Z', status: 'running', run_path: 'server' },
        ...RUNS,
      ])(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-3') {
        return response({ status: 'running', progress: { current: 2, total: 3 } });
      }
      return undefined;
    });
    await renderTab();

    // Load-tolerant: the active-run banner renders after the status poll
    // resolves; under the CI image's parallel load the default 1 s waitFor
    // budget was missed (~1%). The elements are asserted, not assumed.
    await vi.waitFor(() => {
      expect(container.querySelector<HTMLButtonElement>('#evals-run-btn')?.disabled).toBe(true);
      expect(container.querySelector('#evals-run-status')?.textContent).toContain('A run is active');
      expect(container.querySelector('#evals-progress')?.textContent).toContain('2/3');
    }, { timeout: 5000 });
  });
});

describe('run history + detail', () => {
  it('lists runs newest-first with run_path badge and suite counts', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler(RUNS)(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-2') return response(COMPLETE_DETAIL);
      return undefined;
    });
    await renderTab();

    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());
    const rows = Array.from(container.querySelectorAll('#evals-history-table tbody tr'));
    expect(rows.map((r) => r.getAttribute('data-run-id'))).toEqual(['run-2', 'run-1']);
    expect(rows[0].querySelector('.evals-runpath-badge')!.textContent).toBe('server');
    expect(rows[0].textContent).toContain('1 pass · 1 fail · 1 skipped / 3');
    expect(rows[0].textContent).toContain('arm-b');
  });

  it('renders the RunDetail fixture across all verdict states + trend + run_path badge without JS errors', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler(RUNS)(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-2') return response(COMPLETE_DETAIL);
      return undefined;
    });
    await renderTab();

    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());
    await click(container.querySelector('tr[data-run-id="run-2"]'));
    await vi.waitFor(() => expect(container.querySelector('#evals-report')).toBeTruthy());

    // Every verdict state renders a chip with the right palette class.
    const chipOf = (id: string) =>
      container.querySelector(`tr[data-question-id="${id}"] .evals-verdict-chip`)!;
    expect(chipOf('q-pass').className).toContain('pos');
    expect(chipOf('q-pass').textContent).toBe('pass');
    expect(chipOf('q-fail').className).toContain('neg');
    expect(chipOf('q-fail').textContent).toBe('fail');
    expect(chipOf('q-skip').textContent).toBe('skipped');

    // Latency, systems/neptune counts, honesty block, skip reason.
    const failRow = container.querySelector('tr[data-question-id="q-fail"]')!;
    expect(failRow.textContent).toContain('12.9s');
    expect(failRow.textContent).toContain('1 / 0');
    expect(failRow.querySelector('.evals-honesty')!.textContent).toContain('no data available');
    expect(failRow.querySelector('.evals-honesty')!.textContent).toContain(
      'quantified an absent metric',
    );
    expect(container.querySelector('tr[data-question-id="q-skip"]')!.textContent).toContain(
      'requires_seed',
    );

    // The run_path badge and golden_set_sha ride on the report header.
    const report = container.querySelector('#evals-report')!;
    expect(report.querySelector('.evals-runpath-badge')!.textContent).toBe('server');
    expect(report.textContent).toContain('abc123def456');

    // The trend block is present and LABELED informational — it never gates.
    const trend = container.querySelector('#evals-trend')!;
    expect(trend.textContent).toContain('informational — never gates');
    expect(trend.textContent).toContain('regressions 1');
    expect(trend.textContent).toContain('q-fail: pass → fail (regression)');
  });

  it('sink policy: hostile strings in answer_excerpt/failures stay inert literal text', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler(RUNS)(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-2') return response(COMPLETE_DETAIL);
      return undefined;
    });
    await renderTab();
    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());
    await click(container.querySelector('tr[data-run-id="run-2"]'));
    await vi.waitFor(() => expect(container.querySelector('#evals-report')).toBeTruthy());

    // The markup never becomes elements — report data is DATA, never markdown.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect((window as { __pwned2?: number }).__pwned2).toBeUndefined();

    // …and the raw strings are visible verbatim (literal text nodes).
    const failRow = container.querySelector('tr[data-question-id="q-fail"]')!;
    expect(failRow.querySelector('.evals-answer-excerpt')!.textContent).toBe(HOSTILE_EXCERPT);
    expect(failRow.querySelector('.evals-failures')!.textContent).toContain(HOSTILE_FAILURE);
  });

  it('renders a failed run’s error detail', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler(RUNS)(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-1') {
        return response({ status: 'failed', error: 'suite deadline exceeded' });
      }
      return undefined;
    });
    await renderTab();
    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());
    await click(container.querySelector('tr[data-run-id="run-1"]'));
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-detail')!.textContent).toContain(
        'suite deadline exceeded',
      ),
    );
  });
});

describe('managed AgentCore Evaluations section', () => {
  it('reuses the fleet payload: renders score rows and the lag/sampling note', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      if (url.startsWith('/api/admin/agents')) return response(FLEET);
      return undefined;
    });
    await renderTab();

    await vi.waitFor(() => expect(container.querySelector('#evals-managed-table')).toBeTruthy());
    const managed = container.querySelector('#evals-managed-section')!;
    expect(managed.textContent).toContain('Orchestrator');
    expect(managed.textContent).toContain('Correctness');
    expect(managed.textContent).toContain('87%');
    // EVAL_META label mapping applies (ToolSelectionAccuracy → Tool Selection).
    expect(managed.textContent).toContain('Tool Selection');
    // Honest copy: scores lag ~hours and sample ~10% of traffic.
    expect(managed.textContent).toContain('lag');
    expect(managed.textContent).toContain('10%');
    // Complement rule: the section complements the golden lane, never replaces it.
    expect(managed.textContent).toContain('never replaces');
  });

  it('shows the figure check pass rate with its no-figures count, and no rate before one exists', async () => {
    const fleet = {
      agents: [
        {
          display_name: 'Orchestrator',
          has_eval: true,
          eval_scores: {
            Correctness: { avg: 0.87, count: 42 },
            FigureCheck: { avg: 0.75, count: 8, no_figures: 5, unchecked: 1 },
          },
        },
        {
          display_name: 'Orchestrator B',
          eval_scores: { FigureCheck: { avg: null, count: 0, no_figures: 3, unchecked: 0 } },
        },
      ],
    };
    stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      if (url.startsWith('/api/admin/agents')) return response(fleet);
      return undefined;
    });
    await renderTab();

    await vi.waitFor(() => expect(container.querySelector('#evals-managed-table')).toBeTruthy());
    const rows = [...container.querySelectorAll('#evals-managed-table tbody tr')].map(
      (tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent),
    );
    expect(rows).toEqual([
      ['Orchestrator', 'Correctness', '87%', '42'],
      ['Orchestrator', 'Figure check', '75%', '8 · 5 no figures · 1 unchecked'],
      ['Orchestrator B', 'Figure check', '—', '0 · 3 no figures'],
    ]);
  });
});

describe('uploaded customer golden set (ADR-0064)', () => {
  it('states the data posture plainly: this run only, never stored, re-upload, redaction, 7 days', async () => {
    stubFetch(runsHandler([]));
    await renderTab();

    const section = container.querySelector('#evals-upload-section')!;
    const copy = section.textContent!;
    expect(copy).toContain('THIS RUN ONLY');
    expect(copy).toContain('never stored on the server');
    expect(copy).toContain('never committed to the ADT repository');
    expect(copy).toContain('re-upload');
    expect(copy).toContain('NOT persisted');
    expect(copy).toContain('7 days');
    // The variant label + question ids are the two steward-authored strings
    // that DO persist — the copy warns rather than letting them surprise.
    expect(copy).toContain('keep customer identity out of both');
  });

  it('reads the file at submit time and POSTs the RAW bytes with variant as the only query param', async () => {
    const calls = stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      if (url.startsWith('/api/admin/evals/run-upload') && init?.method === 'POST') {
        return response(
          {
            run_id: 'run-up-1',
            status: 'running',
            golden_set_source: 'uploaded',
            golden_set_sha: 'a1b2c3d4e5f6',
            question_count: 17,
          },
          202,
        );
      }
      if (url === '/api/admin/evals/runs/run-up-1') {
        return response({ status: 'running', progress: { current: 0, total: 17 } });
      }
      return undefined;
    });
    await renderTab();

    await type(container.querySelector<HTMLInputElement>('#evals-variant-input')!, ' arm-b ');
    await pickGoldenFile();
    // Attaching a file reads NOTHING and sends NOTHING — no request carries the
    // file until the steward confirms the run.
    expect(calls.every((c) => !bodyText(c.init).includes(CANARY))).toBe(true);

    await click(container.querySelector('#evals-upload-run-btn'));
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(calls.some((c) => c.url.startsWith('/api/admin/evals/run-upload'))).toBe(true),
    );

    const posts = calls.filter((c) => c.url.startsWith('/api/admin/evals/run-upload'));
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe('/api/admin/evals/run-upload?variant=arm-b');
    expect(posts[0].init!.method).toBe('POST');
    expect((posts[0].init!.headers as Record<string, string>)['Content-Type']).toBe(
      'application/json',
    );
    // RAW bytes, not a JSON envelope: the sha binds these exact bytes.
    expect(posts[0].init!.body instanceof ArrayBuffer).toBe(true);
    expect(bodyText(posts[0].init)).toBe(GOLDEN_TEXT);

    // The packaged run route was never touched, and no OTHER request carries
    // a byte of the file.
    expect(calls.some((c) => c.url === '/api/admin/evals/run')).toBe(false);
    for (const call of calls.filter((c) => !c.url.startsWith('/api/admin/evals/run-upload'))) {
      expect(bodyText(call.init)).not.toContain(CANARY);
    }

    // The 202's provenance is shown, with how to verify the sha, and the run
    // is followed.
    await vi.waitFor(() => {
      const started = container.querySelector('#evals-upload-started')!;
      expect(started.textContent).toContain('a1b2c3d4e5f6');
      expect(started.textContent).toContain('17 questions');
      expect(started.textContent).toContain('golden_file_sha');
    });
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-progress')!.textContent).toContain('0/17'),
    );

    // A re-run needs a re-upload: the picker is cleared and the button is
    // disabled again.
    expect(container.querySelector<HTMLButtonElement>('#evals-upload-run-btn')!.disabled).toBe(true);
  });

  it('is confirm-gated: a declined confirm sends NO request and reads nothing', async () => {
    const calls = stubFetch(runsHandler([]));
    await renderTab();

    await pickGoldenFile();
    await click(container.querySelector('#evals-upload-run-btn'));
    // The dialog states the data posture and the cost, one paragraph each.
    const prompt = confirmText();
    expect(prompt.title).toBe('Run your golden set against this deployment?');
    expect(prompt.body).toContain('used for this run only');
    expect(prompt.body).toContain('never stored on the server');
    expect(prompt.body).toContain('not persisted');
    expect(prompt.body).toContain('incurs model cost');
    expect(prompt.buttons).toEqual(['Cancel', 'Run uploaded set']);
    expect(document.querySelectorAll('.confirm-dialog-body p')).toHaveLength(3);
    await answerConfirm('cancel');
    expect(calls.some((c) => c.url.startsWith('/api/admin/evals/run-upload'))).toBe(false);
    expect(calls.every((c) => !bodyText(c.init).includes(CANARY))).toBe(true);
  });

  it('never lets the bytes reach durable browser state (gcTime 0, no storage write)', async () => {
    // The mutation itself refuses to cache: the golden-draft download idiom.
    expect(startUploadedEvalRunMutationOptions(createQueryClient()).gcTime).toBe(0);

    // Web-storage + IndexedDB writes are captured, not assumed absent (the
    // evals-queue.test.tsx D5 idiom — this node/jsdom combo ships a broken
    // localStorage, so a recording stub is the only honest observation).
    const storageWrites: { key: string; value: string }[] = [];
    for (const store of ['localStorage', 'sessionStorage'] as const) {
      vi.stubGlobal(store, {
        getItem: () => null,
        setItem: (key: string, value: string) => void storageWrites.push({ key, value }),
        removeItem: () => {},
      });
    }
    vi.stubGlobal('indexedDB', {
      open: vi.fn(() => {
        throw new Error('indexedDB is not used');
      }),
    });
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const calls = stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      return uploadHandler(202, {
        run_id: 'run-up-2',
        status: 'running',
        golden_set_sha: 'aaaabbbbcccc',
      })(url, init);
    });
    const client = await renderTab();
    await pickGoldenFile();
    await click(container.querySelector('#evals-upload-run-btn'));
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(calls.some((c) => c.url.startsWith('/api/admin/evals/run-upload'))).toBe(true),
    );
    await vi.waitFor(() => expect(container.querySelector('#evals-upload-started')).toBeTruthy());
    // The moment the UI first says "started", the bytes must ALREADY be gone —
    // reset() runs before the started state, not in a trailing finally.
    for (const mutation of client.getMutationCache().getAll()) {
      expect(mutation.state.variables).toBeUndefined();
    }

    // Nothing was written to web storage at all, by any code path in the tab.
    expect(storageWrites).toEqual([]);
    expect(setItem).not.toHaveBeenCalled();

    // Nothing in the query cache holds the bytes, and no mutation is still
    // holding its variables (reset() drops them the moment the call settles).
    expect(JSON.stringify(client.getQueryCache().getAll().map((q) => q.state.data))).not.toContain(
      CANARY,
    );
    for (const mutation of client.getMutationCache().getAll()) {
      expect(mutation.state.variables).toBeUndefined();
    }
  });

  it('renders each refusal with its OWN message, and the sha on the non-UTF-8 case', async () => {
    const cases: Array<{
      status: number;
      payload: unknown;
      expected: string;
      sha?: string;
    }> = [
      {
        status: 413,
        payload: { detail: { reason: 'payload_too_large', detail: 'body exceeds 262144 bytes' } },
        expected: '256 KiB',
      },
      {
        // The BOM / non-UTF-8 case: the server reports the sha it computed over
        // the bytes it received so a steward can reconcile against their file.
        status: 422,
        payload: {
          detail: {
            reason: 'invalid_json',
            detail: 'body is not UTF-8 JSON',
            sha: 'deadbeef1234',
          },
        },
        expected: 'byte-order mark',
        sha: 'deadbeef1234',
      },
      {
        status: 422,
        payload: {
          detail: { reason: 'invalid_golden_set', detail: "duplicate golden id 'cust-q1'" },
        },
        expected: 'not a valid golden set',
      },
      {
        status: 422,
        payload: { detail: { reason: 'too_many_questions', detail: '61 > 40' } },
        expected: 'cap is 40',
      },
      {
        status: 422,
        payload: { detail: { reason: 'unknown_query_params', detail: 'count' } },
        expected: 'no subset selection',
      },
      {
        status: 400,
        payload: { detail: { reason: 'secret_like_content', detail: 'secret-shaped value' } },
        expected: 'carries a credential',
      },
      {
        status: 409,
        payload: { detail: 'eval run already active' },
        expected: 'one golden run executes at a time',
      },
      { status: 403, payload: { detail: 'admin required' }, expected: 'Steward (admin) access' },
    ];

    for (const testCase of cases) {
      const calls = await runUpload(uploadHandler(testCase.status, testCase.payload));
      await vi.waitFor(() => expect(container.querySelector('#evals-upload-refusal')).toBeTruthy());
      const refusal = container.querySelector('#evals-upload-refusal')!;
      expect(refusal.textContent).toContain(testCase.expected);
      if (testCase.sha) {
        expect(refusal.querySelector('.evals-upload-refusal-sha')!.textContent).toContain(
          testCase.sha,
        );
      } else {
        expect(refusal.querySelector('.evals-upload-refusal-sha')).toBeNull();
      }
      // A refusal never re-sends and never echoes the file back at us.
      expect(calls.filter((c) => c.url.startsWith('/api/admin/evals/run-upload'))).toHaveLength(1);
      expect(refusal.textContent).not.toContain(CANARY);

      // Fresh mount per case (the tab keeps its own local status state).
      await act(async () => root.unmount());
      container.remove();
      container = document.createElement('div');
      document.body.appendChild(container);
      root = createRoot(container);
      vi.unstubAllGlobals();
    }
  });

  it('sink policy: a hostile variant label and a hostile server detail stay inert', async () => {
    const HOSTILE_LABEL = '<img src=x onerror="window.__pwned=1">';
    const HOSTILE_DETAIL = '<script>window.__pwned2=1</script> invalid golden set';
    const calls = stubFetch((url, init) => {
      const fromRuns = runsHandler([])(url, init);
      if (fromRuns) return fromRuns;
      if (url.startsWith('/api/admin/evals/run-upload') && init?.method === 'POST') {
        return response({ detail: { reason: 'invalid_golden_set', detail: HOSTILE_DETAIL } }, 422);
      }
      return undefined;
    });
    await renderTab();

    await type(container.querySelector<HTMLInputElement>('#evals-variant-input')!, HOSTILE_LABEL);
    await pickGoldenFile();
    await click(container.querySelector('#evals-upload-run-btn'));
    await answerConfirm('confirm');
    await vi.waitFor(() => expect(container.querySelector('#evals-upload-refusal')).toBeTruthy());

    // The label is percent-encoded into the query string, never interpolated raw.
    const post = calls.find((c) => c.url.startsWith('/api/admin/evals/run-upload'))!;
    expect(post.url).toContain(encodeURIComponent(HOSTILE_LABEL));
    expect(post.url).not.toContain('<img');

    // Neither string ever becomes an element.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect((window as { __pwned2?: number }).__pwned2).toBeUndefined();
    // …and the server's detail is visible verbatim as a literal text node.
    expect(
      container.querySelector('.evals-upload-refusal-detail')!.textContent,
    ).toContain(HOSTILE_DETAIL);
  });
});

describe('golden-set provenance in history and detail (ADR-0064 D3/D5)', () => {
  const MIXED_RUNS: EvalRunSummary[] = [
    {
      run_id: 'run-custom',
      started_at: '2026-08-19T10:00:00Z',
      status: 'complete',
      run_path: 'server',
      golden_set_source: 'uploaded',
      question_count: 17,
      golden_set_sha: 'ffff11112222',
      suite: { pass: 15, fail: 2, skipped: 0, total: 17 },
    },
    {
      run_id: 'run-demo',
      started_at: '2026-08-19T09:00:00Z',
      status: 'complete',
      run_path: 'server',
      golden_set_source: 'packaged',
      question_count: 24,
      golden_set_sha: 'abc123def456',
      suite: { pass: 24, fail: 0, skipped: 0, total: 24 },
    },
    // A row recorded BEFORE the server stamped provenance — em-dashed, never
    // guessed into a source (honest-data rule).
    { run_id: 'run-legacy', started_at: '2026-08-01T09:00:00Z', status: 'complete', run_path: 'server' },
  ];

  const REDACTED_DETAIL: EvalRunDetail = {
    status: 'complete',
    report: {
      run_id: 'run-custom',
      run_path: 'server',
      golden_set: 'uploaded',
      golden_set_source: 'uploaded',
      golden_set_sha: 'ffff11112222',
      answer_text_redacted: true,
      question_count: 17,
      suite: { pass: 1, fail: 1, skipped: 0, total: 2 },
      questions: [
        {
          id: 'cust-q1',
          verdict: 'pass',
          failures: [],
          answer_excerpt: '',
          honesty: { absence_markers_matched: [], fabrication_flags: [] },
        },
        {
          id: 'cust-q2',
          verdict: 'fail',
          failures: ["answer missing required pattern 'B-\\d{4}'", 'error event(s) (redacted, n=2)'],
          answer_excerpt: '',
          honesty: { absence_markers_matched: [], fabrication_flags: ['fabricated-quantity for absent metric'] },
        },
      ],
    },
  };

  it('badges a custom-set row distinctly from a demo-set row and never as comparable', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler(MIXED_RUNS)(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-custom') return response(REDACTED_DETAIL);
      return undefined;
    });
    await renderTab();
    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());

    const badgeOf = (runId: string) =>
      container.querySelector(`tr[data-run-id="${runId}"] .evals-goldensource-badge`)!;
    expect(badgeOf('run-custom').getAttribute('data-golden-source')).toBe('uploaded');
    expect(badgeOf('run-custom').textContent).toBe('custom set');
    // The packaged row does NOT carry the uploaded badge.
    expect(
      container.querySelector('tr[data-run-id="run-demo"] [data-golden-source="uploaded"]'),
    ).toBeNull();
    expect(badgeOf('run-demo').getAttribute('data-golden-source')).toBe('packaged');
    expect(badgeOf('run-demo').textContent).toBe('demo set');
    // Absent provenance is em-dashed, not guessed.
    expect(badgeOf('run-legacy').getAttribute('data-golden-source')).toBe('unknown');
    expect(badgeOf('run-legacy').textContent).toBe('—');

    // The sha + question count ride beside the badge, and the table says the
    // two sources are never comparable.
    const customRow = container.querySelector('tr[data-run-id="run-custom"]')!;
    expect(customRow.textContent).toContain('ffff11112222');
    expect(customRow.textContent).toContain('17 questions');
    expect(container.querySelector('#evals-history-note')!.textContent).toContain(
      'never comparable',
    );
  });

  it('a redacted custom-set report explains the missing excerpts instead of showing a gap', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler(MIXED_RUNS)(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-custom') return response(REDACTED_DETAIL);
      return undefined;
    });
    await renderTab();
    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());
    await click(container.querySelector('tr[data-run-id="run-custom"]'));
    await vi.waitFor(() => expect(container.querySelector('#evals-report')).toBeTruthy());

    // The report-level note names the reason and the horizon.
    const note = container.querySelector('#evals-report-redaction-note')!;
    expect(note.textContent).toContain('not persisted');
    expect(note.textContent).toContain('7 days');
    expect(note.textContent).toContain('scripts/audit_answer.py');

    // Every row carries the per-question note in place of an empty excerpt.
    const rows = Array.from(container.querySelectorAll('#evals-detail-table tbody tr'));
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(row.querySelector('.evals-answer-excerpt')).toBeNull();
      expect(row.querySelector('.evals-answer-redacted')!.textContent).toContain(
        'answer text not persisted',
      );
    }
    // Deterministic failures survive verbatim — they are the diagnostic value.
    expect(container.querySelector('tr[data-question-id="cust-q2"]')!.textContent).toContain(
      "answer missing required pattern 'B-\\d{4}'",
    );
    expect(container.querySelector('tr[data-question-id="cust-q2"]')!.textContent).toContain(
      'error event(s) (redacted, n=2)',
    );
    // The report header badges the source too.
    expect(
      container.querySelector('#evals-report [data-golden-source="uploaded"]')!.textContent,
    ).toBe('custom set');
  });

  it('a packaged run keeps its answer excerpt and shows NO redaction note', async () => {
    stubFetch((url, init) => {
      const fromRuns = runsHandler([
        { ...MIXED_RUNS[1], run_id: 'run-2' } as EvalRunSummary,
      ])(url, init);
      if (fromRuns) return fromRuns;
      if (url === '/api/admin/evals/runs/run-2') return response(COMPLETE_DETAIL);
      return undefined;
    });
    await renderTab();
    await vi.waitFor(() => expect(container.querySelector('#evals-history-table')).toBeTruthy());
    await click(container.querySelector('tr[data-run-id="run-2"]'));
    await vi.waitFor(() => expect(container.querySelector('#evals-report')).toBeTruthy());

    expect(container.querySelector('#evals-report-redaction-note')).toBeNull();
    expect(container.querySelector('.evals-answer-redacted')).toBeNull();
    expect(
      container.querySelector('tr[data-question-id="q-pass"] .evals-answer-excerpt')!.textContent,
    ).toContain('Batch B-2847');
  });
});
