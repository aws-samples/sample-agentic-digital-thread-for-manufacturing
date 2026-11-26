// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { obAttachFile } from '../views/onboard/chat';
import { obResetAllConfirmed } from '../views/onboard/flow';
import {
  appendMsg,
  closeModal,
  flowStore,
  openModal,
  resetToWelcome,
  startWizard,
} from '../views/onboard/flow-store';
import { ob, type ObStepContext } from '../views/onboard/state';
import { getOnboardQueryClient } from '../views/onboard/onboard-queries';

const currentStep = (): ObStepContext => ({
  step: 'discover',
  systemId: 'erp',
  data: {},
  history: [],
});

function prepareResetModal(): void {
  closeModal();
  resetToWelcome();
  startWizard();
  appendMsg('Wizard state must remain on failure');
  openModal({ kind: 'resetAll' });
}

beforeEach(() => {
  prepareResetModal();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  ob.active = null;
  ob.abort = null;
  ob.currentStep = null;
  closeModal();
});

describe('reset-all correctness', () => {
  it('aborts and clears active onboarding before reset, then stops when reset fails', async () => {
    const invalidateQueries = vi.spyOn(getOnboardQueryClient(), 'invalidateQueries');
    const controller = new AbortController();
    ob.active = 'erp';
    ob.abort = controller;
    ob.currentStep = currentStep();
    const onPhase = vi.fn();
    const fetchMock = vi.fn(async () => {
      expect(controller.signal.aborted).toBe(true);
      expect(ob.abort).toBeNull();
      expect(ob.active).toBeNull();
      expect(ob.currentStep).toBeNull();
      return new Response(null, { status: 500 });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(obResetAllConfirmed(onPhase)).rejects.toThrow(
      'Reset failed (HTTP 500)',
    );

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/onboard/reset-all',
      expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
    );
    expect(onPhase).not.toHaveBeenCalled();
    expect(invalidateQueries).not.toHaveBeenCalled();
    expect(flowStore.get().modal).toEqual({ kind: 'resetAll' });
    expect(flowStore.get().mode).toBe('wizard');
    expect(flowStore.get().entries).toEqual([
      expect.objectContaining({ kind: 'msg', md: 'Wizard state must remain on failure' }),
    ]);
  });

  // After a successful reset the systems and graph are gone even when the
  // reseed fails: the cached onboarding state is refreshed and the wizard goes
  // back to welcome, while the modal stays open with the reseed notice.
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  // Reset All runs as a server job: the POST answers its job id, the status
  // poll answers how it ended.
  const resetStarted = () => json({ status: 'started', job_id: 'job-1', full: false });
  const resetDone = () => json({ state: 'done', job_id: 'job-1', result: { status: 'reset' } });

  const afterResetFetches = (...first: Response[]) => {
    const mock = vi
      .fn()
      .mockImplementation(async () =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    for (const response of first) mock.mockResolvedValueOnce(response);
    return mock;
  };

  it('reports a reseed failure, keeps the modal open and refreshes the reset state', async () => {
    const invalidateQueries = vi.spyOn(getOnboardQueryClient(), 'invalidateQueries');
    const onPhase = vi.fn();
    const fetchMock = afterResetFetches(
      resetStarted(),
      resetDone(),
      new Response(null, { status: 503 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(obResetAllConfirmed(onPhase)).rejects.toThrow(
      'Reseed failed (HTTP 503)',
    );

    expect(fetchMock.mock.calls.slice(0, 3).map(([url]) => url)).toEqual([
      '/api/onboard/reset-all',
      '/api/onboard/reset-all/job-1',
      '/api/onboard/reseed',
    ]);
    for (const i of [0, 2]) {
      expect(fetchMock.mock.calls[i][1]).toEqual(
        expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
      );
    }
    expect(onPhase).toHaveBeenCalledOnce();
    expect(onPhase).toHaveBeenCalledWith('Reseeding DB...');
    expect(invalidateQueries).toHaveBeenCalled();
    expect(flowStore.get().modal).toEqual({ kind: 'resetAll' });
    expect(flowStore.get().mode).toBe('welcome');
    expect(flowStore.get().entries).not.toContainEqual(
      expect.objectContaining({ md: 'Wizard state must remain on failure' }),
    );
  });

  it('treats a 200 reseed error envelope as failure and still refreshes the reset state', async () => {
    const invalidateQueries = vi.spyOn(getOnboardQueryClient(), 'invalidateQueries');
    const onPhase = vi.fn();
    const fetchMock = afterResetFetches(
      resetStarted(),
      resetDone(),
      new Response(JSON.stringify({ status: 'error', error: 'seed lambda failed' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(obResetAllConfirmed(onPhase)).rejects.toThrow(
      'Reseed failed (seed lambda failed)',
    );

    expect(fetchMock.mock.calls.slice(0, 3).map(([url]) => url)).toEqual([
      '/api/onboard/reset-all',
      '/api/onboard/reset-all/job-1',
      '/api/onboard/reseed',
    ]);
    expect(invalidateQueries).toHaveBeenCalled();
    expect(flowStore.get().modal).toEqual({ kind: 'resetAll' });
    expect(flowStore.get().mode).toBe('welcome');
    expect(flowStore.get().entries).not.toContainEqual(
      expect.objectContaining({ md: 'Wizard state must remain on failure' }),
    );
  });

  it('treats a skipped reseed (no sample database) as success', async () => {
    const onPhase = vi.fn();
    const fetchMock = afterResetFetches(
      resetStarted(),
      resetDone(),
      new Response(JSON.stringify({ status: 'skipped', reason: 'no_sample_database' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(obResetAllConfirmed(onPhase)).resolves.toBeUndefined();

    expect(flowStore.get().modal).toBeNull();
    expect(flowStore.get().mode).toBe('welcome');
  });

  it('polls the reset job while it runs and reseeds only once it is done', async () => {
    vi.useFakeTimers();
    try {
      const onPhase = vi.fn();
      const running = () => json({ state: 'running', job_id: 'job-1' });
      const fetchMock = afterResetFetches(resetStarted(), running(), running(), resetDone());
      vi.stubGlobal('fetch', fetchMock);

      const done = obResetAllConfirmed(onPhase);
      await vi.advanceTimersByTimeAsync(0);
      expect(onPhase).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(3000);
      expect(onPhase).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(3000);
      await expect(done).resolves.toBeUndefined();

      expect(fetchMock.mock.calls.slice(0, 5).map(([url]) => url)).toEqual([
        '/api/onboard/reset-all',
        '/api/onboard/reset-all/job-1',
        '/api/onboard/reset-all/job-1',
        '/api/onboard/reset-all/job-1',
        '/api/onboard/reseed',
      ]);
      expect(onPhase).toHaveBeenCalledWith('Reseeding DB...');
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    ['reset_incomplete', 503],
    ['reset_lease_lost', 503],
    ['reset_stopped', 503],
    [undefined, 500],
  ])('reports a job that ended in error (reason %s) as a %s of the reset', async (reason, status) => {
    const fetchMock = afterResetFetches(
      resetStarted(),
      json({ state: 'error', job_id: 'job-1', reason, error: 'still deleting' }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const err = await obResetAllConfirmed(vi.fn()).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(status);
    expect((err as ApiError).url).toBe('/api/onboard/reset');
    expect((err as ApiError).message).toBe('Reset failed (still deleting)');
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain('/api/onboard/reseed');
    expect(flowStore.get().modal).toEqual({ kind: 'resetAll' });
  });

  it.each([405, 404])(
    'falls back to the synchronous reset when the job route answers %s (a previous-release task)',
    async (status) => {
      const onPhase = vi.fn();
      const fetchMock = afterResetFetches(
        json({ detail: status === 405 ? 'Method Not Allowed' : 'Not Found' }, status),
        json({ status: 'reset', systems_cleared: 2 }),
      );
      vi.stubGlobal('fetch', fetchMock);

      await expect(obResetAllConfirmed(onPhase)).resolves.toBeUndefined();

      expect(fetchMock.mock.calls.slice(0, 3).map(([url]) => url)).toEqual([
        '/api/onboard/reset-all',
        '/api/onboard/reset',
        '/api/onboard/reseed',
      ]);
      for (const i of [0, 1, 2]) {
        expect(fetchMock.mock.calls[i][1]).toEqual(
          expect.objectContaining({ method: 'POST', credentials: 'same-origin' }),
        );
      }
      expect(onPhase).toHaveBeenCalledWith('Reseeding DB...');
      expect(flowStore.get().modal).toBeNull();
    },
  );

  it('reports a refused synchronous fallback as a refusal of the reset, and never reseeds', async () => {
    const fetchMock = afterResetFetches(
      json({ detail: 'Method Not Allowed' }, 405),
      json({ detail: 'A system is being removed. Nothing was reset.' }, 409),
    );
    vi.stubGlobal('fetch', fetchMock);

    const err = await obResetAllConfirmed(vi.fn()).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).url).toBe('/api/onboard/reset');
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain('/api/onboard/reseed');
  });

  it('reports a refused job start against the reset url, keeping its reason', async () => {
    const fetchMock = afterResetFetches(
      json({ detail: 'restoring', reason: 'startup_restore' }, 503),
    );
    vi.stubGlobal('fetch', fetchMock);

    const err = await obResetAllConfirmed(vi.fn()).catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).url).toBe('/api/onboard/reset');
    expect((err as ApiError).payload).toEqual({ detail: 'restoring', reason: 'startup_restore' });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('fails when the server no longer knows the job, and never reseeds', async () => {
    const fetchMock = afterResetFetches(resetStarted(), json({ state: 'none', job_id: 'job-1' }));
    vi.stubGlobal('fetch', fetchMock);

    const err = await obResetAllConfirmed(vi.fn()).catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(500);
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain('/api/onboard/reseed');
  });
});

describe('OpenAPI attachment correctness', () => {
  it('surfaces an actionable upload error and does not start onboarding chat', async () => {
    closeModal();
    resetToWelcome();
    let attachmentInput: HTMLInputElement | null = null;
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function (
      this: HTMLInputElement,
    ) {
      attachmentInput = this;
    });
    const fetchMock = vi.fn(async () => new Response(null, { status: 422 }));
    vi.stubGlobal('fetch', fetchMock);

    obAttachFile();
    expect(attachmentInput).not.toBeNull();
    const input = attachmentInput as unknown as HTMLInputElement;
    const file = {
      name: 'machine-api.json',
      size: 128,
      text: vi.fn().mockResolvedValue(
        JSON.stringify({ openapi: '3.0.0', info: { title: 'Machine API', version: '1' } }),
      ),
    } as unknown as File;
    Object.defineProperty(input, 'files', { configurable: true, value: [file] });

    await input.onchange?.call(input, { target: input } as unknown as Event);

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/upload/spec',
      expect.objectContaining({ method: 'POST' }),
    );
    const entries = flowStore.get().entries;
    expect(entries).toEqual([
      expect.objectContaining({ kind: 'file', name: 'machine-api.json' }),
      expect.objectContaining({
        kind: 'error',
        text: expect.stringMatching(
          /OpenAPI upload failed for "machine-api\.json" \(HTTP 422\).*try again/i,
        ),
      }),
    ]);
    expect(entries.some((entry) => entry.kind === 'user' || entry.kind === 'stream')).toBe(
      false,
    );
  });
});
