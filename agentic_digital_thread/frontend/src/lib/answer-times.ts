// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Answer times: the model writes every time in UTC and the page shows it in
 * the viewer's own zone.
 *
 * The model used to convert UTC to the user's zone itself and sometimes got
 * the arithmetic wrong (a window end six hours off). Now the prompt asks for
 * one fixed UTC form, `YYYY-MM-DD HH:MM UTC` (seconds when the record has
 * them), and this module turns each such time, or an ISO instant ending in Z,
 * into the viewer's local date and time with Intl, which is always right,
 * daylight saving included.
 *
 * `remarkLocalTimes` is a remark plugin for react-markdown. It rewrites text
 * nodes only: code spans, fenced code, raw HTML and link text (citation
 * badges included) are never touched, and a time with no date or a date with
 * no time is left as written. Each match renders as
 * `<time dateTime="<ISO>" title="<UTC source>">local text</time>`. */

export interface LocalTimeOptions {
  /** IANA zone; undefined means the browser's own zone. */
  timeZone?: string;
  /** Locale for the zone name; undefined means the browser's locale. */
  locale?: string;
}

export interface UtcStamp {
  /** Offsets of the match in the source text. */
  start: number;
  end: number;
  /** The text as the answer wrote it. */
  text: string;
  /** The same instant as an ISO string ending in Z (the <time> dateTime). */
  iso: string;
  /** Whether the source carried seconds. */
  seconds: boolean;
  /** The source's fraction of a second, `.250` (at most 3 digits), or ''. */
  fraction: string;
}

let pinnedZone: string | undefined;
let pinnedLocale: string | undefined;

/** Pin the display zone and locale (tests; a later user setting could use it).
 * `null` returns to the browser's own zone and locale. */
export function setDisplayTimeZone(timeZone: string | null, locale?: string): void {
  pinnedZone = timeZone ?? undefined;
  pinnedLocale = timeZone ? locale : undefined;
}

/** The zone and locale answer times render in right now. */
export function localTimeOptions(): LocalTimeOptions {
  return { timeZone: pinnedZone, locale: pinnedLocale };
}

/** The IANA zone answer times render in (the pinned one, else the browser's). */
export function displayZoneName(): string {
  return pinnedZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** The browser's own zone and locale, sent with every user-triggered run
 * (chat question, pinned-card refresh) so the server's time line resolves
 * "today" against the user's clock. Never the pinned display zone. */
export function clientTimeContext(): { timezone: string; locale: string } {
  return { timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, locale: navigator.language };
}

// Answer form `2026-10-15 14:17[:05] UTC`, or ISO `2026-10-15T14:17[:05[.250]]Z`.
// Not inside a longer word, and never a UTC offset: `UTC+5`, or one spaced on
// the same line (any horizontal space: `UTC +5`, `UTC - 05:30`, `UTC −0530`).
// An offset is a sign and one or two hour digits, with optional minutes, so a
// full range written with a hyphen (`... UTC - 2026-10-16 03:18 UTC`) still
// converts at both ends.
const STAMP_RE =
  /(?<![\w])(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(\.\d{1,9})?)?Z(?![\w])| (\d{2}):(\d{2})(?::(\d{2}))? UTC(?![\w+\u2212]|[^\S\r\n\u2028\u2029]*[+\u2212-][^\S\r\n\u2028\u2029]*\d{1,2}(?::?\d{2})?(?![\d-])))/g;

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** True when the fields name a real calendar instant (no Feb 30, no 24:00). */
function isRealInstant(y: number, mo: number, d: number, h: number, mi: number, s: number): boolean {
  if (mo < 1 || mo > 12 || h > 23 || mi > 59 || s > 59) return false;
  const t = new Date(Date.UTC(y, mo - 1, d, h, mi, s));
  return t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
}

