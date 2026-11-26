<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Wave 4.2 — e2e contract audit of the SPA (test-suite audit)

> **Maintainer documentation** — working notes for contributors and AI agents,
> not part of the user-facing guide.

Every selector, window global, and URL assertion in `tests/e2e/*.py` +
`conftest.py`, verified against the **built SPA** (`npm run build` →
`frontend/dist/`, served by `vite preview`, driven with python-playwright and
`/api/*` mocked via route interception — same admin/demo gating a deployed demo
env presents). Data-driven contracts that need real onboarded data were
DOM-grepped in the built bundle and source-mapped instead; they are marked
`data-driven` and re-verified against the deployed env in Wave 5.

**Current result:** SPA selectors remain present. The browser-entry contract is
now mode-aware: normal deployments authenticate at the CloudFront edge through
the Cognito hosted UI, while workshop/local or explicit edge-off runs retain
the built `login.html` entry.

Statuses: **present** (in the DOM as the test expects) ·
**present-after-activation** (rendered once its view/tab first activates —
matches old lazy-init semantics; the e2e test performs the activation itself) ·
**data-driven** (only rendered with onboarded data; verified in bundle source) ·
**server** (backend contract, unchanged by the frontend port).

## conftest.py

| Contract | Status | Notes |
|---|---|---|
| `GET /health` reachability gate | server | Unchanged (`agents/server.py`). |
| `ADT_EDGE_AUTH=on` (default) | present | Shared helper opens `/`, verifies a `*.amazoncognito.com` hosted-UI URL, signs in, and returns to the app without showing `#login-form`. |
| `ADT_EDGE_AUTH=off` + `/login.html` selectors | present | Workshop/local fallback keeps `#email`, `#password`, and `#submit-btn`; helper waits until the URL leaves `/login.html`. |

## test_health.py

| Contract | Status | Notes |
|---|---|---|
| `/health`, `/api/branding` public; `/api/version` 401 | server | Auth middleware unchanged (Task 4.1 owns `PUBLIC_PREFIXES`). |
| `/css/fonts.css` public 200 (no redirect) | present | `frontend/public/css/fonts.css` byte-copy → served from dist root. |
| Web fonts | present | IBM Plex Mono (400, 600) and Outfit (variable) from the pinned npm packages `@fontsource/ibm-plex-mono` and `@fontsource-variable/outfit`; `src/styles/fonts.css` holds the `@font-face` rules and the build copies the latin and latin-ext woff2 files into `dist/assets/`, served from the app origin (`/assets/` is public, so the login page gets them before sign-in). No font CDN request. |
| `/` unauthenticated → Cognito hosted UI (edge on) or `/login.html` (edge off) | server + present | Edge-on host must match `*.amazoncognito.com`; edge-off retains visible `#login-form`. |

## test_auth.py

| Contract | Status | Notes |
|---|---|---|
| Hosted-UI login returns directly to app | server | Edge Cognito cookies are accepted after JWT validation; `#login-form` never appears a second time. |
| `#error-msg` visible on bad password | present | Edge-off LoginPage keeps id + `.visible`-class error display. |
| Cognito edge cookies or `adt_session` after login | server | Cookie assertion follows `ADT_EDGE_AUTH`; both paths expose the same authenticated shell. |
| `#logout-btn` attached in the shell | present | AppShell header. |
| `/api/auth/me` username | server | Unchanged. |

## test_navigation.py

| Contract | Status | Notes |
|---|---|---|
| `#dashboard-btn` `#agents-btn` `#architecture-btn` `#onboard-btn` | present | AppShell nav, ids verbatim. |
| `#dashboard-view` `#agents-view` `#architecture-view` `#onboard-view` visible on click | present | The four `.view` containers render **eagerly**; click adds `.active` (verified: visible=True, active=True, `location.hash` updates to `#<view>`). Dashboard's body mounts eagerly too (see below); agents/architecture/onboard bodies lazy-mount on first activation — the test clicks first, so ordering is identical. |
| `#ai-briefing-card` visible on default view | present | DashboardView mounts **eagerly** (its skeleton is attached from first paint, like the original static `index.html` markup). *Superseded claim*: the audit previously relied on "the empty container has zero height, so `to_be_visible` cannot pass before the body mounts" — the local e2e triage (see below) disproved this empirically (`#dashboard-view` reported visible while empty), so the lazy mount was replaced by the eager mount. |
| `window.marked/DOMPurify/cytoscape/Popper/tippy` (test_vendored_libraries_loaded) | RETIRED 2026-09 | `globals-shim.ts` deleted (2026-09 architecture review): nothing in `src/` read the globals and the static imports put the 554 KB graph chunk on every entry's preload set. The e2e contract is now `test_graph_libraries_load_lazily_with_the_architecture_view`; `scripts/check-entry-preloads.mjs` gates the build. |
| No gateway-error text, no uncaught JS | present | Probe run clean (0 pageerrors) on index + admin with mocked APIs. |

