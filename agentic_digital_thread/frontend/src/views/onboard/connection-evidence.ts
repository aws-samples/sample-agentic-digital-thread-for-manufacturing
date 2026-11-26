// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* What proved each connection onboarding wrote, in plain words. Pure.
 *
 * A join path carries `basis` (agents/connection_evidence.py) plus the numbers
 * behind it. `method` is what the agents use the join for and is never
 * relabelled: the label comes from `basis` when present. A join path written
 * before evidence was recorded has no `basis`; it is labelled from `method`
 * alone, and an old `foreign_key_match` gets the neutral "key match" (it may
 * have been a declared key, the column-name guess or an identity rule), never
 * "declared foreign key". */
import type { JoinPath } from '../../api/types/onboard';

export interface BasisInfo {
  /** CSS/color key: the basis, or `method:<method>` for an older join path. */
  key: string;
  /** Short badge label. */
  label: string;
  /** Plural count phrase for summaries: "3 declared foreign keys". */
  count: (n: number) => string;
}

const plural = (n: number, one: string, many: string): string =>
  `${n.toLocaleString()} ${n === 1 ? one : many}`;

const BASES: Record<string, Omit<BasisInfo, 'key'>> = {
  declared_foreign_key: {
    label: 'declared foreign key',
    count: (n) => plural(n, 'declared foreign key', 'declared foreign keys'),
  },
  column_name_pattern: {
    label: 'column name match',
    count: (n) => `${n.toLocaleString()} matched by column name only`,
  },
  measured_reference: {
    label: 'checked key',
    count: (n) => plural(n, 'key checked against the data', 'keys checked against the data'),
  },
  value_overlap: {
    label: 'shared values',
    count: (n) => `${n.toLocaleString()} by shared values`,
  },
  same_row_identifiers: {
    label: 'same row',
    count: (n) => plural(n, 'same-row identifier pair', 'same-row identifier pairs'),
  },
  identity_rule: {
    label: 'identity rule',
    count: (n) => `${n.toLocaleString()} from identity rules`,
  },
};

/** Labels for a join path that has no `basis`, by its `method`. */
const OLDER_METHODS: Record<string, Omit<BasisInfo, 'key'>> = {
  foreign_key_match: {
    label: 'key match',
    count: (n) => plural(n, 'key match', 'key matches'),
  },
  value_overlap: BASES.value_overlap,
  same_row_identifier_set: BASES.same_row_identifiers,
  value_exact_match: {
    label: 'exact values',
    count: (n) => `${n.toLocaleString()} by exact values`,
  },
  value_format_match: {
    label: 'format match',
    count: (n) => plural(n, 'format match', 'format matches'),
  },
  user_confirmed: {
    label: 'steward confirmed',
    count: (n) => `${n.toLocaleString()} confirmed by a steward`,
  },
  crosswalk_confirmed: {
    label: 'crosswalk',
    count: (n) => `${n.toLocaleString()} from a confirmed crosswalk`,
  },
};

export function joinBasis(jp: JoinPath): BasisInfo {
  const basis = typeof jp.basis === 'string' ? jp.basis : '';
  if (basis && BASES[basis]) return { key: basis, ...BASES[basis] };
  const method = jp.method || '';
  const older = OLDER_METHODS[method];
  if (older) return { key: `method:${method}`, ...older };
  const label = method ? method.replace(/_/g, ' ') : 'unknown';
  return { key: `method:${method || 'unknown'}`, label, count: (n) => `${n.toLocaleString()} ${label}` };
}

function pct(ratio: number | undefined): string | null {
  if (typeof ratio !== 'number' || !Number.isFinite(ratio)) return null;
  return `${Math.round((ratio <= 1 ? ratio * 100 : ratio))}%`;
}

function sharedValues(jp: JoinPath): string | null {
  if (typeof jp.overlap_count !== 'number' || jp.overlap_count <= 0) return null;
  const share = pct(jp.overlap_ratio);
  const count = plural(jp.overlap_count, 'shared value', 'shared values');
  return share ? `${count}, ${share} of the smaller column (sampled)` : count;
}

/** "pattern:A->B" (the identity-rule transform) as "A → B", else null. */
function formatChange(transform: string | undefined): string | null {
  if (!transform || !transform.startsWith('pattern:')) return null;
  const [from, to] = transform.slice('pattern:'.length).split('->');
  return from && to ? `${from} → ${to}` : null;
}

/** Why onboarding left a column-name join a guess (`reference_check`). */
const GUESS_CHECKS: Record<string, string> = {
  values_missing:
    'Checked against the data: some values in this column are not in the target column.',
  target_not_unique:
    'Checked against the data: the target column does not hold a different value on each row.',
  too_few_values: 'Checked against the data: too few values to tell.',
  too_large: 'Not checked against the data: a table was too large to read in full.',
  timeout: 'Not checked against the data: the read ran out of time.',
  failed: 'Not checked against the data: the read failed.',
  budget: "Not checked against the data: onboarding's time for these checks ran out.",
  cap: 'Not checked against the data: onboarding checks only a limited number of these joins.',
  ambiguous_target:
    'Checked against the data: another table the column name could mean holds the same values, so the target is not certain.',
  type_mismatch:
    "Checked against the data: the two columns' types cannot be compared as they are.",
};

/** The UTC day a measured join was read (`measured_at`), or null. */
function measuredDay(measuredAt: string | undefined): string | null {
  const day = /^(\d{4}-\d{2}-\d{2})T/.exec(measuredAt ?? '');
  return day ? day[1] : null;
}

