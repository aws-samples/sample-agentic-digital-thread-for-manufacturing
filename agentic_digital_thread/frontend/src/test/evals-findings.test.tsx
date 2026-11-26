// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — findings ledger, weekly digest, bound import and the bounded
 * triage annotation status line (ADR-0062 D6/D10/D11).
 *
 * The pins, all against a mocked fetch (no network):
 *   - MEASURED-OR-ABSENT usage: a finding without usage renders NO usage
 *     element (not a zero), a finding with usage renders its token counts, and
 *     no rendered string is ever denominated in money or presented as an
 *     estimate;
 *   - `detail_expired` is its own honest state and it removes the filing
 *     affordance; the two file-proposal 409s render DISTINCTLY;
 *   - the steward note input is bounded and a REFUSED note's text never
 *     reaches the DOM;
 *   - hostile strings in the customer-derived detail and in a steward note
 *     stay inert literal text (repo zero-innerHTML sink policy);
 *   - digest ux_gap references render UNRESOLVED (ids only) and expanding one
 *     performs EXACTLY ONE detail fetch;
 *   - the import surfaces discarded/dropped counts honestly and sends the
 *     report text byte-for-byte as read (the sha binding depends on it);
 *   - the annotator status line has an honest "never ran" state, an honest
 *     unavailable state, and NO run control (that route is not wired here). */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type {
  AuditStatusResponse,
  FindingRow,
  FindingsDigest,
  FindingsLedgerResponse,
} from '../api/types/admin';
import EvalsTab from '../pages/admin/tabs/EvalsTab';
import FindingsLedger from '../pages/admin/tabs/evals/FindingsLedger';
import { answerConfirm, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const MAPPING_ID = 'f-1111111111111111';
const PLAIN_ID = 'f-2222222222222222';
const EXPIRED_ID = 'f-3333333333333333';
const UX_GAP_ID = 'f-4444444444444444';

// Hostile model output over customer data — must stay inert literal text.
const HOSTILE_EXCERPT = '<img src=x onerror="window.__pwned=1"> claimed 42 units';
const HOSTILE_RATIONALE = '<script>window.__pwned2=1</script> **not markdown**';
const HOSTILE_NOTE = '<img src=y onerror="window.__pwned3=1"> steward text';
const REFUSED_NOTE = 'AKIAIOSFODNN7EXAMPLE-shaped-secret-note';

const LEDGER: FindingsLedgerResponse = {
  total: 4,
  groups: {
    mapping_gap: [
      {
        finding_id: MAPPING_ID,
        taxonomy: 'mapping_gap',
        source_report_sha: 'a'.repeat(64),
        question_ref: 'q-aaaa1111',
        created_at: '2026-08-16T10:00:00Z',
      },
    ],
    data_gap: [
      {
        finding_id: PLAIN_ID,
        taxonomy: 'data_gap',
        source_report_sha: 'a'.repeat(64),
        question_ref: 'q-bbbb2222',
        created_at: '2026-08-16T10:00:00Z',
      },
      {
        finding_id: EXPIRED_ID,
        taxonomy: 'data_gap',
        source_report_sha: 'b'.repeat(64),
        question_ref: 'q-cccc3333',
        created_at: '2026-08-01T10:00:00Z',
      },
    ],
    ux_gap: [
      {
        finding_id: UX_GAP_ID,
        taxonomy: 'ux_gap',
        source_report_sha: 'a'.repeat(64),
        question_ref: 'q-dddd4444',
        created_at: '2026-08-16T10:00:00Z',
      },
    ],
  },
  counts: { mapping_gap: 1, data_gap: 2, ux_gap: 1 },
};

const DIGEST: FindingsDigest = {
  window_days: 7,
  total: 3,
  taxonomy_counts: {
    data_gap: 1,
    mapping_gap: 1,
    schema_drift: 0,
    prompt_gap: 0,
    code_bug: 0,
    ux_gap: 1,
    golden_candidate: 0,
  },
  ux_gap_refs: [{ finding_id: UX_GAP_ID, question_ref: 'q-dddd4444' }],
  ux_gap_refs_truncated: true,
};

/** Mapping-shaped, WITH measured usage — the fileable case. */
const MAPPING_FINDING: FindingRow = {
  finding_id: MAPPING_ID,
  taxonomy: 'mapping_gap',
  source_report_sha: 'a'.repeat(64),
  question_ref: 'q-aaaa1111',
  status: 'open',
  created_at: '2026-08-16T10:00:00Z',
  usage: { input_tokens: 1234, output_tokens: 321 },
  detail: {
    data_class: 'customer-derived',
    claim_excerpt: HOSTILE_EXCERPT,
    rationale: HOSTILE_RATIONALE,
    suggested_mapping: { from_system: 'erp', from_field: 'plant_code', to_system: 'mes' },
  },
  detail_expired: false,
  steward: null,
};

/** No usage block at all — nothing may be rendered for it. */
const PLAIN_FINDING: FindingRow = {
  finding_id: PLAIN_ID,
  taxonomy: 'data_gap',
  source_report_sha: 'a'.repeat(64),
  question_ref: 'q-bbbb2222',
  status: 'open',
  created_at: '2026-08-16T10:00:00Z',
  detail: { data_class: 'customer-derived', claim_excerpt: 'no rows returned', rationale: 'gap' },
  detail_expired: false,
  steward: null,
};

/** The 7-day detail aged out. */
const EXPIRED_FINDING: FindingRow = {
  finding_id: EXPIRED_ID,
  taxonomy: 'data_gap',
  source_report_sha: 'b'.repeat(64),
  question_ref: 'q-cccc3333',
  status: 'open',
  created_at: '2026-08-01T10:00:00Z',
  detail: null,
  detail_expired: true,
  steward: null,
};

const UX_GAP_FINDING: FindingRow = {
  finding_id: UX_GAP_ID,
  taxonomy: 'ux_gap',
  source_report_sha: 'a'.repeat(64),
  question_ref: 'q-dddd4444',
  status: 'open',
  created_at: '2026-08-16T10:00:00Z',
  detail: { data_class: 'customer-derived', claim_excerpt: 'chart never rendered', rationale: 'ux' },
  detail_expired: false,
  steward: null,
};

const FINDINGS: Record<string, FindingRow> = {
  [MAPPING_ID]: MAPPING_FINDING,
  [PLAIN_ID]: PLAIN_FINDING,
  [EXPIRED_ID]: EXPIRED_FINDING,
  [UX_GAP_ID]: UX_GAP_FINDING,
};

const NEVER_RAN: AuditStatusResponse = { scheduled_enabled: false, runs: [] };

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

interface FetchCall {
  url: string;
  method: string;
  body: unknown;
}

/** Route-table stub with the findings defaults filled in; a per-test handler
 * takes precedence so each test states only what it is pinning. */
function stubFetch(
  handler?: (url: string, method: string, body: unknown) => unknown | undefined,
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      const rawBody = init?.body ? String(init.body) : '';
      const body = rawBody ? (JSON.parse(rawBody) as unknown) : undefined;
      calls.push({ url, method, body });
      const custom = handler?.(url, method, body);
      if (custom !== undefined) {
        return custom instanceof Object && 'ok' in custom
          ? (custom as Response)
          : response(custom);
      }
      if (url === '/api/admin/evals/findings?window=7d') return response(DIGEST);
      if (url === '/api/admin/evals/findings') return response(LEDGER);
      if (url === '/api/admin/evals/audit/status') return response(NEVER_RAN);
      const detailMatch = /^\/api\/admin\/evals\/findings\/(f-[0-9a-f]{16})$/.exec(url);
      if (detailMatch && method === 'GET') {
        const row = FINDINGS[detailMatch[1]];
        return row ? response(row) : response({ detail: 'Unknown finding_id' }, 404);
      }
      // The tab also mounts the review queue and the fleet-backed managed
      // section; default them so tests stay focused on the ledger.
      if (url.startsWith('/api/admin/evals/queue')) return response({ items: [] });
      if (url.startsWith('/api/admin/agents')) return response({ agents: [] });
      if (url === '/api/admin/evals/runs') return response({ runs: [] });
      throw new Error(`unexpected URL: ${url} (${method})`);
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

async function renderPanel() {
  const client = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <FindingsLedger />
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() => expect(container.querySelector('#evals-findings-section')).toBeTruthy());
  return client;
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
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

/** Expand one ledger row and wait for its detail card. */
async function expand(findingId: string) {
  await vi.waitFor(() =>
    expect(container.querySelector(`.evals-findings-expand[data-finding-id="${findingId}"]`)).toBeTruthy(),
  );
  await click(container.querySelector(`.evals-findings-expand[data-finding-id="${findingId}"]`));
  await vi.waitFor(() => {
    const card = container.querySelector('#evals-finding-detail')!;
    expect(card.textContent).toContain(findingId);
  });
}

async function pickFile(inputId: string, text: string, name: string) {
  const input = document.getElementById(inputId) as HTMLInputElement;
  const file = new File([text], name, { type: 'application/json' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

/** Any money/estimate framing anywhere on the surface is a contract break:
 * usage is measured tokens or absent (ADR-0053). */
const MONEY_SHAPED = /\$|dollar|usd|estimat|\bprice\b|\bpricing\b|\bcost\b/i;

describe('measured-or-absent usage', () => {
  it('renders NO usage element for a finding without measured usage', async () => {
    stubFetch();
    await renderPanel();
    await expand(PLAIN_ID);

    const card = container.querySelector('#evals-finding-detail')!;
    expect(card.querySelector('.evals-finding-usage')).toBeNull();
    expect(card.textContent).not.toContain('measured tokens');
    // …and no zero was invented in its place.
    expect(card.textContent).not.toMatch(/tokens[^0-9]*0\b/i);
  });

  it('renders measured token counts when the server measured them', async () => {
    stubFetch();
    await renderPanel();
    await expand(MAPPING_ID);

    const usage = container.querySelector('.evals-finding-usage')!;
    expect(usage.textContent).toContain('1234');
    expect(usage.textContent).toContain('321');
    expect(usage.textContent).toContain('measured tokens');
  });

  it('never renders a money or estimate framing anywhere on the surface', async () => {
    stubFetch();
    await renderPanel();
    await expand(MAPPING_ID);
    await click(container.querySelector('#evals-findings-import-toggle'));

    expect(container.textContent ?? '').not.toMatch(MONEY_SHAPED);
  });
});

describe('the 7-day customer-derived detail', () => {
  it('renders detail_expired as its own state and offers no filing affordance', async () => {
    stubFetch();
    await renderPanel();
    await expand(EXPIRED_ID);

    const expired = container.querySelector('#evals-finding-detail-expired')!;
    expect(expired.textContent).toContain('Detail expired');
    expect(expired.textContent).toContain('7 days');
    expect(container.querySelector('#evals-finding-detail-body')).toBeNull();
    // No mapping survives the detail, so nothing can be filed from it.
    expect(container.querySelector('#evals-finding-file-proposal')).toBeNull();
  });

  it('labels the detail customer-derived and renders mapping values literally', async () => {
    stubFetch();
    await renderPanel();
    await expand(MAPPING_ID);

    expect(container.querySelector('.evals-finding-dataclass')!.textContent).toBe('customer-derived');
    expect(container.querySelector('.evals-finding-mapping')!.textContent).toContain('plant_code');
    expect(container.querySelector('#evals-finding-file-proposal')).toBeTruthy();
  });

  it('sink policy: hostile excerpt/rationale/note strings stay inert literal text', async () => {
    stubFetch((url, method) => {
      if (url.endsWith('/annotate') && method === 'POST') {
        return {
          status: 'recorded',
          annotation: {
            finding_id: MAPPING_ID,
            status: 'annotated',
            annotated_by: 'steward@example.com',
            annotated_at: '2026-08-18T00:00:00Z',
            steward_note: HOSTILE_NOTE,
          },
        };
      }
      return undefined;
    });
    await renderPanel();
    await expand(MAPPING_ID);

    // The detail's hostile strings render verbatim as text.
    expect(container.querySelector('.evals-finding-excerpt')!.textContent).toContain(HOSTILE_EXCERPT);
    expect(container.querySelector('.evals-finding-rationale')!.textContent).toContain(HOSTILE_RATIONALE);

    // A steward note echoed back by the server is data too.
    await type(container.querySelector<HTMLInputElement>('#evals-finding-note')!, HOSTILE_NOTE);
    await click(container.querySelector('#evals-finding-annotate-btn'));
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-finding-annotate-status')!.textContent).toContain(
        'Annotation saved',
      ),
    );

    // Nothing became an element and no payload executed.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect((window as { __pwned2?: number }).__pwned2).toBeUndefined();
    expect((window as { __pwned3?: number }).__pwned3).toBeUndefined();
  });
});

describe('steward annotation', () => {
  it('bounds the note input to the server ceiling', async () => {
    stubFetch();
    await renderPanel();
    await expand(PLAIN_ID);

    const note = container.querySelector<HTMLInputElement>('#evals-finding-note')!;
    expect(note.maxLength).toBe(500);
  });

  it('never displays a REFUSED note’s text — only the pinned reason copy', async () => {
    stubFetch((url, method) => {
      if (url.endsWith('/annotate') && method === 'POST') {
        return response(
          { detail: { reason: 'secret_like_content', detail: 'steward note looks secret-like' } },
          400,
        );
      }
      return undefined;
    });
    await renderPanel();
    await expand(PLAIN_ID);

    await type(container.querySelector<HTMLInputElement>('#evals-finding-note')!, REFUSED_NOTE);
    await click(container.querySelector('#evals-finding-annotate-btn'));
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-finding-annotate-status')!.textContent).toContain(
        'looks like it carries a secret',
      ),
    );
    expect(container.textContent ?? '').not.toContain(REFUSED_NOTE);
  });
});

describe('file proposal — steward-triggered only', () => {
  it('is confirm-gated and says plainly that filing is never automatic', async () => {
    const calls = stubFetch();
    await renderPanel();
    await expand(MAPPING_ID);

    await click(container.querySelector('#evals-finding-file-proposal'));
    expect(confirmText()).toEqual({
      title: 'File this suggested mapping as a proposal?',
      body: 'Filing is never automatic: the mapping enters the proposal queue as pending for a steward to accept or reject, and nothing is applied to the graph now.',
      buttons: ['Cancel', 'File proposal'],
    });
    await answerConfirm('cancel');
    expect(calls.some((c) => c.url.endsWith('/file-proposal'))).toBe(false);
    expect(container.querySelector('#evals-findings-section')!.textContent).toContain(
      'Filing is never automatic',
    );
    expect(container.querySelector('#evals-findings-section')!.textContent).toContain('pending');
  });

  it('reports a filed proposal as PENDING for steward review', async () => {
    stubFetch((url, method) => {
      if (url.endsWith('/file-proposal') && method === 'POST') {
        return { status: 'filed', finding_id: MAPPING_ID, mapping_id: 'map-77' };
      }
      return undefined;
    });
    await renderPanel();
    await expand(MAPPING_ID);

    await click(container.querySelector('#evals-finding-file-proposal'));
    await answerConfirm('confirm');
    await vi.waitFor(() => {
      const status = container.querySelector('#evals-finding-proposal-status')!;
      expect(status.textContent).toContain('pending proposal map-77');
    });
    expect(container.querySelector('#evals-finding-proposal-status')!.getAttribute('data-conflict')).toBeNull();
  });

  it('renders the two 409 reasons DISTINCTLY', async () => {
    let conflictDetail = 'detail expired';
    stubFetch((url, method) => {
      if (url.endsWith('/file-proposal') && method === 'POST') {
        return response({ detail: conflictDetail }, 409);
      }
      return undefined;
    });
    await renderPanel();
    await expand(MAPPING_ID);

    await click(container.querySelector('#evals-finding-file-proposal'));
    await answerConfirm('confirm');
    await vi.waitFor(() => {
      const status = container.querySelector('#evals-finding-proposal-status')!;
      expect(status.getAttribute('data-conflict')).toBe('detail-expired');
      expect(status.textContent).toContain('kept 7 days');
    });

    conflictDetail = 'finding carries no suggested mapping';
    await click(container.querySelector('#evals-finding-file-proposal'));
    await answerConfirm('confirm');
    await vi.waitFor(() => {
      const status = container.querySelector('#evals-finding-proposal-status')!;
      expect(status.getAttribute('data-conflict')).toBe('no-mapping');
      expect(status.textContent).toContain('no suggested mapping');
    });
  });
});

describe('this-week digest', () => {
  it('renders taxonomy counts and UNRESOLVED ux_gap references (ids only)', async () => {
    const calls = stubFetch();
    await renderPanel();

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-digest')).toBeTruthy());
    const digest = container.querySelector('#evals-findings-digest')!;
    // Only non-zero taxonomies get a chip; zero-count taxonomies are absent.
    const chips = Array.from(digest.querySelectorAll('.evals-digest-count'));
    expect(chips.map((c) => c.getAttribute('data-taxonomy'))).toEqual([
      'data_gap',
      'mapping_gap',
      'ux_gap',
    ]);
    expect(digest.textContent).toContain('Computed on read');

    // The refs are references: the finding id and the opaque question ref, and
    // NOTHING resolved — no excerpt, no rationale, and no detail fetch.
    const ref = digest.querySelector('.evals-digest-ux-gap')!;
    expect(ref.textContent).toContain(UX_GAP_ID);
    expect(ref.textContent).toContain('q-dddd4444');
    expect(digest.textContent).not.toContain('chart never rendered');
    expect(calls.filter((c) => c.url.includes(`/findings/${UX_GAP_ID}`))).toHaveLength(0);
    expect(container.querySelector('#evals-digest-ux-gaps-truncated')).toBeTruthy();
  });

  it('resolves a digest reference on expand with EXACTLY ONE detail fetch', async () => {
    const calls = stubFetch();
    await renderPanel();
    await vi.waitFor(() => expect(container.querySelector('.evals-digest-expand')).toBeTruthy());

    await click(container.querySelector('.evals-digest-expand'));
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-finding-detail')!.textContent).toContain(
        'chart never rendered',
      ),
    );
    expect(calls.filter((c) => c.url === `/api/admin/evals/findings/${UX_GAP_ID}`)).toHaveLength(1);
  });
});

