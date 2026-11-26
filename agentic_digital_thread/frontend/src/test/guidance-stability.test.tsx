// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { AuthProvider } from '../contexts/AuthContext';
import GuidanceEditor from '../pages/admin/tabs/vocabulary/GuidanceEditor';
import {
  AuthorityTag,
  numberOrUnavailable,
  stateColor,
  stateLabel,
  type AuthorityState,
} from '../pages/admin/tabs/vocabulary/guidance-stability-ui';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Call = { url: string; init?: RequestInit };
let container: HTMLDivElement;
let root: Root;
let calls: Call[];

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? 'OK' : 'Failed',
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

const system = {
  system_id: 'mes',
  base: 'Use canonical identifiers.',
  notes: ['existing learned note'],
  learned_count: 1,
  length: 320,
  max_len: 4000,
};
const proposal = {
  mapping_id: 'guidance-1',
  proposed_type: 'guidance_note',
  status: 'pending',
  from_system: 'mes',
  to_value: 'REQUIRED: the answer must use canonical identifiers.',
  guidance_scope: 'answer_requirement',
};
const reconciliation = {
  status: 'ok',
  counts: { accepted: 2, graph: 2, delivered: 2 },
  sources: { accepted: 'ok', graph: 'ok', delivery: 'ok' },
  mismatches: [],
  mismatch_count: 0,
  truncated: false,
};

const appendPreview = {
  outcome: 'append',
  current_digest: 'sha256:current',
  current_length: 320,
  note_digest: 'sha256:note',
  note_length: 54,
  scope: 'answer_requirement',
  result_digest: 'sha256:result',
  result_length: 376,
  evicted_note_digests: [],
};
const evictionPreview = {
  ...appendPreview,
  outcome: 'would_evict',
  current_digest: 'sha256:fresh-current',
  result_length: 3900,
  evicted_note_digests: ['sha256:oldest', 'sha256:next'],
};

function base(url: string, init?: RequestInit): Response | undefined {
  const method = init?.method || 'GET';
  if (url === '/api/auth/me') return response({ user_id: 'steward', email: 's@example.com', is_admin: true });
  if (url === '/api/query-guidance' && method === 'GET') return response({ systems: [system] });
  if (url === '/api/proposed-mappings?proposed_type=guidance_note' && method === 'GET') return response({ mappings: [proposal] });
  if (url === '/api/query-guidance/reconciliation' && method === 'GET') return response(reconciliation);
  if (url === '/api/systems' && method === 'GET') return response({ systems: [{ id: 'mes', name: 'Plant MES' }] });
  return undefined;
}

function stub(handler: (url: string, init?: RequestInit) => Response | undefined) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const result = handler(url, init) ?? base(url, init);
    if (!result) throw new Error(`unexpected fetch ${init?.method || 'GET'} ${url}`);
    return result;
  }));
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  calls = [];
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderEditor() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={createQueryClient()}>
        <AuthProvider><GuidanceEditor /></AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  await vi.waitFor(() => expect(container.querySelector('.guidance-proposal')).toBeTruthy());
}

async function click(element: Element | null) {
  expect(element).not.toBeNull();
  await act(async () => {
    (element as HTMLElement).click();
    await Promise.resolve();
  });
}

function previewButton() {
  return [...container.querySelectorAll<HTMLButtonElement>('.guidance-proposal button')]
    .find((button) => button.textContent?.includes('Preview delivery'))!;
}

function line() {
  return container.querySelector('.guidance-proposal-line')?.textContent ?? '';
}

function cardTechnical() {
  return container.querySelector('.guidance-proposal details.guidance-proposal-technical');
}

