<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0093: The workshop package can carry the AWS Quick connector path

## Status

**Proposed: 2026-10-02.** Implemented and verified offline (synth with the flags on and off, cfn-lint, the
Workshop Studio packaging and Quick stack tests; the packaging is not part of this sample). Not yet deployed: no Workshop Studio
event has run with `--quick`, and no attendee has created the connector.

Builds on [ADR-0071](0071-dedicated-gateway-for-quick-query-bridge.md) (the
bridge-only gateway `adt-quick-gw`) and
[ADR-0084](0084-one-erp-and-the-workshop-story-setting.md) (one story per
package, synth-time context only).

## Context

Two workshop modules have each attendee use AWS Quick in their own Workshop
Studio account: they create an MCP connector to the digital thread with
Service authentication (OAuth 2.0 client credentials), create a chat agent and
ask the plant manager's question. The Quick connector fans out over every tool a
gateway exposes, so it must point at the bridge-only gateway (ADR-0071).

Until now the only way to get that gateway was `quick_api_enabled`
(`deploy.sh --quick-api`), which also builds `QuickApiGatewayStack` (the
kiosk's SigV4 front door) and mints the bridge's `x-quick-auth` token there.
The workshop package carried none of it: no gateway for Quick, nothing an
attendee could paste, no client secret. `TestNoQuickStacksInWorkshopProfiles`
forbade Quick stacks in the workshop root.

Two more gaps showed up while reading the bridge path end to end, both
offline findings, not yet observed live:

- The internal ALB admits only the CloudFront VPC-origin security group. The
  bridge runtime calls the app at `ADT_APP_INTERNAL_URL` (the ALB) from the
  AgentCore runtime security group, so nothing let it in.
- The runtime-bootstrap CR's DELETE sweep removes targets on the shared and
  onboarding gateways only. A `query-bridge` target left on `adt-quick-gw`
  blocks CloudFormation from deleting that gateway.

## Decision

1. **A narrow, explicit flag, off by default.** `package.py --quick`
   (`--profile workshop` only) forwards `-c quick_connector_enabled=true` to
   the engine synth and `-c quick_connector=true` to the root synth. Not a
   live-story default: the path only works once each attendee account has an
   AWS Quick Enterprise subscription, which costs money and is outside the
   template, so the owner opts in per package. Off, every template is
   byte-identical (test-pinned).
2. **Connector-only engine wiring** (`stacks/_quick_connector.py`, additive
   post-construction like `stacks/_observability.py`):
   - app.py builds `adt-quick-gw` when either Quick flag is on;
   - the app task gets `QUICK_API_ENABLED` and `QUICK_GATEWAY_ID`/`URL` (the
     same values `quick_api_enabled` emits), so the app provisions the
     `query-bridge` target there at startup;
   - the `x-quick-auth` token is generated per account by Secrets Manager and
     injected at task start (no fixed name, outside the `adt-*` prefix the
     participant policy reads). Under `quick_api_enabled` the SSM token from
     `QuickApiGatewayStack` is kept, because that API injects the same value;
   - one ingress rule on the ALB security group from the AgentCore runtime
     security group on port 80;
   - `ADTAgentCoreStack` outputs `QuickTokenUrl` and `QuickClientSecretName`,
     and copies the `adt-app-service` client secret into the Secrets Manager
     secret `adt-quick-client-secret`.

   Not `QuickApiGatewayStack`, not the kiosk. Quick reuses the existing
   `adt-app-service` client (ADR-0071 decision 6); no new Cognito client.
3. **Root outputs** (the Workshop Studio packager):
   `QuickGatewayUrl`, `QuickTokenUrl`, `QuickClientId` (the
   `AppServiceClientId`) and `QuickClientSecretName`, re-surfaced from the
   AgentCore child because Workshop Studio shows only root outputs.
4. **The DELETE sweep covers the Quick gateway.** The root passes
   `QuickGatewayId` to the runtime-bootstrap CR; the handler adds it to the
   gateways it sweeps. The property is optional, so roots without `--quick`
   are unchanged.
5. **The subscription is a separate flag, off by default.**
   `--quick-subscription` (needs `--quick`) adds `Custom::ADTQuickSubscription`,
   an inline Lambda that calls `quicksight:CreateAccountSubscription` with
   `Edition=ENTERPRISE`, `AuthenticationMethod=IAM_AND_QUICKSIGHT`,
   `AccountName=adt-workshop-<account id>` and `NotificationEmail` from the
   root parameter `QuickNotificationEmail` (set it in contentspec.yaml). It
   adopts an existing subscription without ever deleting it, and on DELETE
   removes only a subscription it created (termination protection off, then
   `DeleteAccountSubscription`), always answering SUCCESS so teardown never
   wedges. Its IAM is the Quick user guide's example for creating an
   Enterprise account with Quick-managed users, plus
   `iam:CreateServiceLinkedRole` for the User Subscriptions role. Off by
   default because it bills each account; that decision belongs to the
   workshop owner.

   Amendment (2026-10-02): the same handler then registers the attendee's
   console session as a Quick user (`RegisterUser`, `IdentityType=IAM`, the
   role and session name from the root parameters `QuickParticipantRoleArn`
   and `QuickParticipantSessionName`, which contentspec.yaml fills with
   Workshop Studio's `{{.ParticipantRoleArn}}` and
   `{{.ParticipantAssumedRoleSessionName}}`), role `ADMIN_PRO`, email from
   `QuickNotificationEmail`. Self-provisioning only grants the BI-only
   `ADMIN` role, and the chat agents and MCP actions modules 1b and 7b use
   need a Pro role. An existing user counts as success, transient errors are
   retried, and an empty role ARN skips the step. Attendees use Quick in the
   browser: the Quick desktop app needs an administrator to connect an OIDC
   identity provider on an Enterprise account, and Workshop Studio accounts
   have none.

   Amendment (2026-10-21): DELETE no longer answers SUCCESS straight after
   `DeleteAccountSubscription`. Quick finishes the unsubscribe asynchronously
   on the caller's credentials, and CloudFormation deletes the handler role's
   policy as soon as the handler answers: on a stack delete on 2026-10-20 the
   call went out at 23:59:24Z, the policy was deleted at 23:59:31Z and Quick's
   `ds:DescribeDirectories` as the handler role was denied at 23:59:39Z,
   leaving Quick `UNSUBSCRIBE_FAILED`. The handler now polls
   `DescribeAccountSubscription` until the subscription is `UNSUBSCRIBED` or
   not found, inside the 15-minute Lambda timeout. On `UNSUBSCRIBE_FAILED` it
   turns termination protection off again and unsubscribes once more (an
   account had been found failed with protection back on). A second failure,
   a timeout or an API error answers FAILED: the function, role and policy
   are deleted only after the subscription resource, so a stack-delete retry
   runs the handler again with the permissions Quick needs, where SUCCESS
   would leave a billed subscription with no role to remove it on a reused
   account. Event accounts are discarded after the event, so a
   `DELETE_FAILED` there costs nothing; `--retain-resources QuickSubscription`
   skips it. The IAM is unchanged: the wait uses the same Describe call.

   Second amendment (2026-10-21): the handler moved out of the
   template into the Workshop Studio packager
   (it no longer fits the 4096-byte inline limit, and the BootstraplessSynthesizer
   root cannot carry CDK assets). `packaging_quick` zips it deterministically
   under a content-hashed name, `package.py` stages the zip in `dist/assets/`
   and records it in `asset-manifest.json` (`quick_subscription_archive`), the
   root loads it from the Workshop Studio assets bucket, and the structural
   check fails a package whose root names a zip it did not stage. Behaviour:
   - Create adopts only `ACCOUNT_CREATED`. Workshop Studio re-queues the same
     account after deleting a failed deploy, so Create waits out an
     unsubscribe in progress and repairs `UNSUBSCRIBE_FAILED` (termination
     protection off, unsubscribe) before subscribing, answering FAILED if it
     does not clear. A sign-up already in progress is waited out and re-read
     (adopted if it finishes, replaced if it does not).
   - The physical id becomes `created` as soon as `CreateAccountSubscription`
     returns, or fails without an answer (for example a read timeout), so a
     rollback Delete removes it; a call Quick refused is never claimed.
   - Every Quick call is bounded (5 s connect, 20 s read, two attempts; Create
     one, resent only after a throttle, see the 2026-11-23 amendment) and made only with enough Lambda time left for the slowest
     such call plus the response PUT, which has a 10 s timeout, one retry and
     never raises. The custom resource's ServiceTimeout is 960 s (the default
     is 3600 s) and Lambda's asynchronous retries are off.
   - ResourceNotFound from `UpdateAccountSettings` counts as the subscription
     being gone.
   - Status strings: the API reference documents `AccountSubscriptionStatus`
     only as a string. `ACCOUNT_CREATED`, `UNSUBSCRIBE_FAILED` and
     `UNSUBSCRIBED` were observed on this project's accounts;
     `SIGNUP_ATTEMPT_IN_PROGRESS` is reported on AWS re:Post;
     `UNSUBSCRIBE_IN_PROGRESS` is assumed. Any other status (or none) fails
     closed: it is never adopted, created over or deleted, and the answer is
     FAILED. After the handler's own unsubscribe call an unknown status is
     waited on, and that wait ends FAILED, never SUCCESS.
   The IAM is unchanged: the handler makes the same five Quick calls.

   Third amendment (2026-10-21): ownership comes from the subscription's own
   name, which replaces the second amendment's rule that a Create with no
   answer claims the subscription and that every active subscription is
   adopted.
   - The root names the subscription `adt-workshop-<account id>` (the
     resource's AccountName property). The Quick API reference says that name
     is unique over all of AWS and cannot be changed once the account exists
     (CreateAccountSubscription), and DescribeAccountSubscription returns it
     (AccountInfo.AccountName). A subscription with that name is this
     workshop's: Create claims it (physical id `created`) whether it found it
     active from an earlier deploy on a re-queued account, created it now, or
     a duplicate or retried Create created it. Any other name is adopted and
     never deleted.
   - A Create call that fails with no answer, with a server error (any 5xx),
     or is refused as existing or in conflict (a duplicate delivery, or a
     retry after a lost success), may
     have created a subscription Quick doesn't show yet. Quick documents no
     visibility delay, so the handler re-reads the account for as long as the
     Lambda budget allows and claims only on a name match; when time runs
     out it answers FAILED with nothing claimed. Any other refusal created
     nothing and gets one read.
   - The name is reserved: a subscription a person creates by hand as
     `adt-workshop-<account id>` is treated as this workshop's and deleted
     with the stack. On an event account that can't happen before
     provisioning. On a reused account, `rehearse-event.sh deploy`
     says so in its Quick preflight line when it finds that name.
   - Delete re-reads the account before any destructive call. Another name
     is left in place (SUCCESS); a missing name, or a missing AccountName
     property, answers FAILED and deletes nothing. Waiting for an unsubscribe
     Quick is already running deletes nothing and needs no name.
   - botocore's `max_attempts` counts retries after the first request, so the
     clients now set `total_max_attempts` (2, and 1 for
     CreateAccountSubscription), and the time reserve is derived from those
     totals.
   - The handler's EventInvokeConfig caps a queued event's age at 60 s,
     Lambda's minimum (the default is six hours), so an event cannot start
     after CloudFormation's 960 s ServiceTimeout (60 s + the 900 s Lambda
     timeout). What remains: Lambda may still deliver one event twice. That
     is safe because both runs read the same name from Quick and reach the
     same claim; a second Delete finds the subscription gone.

   Amendment (2026-10-22): the attendee session is registered as
   `AUTHOR_PRO`, not `ADMIN_PRO`. The 2026-10-02 amendment chose `ADMIN_PRO`
   because self-provisioning only grants the BI-only roles and modules 1b and
   7b need a Pro role; Author Pro is also a Pro role. The Quick user guide
   ("AWS Quick user types") maps Author Pro to the Quick Enterprise
   subscription and says Author Pro users create content using natural
   language, build knowledge bases and configure actions, which covers the
   MCP action connector, the chat agent and its space. Managing users,
   billing, subscriptions and SPICE needs Admin Pro, and those are controls
   an attendee must not hold: Quick authorizes some of them from the Quick
   user role, so the participant IAM policy's Deny statements alone do not
   remove them. The role is the custom resource's literal property
   `ParticipantUserRole`, so changing it is a property change CloudFormation
   sends as an Update. On Create, a session `RegisterUser` reports as already
   registered (an adopted subscription, or one an earlier package registered
   as `ADMIN_PRO`) is read with `DescribeUser` (user name
   `<role name>/<session name>`) and, when its role differs, moved to
   `ParticipantUserRole` with `UpdateUser` and its own email. Update does the
   same while the subscription is active and touches nothing else; Delete is
   unchanged. The handler role gains `quicksight:DescribeUser` and
   `quicksight:UpdateUser` on the same default-namespace user scope as
   `RegisterUser`.

   Amendment (2026-11-23): a throttled Create is sent again. With 51 seat
   accounts provisioning together, 13 got `ThrottlingException` (HTTP 429)
   from `CreateAccountSubscription` within 3 seconds although each account
   sends it once, so the limit is shared across accounts, and the handler's
   FAILED answer failed those seats. A throttle is a 4xx refusal that created
   nothing, so the handler (not botocore) now resends it: at most 8 sends,
   with full-jitter waits drawn from `[0, min(30, 2 * 2**n)]` s (at most 120 s
   in all), each checked against the same Lambda reserve, so the answer still
   reaches CloudFormation in time; a wait the reserve can't cover ends the
   resends and the throttle is the answer. botocore still sends each call
   once (`total_max_attempts` 1 for Create), and every other error keeps the
   send-once, name-based reconcile above: a timeout or 5xx may have created
   the subscription, so resending it would be refused as existing. botocore's
   standard or adaptive retry modes were not used: they retry timeouts and
   5xx too, their backoff is short and fixed, and an adaptive rate limiter
   lives in one process, so it can't spread 51 accounts.
