// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — review-queue panel + download-draft flow (ADR-0062
 * D3/D4/D5). Pinned here:
 *
 *   - the machine pre-score renders its LITERAL "triage-only" label and no
 *     verdict/score/confidence of any kind (D4);
 *   - decision actions are the only mutation, and a 400 rejection surfaces the
 *     server's reason WITHOUT echoing the note (D4/D9);
 *   - the draft affordance is labelled "Download draft", its copy names an
 *     EXTERNAL customer-owned destination, never names the packaged demo
 *     golden file or pasting into this repository, triggers a real file
 *     download, and leaves NO durable browser state holding the draft (D5);
 *   - `source_expired` renders its own honest state and cannot be drafted;
 *   - NO golden-subset control appears in the queue panel — the run-ALL pin
 *     stays scoped to the golden-suite affordance in EvalsTab.
 *
 * Hostile strings in question/answer/comment/note text stay inert literal
 * text (repo sink policy) — all against a mocked fetch, no network. */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import type { EvalQueueItem, EvalQueueResponse } from '../api/types/admin';
import { downloadGoldenDraftMutationOptions } from '../pages/admin/admin-queries';
import ReviewQueue from '../pages/admin/tabs/evals/ReviewQueue';
import {
  draftFilename,
  isDraftable,
  prescoreText,
  queueCountsText,
  rejectionOf,
  rejectionText,
} from '../pages/admin/tabs/evals/queue-logic';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const QUEUE_URL = '/api/admin/evals/queue';
const DECISION_URL = '/api/admin/evals/queue/decision';
const DRAFT_URL = '/api/admin/evals/queue/golden-draft';

const FEEDBACK_REF = 'feedback:fb-' + 'a'.repeat(32);
const GOLDEN_REF = 'golden-failure:run-2:q-cross-system';
const EXPIRED_REF = 'feedback:fb-' + 'b'.repeat(32);

// Hostile strings — the sink-policy pin: queue text is DATA, never markdown.
const HOSTILE_QUESTION = '<img src=x onerror="window.__pwned=1"> which batch failed?';
const HOSTILE_ANSWER = '<script>window.__pwned2=1</script> **not markdown**';
const HOSTILE_COMMENT = '<iframe src="javascript:window.__pwned3=1"></iframe> wrong batch';

const UNDECIDED: EvalQueueItem = {
  source_ref: FEEDBACK_REF,
  ref_sha: '0123456789abcdef',
  source: 'feedback',
  occurred_at: '2026-08-17T10:00:00Z',
  sentiment: 'negative',
  question: HOSTILE_QUESTION,
  answer_excerpt: HOSTILE_ANSWER,
  user_comment: HOSTILE_COMMENT,
  cited_systems: ['erp'],
  machine_prescore: {
    label: 'triage-only',
    signals: ['no-cited-rules', 'user-comment-present', 'user-flagged-negative'],
  },
  decision: null,
  source_expired: false,
};

const DECIDED: EvalQueueItem = {
  source_ref: GOLDEN_REF,
  ref_sha: 'fedcba9876543210',
  source: 'golden-failure',
  occurred_at: '2026-08-16T09:00:00Z',
  run_id: 'run-2',
  question_id: 'q-cross-system',
  run_path: 'server',
  answer_excerpt: 'No data available for that batch.',
  failures: ['systems_queried 1 < systems_min 2'],
  machine_prescore: { label: 'triage-only', signals: ['golden-verdict-fail', 'no-neptune-query'] },
  decision: {
    verdict: 'incorrect',
    taxonomy_tags: ['routing_gap'],
    decided_at: '2026-08-16T10:00:00Z',
    decided_by: 'steward@example.com',
    ref_sha: 'fedcba9876543210',
  },
  source_expired: false,
};

const EXPIRED: EvalQueueItem = {
  source_ref: EXPIRED_REF,
  ref_sha: 'aaaabbbbccccdddd',
  source: 'feedback',
  occurred_at: null,
  question: null,
  answer_excerpt: null,
  machine_prescore: { label: 'triage-only', signals: [] },
  decision: {
    verdict: 'needs_context',
    taxonomy_tags: [],
    decided_at: '2026-06-01T10:00:00Z',
    decided_by: 'steward@example.com',
  },
  source_expired: true,
};

const QUEUE: EvalQueueResponse = {
  items: [UNDECIDED, DECIDED, EXPIRED],
  window_days: 14,
  limit: 100,
  counts: { total: 3, decided: 2, undecided: 1, source_expired: 1 },
};

