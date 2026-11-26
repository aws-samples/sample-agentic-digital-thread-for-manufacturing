// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
// The global reset returns 503 when runtimes are still deleting (nothing was
// wiped); the Reset All modal tells the operator to retry and re-enables the
// button. A reseed failure is a different failure: the label stays a plain
// 'Failed', and a notice says the reset ran but the sample databases were not
// reseeded (with the server's public error text, when it sent one).
//
// Each case runs on both ways a reset reaches the server: the reset job of
// this release (POST /api/onboard/reset-all, then its status), and a task of
// the previous release during a rolling deploy (the job route answers 405 and
// the synchronous POST /api/onboard/reset runs the reset).
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { openModal } from '../views/onboard/flow-store';
import { ObModals } from '../views/onboard/components/ObModals';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

function reply(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), { status });
}

/** `METHOD url` -> the replies it gives, in order. */
type Replies = Record<string, Response[]>;

const START = 'POST /api/onboard/reset-all';
const STATUS = 'GET /api/onboard/reset-all/job-1';
const SYNC = 'POST /api/onboard/reset';
const RESEED = 'POST /api/onboard/reseed';

/** A fake server answering from *replies*. A request it has no reply for
 * (the catalog and status refreshes after a reset) answers an empty 200.
 * Returns every request made, as `METHOD url`. */
function serve(replies: Replies): string[] {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const key = `${init?.method ?? 'GET'} ${url}`;
      calls.push(key);
      return replies[key]?.shift() ?? reply(200);
    }),
  );
  return calls;
}

/** The reset and reseed requests, in order. */
const resetCalls = (calls: string[]) => calls.filter((c) => /\/api\/onboard\/(reset|reseed)/.test(c));

type ResetPath = {
  name: string;
  /** The reset ran to its end. */
  ended: () => Replies;
  endedCalls: string[];
  /** The reset was refused before it started. */
  refused: (answer: Response) => Replies;
  refusedCalls: string[];
  /** The reset stopped before wiping anything: runtimes still deleting. */
  incomplete: (detail: string) => Replies;
  incompleteCalls: string[];
};

const PATHS: ResetPath[] = [
  {
    name: 'the reset job',
    ended: () => ({
      [START]: [reply(200, { status: 'started', job_id: 'job-1', full: false })],
      [STATUS]: [reply(200, { state: 'done', job_id: 'job-1', result: { status: 'reset' } })],
    }),
    endedCalls: [START, STATUS],
    refused: (answer) => ({ [START]: [answer] }),
    refusedCalls: [START],
    incomplete: (detail) => ({
      [START]: [reply(200, { status: 'started', job_id: 'job-1', full: false })],
      [STATUS]: [
        reply(200, { state: 'error', job_id: 'job-1', reason: 'reset_incomplete', error: detail }),
      ],
    }),
    incompleteCalls: [START, STATUS],
  },
  {
    name: 'a previous-release task (405, then the synchronous reset)',
    ended: () => ({
      [START]: [reply(405, { detail: 'Method Not Allowed' })],
      [SYNC]: [reply(200, { status: 'reset', systems_cleared: 2 })],
    }),
    endedCalls: [START, SYNC],
    refused: (answer) => ({ [START]: [reply(405, { detail: 'Method Not Allowed' })], [SYNC]: [answer] }),
    refusedCalls: [START, SYNC],
    incomplete: (detail) => ({
      [START]: [reply(405, { detail: 'Method Not Allowed' })],
      [SYNC]: [reply(503, { detail })],
    }),
    incompleteCalls: [START, SYNC],
  },
];

async function clickReset(): Promise<HTMLButtonElement> {
  await act(async () => {
    root.render(<ObModals />);
    openModal({ kind: 'resetAll' });
  });
  const button = [...document.body.querySelectorAll('button')].find(
    (b) => b.textContent === 'Reset All Data',
  ) as HTMLButtonElement;
  expect(button).toBeTruthy();
  await act(async () => {
    button.click();
  });
  return button;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

describe.each(PATHS)('Reset All modal, through $name', (path) => {
  it('asks for a retry when the reset reports runtimes still deleting', async () => {
    const calls = serve(path.incomplete('still deleting'));
    const button = await clickReset();
    await vi.waitFor(() => expect(button.textContent).toBe('Still deleting — retry in a minute'));
    expect(button.disabled).toBe(false);
    expect(resetCalls(calls)).toEqual(path.incompleteCalls);
  });

  it('says the app is still restoring when a restart restore refuses the reset', async () => {
    // The startup restore's refusal is a 503 on the same URL; its body carries
    // a stable reason, so it is told apart from a reset whose runtimes are
    // still deleting without matching the text.
    const calls = serve(
      path.refused(
        reply(503, {
          detail:
            'The app is still restoring onboarded systems after a restart. Try again in a minute.',
          reason: 'startup_restore',
        }),
      ),
    );
    const button = await clickReset();
    await vi.waitFor(() =>
      expect(button.textContent).toBe('Still restoring after a restart, retry in a minute'),
    );
    expect(button.disabled).toBe(false);
    expect(document.body.querySelector('[role="alert"]')).toBeNull();
    expect(resetCalls(calls)).toEqual(path.refusedCalls);
  });

  it('says a removal is running when the reset is refused with 409', async () => {
    const calls = serve(path.refused(reply(409, { detail: "'line-mes' is being removed" })));
    const button = await clickReset();
    await vi.waitFor(() =>
      expect(button.textContent).toBe('A removal is running — retry when it finishes'),
    );
    expect(button.disabled).toBe(false);
    expect(resetCalls(calls)).toEqual(path.refusedCalls);
  });

  it('keeps a plain failure label for a reseed 503', async () => {
    const calls = serve({ ...path.ended(), [RESEED]: [reply(503)] });
    const button = await clickReset();
    await vi.waitFor(() => expect(button.textContent).toBe('Failed'));
    expect(resetCalls(calls)).toEqual([...path.endedCalls, RESEED]);
  });

  it('says the systems were reset but the sample data was not when the reseed fails', async () => {
    // /api/onboard/reseed answers 200 {status: 'error'} when the seed
    // function raised (a seed SQL file failed); its error is the public text.
    const error =
      'An upstream service returned an error. Retry in a moment; if this keeps ' +
      'happening, report it. Reference: 0123456789ab';
    const calls = serve({
      ...path.ended(),
      [RESEED]: [reply(200, { status: 'error', error, error_category: 'upstream_error' })],
    });
    const button = await clickReset();
    await vi.waitFor(() => expect(button.textContent).toBe('Failed'));
    const alert = document.body.querySelector('[role="alert"]');
    expect(alert?.textContent).toBe(
      `The systems were reset, but the sample databases were not reseeded. ${error}`,
    );
    expect(button.disabled).toBe(false);
    expect(resetCalls(calls)).toEqual([...path.endedCalls, RESEED]);
  });

  it('shows no reseed notice when the reset itself is refused', async () => {
    const calls = serve(path.refused(reply(409, { detail: "'line-mes' is being removed" })));
    const button = await clickReset();
    await vi.waitFor(() =>
      expect(button.textContent).toBe('A removal is running — retry when it finishes'),
    );
    expect(document.body.querySelector('[role="alert"]')).toBeNull();
    expect(resetCalls(calls)).toEqual(path.refusedCalls);
  });
});
