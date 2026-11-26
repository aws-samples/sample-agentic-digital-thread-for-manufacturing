<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0070: Cognito Authorization@Edge is the default browser entry

## Status

Accepted — 2026-09-16. Verified against a live deployment on 2026-09-16.
Related: [ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (user
identity after application sign-in) and
[ADR-0056](0056-update-agent-runtime-full-replacement-protocol-roundtrip.md)
(runtime authentication configuration preservation).

## Context

The ADT application already authenticated users with Amazon Cognito, but its
browser entry was an application-served `/login.html` form that proxied
credentials to Cognito through `POST /api/auth/login`. That put password
handling inside the application and made the app the only browser surface in
the deployment without an edge authentication boundary; the ERP dashboard and
the HighByte UI already sat behind Cognito Authorization@Edge.

Moving sign-in to the edge removes the application-served credential form
entirely: the first unauthenticated browser request is redirected to the
Cognito hosted UI before it reaches the origin. That is the same boundary the
other two distributions use, it is visible and testable from outside the
deployment, and it leaves `/api/*` on bearer authentication. A browser
redirect to the Cognito hosted UI therefore provides the required
externally visible Cognito signal.

The platform has three human-facing CloudFront distributions in its full
configuration: the ADT app, the simulator ERP dashboard, and the HighByte UI.
It also has API and machine paths whose clients cannot follow a browser OAuth
redirect. The authentication boundary therefore has to distinguish browser
entry from API and bearer-token traffic.

## Decision

1. **Cognito Authorization@Edge is the normal browser default.** The ADT app
   distribution deploys the AWS-published `cloudfront-authorization-at-edge` Serverless
   Application Repository application, pinned to version `2.3.2`. `CheckAuth`
   runs on viewer request, `HttpHeaders` runs on viewer response, and
   `/parseauth`, `/refreshauth`, and `/signout` carry the OAuth callbacks.
   Unauthenticated browser navigation reaches the Cognito hosted UI rather
   than `/login.html`.

   **Amendment (2026-11-05): bare auth-endpoint requests redirect to `/`.**
   The SAR handlers answer a `/parseauth`, `/refreshauth` or `/signout`
   request that is not part of a real flow with an HTTP 200 HTML error page
   ("Invalid query string...", "You are already signed out"), and the SAR has
   no setting to change that. A security scanner reported the bare
   `/parseauth` as an unauthenticated page. Every edge-auth distribution's
   web ACL now ends with `EdgeAuthBareCallbackRedirect`
   (`_edge_auth.py::bare_auth_callback_rule`, priority 4, after the three
   managed rule groups). It is a Block action with a custom 302
   `Location: /` response that carries the same security headers as the
   SAR's HttpHeaders function (with `X-Frame-Options: DENY`), so CheckAuth
   starts the normal sign-in. It matches only a bare request: one of the
   three exact paths with no query string and no (or an empty) Cookie
   header. Every real flow has one or the other. The Cognito callback
   carries `code`/`state` or `error`, CheckAuth's refresh redirect carries
   `requestedUri` and `nonce`, and sign-out carries the session cookies
   (decision 4). Anything else goes on to the unchanged SAR handler, which
   still does all the verification. The rule does not try to copy the SAR's
   checks, so a request with stray cookies or a malformed query still gets
   the SAR page. The Cookie header is inspected as a single header, which
   AWS WAF reads in full, so a large Cookie header is never taken for an
   absent one. We did not use a viewer-request CloudFront Function, because
   it would have meant moving the SAR handlers to origin request. CloudFront
   caches responses that Lambda@Edge generates at origin request, so the
   token-setting `/parseauth` response would then depend on the cache policy
   staying `CachingDisabled`; the Host header and execution location would
   also change. Edge auth off leaves the app web ACL unchanged. Pinned by
   `tests/test_edge_auth_bare_callback.py`.
2. **The switch is explicit, sticky, and deterministic.** CDK context
   `edge_auth` and `deploy.sh --edge-auth on|off` control the feature. Resolution
   precedence is explicit `-c edge_auth=...` over SSM
   `/adt/edge-auth-enabled` over the mode default. The default is on; under
   `--workshop` it defaults off so packaged workshop content can keep its form
   flow. An explicit context value or sticky SSM value still wins. Packaged
   Workshop Studio templates pin `edge_auth=false` because the SAR transform is
   invalid in Workshop Studio child templates.
3. **API boundaries do not redirect.** `/api/*` and `/health` bypass
   `CheckAuth`. Application middleware remains authoritative there: protected
   APIs return 401, `/health` remains public, and the CLI smoke, eval, and
   learning-audit lanes continue to authenticate with
   `POST /api/auth/login`. The HighByte `/mcp*`, `/i3x*`, `/i3X*`, and `/data/*`
   machine paths likewise bypass browser edge auth and retain bearer-token
   enforcement at the origin, except the REST Data Server's Swagger UI
   (`/data/doc` and `/data/doc/*`). The origin serves that HTML page without
   a token, so it has its own behaviors on the same origin with `CheckAuth`
   and `HttpHeaders`, like the default (2026-11-05).

   **Amendment (2026-10-04): the ERP dashboard's `/static/*` bypasses
   `CheckAuth`.** The Streamlit index.html preloads about 110 content-hashed
   files, and behind the guarded default each runs `CheckAuth` and
   `HttpHeaders`. On a fresh Workshop Studio account (2026-10-03) the first
   load after the Cognito redirect stayed blank (title "Streamlit", two
   console errors) and a reload rendered it, which fits part of that
   Lambda@Edge burst failing while every replica is cold, with the reload
   succeeding off browser-cached chunks. Not verified live. `/static/*` is
   the public Streamlit frontend bundle and carries no application data
   (data flows over the still-gated `/_stcore/stream` websocket and the gated
   document), so it is an ungated, CloudFront-cached, GET/HEAD-only behavior
   with the managed security-headers policy
   (`_edge_auth.py::public_bundle_behavior`). Pinned by
   `tests/test_erp_dashboard_static_bundle.py`.
4. **There is no second application login.** `AuthMiddleware` accepts the
   edge-issued `CognitoIdentityServiceProvider.<edge-client-id>.*` cookies only
   after validating the JWT against the edge client id. A valid edge session
   requesting `/login.html` is redirected to `/`. Application logout expires
   only the app's own session cookies and returns `redirect:/signout`. The
   edge cookies stay in place, so the SAR SignOut handler at `/signout` gets
   the id token, calls Cognito `/logout` and expires every edge cookie at
   its own path. (Until 2026-11-05 the API also expired the edge cookies, so
   `/signout` arrived without them and never reached Cognito `/logout`.)
   The SignOut handler passes `https://<host>/` (the SAR's
   `RedirectPathSignOut` default) as Cognito's `logout_uri`, so the callback
   updater registers `/` as well as `/signout` as a sign-out URL for every
   hostname (`EDGE_LOGOUT_PATHS`, pinned by
   `tests/test_edge_callback_updater.py`).

   Sign-out returning to `/` is a deliberate trade-off. After Cognito
   `/logout` the browser lands on `/`, CheckAuth starts sign-in again, and
   where the identity provider still has a session (for example `oidc_only`
   behind a corporate SSO) the user is signed straight back in; Cognito
   `/logout` does not end the upstream IdP session. We do not add an
   anonymous signed-out landing page, because it would itself be an
   unauthenticated HTML 200 page, the class of page this amendment removes.
   A deployment that needs a stable signed-out state needs either an
   upstream IdP logout step or an accepted anonymous landing path (a SAR
   `RedirectPathSignOut` outside CheckAuth, registered as a logout URL).
5. **The ADT app reuses one pool and one domain.** Authorization@Edge uses the
   existing `adt-app` user pool owned by `ADTAgentCoreStack` and that pool's
   existing hosted-UI domain. A Cognito user pool supports exactly one domain;
   constructing a duplicate caused the first deployment to fail CloudFormation
   `ResourceExistenceCheck`. HighByte instead owns its separate
   `highbyte-ui-users` pool and hosted-UI domain.

   **Amendment (2026-09-23) — the edge OAuth client and the optional external
   IdP also live in `ADTAgentCoreStack`.** As originally accepted, the edge
   client (`AdtEdgeAuthClient`) and the external IdP (`AdtEdgeExternalIdp`,
   ADR-0072) were created in `ADTComputeStack` next to the distribution.
   Verified live 2026-09-23: with edge auth on, the browser's tokens are
   minted by the EDGE client, and the app forwards the signed-in user's
   access token as the gateway bearer (ADR-0049,
   `orchestrator.py::_resolve_gateway_bearer`) — but the query and onboarding
   gateways' `CUSTOM_JWT` authorizers listed only `adt-app-client` and
   `adt-app-service`, so every interactive query from an edge-authenticated
   user was rejected 403 at the gateway, MCP client init failed, and the UI
   showed "Query failed". (The CLI smoke/eval lanes never caught it because
   they sign in via `POST /api/auth/login` — an `adt-app-client` token.) The
   fix requires the gateway authorizers to allow the edge client, and the
   client id could not be referenced from `ADTComputeStack`, which is
   DOWNSTREAM of `ADTAgentCoreStack` (a backward cross-stack reference — the
   same cycle that relocated the pool itself, F1 / ADR-0033). Both resources
   therefore moved to `ADTAgentCoreStack` (still constructed exclusively via
   the `stacks/_edge_auth.py` facade, decision 7), both gateways append the
   edge client to `AllowedClients` when edge auth is on (off stays
   byte-identical), and `ADTComputeStack` reads the `edge_auth_client` /
   `edge_external_idp` handles for the SAR app, the callback updater and the
   `EDGE_AUTH_CLIENT_ID` task env. Pinned by
   `tests/test_edge_external_oidc.py` (gateway allowlist + relocation
   tripwire). NOTE: the move REPLACES the live client (new client id), so
   signed-in users re-authenticate once after the deploy that takes it.
6. **All browser distributions use the same edge pattern.** The ERP dashboard
   and HighByte UI use Authorization@Edge. HighByte also carries the standard
   AWS WAF managed rule groups (Common, KnownBadInputs, and IpReputation); its
   dedicated `/` behavior redirects to `/ui/` before the application UI.
7. **Edge-auth construction has one facade.** `stacks/_edge_auth.py` is the ONE
   home for the SAR application, imported edge-function versions, auth
   behaviors, standard WAF, callback updater, and demo-user provider. Individual
   stacks consume that facade and do not construct these resources inline. The
   callback handler's source remains in `stacks/_edge_callback_updater.py`.
   Construct ids are stable contracts: each id maps to the live resource it
   names, including the `LambdaEdgeAuth` nested stack and callback updater.
8. **Callbacks cover every served hostname.** The callback updater registers
   both the raw CloudFront domain and any CDK-managed custom-domain alias for
   callback and logout URLs. The ADT, ERP, and HighByte aliases are provisioned
   in CDK rather than as out-of-band console changes.

## Consequences

- The Cognito hosted UI is the normal browser entry point. `/login.html`
  remains only for edge-off, workshop, and local flows, and the e2e lane selects
  the expected behavior with `ADT_EDGE_AUTH=on|off` (default on).
- The CLI lanes are unchanged because their `/api/*` calls bypass the edge gate
  and retain application-level authentication and 401 behavior.
- Lambda@Edge functions are replicated globally. CloudFormation cannot delete
  their versions until CloudFront replicas disappear, which can take hours.
  `cleanup.sh` handles `DELETE_FAILED` stacks generically by retaining the
  `LambdaEdgeAuth` nested resource; the orphan nested stack can be removed after
  replication clears.
- Security headers are supplied by the edge `HttpHeaders` function. Content
  Security Policy remains report-only while the ADT and HighByte browser asset,
  inline-script, and WebSocket requirements are validated; tightening it is a
  separate measured change.
- The one-pool constraint and stable construct ids are deployment contracts,
  not naming preferences. Duplicating a Cognito domain or renaming an id without
  migration can break an otherwise valid update.

## Alternatives rejected

- **IP allowlist rules in AWS WAF.** Useful as a temporary exposure stopgap,
  but not an authentication boundary, and unsuitable for a redistributable
  sample whose users do not share fixed source addresses.
- **An organization-internal single sign-on provider in front of the app.**
  Works inside one organization but is not a repository-shippable
  authentication mechanism for this public AWS sample. (An external OIDC
  provider can still be federated into Cognito; see ADR-0072.)
- **Keeping the application-served login form.** It leaves password handling
  inside the application and makes the app the one browser surface without
  an edge boundary, while the ERP dashboard and HighByte UI already have one.
