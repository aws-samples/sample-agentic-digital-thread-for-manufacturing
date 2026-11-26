<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0072: Optional external OIDC identity provider for the hosted-UI sign-in

## Status

Accepted — 2026-09-22. Synth-verified (credential-blind, on- and off-path);
not yet exercised against a live identity provider.
Related: [ADR-0070](0070-cognito-authorization-at-edge-default.md) (Cognito
Authorization@Edge is the browser entry) and
[ADR-0049](0049-user-identity-propagation-app-signed-claim.md) (per-system
group enforcement fails closed).

## Context

ADR-0070 made the Amazon Cognito hosted UI the browser entry for every
human-facing distribution (the ADT app, the ERP dashboard, the HighByte UI).
Each surface authenticates against a Cognito user pool whose users are created
by the sample itself (the demo user) or by the operator.

Organisations evaluating the sample usually already run a corporate
single-sign-on system and want their people to sign in with it rather than
receive a second set of credentials. Cognito supports this natively: a user
pool can list one or more OpenID Connect identity providers, and the hosted
UI then offers each of them as a sign-in button alongside the local
directory. The question was how to expose that capability without
compromising three properties the repository already holds:

1. **Vendor neutrality.** This is a public sample. Nothing in code, tests,
   docs, or history may name or assume a specific corporate identity system;
   the configuration surface must be the generic OIDC trio any provider
   exposes.
2. **Byte-identical templates when the option is unused**, including the
   account-agnostic Workshop Studio packaging synth, so existing deployments
   and the packaged workshop are untouched by the feature's presence.
3. **One home for Authorization@Edge plumbing** (`stacks/_edge_auth.py`,
   ADR-0070 decision 7), so the three edge clients cannot drift apart.

## Decision

1. **The configuration is a sticky context trio plus an optional name.**
   CDK context `oidc_issuer_url`, `oidc_client_id`, `oidc_client_secret_arn`
   and `oidc_provider_name` (default `CorporateIdP`) are resolved in `app.py`
   exactly like the custom-domain trio: explicit `-c` value, then the sticky
   SSM parameters `/adt/oidc-{issuer-url,client-id,client-secret-arn,
   provider-name}` that `deploy.sh --oidc-issuer/--oidc-client-id/
   --oidc-secret-arn/--oidc-name` persist, then absent. Partial configuration
   **fails loud** at synth (`resolve_external_oidc`, a pure function) — a
   half-set trio silently dropping the corporate sign-in button from a live
   environment is the failure mode this prevents. Absent configuration adds
   nothing to any template.
2. **One home builds the provider and the client's provider list.**
   `stacks/_edge_auth.py::create_external_oidc_provider` constructs the
   `UserPoolIdentityProviderOidc` (scopes `openid email`, attribute-request
   method `GET`, `email`/`given_name`/`family_name` mapped to the matching
   Cognito attributes), and `create_edge_oauth_client` lists it after
   `COGNITO` and adds a construct dependency on it — Cognito rejects a client
   that names a provider that does not exist yet. The one-home tripwire test
   forbids `UserPoolIdentityProviderOidc(`, `UserPoolClientIdentityProvider.custom(`
   and `supported_identity_providers=` in any other stack file. Every pool
   that carries an edge client (ADT app on `ADTComputeStack`, ERP dashboard,
   HighByte UI) receives exactly one provider when the trio is set.
3. **The client secret is a CloudFormation dynamic reference, never a
   literal.** The provider's `client_secret` is
   `{{resolve:secretsmanager:<arn>:SecretString:::}}`, rendered from
   `SecretValue.secrets_manager(arn).unsafe_unwrap()` — the "unsafe" call
   only emits the placeholder that CloudFormation resolves at deploy time.
   The template, the deploy arguments and the logs never carry the secret;
   the operator stores it in AWS Secrets Manager first and rotates it there.
4. **Edge OAuth client token lifetimes are tightened unconditionally.** All
   three edge clients issue a 60-minute access token, a 60-minute id token
   and a 10-hour refresh token, with or without an external provider. The
   access and id lifetimes are pinned EQUAL: the edge `CheckAuth` handler
   refreshes on id-token expiry only, while the origin validates the access
   token, so an access token shorter than the id token opens a window where
   the edge admits the browser but the origin rejects every API call. The
   Lambda@Edge `RefreshAuth` handler renews both short tokens silently, so
   the user-visible effect is one sign-in per working day. The callback
   updater now also carries `TokenValidityUnits` through its full-replace
   `UpdateUserPoolClient` call; without it the API would reinterpret a
   60-minute id token as 60 hours and reject the update.

   **Amendment (2026-09-23) — access-token validity pinned equal to the id
   token.** As originally accepted, this decision set a 10-minute access
   token beside the 60-minute id token, on the premise that "`RefreshAuth`
   renews the short tokens silently, so short access/id lifetimes cost the
   user nothing." That premise is false for the access token: the SAR
   cloudfront-authorization-at-edge `CheckAuth` handler (v2.3.2,
   `src/lambda-edge/check-auth/index.ts`) verifies ONLY the id token and
   redirects to `/refreshauth` ONLY when the id token has expired — it never
   inspects the access token. The origin, meanwhile, validates the ACCESS
   token (`agents/auth/middleware.py::_validate_edge_session` ->
   `cognito.py::validate_token`, `token_use=access`). From minute 10 to
   minute 60 after sign-in the edge therefore admitted the browser while the
   origin 401'd every `/api/*` call, and the SPA's 401 handler sent the
   browser to `/login.html` — the local username/password form, a dead end
   for external-IdP-only (`oidc_only`) users; this symptom is what surfaced the
   gap. The access-token lifetime is now pinned equal to the id-token
   lifetime (60 minutes) in `stacks/_edge_auth.py`, tripwired by
   `tests/test_edge_token_lifetimes.py` and the synth assertions in
   `tests/test_edge_external_oidc.py`. The refresh token stays at 10 hours.
