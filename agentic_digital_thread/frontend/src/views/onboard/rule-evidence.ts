// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* What was measured for one proposed identity rule, in plain words. Pure.
 *
 * The resolve step measures value overlap for each candidate field pair
 * (`evidence`) and the evidence gate (agents/resolve_gates.py) reads the row
 * for a rule's pair: the exact table-qualified pair first; else, because the
 * model sometimes drops the table (or adds the system id), the rows whose
 * target system and concept (or declared related concept) match and whose
 * ends are the rule's ends with the left-out qualifiers set aside. Exactly one
 * such row is the pair; more than one is ambiguous, and the gate withholds
 * the rule rather than guess.
 * `evidenceMatch` pairs them the same way, so the row shows the numbers the
 * gate judged, and an ambiguous row shows no evidence line at all. A rule the
 * gate kept with no evidence row was kept on its translation check (its
 * pattern's values were found in the target) or, when a side could not be
 * sampled, because both field names look like identifiers; an
 * identifier-core affinity rule carries its own measurement. */
import type { IdentityRule, ResolveEvidence, TransformCheck } from '../../api/types/onboard';

/** Examples shown on a row; the full list is in the Value Overlap Evidence table. */
const SHOWN_EXAMPLES = 3;

const norm = (v: unknown): string => String(v ?? '').trim().toLowerCase();

/** A rule end names a measured end: the same spelling once the leading
 * qualifiers one side leaves out are set aside (the model drops the table, or
 * adds the system id). `tasks.code` never names `jobs.code`. */
function endsMatch(ruleEnd: unknown, evidenceEnd: unknown): boolean {
  const a = norm(ruleEnd);
  const b = norm(evidenceEnd);
  if (a === '' || b === '') return false;
  return a === b || a.endsWith(`.${b}`) || b.endsWith(`.${a}`);
}

function sameConcept(rule: IdentityRule, row: ResolveEvidence): boolean {
  const concept = norm(rule.concept);
  return concept !== '' && (concept === norm(row.concept) || concept === norm(row.related_concept));
}

export interface EvidenceMatch {
  /** The measured row for the rule's pair, or null. */
  row: ResolveEvidence | null;
  /** More than one row could be it: the rule dropped a table they differ by. */
  ambiguous: boolean;
}

/** The evidence row for this rule's field pair, paired as the gate pairs it. */
export function evidenceMatch(
  rule: IdentityRule,
  evidence: readonly ResolveEvidence[] | null | undefined,
): EvidenceMatch {
  const system = norm(rule.target_system);
  const rows = (evidence || []).filter((e) => norm(e.target_system) === system);
  const exact = rows.find(
    (e) =>
      norm(e.source_field) === norm(rule.source_field) &&
      norm(e.target_field) === norm(rule.target_field),
  );
  if (exact) return { row: exact, ambiguous: false };
  const loose = rows.filter(
    (e) =>
      sameConcept(rule, e) &&
      endsMatch(rule.source_field, e.source_field) &&
      endsMatch(rule.target_field, e.target_field),
  );
  if (loose.length === 1) return { row: loose[0]!, ambiguous: false };
  return { row: null, ambiguous: loose.length > 1 };
}

/** The evidence row for this rule's field pair, or null when it was never
 * measured or the match is ambiguous. */
export function evidenceForRule(
  rule: IdentityRule,
  evidence: readonly ResolveEvidence[] | null | undefined,
): ResolveEvidence | null {
  return evidenceMatch(rule, evidence).row;
}

const sharedValues = (n: number): string =>
  `${n.toLocaleString()} shared value${n === 1 ? '' : 's'}`;

/** For a rule that reads values unchanged (the same pattern text on both
 * sides, resolve_gates.reads_values_unchanged), the gate counts only the
 * shared values that are the same whole value in both fields
 * (`unchanged_count`); the shared count also holds values that match only
 * once padding is stripped or inside a JSON value. When fewer are whole
 * values, say how many, so the row never claims more support than the gate
 * judged. Empty otherwise, and for a server that sends no such count. */
function wholeValuesNote(rule: IdentityRule, ev: ResolveEvidence, shared: number): string {
  if (trimmed(rule.source_pattern) !== trimmed(rule.target_pattern)) return '';
  if (typeof ev.unchanged_count !== 'number') return '';
  const whole = ev.unchanged_count;
  return whole < shared ? `, ${whole.toLocaleString()} the same whole value in both fields` : '';
}

/** The note under a rule whose pattern the Resolve gate set from the
 * measured values rather than taking the model's (`pattern_origin`), stored
 * with no pattern (an exact-agreement rule). */
export const PATTERN_ORIGIN_NOTE =
  'pattern set from the measured values, not the model: the shared values match unchanged';

/** The note under a rule whose pattern the Resolve gate completed: the
 * model's pair had no literal text on one side, and the literal text its
 * measured values share there was added to both patterns. */
export const COMPLETED_PATTERN_NOTE =
  'pattern completed from the measured values: literal text they all share was added to both sides';

/** The note for the rule's row, or null when the model's pattern stands. */
export function patternOriginNote(rule: IdentityRule): string | null {
  if (!String(rule.pattern_origin ?? '').trim()) return null;
  const stored = [rule.source_pattern, rule.target_pattern].map((p) => String(p ?? '').trim());
  return stored.every((p) => p === '') ? PATTERN_ORIGIN_NOTE : COMPLETED_PATTERN_NOTE;
}

const trimmed = (v: unknown): string => String(v ?? '').trim();

/** The translation check the server made for this rule's pattern pair on its
 * exact field pair (the pair the check names, the rule's two patterns), or
 * null. The server matched the rule to that pair; a check for the same pair
 * with other patterns is another rule's. */
export function transformCheckForRule(
  rule: IdentityRule,
  checks: readonly TransformCheck[] | null | undefined,
): TransformCheck | null {
  const found = (checks || []).find(
    (c) =>
      norm(c.target_system) === norm(rule.target_system) &&
      endsMatch(rule.source_field, c.source_field) &&
      endsMatch(rule.target_field, c.target_field) &&
      trimmed(c.source_pattern) === trimmed(rule.source_pattern) &&
      trimmed(c.target_pattern) === trimmed(rule.target_pattern),
  );
  return found && (Number(found.overlap_count) || 0) > 0 ? found : null;
}

/** One line for the rule's row: what was measured for its field pair; null
 * (no line) when the evidence is ambiguous. A rule whose translation the
 * server checked (`checks`, resolve_gates.TRANSFORM_CHECKS_KEY) says how many
 * of the values its pattern gave were found in the target, whether or not
 * the pair's tokens also overlap: the gate judged it on that. Examples are
 * the server's display-safe `shown_examples`, shown as they are; the full
 * `examples` are never rendered. */
export function ruleEvidenceText(
  rule: IdentityRule,
  evidence: readonly ResolveEvidence[] | null | undefined,
  checks: readonly TransformCheck[] | null | undefined = [],
): string | null {
  const { row: ev, ambiguous } = evidenceMatch(rule, evidence);
  if (ambiguous) return null;
  const check = transformCheckForRule(rule, checks);
  if (check) {
    const n = Number(check.overlap_count) || 0;
    const examples = (check.shown_examples || []).slice(0, SHOWN_EXAMPLES).map(String);
    const found = `${n.toLocaleString()} translated value${n === 1 ? '' : 's'} found in the target`;
    return examples.length > 0 ? `${found}, e.g. ${examples.join(', ')}` : found;
  }
  if (ev) {
    const examples = (ev.shown_examples || []).slice(0, SHOWN_EXAMPLES).map(String);
    const shared = Number(ev.overlap_count) || 0;
    const count = sharedValues(shared) + wholeValuesNote(rule, ev, shared);
    return examples.length > 0 ? `${count}, e.g. ${examples.join(', ')}` : count;
  }
  if (rule.method === 'value_exact_match') {
    const n = Number(rule.shared_count);
    return Number.isFinite(n) && n > 0
      ? `${sharedValues(n)}, checked exactly in both systems`
      : 'values checked exactly in both systems';
  }
  if (rule.method === 'value_core_match') return 'sampled values share an identifier core';
  return 'not measured: both field names look like identifiers';
}
