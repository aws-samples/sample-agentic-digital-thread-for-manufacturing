// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* Wave 2b — <ContextInterviewCard/>.
 *
 * The load-bearing properties: the card is ADDITIVE (any mount fetch failure
 * collapses to a one-line note, never a blocker); answers travel to the
 * EXISTING guided endpoints with NO tables in the body (server-derived
 * questions — no client fact mirror); a rejected answer does not advance;
 * dismiss hides DEPLOYMENT questions only; confirm posts the exact
 * {systems, business_context} body; and a written-but-unrolled confirm reads
 * as success-with-note pre-provision (flag 5), never as a failure. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ContextInterviewCard,
  preProvisionConfirmLine,
} from '../views/onboard/components/ContextInterviewCard';

let container: HTMLDivElement;
let root: Root;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

type Call = { url: string; init?: RequestInit };

/** URL-keyed stub; a value may be an array consumed in order (last repeats).
 * An Error value (or element) makes that call reject. */
function stubRoutes(routes: Record<string, unknown>, calls: Call[] = []): Call[] {
  const queues = new Map<string, unknown[]>(
    Object.entries(routes).map(([k, v]) => [k, Array.isArray(v) ? [...v] : [v]]),
  );
  const keys = [...queues.keys()].sort((a, b) => b.length - a.length);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const key = keys.find((k) => url.startsWith(k));
      if (!key) throw new Error('unmatched url: ' + url);
      const q = queues.get(key)!;
      const body = q.length > 1 ? q.shift() : q[0];
      if (body instanceof Error) throw body;
      return response(body);
    }),
  );
  return calls;
}

const STATE_URL = '/api/onboard/guided/state';
const QUESTIONS_URL = '/api/onboard/guided/questions';
const TURN_URL = '/api/onboard/guided/turn';
const PROBE_URL = '/api/onboard/guided/probe/system-a';
const CONFIRM_URL = '/api/onboard/guided/confirm-context';
const DISMISS_URL = '/api/onboard/guided/dismiss';

const STATE_FIRST = {
  first_launch: true,
  dismissed: false,
  answered_topics: [],
  draft_business_context: '',
};
const STATE_LATER = { ...STATE_FIRST, first_launch: false };

const DEP_QUESTIONS = {
  questions: [
    {
      id: 'dep-1',
      prompt: 'Where does this deployment run?',
      treatment_phase: 'context',
      examples: [],
    },
  ],
};

const SYS_QUESTION = {
  id: 'retry-semantics',
  prompt: 'When work is retried, what happens?',
  evidence: 'columns work_state and attempt_no',
};

const PROBE_INITIAL = {
  system_id: 'system-a',
  results: [],
  derived: [],
  questions: [SYS_QUESTION],
  draft_business_context: '',
};

function buttonByText(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').toLowerCase().includes(label.toLowerCase()),
  ) as HTMLButtonElement | undefined;
}

function setTextarea(ariaLabel: string, value: string) {
  const el = container.querySelector<HTMLTextAreaElement>(
    `textarea[aria-label="${ariaLabel}"]`,
  )!;
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    'value',
  )!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function mount(firstLaunch = false) {
  await act(async () => {
    root.render(<ContextInterviewCard systemId="system-a" firstLaunch={firstLaunch} />);
  });
  await act(async () => {});
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe('mount and question rendering', () => {
  it('asks deployment questions first on first launch, then per-system ones', async () => {
    stubRoutes({
      [STATE_URL]: STATE_FIRST,
      [QUESTIONS_URL]: DEP_QUESTIONS,
      [PROBE_URL]: PROBE_INITIAL,
    });
    await mount(true);
    expect(container.textContent).toMatch(/where does this deployment run/i);
    expect(container.textContent).toMatch(/question 1 of 2/);
  });

  it('skips the deployment fetch when the server says not first launch', async () => {
    const calls = stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: PROBE_INITIAL,
    });
    await mount();
    expect(container.textContent).toMatch(/when work is retried/i);
    expect(container.textContent).toMatch(/columns work_state and attempt_no/);
    expect(calls.some((c) => c.url.startsWith(QUESTIONS_URL))).toBe(false);
  });

  it('renders derived notes read-only', async () => {
    stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: {
        ...PROBE_INITIAL,
        derived: [
          {
            question_id: 'query-strategy',
            outcome: 'proposed',
            derived_text: 'use aggregate queries',
            evidence: 'about 5,000,000 rows',
          },
        ],
      },
    });
    await mount();
    expect(container.textContent).toMatch(/use aggregate queries/);
    expect(container.textContent).toMatch(/about 5,000,000 rows/);
  });

  it('collapses to a one-line note when a mount fetch fails — never blocks', async () => {
    stubRoutes({ [STATE_URL]: new Error('backend down') });
    await mount();
    expect(container.textContent).toMatch(/context capture unavailable — continue/);
    expect(container.querySelector('textarea')).toBeNull();
  });
});

