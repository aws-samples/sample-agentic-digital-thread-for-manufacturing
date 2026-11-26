// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Regression tests — chat-persistence wipe bug (chat/rehydrate.ts).
 *
 * THE BUG: rehydrateSession adopted the session id up front and then applied
 * whatever /api/memory/events returned; an EMPTY result (AgentCore memory
 * persistence lags the live turn, or every event was tool noise) called
 * setHistory([]) and WIPED the in-progress conversation — investigation
 * graph full, chat panel blank. Clicking the ACTIVE session row in the
 * memory panel or a recent-session row in the '+ New' dropdown triggered it.
 *
 * FIXED SEMANTICS under test:
 * - empty fetched history + non-empty live thread → NO-OP (live state kept,
 *   console.warn, session NOT adopted, modal NOT reset);
 * - non-empty history → legit replace (adopt + reset + show);
 * - nothing to lose → legacy behavior (adopt up front, empty history ok);
 * - fetch failure with a live thread → nothing mutated, session not adopted;
 * - refreshMemory runs on every outcome (legacy finally). */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { buildRehydrateThread, rehydrateSession, type RehydrateDeps } from '../chat/rehydrate';
import { createThreadStore, type ThreadStore } from '../chat/thread-store';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function mkDeps(store: ThreadStore) {
  const deps = {
    store,
    adoptSession: vi.fn(),
    resetModal: vi.fn(),
    showThread: vi.fn(),
    refreshMemory: vi.fn(),
    onError: vi.fn(),
  } satisfies RehydrateDeps;
  return deps;
}

/** A store carrying one live in-progress turn (question + partial answer). */
function liveStore(): ThreadStore {
  const store = createThreadStore();
  store.startTurn('what is the OEE on line 2?');
  store.applyEvent('token', { text: 'Line 2 is running at ' });
  return store;
}

describe('buildRehydrateThread', () => {
  it('maps roles and filters tool noise', () => {
    const { history, turns } = buildRehydrateThread([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'Tool call query_mes(...)' },
      { role: 'assistant', content: 'Tool result: rows' },
      { role: 'assistant', content: 'a1' },
    ]);
    expect(history).toEqual([]);
    expect(turns.map((t) => [t.user.text, t.assistant.answerMd])).toEqual([['q1', 'a1']]);
  });
});

describe('rehydrateSession — wipe guard', () => {
  it('REGRESSION: an empty events fetch must NOT wipe a non-empty live thread (no adoption, warn)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ messages: [] })));
    const store = liveStore();
    const deps = mkDeps(store);

    const applied = await rehydrateSession('sess-lagging', deps);

    expect(applied).toBe(false);
    // Live thread preserved — this is the screenshot bug (graph full, chat empty).
    const s = store.getState();
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].user.text).toBe('what is the OEE on line 2?');
    // Session adoption never silently discards an in-progress thread.
    expect(deps.adoptSession).not.toHaveBeenCalled();
    expect(deps.resetModal).not.toHaveBeenCalled();
    expect(deps.showThread).not.toHaveBeenCalled();
    expect(deps.onError).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
    expect(deps.refreshMemory).toHaveBeenCalledTimes(1);
  });

  it('REGRESSION: tool-noise-only events count as empty (same guard)', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        ok({ messages: [{ role: 'assistant', content: 'Tool call query_mes({})' }] }),
      ),
    );
    const store = liveStore();
    const deps = mkDeps(store);

    expect(await rehydrateSession('sess-noise', deps)).toBe(false);
    expect(store.getState().turns).toHaveLength(1);
    expect(deps.adoptSession).not.toHaveBeenCalled();
  });

  it('non-empty history legitimately replaces a live thread (adopt + reset + show)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        ok({
          messages: [
            { role: 'user', content: 'earlier question' },
            { role: 'assistant', content: 'earlier answer' },
          ],
        }),
      ),
    );
    const store = liveStore();
    const deps = mkDeps(store);

    expect(await rehydrateSession('sess-prior', deps)).toBe(true);
    const s = store.getState();
    expect(s.history).toEqual([]);
    expect(s.turns.map((t) => [t.user.text, t.assistant.answerMd])).toEqual([
      ['earlier question', 'earlier answer'],
    ]);
    expect(deps.adoptSession).toHaveBeenCalledWith('sess-prior');
    expect(deps.resetModal).toHaveBeenCalledTimes(1);
    expect(deps.showThread).toHaveBeenCalledTimes(1);
    expect(deps.refreshMemory).toHaveBeenCalledTimes(1);
  });

  it('with NOTHING to lose, an empty-history session still adopts (legacy switch semantics)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ok({ messages: [] })));
    const store = createThreadStore(); // empty — no live content
    const deps = mkDeps(store);

    expect(await rehydrateSession('sess-empty', deps)).toBe(true);
    expect(deps.adoptSession).toHaveBeenCalledWith('sess-empty');
    expect(deps.resetModal).toHaveBeenCalledTimes(1);
    expect(deps.showThread).toHaveBeenCalledTimes(1);
    expect(store.getState()).toMatchObject({ history: [], turns: [] });
  });

  it('fetch failure with a live thread mutates nothing and does not adopt', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')));
    const store = liveStore();
    const deps = mkDeps(store);

    expect(await rehydrateSession('sess-down', deps)).toBe(false);
    expect(store.getState().turns).toHaveLength(1);
    expect(deps.adoptSession).not.toHaveBeenCalled();
    expect(deps.onError).toHaveBeenCalledWith(expect.any(Error));
    expect(deps.refreshMemory).toHaveBeenCalledTimes(1); // legacy finally
  });

  it('empty sid is a no-op', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const deps = mkDeps(createThreadStore());
    expect(await rehydrateSession('', deps)).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(deps.refreshMemory).not.toHaveBeenCalled();
  });
});
