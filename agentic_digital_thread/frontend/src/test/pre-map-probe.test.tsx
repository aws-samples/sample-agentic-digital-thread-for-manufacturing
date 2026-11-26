// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* T3.1 T-H — <PreMapProbe/>.
 *
 * The load-bearing property is that probing NEVER blocks mapping: no material
 * questions, a failed probe call, or a skip must all hand control straight back
 * to the caller. Everything else is secondary. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PreMapProbe, probeUrl } from '../views/onboard/components/PreMapProbe';

let container: HTMLDivElement;
let root: Root;

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

const TABLES = [
  { name: 'work_orders', columns: [{ name: 'state' }, { name: 'attempt_id' }] },
];

const ONE_QUESTION = {
  system_id: 'sys-1',
  questions: [
    { id: 'identifier-family', prompt: 'What shape do these identifiers follow?', evidence: 'columns unit_tag, serial_no' },
  ],
};

function stub(bodies: unknown[], calls: RequestInit[] = []) {
  let i = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push(init ?? {});
      const body = bodies[Math.min(i, bodies.length - 1)];
      i += 1;
      if (body instanceof Error) throw body;
      return response(body);
    }),
  );
  return calls;
}

function buttonByText(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').toLowerCase().includes(label.toLowerCase()),
  ) as HTMLButtonElement | undefined;
}

