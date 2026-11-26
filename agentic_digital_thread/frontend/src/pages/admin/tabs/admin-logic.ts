// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Pure admin-console logic — kept DOM-free so it is
 * unit-testable under the node vitest environment.
 *
 * NOTE on feedback / guardrails / dashboard: the retired legacy UI shipped
 * loadFeedback(), loadGuardrails() and loadDashboard() with NO panels,
 * tab buttons, tables or KPI elements for them (dead code — switchAdminTab
 * requires both button AND panel, so even a #tab-feedback hash is a no-op).
 * The row/KPI shaping is kept verbatim and unit-tested; the DOM is
 * intentionally NOT extended beyond the established markup contract. */

import type {
  AdminDashboardStats,
  FeedbackRecord,
  GuardrailViolation,
  ModelCatalogEntry,
} from '../../../api/types/admin';

import type { WorkshopGate } from '../../../features/workshop/clock';
import { escapeHtml } from '../../../lib/esc';

/** admin.js escapeHtml() — now the shared helper from src/lib/esc.ts. The only
 * semantic difference vs the admin.js verbatim version is nullish input
 * (String(null) → "null" there, '' here); every call site is nullish-guarded
 * (truncate() → '—', `?? '—'` fallbacks, `r.user_comment ?` ternary, and
 * SettingsTab's `packs.error: string`), so output is identical in practice. */
export { escapeHtml };

/** admin.js truncate() — verbatim (empty/nullish → em dash). */
export function truncate(str: string | null | undefined, n: number): string {
  if (!str) return '—';
  return str.length > n ? str.slice(0, n) + '…' : str;
}

/** The contractual admin tabs (nav.tab-bar order; tab-vocabulary appended
 * by the ADR-0050 vocabulary tab; tab-evals by the Evals admin tab;
 * tab-workshop by the self-gating Workshop tab, rendered only on a live story;
 * tab-conversations by the admin Conversations tab). */
export const ADMIN_TAB_IDS = ['tab-agents', 'tab-settings', 'tab-demoscript', 'tab-thread', 'tab-vocabulary', 'tab-context', 'tab-evals', 'tab-workshop', 'tab-conversations'] as const;
export type AdminTabId = (typeof ADMIN_TAB_IDS)[number];

/** The tab shown when no (renderable) #tab-... hash selects another. */
export const DEFAULT_ADMIN_TAB: AdminTabId = 'tab-agents';

/** Hash-driven tab restore decision (the IIFE at admin.js:56-64): a matching
 * #tab-... hash activates that tab, except tab-demoscript when demo is off
 * and tab-workshop when the workshop gate settled 'hidden' (a frozen/demo
 * deploy renders no workshop section). A 'pending' gate restores it;
 * workshopTabFallback leaves it once the gate settles hidden. */
export function restoreTabFromHash(
  hash: string,
  demoEnabled: boolean,
  workshopGate: WorkshopGate = 'hidden',
): AdminTabId | null {
  const id = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!id) return null;
  if (!(ADMIN_TAB_IDS as readonly string[]).includes(id)) return null;
  if (id === 'tab-demoscript' && !demoEnabled) return null;
  if (id === 'tab-workshop' && workshopGate === 'hidden') return null;
  return id as AdminTabId;
}

/** The tab to switch to when tab-workshop is active but its section is not
 * rendered (gate settled 'hidden': frozen story, 403/404); null otherwise. */
export function workshopTabFallback(activeTab: AdminTabId, workshopGate: WorkshopGate): AdminTabId | null {
  return activeTab === 'tab-workshop' && workshopGate === 'hidden' ? DEFAULT_ADMIN_TAB : null;
}

/** loadFeedback() URL building — 'all' means no sentiment query param. */
export function feedbackUrl(sentiment: string): string {
  return sentiment === 'all' ? '/api/admin/feedback' : `/api/admin/feedback?sentiment=${sentiment}`;
}

/** loadFeedback() tbody rows — verbatim template port (dead code in the
 * static markup; see file header note). */
export function feedbackRowsHtml(records: FeedbackRecord[]): string {
  return (
    records
      .map(
        // Security-reviewed (html-in-template-string): every interpolation is
        // escapeHtml()-wrapped (sentiment class is a computed literal); this
        // is a verbatim template port kept as dead code (see file header).
        // nosemgrep
        (r) => `
    <tr>
      <td class="mono">${r.timestamp ? new Date(r.timestamp).toLocaleString() : '—'}</td>
      <td>${escapeHtml(truncate(r.user_id, 20))}</td>
      <td><span class="badge ${r.sentiment === 'positive' ? 'pos' : 'neg'}">${escapeHtml(r.sentiment ?? '—')}</span></td>
      <td>${escapeHtml(truncate(r.user_message, 60))}</td>
      <td>${r.user_comment ? escapeHtml(truncate(r.user_comment, 50)) : '<span style="color:var(--muted)">—</span>'}</td>
    </tr>`,
      )
      .join('') ||
    '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:1.5rem">No records</td></tr>'
  );
}

