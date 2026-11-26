// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* The "Provisioned" summary, as markdown. Pure. ONE formatter for both
 * onboarding flows (the wizard's message and the chat flow's provision
 * checkpoint) so the two can never say different things.
 *
 * It leads with what got connected (fields mapped, join paths by what proved
 * them, value mappings, the agent's runtime and gateway target), then the
 * identity rules, and when none were written it says why. */
import type {
  IdentityRule,
  ProvisionRuleReason,
  ProvisionStatusResponse,
} from '../../api/types/onboard';
import { systemName } from '../../lib/system-names';
import { joinBasisCounts } from './connection-evidence';
import type { FlowEntry } from './flow-store';

/** What the Resolve step knew, for the identity-rule reason. Each count is
 * `undefined` when unknown (the chat flow may not have the resolve result). */
export interface ProvisionSummaryContext {
  /** Candidates the Resolve evidence gates withheld (`rejected_rules`). */
  withheldRules?: number;
  /** Rules Resolve proposed. */
  proposedRules?: number;
  /** Rules confirmed at the checkpoint and sent to provision. */
  sentRules?: number;
  /** How long the wizard waited for the provision, on the clock its loader
   * line counts (from the request to the poll that saw it done). Wins over
   * the server's `provision_seconds`, which times only the agent and graph
   * writes and not the cross-system checks the job runs after them. */
  elapsedSeconds?: number;
}

const n = (v: number): string => v.toLocaleString();
const plural = (v: number, one: string, many: string): string =>
  `${n(v)} ${v === 1 ? one : many}`;

/** The chat flow's Resolve result for the identity-rule reason: the last
 * resolve checkpoint before entry `beforeId` (inside collapsed step groups
 * too). Counts it does not carry stay unknown. */
export function resolveContextBefore(
  entries: readonly FlowEntry[],
  beforeId: number,
): ProvisionSummaryContext {
  let found: Record<string, unknown> | null = null;
  const walk = (list: readonly FlowEntry[]): void => {
    for (const e of list) {
      if (e.id >= beforeId) continue;
      if (e.kind === 'collapsed') walk(e.children);
      else if (e.kind === 'chatCheckpoint' && e.step === 'resolve') found = e.result;
    }
  };
  walk(entries);
  const res = found as Record<string, unknown> | null;
  if (!res) return {};
  return {
    ...(Array.isArray(res.rejected_rules) ? { withheldRules: res.rejected_rules.length } : {}),
    ...(Array.isArray(res.rules) ? { proposedRules: res.rules.length } : {}),
  };
}

/** "3m 12s" / "45s". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** True when the counts could not be recovered (the job record was lost and
 * the catalog confirmed completion), or are missing. */
export function provisionStatsUnavailable(r: ProvisionStatusResponse): boolean {
  return (
    r.stats_unavailable === true ||
    r.neptune_nodes_created == null ||
    r.identity_rules_created == null
  );
}

function identityRuleLine(r: ProvisionStatusResponse, ctx: ProvisionSummaryContext): string {
  const written = Number(r.identity_rules_created) || 0;
  if (written > 0) return `**Identity rules:** ${plural(written, 'rule', 'rules')} written.`;

  // No rule was written. More than one cause can apply at once (Resolve
  // withheld some candidates AND the steward removed another at the
  // checkpoint, or provision rejected what it was sent AND Resolve withheld
  // others). State EVERY cause that applies instead of the first by
  // precedence, so the summary is not false by omission.
  const reasons: string[] = [];
  const rejected = r.identity_rules_rejected ?? 0;
  if (rejected > 0) {
    reasons.push(`provision rejected ${plural(rejected, 'rule', 'rules')} it was sent`);
  }
  const withheld = ctx.withheldRules ?? 0;
  if (withheld > 0) {
    reasons.push(
      `the Resolve step held back ${plural(withheld, 'candidate', 'candidates')} ` +
        'that did not meet the evidence bar',
    );
  }
  if (ctx.proposedRules != null && ctx.sentRules != null) {
    const removed = Math.max(0, ctx.proposedRules - ctx.sentRules);
    if (removed > 0) reasons.push(`${plural(removed, 'rule', 'rules')} removed at the Resolve checkpoint`);
  }

  if (reasons.length === 0) {
    // Nothing above applies: say the honest reason only when the Resolve
    // result is known, else make no claim.
    if (ctx.proposedRules === 0) {
      reasons.push(
        'the Resolve step found no identifier values shared with another connected system',
      );
    } else {
      reasons.push('no identity rule reached provision from the Resolve step');
    }
  }

  const sentences = reasons.map((reason) => `${reason[0].toUpperCase()}${reason.slice(1)}.`);
  return `**Identity rules:** none written. ${sentences.join(' ')}`;
}

/** Text on one line: every run of line breaks and other control characters
 * (C0, DEL, C1, the Unicode line and paragraph separators) is one space, so
 * model-written text or a live value can never start a new list item. */
// eslint-disable-next-line no-control-regex
const oneLine = (s: string): string => s.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, ' ');
/** Markdown text on one line with inline markup escaped: a reason quotes
 * model-written patterns and live column values. */