describe('taxonomy-grouped ledger', () => {
  it('groups minimized rows by taxonomy and resolves nothing until expand', async () => {
    stubFetch();
    await renderPanel();

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-ledger')).toBeTruthy());
    const groups = Array.from(container.querySelectorAll('.evals-findings-group'));
    // The pinned taxonomy order (agents/audit_core.py TAXONOMY), not the
    // server's key order.
    expect(groups.map((g) => g.getAttribute('data-taxonomy'))).toEqual([
      'data_gap',
      'mapping_gap',
      'ux_gap',
    ]);
    // Minimized rows only: refs and timestamps, never generated free text.
    const ledger = container.querySelector('#evals-findings-ledger')!;
    expect(ledger.textContent).toContain(MAPPING_ID);
    expect(ledger.textContent).toContain('q-aaaa1111');
    expect(ledger.textContent).not.toContain(HOSTILE_EXCERPT);
    expect(container.querySelector('#evals-finding-detail')).toBeNull();
  });

  it('renders an honest empty state', async () => {
    stubFetch((url) => (url === '/api/admin/evals/findings' ? { total: 0, groups: {} } : undefined));
    await renderPanel();
    await vi.waitFor(() => expect(container.querySelector('#evals-findings-empty')).toBeTruthy());
    expect(container.querySelector('#evals-findings-ledger')).toBeNull();
  });
});