/** loadGuardrails() tbody rows — verbatim template port (dead code; see note). */
export function guardrailRowsHtml(violations: GuardrailViolation[]): string {
  return (
    violations
      .map(
        // Security-reviewed (html-in-template-string): every interpolation is
        // escapeHtml()-wrapped; verbatim template port kept as dead code.
        // nosemgrep
        (r) => `
    <tr>
      <td class="mono">${r.timestamp ? new Date(r.timestamp).toLocaleString() : '—'}</td>
      <td>${escapeHtml(truncate(r.user_id, 20))}</td>
      <td class="mono">${escapeHtml(r.source ?? '—')}</td>
      <td>${escapeHtml(r.action ?? '—')}</td>
      <td class="mono">${escapeHtml(truncate(r.content_preview, 70))}</td>
    </tr>`,
      )
      .join('') ||
    '<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:1.5rem">No violations</td></tr>'
  );
}

/** loadDashboard() KPI text mapping — verbatim ('—' for missing values). */
export function dashboardKpiText(d: AdminDashboardStats): {
  requests: string;
  tokens: string;
  users: string;
  avg: string;
  period: string;
} {
  return {
    requests: d.total_requests != null ? String(d.total_requests) : '—',
    tokens: d.total_tokens != null ? String(d.total_tokens) : '—',
    users: d.unique_users != null ? String(d.unique_users) : '—',
    avg: d.avg_tokens_per_request != null ? String(Math.round(d.avg_tokens_per_request)) : '—',
    period: d.period_days != null ? `Last ${d.period_days} days` : '',
  };
}

export interface ModelOption {
  value: string;
  label: string;
  /** Set (true) only for a model outside the deployment allowlist: it is
   * shown greyed out, never hidden, and cannot be picked. */
  disabled?: boolean;
}

/** Annotation for a model the deployment allowlist (ALLOWED_MODELS) excludes. */
export const NOT_OFFERED_NOTE = ' (not offered in this deployment)';

/** "$X" per MTok from a per-1K rate ($0.015/1K → "$15"; trailing zeros trimmed). */
function dollarsPerMTok(per1k: number): string {
  return '$' + String(Number((per1k * 1000).toFixed(4)));
}

/** " · $in/$out per MTok" when the catalog entry carries pricing, else ''.
 * Both role dropdowns offer EVERY catalog model, so the Amazon Bedrock list
 * price (an estimate; SettingsTab links the pricing page) rides in the label
 * to keep heavier picks an informed choice. */
export function priceHint(m: ModelCatalogEntry): string {
  if (typeof m.input_per_1k !== 'number' || typeof m.output_per_1k !== 'number') return '';
  return ` · ${dollarsPerMTok(m.input_per_1k)}/${dollarsPerMTok(m.output_per_1k)} per MTok`;
}

/** _fillModelSelect + _selectModelValue port: options from the catalog (models
 * not enabled in the region are annotated but still selectable; entries with
 * pricing get a " · $in/$out per MTok" list-price hint), plus the
 * currently-configured value appended as "<id> (current)" when the catalog
 * did not include it — so a previously-saved/custom model still displays.
 *
 * Under a deployment allowlist (`allowed: false` entries, `allowlistActive`)
 * the excluded models stay listed but disabled with NOT_OFFERED_NOTE, and a
 * configured value outside the allowlist (in the catalog or not) is still the
 * shown value, disabled, so the page reports what is actually configured. */
export function buildModelOptions(
  items: ModelCatalogEntry[],
  currentValue: string,
  allowlistActive = false,
): ModelOption[] {
  const options: ModelOption[] = items.map((m) =>
    m.allowed === false
      ? { value: m.id, label: m.label + priceHint(m) + NOT_OFFERED_NOTE, disabled: true }
      : {
          value: m.id,
          label: m.label + priceHint(m) + (m.available === false ? ' (not enabled in region)' : ''),
        },
  );
  if (currentValue && !options.some((o) => o.value === currentValue)) {
    options.push(
      allowlistActive
        ? { value: currentValue, label: currentValue + ' (current)' + NOT_OFFERED_NOTE, disabled: true }
        : { value: currentValue, label: currentValue + ' (current)' },
    );
  }
  return options;
}

/** The model id to SAVE from a dropdown: '' (leave that role unchanged) when
 * the selected option is disabled, which only happens while it still shows a
 * configured model the allowlist excludes. The server refuses a disallowed
 * id, so sending it back unchanged would fail the whole save. */
export function modelValueToSave(options: ModelOption[], value: string): string {
  const v = value.trim();
  return options.some((o) => o.value === v && o.disabled) ? '' : v;
}

/** GET /api/admin/llm-settings `memory_model` when the memory's strategies
 * run different models (agents/memory_model.py MIXED). */