const DRAFT_BUNDLE = {
  version: '1',
  questions: [
    {
      id: 'promoted-fedcba9876543210',
      text: 'which batch failed?',
      expect: {
        systems_min: 1,
        neptune_min: 0,
        rules_applied_min: 0,
        answer_must_not_match: ['(?i)as an ai'],
        absence_honesty: false,
        injection_guard: false,
      },
      hints: { observed_systems: 1 },
      provenance: { source: 'promoted', source_ref_sha: 'fedcba9876543210' },
    },
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
    if (url === QUEUE_URL && (!init?.method || init.method === 'GET')) return response(QUEUE);
    throw new Error(`unexpected URL: ${url} (${init?.method || 'GET'})`);
  }));
  return calls;
}

let container: HTMLDivElement;
let root: Root;
let downloads: { name: string; href: string }[];
let storageWrites: { key: string; value: string }[];

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  downloads = [];
  storageWrites = [];
  // Capture every download click and every storage write — the D5 pins.
  Object.assign(URL, {
    createObjectURL: vi.fn(() => 'blob:draft'),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click(this: HTMLAnchorElement) {
    downloads.push({ name: this.download, href: this.href });
  });
  for (const store of ['localStorage', 'sessionStorage'] as const) {
    vi.stubGlobal(store, {
      getItem: () => null,
      setItem: (key: string, value: string) => void storageWrites.push({ key, value }),
      removeItem: () => {},
    });
  }
  vi.stubGlobal('indexedDB', { open: vi.fn(() => { throw new Error('indexedDB is not used'); }) });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (URL as unknown as Record<string, unknown>).createObjectURL;
  delete (URL as unknown as Record<string, unknown>).revokeObjectURL;
});

async function renderQueue() {
  const client = createQueryClient();
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <ReviewQueue />
      </QueryClientProvider>,
    );
    await Promise.resolve();
  });
  // The queue read settles after the render tick; `vi.waitFor` polls until the
  // computed items are on screen (the EvalsTab.test convention).
  await vi.waitFor(() =>
    expect(container.querySelectorAll('.evals-queue-item').length).toBe(3),
  );
  return client;
}

function card(sourceRef: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(`[data-source-ref="${sourceRef}"]`);
  expect(el).not.toBeNull();
  return el!;
}

async function click(el: Element | null) {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await Promise.resolve();
  });
}

const setTextareaValue = Object.getOwnPropertyDescriptor(
  HTMLTextAreaElement.prototype,
  'value',
)!.set!;

async function type(el: HTMLTextAreaElement, value: string) {
  await act(async () => {
    setTextareaValue.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await Promise.resolve();
  });
}

describe('machine pre-score is triage-only (ADR-0062 D4)', () => {
  it('renders the literal "triage-only" label with its signal ids', async () => {
    stubFetch(() => undefined);
    await renderQueue();

    const prescore = card(FEEDBACK_REF).querySelector('.evals-queue-prescore')!;
    expect(prescore.textContent).toContain('triage-only');
    expect(prescore.textContent).toContain('user-flagged-negative');
    // Exactly the label plus the server's signals — nothing synthesised.
    expect(prescore.textContent).toBe(
      'triage-only · no-cited-rules, user-comment-present, user-flagged-negative',
    );
  });

  it('never renders a verdict, score or confidence from the pre-score', async () => {
    stubFetch(() => undefined);
    await renderQueue();

    for (const prescore of Array.from(container.querySelectorAll('.evals-queue-prescore'))) {
      const text = prescore.textContent ?? '';
      expect(text).toContain('triage-only');
      // No steward verdict word, no numeric/percentage score, no confidence.
      expect(text).not.toMatch(/\b(correct|incorrect|needs_context)\b/);
      expect(text).not.toMatch(/confidence|probability|likelihood/i);
      expect(text).not.toMatch(/\d/);
      expect(text).not.toContain('%');
    }
    // An absent signal list still carries the label — never a blank verdict.
    expect(card(EXPIRED_REF).querySelector('.evals-queue-prescore')!.textContent).toBe(
      'triage-only · no signals',
    );
    // The pre-score block is distinct from the steward decision block.
    const decided = card(GOLDEN_REF);
    expect(decided.querySelector('.evals-queue-decision')!.textContent).toContain('incorrect');
    expect(decided.querySelector('.evals-queue-prescore')!.textContent).not.toContain('incorrect');
  });
});

