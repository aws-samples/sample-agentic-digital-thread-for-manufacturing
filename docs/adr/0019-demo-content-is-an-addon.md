<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Demo content is an optional addon; the core runs without it

> **Historical naming.** The project and its demo seed database were renamed
> to `agentic_digital_thread` (ADT) and `adt_demo` after this record was
> written; identifiers in the historical text may reflect the old names.
> Current equivalents: SSM parameters and secrets use the `adt` prefix
> (`/adt/*`, `adt/conn/<id>`). The decision itself is unaffected.


Curated demo content — the sample/“suggested” prompt pack and the CLI demo questions — lives
**outside the core**, isolated in `agentic_digital_thread/agents/demo/` and gated behind a single
`ADT_DEMO` environment flag (default **off**). The core application runs, imports, and passes its
test suite with the addon disabled; the demo deployment **tacks it on** by setting `ADT_DEMO=1`.
The intent is a clean separation where the product core carries no demo-specific data, and a demo
is an additive layer you enable.

## Context

`agents/prompts.py` shipped a hand-tuned `_REGISTRY` of ~20 curated investigation scenarios (daily
SQCDP briefing, cross-plant quality comparison, "Milwaukee Filler Pump Health", …) embedding the
demo's specific plants (Milwaukee/Detroit/Cedar Rapids), schemas, SQL, and SiteWise asset paths;
`agents/main.py` carried matching hardcoded CLI demo questions. These were seeded into every
running instance and served from `/api/prompts`, so the *core* always carried *demo* data.

Per ADR 0009 the orchestrator's *reasoning* topology is already graph-derived (`MATCH (s:System)`
+ per-node `query_guidance`) and carries no hardcoded names — the upstream merge of 2026-06-12
completed that genericization. The remaining hardcoded plant names were **not** mechanical
scaffolding but *curated showcase content* whose value is its specificity. The directive
(2026-06-15) is to make anything demo-related an optional addon so the core runs without it.

## Considered Options

- **Derive the suggested questions from `MATCH (s:System)`** (ADR 0009's original stance for the
  sample questions): rejected for these prompts. They are tuned multi-step demo narratives with real
  SQL/asset paths; generating them from the graph would destroy their value and is not "low-risk".
- **Leave the curated registry in core, just filter by `required_systems`:** rejected. The core
  still ships demo data; filtering hides but does not separate it.
- **Make demo content an addon (chosen):** move it into `agents/demo/`, gate on `ADT_DEMO`, route the
  core consumers through a thin seam; the demo deployment enables it. Core is clean; demo unchanged.

## Decision

- New package `agents/demo/` is the home for demo-only content. `is_enabled()` reads `ADT_DEMO`
  (true for `1/true/yes/on`); `demo_prompts()` / `demo_questions()` return the curated pack.
- `prompts.seed_registry()` and `list_prompts()`’ registry fallback seed/return the pack **only when
  `demo.is_enabled()`**; otherwise no-op (user prompt CRUD + scheduling are unaffected).
  `main.DEMO_QUESTIONS` is the pack when enabled, else an empty list. No core module embeds the pack.
- The demo deployment sets `ADT_DEMO=1` in the `adt-app` container env (`stacks/adt_compute_stack.py`),
  so the showcase is unchanged while the code default stays off.

## Consequences

- Clean core: with `ADT_DEMO` unset the app imports and the full lane is green with no demo data;
  `/api/prompts` returns no `registry` entries and the UI degrades to an empty suggested-prompts
  state (already guarded client-side).
- One flag flips the whole demo layer on; `agents/demo/` is a reusable seam for future demo content.
- **Refines ADR 0009:** the orchestrator *prompt* remains graph-derived (0009 holds, now locked by a
  guard test), but the curated *sample questions* are treated as demo content (addon), not derived
  from the graph — they are showcase narratives, not topology guidance.

## Runtime toggle (admin console)

The addon is **togglable at runtime from the admin console**, so enabling a demo is not a
deploy-time-only decision. `demo.is_enabled()` resolves with precedence **persisted runtime
override → `ADT_DEMO` env (boot default) → off**. The override is persisted in **SSM Parameter
Store** (`/adt/demo-enabled`), mirroring the existing branding / LLM-settings admin
flows (the task role already has `ssm:GetParameter`/`ssm:PutParameter` on `/adt/*`,
so this needed no infra/IAM change). `PUT /api/admin/settings {demo_enabled}` (admin-gated) writes
the override and **takes effect immediately** — seeding the registry on enable and removing the
`source=='registry'` prompts on disable — while a session-authed `GET /api/demo/status` lets the
dashboard show/hide the `▶ Demo` button and the admin console show/hide the Demo Script tab. The
demo seam stays pure (ADR 0019 one-way dep): SSM access lives in a `SsmSettingsStore` injected via
`demo.configure(...)`; the seam holds only an in-memory `_override` (read at boot via
`load_persisted()`), so `is_enabled()` never hits SSM per call. Single-uvicorn assumption (ADR
0005): the in-process override is authoritative; other instances would converge at next boot.

## Deferred — demo content still to addon-ify (do not lose)

Only the prompt pack + CLI questions moved in this slice. Tracked for later migration into the same
`agents/demo/` seam (each its own slice; some are riskier):

- Catalog **seed systems** (`agents/catalog.py` `_INITIAL_SYSTEMS` — Milwaukee/Detroit/Cedar Rapids
  systems) and the schema-prefix demo assumptions.
- Demo SQL embedded inside prompt `text` fields (now in the pack, but still demo-shaped).
- Media/video demo assets (`MEDIA_BUCKET` / `VIDEO_BUCKET`, `video_briefing.py`, onboarding video).
- Any remaining Milwaukee/Detroit/Cedar Rapids literals outside `agents/demo/` (e.g. `dashboard.py`
  site-label CASE map, `system_agent.py` example sites, `runtime_app.py` `MES_SYSTEMS`,
  `agents_routes.py` agent display names/order).

## Related finding (separate fix, not demo-specific)

While wiring runtime persistence we found `APP_SETTINGS_TABLE_NAME` is **unset in every stack**, so
the DynamoDB-backed **dashboard card store silently runs in-memory** (`server.py` reuses that table
for cards) — persisted cards do not survive a container restart in deployment today. This is
unrelated to the demo addon (the demo flag uses SSM, not that table) and needs its own table +
wiring. Tracked here so it is not lost; out of scope for the demo toggle.

## Status

Accepted. Tracked as a item.
