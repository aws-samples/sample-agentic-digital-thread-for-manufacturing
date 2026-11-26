// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Wave 3 / Task 3a — the `vocab_fit` / `concept_proposal` chat checkpoints
 * (ADR-0068): the appendChatCheckpoint contract that carries the two
 * read-only onboarding-tool results into inline chat cards.
 *
 * Load-bearing properties:
 *  - `vocab_fit` renders the PRE-map fit banner (fit_pct / candidate counts /
 *    vocabulary size) and mounts the Context Interview card exactly when the
 *    tool reports questions worth asking OR a non-"ok" computed suggestion;
 *  - `concept_proposal` mounts <ConceptProposalCard/> on the same pipeline;
 *  - error-shaped or malformed results never become cards (the backend hook
 *    additionally guards on fit_pct/proposals — this is the belt half);
 *  - both steps carry honest labels on the checkpoint confirm button.
 * Mirrors onboard-vocab-fit.test.tsx. Generic placeholder data only. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../views/onboard/onboard-queries', () => ({
  getOnboardQueryClient: vi.fn(),
  getCachedConcepts: () => [],
  setConcepts: vi.fn(),
  invalidateOnboardingRemap: vi.fn(async () => {}),
  useConceptsQuery: () => ({ data: [], isError: false, refetch: vi.fn() }),
}));

import { ObFlow } from '../views/onboard/components/ObFlow';
import {
  appendChatCheckpoint,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PACK_GUIDANCE = 'consider importing a domain concept pack';

// ── Guided-endpoint stubs (the Context Interview card fetches on mount) ─────

const STATE_URL = '/api/onboard/guided/state';
const PROBE_URL = '/api/onboard/guided/probe/system-a';

const STATE_LATER = {
  first_launch: false,
  dismissed: false,
  answered_topics: [],
  draft_business_context: '',
};

const PROBE_INITIAL = {
  system_id: 'system-a',
  results: [],
  derived: [],
  questions: [
    {
      id: 'retry-semantics',
      prompt: 'When work is retried, what happens?',
      evidence: 'columns work_state and attempt_no',
    },
  ],
  draft_business_context: '',
};

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function stubGuidedRoutes(): void {
  const routes: Record<string, unknown> = {
    [STATE_URL]: STATE_LATER,
    [PROBE_URL]: PROBE_INITIAL,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const key = Object.keys(routes).find((k) => url.startsWith(k));
      if (!key) throw new Error('unmatched url: ' + url);
      return response(routes[key]);
    }),
  );
}

// ── Fixtures (assess_vocabulary_fit / propose_concepts result shapes) ───────

function fitResult(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    system_id: 'system-a',
    context_questions: 2,
    total_columns: 12,
    columns_with_candidate: 5,
    fit_pct: 42,
    vocabulary_size: 42,
    unmatched_columns: ['work_state', 'attempt_no'],
    generic_columns: ['id', 'created_at'],
    suggestion: 'ask_context',
    ...overrides,
  };
}

const PROPOSAL_RESULT = {
  system_id: 'system-a',
  proposals: [
    {
      id: 'widget-yield',
      name: 'Widget yield',
      description: 'Share of widgets passing final check.',
      action: 'insert',
    },
    {
      id: 'part-count',
      name: 'Part count',
      description: 'Number of parts produced.',
      action: 'skip_exact',
    },
  ],
  insert_count: 1,
  skip_count: 1,
  collision_count: 0,
};

let container: HTMLDivElement;
let root: Root;

async function renderCheckpoint(step: string, result: Record<string, unknown>) {
  appendChatCheckpoint(step, result);
  await act(async () => root.render(<ObFlow />));
  await act(async () => {});
}

beforeEach(() => {
  stubGuidedRoutes();
  resetToWelcome();
  startWizard();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetToWelcome();
  vi.unstubAllGlobals();
});

