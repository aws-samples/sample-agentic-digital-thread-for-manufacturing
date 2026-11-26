// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — Vocabulary artifact surfaces (W4.1, ADR-0050): value-mapping
 * proposals rendered with the IMPORTED SlCards components (type filter,
 * provenance, usage badge), the steward-gate mirror (accept/reject wired and
 * visible for admins only), the user-define assert form's documented body,
 * and the query-guidance editor (base + learned notes, prune via the existing
 * route, propose via the ADR-0050 route) — all against a mocked fetch. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import { queryKeys } from '../api/query-keys';
import type { ProposedMapping } from '../api/types/proposals';
import { AuthProvider } from '../contexts/AuthContext';
import { isValueMappingProposal } from '../pages/admin/admin-queries';
import type { GuidanceSystemWithBase } from '../pages/admin/guidance-queries';
import ArtifactsPanel, {
  missingRequiredFields,
  toUserDefineBody,
} from '../pages/admin/tabs/vocabulary/ArtifactsPanel';
import GuidanceEditor from '../pages/admin/tabs/vocabulary/GuidanceEditor';
import { answerConfirm, confirmDialog, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

installConfirmHost();

const VM_PENDING: ProposedMapping = {
  mapping_id: 'vm-1',
  proposed_type: 'value_mapping',
  status: 'pending',
  confidence: '0.9',
  from_system: 'mes',
  from_field: 'orders.status',
  from_value: 'WIP',
  to_system: 'erp',
  to_field: 'work_orders.state',
  to_value: 'in_progress',
  concept: 'order_status',
  provenance: 'orchestrator',
};

const VM_ACCEPTED: ProposedMapping = {
  mapping_id: 'vm-2',
  proposed_type: 'value_mapping',
  status: 'accepted',
  confidence: '0.8',
  from_system: 'mes',
  from_field: 'orders.priority',
  from_value: 'A',
  to_system: 'erp',
  to_field: 'work_orders.priority',
  to_value: '1',
  concept: 'priority',
  usage_count: '3',
  last_used_at: '2026-07-20T10:00:00Z',
};

/** A join_path row — must be FILTERED OUT of the value-mapping surface. */
const JOIN_PATH: ProposedMapping = {
  mapping_id: 'jp-1',
  proposed_type: 'join_path',
  status: 'pending',
  from_system: 'mes',
  from_field: 'orders.id',
  to_system: 'erp',
  to_field: 'work_orders.mes_id',
};

const MAPPINGS_RESPONSE = { mappings: [VM_PENDING, VM_ACCEPTED, JOIN_PATH], count: 3 };

const GUIDANCE_SYSTEM: GuidanceSystemWithBase = {
  system_id: 'mes',
  base: 'Timestamps are ISO-8601; always filter by plant.',
  notes: ['status column is lowercase', 'join orders on order_id'],
  learned_count: 2,
  length: 128,
  max_len: 4000,
};

const GUIDANCE_RESPONSE = { systems: [GUIDANCE_SYSTEM], count: 1 };

function authMe(isAdmin: boolean) {
  return { user_id: 'u1', email: 'ops@example.com', username: null, is_admin: isAdmin };
}

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
    if (!result) throw new Error(`unexpected URL: ${url} (${init?.method || 'GET'})`);
    return result;
  }));
  return calls;
}

