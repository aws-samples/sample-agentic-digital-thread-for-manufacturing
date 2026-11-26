// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* T8 (ADR-0054 C4a): <BusinessContextCard/> — GET on mount, pinned PUT body
 * shape, honest runtime_refresh rendering, readable 400 reasons, 2000-char
 * cap. The context text is untrusted user input: assertions read textContent /
 * controlled values only — the component has no HTML-string sink. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BUSINESS_CONTEXT_MAX_LEN,
  BusinessContextCard,
} from '../views/onboard/components/BusinessContextCard';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const GET_URL = '/api/admin/business-context/mes-1';

const GET_BODY = {
  system_id: 'mes-1',
  business_context: 'Plant A execution system — <b>oven line</b> & <script>alert(1)</script>',
  enabled: true,
  updated_at: '2026-07-27T18:00:00Z',
  context_hash: 'a1b2c3d4e5f6',
};

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 400 ? 'Request failed' : 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

type FetchCall = { url: string; init?: RequestInit };

/** Stub fetch: GET returns `getBody`; PUT returns `putResponse`. Records calls. */
function stubFetch(putResponse: Response, getBody: unknown = GET_BODY): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url !== GET_URL) throw new Error(`unexpected URL: ${url}`);
      if (init?.method === 'PUT') return putResponse;
      return response(getBody);
    }),
  );
  return calls;
}

async function mount() {
  await act(async () => {
    root.render(<BusinessContextCard systemId="mes-1" />);
  });
}

function textarea(): HTMLTextAreaElement {
  return container.querySelector<HTMLTextAreaElement>(
    'textarea[aria-label="Business context"]',
  )!;
}

function setTextareaValue(value: string) {
  const el = textarea();
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    'value',
  )!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function findButton(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll('button')).find(
    (b) => b.textContent === label,
  );
}

/** Open the T1.5 pre-write warning (first Save click — no PUT yet). */
async function clickSaveButton() {
  await act(async () => {
    findButton('Save')!.click();
  });
}

/** Full save flow: Save → confirm through the restart warning. */
async function clickSave() {
  await clickSaveButton();
  await act(async () => {
    findButton('Save and restart')!.click();
  });
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
  vi.restoreAllMocks();
});

