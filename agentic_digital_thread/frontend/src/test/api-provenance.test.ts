// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Unit tests — the Tier-3 redemption client (src/api/provenance.ts), so the
 * D5 client is covered directly and not only through the EvidenceCard
 * component tests: exact URL construction (both ids percent-encoded — the
 * query_ref is caller-supplied wire data) and the structured ApiError
 * mapping the expander's feature-detection relies on (403 non-admin,
 * 404 unknown/expired). */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import {
  fetchProvenanceDataset,
  fetchProvenanceQuery,
  type ProvenanceDatasetDetail,
  type ProvenanceQueryDetail,
} from '../api/provenance';
import { jsonResponse } from './fetch-stub';

const DETAIL: ProvenanceQueryDetail = {
  query_ref: 'q-abcdefabcdef',
  session_id: 'sess-1',
  system_id: 'erp',
  tool_name: 'erp___query_erp',
  executed_query: 'SELECT 1',
  kg_artifacts: ['subj-0a1b'],
  captured_at: '2026-08-03T19:00:00Z',
};

function stubFetch(response: Response) {
  const mock = vi.fn(async () => response);
  vi.stubGlobal('fetch', mock);
  return mock;
}

afterEach(() => vi.unstubAllGlobals());

describe('fetchProvenanceQuery (D5 client)', () => {
  it('builds the exact redemption URL and returns the decoded body', async () => {
    const mock = stubFetch(jsonResponse(200, DETAIL));
    await expect(fetchProvenanceQuery('q-abcdefabcdef', 'sess-1')).resolves.toEqual(DETAIL);
    expect(mock).toHaveBeenCalledTimes(1);
    const [url, init] = mock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/admin/agents/traces/queries/q-abcdefabcdef?session_id=sess-1');
    expect(init.credentials).toBe('same-origin'); // cookie-session auth
  });

  it('percent-encodes BOTH ids (hostile wire values cannot break the path)', async () => {
    const mock = stubFetch(jsonResponse(200, DETAIL));
    await fetchProvenanceQuery('q-../evil?x=1', 'sess a&b=c#d');
    const [url] = mock.mock.calls[0] as unknown as [string];
    expect(url).toBe(
      '/api/admin/agents/traces/queries/q-..%2Fevil%3Fx%3D1?session_id=sess%20a%26b%3Dc%23d',
    );
  });

  it('maps the structured 404 to an ApiError the expander feature-detects', async () => {
    stubFetch(jsonResponse(404, { detail: 'Unknown or expired query_ref' }));
    const failure = fetchProvenanceQuery('q-abcdefabcdef', 'sess-1');
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      status: 404,
      detail: 'Unknown or expired query_ref',
    });
  });

  it('maps 403 (non-admin) and 400 (invalid ids) the same structured way', async () => {
    stubFetch(jsonResponse(403, { detail: 'Admin access required' }));
    await expect(fetchProvenanceQuery('q-abcdefabcdef', 'sess-1')).rejects.toMatchObject({
      status: 403,
      detail: 'Admin access required',
    });
    stubFetch(jsonResponse(400, { detail: 'Invalid query_ref' }));
    await expect(fetchProvenanceQuery('q-abcdefabcdef', 'sess-1')).rejects.toMatchObject({
      status: 400,
      detail: 'Invalid query_ref',
    });
  });
});

const DATASET: ProvenanceDatasetDetail = {
  dataset_ref: 'tel-0123456789ab',
  stored_at: 1754300000,
  point_count: 1,
  series: [{ timestamp: '2026-04-30T07:00:00Z', value: 10.5, quality: 'GOOD' }],
};

describe('fetchProvenanceDataset (Wave-4 client)', () => {
  it('builds the exact redemption URL and returns the decoded body', async () => {
    const mock = stubFetch(jsonResponse(200, DATASET));
    await expect(fetchProvenanceDataset('tel-0123456789ab', 'sess-1')).resolves.toEqual(DATASET);
    expect(mock).toHaveBeenCalledTimes(1);
    const [url, init] = mock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/admin/agents/traces/datasets/tel-0123456789ab?session_id=sess-1');
    expect(init.credentials).toBe('same-origin'); // cookie-session auth
  });

  it('percent-encodes BOTH ids (hostile wire values cannot break the path)', async () => {
    const mock = stubFetch(jsonResponse(200, DATASET));
    await fetchProvenanceDataset('tel-../evil?x=1', 'sess a&b=c#d');
    const [url] = mock.mock.calls[0] as unknown as [string];
    expect(url).toBe(
      '/api/admin/agents/traces/datasets/tel-..%2Fevil%3Fx%3D1?session_id=sess%20a%26b%3Dc%23d',
    );
  });

  it('maps the structured 404/403/400 to ApiError the dataset view feature-detects', async () => {
    stubFetch(jsonResponse(404, { detail: 'Unknown or expired dataset_ref' }));
    const missing = fetchProvenanceDataset('tel-0123456789ab', 'sess-1');
    await expect(missing).rejects.toBeInstanceOf(ApiError);
    await expect(missing).rejects.toMatchObject({
      status: 404,
      detail: 'Unknown or expired dataset_ref',
    });
    stubFetch(jsonResponse(403, { detail: 'Admin access required' }));
    await expect(fetchProvenanceDataset('tel-0123456789ab', 'sess-1')).rejects.toMatchObject({
      status: 403,
    });
    stubFetch(jsonResponse(400, { detail: 'Invalid dataset_ref' }));
    await expect(fetchProvenanceDataset('tel-0123456789ab', 'sess-1')).rejects.toMatchObject({
      status: 400,
      detail: 'Invalid dataset_ref',
    });
  });
});