function buttonNamed(name: string) {
  return [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find((button) => button.textContent?.trim() === name) ?? null;
}

describe('guidance preview, delivery, and reconciliation', () => {
  it('shows scope, character headroom, PREVIEW, and DELIVERED only after proven accept', async () => {
    stub((url, init) => {
      if (url.endsWith('/guidance-preview')) return response({ status: 'preview', mapping_id: 'guidance-1', preview: appendPreview });
      if (url.endsWith('/accept') && init?.method === 'POST') return response({ status: 'accepted', outcome: 'append', proposed_type: 'guidance_note', version: 7 });
      return undefined;
    });
    await renderEditor();
    expect(container.querySelector('.vocab-guidance-authority')?.textContent).toContain('character budget 320/4000 · headroom 3680');
    await vi.waitFor(() => expect(container.querySelector('.guidance-reconciliation')?.textContent).toContain('accepted 2 · graph 2 · delivered 2'));
    expect(container.querySelector('.guidance-reconciliation [data-state="delivered"]')).toBeTruthy();
    expect(line()).toBe('Choose Preview delivery to see what changes.');

    await click(previewButton());
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal [data-state="preview"]')).toBeTruthy());
    expect(line()).toBe('Ready. Choose Deliver.');
    expect(cardTechnical()?.textContent).toContain('outcome append');
    expect(cardTechnical()?.textContent).toContain('current 320 chars · note 54 · result 376');
    await click(buttonNamed('Deliver'));
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal [data-state="delivered"]')).toBeTruthy());
    expect(line()).toBe('The assistant reads this note from your next question.');
    expect(cardTechnical()?.textContent).toContain('Accepted after graph and agent-store readback (version 7).');
  });

  it('keeps budget, scope, digests and version behind collapsed Technical detail', async () => {
    stub((url) => url.endsWith('/guidance-preview')
      ? response({ status: 'preview', mapping_id: 'guidance-1', preview: appendPreview })
      : undefined);
    await renderEditor();
    const budget = container.querySelector('.vocab-guidance-authority')!;
    const systemTechnical = budget.closest('details.vocab-guidance-technical') as HTMLDetailsElement | null;
    expect(systemTechnical).not.toBeNull();
    expect(systemTechnical!.open).toBe(false);
    expect(systemTechnical!.querySelector('summary')?.textContent).toBe('Technical detail');

    await click(previewButton());
    await vi.waitFor(() => expect(container.querySelector('.guidance-preview-data')).toBeTruthy());
    const card = container.querySelector('.guidance-proposal')!;
    for (const selector of ['.guidance-proposal-scope', '.guidance-preview-data']) {
      const node = card.querySelector(selector)!;
      expect((node.closest('details.guidance-proposal-technical') as HTMLDetailsElement).open).toBe(false);
    }
    expect(card.querySelector('.guidance-proposal-scope')?.textContent).toBe('scope answer_requirement');
    const visible = card.textContent!.replace(cardTechnical()!.textContent!, '');
    expect(visible).not.toMatch(/digest|chars|scope|PENDING|PREVIEW/);
  });

  it('labels the base entry and the proposal card by display name, then id', async () => {
    stub(() => undefined);
    await renderEditor();
    await vi.waitFor(() => expect(container.querySelector('.vocab-guidance-base summary')?.textContent).toContain('Base guidance: Plant MES (mes)'));
    expect(container.querySelector('.guidance-proposal strong')?.textContent).toBe('Plant MES (mes)');
    expect(container.querySelector('.guidance-proposal [data-state="pending"]')?.textContent).toBe('Not delivered');
  });

  it('renders the bare id while /api/systems is pending, then Name (id) once it resolves', async () => {
    let releaseSystems!: () => void;
    const systemsGate = new Promise<void>((resolve) => { releaseSystems = resolve; });
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url === '/api/systems') {
        await systemsGate;
        return response({ systems: [{ id: 'mes', name: 'Plant MES' }] });
      }
      const result = base(url, init);
      if (!result) throw new Error(`unexpected fetch ${init?.method || 'GET'} ${url}`);
      return result;
    }));
    await renderEditor();
    await vi.waitFor(() => expect(calls.some((call) => call.url === '/api/systems')).toBe(true));
    expect(container.querySelector('.vocab-guidance-base summary')?.textContent?.trim()).toBe('Base guidance: mes');
    expect(container.querySelector('.guidance-proposal strong')?.textContent).toBe('mes');

    await act(async () => {
      releaseSystems();
      await systemsGate;
    });
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal strong')?.textContent).toBe('Plant MES (mes)'));
    expect(container.querySelector('.vocab-guidance-base summary')?.textContent).toContain('Base guidance: Plant MES (mes)');
  });

  it('falls back to the system id when the registry is unreadable', async () => {
    stub((url) => url === '/api/systems' ? response({ detail: 'down' }, 503) : undefined);
    await renderEditor();
    await vi.waitFor(() => expect(calls.some((call) => call.url === '/api/systems')).toBe(true));
    expect(container.querySelector('.vocab-guidance-base summary')?.textContent?.trim()).toBe('Base guidance: mes');
    expect(container.querySelector('.guidance-proposal strong')?.textContent).toBe('mes');
  });

  it('turns a 409 WOULD EVICT into exact ordered confirmation, focuses it, then retries exactly', async () => {
    let accepts = 0;
    stub((url, init) => {
      if (url.endsWith('/guidance-preview')) return response({ status: 'preview', mapping_id: 'guidance-1', preview: appendPreview });
      if (url.endsWith('/accept') && init?.method === 'POST') {
        accepts += 1;
        if (accepts === 1) return response({ status: 'pending', outcome: 'would_evict', error: 'stale_preview', preview: evictionPreview }, 409);
        return response({ status: 'accepted', outcome: 'would_evict', proposed_type: 'guidance_note', version: 8 });
      }
      return undefined;
    });
    await renderEditor();
    await click(previewButton());
    await vi.waitFor(() => expect(buttonNamed('Deliver')).toBeTruthy());
    await click(buttonNamed('Deliver'));

    await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeTruthy());
    const checkbox = document.querySelector<HTMLInputElement>('[role="dialog"] input[type="checkbox"]')!;
    expect(document.activeElement).toBe(checkbox);
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('sha256:fresh-current');
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('removes the 2 older learned notes listed below');
    const ordered = [...document.querySelectorAll('[aria-label="Ordered evicted guidance digests"] li')].map((item) => item.textContent);
    expect(ordered).toEqual(['sha256:oldest', 'sha256:next']);
    expect(buttonNamed('Remove them and deliver')?.disabled).toBe(true);
    await click(checkbox);
    expect(buttonNamed('Remove them and deliver')?.disabled).toBe(false);
    await click(buttonNamed('Remove them and deliver'));

    await vi.waitFor(() => expect(accepts).toBe(2));
    const retry = calls.filter((call) => call.url.endsWith('/accept'))[1]!;
    expect(JSON.parse(String(retry.init?.body))).toEqual({
      current_digest: 'sha256:fresh-current',
      evicted_note_digests: ['sha256:oldest', 'sha256:next'],
    });
  });

  it.each([
    ['duplicate', 'This note is already in the guidance. Nothing to deliver.'],
    ['no_room', "This note does not fit in the system's guidance. Shorten it or remove an older learned note, then propose it again."],
    ['unavailable', 'The preview could not be read, so nothing changed. Choose Preview delivery to try again.'],
  ] as const)('keeps %s as a non-accepted result', async (outcome, line) => {
    stub((url) => url.endsWith('/guidance-preview')
      ? response({ status: 'preview', mapping_id: 'guidance-1', preview: { ...appendPreview, outcome, result_digest: null, result_length: null } })
      : undefined);
    await renderEditor();
    await click(previewButton());
    await vi.waitFor(() => expect(container.querySelector('.guidance-proposal-line')?.textContent).toBe(line));
    expect(buttonNamed('Deliver')).toBeNull();
    expect(calls.some((call) => call.url.endsWith('/accept'))).toBe(false);
  });

  it('renders unavailable reconciliation counts as unavailable, never zero, and offers no repair', async () => {
    stub((url) => url === '/api/query-guidance/reconciliation'
      ? response({ ...reconciliation, status: 'unavailable', counts: { accepted: 2, graph: null, delivered: null }, sources: { accepted: 'ok', graph: 'unavailable', delivery: 'unavailable' } })
      : undefined);
    await renderEditor();
    // The reconciliation panel fills in after its own fetch; on a loaded CI
    // runner it is still empty when the editor's first paint lands.
    await vi.waitFor(() => expect(container.querySelector('.guidance-reconciliation')?.textContent).toContain('graph unavailable · delivered unavailable'));
    const text = container.querySelector('.guidance-reconciliation')?.textContent ?? '';
    expect(text).not.toContain('graph 0');
    expect(text.toLowerCase()).not.toContain('repair');
  });
  it('uses fixed copy for reconciliation failures and never renders backend detail', async () => {
    stub((url) => url === '/api/query-guidance/reconciliation'
      ? response({ detail: 'database-password=do-not-render' }, 503)
      : undefined);
    await renderEditor();
    await vi.waitFor(() => expect(container.querySelector('.guidance-reconciliation[role="alert"]')).toBeTruthy());
    const alert = container.querySelector('.guidance-reconciliation[role="alert"]')?.textContent ?? '';
    expect(alert).toContain('Reconciliation is unavailable. Try again.');
    expect(alert).not.toContain('database-password');
  });

  it('does not claim DELIVERED for a malformed accepted response', async () => {
    stub((url, init) => {
      if (url.endsWith('/guidance-preview')) return response({ status: 'preview', mapping_id: 'guidance-1', preview: appendPreview });
      if (url.endsWith('/accept') && init?.method === 'POST') {
        return response({ status: 'accepted', outcome: 'append', error: 'raw backend text' });
      }
      return undefined;
    });
    await renderEditor();
    await click(previewButton());
    await click(buttonNamed('Deliver'));
    await vi.waitFor(() => expect(line()).toContain('nothing was accepted'));
    const text = container.querySelector('.guidance-proposal')?.textContent ?? '';
    expect(container.querySelector('.guidance-proposal [data-state="delivered"]')).toBeNull();
    expect(text).not.toContain('reads this note');
    expect(text).not.toContain('raw backend text');
  });
});

