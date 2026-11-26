// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// @vitest-environment jsdom
/* A REST-family system (Tulip, REST API, MCP) discovers under its base URL,
 * and onboarding stores a join path's table as `<base url>.<table>`. The two
 * join-path tables show the table name only; the stored value is unchanged.
 * A PostgreSQL `schema.table` renders as it always did. Walk 2026-11-12,
 * Team 1: Connections Discovered printed the Tulip base URL as the schema. */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { JoinPath, SystemConnectionsResponse } from '../api/types/onboard';
import { ConnectionsDiscovered } from '../views/onboard/components/ResolveExtras';
import { SystemConnections } from '../views/onboard/components/SystemConnections';
import { displayTable } from '../views/onboard/connection-evidence';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe('displayTable', () => {
  it.each([
    // URL with a path (the API Gateway mock): the last path segment goes too.
    ['https://abc123.execute-api.us-east-1.amazonaws.com/prod.Production Orders', 'Production Orders'],
    // http, and a deeper path.
    ['http://tulip.example.com/api/v3.Work Orders', 'Work Orders'],
    // Trailing slash on the base URL.
    ['https://tulip.example.com/prod/.Defects', 'Defects'],
    // A table name that itself holds a slash.
    ['https://tulip.example.com/prod.A/B Tests', 'A/B Tests'],
    // URL with no path (a tenant host): the table starts at the first part
    // that can't be a lowercase host label.
    ['https://acme.tulip.example.com.Production Orders', 'Production Orders'],
    ['https://acme.tulip.example.com.work_orders', 'work_orders'],
    // Host with a port, no path: the port ends the URL, so a dotted table
    // name is still exact.
    ['http://localhost:8080.Station History', 'Station History'],
    ['http://localhost:8080.orders.v2', 'orders.v2'],
  ])('shows %s as %s', (stored, shown) => {
    expect(displayTable(stored)).toBe(shown);
  });

  it.each([
    'schema_a.table_x', // PostgreSQL schema.table: unchanged
    'erp.shipment_items',
    'NameSpace_MCP',
    '',
    'https://acme.example.com', // nothing after the URL: nothing to strip to
    'https://acme.example.com/prod', // a path with no table part
    'http://localhost:8080.', // a port and an empty table part
    // No path and an all-lowercase one-word table: it reads as a host label,
    // so the boundary is unknown and the stored value is shown as is.
    'https://acme.tulip.example.com.orders',
    'https://ACME.example.com.Orders', // not a lowercase host: left alone
    // A dotted path: the URL/table boundary isn't known, so never guess it
    // (splitting at the first dot showed "json.Production Orders").
    'https://api.example.com/openapi.json.Production Orders',
    'https://api.example.com/v1.2.Production Orders',
    'https://tulip.example.com/prod.Orders.v2',
    // No path, and the part before the table isn't a top-level label.
    'https://acme.tulip.example.co.v1.2 Orders',
  ])('leaves %s unchanged', (stored) => {
    expect(displayTable(stored)).toBe(stored);
  });

  it('passes undefined through as empty', () => {
    expect(displayTable(undefined)).toBe('');
  });
});

const TULIP_JP: JoinPath = {
  from_system: 'tulip-assembly-mes',
  from_table: 'https://abc123.execute-api.us-east-1.amazonaws.com/prod.Production Orders',
  from_field: 'erp_run_number',
  to_system: 'erp',
  to_table: 'erp.shipment_items',
  to_field: 'batch_id',
  method: 'value_exact_match',
  basis: 'identity_rule',
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

function codes(): string[] {
  return [...container.querySelectorAll('tbody code')].map((c) => c.textContent || '');
}

describe('join-path tables show the table name, not the base URL', () => {
  it('Connections Discovered', () => {
    act(() =>
      root.render(
        <ConnectionsDiscovered
          result={{ status: 'done', system_id: 'tulip-assembly-mes', join_paths: [TULIP_JP] }}
        />,
      ),
    );
    expect(codes()).toEqual(['Production Orders.erp_run_number', 'erp.shipment_items.batch_id']);
    expect(container.textContent).not.toContain('https://');
  });

  it("the system's Connections section", async () => {
    const body: SystemConnectionsResponse = {
      system_id: 'tulip-assembly-mes',
      join_paths: [TULIP_JP],
      join_paths_total: 1,
      value_mappings: [],
      value_mappings_total: 0,
      identity_rules: [],
      identity_rules_total: 0,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })),
    );
    await act(async () => root.render(<SystemConnections systemId="tulip-assembly-mes" />));
    await act(async () => {});
    const table = container.querySelector('table[aria-label="Join paths"]')!;
    const cells = [...table.querySelectorAll('tbody code')].map((c) => c.textContent);
    expect(cells).toEqual(['Production Orders.erp_run_number', 'erp.shipment_items.batch_id']);
    expect(table.textContent).not.toContain('https://');
  });
});