describe('BusinessContextCard', () => {
  it('test_loads_and_displays_context', async () => {
    const calls = stubFetch(response({}));
    await mount();
    await vi.waitFor(() => {
      expect(textarea()).not.toBeNull();
    });
    // GET on open, exact C4a route.
    expect(calls[0].url).toBe(GET_URL);
    expect(calls[0].init?.method).toBeUndefined();
    // Untrusted text lands as the controlled value — never parsed as HTML.
    expect(textarea().value).toBe(GET_BODY.business_context);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    const toggle = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(container.textContent).toContain('v:a1b2c3d4e5f6');
    expect(container.querySelector('[data-testid="bc-char-count"]')?.textContent).toBe(
      `${GET_BODY.business_context.length} / 2000`,
    );
  });

  it('test_save_puts_pinned_body_shape', async () => {
    const calls = stubFetch(
      response({
        status: 'ok',
        system_id: 'mes-1',
        enabled: false,
        context_hash: 'feedbeef0123',
        runtime_refresh: 'ok',
      }),
    );
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());

    setTextareaValue('MES for the oven line');
    const toggle = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    await act(async () => {
      toggle.click(); // enabled: true → false
    });
    await clickSave();

    const put = calls.find((c) => c.init?.method === 'PUT')!;
    expect(put.url).toBe(GET_URL);
    // Pinned C4a body — exactly these two keys.
    expect(JSON.parse(put.init!.body as string)).toEqual({
      business_context: 'MES for the oven line',
      enabled: false,
    });
    const line = container.querySelector('[data-testid="bc-save-result"]')!;
    expect(line.textContent).toContain('Saved ✓ — agents refresh in ~1–2 min.');
    // context_hash from the PUT response replaces the loaded one.
    expect(container.textContent).toContain('v:feedbeef0123');
  });

  it('test_400_reason_rendered', async () => {
    stubFetch(
      response(
        {
          status: 'validation_failed',
          reason: 'secret_like_content',
          detail:
            'business context matches a secret-shaped pattern ' +
            '(credential, key, or token) — remove it and retry',
        },
        400,
      ),
    );
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());
    await clickSave();

    const line = container.querySelector('[data-testid="bc-save-result"]')!;
    // Server detail verbatim…
    expect(line.textContent).toContain('secret-shaped pattern');
    expect(line.textContent).toContain('remove it and retry');
    // …plus the WHY: prompts/durable storage would expose a pasted secret.
    expect(line.textContent).toContain('rendered into agent prompts');
    expect(line.textContent).toContain('rotate any real secret');
  });

  it('test_400_too_long_reason_rendered', async () => {
    stubFetch(
      response(
        {
          status: 'validation_failed',
          reason: 'too_long',
          detail: "'business_context' exceeds 2000 chars after strip",
        },
        400,
      ),
    );
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());
    await clickSave();

    const line = container.querySelector('[data-testid="bc-save-result"]')!;
    expect(line.textContent).toContain('Not saved — ');
    expect(line.textContent).toContain("'business_context' exceeds 2000 chars after strip");
  });

  it('test_refresh_failed_warning_shown', async () => {
    stubFetch(
      response({
        status: 'ok',
        system_id: 'mes-1',
        enabled: true,
        context_hash: 'a1b2c3d4e5f6',
        runtime_refresh: 'failed',
      }),
    );
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());
    await clickSave();

    const line = container.querySelector('[data-testid="bc-save-result"]')!;
    // Honest: saved durably, but the running agent was NOT refreshed.
    expect(line.textContent).toContain('Saved, but the runtime refresh failed');
    expect(line.textContent).toContain('stored durably');
    expect(line.textContent).toContain('NOT updated');
  });

  it('test_refresh_skipped_neutral_note_shown', async () => {
    stubFetch(
      response({
        status: 'ok',
        system_id: 'mes-1',
        enabled: true,
        context_hash: 'a1b2c3d4e5f6',
        runtime_refresh: 'skipped',
      }),
    );
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());
    await clickSave();

    const line = container.querySelector('[data-testid="bc-save-result"]')!;
    expect(line.textContent).toContain('Runtime refresh skipped');
  });

  it('test_char_limit_enforced', async () => {
    stubFetch(response({}));
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());

    expect(textarea().maxLength).toBe(BUSINESS_CONTEXT_MAX_LEN);
    // jsdom does not enforce maxLength — the onChange clamp must.
    setTextareaValue('x'.repeat(BUSINESS_CONTEXT_MAX_LEN + 500));
    expect(textarea().value.length).toBe(BUSINESS_CONTEXT_MAX_LEN);
    expect(container.querySelector('[data-testid="bc-char-count"]')?.textContent).toBe(
      '2000 / 2000',
    );
  });

  // ── T1.5 pre-write restart warning ──────────────────────────────────────

  it('test_prewrite_warning_shows_real_count_before_any_put', async () => {
    const calls = stubFetch(response({}));
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());

    await clickSaveButton();
    const warning = container.querySelector('[data-testid="bc-roll-warning"]')!;
    // Real affected-system count (this card rolls exactly ONE system) —
    // wired from the affected-system set, not a hardcoded string.
    expect(warning.textContent).toContain('Saving will restart 1 system agent');
    expect(warning.textContent).toContain('~2 min');
    expect(warning.textContent).toContain('answers may briefly degrade');
    // The warning appears BEFORE the write: no PUT has been issued yet.
    expect(calls.filter((c) => c.init?.method === 'PUT')).toHaveLength(0);
  });

  it('test_prewrite_warning_cancel_issues_no_put', async () => {
    const calls = stubFetch(response({}));
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());

    await clickSaveButton();
    await act(async () => {
      findButton('Cancel')!.click();
    });
    expect(container.querySelector('[data-testid="bc-roll-warning"]')).toBeNull();
    expect(calls.filter((c) => c.init?.method === 'PUT')).toHaveLength(0);
  });

  it('test_prewrite_warning_confirm_issues_put', async () => {
    const calls = stubFetch(
      response({
        status: 'ok',
        system_id: 'mes-1',
        enabled: true,
        context_hash: 'a1b2c3d4e5f6',
        runtime_refresh: 'ok',
        readiness: {
          'mes-1': { status: 'ok', readiness: 'ready_verified', detail: null },
        },
      }),
    );
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());

    await clickSave();
    expect(calls.filter((c) => c.init?.method === 'PUT')).toHaveLength(1);
    // Warm-up verified readiness (T1.5c) surfaces honestly in the save line.
    const line = container.querySelector('[data-testid="bc-save-result"]')!;
    expect(line.textContent).toContain('restarted and verified');
  });

  it('collapses to an Admin only note on a 403 GET', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => response({ detail: 'Forbidden' }, 403)),
    );
    await mount();
    await vi.waitFor(() => {
      expect(container.textContent).toContain('Admin only');
    });
    expect(textarea()).toBeNull();
  });

  it('shows the intake hints as a collapsed disclosure', async () => {
    stubFetch(response({ status: 'ok' }));
    await mount();
    const details = container.querySelector('details');
    expect(details).not.toBeNull();
    const text = details!.textContent ?? '';
    expect(text).toMatch(/what makes good context/i);
    // Generic wording only — the engine neutrality discipline applies to UI strings.
    expect(text).toMatch(/unit of work/i);
    expect(text).toMatch(/workflow noise/i);
    expect(text).toMatch(/does NOT cover/i);
    // KPI formulas (intake question 10): the denominator is the part that gets
    // silently invented when nobody states it.
    expect(text).toMatch(/denominator/i);
    expect(container.querySelectorAll('details li').length).toBe(8);
  });
});

// ── AI assist (POST /api/onboard/guided/refine — stateless, read-only) ──────