describe('scoped context placement polling', () => {
  it('requires confirmation, sends one selected system, polls done, and links PENDING filed proposals', async () => {
    stub((url, init) => {
      if (url === '/api/admin/learning/context-placement-check' && init?.method === 'POST') return response({ job_id: 'job-1' }, 202);
      if (url.endsWith('/context-placement-check/job-1')) return response({ status: 'done', result: { systems_checked: 2, terms_checked: 3, findings: 1, filed: 1, resolved_swept: 0, skipped: [], truncated: false, elapsed_seconds: 0.2 } });
      return undefined;
    });
    await renderEditor();
    await click(buttonNamed('Run scoped check'));
    expect(calls.some((call) => call.url === '/api/admin/learning/context-placement-check')).toBe(false);
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('mes');
    await click(buttonNamed('Run check'));
    await vi.waitFor(() => expect(container.querySelector('.context-placement-result')).toBeTruthy());
    const launch = calls.find((call) => call.url === '/api/admin/learning/context-placement-check')!;
    expect(JSON.parse(String(launch.init?.body))).toEqual({ system_id: 'mes' });
    const placement = container.querySelector('.context-placement-control')!;
    expect(placement.querySelector('[data-state="found"]')).toBeTruthy();
    expect(placement.querySelector('[data-state="pending"]')).toBeTruthy();
    expect(placement.querySelector('[data-state="delivered"]')).toBeNull();
    expect(container.querySelector('.context-placement-result')?.textContent).toContain('Found — 1 placement finding');
    expect(container.querySelector('.context-placement-result')?.textContent).toContain('Pending');
    expect(container.querySelector<HTMLAnchorElement>('.context-placement-result a')?.getAttribute('href')).toBe('/#architecture');
  });

  it('renders None only for a completed check with no findings and no skips', async () => {
    stub((url, init) => {
      if (url === '/api/admin/learning/context-placement-check' && init?.method === 'POST') return response({ job_id: 'job-zero' }, 202);
      if (url.endsWith('/job-zero')) return response({ status: 'done', result: { systems_checked: 2, terms_checked: 3, findings: 0, filed: 0, resolved_swept: 0, skipped: [], truncated: false, elapsed_seconds: 0.1 } });
      return undefined;
    });
    await renderEditor();
    await click(buttonNamed('Run scoped check'));
    await click(buttonNamed('Run check'));
    await vi.waitFor(() => expect(container.querySelector('.context-placement-result')?.textContent).toContain('None — the completed check found no placement findings'));
  });

  it('renders skipped systems as Unavailable rather than zero findings', async () => {
    stub((url, init) => {
      if (url === '/api/admin/learning/context-placement-check' && init?.method === 'POST') return response({ job_id: 'job-skipped' }, 202);
      if (url.endsWith('/job-skipped')) return response({ status: 'done', result: { systems_checked: 0, terms_checked: 0, findings: 0, filed: 0, resolved_swept: 0, skipped: [{ system_id: 'mes', reason: 'schema-unavailable' }], truncated: false, elapsed_seconds: 0.1 } });
      return undefined;
    });
    await renderEditor();
    await click(buttonNamed('Run scoped check'));
    await click(buttonNamed('Run check'));
    await vi.waitFor(() => expect(container.querySelector('.context-placement-skip')?.textContent).toContain('Unavailable'));
    // Named as the rest of the Admin guidance surfaces name it: "Name (id)".
    expect(container.querySelector('.context-placement-skip')?.textContent).toContain('Plant MES (mes): Schema is unavailable.');
    expect(container.querySelector('.context-placement-skip')?.textContent).not.toContain('schema-unavailable');
    expect(container.querySelector('.context-placement-result')?.textContent).not.toContain('None —');
    expect(container.querySelector('.context-placement-control')?.textContent).not.toContain('Delivered');
  });

  it('renders an async poll error honestly', async () => {
    stub((url, init) => {
      if (url === '/api/admin/learning/context-placement-check' && init?.method === 'POST') return response({ job_id: 'job-error' }, 202);
      if (url.endsWith('/job-error')) return response({ status: 'error', error: 'context-placement check failed: RuntimeError' });
      return undefined;
    });
    await renderEditor();
    await click(buttonNamed('Run scoped check'));
    await click(buttonNamed('Run check'));
    await vi.waitFor(() => expect(container.querySelector('.context-placement-control [role="alert"]')?.textContent).toContain('Unavailable — the placement check did not complete.'));
    expect(container.querySelector('.context-placement-control')?.textContent).not.toContain('RuntimeError');
    expect(container.querySelector('.context-placement-control [data-state="unavailable"]')).toBeTruthy();
  });

  it('renders poll errors and launch authorization denial as Unavailable', async () => {
    stub((url, init) => {
      if (url === '/api/admin/learning/context-placement-check' && init?.method === 'POST') return response({ detail: 'Steward access required' }, 403);
      return undefined;
    });
    await renderEditor();
    await click(buttonNamed('Run scoped check'));
    await click(buttonNamed('Run check'));
    await vi.waitFor(() => expect(container.querySelector('.context-placement-control [role="alert"]')?.textContent).toContain('Unavailable — the context placement check could not start.'));
    expect(container.querySelector('.context-placement-control')?.textContent).not.toContain('Steward access required');
    expect(container.querySelector('.context-placement-control [data-state="unavailable"]')).toBeTruthy();
  });
});