5. **Local Cognito sign-in stays by default; `oidc_only` makes the external
   provider the sole sign-in.** The provider is *added* to the hosted UI, not
   substituted for the local directory: customers who want only the built-in
   users never configure the option, and the demo user keeps working when
   they do. A deployment whose users all come from the corporate provider
   can set the sticky boolean `oidc_only` (context `oidc_only`, SSM
   `/adt/oidc-only`, `deploy.sh --oidc-only on|off`). It removes `COGNITO`
   from every edge client so the provider is the client's only identity
   provider; Cognito then skips the hosted-UI chooser and `/oauth2/authorize`
   redirects straight to the provider, so an unauthenticated request never
   reaches a local-password form — the redirect chain ends at the corporate
   sign-in. `oidc_only` without the trio fails loud (a
   provider-less client would lock every user out), a non-boolean value fails
   loud, and `create_edge_oauth_client` guards the same invariant at the
   construct level. Nothing else in the template changes (pinned by the
   only-path synth test).
6. **Redirect URIs are derivable before the first deploy.** Because the
   hosted-UI domain prefixes are deterministic per account
   (`stacks/_cognito_domain.py`: `adt-app-<last8>`,
   `erp-dashboard-<last12>`, `highbyte-ui-<last8>`), `docs/deployment.md`
   documents how to compute `https://<domain>.auth.<region>.amazoncognito.com/oauth2/idpresponse`
   for each pool from the account id, so the provider-side client can be
   registered up front.
7. **The sample carries a tripwire against internal identifiers.**
   `tests/test_no_internal_identity_strings.py` scans every git-tracked text
   file for a short, case-insensitive list of corporate-SSO tokens and
   internal hostnames (needles assembled at runtime so the test cannot trip
   itself). It passes on `main` today and keeps the option vendor-neutral
   going forward.

## Consequences

- **Usernames are `<ProviderName>_<subject>`.** Cognito creates a linked pool
  user per external person, named from the provider and the provider's stable
  `sub`. That is the `LastAuthUser` cookie the edge writes and therefore the
  app's session username; each such user has its own Cognito `sub`, so
  AgentCore conversation memory remains per person. `agents/auth/edge_session.py`
  passes the name through untouched (pinned in `test_edge_session.py`).
- **No `cognito:groups` by default — enforcement fails closed.** Externally
  provisioned users belong to no pool groups. They sign in as plain users (no
  Admin/integrator role), and a system runtime with `SYSTEM_REQUIRED_GROUP`
  set denies them until an operator adds the linked user to the group. This
  is the ADR-0049 fail-closed behaviour, not a gap; granting roles is an
  explicit administrative act.
- **Attribute claims are best-effort.** The mapping requests `email`,
  `given_name` and `family_name`; a provider that omits them leaves the
  Cognito attributes empty. Nothing in the app requires them — the existing
  email fallback (`admin_get_user`) already tolerates a missing address.
- **Construct ids are new migration contracts.** `AdtEdgeExternalIdp`
  (ADTAgentCoreStack — relocated from ADTComputeStack alongside the edge
  client on 2026-09-23, ADR-0070 amendment), `DashboardExternalIdp` (ERP pool
  scope) and
  `HighByteUiExternalIdp` (HighByte pool scope) map to live resources once
  deployed; renaming one replaces the provider and breaks linked users.
- **Removing the option is a redeploy, not a console change.** Deleting the
  four SSM parameters and redeploying removes the provider and the second
  button; linked pool users remain as inert records until deleted.
- **`oidc_only` disables the browser path for local users.** With the
  external provider as the sole client provider, the `adt-demo-password`
  users cannot sign in through the edge gate on that surface; `/api/*` keeps
  its own bearer/basic auth, so the CLI eval and smoke lanes are unaffected.
  A deployment that must serve both audiences keeps the default two-button
  mode or runs one surface per audience.
- **Not live-verified.** Synth output is pinned by tests; the end-to-end
  hosted-UI round trip against a real provider is the first thing to check
  on a deployment that enables the option.

## Alternatives rejected

- **Replacing the local directory with the external provider
  unconditionally.** Would remove the demo user and the browser login for
  every customer who enables the option; the additive button keeps both, and
  `oidc_only` makes the substitution an explicit, sticky, per-deployment
  choice instead.
- **Passing the client secret as a plain context value.** It would land in
  the synthesized template, `cdk.out`, and the deploy log. The dynamic
  reference keeps the value inside Secrets Manager.
- **Configuring the provider on the ADT app pool only.** The ERP dashboard
  and HighByte UI pools would then present a different sign-in experience;
  one home applying the same provider to every edge pool keeps the surfaces
  consistent and the change reviewable in one place.
- **A per-surface toggle.** No use case was identified for showing the
  corporate button on one hosted UI but not another; a single trio is
  simpler to reason about and to remove.
- **Requesting the `profile` scope.** The app uses only the address for
  greeting and the two name attributes for display; requesting `profile`
  would ask providers for more than the sample consumes.
