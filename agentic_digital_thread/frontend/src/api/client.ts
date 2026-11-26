// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Shared API client — cookie-session auth.
 *
 * Requests stay same-origin and use the browser-managed `adt_session` cookie.
 * There is deliberately no bearer-token storage or Authorization-header
 * injection (see agents/auth/middleware.py). */

/** A checked HTTP failure from a JSON API request. */
export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly detail: string;
  readonly payload: unknown;

  constructor(status: number, url: string, detail: string, payload?: unknown) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.url = url;
    this.detail = detail;
    this.payload = payload;
  }
}

const RAW_RESPONSE = Symbol('rawResponse');

type InternalRequestInit = RequestInit & {
  /** Transitional escape hatch used only by authFetch's Response contract. */
  [RAW_RESPONSE]?: true;
};

function printableDetail(value: unknown): string | null {
  if (typeof value === 'string') return value.trim() || null;
  if (value == null) return null;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}

function errorDetail(response: Response, text: string): string {
  if (text) {
    try {
      const envelope = JSON.parse(text) as unknown;
      if (envelope && typeof envelope === 'object' && !Array.isArray(envelope)) {
        const fields = envelope as Record<string, unknown>;
        for (const key of ['detail', 'error', 'message'] as const) {
          const detail = printableDetail(fields[key]);
          if (detail) return detail;
        }
      }
    } catch {
      // A non-JSON response is still useful server-provided error detail.
    }
    return text;
  }
  return response.statusText || `HTTP ${response.status}`;
}

/**
 * Fetch and decode a same-origin JSON response.
 *
 * The caller's method, headers, body, and AbortSignal are forwarded unchanged.
 * Network failures and AbortError are intentionally allowed to propagate.
 */
export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const internalInit = init as InternalRequestInit;
  const rawResponse = internalInit[RAW_RESPONSE] === true;
  const fetchInit: InternalRequestInit = { ...internalInit, credentials: 'same-origin' };
  delete fetchInit[RAW_RESPONSE];

  const response = await fetch(url, fetchInit);
  // Session-expiry redirect — but never for the login call itself: its 401
  // means "bad credentials" and must surface inline, not reload the page.
  if (response.status === 401 && !url.includes('/api/auth/login')) {
    window.location.href = '/login.html';
    // authFetch temporarily retains its historical null-on-redirect contract.
    if (rawResponse) return null as T;
  }

  // authFetch is retained for streaming/non-JSON callers until their migration.
  if (rawResponse) return response as T;

  if (!response.ok) {
    const text = (await response.text()).trim();
    let payload: unknown;
    if (text) {
      try {
        payload = JSON.parse(text) as unknown;
      } catch {
        payload = undefined;
      }
    }
    throw new ApiError(response.status, url, errorDetail(response, text), payload);
  }

  return (await response.json()) as T;
}

/** Auth-aware raw fetch retained temporarily for streaming/legacy callers. */
export function authFetch(
  url: string,
  options: RequestInit = {},
): Promise<Response | null> {
  return requestJson<Response | null>(url, {
    ...options,
    [RAW_RESPONSE]: true,
  } as InternalRequestInit);
}

/** GET a checked JSON endpoint. */
export function getJson<T>(url: string): Promise<T> {
  return requestJson<T>(url);
}

/** POST a JSON body and decode the checked JSON response. */
export function postJson<T>(url: string, body: unknown): Promise<T> {
  return requestJson<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