/** The evidence for one join path, one plain sentence per line. */
export function joinEvidenceLines(jp: JoinPath): string[] {
  const lines: string[] = [];
  const shared = sharedValues(jp);
  switch (jp.basis) {
    case 'declared_foreign_key':
      lines.push(
        jp.constraint_name
          ? `Declared foreign key ${jp.constraint_name}.`
          : 'Declared foreign key in the database.',
      );
      return lines;
    case 'column_name_pattern': {
      const check = jp.reference_check ? GUESS_CHECKS[jp.reference_check] : undefined;
      if (check) lines.push('Guessed from the column names.', check);
      else lines.push('Guessed from the column names. The data was not checked.');
      return lines;
    }
    case 'measured_reference': {
      if (typeof jp.reference_count === 'number') {
        lines.push(
          `All ${plural(jp.reference_count, 'value', 'values')} in this column are in the target column.`,
        );
      }
      if (typeof jp.key_count === 'number') {
        lines.push(
          `The target column holds a different value on each of its ${plural(jp.key_count, 'row', 'rows')}.`,
        );
      }
      const day = measuredDay(jp.measured_at);
      lines.push(
        `Checked against the data at onboarding${day ? ` (${day} UTC)` : ''}. No foreign key is declared.`,
      );
      return lines;
    }
    case 'value_overlap':
      lines.push(shared ? `${shared}.` : 'Shared values between the two columns.');
      if (jp.transform && !['identity', 'direct'].includes(jp.transform)) {
        lines.push(`Values compared after the ${jp.transform.replace(/_/g, ' ')} step.`);
      }
      return lines;
    case 'same_row_identifiers':
      if (jp.unique_constraints === true) {
        lines.push('Both columns have a UNIQUE constraint on this table.');
      } else if (typeof jp.pair_count === 'number') {
        lines.push(
          `${plural(jp.pair_count, 'value pair', 'value pairs')} match one to one on the same row.`,
        );
      } else {
        lines.push('Two identifier columns of the same row.');
      }
      return lines;
    case 'identity_rule': {
      if (typeof jp.overlap_count === 'number' && jp.overlap_count > 0) {
        lines.push(`${plural(jp.overlap_count, 'value', 'values')} found in both systems.`);
      }
      const change = formatChange(jp.transform);
      if (change) lines.push(`Values match after a format change: ${change}.`);
      if (jp.rule_method === 'value_exact_match') {
        lines.push('From an identity rule that checked exact values in both systems.');
      } else if (jp.rule_method === 'value_core_match') {
        lines.push('From an identity rule: sampled values share an identifier core.');
      } else {
        lines.push('From an identity rule confirmed at the Resolve step.');
      }
      return lines;
    }
    default:
      break;
  }
  // Older join path: no basis recorded. Say so, and show what it does carry.
  if (jp.method === 'user_confirmed') lines.push('Confirmed by a steward.');
  if (jp.method === 'crosswalk_confirmed') lines.push('From a crosswalk a steward confirmed.');
  if (shared) lines.push(`${shared}.`);
  lines.push('Recorded before the evidence for each connection was kept.');
  return lines;
}

/** Join paths counted by basis, in first-seen order: [["3 declared foreign
 * keys"], ...] as phrases. */
export function joinBasisCounts(joinPaths: readonly JoinPath[]): string[] {
  const order: string[] = [];
  const counts = new Map<string, { info: BasisInfo; n: number }>();
  for (const jp of joinPaths) {
    const info = joinBasis(jp);
    const hit = counts.get(info.key);
    if (hit) hit.n += 1;
    else {
      counts.set(info.key, { info, n: 1 });
      order.push(info.key);
    }
  }
  return order.map((k) => {
    const { info, n } = counts.get(k)!;
    return info.count(n);
  });
}

const URL_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;
const HOST_LABEL = /^[a-z0-9-]+$/;
const HOST_PORT_LABEL = /^[a-z0-9-]+:\d+$/;
const TOP_LEVEL_LABEL = /^[a-z]{2,63}$/;

/** A join path's table as the tables show it. A REST-family system (Tulip,
 * REST API, MCP) discovers under its base URL, so onboarding stores its
 * table as `<base url>.<table>`; this drops the URL part. Display only: the
 * stored value is not changed. Anything that doesn't start with a URL scheme
 * (a PostgreSQL `schema.table`) is returned unchanged.
 *
 * Both the URL and the table may hold dots, so the split is made only where
 * the shape leaves one answer; otherwise the stored value is shown as is
 * (never a wrong table name):
 * - a path holding exactly one `.`: the table follows it
 *   (`…/prod.Production Orders`);
 * - a `host:port` with no path: the table follows the port;
 * - no path, no port: the table is the one last part that can't be a
 *   lowercase host label, right after an all-letter top-level label
 *   (`…tulip.example.com.Production Orders`).
 * shortcut: a dotted path (`/openapi.json`), a dotted table name after a
 * no-port host, or an all-lowercase one-word table after a no-path host is
 * shown unchanged; a backend display field would settle these if a vendor
 * ever needs it. */
export function displayTable(table?: string): string {
  const stored = table || '';
  const scheme = URL_SCHEME.exec(stored);
  if (!scheme) return stored;
  const rest = stored.slice(scheme[0].length);
  const slash = rest.indexOf('/');
  if (slash >= 0) {
    const path = rest.slice(slash);
    const dots = path.split('.').length - 1;
    return dots === 1 ? path.slice(path.indexOf('.') + 1) || stored : stored;
  }
  const parts = rest.split('.');
  const first = parts.findIndex((p) => !HOST_LABEL.test(p));
  if (first < 0) return stored;
  if (HOST_PORT_LABEL.test(parts[first])) {
    return parts.slice(first + 1).join('.') || stored;
  }
  const lastPart = first === parts.length - 1;
  return first > 0 && lastPart && TOP_LEVEL_LABEL.test(parts[first - 1])
    ? parts[first]
    : stored;
}