/** Base GET handler: auth (admin flag per test), proposals, guidance. */
function baseHandler(isAdmin: boolean) {
  return (url: string, init?: RequestInit): Response | undefined => {
    if (init?.method && init.method !== 'GET') return undefined;
    if (url === '/api/auth/me') return response(authMe(isAdmin));
    // Both vocabulary queries now ask the server for their own proposal type.
    if (url === '/api/proposed-mappings?proposed_type=value_mapping'
        || url === '/api/proposed-mappings?proposed_type=guidance_note') return response(MAPPINGS_RESPONSE);
    if (url === '/api/query-guidance') return response(GUIDANCE_RESPONSE);
    if (url === '/api/systems') return response({ systems: [] });
    return undefined;
  };
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

async function render(node: ReactNode) {
  const client = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider>{node}</AuthProvider>
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
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

describe('value-mapping cards (imported SlCards)', () => {
  it('renders SlCard for value_mapping rows only, with type chip and usage badge', async () => {
    stubFetch(baseHandler(true));
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-vm-cards')).toBeTruthy());

    // Pending filter (default): the pending value mapping renders as an sl-card.
    const pending = container.querySelector('.sl-card[data-mapping-id="vm-1"]')!;
    expect(pending).toBeTruthy();
    expect(pending.querySelector('.sl-type')!.textContent).toBe('Value Mapping');
    expect(pending.textContent).toContain('orders.status');
    expect(pending.textContent).toContain('via orchestrator');
    // The join_path row is filtered out of this surface entirely.
    expect(container.querySelector('[data-mapping-id="jp-1"]')).toBeNull();

    // Accepted filter: the accepted card renders with the SlCards usage badge.
    await click(container.querySelector('.vocab-vm-filter[data-status="accepted"]'));
    const accepted = container.querySelector('.sl-card[data-mapping-id="vm-2"]')!;
    expect(accepted).toBeTruthy();
    expect(accepted.querySelector('.sl-usage')!.textContent).toContain('used 3×');
  });

  it('isValueMappingProposal treats an absent proposed_type as value_mapping', () => {
    expect(isValueMappingProposal({ mapping_id: 'x' })).toBe(true);
    expect(isValueMappingProposal(JOIN_PATH)).toBe(false);
    expect(isValueMappingProposal(VM_PENDING)).toBe(true);
  });

  it('admin accept/reject post to the existing routes', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/proposed-mappings/vm-1/accept' && init?.method === 'POST') {
        return response({ status: 'accepted' });
      }
      return baseHandler(true)(url, init);
    });
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('[data-mapping-id="vm-1"]')).toBeTruthy());

    await click(container.querySelector('[data-mapping-id="vm-1"] .sl-accept'));
    await vi.waitFor(() =>
      expect(calls.some((c) => c.url === '/api/proposed-mappings/vm-1/accept' && c.init?.method === 'POST')).toBe(true),
    );
    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-vm-action-status')!.textContent).toContain('Accepted'),
    );
  });

  it('non-admin (is_admin=false) hides accept/reject and wires no handlers', async () => {
    const calls = stubFetch(baseHandler(false));
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('[data-mapping-id="vm-1"]')).toBeTruthy());

    // The panel-scoped read-only rule suppresses the imported card's actions.
    const style = container.querySelector('#vocab-artifacts-panel style')!;
    expect(style.textContent).toContain('.sl-card-actions{display:none;}');

    // Defense in depth: no handler is wired — clicking produces NO network call.
    await click(container.querySelector('[data-mapping-id="vm-1"] .sl-accept'));
    await click(container.querySelector('[data-mapping-id="vm-1"] .sl-reject'));
    expect(calls.some((c) => c.url.includes('/accept') || c.url.includes('/reject'))).toBe(false);
  });

  it('admin sees no read-only rule (actions stay visible)', async () => {
    stubFetch(baseHandler(true));
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('[data-mapping-id="vm-1"]')).toBeTruthy());

    const style = container.querySelector('#vocab-artifacts-panel style')!;
    expect(style.textContent).not.toContain('.sl-card-actions{display:none;}');
    expect(container.querySelector('[data-mapping-id="vm-1"] .sl-accept')).toBeTruthy();
  });

  it('shows the identity/authority-rule follow-up banner', async () => {
    stubFetch(baseHandler(true));
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-artifacts-followup')).toBeTruthy());
    expect(container.querySelector('#vocab-artifacts-followup')!.textContent).toContain('follow-ups');
  });
});

