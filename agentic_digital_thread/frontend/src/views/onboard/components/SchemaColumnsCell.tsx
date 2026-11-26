// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* <SchemaColumnsCell/> — the Columns cell of a discovered-schema table.
 *
 * When discovery returned the column names (POST /api/onboard/discover does),
 * the cell is a collapsed fold: the summary is the count, the body lists each
 * name with its type. Without names it degrades to the bare count, or a dash.
 * Lets a user confirm WHAT discovery found at the Discover step instead of
 * first seeing column names at Map. All text renders as JSX (escaped). */
import type { DiscoveredTable } from '../../../api/types/onboard';

function columnLabel(column: { name: string; data_type?: unknown }): string {
  const type = typeof column.data_type === 'string' && column.data_type ? column.data_type : '';
  return type ? `${column.name} (${type})` : column.name;
}

export function SchemaColumnsCell({ table }: { table: DiscoveredTable }) {
  const columns = (Array.isArray(table.columns) ? table.columns : []).filter(
    (c) => c && typeof c.name === 'string' && c.name,
  );
  if (columns.length === 0) {
    return <>{table.column_count || '—'}</>;
  }
  const n = columns.length;
  return (
    <details className="ob-schema-columns">
      <summary>{`${n} column${n === 1 ? '' : 's'}`}</summary>
      <ul>
        {columns.map((c, i) => (
          <li key={i}>{columnLabel(c)}</li>
        ))}
      </ul>
    </details>
  );
}
