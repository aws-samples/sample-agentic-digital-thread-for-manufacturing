// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Single shared HTML-escape helper (DRY consolidation). The three legacy
 * helpers escHtml (chat), obEsc (onboard) and slEsc (architecture)
 * are output-identical (nullish → ''); they are re-exported under their
 * legacy names from chat/md.ts, views/onboard/util.ts and
 * views/architecture/sl-cards.ts so call sites and tests stay untouched.
 *
 * NOTE: pages/admin/tabs/admin-logic.ts re-exports this helper too — its
 * verbatim legacy copy (no nullish guard) was folded in after auditing the
 * call sites: all are nullish-guarded, so output is identical in practice. */

/** Escape untrusted text (LLM-authored fields, source-derived values, system
 * labels) before it is interpolated into an HTML STRING PAYLOAD (e.g. the
 * Word-export download document — no in-page innerHTML sink remains after the
 * zero-innerHTML migration). Escapes the five HTML-significant characters;
 * nullish input renders as ''. */
export function escapeHtml(str: unknown): string {
  return String(str == null ? '' : str).replace(
    /[&<>"']/g,
    (c) =>
      (({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }) as Record<
        string,
        string
      >)[c],
  );
}