6. **The package checks itself.** `asset-manifest.json` records
   `quick_connector` and `quick_subscription`; `packaging_quick.check`
   compares them with the root outputs, the CR property, the subscription CR,
   the AgentCore child's gateways and outputs and the compute child's env, and
   fails the package on any mismatch or on a staged Quick stack.

## Consequences

- `TestNoQuickStacksInWorkshopProfiles` now states the rule as: no Quick
  stack ever; flag off, no Quick resource in the root; flag on, only the set
  above. The packaging gate gains a `live-quick` packaging.
- Attendee steps that stay manual: subscribe the account to AWS Quick
  Enterprise (unless `--quick-subscription`), open Quick and finish user
  setup (with `--quick-subscription` and the participant parameters set, the
  user is already registered), create the MCP connector and the chat agent. The connector cannot be
  pre-created (no MCP type in `AWS::QuickSight::ActionConnector`).
- The participant role needs Quick console permissions, which live in the
  workshop repo, and the root outputs must be listed in its contentspec
  `participantVisibleStackOutputs`.
- The bridge is provisioned by the app's startup thread. If that fails it
  only logs; there is no readiness check yet. A preflight check that
  `adt-quick-gw` has a READY `query-bridge` target would close that.
- The ALB ingress and the DELETE sweep fixes apply only under the connector
  flag. Whether the `--quick-api` bridge path has the same ALB gap was not
  checked live; it was left unchanged here.