describe('assert value mapping (user-define)', () => {
  const FIELD_IDS: Record<string, string> = {
    'vocab-vm-from-system': 'mes',
    'vocab-vm-from-field': 'orders.status',
    'vocab-vm-from-value': 'WIP',
    'vocab-vm-to-system': 'erp',
    'vocab-vm-to-field': 'work_orders.state',
    'vocab-vm-to-value': 'in_progress',
    'vocab-vm-concept': 'order_status',
    'vocab-vm-description': 'Same lifecycle stage',
  };

  it('posts the documented body through POST /api/proposed-mappings/user-define', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/proposed-mappings/user-define' && init?.method === 'POST') {
        return response({ status: 'accepted', mapping_id: 'ud-1' });
      }
      return baseHandler(true)(url, init);
    });
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-vm-assert-form')).toBeTruthy());

    for (const [id, value] of Object.entries(FIELD_IDS)) {
      await type(container.querySelector<HTMLInputElement>(`#${id}`)!, value);
    }
    await click(container.querySelector('#vocab-vm-assert-btn'));

    const post = calls.find((c) => c.url === '/api/proposed-mappings/user-define');
    expect(post).toBeTruthy();
    expect(post!.init?.method).toBe('POST');
    expect(JSON.parse(String(post!.init!.body))).toEqual({
      from_system: 'mes',
      from_field: 'orders.status',
      from_value: 'WIP',
      to_system: 'erp',
      to_field: 'work_orders.state',
      to_value: 'in_progress',
      concept: 'order_status',
      proposed_type: 'value_mapping',
      description: 'Same lifecycle stage',
    });
    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-vm-assert-status')!.textContent).toContain('Saved'),
    );
  });

  it("says a non-steward's assertion is waiting for a steward (pending, not an error)", async () => {
    stubFetch((url, init) => {
      if (url === '/api/proposed-mappings/user-define' && init?.method === 'POST') {
        return response({ status: 'pending', mapping_id: 'ud-2', proposed_type: 'value_mapping', version: 1 });
      }
      return baseHandler(false)(url, init);
    });
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-vm-assert-form')).toBeTruthy());

    for (const [id, value] of Object.entries(FIELD_IDS)) {
      await type(container.querySelector<HTMLInputElement>(`#${id}`)!, value);
    }
    await click(container.querySelector('#vocab-vm-assert-btn'));

    await vi.waitFor(() => {
      const text = container.querySelector('#vocab-vm-assert-status')!.textContent || '';
      expect(text).toContain('steward');
      expect(text).not.toContain('Error');
      expect(text).not.toContain('added to the knowledge graph');
    });
    // The form clears: the proposal was filed.
    expect(container.querySelector<HTMLInputElement>('#vocab-vm-from-system')!.value).toBe('');
  });

  it('blocks a missing required field client-side (no request leaves)', async () => {
    const calls = stubFetch(baseHandler(true));
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-vm-assert-form')).toBeTruthy());

    await type(container.querySelector<HTMLInputElement>('#vocab-vm-from-system')!, 'mes');
    await click(container.querySelector('#vocab-vm-assert-btn'));

    expect(container.querySelector('#vocab-vm-assert-status')!.textContent).toContain('Fill in:');
    expect(calls.some((c) => c.url === '/api/proposed-mappings/user-define')).toBe(false);
  });

  it('renders a server 400 message without throwing', async () => {
    stubFetch((url, init) => {
      if (url === '/api/proposed-mappings/user-define' && init?.method === 'POST') {
        return response({ error: "to_system 'erp' is not an onboarded system" }, 400);
      }
      return baseHandler(true)(url, init);
    });
    await render(<ArtifactsPanel />);
    await vi.waitFor(() => expect(container.querySelector('#vocab-vm-assert-form')).toBeTruthy());

    for (const [id, value] of Object.entries(FIELD_IDS)) {
      await type(container.querySelector<HTMLInputElement>(`#${id}`)!, value);
    }
    await click(container.querySelector('#vocab-vm-assert-btn'));

    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-vm-assert-status')!.textContent).toContain(
        'not an onboarded system',
      ),
    );
  });

  it('toUserDefineBody trims and pins proposed_type; missingRequiredFields mirrors the 400', () => {
    const body = toUserDefineBody({
      from_system: ' mes ',
      from_field: 'orders.status',
      from_value: 'WIP',
      to_system: 'erp',
      to_field: 'work_orders.state',
      to_value: 'in_progress',
      concept: 'order_status',
      description: '',
    });
    expect(body.from_system).toBe('mes');
    expect(body.proposed_type).toBe('value_mapping');
    expect(missingRequiredFields(body)).toEqual([]);
    expect(missingRequiredFields({ ...body, concept: '', to_value: '' })).toEqual([
      'to_value',
      'concept',
    ]);
  });
});

