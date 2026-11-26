// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* A user's PostgreSQL login that can do more than read is refused by the
 * connection Edit and by provisioning, with the server's advice; when the
 * login could not be checked the Edit still saves and says so. */

// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import { submitEditConnection } from '../views/onboard/catalog';
import { provisionErrorText } from '../views/onboard/wizard';

const ADVICE = 'Register a dedicated read-only login with CONNECT on the database.';
const UNCHECKED = "the PostgreSQL login's privileges could not be checked, so it was not refused.";

function reply(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('provisionErrorText', () => {
  it('shows the advice for a privileged-login refusal', () => {
    expect(
      provisionErrorText({
        status: 'error',
        error: 'provision_failed',
        reason: 'privileged_login',
        detail: ADVICE,
        retryable: false,
      }),
    ).toBe(ADVICE);
  });

  it('still keeps the bare code for a detail it was not told to show', () => {
    expect(
      provisionErrorText({ status: 'error', error: 'provision_failed', reason: 'other', detail: 'x' }),
    ).toBe('provision_failed');
  });
});

describe('submitEditConnection', () => {
  it('shows the advice when the Edit is refused for a privileged login', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        reply(400, { status: 'validation_failed', reason: 'privileged_login', detail: ADVICE }),
      ),
    );
    const out = await submitEditConnection('lab', [], {});
    expect(out?.text).toBe('✗ Validation failed · ' + ADVICE);
  });

  it('saves but says so when the login could not be checked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply(200, { status: 'ok', reconnected: true, warnings: [UNCHECKED] })),
    );
    const out = await submitEditConnection('lab', [], {});
    expect(out).toEqual({ color: 'var(--amber, #fbbf24)', text: '✓ Reconnected · ' + UNCHECKED });
  });

  it('a checked Edit is unchanged', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => reply(200, { status: 'ok', reconnected: true })));
    const out = await submitEditConnection('lab', [], {});
    expect(out).toEqual({ color: 'var(--green, #4ade80)', text: '✓ Reconnected' });
  });
});