describe('answering', () => {
  it('posts a deployment answer to /turn and advances', async () => {
    const calls = stubRoutes({
      [STATE_URL]: STATE_FIRST,
      [QUESTIONS_URL]: DEP_QUESTIONS,
      [PROBE_URL]: PROBE_INITIAL,
      [TURN_URL]: {
        outcome: 'draft',
        question_id: 'dep-1',
        landing: 'business_context_draft',
        draft_business_context: 'single-site deployment',
      },
    });
    await mount(true);
    setTextarea('Context answer', 'single site');
    await act(async () => {
      buttonByText('Answer')!.click();
    });
    const turn = calls.find((c) => c.url.startsWith(TURN_URL))!;
    expect(JSON.parse(String(turn.init?.body))).toEqual({
      question_id: 'dep-1',
      answer: 'single site',
    });
    expect(container.textContent).toMatch(/captured/i);
    expect(container.textContent).toMatch(/when work is retried/i);
    expect(container.textContent).toMatch(/question 2 of 2/);
  });

  it('posts a per-system answer to the probe url with NO tables in the body', async () => {
    const calls = stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: [
        PROBE_INITIAL,
        {
          system_id: 'system-a',
          results: [{ outcome: 'draft', question_id: 'retry-semantics' }],
          questions: [],
          draft_business_context: '',
        },
      ],
    });
    await mount();
    setTextarea('Context answer', 'the old record is closed as cancelled');
    await act(async () => {
      buttonByText('Answer')!.click();
    });
    const probeCalls = calls.filter((c) => c.url.startsWith(PROBE_URL));
    expect(probeCalls).toHaveLength(2);
    const body = JSON.parse(String(probeCalls[1].init?.body));
    expect(body.answers).toEqual([
      { question_id: 'retry-semantics', answer: 'the old record is closed as cancelled' },
    ]);
    expect(body.tables).toBeUndefined();
    expect(body.schema_facts).toBeUndefined();
  });

  it("skip ('I don't know') posts an empty answer and advances", async () => {
    stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: [
        PROBE_INITIAL,
        {
          system_id: 'system-a',
          results: [{ outcome: 'skipped', question_id: 'retry-semantics' }],
          questions: [],
          draft_business_context: '',
        },
      ],
    });
    await mount();
    await act(async () => {
      buttonByText("I don't know")!.click();
    });
    expect(container.textContent).toMatch(/skipped/i);
    expect(container.textContent).not.toMatch(/when work is retried/i);
  });

  it('a rejected answer shows the server reason verbatim and does not advance', async () => {
    stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: [
        PROBE_INITIAL,
        {
          system_id: 'system-a',
          results: [
            {
              outcome: 'rejected',
              question_id: 'retry-semantics',
              reason: 'secret_like_content',
              detail: 'remove the credential and retry',
            },
          ],
          questions: [SYS_QUESTION],
          draft_business_context: '',
        },
      ],
    });
    await mount();
    setTextarea('Context answer', 'password: hunter2');
    await act(async () => {
      buttonByText('Answer')!.click();
    });
    expect(container.textContent).toMatch(/remove the credential and retry/);
    expect(container.textContent).toMatch(/when work is retried/i);
    expect(container.textContent).toMatch(/question 1 of 1/);
  });

  it('an answer the server could not file stays on the question with the reason', async () => {
    // A proposal the lifecycle gated or staged was not filed, so the card
    // must not move on as if the answer had landed. The route answers `rejected` with `not_ingested` (pinned by
    // tests/unit/test_guided_probe_kept_answers.py), which the card already
    // treats as "fix, don't advance": no client special case.
    const idQuestion = {
      id: 'identifier-family',
      prompt: 'What shape do these identifiers follow?',
      evidence: 'columns unit_tag, serial_no',
    };
    stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: [
        { ...PROBE_INITIAL, questions: [idQuestion] },
        {
          system_id: 'system-a',
          results: [
            {
              outcome: 'rejected',
              question_id: 'identifier-family',
              reason: 'not_ingested',
              detail: 'the proposal was gated or staged upstream',
            },
          ],
          questions: [idQuestion],
          draft_business_context: '',
        },
      ],
    });
    await mount();
    setTextarea('Context answer', 'unit tags are SITE-UNIT-NN');
    await act(async () => {
      buttonByText('Answer')!.click();
    });
    expect(container.textContent).toMatch(/gated or staged upstream/);
    expect(container.textContent).toMatch(/what shape do these identifiers follow/i);
    expect(container.textContent).toMatch(/question 1 of 1/);
  });

  it('skip all collapses the card to a one-line summary', async () => {
    stubRoutes({ [STATE_URL]: STATE_LATER, [PROBE_URL]: PROBE_INITIAL });
    await mount();
    await act(async () => {
      buttonByText('Skip all')!.click();
    });
    expect(container.textContent).toMatch(/context questions skipped/i);
    expect(container.querySelector('textarea')).toBeNull();
  });
});

