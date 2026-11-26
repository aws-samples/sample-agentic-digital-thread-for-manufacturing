// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — runtime steward questions (ADR-0059) as a conversation IN the
 * onboarding chat: the agent asks ONE pending question at a time; answers
 * render as user bubbles and the landing outcome as the agent's reply;
 * rejected / not-closed / transport failures stay re-askable with the
 * server's text relayed verbatim; "Skip for now" posts nothing; the
 * structured promotion control keeps its PENDING-only wording; the bell deep
 * link (/#onboard/questions) opens the thread as a flow entry (once, re-open
 * refetches) instead of a grid-child side panel; prompts never render as
 * <label> (the login page's global label rule uppercased them). */
// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../api/query-client';
import {
  ANSWER_MAX_LEN,
  filedDateLabel,
  RUNTIME_QUESTIONS_URL,
  RUNTIME_TURN_URL,
  RuntimeQuestions,
  type RuntimeQuestion,
  type RuntimeTurnResult,
  turnResultLine,
} from '../views/onboard/components/RuntimeQuestions';
import { flowStore, openRuntimeQuestions, resetToWelcome } from '../views/onboard/flow-store';
import { useRuntimeQuestionsDeepLink } from '../views/onboard/runtime-questions-link';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let fetchMock: ReturnType<typeof vi.fn>;

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

const QUESTIONS: RuntimeQuestion[] = [
  {
    id: 'q-1',
    system: 'erp',
    shape: 'terminology_miss',
    prompt: 'Queries against erp repeatedly miss an identifier. Is there a translation?',
    filed_at: '2026-08-06T10:00:00+00:00',
  },
  {
    id: 'q-2',
    system: 'mes',
    shape: 'empty_join',
    prompt: 'Joins between mes and erp return no rows. Is there a mapping between these ids?',
    filed_at: '2026-08-07T09:30:00+00:00',
  },
];

function routeFetch(
  questions: RuntimeQuestion[],
  turn: (body: { question_id: string; answer: string }) => RuntimeTurnResult,
) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === RUNTIME_QUESTIONS_URL) return ok({ questions });
    if (url === RUNTIME_TURN_URL) {
      return ok(turn(JSON.parse(String(init?.body ?? '{}'))));
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  fetchMock = vi.fn(async () => ok({ questions: QUESTIONS }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.location.hash = '';
  resetToWelcome();
});

async function mount(openSeq = 1) {
  await act(async () => {
    root.render(
      <QueryClientProvider client={createQueryClient()}>
        <RuntimeQuestions openSeq={openSeq} />
      </QueryClientProvider>,
    );
  });
  await act(async () => {});
}

const thread = () => document.getElementById('runtime-questions')!;
const current = () => thread().querySelector('.rq-question') as HTMLElement | null;

function typeAnswer(value: string) {
  const textarea = current()!.querySelector('textarea') as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
  setter.call(textarea, value);
  act(() => {
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function click(el: Element) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await act(async () => {});
}

const sendButton = () => current()!.querySelector('.rq-send') as HTMLButtonElement;

describe('RuntimeQuestions — conversation', () => {
  it('opens with an agent intro and asks ONE question at a time', async () => {
    await mount();
    expect(fetchMock.mock.calls[0]![0]).toBe(RUNTIME_QUESTIONS_URL);
    expect(thread().querySelector('.rq-intro')!.textContent).toContain('I have 2 questions for you');
    expect(thread().textContent).toContain('nothing is applied automatically');
    expect(thread().querySelectorAll('.rq-question')).toHaveLength(1);
    const q = current()!;
    // The chip names the system (resolver fallback; no catalog in this file).
    expect(q.querySelector('.rq-system')!.textContent).toBe('ERP');
    expect(q.querySelector('.rq-shape')!.textContent).toBe('terminology_miss');
    expect(q.textContent).toContain(`filed ${filedDateLabel(QUESTIONS[0]!.filed_at)}`);
    expect(q.textContent).toContain('question 1 of 2');
    expect(q.querySelector('.rq-prompt')!.textContent).toBe(QUESTIONS[0]!.prompt);
    expect(thread().textContent).not.toContain(QUESTIONS[1]!.prompt);
  });

  it('renders the prompt as a paragraph inside an agent bubble — never a <label>', async () => {
    await mount();
    const prompt = current()!.querySelector('.rq-prompt')!;
    expect(prompt.tagName).toBe('P');
    expect(prompt.closest('.ob-msg')).not.toBeNull();
    expect(thread().querySelector('label')).toBeNull();
    expect(current()!.querySelector('textarea')!.getAttribute('aria-label')).toBe(
      `Answer: ${QUESTIONS[0]!.prompt}`,
    );
  });

  it('a landed answer becomes a user bubble + agent reply, then the next question is asked', async () => {
    const turn = vi.fn(
      (body: { question_id: string; answer: string }): RuntimeTurnResult => ({
        outcome: 'proposed',
        proposal_ids: ['p-9'],
        question_id: body.question_id,
        question_superseded: true,
      }),
    );
    routeFetch(QUESTIONS, turn);
    await mount();
    typeAnswer('WO- prefixed ids in erp map to line names in mes.');
    await click(sendButton());

    expect(turn).toHaveBeenCalledWith({
      question_id: 'q-1',
      answer: 'WO- prefixed ids in erp map to line names in mes.',
    });
    expect(thread().querySelector('.rq-user-answer')!.textContent).toBe(
      'WO- prefixed ids in erp map to line names in mes.',
    );
    expect(thread().querySelector('.rq-landed')!.textContent).toContain(
      'Answer landed as a proposal for review',
    );
    expect(current()!.querySelector('.rq-prompt')!.textContent).toBe(QUESTIONS[1]!.prompt);
    expect(current()!.textContent).toContain('question 2 of 2');
    expect((current()!.querySelector('textarea') as HTMLTextAreaElement).value).toBe('');
  });

  it('a REJECTED answer relays the server detail and keeps the question (draft kept)', async () => {
    routeFetch(QUESTIONS, () => ({
      outcome: 'rejected',
      reason: 'secret_shaped',
      detail: 'The answer looks like a credential and was not stored.',
      question_superseded: false,
    }));
    await mount();
    typeAnswer('AKIAIOSFODNN7EXAMPLE');
    await click(sendButton());
    expect(current()!.querySelector('.rq-line')!.textContent).toBe(
      'The answer looks like a credential and was not stored.',
    );
    expect(current()!.querySelector('.rq-line')!.getAttribute('role')).toBe('alert');
    expect(current()!.querySelector('.rq-prompt')!.textContent).toBe(QUESTIONS[0]!.prompt);
    expect((current()!.querySelector('textarea') as HTMLTextAreaElement).value).toBe('AKIAIOSFODNN7EXAMPLE');
    expect(thread().querySelectorAll('.rq-landed')).toHaveLength(0);
  });

  it('a landed answer whose row could not be closed says so honestly and stays', async () => {
    routeFetch(QUESTIONS, () => ({ outcome: 'proposed', proposal_ids: ['p-1'], question_superseded: false }));
    await mount();
    typeAnswer('An answer.');
    await click(sendButton());
    expect(current()!.querySelector('.rq-line')!.textContent).toContain('could not be marked answered');
    expect(current()!.querySelector('.rq-prompt')!.textContent).toBe(QUESTIONS[0]!.prompt);
  });

  it('a transport failure surfaces the ApiError detail inline', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url === RUNTIME_QUESTIONS_URL) return ok({ questions: QUESTIONS });
      return {
        ok: false,
        status: 404,
        statusText: 'Not Found',
        text: async () => JSON.stringify({ detail: 'unknown_question_id' }),
      } as unknown as Response;
    });
    await mount();
    typeAnswer('An answer.');
    await click(sendButton());
    expect(current()!.querySelector('.rq-line')!.textContent).toBe('unknown_question_id');
  });

  it('"Skip for now" posts nothing, notes it stays pending, and advances', async () => {
    routeFetch(QUESTIONS, () => ({ outcome: 'proposed', question_superseded: true }));
    await mount();
    await click(current()!.querySelector('.rq-skip')!);
    expect(fetchMock.mock.calls.filter(([u]) => u === RUNTIME_TURN_URL)).toHaveLength(0);
    expect(thread().querySelector('.rq-skipped')!.textContent).toContain('stays pending');
    expect(current()!.querySelector('.rq-prompt')!.textContent).toBe(QUESTIONS[1]!.prompt);
    await click(current()!.querySelector('.rq-skip')!);
    expect(current()).toBeNull();
    expect(thread().querySelector('.rq-done')!.textContent).toContain('2 skipped questions stay pending');
  });

  it('shows the empty state when no questions are pending', async () => {
    routeFetch([], () => ({ outcome: 'proposed' }));
    await mount();
    expect(thread().querySelector('.rq-empty .rq-prompt')!.textContent).toBe('No questions right now.');
    // e2e contract (test_adr0063_closure): the no-auto-apply promise is
    // visible even with an empty queue.
    expect(thread().textContent).toContain('nothing is applied automatically');
  });

  it('a load failure offers Retry', async () => {
    fetchMock.mockImplementation(async () => ({
      ok: false,
      status: 500,
      statusText: 'err',
      text: async () => JSON.stringify({ detail: 'boom' }),
    }) as unknown as Response);
    await mount();
    expect(thread().querySelector('.rq-load-error')!.textContent).toContain('boom');
    routeFetch(QUESTIONS, () => ({ outcome: 'proposed' }));
    await click(thread().querySelector('.rq-load-error button')!);
    expect(current()).not.toBeNull();
  });

  it('bounds the answer to ANSWER_MAX_LEN and disables an empty send', async () => {
    await mount();
    const textarea = current()!.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea.maxLength).toBe(ANSWER_MAX_LEN);
    expect(sendButton().disabled).toBe(true);
    typeAnswer('x');
    expect(sendButton().disabled).toBe(false);
  });

  it('a re-open (openSeq bump) refetches the live queue', async () => {
    await mount(1);
    await mount(2);
    expect(fetchMock.mock.calls.filter(([u]) => u === RUNTIME_QUESTIONS_URL)).toHaveLength(2);
  });
});

