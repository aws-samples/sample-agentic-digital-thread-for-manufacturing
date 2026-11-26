<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0097: Answers write times in UTC; the browser shows them in the viewer's zone

## Status

**Accepted: 2026-10-15.** Verified offline (unit lane, frontend typecheck, vitest
and build). Not yet checked on a deployed account.

## Context

Every chat turn carries a per-turn time line (`agents/time_context.py`,
`render_time_context`), composed by `server._prepare_question` from the zone and
locale the browser sends (`frontend/src/chat/useAskStream.ts`). It gives the
current UTC instant and, when the zone is known, the user's local time, so
relative phrases such as "today", "last 24 hours" and "this month" resolve
against the user's clock.

From 2026-07-16 the same line also told the model to present every time in the
answer in the user's zone, keeping UTC in parentheses for audit values. No code
converted those times: the model did the arithmetic itself. It got it wrong
more than once:

- 2026-10-04 (a fresh workshop deployment): given `-0600`, the model subtracted seven hours
  and wrote 15:02 MDT for 22:02Z. The line gained an offset sentence that
  spells the arithmetic out and applies it to the current instant.
- 2026-10-15 (a fresh workshop deployment): an answer's "window queried" end read 02:17 MDT
  for 08:17 MDT (14:17Z), six hours off. Two paths fit: a local time sent to a
  system agent that read it as UTC and was then shifted again, or a UTC time
  written as local. Times sent to systems were made UTC with `Z` the same day,
  which closes the first path only.

A browser converts an instant to its own zone correctly every time, daylight
saving included, with `Intl.DateTimeFormat`.

## Decision

1. **The model never converts a time.** Every time it sends to a system (a
   delegated question, a tool's time argument, a query) is UTC, ISO with `Z`.
   Every time it writes in an answer is UTC in one form,
   `YYYY-MM-DD HH:MM UTC`, with seconds only when the record has them and
   always with its date. The standing orchestrator rule `_TIMES_SENT_RULE`
   (`TIMES ARE UTC`) says this in both modes and both Quick variants. The
   window echo states its window as UTC instants in the same form.
2. **The user's zone only resolves relative phrases.** The zoned time line keeps
   the zone and local "now", says to use them only to turn relative phrases into
   UTC bounds, and says answer times stay UTC because the user's screen shows
   them in the user's own zone. Its offset sentence now runs local to UTC
   ("MDT is UTC-06:00 at this instant: add 6 hours to a local time to get
   UTC"), the one direction the model still converts in, and says it holds for
   the current instant only. The UTC-only line (system agents, Quick) is
   unchanged.
3. **Local period starts are computed, not converted by the model.** One
   offset is wrong for a local time across a daylight-saving change: at 03:00
   MST on the day Denver falls back, that day's midnight was still MDT, so
   "today" starts at 06:00 UTC, not 07:00. The zoned line lists, as UTC
   instants computed with ZoneInfo at each bound's own offset, the start of
   today, of yesterday, of this week (a week starts on Monday), of this month
   and of last month, and now. A day whose midnight a spring-forward gap skips
   starts when the gap starts. A shift has no server bound: its local start can
   sit on either side of a change and only a system's records know when it
   began, so the line says to take a shift's start and end as absolute times
   from a system, and to say the window could not be resolved when none gives
   them.
4. **Every path that runs a question carries the time line.** `/api/ask`,
   running a saved prompt and refreshing a pinned card enrich the text through
   one helper, `time_context.with_time_context`; the browser sends its zone and
   locale on the last two as it does on a chat question. A scheduled run gets
   the UTC-only line with the clock read at the run. No zone is stored with a
   schedule.
5. **System agents write the same form.** Their policy tail (every connection
   type, after any registration prompt) asks for `YYYY-MM-DD HH:MM UTC` with
   its date. A time a record stores with another offset is quoted as stored.
   The rule names no screen, since an answer relayed to Quick is read as text.
6. **The browser shows each UTC time in the viewer's zone.** A remark plugin
   (`frontend/src/lib/answer-times.ts`, `remarkLocalTimes`) runs in the shared
   `<Md/>` component and in the investigation trace's prose, so every surface
   that renders answer markdown uses it: the chat answer and its stream, the
   data modal, the dashboard briefing and pinned cards. It matches
   `YYYY-MM-DD HH:MM[:SS] UTC` and ISO `YYYY-MM-DDTHH:MM[:SS[.fff]]Z` in text
   and renders `<time dateTime="<ISO>" title="<UTC source>">` with the local
   date and time and the zone's short name, for example `2026-10-15 08:17 MDT`.
   Code spans, fenced code, raw HTML and link text (citation badges included)
   are never rewritten, nor is a time with no date or a date with no time. A
   UTC time in parentheses right after a zoned local time (an answer stored
   before this change, `08:17 MDT (2026-10-15 14:17 UTC)`) is left as written,
   so the pair stays one value. A UTC time followed on the same line by an
   offset (`UTC+5`, `UTC +5`, `UTC - 05:30`, any horizontal space) is not
   converted; a sign followed by a full date is a range separator, not an
   offset. A range is written as two full times (`2026-10-16 02:19 UTC →
   2026-10-16 03:18 UTC`; the orchestrator and system-agent times rules), so
   each end converts on its own; a range
   written with one date or one `UTC` stays as written, in UTC. Parsing that
   short form was tried and dropped: it kept misreading identifiers, paths,
   offsets and punctuation next to a range.
7. **Charts and the Word export match the screen.** Chart labels are full UTC
   instants (the orchestrator chart rule and the SiteWise and i3X chart
   builders), and a SiteWise chart title states its window in the answer form.
   The chart shows labels in the viewer's zone (`HH:MM`, with the date when the
   series crosses local midnight; the full local date, time and zone in the
   tooltip) and converts the title's times. The Word export renders the stored
   answer with the same conversion as `<time>` elements and says which zone it
   uses.

## Consequences

- One correct conversion, in the browser, replaces model arithmetic on every
  answer. Hovering a converted time shows the UTC text the answer wrote.
- AWS Quick shows the answer as written, so Quick readers see UTC with the
  `UTC` label. The standing rule therefore gives no reason that is only true of
  the chat page.
- A downloaded Word file shows the zone of the browser that made it, and says
  so; each time's title keeps its UTC value.
- Chart labels are longer in the tag (an ISO instant instead of `HH:MM`).
- Tool payloads shown in the investigation trace stay literal data and are not
  converted; the trace's prose is.
- Local bounds other than the listed starts (for example the start of a
  quarter, or a named local hour across a change) still rely on the offset
  sentence, which says it may not hold across a daylight-saving change.

## Alternatives considered

- **Keep model-side conversion and add more worked examples.** Two rounds of
  wording already failed on live answers; the arithmetic stays the model's.
- **Convert on the server before streaming.** The server does not know the
  viewer's zone for a shared or rehydrated answer, and stored answers would
  carry one viewer's zone forever.
- **Show both, `14:17 UTC (08:17 MDT)`, in the text.** Doubles every time in
  tables and still needs a converter; the `<time>` title keeps the UTC value
  one hover away.