const REFINE_URL = '/api/onboard/guided/refine';

/** Stub fetch for the assist flow: GET serves the card, POST /refine serves
 * `refineResponse`. Records calls like stubFetch. */
function stubAssistFetch(refineResponse: Response): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url === GET_URL) return response(GET_BODY);
      if (url === REFINE_URL && init?.method === 'POST') return refineResponse;
      throw new Error(`unexpected URL: ${url}`);
    }),
  );
  return calls;
}

function setInputValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function instructionInput(): HTMLInputElement {
  return container.querySelector<HTMLInputElement>(
    'input[aria-label="Revision instruction"]',
  )!;
}

function previewTextarea(): HTMLTextAreaElement | null {
  return container.querySelector<HTMLTextAreaElement>(
    'textarea[aria-label="Suggested revision"]',
  );
}

/** Open the assist, type an instruction, and ask for a suggestion. */
async function askForSuggestion(instruction: string) {
  await mount();
  await vi.waitFor(() => expect(textarea()).not.toBeNull());
  await act(async () => {
    findButton('Improve with AI')!.click();
  });
  setInputValue(instructionInput(), instruction);
  await act(async () => {
    findButton('Suggest revision')!.click();
  });
}

describe('BusinessContextAssist (AI assist)', () => {
  // The draft is untrusted LLM output over untrusted steward text — it must
  // only ever appear as a controlled plain-text value, never parsed as HTML.
  const DRAFT =
    'Execution records for the oven line. <b>bold</b> <script>alert(1)</script> Excludes rework.';
  const NOTES = 'Tightened wording; the KPI-definitions topic remains unaddressed.';

  it('is collapsed to one button by default — no panel, manual flow untouched', async () => {
    stubFetch(response({}));
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());
    expect(container.querySelector('[data-testid="bc-assist"]')).toBeNull();
    expect(findButton('Improve with AI')).not.toBeUndefined();
    // Collapsed assist issues no request beyond the card's GET.
  });

  it('instruction → suggest → preview → "Use this draft" replaces the textarea value', async () => {
    const calls = stubAssistFetch(response({ draft: DRAFT, notes: NOTES }));
    await askForSuggestion('Tighten the wording.');

    await vi.waitFor(() => expect(previewTextarea()).not.toBeNull());
    // Pinned request shape: the CURRENT textarea text rides along (stateless).
    const post = calls.find((c) => c.url === REFINE_URL)!;
    expect(JSON.parse(post.init!.body as string)).toEqual({
      system_id: 'mes-1',
      instruction: 'Tighten the wording.',
      current_text: GET_BODY.business_context,
    });
    // Draft renders ONLY as a controlled value + notes as a text node.
    expect(previewTextarea()!.value).toBe(DRAFT);
    expect(previewTextarea()!.readOnly).toBe(true);
    expect(container.querySelector('[data-testid="bc-assist-notes"]')?.textContent).toBe(NOTES);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('b')).toBeNull();

    await act(async () => {
      findButton('Use this draft')!.click();
    });
    // The MAIN textarea now carries the draft; preview is gone; nothing saved
    // (no PUT — saving stays the Save flow with its roll warning).
    expect(textarea().value).toBe(DRAFT);
    expect(previewTextarea()).toBeNull();
    expect(calls.filter((c) => c.init?.method === 'PUT')).toHaveLength(0);
  });

  it('"Discard" leaves the textarea untouched', async () => {
    stubAssistFetch(response({ draft: DRAFT, notes: '' }));
    await askForSuggestion('Tighten the wording.');
    await vi.waitFor(() => expect(previewTextarea()).not.toBeNull());

    await act(async () => {
      findButton('Discard')!.click();
    });
    expect(previewTextarea()).toBeNull();
    expect(textarea().value).toBe(GET_BODY.business_context);
  });

  it('a fetch error shows an honest error line — never a draft', async () => {
    stubAssistFetch(
      response({ detail: 'context assistant unavailable (Timeout) — try again' }, 502),
    );
    await askForSuggestion('Tighten the wording.');

    await vi.waitFor(() =>
      expect(container.querySelector('[data-testid="bc-assist-error"]')).not.toBeNull(),
    );
    const line = container.querySelector('[data-testid="bc-assist-error"]')!;
    expect(line.textContent).toContain('No suggestion —');
    expect(line.textContent).toContain('context assistant unavailable');
    expect(previewTextarea()).toBeNull();
    // The manual textarea is untouched by the failure.
    expect(textarea().value).toBe(GET_BODY.business_context);
  });

  it('Suggest revision stays disabled until an instruction is typed', async () => {
    stubFetch(response({}));
    await mount();
    await vi.waitFor(() => expect(textarea()).not.toBeNull());
    await act(async () => {
      findButton('Improve with AI')!.click();
    });
    expect(findButton('Suggest revision')!.disabled).toBe(true);
    setInputValue(instructionInput(), 'Tighten the wording.');
    expect(findButton('Suggest revision')!.disabled).toBe(false);
  });
});
