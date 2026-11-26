// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Shared fetch Response stub for tests that mock the network (the D5
 * redemption client and the EvidenceCard Tier-3 expander) — `ok` derives
 * from the status, and json()/text() serve the same body, matching how
 * api/client.ts reads responses. Not a test file: vitest collects only
 * `*.test.*` under src/test. */

export function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}