describe('dismiss (deployment scope only)', () => {
  it('hides deployment questions only — per-system questions remain', async () => {
    const calls = stubRoutes({
      [STATE_URL]: STATE_FIRST,
      [QUESTIONS_URL]: DEP_QUESTIONS,
      [PROBE_URL]: PROBE_INITIAL,
      [DISMISS_URL]: { status: 'ok', dismissed: true },
    });
    await mount(true);
    expect(container.textContent).toMatch(/where does this deployment run/i);
    await act(async () => {
      buttonByText("Don't ask during onboarding again")!.click();
    });
    expect(calls.some((c) => c.url.startsWith(DISMISS_URL))).toBe(true);
    expect(container.textContent).not.toMatch(/where does this deployment run/i);
    expect(container.textContent).toMatch(/when work is retried/i);
  });

  it('offers no dismiss affordance on a per-system question or without firstLaunch', async () => {
    stubRoutes({ [STATE_URL]: STATE_LATER, [PROBE_URL]: PROBE_INITIAL });
    await mount(false);
    expect(buttonByText("Don't ask during onboarding again")).toBeUndefined();
  });
});

describe('confirm', () => {
  async function mountThroughConfirm(confirmResponse: unknown): Promise<Call[]> {
    const calls = stubRoutes({
      [STATE_URL]: STATE_LATER,
      [PROBE_URL]: [
        PROBE_INITIAL,
        {
          system_id: 'system-a',
          results: [{ outcome: 'draft', question_id: 'retry-semantics' }],
          questions: [],
          draft_business_context: 'system-a tracks widget work orders',
        },
      ],
      [CONFIRM_URL]: confirmResponse,
    });
    await mount();
    setTextarea('Context answer', 'closed as cancelled');
    await act(async () => {
      buttonByText('Answer')!.click();
    });
    return calls;
  }

  it('shows the editable draft after the last question and posts the exact confirm body', async () => {
    const calls = await mountThroughConfirm({
      status: 'ok',
      systems: { 'system-a': { written: true, roll: 'ok' } },
    });
    expect(container.textContent).toMatch(/business context draft/i);
    setTextarea('Business context draft', 'system-a tracks widget work orders (edited)');
    await act(async () => {
      buttonByText('Save context for System A')!.click();
    });
    const confirm = calls.find((c) => c.url.startsWith(CONFIRM_URL))!;
    expect(JSON.parse(String(confirm.init?.body))).toEqual({
      systems: ['system-a'],
      business_context: 'system-a tracks widget work orders (edited)',
    });
    expect(container.textContent).toMatch(/context saved/i);
  });

  it('written but not rolled reads as success-with-note pre-provision (flag 5)', async () => {
    await mountThroughConfirm({
      status: 'ok',
      systems: { 'system-a': { written: true, roll: 'unknown' } },
    });
    await act(async () => {
      buttonByText('Save context for System A')!.click();
    });
    expect(container.textContent).toMatch(/context saved/i);
    expect(container.textContent).toMatch(/read the context when it starts/i);
    expect(container.textContent).not.toMatch(/not saved/i);
  });

  it('a dropped write never reads as success', async () => {
    await mountThroughConfirm({
      status: 'partial',
      systems: { 'system-a': { written: false, reason: 'write_failed' } },
    });
    await act(async () => {
      buttonByText('Save context for System A')!.click();
    });
    expect(container.textContent).toMatch(/not saved for system-a/i);
    expect(container.textContent).toMatch(/write_failed/);
  });

  it('disables Save while the confirm is in flight, then re-enables it on an error', async () => {
    await mountThroughConfirm({ status: 'ok', systems: {} });
    let finish!: (r: Response) => void;
    const confirmCalls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        confirmCalls.push(url);
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }),
    );
    await act(async () => {
      buttonByText('Save context for System A')!.click();
    });
    const pending = buttonByText('Saving…');
    expect(pending?.disabled).toBe(true);
    await act(async () => {
      pending!.click();
    });
    expect(confirmCalls).toEqual([CONFIRM_URL]);
    const busy =
      'Business context for this system is already being saved. ' +
      'Wait for that save to finish, then save again.';
    await act(async () => {
      finish(response({ detail: busy }, 409));
    });
    expect(container.textContent).toContain(busy);
    expect(container.textContent).not.toMatch(/[{}]/);
    expect(buttonByText('Save context for System A')?.disabled).toBe(false);
  });
});

describe('preProvisionConfirmLine (pure)', () => {
  it('adapts only the written+non-ok-roll case; everything else defers to confirmResultLine', () => {
    const adapted = preProvisionConfirmLine({
      status: 'ok',
      systems: { 'system-a': { written: true, roll: 'failed' } },
    });
    expect(adapted.text).toMatch(/read the context when it starts/i);
    const clean = preProvisionConfirmLine({
      status: 'ok',
      systems: { 'system-a': { written: true, roll: 'ok' } },
    });
    expect(clean.text).toMatch(/context saved\./i);
    const dropped = preProvisionConfirmLine({
      status: 'partial',
      systems: { 'system-a': { written: false, reason: 'write_failed' } },
    });
    expect(dropped.text).toMatch(/not saved/i);
  });
});
