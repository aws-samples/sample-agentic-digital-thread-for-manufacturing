// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Feedback details block (<FeedbackRow/> + <FeedbackDetails/>): the thumb
 * click's IMMEDIATE POST stays byte-identical (no comment keys), then an
 * optional details block sends a SECOND postFeedback carrying the comment +
 * the cited context with the SAME message_id/sentiment. Pins kill the plan
 * mutations M32 (second POST loses comment/context or first POST changed),
 * M33 (textarea unbounded), M34 (strings hardcoded / a dictionary missing
 * the new keys), and M36 (details block adds a .feedback-btn). */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { requestJson } from '../api/client';
import { FeedbackRow } from '../chat/Feedback';
import { setLang } from '../lib/i18n';

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, requestJson: vi.fn() };
});

const requestJsonMock = vi.mocked(requestJson);

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let store: Map<string, string>;

beforeEach(() => {
  // This node/jsdom combo ships a broken localStorage — stub a Map-backed
  // store (the i18n-chrome.test.tsx pattern) so useI18n/setLang work.
  store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
  });
  setLang('en');
  requestJsonMock.mockReset();
  requestJsonMock.mockResolvedValue({ status: 'success' });
  window.currentSessionId = 'session-test-1';
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

// Id-shaped fixtures: the citedSystems prop carries system IDS
// derived from citation tool names — never display labels.
const CITED_SYSTEMS = Array.from({ length: 14 }, (_, n) => `sys-${n}`);
const CITED_RULES = Array.from({ length: 22 }, (_, n) => ({ id: `r-${n}`, label: `Rule ${n}` }));

const render = () =>
  act(() =>
    root.render(
      <FeedbackRow
        messageId="msg-77"
        userMessage="original question?"
        assistantResponse="the answer"
        citedSystems={CITED_SYSTEMS}
        citedRules={CITED_RULES}
      />,
    ),
  );

function postedBody(callIndex: number): Record<string, unknown> {
  const [url, init] = requestJsonMock.mock.calls[callIndex] as [string, RequestInit];
  expect(url).toBe('/api/feedback');
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

async function clickThumb(sentiment: 'positive' | 'negative') {
  const btn = container.querySelector<HTMLButtonElement>(
    `.feedback-btn[data-sentiment="${sentiment}"]`,
  )!;
  await act(async () => {
    btn.click();
  });
}

function typeComment(text: string) {
  const textarea = container.querySelector<HTMLTextAreaElement>('textarea.feedback-comment')!;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    'value',
  )!.set!;
  act(() => {
    setter.call(textarea, text);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

describe('FeedbackRow details block', () => {
  it('thumb click posts IMMEDIATELY with the legacy payload — no comment keys (M32 first-POST half)', async () => {
    render();
    await clickThumb('negative');
    expect(requestJsonMock).toHaveBeenCalledTimes(1);
    const body = postedBody(0);
    expect(body).toEqual({
      message_id: 'msg-77',
      session_id: 'session-test-1',
      user_message: 'original question?',
      assistant_response: 'the answer',
      tools_used: [],
      sentiment: 'negative',
    });
    expect(body).not.toHaveProperty('user_comment');
    expect(body).not.toHaveProperty('cited_systems');
  });

  it('reveals a bounded textarea (maxLength 1000) and NO extra .feedback-btn (M33, M36)', async () => {
    render();
    expect(container.querySelector('.feedback-details')).toBeNull(); // hidden until a click
    await clickThumb('negative');
    const textarea = container.querySelector<HTMLTextAreaElement>('textarea.feedback-comment')!;
    expect(textarea).not.toBeNull();
    expect(textarea.maxLength).toBe(1000);
    // the thumbs-count DOM contract stays exactly 2 (chat-thread.test.tsx pin)
    expect(container.querySelectorAll('.feedback-btn')).toHaveLength(2);
    expect(container.querySelectorAll('.feedback-details .feedback-btn')).toHaveLength(0);
  });

  it('send posts a SECOND feedback with comment + capped cited context + same id/sentiment (M32)', async () => {
    render();
    await clickThumb('negative');
    typeComment('the totals looked wrong');
    const send = container.querySelector<HTMLButtonElement>('button.feedback-send')!;
    expect(send.disabled).toBe(false);
    await act(async () => {
      send.click();
    });
    expect(requestJsonMock).toHaveBeenCalledTimes(2);
    const body = postedBody(1);
    expect(body.message_id).toBe('msg-77');
    expect(body.sentiment).toBe('negative');
    expect(body.user_comment).toBe('the totals looked wrong');
    expect(body.cited_systems).toHaveLength(12); // client-side cap mirrors the server
    expect(body.cited_systems).toEqual(CITED_SYSTEMS.slice(0, 12));
    expect(body.cited_rules).toHaveLength(20);
    expect((body.cited_rules as { id: string }[])[0]).toEqual({ id: 'r-0', label: 'Rule 0' });
    // thanks line replaces the details block
    expect(container.querySelector('.feedback-thanks')?.textContent).toBe(
      'Thanks — your feedback was recorded.',
    );
    expect(container.querySelector('textarea.feedback-comment')).toBeNull();
  });

  it('send is disabled for an empty comment (no second POST fires)', async () => {
    render();
    await clickThumb('positive');
    const send = container.querySelector<HTMLButtonElement>('button.feedback-send')!;
    expect(send.disabled).toBe(true);
    await act(async () => {
      send.click();
    });
    expect(requestJsonMock).toHaveBeenCalledTimes(1); // only the thumb POST
  });
});

describe('i18n — feedback strings (M34)', () => {
  it('renders the thumb titles and placeholder through t() (en values)', async () => {
    render();
    expect(
      container.querySelector<HTMLButtonElement>('.feedback-btn[data-sentiment="positive"]')!.title,
    ).toBe('Good response');
    expect(
      container.querySelector<HTMLButtonElement>('.feedback-btn[data-sentiment="negative"]')!.title,
    ).toBe('Bad response');
    await clickThumb('negative');
    expect(
      container.querySelector<HTMLTextAreaElement>('textarea.feedback-comment')!.placeholder,
    ).toBe('Add an optional comment about this response…');
    expect(container.querySelector<HTMLButtonElement>('button.feedback-send')!.textContent).toBe(
      'Send feedback',
    );
  });

  it('ALL SIX dictionaries carry the five feedback keys (parity)', () => {
    // Evaluate the shipped I18N literal (the i18n-chrome.test.tsx helper).
    const src = readFileSync(path.resolve(process.cwd(), 'src/lib/i18n.ts'), 'utf-8');
    const decl = 'const I18N: Record<string, Record<string, string>> = {';
    const start = src.indexOf(decl);
    const end = src.indexOf('\n};', start) + 3;
    const objText = src.slice(src.indexOf('{', start + decl.length - 1), end - 1);
    const dicts = new Function(`return ${objText}`)() as Record<string, Record<string, string>>;
    const keys = [
      'chat.feedbackGood',
      'chat.feedbackBad',
      'chat.feedbackDetailsPlaceholder',
      'chat.feedbackSend',
      'chat.feedbackThanks',
    ];
    for (const lang of ['en', 'ja', 'de', 'fr', 'ko', 'zh']) {
      for (const key of keys) {
        expect(dicts[lang]?.[key], `${lang}:${key}`).toBeTruthy();
      }
    }
  });
});