describe('structured answer-requirement promotion', () => {
  const structured: RuntimeQuestion = {
    id: 'q-context',
    system: 'erp',
    shape: 'context_not_reflected',
    prompt: 'Narrative mentions unrelated hours and days.',
    filed_at: '2026-08-24T10:00:00Z',
    requirement_terms: { asserted: 'minutes', negated: 'seconds' },
  };

  it('posts only structured terms and states that the result is PENDING, not applied', async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === RUNTIME_QUESTIONS_URL) return ok({ questions: [structured] });
      if (url === '/api/admin/vocabulary/guidance/erp/promote-requirement') {
        expect(JSON.parse(String(init?.body))).toEqual({ asserted: 'minutes', negated: 'seconds' });
        return ok({ status: 'pending', mapping_id: 'guidance-opaque-1', requirement: 'server text' });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    await mount();
    const button = container.querySelector<HTMLButtonElement>('.rq-promote-requirement')!;
    expect(button.getAttribute('aria-label')).toContain('ERP');
    const card = button.closest('.rq-question')!;
    expect(card.textContent).toContain('Creates a PENDING proposal for steward review');
    expect(card.textContent).toContain('does not apply a requirement');
    await act(async () => button.click());
    await vi.waitFor(() =>
      expect(container.querySelector('.rq-promotion-line')?.textContent).toContain(
        'PENDING proposal guidance-opaque-1',
      ),
    );
    expect(container.querySelector('.rq-promotion-line')?.textContent).toContain('not applied');
  });

  it('never parses narrative prose when structured terms are absent', async () => {
    routeFetch(
      [{ ...structured, requirement_terms: undefined, prompt: "Use 'minutes', not 'seconds'." }],
      () => ({ outcome: 'proposed' }),
    );
    await mount();
    expect(container.querySelector('.rq-promote-requirement')).toBeNull();
  });

  it('renders steward authorization denial as a non-accepted error', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url === RUNTIME_QUESTIONS_URL) return ok({ questions: [structured] });
      return {
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        text: async () => JSON.stringify({ detail: 'Steward access required' }),
      } as unknown as Response;
    });
    await mount();
    await act(async () => container.querySelector<HTMLButtonElement>('.rq-promote-requirement')!.click());
    await vi.waitFor(() =>
      expect(container.querySelector('[role="alert"]')?.textContent).toContain('Steward access required'),
    );
  });
});

