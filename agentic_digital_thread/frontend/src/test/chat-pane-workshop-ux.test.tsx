// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — chat pane behaviour while a long answer runs (workshop UX):
 *
 *  (a) a send attempted while a turn streams is HELD: the sidebar composer,
 *      the pop-out composer and chip clicks never clear or overwrite typed
 *      text, and one muted line explains why; Stop asks for confirmation
 *      only when the composer holds text;
 *  (b) a non-abort transport failure shows ONE fixed line (the raw message
 *      goes to the console), and "Ask again" re-sends the turn's question;
 *  (c) the stream-position indicator counts elapsed time and adds the
 *      "2 to 5 minutes" line after 60 s;
 *  (d) leaving the page asks for confirmation only while a turn streams. */
// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

import { postSse } from '../api/sse';
import { archStore, resetArchitectureState } from '../lib/arch-store';
import { setLang } from '../lib/i18n';
import { ChatThread, formatElapsed } from '../chat/ChatThread';
import ChatSidebar from '../chat/ChatSidebar';
import { createThreadStore } from '../chat/thread-store';
import { chatThreadStore, runAskTurn } from '../chat/useAskStream';
import { answerConfirm, confirmDialog, confirmText, installConfirmHost } from './confirm-host';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const postSseMock = vi.mocked(postSse);

installConfirmHost();

const HINT =
  'The assistant is still answering. Your question stays here; press Send when the answer finishes.';
const DROPPED =
  'The connection dropped before the answer arrived. Ask again. If it happens twice, ask for help.';

let lsStore: Map<string, string>;

beforeEach(() => {
  lsStore = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => lsStore.get(k) ?? null,
    setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
    removeItem: (k: string) => void lsStore.delete(k),
  });
});