const mdText = (s: string): string =>
  oneLine(s).replace(/[\\`*_[\]<>#|~]/g, (c) => `\\${c}`);
/** An inline code span on one line the text cannot close early. */
const mdCode = (s: string): string => `\`${oneLine(s).replace(/`/g, "'")}\``;

/** "`table.col` → System `table.col`" for a rejected rule, from what it names. */
function rejectedRuleLabel(rule: IdentityRule | undefined): string {
  const source = String(rule?.source_field ?? '').trim();
  const target = String(rule?.target_field ?? '').trim();
  const system = systemName(String(rule?.target_system ?? ''));
  const right = [system ? mdText(system) : '', target ? mdCode(target) : ''].filter(Boolean);
  if (!source && !right.length) return 'A rule';
  return `${source ? mdCode(source) : 'A rule'}${right.length ? ` → ${right.join(' ')}` : ''}`;
}

/** `head`, then one line per rule with the reason the provision gave
 * (`rejected_details` / `unchecked_details`). A result without the details
 * (an older server, a recovered job record) says only how many. */
function ruleReasonsMd(head: string, details: unknown): string {
  const rows = (Array.isArray(details) ? details : []).filter(
    (d): d is ProvisionRuleReason => !!d && typeof d === 'object',
  );
  if (!rows.length) return `${head}. The result did not include the reasons.`;
  const lines = rows.map((d) => {
    const reason = String(d.reason ?? '').trim();
    return `- ${rejectedRuleLabel(d.rule)}: ${reason ? mdText(reason) : 'no reason given'}`;
  });
  return `${head}:\n${lines.join('\n')}`;
}

export function provisionSummaryMd(
  r: ProvisionStatusResponse,
  ctx: ProvisionSummaryContext = {},
): string {
  const tool = `Tool \`${r.tool_name || ''}\` ready.`;
  if (provisionStatsUnavailable(r)) {
    return `✓ **Provisioned.** Completion confirmed, but statistics could not be recovered. ${tool}`;
  }
  const seconds =
    typeof ctx.elapsedSeconds === 'number' ? ctx.elapsedSeconds : r.provision_seconds;
  const took = typeof seconds === 'number' ? ` Took ${formatDuration(seconds)}.` : '';
  const connected: string[] = [];
  if (typeof r.mapped_fields === 'number') {
    connected.push(`${plural(r.mapped_fields, 'field', 'fields')} mapped to concepts`);
  }
  const joinPaths = Array.isArray(r.join_paths) ? r.join_paths : null;
  if (joinPaths) {
    connected.push(
      joinPaths.length === 0
        ? 'No join paths yet'
        : `${plural(joinPaths.length, 'join path', 'join paths')}: ${joinBasisCounts(joinPaths).join(', ')}`,
    );
  }
  const valueMappings = Array.isArray(r.value_mappings)
    ? r.value_mappings.length
    : typeof r.value_mappings_written === 'number'
      ? r.value_mappings_written
      : null;
  if (valueMappings != null) {
    connected.push(plural(valueMappings, 'value mapping', 'value mappings'));
  }
  if (r.runtime_name) {
    connected.push(
      `Agent runtime \`${r.runtime_name}\`` +
        (r.gateway_target ? `, gateway target \`${r.gateway_target}\`` : ''),
    );
  }
  const parts = [`✓ **Provisioned.** ${tool}${took}`];
  if (connected.length) parts.push('**Connected:**\n' + connected.map((c) => `- ${c}`).join('\n'));
  parts.push(identityRuleLine(r, ctx));
  const rejected = r.identity_rules_rejected ?? 0;
  if (rejected > 0) {
    parts.push(
      ruleReasonsMd(`⚠ ${plural(rejected, 'rule', 'rules')} rejected by provision`, r.rejected_details),
    );
  }
  // Written, but the provision could not re-check them against live data.
  const unchecked = Number(r.identity_rules_unchecked) || 0;
  if (unchecked > 0) {
    parts.push(
      ruleReasonsMd(
        `${plural(unchecked, 'rule', 'rules')} written without the live re-check`,
        r.unchecked_details,
      ),
    );
  }
  return parts.join('\n\n');
}
