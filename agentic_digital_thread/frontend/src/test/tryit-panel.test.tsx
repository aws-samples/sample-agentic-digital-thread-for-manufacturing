// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — try-it validation drawer (TryItPanel.tsx, B2 task 3.2):
 * (1) cost warning renders BEFORE launch — nothing is POSTed until the
 *     steward explicitly clicks Run;
 * (2) post → poll → diff: 202 {job_id} → GET running → GET done renders the
 *     side-by-side baseline/candidate columns (systems chips, rules with
 *     candidate-only highlight, translation steps, answer excerpts) and the
 *     routing-delta strip;
 * (3) 409 renders the "already running" message (ONE concurrent job);
 * (4) an error job renders the honest error state (never a fabricated diff);
 * (5) polling stops on done — no further GETs after the terminal poll;
 * (6) SlCard Try-it button is steward-visible only (/api/auth/me is_admin)
 *     and never renders on insight cards. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, requestJson } from '../api/client';
import type { TryItResult } from '../api/types/learning';
import type { ProposedMapping } from '../api/types/proposals';
import { SlCard } from '../views/architecture/SlCards';
import { _resetStewardCheck, TryItPanel } from '../views/architecture/TryItPanel';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

const requestJsonMock = vi.mocked(requestJson);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MAPPING: ProposedMapping = {
  mapping_id: 'm-try-1',
  proposed_type: 'join_path',
  status: 'pending',
  confidence: '0.8',
  from_system: 'mes-a',
  from_field: 'work_orders.operator_id',
  to_system: 'mes-b',
  to_field: 'runs.operator_id',
  query_context: 'Which work orders touch the flagged operator?',
};

const RESULT: TryItResult = {
  question: 'Which work orders touch the flagged operator?',
  baseline: {
    systems_queried: ['mes-a'],
    rules_applied: [{ id: 'jp-shared', label: 'shared join mes-a↔cmms' }],
    translation_steps: ['resolve operator via cmms'],
    answer_excerpt: 'baseline answer text',
    elapsed_seconds: 118.2,
    injected: false,
  },
  candidate: {
    systems_queried: ['mes-a', 'mes-b'],
    rules_applied: [
      { id: 'jp-shared', label: 'shared join mes-a↔cmms' },
      { id: 'jp-cand', label: 'candidate operator join' },
    ],
    translation_steps: ['resolve operator via cmms', 'join mes-b runs directly'],
    answer_excerpt: 'candidate answer text',
    elapsed_seconds: 121.4,
    injected: true,
  },
  diff: {
    systems_added: ['mes-b'],
    systems_removed: [],
    rules_added: [{ id: 'jp-cand', label: 'candidate operator join' }],
    rules_removed: [],
  },
  candidate_injected: true,
};

/** X-4 shape: the candidate could not be injected — both runs are the same
 * seed, the diff is empty, and diff.note carries the server's honesty. */