describe('bound import', () => {
  it('sends both halves (report text byte-for-byte) and surfaces discarded/dropped counts', async () => {
    const summary = {
      source_report_sha: 'c'.repeat(64),
      written: 3,
      deleted: 5,
      discarded_invalid: 2,
      rejected_fields: { secret_like_content: 1, too_long: 2 },
      taxonomy_counts: { data_gap: 3 },
      usage_bumped: 3,
    };
    const calls = stubFetch((url, method) =>
      url === '/api/admin/evals/findings/import' && method === 'POST' ? summary : undefined,
    );
    await renderPanel();
    await click(container.querySelector('#evals-findings-import-toggle'));

    // Deliberately irregular whitespace: the sha binding is over these bytes,
    // so the panel must not re-serialize the report.
    const reportText = '{\n   "run_id": "run-7",\n   "questions": []\n}\n';
    const findingsText = JSON.stringify({
      version: 1,
      source_report_sha: 'c'.repeat(64),
      findings: [{ id: '1', taxonomy: 'data_gap', question_id: 'q1' }],
    });
    await pickFile('evals-findings-file', findingsText, 'findings.json');
    await pickFile('evals-findings-report', reportText, 'report.json');
    await vi.waitFor(() =>
      expect(container.querySelector<HTMLButtonElement>('#evals-findings-import-btn')!.disabled).toBe(false),
    );
    await click(container.querySelector('#evals-findings-import-btn'));

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-import-result')).toBeTruthy());
    const post = calls.find((c) => c.url === '/api/admin/evals/findings/import')!;
    const body = post.body as { findings_file?: { version?: number }; source_report?: string };
    expect(body.findings_file?.version).toBe(1);
    expect(body.source_report).toBe(reportText);

    const result = container.querySelector('#evals-findings-import-result')!.textContent ?? '';
    expect(result).toContain('3 findings recorded');
    expect(result).toContain('5 prior generated rows replaced');
    expect(result).toContain('2 findings discarded as invalid');
    expect(result).toContain('secret_like_content');
    expect(result).toContain('too_long');
  });

  it('refuses to send findings without their source report', async () => {
    const calls = stubFetch();
    await renderPanel();
    await click(container.querySelector('#evals-findings-import-toggle'));

    await pickFile('evals-findings-file', '{"version":1,"findings":[]}', 'findings.json');
    // No report picked — the button stays disabled and nothing is posted.
    expect(container.querySelector<HTMLButtonElement>('#evals-findings-import-btn')!.disabled).toBe(true);
    await click(container.querySelector('#evals-findings-import-btn'));
    expect(calls.some((c) => c.url === '/api/admin/evals/findings/import')).toBe(false);
  });

  it('rejects an unparseable findings file client-side', async () => {
    const calls = stubFetch();
    await renderPanel();
    await click(container.querySelector('#evals-findings-import-toggle'));

    await pickFile('evals-findings-file', 'not json', 'findings.json');
    await pickFile('evals-findings-report', '{}', 'report.json');
    // Both halves are loaded (FileReader is async) before the click.
    await vi.waitFor(() =>
      expect(container.querySelector<HTMLButtonElement>('#evals-findings-import-btn')!.disabled).toBe(false),
    );
    await click(container.querySelector('#evals-findings-import-btn'));
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-findings-import-status')!.textContent).toContain(
        'not valid JSON',
      ),
    );
    expect(calls.some((c) => c.url === '/api/admin/evals/findings/import')).toBe(false);
  });
});