// A zoned local time right before `(`: `08:17 MDT (`, `8:17 a.m. CDT (`,
// `19:47 +05:30 (`. Answers written before times went UTC gave local time
// first and the UTC value in parentheses; that pair is one value, so the UTC
// half stays as written instead of rendering a second local copy.
const ZONED_LOCAL_BEFORE_PAREN_RE =
  /\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?\s+(?:(?!UTC\b|GMT\b)[A-Z]{2,5}|[+\-\u2212]\d{2}:?\d{2}|(?:UTC|GMT)[+\-\u2212]\d{1,2}(?::?\d{2})?)\s*\(\s*$/;

/** True when the stamp at `start` is the UTC audit half of a stored
 * `<local time> (<UTC time>)` pair. */
function isLegacyAuditStamp(text: string, start: number): boolean {
  return ZONED_LOCAL_BEFORE_PAREN_RE.test(text.slice(Math.max(0, start - 48), start));
}

/** Every UTC time in `text` that has both a date and a time. */
export function findUtcStamps(text: string): UtcStamp[] {
  const out: UtcStamp[] = [];
  STAMP_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = STAMP_RE.exec(text)) !== null) {
    const iso = m[4] !== undefined;
    const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
    const h = Number(iso ? m[4] : m[8]);
    const mi = Number(iso ? m[5] : m[9]);
    const secText = iso ? m[6] : m[10];
    if (!isRealInstant(y, mo, d, h, mi, Number(secText ?? 0))) continue;
    if (isLegacyAuditStamp(text, m.index)) continue;
    const isoText = iso
      ? m[0]
      : `${m[1]}-${m[2]}-${m[3]}T${pad2(h)}:${pad2(mi)}${secText !== undefined ? ':' + secText : ''}Z`;
    out.push({
      start: m.index,
      end: m.index + m[0].length,
      text: m[0],
      iso: isoText,
      seconds: secText !== undefined,
      fraction: iso && m[7] ? m[7].slice(0, 4) : '',
    });
  }
  return out;
}

const formatters = new Map<string, { parts: Intl.DateTimeFormat; zone: Intl.DateTimeFormat }>();

function formattersFor(seconds: boolean, opts: LocalTimeOptions) {
  const key = `${opts.timeZone ?? ''}|${opts.locale ?? ''}|${seconds ? 's' : ''}`;
  let f = formatters.get(key);
  if (!f) {
    // Digits from a fixed locale so the shape matches the answer's own form;
    // the zone name in the viewer's locale (e.g. MDT, GMT+5:30, MESZ).
    f = {
      parts: new Intl.DateTimeFormat('en-US', {
        timeZone: opts.timeZone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        ...(seconds ? { second: '2-digit' as const } : {}),
        hourCycle: 'h23',
      }),
      zone: new Intl.DateTimeFormat(opts.locale, { timeZone: opts.timeZone, timeZoneName: 'short' }),
    };
    formatters.set(key, f);
  }
  return f;
}

interface LocalParts {
  /** `YYYY-MM-DD` in the zone. */
  date: string;
  /** `HH:MM[:SS[.fff]]` in the zone. */
  time: string;
  /** Short zone name (`MDT`, `GMT+5:30`), '' when the runtime gives none. */
  zone: string;
}

/** The local date, time and zone name of a UTC instant, or null when the
 * zone is unknown. A fraction of a second is copied from the source: zone
 * offsets are whole minutes, so it does not change. */
function localParts(iso: string, seconds: boolean, opts: LocalTimeOptions, fraction = ''): LocalParts | null {
  let f;
  try {
    f = formattersFor(seconds, opts);
  } catch {
    return null;
  }
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  const part = (type: string) => f.parts.formatToParts(when).find((p) => p.type === type)?.value ?? '';
  const zone = f.zone.formatToParts(when).find((p) => p.type === 'timeZoneName')?.value ?? '';
  const time = `${part('hour')}:${part('minute')}${seconds ? ':' + part('second') + fraction : ''}`;
  return { date: `${part('year')}-${part('month')}-${part('day')}`, time, zone };
}

function joinParts(p: LocalParts): string {
  return `${p.date} ${p.time}${p.zone ? ' ' + p.zone : ''}`;
}

/** `YYYY-MM-DD HH:MM[:SS] <zone>` in the given zone, or null when the zone
 * is unknown (the caller then keeps the UTC text). */
export function formatLocalStamp(iso: string, seconds: boolean, opts: LocalTimeOptions = {}): string | null {
  const p = localParts(iso, seconds, opts);
  return p ? joinParts(p) : null;
}

/** `text` with each UTC time replaced by `render(stamp, local)`; a time whose
 * zone cannot be resolved stays as written. */
function replaceStamps(text: string, opts: LocalTimeOptions, render: (s: UtcStamp, local: string) => string): string {
  let out = '';
  let last = 0;
  for (const s of findUtcStamps(text)) {
    const local = formatLocalStamp(s.iso, s.seconds, opts);
    if (local === null) continue;
    out += text.slice(last, s.start) + render(s, local);
    last = s.end;
  }
  return out + text.slice(last);
}