## test_dashboard_layout.py

| Contract | Status | Notes |
|---|---|---|
| `#ops-summary-card` + `#ai-briefing-card`, parent 2-col grid, briefing track wider | present-after-activation | Probed computed style: `283.656px 567.344px` (1fr/2fr). |
| `.kpi-row` `#metrics-row` `#metrics-oee` attached | present-after-activation | Static skeleton in DashboardView JSX. |
| `#line-state-card` visible; `#ls-running/#ls-idle/#ls-down` attached | present-after-activation | |
| `#ls-systems-badge`, `#ls-sites` | present-after-activation | |
| `#live-feed-panel` visible, `#live-feed` attached | present-after-activation | |
| `#detail-modal-overlay` attached + `display:none` on load | present-after-activation | Probed hidden=True. |
| `#entity-sections` attached, `display:none`, 0 inline tables | present-after-activation | Probed hidden=True, tables=0. |
| `[id^="kpi-card-"]`, `.kpi-card.drillable`, `.kpi-card-hint` "click to drill in" | data-driven | Rendered by `views/dashboard/cards.ts` from SSE snapshot; literals confirmed in built bundle (`kpi-card-`, `drillable`, `click to drill in`). |
| `#detail-modal-title` `#detail-modal-body` `.detail-modal-close`, overlay `.open` class | present-after-activation | Skeleton ids probed; `.open` toggling in `sections.ts` (ported 1:1). |
| `?diag=1` → `body.diag`; `#line-state-card .diag-badge` revealed; `toggleDiag()` → False | present-after-activation | Probed end-to-end incl. the flip-off. See "Known deltas" §1. |
| `#kpi-card-machine-state` / `#kpi-card-quality-by-line` never KPI tiles | present | Bar-card exclusion ported (`cards.ts:91` — `group_by`/`viz_type==='bar'`/`by-dimension` routed to panels, `machine-state` → `loadLineState`). |
| `#ls-sites .line-state-site-group` / `.line-state-line-chip` | data-driven | Literals in built bundle. |
| `#quality-by-line-panel` + `.dim-bar-subtitle` with "pass rate" | data-driven | Panel id probed (attached, hidden-when-empty). Subtitle string byte-identical to static (`panels.ts:81` — "Pass rate = % of units passing quality checks."; test lowercases). |
| `.kpi-asof` position:absolute; `.kpi-target` not nested in `.kpi-value` | data-driven | Class literals in bundle; CSS verbatim (`dashboard.css` diff-identical). |
| `#kpi-card-oee` → `.oee-drill-table` with Measurement/Source header | data-driven | Literals in built bundle (`oee-drill-table`, `Measurement`, `Source`). |

## test_charts.py

| Contract | Status | Notes |
|---|---|---|
| `_CHART_PROBE_JS`: sandboxed `srcdoc` iframe loads `${origin}/js/vendor/chart.umd.min.js`, `Chart` global defined, chart draws | present | Probe read and executed verbatim against `vite preview`: `{"loaded": true, "drew": true}`. Works because `chart.umd.min.js` is a **plain asset**: the build copies the pinned npm Chart.js (`node_modules/chart.js/dist/chart.umd.min.js`) into `dist/js/vendor/`, reachable by absolute URL from the opaque-origin iframe; bundled Chart.js would not be. The app's own chart iframes (`chat/charts.ts`, thread) use the same absolute-URL template, so real chart-blocks behave identically. |

## test_onboarding.py

| Contract | Status | Notes |
|---|---|---|
| `#onboard-btn` → `#catalog-list` visible | present-after-activation | Probed (mocked catalog). |
| `#card-<sys>`, `#btn-<sys>` | present-after-activation | Probed with `cmms` (registered) + `erp` (onboarded): both cards render, `#btn-cmms` present, `#card-erp` has class `onboarded` and no `#btn-erp`. |
| card `onboarded` class after pipeline | data-driven (probed statically) | `catalog.ts` renders `sys-card onboarded` for `status==='onboarded'` — verified live with mock. |
| `.ob-confirm-btn` (incl. disabled-until-mapped) | data-driven | `wizard.ts:382,551` — "Confirm Schema →" enabled, "Confirm Mappings →" rendered `disabled`; literals in built bundle. |
| "Skip All Unmapped" button | data-driven | Literal in built bundle. |
| `#card-<sys> .sys-card-disconnect-btn` | present-after-activation | Probed on the mocked onboarded card. |
| `POST /api/onboard/reset/<sys>` | server | Unchanged. |