describe('vocab_fit chat checkpoint', () => {
  it('renders the pre-map fit banner and mounts the interview card on ask_context', async () => {
    await renderCheckpoint('vocab_fit', fitResult());

    const banner = container.querySelector('.ob-vocab-fit');
    expect(banner).not.toBeNull();
    expect(container.textContent).toContain('Vocabulary fit: 42%');
    expect(container.textContent).toContain('5 of 12 columns have a candidate concept');
    expect(container.textContent).toContain('42 concepts in vocabulary');
    // Non-"ok" suggestion → warning tone, but pack guidance only on consider_pack.
    expect(container.querySelector('.ob-vocab-fit-low')).not.toBeNull();
    expect(container.textContent).not.toContain(PACK_GUIDANCE);
    // The interview card mounted and fetched its server-owned question.
    expect(container.querySelector('section[aria-label="Context interview"]')).not.toBeNull();
    expect(container.textContent).toMatch(/when work is retried/i);
    // Honest checkpoint label.
    expect(container.textContent).toContain('Confirm Vocabulary fit →');
  });

  it('adds the domain-pack guidance and still mounts the card on consider_pack', async () => {
    await renderCheckpoint(
      'vocab_fit',
      fitResult({ suggestion: 'consider_pack', context_questions: 0, fit_pct: 10 }),
    );

    expect(container.querySelector('.ob-vocab-fit-low')).not.toBeNull();
    expect(container.textContent).toContain(PACK_GUIDANCE);
    expect(container.textContent).toContain('Admin → Vocabulary');
    expect(container.querySelector('section[aria-label="Context interview"]')).not.toBeNull();
  });

  it('mounts NO interview card when the fit is ok and no questions are worth asking', async () => {
    await renderCheckpoint(
      'vocab_fit',
      fitResult({ suggestion: 'ok', context_questions: 0, fit_pct: 90 }),
    );

    expect(container.querySelector('.ob-vocab-fit')).not.toBeNull();
    expect(container.querySelector('.ob-vocab-fit-low')).toBeNull();
    expect(container.querySelector('section[aria-label="Context interview"]')).toBeNull();
  });

  it('mounts the interview card when the fit is ok but questions remain', async () => {
    await renderCheckpoint(
      'vocab_fit',
      fitResult({ suggestion: 'ok', context_questions: 1, fit_pct: 90 }),
    );

    expect(container.querySelector('section[aria-label="Context interview"]')).not.toBeNull();
  });

  it('hides the banner when a counter is malformed (honest-numbers rule)', async () => {
    await renderCheckpoint(
      'vocab_fit',
      fitResult({ columns_with_candidate: '5' }),
    );

    expect(container.querySelector('.ob-vocab-fit')).toBeNull();
    // The checkpoint itself still renders (fit_pct is a number).
    expect(container.textContent).toContain('Vocabulary Fit');
  });

  it('renders nothing for an error-shaped result (no fit_pct)', async () => {
    await renderCheckpoint('vocab_fit', {
      error: 'No tables discovered yet. Call discover first.',
    });

    expect(container.querySelector('.ob-checkpoint')).toBeNull();
    expect(container.textContent).not.toContain('Vocabulary Fit');
  });
});

describe('concept_proposal chat checkpoint', () => {
  it('mounts <ConceptProposalCard/> with the tool result', async () => {
    await renderCheckpoint('concept_proposal', PROPOSAL_RESULT);

    expect(container.querySelector('section[aria-label="Proposed concepts"]')).not.toBeNull();
    expect(container.textContent).toContain('Widget yield');
    expect(container.textContent).toContain('Part count');
    // Honest checkpoint label.
    expect(container.textContent).toContain('Confirm Proposed concepts →');
  });

  it('renders nothing for an error-shaped result (no proposals key)', async () => {
    await renderCheckpoint('concept_proposal', { error: 'invalid JSON' });

    expect(container.querySelector('section[aria-label="Proposed concepts"]')).toBeNull();
    expect(container.querySelector('.ob-checkpoint')).toBeNull();
  });
});
