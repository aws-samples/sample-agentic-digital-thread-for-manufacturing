// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Tests — wizard step-error surfacing with Retry (step-retry.ts +
 * flow-store appendRetryError), the port of the merged legacy
 * obStepErrorText/obAppendRetryError/obRetryStep delta. No DOM — the retry
 * click is simulated by invoking the store entry's retry callback, exactly
 * what <ObFlow/>'s Retry button does. */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  flowStore,
  startWizard,
  type FlowEntry,
} from '../views/onboard/flow-store';
import { obRetryStep, obStepErrorText } from '../views/onboard/step-retry';

const entries = () => flowStore.get().entries;
const lastRetryError = () => {
  const errs = entries().filter(
    (e): e is Extract<FlowEntry, { kind: 'error' }> => e.kind === 'error',
  );
  return errs[errs.length - 1];
};

beforeEach(() => {
  startWizard(); // clean entries
});

describe('obStepErrorText', () => {
  const ok = { ok: true, status: 200 };
  const bad = { ok: false, status: 502 };

  it('returns "" on a clean 2xx body', () => {
    expect(obStepErrorText(ok, { tables: [] })).toBe('');
    expect(obStepErrorText(null, {})).toBe('');
  });

  it('surfaces assume_role_denied detail VERBATIM (cross-account probe)', () => {
    expect(
      obStepErrorText(ok, {
        reason: 'assume_role_denied',
        detail: 'Add sts:AssumeRole for arn:aws:iam::111122223333:role/adt-read',
      }),
    ).toBe('Add sts:AssumeRole for arn:aws:iam::111122223333:role/adt-read');
  });

  it('surfaces {error} bodies even on 2xx', () => {
    expect(obStepErrorText(ok, { error: 'boom' })).toBe('boom');
  });

  it('surfaces detail mentioning assume_role_denied without the reason field', () => {
    expect(obStepErrorText(ok, { detail: 'probe: assume_role_denied by SCP' })).toBe(
      'probe: assume_role_denied by SCP',
    );
  });

  it('non-2xx falls back detail → message → HTTP status', () => {
    expect(obStepErrorText(bad, { detail: 'gateway sad' })).toBe('gateway sad');
    expect(obStepErrorText(bad, { message: 'msg' })).toBe('msg');
    expect(obStepErrorText(bad, {})).toBe('Request failed (HTTP 502)');
    expect(obStepErrorText(bad, null)).toBe('Request failed (HTTP 502)');
  });
});

describe('obRetryStep', () => {
  it('resolves immediately with the data on first success (no error entries)', async () => {
    const data = await obRetryStep(async () => ({ ok: true as const, data: 42 }));
    expect(data).toBe(42);
    expect(entries().filter((e) => e.kind === 'error')).toHaveLength(0);
  });

  it('renders a retryable error entry on failure and re-invokes on Retry', async () => {
    let calls = 0;
    const promise = obRetryStep(async () => {
      calls += 1;
      if (calls < 2) return { ok: false as const, error: 'Connection failed: nope' };
      return { ok: true as const, data: 'ok' };
    });
    // First attempt failed — a retryable error entry is in the flow.
    await Promise.resolve(); // let the first attempt settle
    await new Promise((r) => setTimeout(r, 0));
    const err = lastRetryError();
    expect(err.text).toBe('Connection failed: nope');
    expect(err.retry).toBeTypeOf('function');
    // Retry click: entry removed, attempt re-invoked, promise resolves.
    err.retry!();
    await expect(promise).resolves.toBe('ok');
    expect(calls).toBe(2);
    expect(entries().find((e) => e.id === err.id)).toBeUndefined();
  });

  it('treats a thrown exception as a failure with its message', async () => {
    let calls = 0;
    const promise = obRetryStep(async () => {
      calls += 1;
      if (calls < 2) throw new Error('kapow');
      return { ok: true as const, data: 1 };
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(lastRetryError().text).toBe('kapow');
    lastRetryError().retry!();
    await expect(promise).resolves.toBe(1);
  });

  it('falls back to the generic text when the failure has no detail', async () => {
    let calls = 0;
    const promise = obRetryStep(async () => {
      calls += 1;
      if (calls < 2) return { ok: false as const, error: '' };
      return { ok: true as const, data: null };
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(lastRetryError().text).toBe('Step failed (no error detail).');
    lastRetryError().retry!();
    await expect(promise).resolves.toBeNull();
  });

  it('says how to recover when the discovered schema is gone (Retry cannot help)', async () => {
    let calls = 0;
    const promise = obRetryStep(async () => {
      calls += 1;
      if (calls < 2) return { ok: false as const, error: 'Provision failed: schema_provenance_invalid' };
      return { ok: true as const, data: 'ok' };
    });
    await new Promise((r) => setTimeout(r, 0));
    const text = lastRetryError().text;
    expect(text).toContain('Cancel');
    expect(text).toContain('Onboard');
    expect(text).toContain('schema_provenance_invalid'); // the code stays, for support
    expect(text.startsWith('Provision failed:')).toBe(true); // the step name stays
    lastRetryError().retry!();
    await expect(promise).resolves.toBe('ok');
  });

  it('leaves every other error text as the step gave it', async () => {
    let calls = 0;
    const promise = obRetryStep(async () => {
      calls += 1;
      if (calls < 2) return { ok: false as const, error: 'Provision failed: Failed to fetch' };
      return { ok: true as const, data: 1 };
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(lastRetryError().text).toBe('Provision failed: Failed to fetch');
    lastRetryError().retry!();
    await expect(promise).resolves.toBe(1);
  });
});