describe('the OPTIONAL transient evidence bundle', () => {
  const REPORT_TEXT = '{\n  "run_id": "run-7",\n  "questions": []\n}\n';
  const FINDINGS_TEXT = JSON.stringify({
    version: 1,
    source_report_sha: 'c'.repeat(64),
    findings: [{ id: '1', taxonomy: 'data_gap', question_id: 'q1' }],
  });

  /** Open the import card and load the two BOUND halves. */
  async function openWithBoundHalves() {
    const calls = stubFetch((url, method) =>
      url === '/api/admin/evals/findings/import' && method === 'POST'
        ? { source_report_sha: 'c'.repeat(64), written: 1, discarded_invalid: 0 }
        : undefined,
    );
    await renderPanel();
    await click(container.querySelector('#evals-findings-import-toggle'));
    await pickFile('evals-findings-file', FINDINGS_TEXT, 'findings.json');
    await pickFile('evals-findings-report', REPORT_TEXT, 'report.json');
    await vi.waitFor(() =>
      expect(container.querySelector<HTMLButtonElement>('#evals-findings-import-btn')!.disabled).toBe(false),
    );
    return calls;
  }

  function importBody(calls: Array<{ url: string; body?: unknown }>) {
    const post = calls.find((c) => c.url === '/api/admin/evals/findings/import')!;
    return post.body as { evidence?: unknown[]; source_report?: string };
  }

  /** Pick the optional bundle and wait until the panel has actually READ it
   * (FileReader is async — clicking Import before that would post nothing). */
  async function pickEvidence(text: string) {
    await pickFile('evals-findings-evidence', text, 'evidence.json');
    await vi.waitFor(() =>
      expect(container.querySelector('#evals-findings-evidence-loaded')).toBeTruthy(),
    );
  }

  it('offers the input as OPTIONAL and says it is only used to resolve references', async () => {
    stubFetch();
    await renderPanel();
    await click(container.querySelector('#evals-findings-import-toggle'));

    const input = container.querySelector<HTMLInputElement>('#evals-findings-evidence')!;
    expect(input.type).toBe('file');
    expect(input.required).toBe(false);
    expect(input.getAttribute('aria-label')).toContain('optional');
    const hint = container.querySelector('#evals-findings-evidence-hint')!.textContent ?? '';
    expect(hint).toContain('Optional');
    expect(hint).toContain('resolve');
    expect(hint).toContain('never stored');
    expect(hint).toContain('discarded');
  });

  it('omits the evidence key entirely when no bundle is chosen', async () => {
    const calls = await openWithBoundHalves();
    await click(container.querySelector('#evals-findings-import-btn'));

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-import-result')).toBeTruthy());
    const body = importBody(calls);
    expect('evidence' in body).toBe(false);
    // The bound halves are untouched by the new part.
    expect(body.source_report).toBe(REPORT_TEXT);
  });

  it('sends the parsed bundle when one is chosen, and stays optional for the button', async () => {
    const calls = await openWithBoundHalves();
    const evidenceText = JSON.stringify([
      { trace_id: 'trace-ev-77', steps: [{ step_id: 'step-ev-88' }] },
    ]);
    await pickEvidence(evidenceText);
    await click(container.querySelector('#evals-findings-import-btn'));

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-import-result')).toBeTruthy());
    const body = importBody(calls);
    expect(body.evidence).toEqual(JSON.parse(evidenceText));
    expect(body.source_report).toBe(REPORT_TEXT);
  });

  it('wraps a single evidence document without requiring an array', async () => {
    const calls = await openWithBoundHalves();
    await pickEvidence('{"trace_id":"trace-ev-77"}');
    await click(container.querySelector('#evals-findings-import-btn'));

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-import-result')).toBeTruthy());
    expect(importBody(calls).evidence).toEqual([{ trace_id: 'trace-ev-77' }]);
  });

  it('rejects an unparseable evidence file client-side and posts nothing', async () => {
    const calls = await openWithBoundHalves();
    await pickEvidence('not json');
    await click(container.querySelector('#evals-findings-import-btn'));

    await vi.waitFor(() =>
      expect(container.querySelector('#evals-findings-import-status')!.textContent).toContain(
        'evidence file is not valid JSON',
      ),
    );
    expect(calls.some((c) => c.url === '/api/admin/evals/findings/import')).toBe(false);
  });

  it('surfaces discarded_invalid even when a bundle was supplied', async () => {
    const summary = {
      source_report_sha: 'c'.repeat(64),
      written: 1,
      discarded_invalid: 2,
    };
    const calls = stubFetch((url, method) =>
      url === '/api/admin/evals/findings/import' && method === 'POST' ? summary : undefined,
    );
    await renderPanel();
    await click(container.querySelector('#evals-findings-import-toggle'));
    await pickFile('evals-findings-file', FINDINGS_TEXT, 'findings.json');
    await pickFile('evals-findings-report', REPORT_TEXT, 'report.json');
    await pickEvidence('{"trace_id":"trace-ev-77"}');
    await vi.waitFor(() =>
      expect(container.querySelector<HTMLButtonElement>('#evals-findings-import-btn')!.disabled).toBe(false),
    );
    await click(container.querySelector('#evals-findings-import-btn'));

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-import-result')).toBeTruthy());
    // Supplying evidence does not make discards disappear from the summary.
    expect(container.querySelector('#evals-findings-import-result')!.textContent).toContain(
      '2 findings discarded as invalid',
    );
    expect(importBody(calls).evidence).toEqual([{ trace_id: 'trace-ev-77' }]);
  });
});