- Note (2026-11-02): `deploy.sh --quick-connector` passes the same
  `-c quick_connector_enabled=true` for an in-place deploy outside Workshop
  Studio (not sticky). It closes the readiness gap above for that path only:
  deploy.sh waits for a READY `query-bridge` target and prints the connector
  values. It never subscribes the account to Quick; outside the workshop the
  operator subscribes in the Quick console. A deploy with neither Quick flag
  stops before CDK while `adt-quick-gw` lists a target, and `cleanup.sh`
  deletes that gateway's targets. Not yet run live.

## Alternatives rejected

- **Turn on `quick_api_enabled` in the workshop package.** Ships the kiosk's
  API Gateway and VPC link into every attendee account for no use, and its
  per-synth token would be the same literal in every account.
- **Make Quick a live-story default.** Every live package would carry a
  gateway and bridge that only work after a paid subscription, including
  events that skip the Quick modules.
- **Subscribe Quick by default.** Bills every attendee account; the owner
  must choose it.

## Amendment 2026-11-07: the connector secret is Quick's own client

Decisions 2 and 3 copied the `adt-app-service` client secret into
`adt-quick-client-secret` and surfaced `AppServiceClientId` as the root
output `QuickClientId`. The attendee-readable secret therefore worked on
`adt-query-gw` and `adt-onboard-gw` as well, which contradicts module 1b's
"Quick never gets your raw systems". Now the secret is the secret of Quick's
own client `adt-quick-service`, which only `adt-quick-gw` accepts, and the
root output `QuickClientId` re-surfaces the new `ADTAgentCoreStack` output
`QuickClientId`
([ADR-0071 amendment 2026-11-07](0071-dedicated-gateway-for-quick-query-bridge.md)).
The root output names, the secret name and the participant policy are
unchanged; a repackage picks the change up.
