// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The Provisioned summary (one formatter for the wizard and the chat flow)
 * and the per-connection evidence wording. Pure. */
import { describe, expect, it } from 'vitest';

import type { JoinPath, ProvisionStatusResponse } from '../api/types/onboard';
import {
  joinBasis,
  joinBasisCounts,
  joinEvidenceLines,
} from '../views/onboard/connection-evidence';
import type { FlowEntry } from '../views/onboard/flow-store';
import {
  formatDuration,
  provisionSummaryMd,
  resolveContextBefore,
} from '../views/onboard/provision-summary';

const jp = (over: Partial<JoinPath>): JoinPath => ({
  from_system: 'sys-a',
  from_table: 'schema_a.table_x',
  from_field: 'key_b_id',
  to_system: 'sys-a',
  to_table: 'schema_a.table_b',
  to_field: 'key_b_id',
  ...over,
});

const done = (over: Partial<ProvisionStatusResponse> = {}): ProvisionStatusResponse => ({
  status: 'done',
  system_id: 'sys-a',
  tool_name: 'query_sys_a',
  neptune_nodes_created: 120,
  identity_rules_created: 0,
  identity_rules_rejected: 0,
  ...over,
});

describe('join path basis labels', () => {
  it('labels each recorded basis', () => {
    expect(joinBasis(jp({ basis: 'declared_foreign_key', method: 'foreign_key_match' })).label).toBe(
      'declared foreign key',
    );
    expect(joinBasis(jp({ basis: 'value_overlap', method: 'value_overlap' })).label).toBe(
      'shared values',
    );
    expect(
      joinBasis(jp({ basis: 'same_row_identifiers', method: 'same_row_identifier_set' })).label,
    ).toBe('same row');
    expect(joinBasis(jp({ basis: 'identity_rule', method: 'foreign_key_match' })).label).toBe(
      'identity rule',
    );
  });

  it('labels an identity rule join the same under the method that names the rule', () => {
    const rule = jp({ to_system: 'sys-b', basis: 'identity_rule', method: 'identity_rule_match' });
    expect(joinBasis(rule)).toMatchObject({ key: 'identity_rule', label: 'identity rule' });
    expect(joinEvidenceLines(rule)).toEqual([
      'From an identity rule confirmed at the Resolve step.',
    ]);
  });

  it('gives the naming guess its own label, not a key label', () => {
    const b = joinBasis(jp({ basis: 'column_name_pattern', method: 'foreign_key_match' }));
    expect(b.label).toBe('column name match');
    expect(joinEvidenceLines(jp({ basis: 'column_name_pattern' }))).toEqual([
      'Guessed from the column names. The data was not checked.',
    ]);
  });

  it('never calls an older foreign_key_match join declared', () => {
    const old = jp({ method: 'foreign_key_match' });
    expect(joinBasis(old).label).toBe('key match');
    expect(joinBasis(old).label).not.toMatch(/declared/);
    const lines = joinEvidenceLines(old);
    expect(lines.join(' ')).not.toMatch(/declared/i);
    expect(lines).toContain('Recorded before the evidence for each connection was kept.');
  });

  it('keeps the numbers an older value-overlap join carries', () => {
    const lines = joinEvidenceLines(
      jp({ method: 'value_overlap', overlap_count: 12, overlap_ratio: 0.5 }),
    );
    expect(lines[0]).toBe('12 shared values, 50% of the smaller column (sampled).');
  });
});

describe('join path evidence in plain words', () => {
  it('names a declared constraint', () => {
    expect(
      joinEvidenceLines(jp({ basis: 'declared_foreign_key', constraint_name: 'fk_x' })),
    ).toEqual(['Declared foreign key fk_x.']);
  });

  it('gives the shared-value count and share of the smaller column', () => {
    expect(
      joinEvidenceLines(
        jp({ basis: 'value_overlap', overlap_count: 1240, overlap_ratio: 0.98, transform: 'identity' }),
      ),
    ).toEqual([`${(1240).toLocaleString()} shared values, 98% of the smaller column (sampled).`]);
  });

  it('counts the value pairs of a same-row pair, or names its UNIQUE constraints', () => {
    expect(joinEvidenceLines(jp({ basis: 'same_row_identifiers', pair_count: 412 }))).toEqual([
      '412 value pairs match one to one on the same row.',
    ]);
    expect(
      joinEvidenceLines(jp({ basis: 'same_row_identifiers', unique_constraints: true })),
    ).toEqual(['Both columns have a UNIQUE constraint on this table.']);
  });

  it('explains an identity-rule join by its rule', () => {
    expect(
      joinEvidenceLines(
        jp({ basis: 'identity_rule', rule_method: 'value_exact_match', overlap_count: 57 }),
      ),
    ).toEqual([
      '57 values found in both systems.',
      'From an identity rule that checked exact values in both systems.',
    ]);
    expect(
      joinEvidenceLines(jp({ basis: 'identity_rule', transform: 'pattern:A-{n}->{n}' })),
    ).toEqual([
      'Values match after a format change: A-{n} → {n}.',
      'From an identity rule confirmed at the Resolve step.',
    ]);
  });

  it('counts join paths by basis in first-seen order', () => {
    expect(
      joinBasisCounts([
        jp({ basis: 'declared_foreign_key' }),
        jp({ basis: 'column_name_pattern' }),
        jp({ basis: 'declared_foreign_key' }),
        jp({ method: 'user_confirmed' }),
      ]),
    ).toEqual(['2 declared foreign keys', '1 matched by column name only', '1 confirmed by a steward']);
  });
});

