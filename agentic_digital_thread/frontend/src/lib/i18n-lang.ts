// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* i18n — AI Response Language directive.
 *
 * Wraps prompts sent to the Orchestrator with an output-language instruction.
 * The AI writes its final answer in the selected language while keeping
 * technical tokens and machine-parsed markup in English. This does NOT
 * translate the UI.
 *
 * The directive is appended to the END of the prompt: the orchestrator runs a
 * long tool-calling loop with a fixed (English) system prompt, and models
 * weight the most recent instruction most heavily when producing the final
 * answer. A bracketed prefix at the start gets buried by the workflow and is
 * honored inconsistently. */

// Per-language final-answer directive. `en` has none (no wrapping → prompt unchanged).
// Each directive explicitly protects SQL, identifiers, citations, chart tags, and the
// structured proposal/followup block labels that the front-end and backend parse —
// translating those would break rendering and proposal capture.
const _PROTECT =
  'Do NOT translate the following — keep them verbatim in English: '
  + 'SQL, table/column/field names, system IDs, numeric values and units, citation '
  + 'markers like [1] [2], <chart-block ...> tags, and the structured block labels and '
  + 'keys (---VALUE-MAPPING-PROPOSAL---, ---CORRELATION-PROPOSAL---, ---FOLLOWUPS---, '
  + 'from_system, to_system, from_field, to_field, method, confidence, etc.).';

const _LANG_DIRECTIVES: Record<string, string> = {
  ja: `[OUTPUT LANGUAGE: Write your ENTIRE final answer to the user in Japanese (日本語) — all prose, headings, explanations, and the text of follow-up questions and recommended actions. ${_PROTECT} This overrides any default to English.]`,
  de: `[OUTPUT LANGUAGE: Write your ENTIRE final answer to the user in German (Deutsch) — all prose, headings, explanations, and the text of follow-up questions and recommended actions. ${_PROTECT} This overrides any default to English.]`,
  fr: `[OUTPUT LANGUAGE: Write your ENTIRE final answer to the user in French (Français) — all prose, headings, explanations, and the text of follow-up questions and recommended actions. ${_PROTECT} This overrides any default to English.]`,
  ko: `[OUTPUT LANGUAGE: Write your ENTIRE final answer to the user in Korean (한국어) — all prose, headings, explanations, and the text of follow-up questions and recommended actions. ${_PROTECT} This overrides any default to English.]`,
  zh: `[OUTPUT LANGUAGE: Write your ENTIRE final answer to the user in Chinese (中文) — all prose, headings, explanations, and the text of follow-up questions and recommended actions. ${_PROTECT} This overrides any default to English.]`,
};

/** Languages offered by the header #lang-select (value → label). */
export const SUPPORTED_LANGS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'en', label: 'EN' },
  { value: 'ja', label: '日本語' },
  { value: 'de', label: 'DE' },
  { value: 'fr', label: 'FR' },
  { value: 'ko', label: '한국어' },
  { value: 'zh', label: '中文' },
];

export function getResponseLang(): string {
  return localStorage.getItem('adt-lang') || navigator.language.split('-')[0] || 'en';
}

/** Append the output-language directive to a prompt. Returns the prompt
 * unchanged for English / unsupported languages so the default English path is
 * untouched. */
export function wrapResponseLang(question: string): string {
  const directive = _LANG_DIRECTIVES[getResponseLang()];
  return directive ? `${question}\n\n${directive}` : question;
}

export function setResponseLang(lang: string): void {
  localStorage.setItem('adt-lang', lang);
}

/** Initial #lang-select value: saved/browser language when it's a supported
 * option, else 'en' (port of the DOMContentLoaded init in i18n-lang.js). */
export function initialSelectLang(): string {
  const lang = getResponseLang();
  return SUPPORTED_LANGS.some((l) => l.value === lang) ? lang : 'en';
}
