<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# Browser test suite

Playwright (`pytest-playwright`) smoke/regression suite that drives a **running**
deployment in a real browser. The same tests run against a CloudFront URL from a
`cdk deploy` or a local `uvicorn` instance — everything is configured through environment variables.

## What it covers

| File | Checks |
|---|---|
| `test_health.py` | Public `/health` and `/api/branding`; protected `/api/version` returns 401 unauthenticated; `/` reaches the Cognito hosted UI when edge auth is on and `/login.html` when off. No credentials needed. |
| `test_auth.py` | The shared login helper completes hosted-UI sign-in without a second `#login-form` in edge-on mode; edge-off keeps the inline bad-password and `adt_session` checks; `/api/auth/me` reflects the user. |
| `test_navigation.py` | Dashboard renders with no 504 / Bad Gateway and no uncaught JS; each primary nav view (Dashboard, Architecture, Threads, Onboard) activates; full round-trip raises no JS exceptions; graph libraries load lazily with the Architecture view. |
| `test_admin.py` | Admin page (`/admin.html`) regressions: Thread tab activates without JS errors; Evals tab renders history, run-ALL-only control, triage-only review queue, findings ledger, and measured-or-absent audit status (never triggers a billable run). |
| `test_adr0063_closure.py` | Deployed ADR-0063 closure: trust chips, guidance-budget reconciliation, and placement controls are read-only; promotion is reachable but not invoked. Mutation checks are opt-in only (see below). |
| `test_charts.py` | Chart.js loads and draws inside the sandboxed `<chart-block>` iframe (opaque-origin `srcdoc` mechanism reproduced deterministically). |
| `test_conversational_onboarding.py` | The FULL ADR-0068 conversational onboarding round trip (see below). |
| `test_dashboard_layout.py` | Reference dashboard shell layout, no inline entity-table leak, click-to-drill modal, diagnostic-mode traces, honest `unavailable` KPI rendering, per-site Line State groups. |
| `test_guided_context.py` | Admin → Context edits the live per-system business context; the standalone guided-interview panel is retired while `/api/onboard/guided/state` still serves. |
| `test_onboarding.py` | Deterministic catalog "Onboard" flow for `cmms` (discover → map → resolve → provision → verify through the three confirm checkpoints) leaves the system onboarded with `MAPS_TO` edges. |
| `test_product_hardening.py` | The steward digest's cost trend renders when cost is measured (ADR-0053), behind the "Technical details" expander. |
| `test_provenance.py` | Three-tier provenance UX: Sources chips and inline cite badges, per-claim evidence card (no raw SQL in Tier 2), and the Technical-detail expander—tolerant of honest-sparse records. |
| `test_steward_review.py` | Steward-transparency surfaces (ADR-0052): digest card, consequence-ordered proposals with badges, "Try it" on a pending card completes with an honest diff. |

## Configuration