describe('bounded triage annotation status', () => {
  it('renders an honest "never ran" state, no usage, and NO run control', async () => {
    stubFetch();
    await renderPanel();

    await vi.waitFor(() => expect(container.querySelector('#evals-audit-never-ran')).toBeTruthy());
    const section = container.querySelector('#evals-audit-status')!;
    expect(section.textContent).toContain('never run');
    expect(section.textContent).toContain('Bounded triage annotation');
    // No measured usage exists, so no usage element and no budget line either.
    expect(section.querySelector('.evals-audit-run-usage')).toBeNull();
    expect(container.querySelector('#evals-audit-budget')).toBeNull();
    // Triggering a run is NOT an affordance on this surface.
    expect(section.querySelectorAll('button')).toHaveLength(0);
  });

  it('renders measured tokens per run, omits them when unmeasured, and shows the budget', async () => {
    const status: AuditStatusResponse = {
      scheduled_enabled: false,
      runs: [
        {
          run_id: 'audit-1',
          trigger: 'manual',
          status: 'complete',
          started_at: '2026-08-18T02:00:00Z',
          usage: { input_tokens: 900, output_tokens: 120 },
        },
        {
          run_id: 'audit-0',
          trigger: 'scheduled',
          status: 'skipped_budget',
          started_at: '2026-08-17T02:00:00Z',
        },
      ],
      budget: { day_key: '2026-08-18', claimed: 3, cap: 10, durable: true },
      telemetry: { 'eval-finding': 12, 'audit-run': 2 },
    };
    stubFetch((url) => (url === '/api/admin/evals/audit/status' ? status : undefined));
    await renderPanel();

    await vi.waitFor(() => expect(container.querySelector('#evals-audit-runs')).toBeTruthy());
    const rows = Array.from(container.querySelectorAll('.evals-audit-run'));
    expect(rows).toHaveLength(2);
    expect(rows[0].querySelector('.evals-audit-run-usage')!.textContent).toContain('900');
    expect(rows[0].textContent).toContain('manual');
    // The unmeasured run gets NO usage element — not a zero.
    expect(rows[1].querySelector('.evals-audit-run-usage')).toBeNull();
    expect(rows[1].textContent).toContain('skipped_budget');

    expect(container.querySelector('#evals-audit-budget')!.textContent).toContain(
      '3 of 10 annotation slots claimed today',
    );
    expect(container.querySelector('#evals-audit-telemetry')!.textContent).toContain('eval-finding 12');
    expect(container.textContent ?? '').not.toMatch(MONEY_SHAPED);
  });

  it('says the status is unavailable rather than inventing zeros', async () => {
    stubFetch((url) =>
      url === '/api/admin/evals/audit/status' ? response({ detail: 'Not Found' }, 404) : undefined,
    );
    await renderPanel();

    await vi.waitFor(() =>
      expect(container.querySelector('#evals-audit-status-unavailable')).toBeTruthy(),
    );
    expect(container.querySelector('#evals-audit-status-unavailable')!.textContent).toContain(
      'Not Found',
    );
    expect(container.querySelector('#evals-audit-never-ran')).toBeNull();
    expect(container.querySelector('#evals-audit-budget')).toBeNull();
  });

  it('renders an honest unavailable line for the ledger and the digest too', async () => {
    stubFetch((url) => {
      if (url === '/api/admin/evals/findings') return response({ detail: 'boom' }, 500);
      if (url === '/api/admin/evals/findings?window=7d') return response({ detail: 'boom' }, 500);
      return undefined;
    });
    await renderPanel();

    await vi.waitFor(() => expect(container.querySelector('#evals-findings-unavailable')).toBeTruthy());
    expect(container.querySelector('#evals-findings-digest-unavailable')).toBeTruthy();
    expect(container.querySelector('#evals-findings-empty')).toBeNull();
  });
});