afterEach(() => {
  resetArchitectureState();
  chatThreadStore.clear();
  postSseMock.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** postSse that stays open until the turn is aborted (a long answer). */
function streamUntilAborted() {
  postSseMock.mockImplementation(
    (_url, _body, _onEvent, options) =>
      new Promise<void>((_resolve, reject) => {
        options?.signal?.addEventListener('abort', () =>
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' })),
        );
      }),
  );
}

describe('<ChatSidebar/> while a turn streams', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as Response),
    );
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(<ChatSidebar />);
    });
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const input = () => document.getElementById('question-input') as HTMLTextAreaElement;
  const modalInput = () => document.getElementById('modal-question-input') as HTMLTextAreaElement;
  const sendBtn = () => document.getElementById('send-btn') as HTMLButtonElement;
  const hints = () =>
    [...document.querySelectorAll<HTMLElement>('.chat-composer-hint')].map((h) => h.textContent);

  async function startLongTurn(question = 'first question') {
    streamUntilAborted();
    input().value = question;
    await act(async () => {
      window.sendMessage?.();
    });
    expect(sendBtn().classList.contains('streaming')).toBe(true);
    expect(input().value).toBe('');
  }

  function pressEnter(el: HTMLElement) {
    act(() => {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
  }

  it('Enter mid-turn keeps the typed question and shows the muted line', async () => {
    await startLongTurn();
    expect(hints()).toEqual([]);
    input().value = 'next question';
    pressEnter(input());
    expect(input().value).toBe('next question');
    expect(postSseMock).toHaveBeenCalledTimes(1);
    expect(hints()).toEqual([HINT]);
    expect(document.querySelector('.chat-composer-hint')!.getAttribute('role')).toBe('status');
  });

  it('a chip clicked mid-turn never overwrites a typed draft', async () => {
    await startLongTurn();
    input().value = 'my own draft';
    act(() => window.askQuestion?.('chip question'));
    expect(input().value).toBe('my own draft');
    expect(postSseMock).toHaveBeenCalledTimes(1);
    expect(hints()).toEqual([HINT]);
  });

  it('a chip clicked mid-turn fills an EMPTY composer so it is ready to send', async () => {
    await startLongTurn();
    act(() => window.askQuestion?.('chip question'));
    expect(input().value).toBe('chip question');
    expect(postSseMock).toHaveBeenCalledTimes(1);
  });

  it('the pop-out composer keeps its text mid-turn and shows the line there', async () => {
    await startLongTurn();
    act(() => window.openChatModal?.());
    modalInput().value = 'popout question';
    pressEnter(modalInput());
    expect(modalInput().value).toBe('popout question');
    expect(input().value).toBe('');
    expect(postSseMock).toHaveBeenCalledTimes(1);
    const modalHint = document.querySelector('#chat-modal-overlay .chat-composer-hint');
    expect(modalHint?.textContent).toBe(HINT);
    // The sidebar composer did not hold anything, so it shows no line.
    expect(document.querySelector('#chat-sidebar .chat-composer-hint')).toBeNull();
  });

  it('Stop with an empty composer stops in one click', async () => {
    const nativeConfirm = vi.fn(() => true);
    vi.stubGlobal('confirm', nativeConfirm);
    await startLongTurn();
    await act(async () => {
      sendBtn().click();
    });
    expect(confirmDialog()).toBeNull();
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(sendBtn().classList.contains('streaming')).toBe(false);
    expect(document.querySelector('.response-canceled')).not.toBeNull();
  });

  it('Stop with text in the composer asks first; declining keeps the answer running', async () => {
    const nativeConfirm = vi.fn(() => true);
    vi.stubGlobal('confirm', nativeConfirm);
    await startLongTurn();
    input().value = 'typed next';
    await act(async () => {
      sendBtn().click();
    });
    // The app dialog asks (never the native confirm), and nothing is aborted
    // while it is open.
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(confirmText()).toEqual({
      title: 'Stop the answer?',
      body: 'Your new question stays in the box.',
      buttons: ['Cancel', 'Stop answer'],
    });
    expect(sendBtn().classList.contains('streaming')).toBe(true);
    await answerConfirm('cancel');
    expect(confirmDialog()).toBeNull();
    expect(sendBtn().classList.contains('streaming')).toBe(true);
    expect(input().value).toBe('typed next');

    // Accepting stops the answer; the typed question stays in the box and
    // the held line goes away with the stream.
    pressEnter(input());
    expect(hints()).toEqual([HINT]);
    await act(async () => {
      sendBtn().click();
    });
    await answerConfirm('confirm');
    expect(sendBtn().classList.contains('streaming')).toBe(false);
    expect(input().value).toBe('typed next');
    expect(hints()).toEqual([]);
  });

  it('a double-click on Stop opens one dialog, and the second click does not dismiss it', async () => {
    await startLongTurn();
    input().value = 'typed next';
    await act(async () => {
      sendBtn().dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    });
    // The second click of the double-click lands on the dialog backdrop.
    await act(async () => {
      confirmDialog()!.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 2 }));
    });
    expect(document.querySelectorAll('.confirm-dialog-overlay')).toHaveLength(1);
    expect(sendBtn().classList.contains('streaming')).toBe(true);
    await answerConfirm('confirm');
    expect(sendBtn().classList.contains('streaming')).toBe(false);
  });

  it('+ New mid-turn asks first: declining keeps the turn, accepting cancels it and starts fresh', async () => {
    await startLongTurn();
    await act(async () => {
      void window.startNewSession?.();
    });
    expect(confirmText()).toEqual({
      title: 'Start a new session?',
      body: 'An answer is still running. Starting a new session cancels it.',
      buttons: ['Cancel', 'Start new session'],
    });
    await answerConfirm('cancel');
    expect(sendBtn().classList.contains('streaming')).toBe(true);
    expect(chatThreadStore.getState().turns).toHaveLength(1);

    await act(async () => {
      void window.startNewSession?.();
    });
    await answerConfirm('confirm');
    expect(sendBtn().classList.contains('streaming')).toBe(false);
    expect(chatThreadStore.getState().turns).toHaveLength(0);
  });

  it('the chat dialogs speak the UI language', async () => {
    setLang('de');
    try {
      await startLongTurn();
      input().value = 'nächste Frage';
      await act(async () => {
        sendBtn().click();
      });
      expect(confirmText()).toEqual({
        title: 'Antwort stoppen?',
        body: 'Ihre neue Frage bleibt im Eingabefeld.',
        buttons: ['Abbrechen', 'Antwort stoppen'],
      });
      await answerConfirm('cancel');
      await act(async () => {
        void window.startNewSession?.();
      });
      expect(confirmText()).toEqual({
        title: 'Neue Sitzung starten?',
        body: 'Eine Antwort wird noch erstellt. Eine neue Sitzung bricht sie ab.',
        buttons: ['Abbrechen', 'Neue Sitzung starten'],
      });
      await answerConfirm('cancel');
      expect(sendBtn().classList.contains('streaming')).toBe(true);
    } finally {
      setLang('en');
    }
  });

  it('+ New with no turn running resets at once, with no dialog', async () => {
    await act(async () => {
      void window.startNewSession?.();
    });
    expect(confirmDialog()).toBeNull();
  });

  it('"Ask again" on an errored turn re-sends that turn\'s question', async () => {
    postSseMock.mockImplementationOnce(async () => {
      throw new Error('POST /api/ask → 504');
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    input().value = 'which lot failed?';
    await act(async () => {
      window.sendMessage?.();
    });
    const again = document.querySelector<HTMLButtonElement>('#messages .ask-again-btn')!;
    expect(again).not.toBeNull();
    // The retry's stream closes cleanly with NO done/error frame (a proxy
    // or task closing the body): it must settle, not stay "Working".
    postSseMock.mockImplementationOnce(async () => {});
    await act(async () => {
      again.click();
    });
    expect(postSseMock).toHaveBeenCalledTimes(2);
    const first = postSseMock.mock.calls[0][1] as { question: string; session_id: string };
    const retry = postSseMock.mock.calls[1][1] as { question: string; session_id: string };
    expect(retry.question).toBe('which lot failed?');
    // Same Conversation Session, a distinct second turn.
    expect(retry.session_id).toBe(first.session_id);
    const { turns } = chatThreadStore.getState();
    expect(turns).toHaveLength(2);
    expect(turns[1].id).not.toBe(turns[0].id);
    expect(turns[1].user.raw).toBe('which lot failed?');
    // The retry settled to the fixed attendee copy; nothing is left running.
    expect(turns[1].assistant.streaming).toBe(false);
    expect(turns[1].assistant.error).toBe(DROPPED);
    expect(document.querySelector('.thinking-inline')).toBeNull();
    expect(sendBtn().classList.contains('streaming')).toBe(false);
    expect(document.querySelectorAll('#messages .turn-error')).toHaveLength(2);
  });

  it('"Ask again" on the last turn tells the server it is a retry; a normal send does not', async () => {
    postSseMock.mockImplementationOnce(async () => {
      throw new Error('POST /api/ask → 504');
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    input().value = 'how many records failed?';
    await act(async () => {
      window.sendMessage?.();
    });
    const first = postSseMock.mock.calls[0][1] as Record<string, unknown>;
    expect('retry' in first).toBe(false);
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('done', {});
    });
    await act(async () => {
      document.querySelector<HTMLButtonElement>('#messages .ask-again-btn')!.click();
    });
    const retry = postSseMock.mock.calls[1][1] as Record<string, unknown>;
    expect(retry.question).toBe('how many records failed?');
    expect(retry.retry).toBe(true);
    expect(retry.session_id).toBe(first.session_id);
  });

  it('"Ask again" on an older turn, and any other chip, is an ordinary question', async () => {
    postSseMock.mockImplementationOnce(async () => {
      throw new Error('POST /api/ask → 504');
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    input().value = 'first question';
    await act(async () => {
      window.sendMessage?.();
    });
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'An answer.' });
      onEvent('done', {});
    });
    input().value = 'second question';
    await act(async () => {
      window.sendMessage?.();
    });
    const again = document.querySelectorAll<HTMLButtonElement>('#messages .ask-again-btn');
    expect(again).toHaveLength(1);
    postSseMock.mockImplementation(async (_url, _body, onEvent) => {
      onEvent('done', {});
    });
    await act(async () => {
      again[0].click();
    });
    const older = postSseMock.mock.calls[2][1] as Record<string, unknown>;
    expect(older.question).toBe('first question');
    expect('retry' in older).toBe(false);
    await act(async () => {
      window.askQuestion?.('a suggested question');
    });
    const chip = postSseMock.mock.calls[3][1] as Record<string, unknown>;
    expect(chip.question).toBe('a suggested question');
    expect('retry' in chip).toBe(false);
  });

  it('a Stop whose abort races an error frame shows only the canceled marker', async () => {
    // The fetch is open; on Stop the buffered `event: error` frame is still
    // delivered, then the read rejects with AbortError.
    postSseMock.mockImplementationOnce(
      (_url, _body, onEvent, options) =>
        new Promise<void>((_resolve, reject) => {
          options?.signal?.addEventListener('abort', () => {
            onEvent('error', { text: 'Query failed. Retry in a moment.', correlation_id: 's1' });
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          });
        }),
    );
    input().value = 'long question';
    await act(async () => {
      window.sendMessage?.();
    });
    await act(async () => {
      sendBtn().click();
    });
    const msg = document.querySelector<HTMLElement>('#messages .message.assistant')!;
    expect(msg.querySelector('.response-canceled')).not.toBeNull();
    expect(msg.querySelector('.turn-error')).toBeNull();
    expect(msg.innerHTML).not.toContain('var(--red)');
  });

  it('asks before leaving the page only while a turn streams', async () => {
    const idle = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(idle);
    expect(idle.defaultPrevented).toBe(false);

    await startLongTurn();
    const busy = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(busy);
    expect(busy.defaultPrevented).toBe(true);

    await act(async () => {
      sendBtn().click();
    });
    const after = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(false);
  });
});

