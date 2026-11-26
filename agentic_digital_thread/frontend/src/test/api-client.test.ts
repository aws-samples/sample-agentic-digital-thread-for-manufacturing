// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, authFetch, getJson, postJson, requestJson } from '../api/client';

function response(status: number, body = '', statusText = ''): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    text: async () => body,
    json: async () => JSON.parse(body) as unknown,
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('requestJson', () => {
  it('returns typed JSON and uses same-origin cookie credentials without bearer auth', async () => {
    interface Payload {
      count: number;
      label: string;
    }
    const fetchMock = vi.fn().mockResolvedValue(response(200, '{"count":2,"label":"ready"}'));
    const setItem = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('localStorage', { getItem: vi.fn(), setItem });

    const result = await requestJson<Payload>('/api/example');

    expect(result.count).toBe(2);
    expect(result.label).toBe('ready');
    expect(fetchMock).toHaveBeenCalledWith('/api/example', { credentials: 'same-origin' });
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toBeUndefined();
    expect(setItem).not.toHaveBeenCalled();
  });

  it.each([
    ['detail', { detail: 'specific detail' }, 'specific detail'],
    ['error', { error: 'error field' }, 'error field'],
    ['message', { message: 'message field' }, 'message field'],
    [
      'priority',
      { detail: 'first', error: 'second', message: 'third' },
      'first',
    ],
  ])('extracts the %s JSON error envelope', async (_label, envelope, expected) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(422, JSON.stringify(envelope))));

    await expect(requestJson('/api/failure')).rejects.toMatchObject({ detail: expected });
  });

  it('uses invalid response text as detail and status text for an empty body', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(502, 'upstream exploded'))
      .mockResolvedValueOnce(response(503, '', 'Service Unavailable'))
      .mockResolvedValueOnce(response(500));
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestJson('/api/invalid')).rejects.toMatchObject({
      detail: 'upstream exploded',
    });
    await expect(requestJson('/api/empty')).rejects.toMatchObject({
      detail: 'Service Unavailable',
    });
    await expect(requestJson('/api/status')).rejects.toMatchObject({ detail: 'HTTP 500' });
  });

  it('rejects non-2xx responses with all ApiError fields', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(response(409, '{"detail":"revision conflict"}')),
    );

    const error = await requestJson('/api/widgets/7').catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({
      name: 'ApiError',
      message: 'revision conflict',
      detail: 'revision conflict',
      status: 409,
      url: '/api/widgets/7',
    });
  });

  it('redirects a 401 to login and rejects instead of returning usable data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(401, '{"message":"expired"}')));
    vi.stubGlobal('location', { href: 'http://localhost/app' });

    await expect(requestJson('/api/private')).rejects.toMatchObject({
      status: 401,
      detail: 'expired',
    });
    expect(window.location.href).toBe('/login.html');
  });

  it('forwards the caller signal and preserves AbortError', async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        });
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const pending = requestJson('/api/slow', { signal: controller.signal });
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('preserves caller headers, method, body, and signal', async () => {
    const signal = new AbortController().signal;
    const headers = new Headers({ 'Content-Type': 'application/json', 'X-Trace': 'trace-1' });
    const body = JSON.stringify({ enabled: true });
    const fetchMock = vi.fn().mockResolvedValue(response(200, '{"saved":true}'));
    vi.stubGlobal('fetch', fetchMock);

    await requestJson('/api/settings', { method: 'PATCH', headers, body, signal });

    expect(fetchMock).toHaveBeenCalledWith('/api/settings', {
      method: 'PATCH',
      headers,
      body,
      signal,
      credentials: 'same-origin',
    });
  });
});

describe('compatibility wrappers', () => {
  it('getJson and postJson delegate to the checked typed request behavior', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response(200, '{"value":1}'))
      .mockResolvedValueOnce(response(200, '{"value":2}'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getJson<{ value: number }>('/api/value')).resolves.toEqual({ value: 1 });
    await expect(postJson<{ value: number }>('/api/value', { next: 2 })).resolves.toEqual({
      value: 2,
    });
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/value', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"next":2}',
      credentials: 'same-origin',
    });
  });

  it('authFetch preserves its temporary raw Response and null-on-redirect contract', async () => {
    const raw = response(204);
    const fetchMock = vi.fn().mockResolvedValueOnce(raw).mockResolvedValueOnce(response(401));
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('location', { href: 'http://localhost/app' });

    await expect(authFetch('/api/raw', { headers: { Accept: 'text/event-stream' } })).resolves.toBe(
      raw,
    );
    await expect(authFetch('/api/raw')).resolves.toBeNull();
    expect(window.location.href).toBe('/login.html');
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/raw', {
      headers: { Accept: 'text/event-stream' },
      credentials: 'same-origin',
    });
  });
});