function setAnswer(value: string) {
  const el = container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Probe answer"]')!;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

async function mount(onContinue = vi.fn()) {
  await act(async () => {
    root.render(<PreMapProbe systemId="sys-1" tables={TABLES} onContinue={onContinue} />);
  });
  return onContinue;
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

describe('probing never blocks mapping', () => {
  it('continues immediately when nothing is material to ask', async () => {
    stub([{ system_id: 'sys-1', questions: [], derived: [] }]);
    const onContinue = await mount();
    expect(onContinue).toHaveBeenCalled();
    expect(container.textContent).toBe('');
  });

  it('auto-continues only when nothing was asked AND nothing was derived', async () => {
    // Kills reverting the auto-continue effect to questions-only: a derived
    // note with no questions must PAUSE for review, not vanish.
    stub([
      {
        system_id: 'sys-1',
        questions: [],
        derived: [
          {
            question_id: 'query-strategy',
            outcome: 'proposed',
            derived_text: 'use aggregate queries',
            evidence: 'about 5,000,000 rows',
          },
        ],
      },
    ]);
    const onContinue = await mount();
    expect(onContinue).not.toHaveBeenCalled();
    expect(container.textContent).toMatch(/use aggregate queries/);
    expect(container.textContent).toMatch(/about 5,000,000 rows/);
    await act(async () => {
      buttonByText('Continue')!.click();
    });
    expect(onContinue).toHaveBeenCalled();
  });

  it('continues when the probe call fails outright', async () => {
    stub([new Error('probe down')]);
    const onContinue = await mount();
    expect(onContinue).toHaveBeenCalled();
  });

  it('continues when the only derived entry was not filed (nothing to show)', async () => {
    // An unfiled derivation comes back rejected with no
    // text. It renders no note, so it must not hold the wizard on an empty card.
    stub([
      {
        system_id: 'sys-1',
        questions: [],
        derived: [{ question_id: 'query-strategy', outcome: 'rejected', reason: 'not_ingested' }],
      },
    ]);
    const onContinue = await mount();
    expect(onContinue).toHaveBeenCalled();
    expect(container.textContent).toBe('');
  });

  it('skip hands control straight back', async () => {
    stub([ONE_QUESTION]);
    const onContinue = await mount();
    expect(onContinue).not.toHaveBeenCalled();
    await act(async () => {
      buttonByText('Skip and map now')!.click();
    });
    expect(onContinue).toHaveBeenCalled();
  });
});

describe('asking', () => {
  it('shows the question with the schema evidence that triggered it', async () => {
    stub([ONE_QUESTION]);
    await mount();
    expect(container.textContent).toMatch(/what shape do these identifiers follow/i);
    expect(container.textContent).toMatch(/columns unit_tag, serial_no/);
    expect(container.textContent).toMatch(/1 of 1/);
  });

  it('says where an answer goes and that it does not change the next mapping', async () => {
    // No answer reaches the map that runs next; the server files each one
    // as a proposal for review, so the card must not say an answer shapes
    // how these fields are mapped.
    stub([ONE_QUESTION]);
    await mount();
    expect(container.textContent).not.toMatch(/shapes how these fields are mapped/i);
    expect(container.textContent).not.toMatch(/re-map/i);
    expect(container.textContent).toMatch(/filed as a proposal for review/i);
    expect(container.textContent).toMatch(/does not change the mapping that runs next/i);
  });

  it('stays on the question when a submitted answer was not filed', async () => {
    // The lifecycle can drop or stage a proposal (outcome skipped, reason
    // not_ingested). That answer is not kept, so the card must not move on
    // as if it were.
    stub([
      ONE_QUESTION,
      {
        system_id: 'sys-1',
        results: [{
          outcome: 'skipped',
          question_id: 'identifier-family',
          reason: 'not_ingested',
          detail: 'the proposal was gated or staged upstream',
        }],
        questions: [],
      },
    ]);
    const onContinue = await mount();
    setAnswer('tags are SITE-UNIT-NN');
    await act(async () => {
      buttonByText('Next')!.click();
    });
    expect(onContinue).not.toHaveBeenCalled();
    expect(container.textContent).toMatch(/not filed/i);
    expect(container.textContent).toMatch(/gated or staged upstream/);
  });

  it("moves on after I don't know without filing anything", async () => {
    const calls = stub([
      ONE_QUESTION,
      { system_id: 'sys-1', results: [{ outcome: 'skipped', question_id: 'identifier-family' }], questions: [] },
    ]);
    const onContinue = await mount();
    await act(async () => {
      buttonByText("I don't know")!.click();
    });
    expect(onContinue).toHaveBeenCalled();
    expect(JSON.parse(String(calls[1].body)).answers[0].answer).toBe('');
  });

  it('continues after the last question is answered', async () => {
    stub([
      ONE_QUESTION,
      { system_id: 'sys-1', results: [{ outcome: 'proposed', question_id: 'identifier-family', proposal_ids: ['p-1'] }], questions: [] },
    ]);
    const onContinue = await mount();
    setAnswer('the old record is closed as cancelled');
    await act(async () => {
      buttonByText('Next')!.click();
    });
    expect(onContinue).toHaveBeenCalled();
  });

  it('does NOT advance or continue on a rejected answer', async () => {
    stub([
      ONE_QUESTION,
      {
        system_id: 'sys-1',
        results: [{
          outcome: 'rejected',
          question_id: 'identifier-family',
          reason: 'secret_like_content',
          detail: 'remove the credential and retry',
        }],
        questions: ONE_QUESTION.questions,
      },
    ]);
    const onContinue = await mount();
    setAnswer('password: hunter2');
    await act(async () => {
      buttonByText('Next')!.click();
    });
    expect(container.textContent).toMatch(/remove the credential/);
    expect(onContinue).not.toHaveBeenCalled();
  });

  it('posts the raw discover tables and the answer to the per-system probe url', async () => {
    // Kills reintroducing a client-side fact producer (a TS mirror): the body
    // must carry the discover tables VERBATIM and no schema_facts at all.
    const calls = stub([
      ONE_QUESTION,
      { system_id: 'sys-1', results: [{ outcome: 'proposed', question_id: 'identifier-family', proposal_ids: ['p-1'] }], questions: [] },
    ]);
    await mount();
    setAnswer('closed as cancelled');
    await act(async () => {
      buttonByText('Next')!.click();
    });
    const body = JSON.parse(String(calls[1].body));
    expect(body.answers[0]).toEqual({ question_id: 'identifier-family', answer: 'closed as cancelled' });
    expect(body.tables).toEqual(TABLES);
    expect(body.schema_facts).toBeUndefined();
  });

  it('sends every probe request as JSON, so the route can read its body', async () => {
    // Walk 2026-10-21: the probe POST went without a Content-Type, the browser
    // sent text/plain, and the route answered 422 (FastAPI reads a body as a
    // model only when it is declared JSON). The catch then skipped the
    // questions silently. Both the first call and an answer must declare JSON.
    const calls = stub([
      ONE_QUESTION,
      { system_id: 'sys-1', results: [{ outcome: 'proposed', question_id: 'identifier-family', proposal_ids: ['p-1'] }], questions: [] },
    ]);
    await mount();
    setAnswer('closed as cancelled');
    await act(async () => {
      buttonByText('Next')!.click();
    });
    expect(calls).toHaveLength(2);
    for (const init of calls) {
      expect(init.method).toBe('POST');
      expect(new Headers(init.headers).get('Content-Type')).toBe('application/json');
      // The wizard has no confirm step, so it asks only for questions whose
      // answer is filed when given (the server refuses a held one).
      expect(JSON.parse(String(init.body)).durable_answers_only).toBe(true);
    }
  });
});

describe('url building', () => {
  it('encodes the system id', () => {
    expect(probeUrl('a/b')).toBe('/api/onboard/guided/probe/a%2Fb');
  });
});