describe('runAskTurn — transport failures read as one fixed line', () => {
  const deps = { plant: 'all', showThread: () => {}, onAnswerReady: () => {} };

  it.each([
    ['an HTTP status', new Error('POST /api/ask → 504')],
    ['a dropped fetch', new TypeError('Failed to fetch')],
    ['a JS exception', new Error("Cannot read properties of undefined (reading 'x')")],
  ])('maps %s to the fixed line; the raw message goes to the console', async (_label, err) => {
    postSseMock.mockRejectedValueOnce(err);
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    await runAskTurn('q', new AbortController(), deps);
    const turn = chatThreadStore.getState().turns.at(-1)!;
    expect(turn.assistant.error).toBe(DROPPED);
    expect(turn.assistant.errorRef).toBeNull();
    expect(consoleSpy).toHaveBeenCalledWith('[ask] stream failed:', err.message);
  });

  it('keeps the fixed server copy of an SSE `event: error` frame', async () => {
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('error', {
        text: 'Query failed. Retry in a moment; if this keeps happening, report it.',
        correlation_id: 'sess-1',
      });
    });
    await runAskTurn('q', new AbortController(), deps);
    const a = chatThreadStore.getState().turns.at(-1)!.assistant;
    expect(a.error).toBe('Query failed. Retry in a moment; if this keeps happening, report it.');
    expect(a.errorRef).toBe('sess-1');
  });

  it('a Stop (abort) stays the quiet canceled marker, never the error line', async () => {
    postSseMock.mockRejectedValueOnce(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    await runAskTurn('q', new AbortController(), deps);
    const a = chatThreadStore.getState().turns.at(-1)!.assistant;
    expect(a.error).toBeNull();
    expect(a.canceled).toBe(true);
  });

  it('ignores an `event: error` delivered after Stop, then settles as canceled', async () => {
    const controller = new AbortController();
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      controller.abort();
      onEvent('error', { text: 'Query failed.', correlation_id: 'sess-1' });
      throw Object.assign(new Error('aborted'), { name: 'AbortError' });
    });
    // The frame is dropped outright: the turn never holds an error, even
    // for one notify (the abort reducer clearing it later is a second guard).
    let sawError = false;
    const unsubscribe = chatThreadStore.subscribe(() => {
      if (chatThreadStore.getState().turns.at(-1)?.assistant.error != null) sawError = true;
    });
    await runAskTurn('q', controller, deps);
    unsubscribe();
    expect(sawError).toBe(false);
    const a = chatThreadStore.getState().turns.at(-1)!.assistant;
    expect(a.error).toBeNull();
    expect(a.errorRef).toBeNull();
    expect(a.canceled).toBe(true);
    expect(a.streaming).toBe(false);
    expect(archStore.getState().investigationActive).toBe(false);
  });

  it('a Stop after an error frame clears the error (abort reducer)', () => {
    const store = createThreadStore();
    store.startTurn('q');
    store.errorTurn('Query failed.', 'sess-1');
    store.abortTurn();
    const a = store.getState().turns[0].assistant;
    expect(a.error).toBeNull();
    expect(a.errorRef).toBeNull();
    expect(a.canceled).toBe(true);
  });

  it('a clean end of the stream with no done or error frame fails the turn', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'partial reasoning' });
    });
    await runAskTurn('q', new AbortController(), deps);
    const a = chatThreadStore.getState().turns.at(-1)!.assistant;
    expect(a.streaming).toBe(false);
    expect(a.error).toBe(DROPPED);
    expect(a.canceled).toBe(false);
    expect(a.segments.streamingText).toContain('partial reasoning');
    expect(archStore.getState().investigationActive).toBe(false);
  });

  it('a clean end after `done` leaves the answered turn alone', async () => {
    postSseMock.mockImplementationOnce(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'the answer' });
      onEvent('done', {});
    });
    await runAskTurn('q', new AbortController(), deps);
    const a = chatThreadStore.getState().turns.at(-1)!.assistant;
    expect(a.error).toBeNull();
    expect(a.answerMd).toContain('the answer');
  });

  it('a Stop that races the final read settles as canceled, not as an error', async () => {
    const controller = new AbortController();
    postSseMock.mockImplementationOnce(async () => {
      controller.abort(); // aborted, but the body had already ended cleanly
    });
    await runAskTurn('q', controller, deps);
    const a = chatThreadStore.getState().turns.at(-1)!.assistant;
    expect(a.error).toBeNull();
    expect(a.canceled).toBe(true);
    expect(a.streaming).toBe(false);
  });
});