describe('AuthorityTag visible words', () => {
  const STATES: Array<[AuthorityState, string, string]> = [
    ['PREVIEW', 'Preview', 'preview'],
    ['FOUND', 'Found', 'found'],
    ['PENDING', 'Pending', 'pending'],
    ['DELIVERED', 'Delivered', 'delivered'],
    ['WOULD EVICT', 'Would remove notes', 'would-evict'],
    ['UNAVAILABLE', 'Unavailable', 'unavailable'],
    ['ZERO', 'None', 'zero'],
  ];

  function renderTag(state: AuthorityState, label?: string): HTMLElement {
    act(() => root.render(<AuthorityTag state={state} label={label} />));
    return container.querySelector<HTMLElement>('.guidance-authority-tag')!;
  }

  it.each(STATES)('%s shows a sentence-case default word and keeps its data-state and color', (state, word, dataState) => {
    const tag = renderTag(state);
    expect(tag.textContent).toBe(word);
    expect(tag.textContent).not.toBe(state);
    expect(tag.getAttribute('data-state')).toBe(dataState);
    expect(tag.style.color).toBe(stateColor(state));
    expect(stateLabel(state)).toBe(word);
  });

  it('an explicit label still wins over the default word', () => {
    const tag = renderTag('WOULD EVICT', 'Confirm removal');
    expect(tag.textContent).toBe('Confirm removal');
    expect(tag.getAttribute('data-state')).toBe('would-evict');
  });

  it('numberOrUnavailable renders a missing count as lowercase unavailable', () => {
    expect(numberOrUnavailable(null)).toBe('unavailable');
    expect(numberOrUnavailable(0)).toBe('0');
    expect(numberOrUnavailable(7)).toBe('7');
  });
});