describe('mounted in the Evals tab', () => {
  it('is a DISTINCT surface from the run-ALL golden control and adds no run affordance', async () => {
    stubFetch();
    const client = createQueryClient();
    await act(async () => {
      root.render(
        <QueryClientProvider client={client}>
          <EvalsTab />
        </QueryClientProvider>,
      );
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(container.querySelector('#evals-findings-section')).toBeTruthy());
    // Deterministic settle: the digest renders async after its own fetch; wait
    // for it so this assertion always covers the FULLY rendered surface (the
    // fixture sets ux_gap_refs_truncated, so the truncated note must be up).
    await vi.waitFor(() => expect(container.querySelector('#evals-digest-ux-gaps-truncated')).toBeTruthy());

    // The golden-suite affordance is still exactly one button, and it is NOT
    // inside the findings surface (ADR-0062 D11: distinct surfaces).
    const runBtn = container.querySelector('#evals-run-btn')!;
    expect(container.querySelectorAll('#evals-run-form button')).toHaveLength(1);
    const findings = container.querySelector('#evals-findings-section')!;
    expect(findings.contains(runBtn)).toBe(false);
    // "run" as a whole id SEGMENT (evals-run-btn), never a substring — a bare
    // [id*="run"] false-positives on ids like ...-ux-gaps-tRUNcated.
    const runSegmentIds = Array.from(findings.querySelectorAll('[id]'))
      .filter((el) => /(^|-)run(-|$)/.test(el.id));
    expect(runSegmentIds).toHaveLength(0);

    // The findings surface adds no input to the tab's default render, so the
    // run-ALL-only pin (one variant label input) is untouched.
    expect(findings.querySelectorAll('input')).toHaveLength(0);
  });
});