## test_admin.py

| Contract | Status | Notes |
|---|---|---|
| `/admin.html` entry | present | MPA entry in dist. |
| `nav.tab-bar` visible; `#access-denied` hidden for admin | present | Probed (mock `is_admin:true`). |
| `button[data-tab=tab-agents\|tab-settings\|tab-demoscript\|tab-thread]` + `#<tab>` activates | present | All four probed clickable → panel visible. **Note:** `tab-demoscript` is hidden when `/api/admin/settings.demo_enabled` is false — identical to static `admin.js applyDemoTabVisibility()`; the e2e test passes only in demo-enabled envs, exactly as today. |
| `#thread-loading` count → 0 after Thread tab load | present-after-activation | Probed: count=0 after activation (ThreadPanel resolves and removes it). |
| `#thread-empty` onboard link href contains `#onboard` | present | ThreadPanel admin-safe default → `/#onboard` (3.1 notes); empty-state hidden in probe env, link asserted in source. |
| Quick-links `#link-highbyte` `#link-erp-dashboard` `#link-console-start` `#link-i3x-highbyte` `#link-i3x-sitewise` hidden when unconfigured | present | Probed: all five in DOM, `href='#'`, hidden=True. |
| No uncaught JS per tab | present | 0 pageerrors across all tab activations in probe. |

## Known deltas checked (from the plan + Wave-3 integration report)