export const MEMORY_MODEL_MIXED = 'mixed';

/** Options for the memory extraction model select: the catalog's `memory`
 * list (buildModelOptions), plus a disabled entry standing for a value that
 * is not a model: 'mixed', or '' when the current model is not known. */
export function buildMemoryModelOptions(
  items: ModelCatalogEntry[],
  currentValue: string,
  allowlistActive = false,
): ModelOption[] {
  if (currentValue === MEMORY_MODEL_MIXED) {
    return [
      ...buildModelOptions(items, '', allowlistActive),
      { value: MEMORY_MODEL_MIXED, label: 'Mixed (the strategies run different models)', disabled: true },
    ];
  }
  if (!currentValue) {
    return [{ value: '', label: 'Not known', disabled: true }, ...buildModelOptions(items, '', allowlistActive)];
  }
  return buildModelOptions(items, currentValue, allowlistActive);
}

/** The memory model to SAVE: '' (nothing to save) unless the page may
 * change it and the selection is an enabled model other than the current
 * one, or the current one when `repair` (the next deploy would not keep it,
 * so saving it writes only the saved value). */
export function memoryModelToSave(
  options: ModelOption[],
  value: string,
  current: string,
  selectable: boolean,
  repair = false,
): string {
  const v = modelValueToSave(options, value);
  return selectable && v && v !== MEMORY_MODEL_MIXED && (v !== current || repair) ? v : '';
}

/** The words for a memory model id on the Settings card: its catalog label,
 * 'different models (mixed)' for MEMORY_MODEL_MIXED, else the id itself. */
export function memoryModelLabel(items: ModelCatalogEntry[], id: string): string {
  if (id === MEMORY_MODEL_MIXED) return 'different models (mixed)';
  return items.find((m) => m.id === id)?.label || id;
}

/** loadDemoScript() company substitution — the placeholder string in
 * demo-script.md must match the default branding name. */
export function replaceDemoScriptCompany(md: string, companyName: string): string {
  return md.replace(/Agentic Digital Thread/g, companyName);
}

/** loadDemoScript() friendly error copy — verbatim. */
export function demoScriptErrorHtml(message: string): string {
  const friendly = /not found/i.test(message)
    ? 'No demo script is configured for this deployment. Add <code>agentic_digital_thread/frontend/public/demo-script.md</code> to populate this tab.'
    : 'Failed to load demo script: ' + escapeHtml(message);
  return '<div style="color:var(--text2);padding:1rem 0;">' + friendly + '</div>';
}

/** loadDemoScript() post-render style pass — selector → cssText, applied in
 * order (verbatim from admin.js; `code` skips blocks inside <pre>). */
export const DEMO_SCRIPT_STYLES: ReadonlyArray<readonly [string, string]> = [
  ['h1', 'font-size:1.6rem;font-weight:700;margin:0 0 0.5rem;color:var(--text);border-bottom:2px solid var(--accent);padding-bottom:0.5rem;'],
  ['h2', 'font-size:1.15rem;font-weight:600;margin:2rem 0 0.75rem;color:var(--accent);'],
  ['h3', 'font-size:1rem;font-weight:600;margin:1.5rem 0 0.5rem;color:var(--text);'],
  ['p', 'margin:0 0 0.75rem;color:var(--text2);'],
  ['ul,ol', 'margin:0 0 0.75rem 1.25rem;color:var(--text2);'],
  ['li', 'margin-bottom:0.3rem;'],
  ['blockquote', 'border-left:3px solid var(--accent);margin:0.75rem 0;padding:0.5rem 1rem;background:var(--surface2);border-radius:0 4px 4px 0;color:var(--text2);font-style:italic;'],
  ['code', 'background:var(--surface2);padding:0.1rem 0.35rem;border-radius:3px;font-size:0.85em;color:var(--accent);'],
  ['table', 'width:100%;border-collapse:collapse;margin:1rem 0;font-size:0.85rem;'],
  ['th', 'text-align:left;padding:0.5rem 0.75rem;background:var(--surface2);color:var(--text);font-weight:600;border-bottom:1px solid var(--border);'],
  ['td', 'padding:0.5rem 0.75rem;border-bottom:1px solid var(--border);color:var(--text2);'],
  ['hr', 'border:none;border-top:1px solid var(--border);margin:2rem 0;'],
  ['a', 'color:var(--accent);'],
];

/** Apply the demo-script style pass to a rendered container. */
export function applyDemoScriptStyles(el: HTMLElement): void {
  for (const [selector, cssText] of DEMO_SCRIPT_STYLES) {
    el.querySelectorAll<HTMLElement>(selector).forEach((node) => {
      // Inline code only — fenced blocks inside <pre> keep their styling.
      if (selector === 'code' && node.parentElement?.tagName === 'PRE') return;
      node.style.cssText = cssText;
    });
  }
}
