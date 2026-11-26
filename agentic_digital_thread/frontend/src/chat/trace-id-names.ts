// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Graph names for the ids in the Investigation trace's final answer.
 *
 * The trace route returns the stored answer as written plus
 * `final_answer_names`, the graph names of the ids in it
 * (agents_trace_routes._with_answer_names). The card places them with
 * `remarkIdNames`, listed after every other remark plugin of the trace
 * prose, so it runs after remark-gfm (whose literal autolinks are made while
 * parsing) and remarkLocalTimes. Each id in a text, inline code or code node
 * value becomes `Name (prefix…)` as plain node value, and so does an id in
 * an image's alt text, which the trace shows as text. Parsing is done by
 * then, so a name cannot become markup, a link or a time.
 *
 * Left as they are: link and image urls, definitions, raw HTML, the visible
 * text of a link that shows its own destination (an autolink or a literal
 * URL, `www.` or email), and a text node another plugin gave its own element
 * (a local time).
 *
 * One pass over each value, as `id_name_resolver.annotate_text` names the
 * chat trace: a name is shown exactly as the graph gave it, even when it
 * holds another known id. */

/** An id the route names: the UUID shape `id_name_resolver` resolves. */
const ID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
/** The route sends at most one lookup's worth of names. */
const MAX_NAMES = 50;
/** How many leading characters of the id stay as its short reference. */
const ID_PREFIX_CHARS = 8;

export type IdNames = ReadonlyMap<string, string>;

/** The route's `final_answer_names`, read defensively: only id-shaped keys
 * with a non-empty string name other than the id, at most 50; `null` when
 * none is usable. */
export function readIdNames(value: unknown): IdNames | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const out = new Map<string, string>();
  for (const [id, name] of Object.entries(value as Record<string, unknown>)) {
    if (out.size >= MAX_NAMES) break;
    if (ID_RE.test(id) && typeof name === 'string' && name !== '' && name !== id) out.set(id, name);
  }
  return out.size > 0 ? out : null;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** `text` with every known id replaced by `Name (prefix…)`, in one pass. */
export function nameIds(text: string, names: IdNames): string {
  const ids = [...names.keys()].filter((id) => text.includes(id));
  if (ids.length === 0) return text;
  const pattern = new RegExp(
    ids.sort((a, b) => b.length - a.length).map(escapeRegExp).join('|'),
    'g',
  );
  return text.replace(pattern, (id) => `${names.get(id)} (${id.slice(0, ID_PREFIX_CHARS)}\u2026)`);
}

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  alt?: string | null;
  children?: MdNode[];
  data?: Record<string, unknown>;
}

/** Nodes whose content is never named. */
const LEAVE = new Set(['html', 'definition']);
/** Nodes whose `value` is shown as text. */
const VALUES = new Set(['text', 'inlineCode', 'code']);
/** Nodes whose `alt` is shown as text (the trace renders an image as its alt). */
const ALTS = new Set(['image', 'imageReference']);

function plainText(node: MdNode): string {
  if (typeof node.value === 'string') return node.value;
  return (node.children ?? []).map(plainText).join('');
}

/** A link whose visible text is its destination, as remark writes one: an
 * autolink or literal URL (the same text), a literal `www.` link (any case,
 * as GFM accepts; a web scheme in front), or an email (`mailto:` in front).
 * An explicit link whose text only ends its destination is an ordinary link
 * and is named. */
function showsItsUrl(node: MdNode): boolean {
  if (node.type !== 'link' || typeof node.url !== 'string') return false;
  const label = plainText(node);
  if (label === '') return false;
  const url = node.url;
  return (
    url === label ||
    (label.slice(0, 4).toLowerCase() === 'www.' && (url === `http://${label}` || url === `https://${label}`)) ||
    (label.includes('@') && url === `mailto:${label}`)
  );
}

function rename(node: MdNode, names: IdNames): void {
  if (VALUES.has(node.type) && typeof node.value === 'string') {
    if (!node.data?.hName) node.value = nameIds(node.value, names);
    return;
  }
  if (ALTS.has(node.type)) {
    if (typeof node.alt === 'string') node.alt = nameIds(node.alt, names);
    return;
  }
  if (LEAVE.has(node.type) || showsItsUrl(node)) return;
  for (const child of node.children ?? []) rename(child, names);
}

/** react-markdown remark plugin placing `names`; list it LAST. */
export function remarkIdNames(names: IdNames) {
  return () => (tree: MdNode) => rename(tree, names);
}