1. **`?diag=1` deferred registration** — **RESOLVED by the eager dashboard
   mount** (local e2e triage, below). `initDiagMode` now runs at first paint on
   every page load (DashboardView mounts unconditionally inside
   `#dashboard-view`), matching the original rAF boot. The earlier audit
   rationale ("empty container has zero height so `to_be_visible` cannot pass
   before mount") was disproved against a live deployment: `#dashboard-view`
   reported Playwright-visible while empty, and `test_diagnostic_mode_reveals_traces`
   failed. The Wave-5 residual (non-dashboard landing hash deferring `body.diag`)
   is gone for the same reason. `window.toggleDiag` still registers at module
   import time (`diag.ts` side effect).
2. **`test_vendored_libraries_loaded`** — was satisfied by `globals-shim.ts`
   (probed, all five globals defined). RETIRED 2026-09 together with the shim;
   replaced by `test_graph_libraries_load_lazily_with_the_architecture_view`.
3. **`_CHART_PROBE_JS`** — probe executed verbatim: loaded+drew true (see
   test_charts above).
4. **Lazy-view semantics** — the shell renders all four `.view` containers
   eagerly; every e2e assertion on view internals either performs the
   activation itself (`page.click("#onboard-btn")`, admin tab clicks) or
   auto-waits (`expect(...).to_be_visible(timeout=...)`) across the
   first-activation mount. No test asserts non-active-view internals eagerly →
   **no SPA skeleton changes needed, no test rewrites needed.**
5. **`demo.ts` toggles `#chat-sidebar` 'collapsed' class directly** (Wave-3
   flag) — no e2e test asserts sidebar collapse state, so no contract impact;
   remains a UI-polish note for Wave 5.
6. **Agents view no longer refetches per re-activation** (Wave-3 flag) —
   `test_main_views_load` only asserts container visibility; no contract
   impact.

## Unit-test repoint (part b of this task)

The legacy `static/` UI was removed 2026-07-31; the four source-inspection
unit tests (`tests/unit/{test_analysis_util,test_architecture_util,
test_answer_assembly,test_agent_analysis_panel}.py`) target the frontend
sources unconditionally. The dual-target `ADT_FRONTEND_SRC` switch that
staged the cutover no longer exists (see git history for the migration
mechanics).

## Method / reproduction

```bash
cd agentic_digital_thread/frontend && npm run build && npx vite preview --port 4199
# python-playwright probe with /api mocks (auth/me admin, branding, onboard
# status+catalog, demo enabled, admin settings demo_enabled) — asserts every
# table row above; chart probe copied verbatim from test_charts.py.
# Unit lane:
PYTHONPATH=. python -m pytest tests/unit -q
```

Full-suite e2e verification against a deployed environment (real data, real
auth, real SSE) remains the Wave-5 acceptance gate.

## Local e2e triage (2026-07-13)

Full `tests/e2e` suite run against the SPA dev rig (`http://localhost:5173`,
vite proxying `/api` + `/i3x-explorer` + `/health` to a live deployed backend)
with each failure comparatively re-run against the **original frontend served
by the same backend** (`ADT_BASE_URL=https://<cloudfront>`). Same backend, same
data ⇒ any SPA-only failure is a port regression; a failure on both is
environment/data-gated.

Initial SPA run: 23 passed, 9 failed, 1 error, 2 skipped.
After fixes: **28 passed, 3 failed (all also fail on baseline), 4 skipped.**

| Test | SPA (before) | Baseline | Classification | Action |
|---|---|---|---|---|
| `test_health.py::test_health_ok` | FAIL (JSONDecodeError — vite answered `/health` with index.html) | PASS | **Rig limitation** — `/health` was not in the dev-proxy list | Added `/health` to `vite.config.ts` proxy (dev-only). SPA now PASSES. |
| `test_admin.py::…[tab-settings]` | ERROR (fixture `/api/auth/me` got an upstream **502 Bad Gateway**) | PASS | **Transient backend flake** (`/api` is proxied; one-off 502 through CloudFront) | None. SPA re-run PASSES. |
| `test_admin.py::…[tab-demoscript]` | FAIL (click timeout — tab hidden) | FAIL (identical) | **Env-gated** — `/api/demo/status` → `{"enabled": false}`; `applyDemoTabVisibility` hides the tab in both frontends | None (documented admin-tab caveat above). |
| `test_dashboard_layout.py::test_dashboard_shell_layout` | FAIL (`#ops-summary-card` not found) | PASS | **Port regression** — DashboardView mounted only after the async `/api/onboard/status` fetch; `#dashboard-view` (shell container) is Playwright-visible while still empty, so "container visible ⇒ skeleton attached" (true in the original: all markup static in `index.html:63-…`) was broken | Eager mount of `<DashboardView/>` in AppShell. SPA now PASSES. |
| `test_dashboard_layout.py::test_entity_tables_do_not_leak_inline` | FAIL (`#entity-sections` not attached) | PASS | Same root cause | Same fix. SPA now PASSES. |
| `test_dashboard_layout.py::test_diagnostic_mode_reveals_traces` | FAIL (`body.diag` missing on `?diag=1`) | PASS (flaky once on a transient login timeout; passed 2/3) | Same root cause — `initDiagMode` ran on DashboardView mount, deferred behind the status fetch; original runs it in the boot rAF (`dashboard.js:9-13`) | Same fix (mount effect now runs at first paint). SPA now PASSES. |
| `test_dashboard_layout.py::test_line_state_per_site_groups` | FAIL (`#ls-systems-badge` not attached) | SKIP (badge attached; skips on "machine-state unmapped in this env") | Same root cause (element missing vs attached-but-dataless) | Same fix. SPA now SKIPS identically to baseline. |
| `test_dashboard_layout.py::test_quality_panel_has_context_subtitle` | FAIL (`#quality-by-line-panel` no element) | SKIP (panel attached, hidden — no data) | Same root cause | Same fix. SPA now SKIPS identically to baseline. |
| `test_dashboard_layout.py::test_click_to_drill_opens_modal` | FAIL (hint text `click for breakdown` ≠ `click to drill in`) | FAIL (identical) | **Env-gated** — the only drillable card in this env is the OEE gauge, whose hint is legitimately `click for breakdown` (`dashboard.js` OEE card branch); the test assumes a table-drillable card exists | None. Fails identically on both. |
| `test_dashboard_layout.py::test_oee_drill_shows_labeled_breakdown` | SKIP before fix (dashboard never mounted) → FAIL after fix (`.oee-drill-table` count 0) | FAIL (identical; manual probe: both frontends open the modal with `No OEE components resolved.`) | **Env-gated** — no OEE component data resolves in this env | None. Fails identically on both. |
| `test_onboarding.py::test_onboard_cmms_end_to_end_seeds_graph` | FAIL (`#card-cmms` not visible) | FAIL (identical) | **Env-gated**: `/api/onboard/catalog` contains only `lake-athena`/`telem-sitewise` (cross-account environment, no seed data); the test fails at card-visibility **before any onboarding write occurs** (no residue left in either run) | None. |

Root-cause note (the one real port regression, one fix): the original ships the
entire dashboard skeleton statically in `static/index.html` (`#dashboard-view`
is `view active` at parse time, `#ops-summary-card`/`#entity-sections`/
`#ls-systems-badge`/`#quality-by-line-panel` always attached), and
`architecture.js`'s `DOMContentLoaded` calls `switchView('dashboard')`
(loadSystems/SSE) plus `_startBriefingLoop()` before the fresh-install status
check resolves, while `dashboard.js`'s rAF boot runs `initDiagMode()`
unconditionally. The SPA gated the whole `<DashboardView/>` mount on the
`/api/onboard/status` response, so for the fetch's duration the visible
container was empty and `initDiagMode` had not run. Fix
(`src/pages/app/AppShell.tsx`): render `<DashboardView active={view === 'dashboard'}/>`
unconditionally — skeleton attached from first paint, boot-time
`initDemoGate`/`initDiagMode`/briefing-loop, SSE still gated on the view being
active, fresh-install still lands on Onboard via the status check.

Remaining SPA failures (3) are byte-for-byte the baseline failures — env-gated,
no port deltas outstanding.

### Update (2026-07-14, data-mode-aware suite)

The merged base branch made `tests/e2e` data-mode-aware (conftest login retry;
drill/diag race fixes; base-env skips for bundled-demo assumptions). Full-suite
re-run against the SPA dev rig after porting the merged frontend deltas
(wizard step-error/Retry, SL review-reason banner + crosswalk type, G3
session-systems prefill):

**29 passed, 6 skipped, 0 failed** — the previous baseline's 3 env-gated
FAILURES are now SKIPS by design of the updated suite:

| Previously (old suite) | Now (data-mode-aware suite) |
|---|---|
| `test_click_to_drill_opens_modal` FAIL (gauge-only env) | SKIP — "no KPI cards rendered / no table-drillable card" |
| `test_oee_drill_shows_labeled_breakdown` FAIL (no OEE data) | SKIP — honest `.detail-modal-empty` note detected |
| `test_onboard_cmms_end_to_end_seeds_graph` FAIL (no bundled demo systems) | SKIP — "'cmms' not in catalog (base env)" |
| `test_admin…[tab-demoscript]` FAIL (demo disabled) | SKIP — "admin tab not available in this environment" |

One transient during the full run: `test_quality_panel_has_context_subtitle`
errored at the login fixture (`wait_for_url` 30s timeout post-submit — the
suite's new retry covers the `#email` fill stall, not post-submit navigation);
solo re-run SKIPS identically to baseline ("panel hidden, no data"). No SPA
regressions; the suite's updated selectors (`.kpi-card-hint` filter,
`body.diag` poll, `.detail-modal-empty`, `#btn-<id>` wait) all match the SPA
DOM. Note: the deployed backend does NOT run the merged backend code — the
merged entity-rescan endpoint (ADR-0040) has no legacy-frontend UI delta, so
there was nothing to render-safety check on the SPA side.

## JSX migration end-state (2026-07-14)

Wave-6 sweep of `frontend/src` (excluding `src/test/` and the verbatim-contract
`lib/{analysis-util,architecture-util}.js`) for every
`innerHTML` / `dangerouslySetInnerHTML` / `insertAdjacentHTML` / `outerHTML` /
`onclick=` occurrence, classified against the migration plan's target
end-state. Waves: W1.1/W1.2 (`Md`/`SanitizedHtml`/store primitives +
turn model), W2.1, W2.2, W2.3, W2.4, W3.1, W5.1, W5.2, W6.

### Allowed-sink inventory

Class legend: **(a)** sanitizer-output sink inside `<Md/>`/`<SanitizedHtml/>`
(or funneling into them) · **(b)** tippy/cytoscape-owned popover container ·
**(c)** sandboxed `srcdoc` chart-iframe template.

| File:line | Sink | Class | Notes |
|---|---|---|---|
| `shared/ui/Md.tsx:62,64` | `dangerouslySetInnerHTML` | a | THE markdown sink — `mdToSafeHtml` (marked + DOMPurify) output only. |
| `shared/ui/SanitizedHtml.tsx:40` | `dangerouslySetInnerHTML` | a | Pre-sanitized HTML strings only (documented prop contract). |
| `chat/analysis-panel.ts:147` | `d.innerHTML = sanitize(part)` | a | Panel body parts wrapped in `DOMPurify.sanitize` (W6); the assembled `<details>` renders via `<SanitizedHtml/>` in `AssistantTurn`. Python textual contract reads this file — churn minimized. |
| `lib/arch-store.ts:755` | `div.innerHTML = html` | b | tippy-owned popover content element (library-owned DOM); the inline `onclick` inside it became `data-nav` + programmatic listener in W2.3. |
| `chat/charts.ts:56` | `iframe.srcdoc = html` | c | Sandboxed chart-iframe string template by design (absolute-URL vendor asset — e2e chart probe depends on it). |

Non-sink reads (serialization, not injection): `chat/AssistantTurn.tsx:127`
(`details.outerHTML` → `<SanitizedHtml/>`), `shared/thread/data-modal.tsx:210`
(`#modal-messages` innerHTML read for Word export),
`chat/charts.ts:200` (`outerHTML` in a `console.warn` diagnostic).

Inline `onclick="..."` handler strings: **zero** (remaining `onclick=` grep
hits are comments describing the removed pattern).

### Violations found and fixed in W6

| Was | Fix |
|---|---|
| `views/architecture/neptune-graph.ts` tap handler — `selPanel.innerHTML` with **unescaped** graph-sourced label/property interpolation (`#kg-selection-info`, the W2.3 known leftover) | `kgSelectionStore` + `<KgSelectionInfo/>` (`KgSelectionInfo.tsx`) — all graph data renders as JSX-escaped text; markup byte-comparable. |
| `views/architecture/ArchitectureView.tsx` — static-string `dangerouslySetInnerHTML` placeholder for `#kg-selection-info` | Placeholder is now JSX inside `<KgSelectionInfo/>`. |
| `chat/analysis-panel.ts:147` — `d.innerHTML = part` unwrapped | Wrapped in `sanitize()` (DOMPurify) — sink is now sanitizer output. |
| `window.switchArchTab` consumed via `?.` (thread-clicks.ts, dashboard demo.ts, onboard ResolveExtras.tsx) but never registered — those navigations silently no-oped | Registered from `ArchitectureView` mount effect (documented shim). |

### Fixes (2026-07-14)

| Problem | Fix |
|---|---|
| `window.handleArchEvent` / `window.resetArchitectureState` never registered — dashboard demo mode's guarded window calls silently no-oped, so the demo's investigation graph received nothing and never reset. Also `switchArchTab('investigation')` no-oped on the FIRST visit (views lazy-mount; the global registers in ArchitectureView's mount effect, after demo's synchronous call). | `demo.ts` imports `handleArchEvent`/`resetArchitectureState` directly from `chat/arch-bridge` (same bundle); the dead optional window declarations were removed from `chat/globals.d.ts`. New `views/architecture/arch-tab-bridge.ts` (`requestArchTab`/`consumePendingArchTab`) stashes a pre-mount tab request; ArchitectureView drains it at the end of its mount effect. |
| `window.cy` never published (the pre-SPA code assigned it as an implicit global) — citation-badge / Sources-row clicks switched view but never resize/fit/select/openDataModal; ChatSidebar's `cy?.resize()` no-oped. | `initArchitecture` publishes `window.cy`; `destroyInvGraph` clears it to `null`. |
| thread-store pills/traces Map-keyed `tool_id \|\| tool` — duplicate calls to the same tool WITHOUT a tool_id overwrote the earlier pill/trace (legacy appended DOM per call): pill count diverged from `toolCallCount`, first trace content lost. | No-tool_id keys uniquified as `` `${tool}#${toolCallCount}` `` at `tool_start`; a no-tool_id `tool_result` resolves to the LAST trace recorded for that tool. Unit-tested (`thread-store.test.ts`). |
| `data-modal.tsx` Word-export title interpolated unescaped into the downloaded-.doc HTML. | Not changed: legacy-identical, download-only surface. |
| `chat/charts.ts` srcdoc `<script>` JSON breakable by `</script>` in labels. | Not changed: bounded by sandbox + opaque origin, legacy-identical. |
| AgentsFleet hour-filter response race (legacy parity); stale `./html.ts` comment. | Not changed. |

### Remaining window-global shims (documented registry)

Each is registered by its owning module and consumed cross-module (or by e2e
probes / md-rendered content):

| Shim | Registered by |
|---|---|
| `switchView`, `openDataModal`, `closeDataModal`, `showToast`, `saveChatModalAsWord`, `refreshSuggestions` | `pages/app/AppShell.tsx` |
| `askQuestion`, `sendMessage`, `stopStreaming`, `startNewSession`, `openChatModal`, `closeChatModal`, `mdToSafeHtml`, `parseAnswerBlocks`, `getSystemLabel`, `getToolLabel`, `refreshMemory`, `rehydrateSession`, `toggleChatSidebar`, `toggleMemorySidebar` | `chat/ChatSidebar.tsx` |
| `toggleDiag` (e2e probe) | `views/dashboard/diag.ts` (module-import side effect) |
| `switchArchTab`, `filterToQuestion` | `views/architecture/ArchitectureView.tsx` (pre-mount `switchArchTab` requests go through `views/architecture/arch-tab-bridge.ts` `requestArchTab`) |
| `cy` (investigation-graph cytoscape instance — thread-clicks `focusArchNode`, ChatSidebar resize) | `views/architecture/inv-graph.ts` (`initArchitecture` publishes / `destroyInvGraph` clears) |
| `acceptMapping`, `rejectMapping`, `editMapping`, `pruneGuidanceNote`, `clearSystemGuidance` | `views/architecture/proposals.ts` (`registerProposalGlobals`) |
| `ob*` family (`obLoadCatalog`, `toggleConn`, `showSystemDetail`, `obRestoreFlow`, `showConnectNewForm`, `obJoinCandidateDecide`, `obGuardStart`, `obReset`, `obResetAll`, `obResetAndConnect`, `obDisconnectSystem`, `obAskAbout`, `obAttachFile`, `obSkipAllUnmapped`, `checkMappingState`, `openOnboardingVideo`, `closeOnboardingVideo`) | `views/onboard/globals.ts` (`registerOnboardGlobals`) |
| `remapSystem`, `saveRemappings` | RETIRED 2026-10 with the Re-map screen: its Save was a full, destructive re-provision of an onboarded system. `onboard-remap-removed.test.tsx` pins that the mounted Onboard view installs neither. |
| `marked`, `DOMPurify`, `cytoscape`, `Popper`, `tippy` (vendored-library e2e contract) | RETIRED 2026-09 — `globals-shim.ts` deleted; `session-bootstrap.ts` keeps only the eager session-id mint |

### DOM contracts preserved

Every id/class/data-attribute contract in this document (the Wave-4.2 tables
above) was preserved through the JSX migration — verified per-wave by the
harness (tsc + vitest + build + both python unit lanes) and by the wave-level
e2e runs; the full-suite e2e re-run for W6 matches the "Local e2e triage"
baseline pattern (3 env-gated failures, zero new). Waves: W2.1
(dashboard), W2.3 (architecture), W2.4, W3.1 (chat core), W5.1 (onboard),
W5.2 (proposals/agents/login).

---

## Zero-innerHTML end-state (2026-07-14)

Supersedes the "Allowed-sink inventory" above: **every** HTML-string sink was
eliminated. `grep -rn "innerHTML|dangerouslySetInnerHTML|insertAdjacentHTML|outerHTML"
src` (excluding `src/test/`) returns **zero code occurrences** — every
remaining hit is a comment describing the removed pattern.

Markdown renders as **React elements via react-markdown + remark-gfm**
(`react-markdown@10.1.0` / `remark-gfm@4.0.1`, exact pins). No sanitizer is
needed on the render path: react-markdown never parses raw HTML (no
rehype-raw), so hostile markup arrives as inert text.

### Per-sink conversion

| Was (sink) | Now |
|---|---|
| `shared/ui/Md.tsx:62,64` `dangerouslySetInnerHTML` (marked + DOMPurify) | `<ReactMarkdown remarkPlugins={[remarkGfm]}>` with an `a` component override; `inline` unwraps `p` into a `<span>`; `bare` renders element chunks for composition. Memoized on text. |
| `shared/ui/SanitizedHtml.tsx:40` | **DELETED** — no consumer remains. |
| Answer pipeline (mdParse → sanitize → `[N]`-badge string replace → DOM chart reinjection → DOM collapse post-processor) | `<AnswerBody/>`: string-level `splitChartBlocks` → `injectCitationLinks` (`[N]` → `[N](<#cite-TARGET> "label")` markdown links; the `a` override renders `.cite-badge` + `data-cite-target` — the delegated thread-click contract is unchanged) → `buildAnswerModel` section split at the SAME h2/length gates as `collapseResponseSections` (same `.response-section` classes and default-open rules; user toggles persist as React state). `collapse-sections.ts` **DELETED**. |
| Tool traces (`sanitize(renderToolInputBlock(...) + trace-result html)`) | `<ToolInputBlock/>` (JSX from `formatToolInput` data) + `.trace-result` `<Md/>`. `renderToolInputBlock` removed; `ToolTrace.inputHtml` dropped from the model (data-only `toolInput`). |
| Error paragraph (transport pre-escaped `<p style="color: var(--red);">` html) | Transport stores PLAIN message text; `<AssistantTurn/>` renders the red paragraph as JSX. |
| `chat/analysis-panel.ts:147` `d.innerHTML = sanitize(part)` | `analysis-panel.tsx` — `<AgentAnalysisPanel/>` renders the whole panel from structured data (`buildAgentAnalysisPanelData`, assembled by the thread store on done); LLM fields are JSX text. Python textual contract adapted (frontend branch only). |
| `lib/arch-store.ts:755` `div.innerHTML` (Query Intelligence box) | `createElement`/`textContent` construction; the `data-nav="self-learning"` listener registration is unchanged. |
| Data-modal raw path (`openDataModal(..., raw)` → `<SanitizedHtml/>`) | Removed — it had NO remaining callers; content is markdown only, chart blocks split to `<ChartIframe/>`. |
| Demo overlay (`demoRenderText` html string + DOMPurify → `<SanitizedHtml/>`) | Store carries raw page text; `<DemoPageText/>` renders the same markdown-ish tokens (headings/bold/em/code/br) as JSX. |
| Word export `#modal-messages.innerHTML` READ + unescaped title | `XMLSerializer` child serialization; `escHtml(title)` in `<title>`/`<h1>`. The export blob is a DOWNLOAD payload, not a DOM sink (`mdToSafeHtml` survives only here + the `window.mdToSafeHtml` e2e contract; header note in `chat/md.ts`). |
| `chat/charts.ts:200` `outerHTML` in a console.warn | Gone with `renderChartBlocks` (dead after string-level splitting — extraction always caught every `<chart-block>` before it). |

Sandboxed `srcdoc` chart iframes remain by design (`<ChartIframe/>`,
opaque-origin `sandbox="allow-scripts"`, absolute vendor URL — the e2e chart
probe contract).

### react-markdown fixes (2026-07-14)

The react-markdown conversion changed how one tag degrades.
`<chart-block .../>` is the single model-emitted pseudo-HTML tag; the parser
swap changed its degradation from "silently stripped by DOMPurify" to
"visible escaped text" on two paths — both fixed.

| Problem | Action |
|---|---|
| `data-modal.tsx` follow-up answers rendered `<chart-block/>` as permanently visible escaped text (old path stripped it invisibly). | Done answers route through `ModalMdContent` (`splitChartBlocks` + sandboxed `<ChartIframe preserveExisting={false}/>` — the chart now RENDERS, an improvement over old); the streaming preview renders `stripChartBlocksForStream(answerMd)` (complete tags AND a partial trailing tag hidden). Unit-tested (`data-modal.test.tsx`). |
| `AssistantTurn.tsx` `.stream-md` runs showed a completed chart-tag's escaped source mid-stream until `done`. | `stripChartBlocks` on completed runs, `stripChartBlocksForStream` on the live run (new helper in `charts.ts` also drops an un-terminated trailing `<chart-block` tail). |
| `patchFollowup` fired a store update + full react-markdown re-parse PER TOKEN (~15x marked's cost, O(n²) over the answer). | Token patches rAF-coalesced in `sendModalFollowup` (one store patch per animation frame; done/error cancel + supersede any pending). `rafPair` extracted to `lib/raf.ts`, shared with `thread-store.ts`. Coalescing + mid-stream state unit-tested. |
| `citations.ts` replaced `[N]` inside INLINE code spans, leaving raw link markup visible inside `<code>`. | `mapOutsideCodeSpans` (CommonMark equal-length backtick-run spans, per line) skips code-span ranges before the replace. Unit-tested incl. multi-backtick spans. |
| `charts.ts` srcdoc interpolated `JSON.stringify(chartConfig)` into `<script>` unescaped — a model-authored `</script>` label could close the script context (contained by the opaque-origin sandbox). | `<` escaped as `\u003c` in the config JSON. Unit-tested. |
| `md.ts` `sanitize()`/`mdParse()` had zero consumers post-migration. | Removed (dead code); `mdToSafeHtml` + `escHtml` remain for the Word-export payload, window contract, and python textual contract. |
| `TraceEntries.tsx` stale comment claimed `<Md/>` wraps `mdToSafeHtml`. | Comment corrected (react-markdown element rendering; raw HTML inert). |
| `answer-sections.ts` `H2_RE` missed 1–3-space-indented ATX h2s (gate/split disagreed with remark's rendering) and kept ATX closing sequences in summaries (`## Foo ##` → "Foo ##"). | Tightened to `/^ {0,3}##\s+(.*?)(?:\s+#+)?\s*$/` — the closing run must be whitespace-separated (CommonMark), so `## Line #4` / `## Bay C#` keep their `#`. Unit-tested. |

All eight were fixed; none was left as a documented deviation.

### Parser-swap behavior differences (marked → react-markdown)- **Raw HTML in model output renders as inert literal TEXT** (escaped), where
  marked + DOMPurify rendered benign tags (`<b>`, `<br>`) as markup and
  silently stripped hostile ones. A deliberate security improvement; model
  answers that relied on raw HTML tags now show them as text.
- `[N]` inside fenced code — and inside INLINE code
  spans — stays literal (the old post-HTML replace injected a live anchor into
  code content); `[N](` existing markdown links are left alone (negative
  lookahead).
- Section split keys on ATX `## ` headings outside fences (the DOM version
  keyed on rendered `<h2>`, which also caught setext headings — not observed
  in real answers).
- `javascript:` link destinations are neutralized by react-markdown's default
  url transform.

### Perf

`<Md/>` is `React.memo`ized and the answer model is memoized on `answerMd` —
the final answer parses once on done; during token streaming only the live
`.stream-md` run re-parses per rAF frame (completed runs and earlier sections
are memo-stable). react-markdown's parse is heavier than marked's; verified
smooth against the dev rig.