describe('steward decisions are the only mutation', () => {
  it('POSTs {source_ref, verdict, note} and nothing else', async () => {
    const calls = stubFetch((url, init) => {
      if (url === DECISION_URL && init?.method === 'POST') {
        return response({ status: 'recorded', decision: { verdict: 'correct' } });
      }
      return undefined;
    });
    await renderQueue();

    await type(
      card(FEEDBACK_REF).querySelector<HTMLTextAreaElement>('.evals-queue-note')!,
      '  routing looks wrong  ',
    );
    await click(card(FEEDBACK_REF).querySelector('[data-verdict="correct"]'));

    const posts = calls.filter((c) => c.init?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe(DECISION_URL);
    expect(JSON.parse(String(posts[0].init!.body))).toEqual({
      source_ref: FEEDBACK_REF,
      verdict: 'correct',
      note: 'routing looks wrong',
    });
    // All three verdicts are offered — and only those three.
    const verdicts = Array.from(card(FEEDBACK_REF).querySelectorAll('.evals-queue-verdict-btn'))
      .map((btn) => btn.getAttribute('data-verdict'));
    expect(verdicts).toEqual(['correct', 'incorrect', 'needs_context']);
  });

  it('shows a 400 rejection reason WITHOUT echoing the note', async () => {
    const secretish = 'AKIAIOSFODNN7EXAMPLE token pasted by mistake';
    stubFetch((url, init) => {
      if (url === DECISION_URL && init?.method === 'POST') {
        return response(
          {
            detail: {
              reason: 'secret_like_content',
              detail: 'the steward note looks like a credential — remove it and retry',
            },
          },
          400,
        );
      }
      return undefined;
    });
    await renderQueue();

    await type(card(FEEDBACK_REF).querySelector<HTMLTextAreaElement>('.evals-queue-note')!, secretish);
    await click(card(FEEDBACK_REF).querySelector('[data-verdict="incorrect"]'));

    const status = await vi.waitFor(() => {
      const el = card(FEEDBACK_REF).querySelector('.evals-queue-item-status')!;
      expect(el.textContent).toContain('secret_like_content');
      return el;
    });
    expect(status.textContent).toContain('looks like a credential');
    // The rejected note is NEVER part of the rendered rejection.
    expect(status.textContent).not.toContain('AKIA');
  });
});

describe('download draft (ADR-0062 D5)', () => {
  it('is labelled Download, names an EXTERNAL customer-owned destination, and never mentions the packaged golden file', async () => {
    stubFetch(() => undefined);
    await renderQueue();

    const draftBtn = card(GOLDEN_REF).querySelector('.evals-queue-draft-btn')!;
    expect(draftBtn.textContent).toContain('Download draft');
    expect(draftBtn.textContent).not.toMatch(/copy/i);

    const copy = (container.textContent ?? '') + ' ' + String(draftBtn.getAttribute('title'));
    expect(copy).toMatch(/EXTERNAL/i);
    expect(copy).toMatch(/customer-owned/i);
    expect(copy).toMatch(/outside this repository/i);
    // The boundary strings that must NEVER appear in this panel.
    expect(copy).not.toContain('golden_evals.json');
    expect(copy).not.toMatch(/paste/i);
  });

  it('POSTs {source_ref}, triggers a real file download with an opaque name, and keeps NO durable browser state', async () => {
    const calls = stubFetch((url, init) => {
      if (url === DRAFT_URL && init?.method === 'POST') return response(DRAFT_BUNDLE);
      return undefined;
    });
    await renderQueue();

    await click(card(GOLDEN_REF).querySelector('.evals-queue-draft-btn'));

    const posts = calls.filter((c) => c.init?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe(DRAFT_URL);
    expect(JSON.parse(String(posts[0].init!.body))).toEqual({ source_ref: GOLDEN_REF });

    // A real download fired, named from the OPAQUE sha only.
    await vi.waitFor(() => expect(downloads).toHaveLength(1));
    expect(downloads[0].name).toBe('golden-draft-fedcba9876543210.json');
    expect(downloads[0].name).not.toContain('which batch');
    expect(URL.revokeObjectURL).toHaveBeenCalled();

    // NOTHING durable holds the draft: no storage write at all, and the
    // draft text is nowhere in the DOM.
    expect(storageWrites).toEqual([]);
    expect(container.textContent).not.toContain('answer_must_not_match');
    expect(container.textContent).not.toContain('promoted-fedcba9876543210');
  });

  it('holds the draft in NO cache: the download mutation is configured gcTime 0', () => {
    // Query/mutation cache retention is the durable-state seam that a spy
    // cannot see — pin the option itself (ADR-0062 D5).
    expect(downloadGoldenDraftMutationOptions().gcTime).toBe(0);
  });

  it('surfaces a 409 reason and offers no draft for an undecided item', async () => {
    stubFetch((url, init) => {
      if (url === DRAFT_URL && init?.method === 'POST') {
        return response(
          { detail: { reason: 'no_question_text', detail: 'this source carries no promotable question text' } },
          409,
        );
      }
      return undefined;
    });
    await renderQueue();

    // Undecided ⇒ the draft affordance is disabled with honest copy.
    const undecided = card(FEEDBACK_REF);
    expect(undecided.querySelector<HTMLButtonElement>('.evals-queue-draft-btn')!.disabled).toBe(true);
    expect(undecided.textContent).toContain('Record a decision first');

    await click(card(GOLDEN_REF).querySelector('.evals-queue-draft-btn'));
    await vi.waitFor(() =>
      expect(card(GOLDEN_REF).querySelector('.evals-queue-item-status')!.textContent).toContain(
        'no_question_text',
      ),
    );
    expect(downloads).toEqual([]);
  });
});

describe('source_expired renders its own honest state', () => {
  it('badges the item, explains the retention window, keeps the decision, and disables the draft', async () => {
    stubFetch(() => undefined);
    await renderQueue();

    const expired = card(EXPIRED_REF);
    expect(expired.getAttribute('data-source-expired')).toBe('true');
    expect(expired.querySelector('.evals-queue-expired-badge')!.textContent).toBe('source expired');
    expect(expired.querySelector('.evals-queue-expired')!.textContent).toContain('retention window');
    // The steward's decision survives the source.
    expect(expired.querySelector('.evals-queue-decision')!.textContent).toContain('needs_context');
    expect(expired.querySelector<HTMLButtonElement>('.evals-queue-draft-btn')!.disabled).toBe(true);
    // No text is invented for an expired source.
    expect(expired.querySelector('.evals-queue-question')).toBeNull();
    expect(expired.querySelector('.evals-queue-answer')).toBeNull();
    // The counts line reports the state honestly.
    expect(container.querySelector('#evals-queue-summary')!.textContent).toContain(
      '1 source expired',
    );
    // A live item is NOT badged expired.
    expect(card(GOLDEN_REF).getAttribute('data-source-expired')).toBe('false');
  });
});

describe('sink policy + no golden-subset control', () => {
  it('renders hostile question/answer/comment text as inert literal text', async () => {
    stubFetch(() => undefined);
    await renderQueue();

    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('iframe')).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
    expect((window as { __pwned2?: number }).__pwned2).toBeUndefined();
    expect((window as { __pwned3?: number }).__pwned3).toBeUndefined();

    const item = card(FEEDBACK_REF);
    expect(item.querySelector('.evals-queue-question')!.textContent).toBe(HOSTILE_QUESTION);
    expect(item.querySelector('.evals-queue-answer')!.textContent).toBe(HOSTILE_ANSWER);
    expect(item.querySelector('.evals-queue-comment')!.textContent).toContain(HOSTILE_COMMENT);
  });

  it('offers no golden-subset control: no count/category/question-id selection anywhere', async () => {
    stubFetch(() => undefined);
    await renderQueue();

    expect(container.querySelectorAll('select')).toHaveLength(0);
    expect(container.querySelectorAll('input')).toHaveLength(0);
    for (const el of Array.from(container.querySelectorAll('input, button, select, textarea'))) {
      const idName = `${el.id} ${(el as HTMLButtonElement).name ?? ''}`;
      expect(idName).not.toMatch(/count|category|question[-_]?ids?\b/i);
    }
    // The panel never offers to run the golden suite — that affordance is the
    // EvalsTab run-ALL button and stays distinct.
    expect(container.textContent).not.toMatch(/run (the )?(full )?golden/i);
  });
});

describe('queue-logic (pure)', () => {
  it('formats pre-scores, counts, filenames and rejections without inventing verdicts', () => {
    expect(prescoreText(undefined)).toBe('triage-only · no signals');
    expect(prescoreText({ signals: ['golden-verdict-fail'] })).toBe(
      'triage-only · golden-verdict-fail',
    );
    expect(queueCountsText(undefined)).toBe('—');
    expect(queueCountsText({ total: 2, decided: 1, undecided: 1, source_expired: 0 })).toBe(
      '2 in window · 1 decided · 1 undecided · 0 source expired',
    );

    // Filename: opaque sha from the draft, else the item's ref_sha, else fixed.
    expect(draftFilename(DRAFT_BUNDLE)).toBe('golden-draft-fedcba9876543210.json');
    expect(draftFilename({ questions: [{ text: 'q' }] }, '0123456789abcdef')).toBe(
      'golden-draft-0123456789abcdef.json',
    );
    expect(draftFilename({}, 'not-a-sha')).toBe('golden-draft.json');

    // Draftability mirrors the server's 409 states.
    expect(isDraftable(UNDECIDED)).toBe(false);
    expect(isDraftable(EXPIRED)).toBe(false);
    expect(isDraftable(DECIDED)).toBe(true);

    // Rejections parse to the pinned pair; plain text passes through.
    expect(rejectionOf(new Error('{"reason":"too_long","detail":"note is too long"}'))).toEqual({
      reason: 'too_long',
      detail: 'note is too long',
    });
    expect(rejectionText(new Error('Admin access required'))).toBe('Admin access required');
  });
});