| Environment variable | Default | Notes |
|---|---|---|
| `ADT_BASE_URL` | `http://localhost:8000` | Deployment base URL, no trailing slash. |
| `ADT_USERNAME` | `ADT` | Demo Cognito username. |
| `ADT_PASSWORD` | _(unset)_ | Required for auth tests. When unset, those tests **skip** (public checks still run) — so no secret is ever committed. |
| `ADT_EDGE_AUTH` | `on` | `on` drives the Cognito hosted UI; `off` for `--edge-auth off`, workshop, or local app-form runs; `oidc` for `--oidc-only on` deployments (seeds the browser with the IdP session from `ADT_OIDC_COOKIE_FILE` — a Netscape cookie file from your IdP's CLI sign-in; required in this mode, the suite skips without it). Any other value fails configuration. |
| `ADT_E2E_ADR0063_MUTATIONS` | _(unset)_ | Set to `1` only to run the ADR-0063 mutation checks. The default closure suite never invokes a mutation. |
| `ADT_E2E_SYNTHETIC_SYSTEM_ID` | _(unset)_ | Required with the mutation flag. Must name one explicitly designated, onboarded synthetic system; no system id is embedded in the tests. |
| `ADT_PROTECTED_URL_MARKERS` | _(unset)_ | Comma-separated URL markers that refuse mutation checks. URLs naming the protected deployment tier are refused independently. |
| `ADT_E2E_ADR0063_ALLOW_PROTECTED` | _(unset)_ | Separate protected-environment authorization. Set to `1` only after explicit approval; the mutation flag alone never authorizes protected-tier URLs. |

Pull the deployed values:

```bash
export ADT_BASE_URL=$(aws cloudformation describe-stacks --stack-name ADTComputeStack \
  --profile <profile> --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ADTCloudFrontUrl'].OutputValue" --output text)

export ADT_PASSWORD=$(aws secretsmanager get-secret-value --secret-id adt-demo-password \
  --profile <profile> --region us-east-1 \
  --query SecretString --output text)
```

## Install and run the browser tests

1. From `agentic_digital_thread/`, install the test dependencies:

   ```bash
   pip install -e '.[test]'
   ```

2. Install Chromium once:

   ```bash
   playwright install chromium
   ```

3. Run the required test mode:

   ```bash
   pytest tests/e2e                 # headless
   pytest tests/e2e --headed        # watch it run
   pytest tests/e2e -m e2e -v       # verbose
   ```

Without `ADT_PASSWORD`, only the public checks in `test_health.py` execute; the
auth and navigation tests report as skipped. The default `ADT_EDGE_AUTH=on`
matches normal deployments. Export `ADT_EDGE_AUTH=off` when targeting a
workshop, an explicit `--edge-auth off` deployment, or local `uvicorn`.

## ADR-0063 closure runbook

`test_adr0063_closure.py` is **read-only by default**. It verifies deployed trust
chips, server-family batch headers, the collapsed precision block, guidance
budget/reconciliation states, promotion and scoped-placement controls, and a
query fingerprint on an existing admin provenance handle. It never asks a new
chat question to manufacture provenance and never clicks a mutating control.
Data-dependent assertions skip honestly when the live environment has no
matching pending row, family, question, precision sample, or existing handle;
record those skips as live-only verification blockers rather than creating data.

```bash
# Safe default: all mutation tests skip.
pytest tests/e2e/test_adr0063_closure.py -m e2e -v
```

The opt-in checks are for a disposable environment with one explicitly
designated synthetic system. They may only (1) file an answer-requirement as a
**PENDING** proposal and (2) run the selected-system context-placement check.
They never accept, batch-adjudicate, wildcard-repair, or edit business context.
The promotion row and any run-created pending placement rows are cleaned up by
explicit per-id **Reject** calls through the normal steward lifecycle; there is
no direct-delete cleanup.

```bash
export ADT_E2E_ADR0063_MUTATIONS=1
export ADT_E2E_SYNTHETIC_SYSTEM_ID=<designated-synthetic-system-id>
pytest tests/e2e/test_adr0063_closure.py -m e2e -v
```

The harness refuses a base URL naming the protected deployment tier or any
case-insensitive marker configured in `ADT_PROTECTED_URL_MARKERS`. Running there
requires a **separate** explicit authorization in addition to the mutation flag:

```bash
export ADT_PROTECTED_URL_MARKERS=<protected-marker-1>,<protected-marker-2>
export ADT_E2E_ADR0063_ALLOW_PROTECTED=1  # only after explicit authorization
```

A scoped placement run can also supersede an already-pending generated
placement finding when the deterministic evidence no longer exists; use only a
designated disposable system. The harness cleans up only rows whose ids were
absent before its run, remain pending, carry `source=context-placement`, and
name that selected system. It never selects cleanup rows by wildcard or family.

## Conversational onboarding journey

`test_conversational_onboarding.py` drives the FULL ADR-0068 conversational
journey against a deployed environment: welcome chat asks the onboarding agent
to run `discover` + `assess_vocabulary_fit` for one onboarded system
(`ADT_ONBOARD_SYSTEM`, default `erp`), waits agent-turn-scale (up to 240 s) for
the `vocab_fit` checkpoint card, asserts the pre-map fit banner reports a real
integer percentage, then works the inline Context Interview card: answer ONE
question (asserting the honest outcome line and advancement), skip the rest
with "I don't know", and — when a draft accumulated — save it and assert the
"Context saved" confirm line (which rolls the system agent, ~2 min). The
target system's business context is snapshotted (`GET
/api/admin/business-context/{id}`) before the run and restored (PUT) after, so
the environment is left as found; the test never resets a system. Because the
question set is computed live and the answered-topics store is in-memory on
the app process, a repeat run against the same system can honestly report
zero remaining questions — the test then SKIPS naming the observed count
(re-target another onboarded system via `ADT_ONBOARD_SYSTEM`, or wait for an
app restart). Measured runtime against the demo deployment: ~25 s when the
onboarding session already exists, ~90 s for a fresh discover + confirm.