const NOT_INJECTED_RESULT: TryItResult = {
  ...RESULT,
  candidate: { ...RESULT.baseline, injected: false },
  diff: {
    systems_added: [],
    systems_removed: [],
    rules_added: [],
    rules_removed: [],
    note: 'candidate not injectable in this environment',
  },
  candidate_injected: false,
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  requestJsonMock.mockReset();
  _resetStewardCheck();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function render(el: React.ReactElement): Promise<void> {
  await act(async () => {
    root.render(el);
  });
}

async function clickAsync(el: Element | null): Promise<void> {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).click();
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('TryItPanel: launch and polling', () => {
  it('post_then_poll_renders_diff — cost warning first, 202 → running → done diff', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j1' }) // POST
      .mockResolvedValueOnce({ status: 'running' }) // poll 1
      .mockResolvedValueOnce({ status: 'done', result: RESULT }); // poll 2

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);

    // Cost warning renders BEFORE anything launches — no fetch yet.
    const warning = container.querySelector('.tryit-warning');
    expect(warning?.textContent).toContain('one real question twice');
    expect(warning?.textContent).toContain('about 2 minutes');
    expect(requestJsonMock).not.toHaveBeenCalled();

    await clickAsync(container.querySelector('.tryit-launch'));
    expect(requestJsonMock).toHaveBeenCalledWith(
      '/api/admin/learning/tryit',
      expect.objectContaining({
        method: 'POST',
        // The pre-filled question rides the launch payload.
        body: JSON.stringify({
          mapping_id: 'm-try-1',
          question: 'Which work orders touch the flagged operator?',
        }),
      }),
    );
    expect(container.querySelector('.tryit-running')?.textContent).toContain(
      'takes a few minutes',
    );

    // First 5 s poll: still running.
    await advance(5_000);
    expect(requestJsonMock).toHaveBeenCalledWith('/api/admin/learning/tryit/j1');
    expect(container.querySelector('.tryit-running')).not.toBeNull();

    // Second poll: done — side-by-side diff renders.
    await advance(5_000);
    expect(container.querySelector('.tryit-running')).toBeNull();
    expect(container.querySelector('.tryit-question')?.textContent).toContain(
      'Which work orders touch the flagged operator?',
    );
    const cols = container.querySelectorAll('.tryit-col');
    expect(cols).toHaveLength(2);
    // Baseline column: its system chip + excerpt.
    expect(cols[0].textContent).toContain('Baseline');
    expect(cols[0].querySelectorAll('.sl-chip')).toHaveLength(1);
    expect(cols[0].textContent).toContain('baseline answer text');
    // Candidate column: both systems, candidate-only rule highlighted.
    expect(cols[1].querySelectorAll('.sl-chip')).toHaveLength(2);
    const highlighted = cols[1].querySelectorAll('.tryit-rule--new');
    expect(highlighted).toHaveLength(1);
    expect(highlighted[0].textContent).toBe('candidate operator join');
    expect(cols[1].textContent).toContain('join mes-b runs directly');
    expect(cols[1].textContent).toContain('candidate answer text');
    // Diff strip: server-computed routing delta. Rule deltas are {id,label}
    // objects (X-1) — label text renders, the id rides the title attr, and
    // objects are NEVER string-joined.
    const strip = container.querySelector('.tryit-diff-strip');
    // Systems read through the shared resolver (no catalog here: the
    // id-derived fallback, never the raw id).
    expect(strip?.textContent).toContain('＋ systems: MES B');
    expect(strip?.textContent).toContain('＋ rules: candidate operator join');
    expect(strip?.textContent).not.toContain('[object Object]');
    const diffRule = strip?.querySelector('.tryit-diff-rule');
    expect(diffRule?.textContent).toBe('candidate operator join');
    expect(diffRule?.getAttribute('title')).toBe('jp-cand');
    // Injected normally — no not-injected banner (X-4).
    expect(container.querySelector('.tryit-notinjected')).toBeNull();
  });

  it('test_409_message — a concurrent job renders the already-running state', async () => {
    requestJsonMock.mockRejectedValueOnce(
      new ApiError(409, '/api/admin/learning/tryit', 'try-it already running'),
    );

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));

    const busy = container.querySelector('.tryit-busy');
    expect(busy?.textContent).toContain('A try-it is already running');
    expect(container.querySelector('.tryit-running')).toBeNull();
  });

  it('test_error_job_renders_error_state — job status error surfaces honestly', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j2' })
      .mockResolvedValueOnce({ status: 'error', error: 'runtime exploded' });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);

    const error = container.querySelector('.tryit-error');
    expect(error?.textContent).toContain('Try-it failed:');
    expect(error?.textContent).toContain('runtime exploded');
    expect(container.querySelector('.tryit-cols')).toBeNull(); // never a fabricated diff
  });

  it('test_error_detail_preferred — X-5: server error_detail renders as plain text', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j6' })
      .mockResolvedValueOnce({
        status: 'error',
        error: 'TimeoutError',
        error_detail: 'try-it run exceeded 240s wall clamp',
      });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);

    const error = container.querySelector('.tryit-error');
    expect(error?.textContent).toContain('try-it run exceeded 240s wall clamp');
    expect(error?.textContent).not.toContain('try-it run failed'); // detail wins
  });

  it('test_error_generic_fallback — X-5: generic copy ONLY when detail is absent', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j7' })
      .mockResolvedValueOnce({ status: 'error' });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);

    expect(container.querySelector('.tryit-error')?.textContent).toContain('try-it run failed');
  });

  it('test_not_injected_banner — X-4: banner renders and the no-diff verdict is suppressed', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j8' })
      .mockResolvedValueOnce({ status: 'done', result: NOT_INJECTED_RESULT });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);

    // The honest banner (exact contract copy).
    const banner = container.querySelector('.tryit-notinjected');
    expect(banner?.textContent).toBe(
      "The candidate wasn't part of this run's routing context, so this comparison does not reflect the mapping's effect.",
    );
    // The "No routing difference" verdict must NOT render — the comparison
    // never carried the candidate, so a no-diff claim would be a lie.
    expect(container.querySelector('.tryit-diff-none')).toBeNull();
    expect(container.textContent).not.toContain('No routing difference');
    // The two run columns still render (honest raw observations).
    expect(container.querySelectorAll('.tryit-col')).toHaveLength(2);
  });

  it('test_polling_stops_on_done — no further GETs after the terminal poll', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j3' })
      .mockResolvedValueOnce({ status: 'done', result: RESULT });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);
    expect(container.querySelector('.tryit-cols')).not.toBeNull();

    const callsAtDone = requestJsonMock.mock.calls.length;
    await advance(30_000);
    expect(requestJsonMock.mock.calls.length).toBe(callsAtDone);
  });

  it('failed poll fetch renders the error state and stops polling', async () => {
    vi.useFakeTimers();
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j4' })
      .mockRejectedValueOnce(new ApiError(500, '/api/admin/learning/tryit/j4', 'boom'));

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);

    expect(container.querySelector('.tryit-error')?.textContent).toContain('boom');
    const callsAtError = requestJsonMock.mock.calls.length;
    await advance(15_000);
    expect(requestJsonMock.mock.calls.length).toBe(callsAtError);
  });
});

const setInputValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;

async function typeInto(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    setInputValue.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

describe('TryItPanel: steward-editable test question', () => {
  it('prefills the question input with the proposal query_context', async () => {
    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);

    const input = container.querySelector<HTMLInputElement>('.tryit-question-input');
    expect(input).not.toBeNull();
    expect(input!.value).toBe('Which work orders touch the flagged operator?');
    expect(input!.maxLength).toBe(500);
    expect(requestJsonMock).not.toHaveBeenCalled(); // still nothing launched
  });

  it('sends the edited question (trimmed) as the launch payload', async () => {
    requestJsonMock.mockResolvedValueOnce({ job_id: 'j9' });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    const input = container.querySelector<HTMLInputElement>('.tryit-question-input')!;
    await typeInto(input, '  Which operators worked both systems yesterday?  ');
    await clickAsync(container.querySelector('.tryit-launch'));

    expect(requestJsonMock).toHaveBeenCalledWith(
      '/api/admin/learning/tryit',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          mapping_id: 'm-try-1',
          question: 'Which operators worked both systems yesterday?',
        }),
      }),
    );
    // The running state shows what was asked.
    expect(container.querySelector('.tryit-running .tryit-question')?.textContent).toContain(
      'Which operators worked both systems yesterday?',
    );
  });

  it('omits the question key when the input is emptied (server-side fallback)', async () => {
    requestJsonMock.mockResolvedValueOnce({ job_id: 'j10' });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await typeInto(container.querySelector<HTMLInputElement>('.tryit-question-input')!, '   ');
    await clickAsync(container.querySelector('.tryit-launch'));

    expect(requestJsonMock).toHaveBeenCalledWith(
      '/api/admin/learning/tryit',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ mapping_id: 'm-try-1' }),
      }),
    );
  });

  it('renders the question actually used above the run columns on done', async () => {
    vi.useFakeTimers();
    const overridden: TryItResult = {
      ...RESULT,
      question: 'Which operators worked both systems yesterday?',
    };
    requestJsonMock
      .mockResolvedValueOnce({ job_id: 'j11' })
      .mockResolvedValueOnce({ status: 'done', result: overridden });

    await render(<TryItPanel m={MAPPING} onClose={vi.fn()} />);
    await typeInto(
      container.querySelector<HTMLInputElement>('.tryit-question-input')!,
      'Which operators worked both systems yesterday?',
    );
    await clickAsync(container.querySelector('.tryit-launch'));
    await advance(5_000);

    // Server echo (result.question) renders — and ABOVE the run columns.
    const question = container.querySelector('.tryit-result .tryit-question');
    expect(question?.textContent).toContain('Which operators worked both systems yesterday?');
    const cols = container.querySelector('.tryit-cols');
    expect(cols).not.toBeNull();
    expect(
      // eslint-disable-next-line no-bitwise
      question!.compareDocumentPosition(cols!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe('SlCard: Try-it button steward gating', () => {
  it('renders the Try-it button for stewards and opens the drawer', async () => {
    requestJsonMock.mockImplementation((url: string) => {
      if (url === '/api/auth/me') return Promise.resolve({ is_admin: true });
      return Promise.reject(new Error(`unexpected fetch: ${url}`));
    });

    await render(<SlCard m={MAPPING} />);

    const button = container.querySelector<HTMLButtonElement>('.sl-tryit');
    expect(button).not.toBeNull();
    expect(button!.textContent).toContain('Try it');
    expect(container.querySelector('.tryit-panel')).toBeNull();

    await clickAsync(button);
    expect(container.querySelector('.tryit-panel')).not.toBeNull();
    expect(container.querySelector('.tryit-warning')).not.toBeNull();
    expect(button!.disabled).toBe(true);
  });

  it('hides the Try-it button for non-stewards', async () => {
    requestJsonMock.mockImplementation((url: string) => {
      if (url === '/api/auth/me') return Promise.resolve({ is_admin: false });
      return Promise.reject(new Error(`unexpected fetch: ${url}`));
    });

    await render(<SlCard m={MAPPING} />);

    expect(container.querySelector('.sl-tryit')).toBeNull();
  });

  it('never renders the Try-it button on insight cards, even for stewards', async () => {
    requestJsonMock.mockImplementation((url: string) => {
      if (url === '/api/auth/me') return Promise.resolve({ is_admin: true });
      return Promise.reject(new Error(`unexpected fetch: ${url}`));
    });

    await render(
      <SlCard m={{ ...MAPPING, proposed_type: 'insight', from_field: '', to_field: '' }} />,
    );

    expect(container.querySelector('.sl-tryit')).toBeNull();
  });
});