describe('a column-name join onboarding checked against the data', () => {
  it('labels a measured join and gives the numbers behind it', () => {
    const measured = jp({
      basis: 'measured_reference',
      method: 'foreign_key_match',
      reference_count: 412,
      key_count: 96,
    });
    expect(joinBasis(measured).label).toBe('checked key');
    expect(joinBasis(measured).label).not.toMatch(/declared/);
    expect(joinEvidenceLines(measured)).toEqual([
      'All 412 values in this column are in the target column.',
      'The target column holds a different value on each of its 96 rows.',
      'Checked against the data at onboarding. No foreign key is declared.',
    ]);
  });

  it('says why a column-name join stayed a guess', () => {
    const lines = (reference_check: string) =>
      joinEvidenceLines(jp({ basis: 'column_name_pattern', reference_check }));
    expect(lines('values_missing')).toEqual([
      'Guessed from the column names.',
      'Checked against the data: some values in this column are not in the target column.',
    ]);
    expect(lines('target_not_unique')[1]).toBe(
      'Checked against the data: the target column does not hold a different value on each row.',
    );
    expect(lines('too_few_values')[1]).toBe('Checked against the data: too few values to tell.');
    expect(lines('too_large')[1]).toBe(
      'Not checked against the data: a table was too large to read in full.',
    );
    expect(lines('timeout')[1]).toBe('Not checked against the data: the read ran out of time.');
    expect(lines('failed')[1]).toBe('Not checked against the data: the read failed.');
    expect(lines('budget')[1]).toBe(
      "Not checked against the data: onboarding's time for these checks ran out.",
    );
    expect(lines('cap')[1]).toBe(
      'Not checked against the data: onboarding checks only a limited number of these joins.',
    );
    expect(lines('ambiguous_target')[1]).toBe(
      'Checked against the data: another table the column name could mean holds the same values, so the target is not certain.',
    );
    expect(lines('type_mismatch')[1]).toBe(
      "Checked against the data: the two columns' types cannot be compared as they are.",
    );
  });

  it('says on which day a measured join was read', () => {
    const measured = jp({
      basis: 'measured_reference',
      method: 'foreign_key_match',
      reference_count: 3,
      key_count: 3,
      measured_at: '2026-10-17T22:58:04+00:00',
    });
    expect(joinEvidenceLines(measured)[2]).toBe(
      'Checked against the data at onboarding (2026-10-17 UTC). No foreign key is declared.',
    );
  });

  it('counts measured joins apart from the guesses', () => {
    expect(
      joinBasisCounts([
        jp({ basis: 'measured_reference' }),
        jp({ basis: 'column_name_pattern', reference_check: 'values_missing' }),
        jp({ basis: 'measured_reference' }),
      ]),
    ).toEqual(['2 keys checked against the data', '1 matched by column name only']);
  });
});