describe('openRuntimeQuestions (flow store)', () => {
  it('appends ONE conversation entry; a re-open bumps openSeq instead of stacking', () => {
    resetToWelcome();
    openRuntimeQuestions();
    openRuntimeQuestions();
    const entries = flowStore.get().entries.filter((e) => e.kind === 'runtimeQuestions');
    expect(entries).toHaveLength(1);
    expect(entries[0]!.kind === 'runtimeQuestions' && entries[0]!.openSeq).toBe(2);
  });
});

describe('useRuntimeQuestionsDeepLink', () => {
  function Probe() {
    useRuntimeQuestionsDeepLink();
    return null;
  }

  it('opens the conversation for /#onboard/questions and on later hashchange only', async () => {
    resetToWelcome();
    window.location.hash = '#onboard';
    await act(async () => root.render(<Probe />));
    expect(flowStore.get().entries.some((e) => e.kind === 'runtimeQuestions')).toBe(false);
    await act(async () => {
      window.location.hash = '#onboard/questions';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(flowStore.get().entries.filter((e) => e.kind === 'runtimeQuestions')).toHaveLength(1);
  });
});

describe('turnResultLine', () => {
  it('maps outcomes to honest lines (rejection relays detail over reason)', () => {
    expect(turnResultLine({ outcome: 'proposed', question_superseded: true }).text).toBe(
      'Answer landed as a proposal for review.',
    );
    expect(turnResultLine({ outcome: 'proposed', question_superseded: false }).text).toContain(
      'could not be marked answered',
    );
    expect(turnResultLine({ outcome: 'rejected', reason: 'r', detail: 'the detail' }).text).toBe('the detail');
    expect(turnResultLine({ outcome: 'rejected', reason: 'the reason' }).text).toBe('the reason');
    expect(turnResultLine({ outcome: 'rejected' }).text).toBe('Not accepted.');
    expect(turnResultLine({ outcome: 'conflict', conflict_with: 'mes' }).text).toContain(
      'another system (mes) is already',
    );
    expect(turnResultLine({ outcome: 'skipped' }).text).toContain('stays pending');
  });
});

describe('filedDateLabel', () => {
  it('renders a locale date for parseable timestamps and the raw string otherwise', () => {
    expect(filedDateLabel('2026-08-06T10:00:00+00:00')).toBe(
      new Date('2026-08-06T10:00:00+00:00').toLocaleDateString(),
    );
    expect(filedDateLabel('not-a-date')).toBe('not-a-date');
  });
});
