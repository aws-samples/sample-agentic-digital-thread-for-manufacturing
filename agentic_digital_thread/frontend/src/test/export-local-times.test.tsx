// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Word export shows the same times as the screen: UTC answer times become
 * <time datetime title> elements with the viewer-local text, code and link
 * text stay as written, and the file says which zone it uses. Also: a pinned
 * card refresh sends the browser zone and locale, as a chat question does. */
// @vitest-environment jsdom
import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { mdToSafeHtml } from '../chat/md';
import { setDisplayTimeZone } from '../lib/answer-times';
import { dataModalStore, openDataModal, saveModalAsWord } from '../shared/thread/data-modal';
import { refreshCard } from '../views/dashboard/pinned';
import { jsonResponse } from './fetch-stub';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => setDisplayTimeZone('America/Denver', 'en-US'));
afterEach(() => {
  setDisplayTimeZone(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function exportedHtml(): string {
  const parts: BlobPart[][] = [];
  const OrigBlob = globalThis.Blob;
  vi.stubGlobal(
    'Blob',
    class extends OrigBlob {
      constructor(p: BlobPart[] = [], o?: BlobPropertyBag) {
        super(p, o);
        parts.push(p);
      }
    },
  );
  Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:test'), revokeObjectURL: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  saveModalAsWord();
  vi.unstubAllGlobals();
  return String(parts[0][1]);
}

describe('Word export shows local times', () => {
  it('converts answer and follow-up times and stamps the zone', () => {
    act(() => openDataModal('Times', 'Window end 2026-10-15 14:17 UTC.\n\n| WO | Raised |\n|---|---|\n| 1 | 2026-10-15T06:00Z |'));
    act(() =>
      dataModalStore.update((s) => ({
        ...s,
        followupTurns: [{ id: 1, question: 'q', answerMd: 'Due 2026-10-16 12:00 UTC', status: 'done' } as never],
      })),
    );
    const html = exportedHtml();
    expect(html).toContain('<time datetime="2026-10-15T14:17Z" title="2026-10-15 14:17 UTC">2026-10-15 08:17 MDT</time>');
    expect(html).toContain('<time datetime="2026-10-15T06:00Z" title="2026-10-15T06:00Z">2026-10-15 00:00 MDT</time>');
    expect(html).toContain('2026-10-16 06:00 MDT');
    expect(html).toContain('Times shown in America/Denver');
  });

  it('leaves code and link text as written', () => {
    const html = mdToSafeHtml('`2026-10-15 14:17 UTC` and [2026-10-15 14:17 UTC](https://example.com)', {
      localTimes: true,
    });
    expect(html).not.toContain('<time');
    expect(html).toContain('<code>2026-10-15 14:17 UTC</code>');
    expect(html).toContain('>2026-10-15 14:17 UTC</a>');
  });

  it('keeps the shared sanitizer unchanged when not asked to convert', () => {
    expect(mdToSafeHtml('At 2026-10-15 14:17 UTC.')).toBe('<p>At 2026-10-15 14:17 UTC.</p>\n');
  });
});

describe('pinned card refresh', () => {
  it('sends the browser zone and locale', async () => {
    const fetchFn = vi.fn().mockResolvedValue(jsonResponse(200, { card_id: 'c1' }));
    vi.stubGlobal('fetch', fetchFn);
    await refreshCard('c1');
    const [url, init] = fetchFn.mock.calls[0];
    expect(String(url)).toBe('/api/dashboard-cards/c1/refresh');
    expect(init.method).toBe('POST');
    const body = JSON.parse(String(init.body));
    expect(body.timezone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(body.locale).toBe(navigator.language);
  });
});
