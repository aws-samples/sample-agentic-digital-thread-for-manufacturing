// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests for the store-driven <DataModal/> (jsx-migration W2.4;
 * zero-innerHTML migration): the imperative openDataModal/closeDataModal
 * window contract dispatches to dataModalStore; markdown renders through
 * <Md/> (react-markdown elements — the legacy `raw` pre-sanitized-HTML path
 * had no callers and was removed); the Word export builds its DOWNLOAD
 * payload from STORE content (not the live DOM); and ChatSidebar resets go
 * through resetModalConversation (W3.1 — the window._modalConversation
 * bridge is gone). */
// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { postSse } from '../api/sse';
import {
  closeDataModal,
  DataModal,
  dataModalStore,
  openDataModal,
  resetModalConversation,
  saveModalAsWord,
  sendModalFollowup,
  showToast,
  type DataModalState,
} from '../shared/thread/data-modal';
import { ToastRegion } from '../shared/ui/ToastRegion';
import { clear as clearToasts } from '../shared/ui/toast-store';

vi.mock('../api/sse', () => ({ postSse: vi.fn() }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const INITIAL: DataModalState = {
  open: false,
  title: '',
  contentMd: null,
  conversation: [],
  followupTurns: [],
};

let container: HTMLDivElement;
let root: Root;
// This node/jsdom combo ships a broken localStorage (getItem is not a
// function) — stub a Map-backed store: sendModalFollowup reads 'adt-lang'
// through wrapResponseLang, and DataModal renders t() strings.
let lsStore: Map<string, string>;

beforeEach(() => {
  lsStore = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => lsStore.get(k) ?? null,
    setItem: (k: string, v: string) => void lsStore.set(k, String(v)),
    removeItem: (k: string) => void lsStore.delete(k),
  });
  dataModalStore.set({ ...INITIAL });
  clearToasts();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(
      <>
        <DataModal />
        <ToastRegion />
      </>,
    );
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  clearToasts();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function overlay(): HTMLElement {
  return document.getElementById('data-modal-overlay')!;
}

describe('<DataModal/> store-driven rendering', () => {
  it('keeps the closed contractual markup inert and activates it when opened', () => {
    expect(overlay().style.display).toBe('none');
    expect(overlay().getAttribute('aria-hidden')).toBe('true');
    expect(overlay().getAttribute('role')).toBeNull();
    act(() => openDataModal('Contract', 'Body'));
    for (const id of ['data-modal-overlay', 'data-modal-title', 'data-modal-content', 'data-modal-chat-input', 'data-modal-followup']) {
      expect(document.getElementById(id)).not.toBeNull();
    }
  });

  it('openDataModal(title, md) opens and renders sanitized markdown', () => {
    act(() => openDataModal('Line 2 OEE', 'This is **important** data.'));
    expect(overlay().style.display).toBe('flex');
    expect(overlay().getAttribute('role')).toBe('dialog');
    expect(overlay().getAttribute('aria-modal')).toBe('true');
    expect(document.getElementById('data-modal-title')!.textContent).toBe('Line 2 OEE');
    const content = document.getElementById('data-modal-content')!;
    expect(content.innerHTML).toContain('<strong>important</strong>');
    // Conversation context seeded for follow-ups.
    expect(dataModalStore.get().conversation).toEqual([
      { role: 'user', text: 'Line 2 OEE' },
      { role: 'assistant', text: 'This is **important** data.' },
    ]);
  });

  it('owns the viewport and locks body scrolling only while open', () => {
    expect(overlay().parentElement).toBe(document.body);
    expect(document.body.style.overflow).toBe('');

    act(() => openDataModal('Viewport report', 'Body'));
    expect(document.body.style.overflow).toBe('hidden');

    act(() => closeDataModal());
    expect(document.body.style.overflow).toBe('');
  });

  it('neutralizes XSS in the markdown path', () => {
    act(() => openDataModal('T', 'Safe.<script>window.__pwned = true;</script>'));
    expect(document.getElementById('data-modal-content')!.querySelector('script')).toBeNull();
    expect((window as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it('renders raw HTML in markdown as inert literal text (react-markdown)', () => {
    act(() => openDataModal('T', 'Before <img src="x" onerror="alert(1)"> after'));
    const content = document.getElementById('data-modal-content')!;
    expect(content.querySelector('img')).toBeNull();
    expect(content.querySelector('[onerror]')).toBeNull();
    expect(content.textContent).toContain('Before');
    expect(content.textContent).toContain('after');
  });

  it('labels the dialog, focuses its composer, closes on Escape, and restores focus', () => {
    const trigger = document.createElement('button');
    trigger.textContent = 'Open report';
    document.body.appendChild(trigger);
    trigger.focus();

    act(() => openDataModal('Line 2 OEE', 'body'));
    const dialog = overlay();
    expect(dialog.getAttribute('aria-labelledby')).toBe('data-modal-title');
    expect(document.getElementById('data-modal-title')!.textContent).toBe('Line 2 OEE');
    expect(document.activeElement).toBe(document.getElementById('data-modal-chat-input'));

    act(() => {
      dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(overlay().style.display).toBe('none');
    expect(overlay().getAttribute('aria-hidden')).toBe('true');
    expect(document.activeElement).toBe(trigger);
    expect(dataModalStore.get().contentMd).toBe('body');
    trigger.remove();
  });

  it('keeps overlay-click dismissal behavior', () => {
    act(() => openDataModal('T', 'body'));
    const dialog = overlay();
    act(() => dialog.click());
    expect(overlay().style.display).toBe('none');
    expect(overlay().getAttribute('aria-hidden')).toBe('true');
  });

  it('closeDataModal hides the dialog without clearing store content', () => {
    act(() => openDataModal('T', 'body'));
    act(() => closeDataModal());
    expect(overlay().style.display).toBe('none');
    expect(overlay().getAttribute('aria-hidden')).toBe('true');
    expect(dataModalStore.get().contentMd).toBe('body');
  });

  it('dispatches compatibility notices into ToastRegion without an imperative toast sink', () => {
    act(() => showToast('Report ready'));
    const notice = document.querySelector('[data-toast-kind="info"]');
    expect(notice?.textContent).toContain('Report ready');
    expect(notice?.closest('[role="status"]')).not.toBeNull();
    expect(showToast.toString()).not.toContain('document.createElement');
  });

  it('resetModalConversation clears the follow-up context (ChatSidebar reset)', () => {
    act(() => openDataModal('T', 'ctx'));
    expect(dataModalStore.get().conversation.length).toBe(2);
    // ChatSidebar's "+ New" / rehydrate reset dispatches the store action
    // (W3.1 — the legacy window._modalConversation bridge is gone).
    act(() => resetModalConversation());
    expect(dataModalStore.get().conversation).toEqual([]);
  });
});

describe('Word export reads the store', () => {
  it('exports store content, not #data-modal-content.innerHTML', () => {
    act(() => openDataModal('Export Title', 'Hello **world**'));
    // Capture the exported html via the Blob constructor (jsdom's Blob has
    // no .text()) and neuter the download plumbing.
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
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    try {
      saveModalAsWord();
    } finally {
      vi.unstubAllGlobals();
    }

    expect(click).toHaveBeenCalledTimes(1);
    expect(parts.length).toBe(1);
    const html = String(parts[0][1]);
    expect(html).toContain('Export Title');
    expect(html).toContain('<strong>world</strong>');
  });

  it('exports the telemetry text sections it shows, escaped and literal', () => {
    /* Reproduction (round 8 review, MED): the modal showed the drill-down's
     * typed telemetry lines, but the Word export built its file from the
     * markdown and follow-ups only, so the downloaded report dropped them.
     * They are now exported as escaped text, never markdown or chart
     * parsing, so a line holding either stays literal. */
    const line = 'Telemetry read: rig-5d1e / **axis** <chart-block kind="notice" title="X"/>, 24 points';
    act(() =>
      openDataModal('System', '1 time-series: 24 data points total', {
        kind: 'inv-node',
        calls: [{ telemetryCoverage: [line] }],
      }),
    );
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
    try {
      saveModalAsWord();
    } finally {
      vi.unstubAllGlobals();
    }
    const html = String(parts[0][1]);
    expect(html).toContain('1 time-series: 24 data points total');
    expect(html).toContain('<h4>Request 1</h4>');
    expect(html).toContain(
      'Telemetry read: rig-5d1e / **axis** &lt;chart-block kind=&quot;notice&quot; title=&quot;X&quot;/&gt;, 24 points',
    );
    expect(html).not.toContain('<strong>axis</strong>');
  });
});

describe('bounded modal content + shared Send treatment', () => {
  // jsdom rewrites import.meta.url to an http: URL — resolve from the vitest
  // cwd (agentic_digital_thread/frontend) instead.
  const css = readFileSync(resolve(process.cwd(), 'src/styles/dashboard.css'), 'utf-8');

  it('keeps the follow-up row outside the independently scrolling content region', () => {
    const row = document.getElementById('data-modal-followup')!;
    const content = document.getElementById('data-modal-content')!;
    expect(row.classList.contains('data-modal-followup')).toBe(true);
    expect(row.closest('.data-modal')).not.toBeNull();
    expect(content.contains(row)).toBe(false);
  });

  it('dashboard.css makes the overlay viewport-fixed and bounds scrolling to the content region', () => {
    const overlayRule = css.match(/\.data-modal-overlay\s*\{[^}]*\}/)![0];
    expect(overlayRule).toContain('position:fixed');
    expect(overlayRule).toContain('inset:0');
    expect(overlayRule).toMatch(/z-index:\s*(?:[1-9]\d{3,})/);

    const modalRule = css.match(/\.data-modal\s*\{[^}]*\}/)![0];
    expect(modalRule).toContain('max-height:85vh');
    expect(modalRule).toContain('overflow:hidden');

    const bodyRule = css.match(/\.data-modal-body\s*\{[^}]*\}/)![0];
    expect(bodyRule).toContain('max-height:calc(85vh');
    expect(bodyRule).toContain('overflow-y:auto');
  });

  it('the modal Send button uses the SHARED .btn-send treatment (chat-panel look), not inline colors', () => {
    const row = document.getElementById('data-modal-followup')!;
    const send = row.querySelector<HTMLButtonElement>('button.btn-send')!;
    expect(send).not.toBeNull();
    // The blue-bg/black-text inline fork is gone — treatment comes from CSS.
    expect(send.style.background).toBe('');
    expect(send.style.color).toBe('');
  });

  it('ONE dashboard.css rule paints both surfaces: .btn-send grouped with .chat-input button', () => {
    // The single-source treatment rule lists both selectors and the chat
    // panel colors (accent background, var(--bg) text — NOT #000).
    const rule = css.match(/\.btn-send,\s*\n?\s*\.chat-input button\s*\{[^}]*\}/)![0];
    expect(rule).toContain('background: var(--accent)');
    expect(rule).toContain('color: var(--bg)');
    // No competing hardcoded-color Send treatment remains in the modal path.
    expect(css.match(/\.btn-send[^{]*\{[^}]*#000/)).toBeNull();
  });
});

describe('modal follow-up streaming (store-driven)', () => {
  it('renders a VISIBLE loading turn (question echo + animated dots) before the first token', async () => {
    act(() => openDataModal('Topic', 'Original answer'));
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    vi.mocked(postSse).mockImplementation(async () => {
      await gate;
    });

    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'why?';
    let sendPromise!: Promise<void>;
    act(() => {
      sendPromise = sendModalFollowup();
    });

    const content = document.getElementById('data-modal-content')!;
    // The user's question is echoed (the composer was cleared on send).
    expect(content.textContent).toContain('Follow-up: why?');
    expect(content.textContent).toContain('Thinking');
    // Animated three-dot indicator: dot-bounce targets `.loading-dots span`
    // (dashboard.css) — the dots MUST have child spans or nothing animates.
    const dots = content.querySelector('.loading-dots')!;
    expect(dots).not.toBeNull();
    expect(dots.querySelectorAll('span')).toHaveLength(3);

    await act(async () => {
      release();
      await sendPromise;
    });
  });

  it('pins the .data-modal-body content scroller when a follow-up turn is appended', async () => {
    act(() => openDataModal('Topic', 'Original answer'));
    // The content region is the sole vertical scroller; the modal shell keeps
    // its header and composer visible. jsdom has no layout, so stub heights to
    // prove which element receives the scroll update.
    const modal = document.querySelector('.data-modal') as HTMLElement;
    const body = document.querySelector('.data-modal-body') as HTMLElement;
    Object.defineProperty(modal, 'scrollHeight', { value: 2000, configurable: true });
    Object.defineProperty(body, 'scrollHeight', { value: 1800, configurable: true });
    modal.scrollTop = 0;
    body.scrollTop = 0;

    vi.mocked(postSse).mockImplementation(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'answer' });
    });
    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'why?';
    await act(async () => {
      await sendModalFollowup();
    });

    expect(body.scrollTop).toBe(1800);
    expect(modal.scrollTop).toBe(0);
  });

  it('streams a follow-up turn into the modal body and extends the context', async () => {
    act(() => openDataModal('Topic', 'Original answer'));
    const mocked = vi.mocked(postSse);
    mocked.mockImplementation(async (_url, _body, onEvent) => {
      onEvent('token', { text: 'Streamed ' });
      onEvent('token', { text: '**answer**' });
    });

    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'why?';
    await act(async () => {
      await sendModalFollowup();
    });

    const content = document.getElementById('data-modal-content')!;
    expect(content.textContent).toContain('Follow-up: why?');
    const answer = content.querySelector('.followup-answer')!;
    expect(answer.innerHTML).toContain('<strong>answer</strong>');
    // Context extended with the clean exchange.
    const convo = dataModalStore.get().conversation;
    expect(convo[convo.length - 2]).toEqual({ role: 'user', text: 'why?' });
    expect(convo[convo.length - 1].text).toContain('Streamed');
  });

  it('renders a visible error state when the stream fails', async () => {
    act(() => openDataModal('Topic', 'Original answer'));
    vi.mocked(postSse).mockRejectedValue(new Error('boom'));

    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'why?';
    await act(async () => {
      await sendModalFollowup();
    });

    expect(document.getElementById('data-modal-content')!.textContent).toContain('Error: boom');
    // The question echo stays visible in the error box.
    expect(document.getElementById('data-modal-content')!.textContent).toContain('Follow-up: why?');
  });

  it('renders a chart-block in a DONE follow-up answer as a sandboxed iframe, never as text', async () => {
    act(() => openDataModal('Topic', 'Original answer'));
    vi.mocked(postSse).mockImplementation(async (_url, _body, onEvent) => {
      onEvent('token', {
        text:
          'Trend below ' +
          `<chart-block type='line' labels='["a","b"]' datasets='[{"label":"x","data":[1,2]}]'/>` +
          ' end.',
      });
    });

    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'chart it';
    await act(async () => {
      await sendModalFollowup();
    });

    const answer = document.getElementById('data-modal-content')!.querySelector('.followup-answer')!;
    // The chart renders (an improvement over the old DOMPurify strip)...
    const iframe = answer.querySelector('iframe')!;
    expect(iframe).not.toBeNull();
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts');
    // ...and the tag source is NEVER visible as escaped text.
    expect(answer.textContent).not.toContain('chart-block');
    expect(answer.textContent).toContain('Trend below');
    expect(answer.textContent).toContain('end.');
  });

  it('hides chart-tag source (even a partial tail) in the rAF-coalesced streaming preview', async () => {
    act(() => openDataModal('Topic', 'Original answer'));
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    vi.mocked(postSse).mockImplementation(async (_url, _body, onEvent) => {
      onEvent('token', { text: "Partial <chart-block type='line' labels='[\"a" });
      await gate;
      onEvent('token', { text: '","b"]\' datasets=\'[]\'/> tail' });
    });

    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'chart it';
    let sendPromise!: Promise<void>;
    act(() => {
      sendPromise = sendModalFollowup();
    });

    // Flush the coalesced rAF patch, then inspect the mid-stream preview.
    await act(async () => {
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    });
    const content = document.getElementById('data-modal-content')!;
    expect(content.textContent).toContain('Partial');
    expect(content.textContent).not.toContain('chart-block');

    await act(async () => {
      release();
      await sendPromise;
    });
    expect(content.textContent).toContain('tail');
    expect(content.textContent).not.toContain('chart-block');
  });
});

describe('modal follow-up answer selection (same rule as the main chat)', () => {
  const REPORT = 'FULL REPORT: four customers received parts from the lot. '.repeat(5);
  const PLANNING =
    'The shipment lines came back, so next I will price them by joining each line to its order line. '.repeat(3);
  const SHORT = 'Total: 110 units.';

  async function ask(events: Array<[string, unknown]>): Promise<string> {
    act(() => openDataModal('Topic', 'Original answer'));
    vi.mocked(postSse).mockImplementation(async (_url, _body, onEvent) => {
      for (const [type, data] of events) onEvent(type, data);
    });
    const input = document.getElementById('data-modal-chat-input') as HTMLInputElement;
    input.value = 'why?';
    await act(async () => {
      await sendModalFollowup();
    });
    const turns = dataModalStore.get().followupTurns;
    return turns[turns.length - 1].answerMd;
  }

  it.each(['save_correlation', 'digital-thread___save_correlation'])(
    'keeps the report before a %s call and the epilogue after it',
    async (tool) => {
      const answer = await ask([
        ['token', { text: 'Now I will query ERP.' }],
        ['tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: {} }],
        ['tool_end', { tool: 'erp___query_erp', tool_id: 't1' }],
        ['token', { text: REPORT }],
        ['tool_start', { tool, tool_id: 't2', input: {} }],
        ['tool_end', { tool, tool_id: 't2' }],
        ['token', { text: 'Correlation saved.' }],
      ]);
      expect(answer).toBe(`${REPORT}\n\nCorrelation saved.`.trim());
      expect(answer).not.toContain('Now I will query ERP.');
    },
  );

  it('keeps a short answer after a longer planning run and a data call', async () => {
    expect(PLANNING.length).toBeGreaterThan(SHORT.length);
    const answer = await ask([
      ['token', { text: PLANNING }],
      ['tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: {} }],
      ['tool_end', { tool: 'erp___query_erp', tool_id: 't1' }],
      ['token', { text: SHORT }],
    ]);
    expect(answer).toBe(SHORT);
  });

  it('stores the selected answer, not the planning, in the conversation context', async () => {
    await ask([
      ['token', { text: PLANNING }],
      ['tool_start', { tool: 'erp___query_erp', tool_id: 't1', input: {} }],
      ['tool_end', { tool: 'erp___query_erp', tool_id: 't1' }],
      ['token', { text: SHORT }],
    ]);
    const convo = dataModalStore.get().conversation;
    expect(convo[convo.length - 1]).toEqual({ role: 'assistant', text: SHORT });
  });
});