/** Plain text with its UTC times in the viewer's zone (chart titles). */
export function localizeUtcText(text: string, opts: LocalTimeOptions = localTimeOptions()): string {
  return replaceStamps(text, opts, (_s, local) => local);
}

/** HTML-escaped text with each UTC time as `<time datetime title>local</time>`
 * (the Word export, which renders markdown to an HTML string). A stamp holds
 * only digits, `-`, `:`, `.`, `T`, `Z`, space and `UTC`, so escaping cannot
 * have changed it, and its text is safe inside an attribute. */
export function utcStampsToTimeHtml(escaped: string, opts: LocalTimeOptions = localTimeOptions()): string {
  return replaceStamps(
    escaped,
    opts,
    (s, local) => `<time datetime="${s.iso}" title="${s.text}">${local}</time>`,
  );
}

/* ---- chart labels ---- */

export interface ChartTimeLabels {
  /** Short axis ticks: `HH:MM`, with `MM-DD` (or the full date across years)
   * when the series covers more than one local day. */
  ticks: unknown[];
  /** Full local date, time and zone (the tooltip). */
  full: unknown[];
}

/** A label that is exactly one UTC instant, or null. */
function chartStamp(label: unknown): UtcStamp | null {
  if (typeof label !== 'string') return null;
  const text = label.trim();
  const [stamp] = findUtcStamps(text);
  return stamp && stamp.start === 0 && stamp.end === text.length ? stamp : null;
}

function tickText(p: LocalParts, days: Set<string>): string {
  if (days.size <= 1) return p.time;
  const years = new Set([...days].map((d) => d.slice(0, 4)));
  return `${years.size > 1 ? p.date : p.date.slice(5)} ${p.time}`;
}

/** Chart labels that are UTC instants, shown in the viewer's zone. Labels
 * that are not instants pass through; null when no label is an instant (or
 * the zone is unknown), so the caller leaves the chart as it was. */
export function localChartLabels(labels: unknown[], opts: LocalTimeOptions = localTimeOptions()): ChartTimeLabels | null {
  const parts = labels.map((label) => {
    const s = chartStamp(label);
    return s ? localParts(s.iso, s.seconds, opts, s.fraction) : null;
  });
  if (!parts.some(Boolean)) return null;
  const days = new Set(parts.flatMap((p) => (p ? [p.date] : [])));
  return {
    ticks: labels.map((label, i) => {
      const p = parts[i];
      return p ? tickText(p, days) : label;
    }),
    full: labels.map((label, i) => {
      const p = parts[i];
      return p ? joinParts(p) : label;
    }),
  };
}

/* ---- remark plugin ---- */

interface MdNode {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: Record<string, unknown>;
}

// Never rewrite inside these: code and raw HTML stay byte for byte, and link
// text (citation badges included) is the author's label.
const SKIP = new Set(['code', 'inlineCode', 'html', 'link', 'linkReference', 'image', 'imageReference', 'definition']);

/** Split one text value into text and <time> nodes; null when nothing matched. */
function splitText(value: string, opts: LocalTimeOptions): MdNode[] | null {
  const stamps = findUtcStamps(value);
  if (stamps.length === 0) return null;
  const out: MdNode[] = [];
  let last = 0;
  for (const s of stamps) {
    const local = formatLocalStamp(s.iso, s.seconds, opts);
    if (local === null) continue;
    if (s.start > last) out.push({ type: 'text', value: value.slice(last, s.start) });
    out.push({
      type: 'text',
      value: local,
      data: { hName: 'time', hProperties: { dateTime: s.iso, title: s.text } },
    });
    last = s.end;
  }
  if (out.length === 0) return null;
  if (last < value.length) out.push({ type: 'text', value: value.slice(last) });
  return out;
}

function rewrite(node: MdNode, opts: LocalTimeOptions): void {
  if (!node.children) return;
  const out: MdNode[] = [];
  let changed = false;
  for (const child of node.children) {
    const pieces = child.type === 'text' && typeof child.value === 'string' ? splitText(child.value, opts) : null;
    if (pieces) {
      out.push(...pieces);
      changed = true;
      continue;
    }
    if (!SKIP.has(child.type)) rewrite(child, opts);
    out.push(child);
  }
  if (changed) node.children = out;
}

/** react-markdown remark plugin: UTC answer times render in the viewer's zone. */
export function remarkLocalTimes() {
  return (tree: MdNode) => rewrite(tree, localTimeOptions());
}