describe('provisionSummaryMd', () => {
  it('leads with what got connected', () => {
    const md = provisionSummaryMd(
      done({
        mapped_fields: 42,
        provision_seconds: 192,
        runtime_name: 'adt_sys_a',
        gateway_target: 'sys-a',
        join_paths: [
          jp({ basis: 'declared_foreign_key' }),
          jp({ basis: 'declared_foreign_key' }),
          jp({ basis: 'value_overlap' }),
        ],
        value_mappings: [{ from_value: 'a', to_value: 'b' }],
        identity_rules_created: 2,
      }),
    );
    expect(md).toBe(
      [
        '✓ **Provisioned.** Tool `query_sys_a` ready. Took 3m 12s.',
        '**Connected:**\n' +
          '- 42 fields mapped to concepts\n' +
          '- 3 join paths: 2 declared foreign keys, 1 by shared values\n' +
          '- 1 value mapping\n' +
          '- Agent runtime `adt_sys_a`, gateway target `sys-a`',
        '**Identity rules:** 2 rules written.',
      ].join('\n\n'),
    );
    expect(md).not.toMatch(/graph nodes/);
  });

  it("takes the wizard's own elapsed time over the server's shorter span", () => {
    // provision_seconds times the agent and graph writes only; the job runs
    // its cross-system checks after them, and the wizard's counter covers both.
    const md = provisionSummaryMd(done({ provision_seconds: 200 }), { elapsedSeconds: 271 });
    expect(md).toContain('Took 4m 31s.');
    expect(md).not.toContain('3m 20s');
    expect(provisionSummaryMd(done({ provision_seconds: 200 }))).toContain('Took 3m 20s.');
    expect(provisionSummaryMd(done(), { elapsedSeconds: 45 })).toContain('Took 45s.');
  });

  it('says why no identity rule was written: the evidence gates held candidates back', () => {
    const md = provisionSummaryMd(done({ join_paths: [] }), {
      withheldRules: 2,
      proposedRules: 0,
      sentRules: 0,
    });
    expect(md).toContain(
      '**Identity rules:** none written. The Resolve step held back 2 candidates that did ' +
        'not meet the evidence bar.',
    );
    expect(md).toContain('- No join paths yet');
  });

  it('says why: the proposed rules were removed at the checkpoint', () => {
    expect(
      provisionSummaryMd(done(), { withheldRules: 0, proposedRules: 3, sentRules: 0 }),
    ).toContain('3 rules removed at the Resolve checkpoint.');
  });

  it('composes a mixed withheld-plus-removed reason', () => {
    // Resolve held back 2 weak candidates and proposed 1 that the steward
    // removed at the checkpoint. Both are why no rule was written; the summary
    // must say both, not the first by precedence.
    const md = provisionSummaryMd(done(), {
      withheldRules: 2,
      proposedRules: 1,
      sentRules: 0,
    });
    expect(md).toContain('The Resolve step held back 2 candidates that did not meet the evidence bar');
    expect(md).toContain('1 rule removed at the Resolve checkpoint');
  });

  it('composes a mixed provision-rejected-plus-withheld reason', () => {
    const md = provisionSummaryMd(done({ identity_rules_rejected: 1 }), {
      withheldRules: 4,
      proposedRules: 1,
      sentRules: 1,
    });
    expect(md).toContain(
      '**Identity rules:** none written. Provision rejected 1 rule it was sent. ' +
        'The Resolve step held back 4 candidates that did not meet the evidence bar.',
    );
    expect(md).not.toContain('; ');
  });

  it('says why: Resolve found nothing shared', () => {
    expect(provisionSummaryMd(done(), { withheldRules: 0, proposedRules: 0 })).toContain(
      'The Resolve step found no identifier values shared with another connected system.',
    );
  });

  it('claims no Resolve reason when the Resolve result is unknown', () => {
    const md = provisionSummaryMd(done());
    expect(md).toContain('No identity rule reached provision from the Resolve step.');
    expect(md).not.toMatch(/held back|found no identifier/);
  });

  it('keeps the provision-rejected warning and still names the withheld candidates', () => {
    const md = provisionSummaryMd(done({ identity_rules_rejected: 2 }), { withheldRules: 4 });
    expect(md).toContain('Provision rejected 2 rules it was sent. The Resolve step held back 4 candidates');
    expect(md).toContain('⚠ 2 rules rejected by provision. The result did not include the reasons.');
  });

  it('shows the reason provision gave for each rejected rule', () => {
    const md = provisionSummaryMd(
      done({
        identity_rules_rejected: 2,
        rejected_details: [
          {
            rule: {
              concept: 'operator',
              source_field: 'work_log.signed_by',
              target_system: 'line-sys',
              target_field: 'stops.closed_by',
            },
            reason: "pattern 'WO-{n}' doesn't match sample value 'A. Lee' (no value of work_log.signed_by fits it)",
          },
          {
            rule: { source_field: 'work_log.unit_ref', target_system: 'gone-sys', target_field: 'units.ref' },
            reason: "target_system 'gone-sys' is not an onboarded system",
          },
        ],
      }),
    );
    expect(md).toContain('⚠ 2 rules rejected by provision:');
    expect(md).toContain(
      "- `work_log.signed_by` → Line Sys `stops.closed_by`: pattern 'WO-{n}' doesn't match sample value 'A. Lee' (no value of work\\_log.signed\\_by fits it)",
    );
    expect(md).toContain(
      "- `work_log.unit_ref` → Gone Sys `units.ref`: target\\_system 'gone-sys' is not an onboarded system",
    );
    expect(md).not.toContain('pattern mismatch or missing target');
  });

  it('says "1 rule", and keeps model-written text from breaking out of its markup', () => {
    const md = provisionSummaryMd(
      done({
        identity_rules_rejected: 1,
        rejected_details: [
          { rule: { source_field: 'a`b', target_field: 'c' }, reason: 'pattern `x` *bold* [link](y)' },
        ],
      }),
    );
    expect(md).toContain('⚠ 1 rule rejected by provision:');
    expect(md).toContain("- `a'b` → `c`: pattern \\`x\\` \\*bold\\* \\[link\\](y)");
  });

  it('keeps multiline model text and live values on their own line', () => {
    const md = provisionSummaryMd(
      done({
        identity_rules_rejected: 1,
        rejected_details: [
          {
            rule: { source_field: 'log.signed\nby', target_system: 'line-sys', target_field: 'stops.\r\nclosed_by' },
            reason: "pattern 'a\nb' doesn't match sample value 'x\r\n- injected item\u2028# heading\u0007'",
          },
        ],
      }),
    );
    const block = md.slice(md.indexOf('⚠ 1 rule rejected by provision:'));
    const lines = block.split('\n');
    expect(lines).toHaveLength(2);
    expect(lines[1]).toBe(
      "- `log.signed by` → Line Sys `stops. closed_by`: pattern 'a b' doesn't match sample value 'x - injected item \\# heading '",
    );
    expect(md).not.toMatch(/[\u0000-\u0009\u000b-\u001f\u007f\u2028\u2029]/);
  });

  it('says which rules were written without the live re-check, and why', () => {
    const md = provisionSummaryMd(
      done({
        identity_rules_created: 2,
        identity_rules_unchecked: 1,
        unchecked_details: [
          {
            rule: { source_field: 'work_log.signed_by', target_system: 'hub-sys', target_field: 'stops.closed_by' },
            reason: "the target 'hub-sys' is a REST API system, so its column was not read",
          },
        ],
      }),
    );
    expect(md).toContain('**Identity rules:** 2 rules written.');
    expect(md).toContain(
      "1 rule written without the live re-check:\n- `work_log.signed_by` → Hub Sys `stops.closed_by`: the target 'hub-sys' is a REST API system, so its column was not read",
    );
    expect(provisionSummaryMd(done({ identity_rules_created: 1 }))).not.toContain('re-check');
  });

  it('keeps the statistics-could-not-be-recovered branch', () => {
    const lost = '✓ **Provisioned.** Completion confirmed, but statistics could not be recovered. Tool `query_sys_a` ready.';
    expect(provisionSummaryMd({ status: 'done', stats_unavailable: true, tool_name: 'query_sys_a' })).toBe(lost);
    expect(provisionSummaryMd({ status: 'done', tool_name: 'query_sys_a' })).toBe(lost);
  });

  it('uses no em or en dash as punctuation', () => {
    const md = provisionSummaryMd(done({ join_paths: [jp({ basis: 'identity_rule' })] }), {
      withheldRules: 1,
    });
    expect(md).not.toMatch(/[\u2013\u2014]/);
  });

  it('formats durations', () => {
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(192)).toBe('3m 12s');
  });
});

describe('resolveContextBefore (chat flow)', () => {
  const checkpoint = (id: number, step: string, result: Record<string, unknown>): FlowEntry => ({
    id,
    kind: 'chatCheckpoint',
    step,
    result,
    confirmed: true,
  });

  it('reads the last resolve checkpoint before the provision entry, in collapsed groups too', () => {
    const entries: FlowEntry[] = [
      {
        id: 1,
        kind: 'collapsed',
        label: 'Resolve',
        children: [checkpoint(2, 'resolve', { rules: [], rejected_rules: [{}, {}, {}] })],
      },
      checkpoint(3, 'provision', {}),
      checkpoint(4, 'resolve', { rules: [{}], rejected_rules: [] }),
    ];
    expect(resolveContextBefore(entries, 3)).toEqual({ withheldRules: 3, proposedRules: 0 });
  });

  it('knows nothing without a resolve checkpoint', () => {
    expect(resolveContextBefore([checkpoint(1, 'provision', {})], 1)).toEqual({});
  });
});