describe('stream-position indicator — elapsed time', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-04T10:00:00Z'));
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it('formats m:ss', () => {
    expect(formatElapsed(0)).toBe('0:00');
    expect(formatElapsed(9.9)).toBe('0:09');
    expect(formatElapsed(102)).toBe('1:42');
    expect(formatElapsed(-3)).toBe('0:00');
  });

  it('counts up from the turn start and adds the long-wait line after 60 s', () => {
    const store = createThreadStore();
    act(() => {
      store.startTurn('q');
    });
    act(() => root.render(<ChatThread store={store} />));
    const indicator = () => container.querySelector<HTMLElement>('.thinking-inline');
    const slow = () => container.querySelector<HTMLElement>('.thinking-inline .thinking-slow-hint');
    expect(indicator()?.querySelector('.thinking-elapsed')?.textContent).toBe('Working... 0:00');
    expect(slow()).toBeNull();

    act(() => {
      vi.advanceTimersByTime(59_000);
    });
    expect(indicator()?.textContent).toContain('Working... 0:59');
    expect(slow()).toBeNull();

    act(() => {
      vi.advanceTimersByTime(43_000);
    });
    expect(indicator()?.querySelector('.thinking-elapsed')?.textContent).toBe('Working... 1:42');
    expect(slow()?.textContent).toBe('Questions that reach several systems take 2 to 5 minutes.');

    // The indicator (and its clock) goes away when the turn settles.
    act(() => {
      store.abortTurn();
    });
    expect(indicator()).toBeNull();
  });

  it('a surface mounted mid-turn shows the same clock (counted from the turn start)', () => {
    const store = createThreadStore();
    act(() => {
      store.startTurn('q');
    });
    act(() => {
      vi.advanceTimersByTime(75_000);
    });
    act(() => root.render(<ChatThread store={store} />));
    expect(
      container.querySelector('.thinking-inline .thinking-elapsed')?.textContent,
    ).toBe('Working... 1:15');
    expect(container.querySelector('.thinking-slow-hint')).not.toBeNull();
  });

  /** jsdom has no layout: the host's scrollHeight grows by 60 px while the
   * slow line is in the DOM, with a 300 px viewport. */
  function layoutHost(): { current: HTMLElement } {
    Object.defineProperty(container, 'clientHeight', { value: 300, configurable: true });
    Object.defineProperty(container, 'scrollHeight', {
      configurable: true,
      get: () => (container.querySelector('.thinking-slow-hint') ? 560 : 500),
    });
    return { current: container };
  }

  it('keeps a following reader at the bottom when the 60 s line appears', () => {
    const host = layoutHost();
    const store = createThreadStore();
    act(() => {
      store.startTurn('q');
    });
    act(() => root.render(<ChatThread store={store} containerRef={host} />));
    expect(container.scrollTop).toBe(500);
    // A quiet stream: no token or pill, only the indicator's own clock.
    act(() => {
      vi.advanceTimersByTime(61_000);
    });
    expect(container.querySelector('.thinking-slow-hint')).not.toBeNull();
    expect(container.scrollTop).toBe(560);
  });

  it('leaves a reader who scrolled up where they are when the 60 s line appears', () => {
    const host = layoutHost();
    const store = createThreadStore();
    act(() => {
      store.startTurn('q');
    });
    act(() => root.render(<ChatThread store={store} containerRef={host} />));
    act(() => {
      container.scrollTop = 0;
      container.dispatchEvent(new Event('scroll'));
    });
    act(() => {
      vi.advanceTimersByTime(61_000);
    });
    expect(container.querySelector('.thinking-slow-hint')).not.toBeNull();
    expect(container.scrollTop).toBe(0);
  });
});