describe('query-guidance editor', () => {
  it('lists base guidance and learned notes per system (imported LgCard)', async () => {
    stubFetch(baseHandler(true));
    await render(<GuidanceEditor />);
    await vi.waitFor(() =>
      expect(container.querySelector('.vocab-guidance-system[data-system-id="mes"]')).toBeTruthy(),
    );

    // Base guidance block.
    expect(container.querySelector('.vocab-guidance-base-text')!.textContent).toContain(
      'Timestamps are ISO-8601',
    );
    // Learned notes rendered by the imported LgCard, with prune controls + meter.
    const notes = Array.from(container.querySelectorAll('.lg-note-text')).map((n) => n.textContent);
    expect(notes).toEqual(['status column is lowercase', 'join orders on order_id']);
    expect(container.querySelectorAll('.lg-prune')).toHaveLength(2);
    expect(container.querySelector('.lg-meter')!.textContent).toContain('128/4000');
  });

  it('prune calls the existing prune route with the exact note', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/query-guidance/mes/prune' && init?.method === 'POST') {
        return response({ status: 'pruned', removed: 1, remaining: 1 });
      }
      return baseHandler(true)(url, init);
    });
    await render(<GuidanceEditor />);
    await vi.waitFor(() => expect(container.querySelectorAll('.lg-prune')).toHaveLength(2));

    await click(container.querySelectorAll('.lg-prune')[0]);
    expect(confirmText()).toEqual({
      title: 'Remove learned note?',
      body: 'This removes the learned note from the system guidance.',
      buttons: ['Cancel', 'Remove note'],
    });
    expect(calls.some((c) => c.url.includes('/prune'))).toBe(false);
    await answerConfirm('confirm');

    const post = calls.find((c) => c.url === '/api/query-guidance/mes/prune');
    expect(post).toBeTruthy();
    expect(JSON.parse(String(post!.init!.body))).toEqual({ note: 'status column is lowercase' });
    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-guidance-action-status')!.textContent).toContain('pruned'),
    );
  });

  it('Clear learned asks first (danger), and only a confirm calls the clear route', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/query-guidance/mes/clear' && init?.method === 'POST') {
        return response({ status: 'cleared', removed: 2 });
      }
      return baseHandler(true)(url, init);
    });
    await render(<GuidanceEditor />);
    await vi.waitFor(() => expect(container.querySelectorAll('.lg-prune')).toHaveLength(2));
    const clearButton = () =>
      Array.from(container.querySelectorAll('button')).find((b) => b.textContent === 'Clear learned')!;

    await click(clearButton());
    expect(confirmText()).toEqual({
      title: 'Remove all learned notes for mes?',
      body: 'The onboarded base guidance is kept.',
      buttons: ['Cancel', 'Remove all notes'],
    });
    expect(confirmDialog()!.querySelector('.confirm-danger-btn')).not.toBeNull();
    await answerConfirm('cancel');
    expect(calls.some((c) => c.url.includes('/clear'))).toBe(false);

    await click(clearButton());
    await answerConfirm('confirm');
    await vi.waitFor(() =>
      expect(container.querySelector('#vocab-guidance-action-status')!.textContent).toContain('Cleared 2 learned notes'),
    );
    expect(calls.filter((c) => c.url === '/api/query-guidance/mes/clear')).toHaveLength(1);
  });

  it('propose posts the note to the ADR-0050 propose route', async () => {
    const calls = stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/guidance/mes/propose' && init?.method === 'POST') {
        return response({ status: 'pending', mapping_id: 'g-1' });
      }
      return baseHandler(true)(url, init);
    });
    await render(<GuidanceEditor />);
    await vi.waitFor(() => expect(container.querySelector('.vocab-guidance-note-input')).toBeTruthy());

    await type(
      container.querySelector<HTMLInputElement>('.vocab-guidance-note-input')!,
      'quantities are in base units',
    );
    await click(container.querySelector('.vocab-guidance-propose-btn'));

    const post = calls.find((c) => c.url === '/api/admin/vocabulary/guidance/mes/propose');
    expect(post).toBeTruthy();
    expect(post!.init?.method).toBe('POST');
    expect(JSON.parse(String(post!.init!.body))).toEqual({ note: 'quantities are in base units' });
    await vi.waitFor(() =>
      expect(container.querySelector('.vocab-guidance-propose-status')!.textContent).toBe(
        'Proposed. It is waiting in the Guidance delivery queue above: choose Preview delivery, then Deliver.',
      ),
    );
  });

  it('a proposed note appears in the delivery queue without a reload (queue query is invalidated)', async () => {
    // The queue is GET /api/proposed-mappings filtered to guidance_note. It starts
    // empty; after the propose POST the server would include the new pending
    // note, so the stub flips its response once the POST has happened.
    let proposed = false;
    const note = 'Holds only the archived February week; live telemetry is in SiteWise.';
    const GUIDANCE_NOTE: ProposedMapping = {
      mapping_id: 'g-1',
      proposed_type: 'guidance_note',
      status: 'pending',
      from_system: 'mes',
      from_field: '',
      to_system: '',
      to_field: '',
      to_value: note,
    };
    const calls = stubFetch((url, init) => {
      if (url === '/api/admin/vocabulary/guidance/mes/propose' && init?.method === 'POST') {
        proposed = true;
        return response({ status: 'pending', mapping_id: 'g-1' });
      }
      if (url === '/api/proposed-mappings?proposed_type=guidance_note' && (!init?.method || init.method === 'GET')) {
        return response(proposed
          ? { mappings: [...MAPPINGS_RESPONSE.mappings, GUIDANCE_NOTE], count: 4 }
          : MAPPINGS_RESPONSE);
      }
      return baseHandler(true)(url, init);
    });
    await render(<GuidanceEditor />);
    const queue = () => container.querySelector('[aria-label="Pending guidance delivery"]');
    await vi.waitFor(() => expect(queue()?.querySelector('.guidance-authority-tag')?.getAttribute('data-state')).toBe('zero'));
    expect(queue()?.querySelector('.guidance-authority-tag')?.textContent).toBe('None');

    await type(container.querySelector<HTMLInputElement>('.vocab-guidance-note-input')!, note);
    await click(container.querySelector('.vocab-guidance-propose-btn'));

    // Without the invalidation the queue kept its cached empty list (30 s
    // staleTime, no polling) and read None until a page reload.
    await vi.waitFor(() => expect(queue()?.querySelector('.guidance-authority-tag')?.getAttribute('data-state')).toBe('pending'));
    expect(queue()?.querySelector('.guidance-authority-tag')?.textContent).toBe('Pending');
    expect(queue()?.textContent).toContain(note);
    const proposalFetches = calls.filter((c) => c.url === '/api/proposed-mappings?proposed_type=guidance_note');
    expect(proposalFetches.length).toBeGreaterThanOrEqual(2);
  });

  describe('the propose status line follows the proposal', () => {
    const WAITING =
      'Proposed. It is waiting in the Guidance delivery queue above: choose Preview delivery, then Deliver.';
    const note = 'Quantities are in base units.';

    /** A server holding one guidance note after the propose POST; `noteStatus`
     * (or `null`: the row has left the list) is what the list serves next. */
    function proposeServer(state: { noteStatus: string | null }) {
      let proposed = false;
      return stubFetch((url, init) => {
        const method = init?.method || 'GET';
        if (url === '/api/admin/vocabulary/guidance/mes/propose' && method === 'POST') {
          proposed = true;
          return response({ status: 'pending', mapping_id: 'g-7' });
        }
        if (url === '/api/proposed-mappings?proposed_type=guidance_note' && method === 'GET') {
          const row: ProposedMapping = {
            mapping_id: 'g-7',
            proposed_type: 'guidance_note',
            status: state.noteStatus ?? 'pending',
            from_system: 'mes',
            to_value: note,
          };
          return response({ mappings: proposed && state.noteStatus ? [row] : [] });
        }
        if (url.endsWith('/guidance-preview')) {
          return response({
            status: 'preview',
            mapping_id: 'g-7',
            preview: {
              outcome: 'append',
              current_digest: 'sha256:current',
              current_length: 128,
              note_digest: 'sha256:note',
              note_length: note.length,
              scope: 'query_hint',
              result_digest: 'sha256:result',
              result_length: 160,
              evicted_note_digests: [],
            },
          });
        }
        if (url === '/api/proposed-mappings/g-7/accept' && method === 'POST') {
          state.noteStatus = 'accepted';
          return response({ status: 'accepted', outcome: 'append', proposed_type: 'guidance_note', version: 3 });
        }
        if (url === '/api/query-guidance/reconciliation') {
          return response({
            status: 'ok',
            counts: { accepted: 0, graph: 0, delivered: 0 },
            sources: { accepted: 'ok', graph: 'ok', delivery: 'ok' },
            mismatches: [],
            mismatch_count: 0,
            truncated: false,
          });
        }
        return baseHandler(true)(url, init);
      });
    }

    const statusLine = () => container.querySelector('.vocab-guidance-propose-status')!.textContent;
    const cardButton = (name: string) =>
      [...container.querySelectorAll<HTMLButtonElement>('.guidance-proposal button')]
        .find((button) => button.textContent?.trim() === name) ?? null;

    async function proposeNote() {
      await vi.waitFor(() => expect(container.querySelector('.vocab-guidance-note-input')).toBeTruthy());
      await type(container.querySelector<HTMLInputElement>('.vocab-guidance-note-input')!, note);
      await click(container.querySelector('.vocab-guidance-propose-btn'));
      await vi.waitFor(() => expect(container.querySelector('.guidance-proposal')).toBeTruthy());
      await vi.waitFor(() => expect(statusLine()).toBe(WAITING));
    }

    it('reads Delivered. after the steward delivers it from the queue', async () => {
      const state = { noteStatus: 'pending' as string | null };
      proposeServer(state);
      await render(<GuidanceEditor />);
      await proposeNote();

      await click(cardButton('Preview delivery'));
      await vi.waitFor(() => expect(cardButton('Deliver')).toBeTruthy());
      await click(cardButton('Deliver'));

      await vi.waitFor(() => expect(statusLine()).toBe('Delivered.'));
      expect(container.textContent).not.toContain(WAITING);
    });

    it('clears the waiting line once the proposals list no longer holds the note', async () => {
      const state = { noteStatus: 'pending' as string | null };
      proposeServer(state);
      const client = await render(<GuidanceEditor />);
      await proposeNote();

      state.noteStatus = null;
      await act(async () => {
        await client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyGuidanceProposals() });
      });

      await vi.waitFor(() => expect(statusLine()).toBe(''));
      expect(container.textContent).not.toContain(WAITING);
    });

    it('clears the waiting line when the note is rejected rather than delivered', async () => {
      const state = { noteStatus: 'pending' as string | null };
      proposeServer(state);
      const client = await render(<GuidanceEditor />);
      await proposeNote();

      state.noteStatus = 'rejected';
      await act(async () => {
        await client.invalidateQueries({ queryKey: queryKeys.admin.vocabularyGuidanceProposals() });
      });

      await vi.waitFor(() => expect(statusLine()).toBe(''));
      expect(container.textContent).not.toContain('Delivered.');
    });
  });

  it('propose with an empty note is blocked client-side', async () => {
    const calls = stubFetch(baseHandler(true));
    await render(<GuidanceEditor />);
    await vi.waitFor(() => expect(container.querySelector('.vocab-guidance-propose-btn')).toBeTruthy());

    await click(container.querySelector('.vocab-guidance-propose-btn'));
    expect(container.querySelector('.vocab-guidance-propose-status')!.textContent).toContain(
      'Enter a note',
    );
    expect(calls.some((c) => c.url.endsWith('/propose'))).toBe(false);
  });

  it('non-admin hides prune/clear and wires no handlers; propose stays open', async () => {
    const calls = stubFetch(baseHandler(false));
    await render(<GuidanceEditor />);
    await vi.waitFor(() => expect(container.querySelectorAll('.lg-prune')).toHaveLength(2));

    const style = container.querySelector('#vocab-guidance-editor style')!;
    expect(style.textContent).toContain('.lg-prune');
    expect(style.textContent).toContain('display:none');

    await click(container.querySelectorAll('.lg-prune')[0]);
    expect(confirmDialog()).toBeNull();
    expect(calls.some((c) => c.url.includes('/prune'))).toBe(false);

    // The propose affordance remains available (open lifecycle).
    expect(container.querySelector('.vocab-guidance-propose-btn')).toBeTruthy();
  });
});
