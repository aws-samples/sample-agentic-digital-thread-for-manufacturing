<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0 -->

# ADR-0096: System agents read demo databases with their own read-only login

## Status

**Proposed: 2026-10-12.** Verified offline and on a local PostgreSQL 16 server.
Not yet deployed to an AWS account.

## Context

A system agent answers a question by running SQL its model wrote
(`agents/system_agent.py`, Mode A `query_<schema>` and Mode B `run_query`). For
the databases ADT provisions (the demo seed RDS instance and the ERP Aurora
cluster), the agent connected as the database owner (`postgres` on the seed
database, `erp_admin` on the ERP database). A read-only transaction, a
statement timeout and `search_path` were the only limits. `search_path` is
name resolution, not authorization: a schema-qualified query could read every
other system's schema, and a `SELECT` could call functions whose effect lands
outside the transaction, for example `SELECT pg_terminate_backend(<pid>)`.

The ERP dashboard's SQL console already runs operator SQL as its own
SELECT-only login (`erp_simulation/console_login.py`).

## Decision

1. **One read-only login per schema.** Each schema of a demo database gets a
   login `adt_reader_<schema>`: LOGIN, no superuser, createdb, createrole,
   replication or bypassrls, no role membership, `default_transaction_read_only`
   on, a statement timeout, CONNECT on the database, USAGE on its schema,
   SELECT on every table and view in it, and default privileges so tables the
   owner creates there later stay readable. Nothing on any other schema. Per
   schema rather than per database, so a system agent cannot read another
   system on the same server. Cost: 14 generated AWS Secrets Manager secrets
   (9 seed schemas, 5 ERP view schemas), one setup function, layer and custom
   resource per database, one IAM statement on the app task role listing the
   reader secrets.
2. **A deploy-time custom resource keeps the logins** (`lambda-db-reader`,
   `stacks/_db_readers.py`). It runs with the owner credential, after the
   database's own seed or initialisation, and again whenever that re-runs (a
   reset recreates schemas and tables, which drops their grants). Every run
   resets the password from the reader's secret, revokes anything else the role
   holds and verifies the exact result. The password reaches the server as a
   SCRAM verifier only. A reset outside CloudFormation re-runs it too: the ERP
   dashboard's Reset All (or a hand invoke) runs the ERP db-init full reset
   directly, which drops and recreates `erp_data`, the three legacy site
   schemas and every public table. After that reset
   db-init runs what CloudFormation runs after its own, in the same order
   (`lambda-db-init/handler.py` `run_direct_reset_steps`,
   `stacks/_direct_reset.py`): this setup
   with the Update event CloudFormation sends it (the deployed properties as
   new and old), then the steps each opt-in stack registered in its own SSM
   parameter (for example the live-story history setup, the same kind of
   event). Each step is
   one invoke of the function that already does the work, with
   `lambda:InvokeFunction` on those functions only; db-init grants nothing
   itself. Every step is read and checked before the reset drops anything:
   when such a stack is expected, a missing registry parameter (that stack
   not deployed yet) refuses the reset. The steps run after the reset has
   committed and inside db-init's own timeout, so each waits its function's
   timeout plus a margin, cut to what db-init has left, and a step that
   cannot be given enough time is not started. A step that fails or is not
   started fails the reset, naming every step not restored, so the dashboard
   reports Reset All as failed; a second Reset All restores them. No other
   direct path drops a schema: the seed database's
   reseed, the cleanup Lambda's TRUNCATE, the story-history actions and the
   view migration keep every grant.
3. **The app hands a system agent its schema's login, never the owner's**
   (`agents/demo/db_routing.py` `system_agent_pg_config`, called from
   `agent_manager.system_connection_config` on provision, restore and admin
   re-sync). The login reaches the runtime the way the owner login did, in the
   system's `adt/agent-config/<id>` secret, so the runtime env contract is
   unchanged. A demo schema with no reader configured is refused, not given the
   owner login. A customer-registered connection keeps the customer's login.
4. **No runtime can read a database owner secret.** The shared orchestrator and
   system runtime role loses `AdtDbSecret*` and `DatabaseSecret*`; reader
   secrets are named so no runtime wildcard matches them. A PostgreSQL runtime
   makes no secret read at start beyond its agent-config secret.
5. **Defense in depth in the SQL guard.** `sql_safety.ensure_select_only` now
   runs before Mode A executes (Mode B and Athena already ran it) and refuses
   calls to functions with an effect outside the query: ending or cancelling
   sessions, reloading or rotating server state, reading server files, large
   object server I/O, `dblink`, advisory locks, `set_config`, functions that run
   SQL held in a text value, and `pg_sleep` (no analytical use; it holds a
   connection until the timeout and is the usual timing probe). The login is
   the boundary; the guard is a second check.
6. **Preflight advises on customer logins.** Onboarding preflight adds a
   warning, never a block, when a user-registered PostgreSQL login is a
   superuser or can create or change data in the target schema.

## Consequences

- **One-time upgrade step for an existing deployment** (fresh deployments,
  workshop accounts and customer deploys need none). A stack update creates
  the logins but does not touch running system runtimes: each still holds the
  owner login in its agent-config secret, and an app restart does not refresh
  it (the start path reuses a READY runtime), by design: changing runtimes at
  app start can outlast the ECS health-check grace. Move each onboarded demo
  PostgreSQL system with the admin connection Edit, changing nothing (Onboard >
  Edit connection > Save & Reconnect, or `PUT /api/admin/connection/<id>` with
  `{}`). A demo system's node stores its connection as fields (host, port,
  dbname, search_path, and the database secret it points at), with no
  connection string; an Edit that changes nothing there writes and probes
  nothing and only re-syncs. The re-sync writes the schema's reader login
  into the agent-config secret and deploys a new runtime version, which reads
  it at start; no separate restart. For a PostgreSQL system on the same host and database the
  re-sync now keeps the agent-config's generated `system_prompt`,
  `tables_desc` and `concept_mappings` (its whole-value rewrite used to drop
  them), so nothing is re-onboarded. `scripts/audit_runtime_env.py
  --reader-logins` reports, annotate-only, which systems still use the
  deployment's login. `scripts/reonboard.py` also moves a system but
  regenerates its generated rows. Steps: [docs/deployment.md](../deployment.md#upgrading-an-existing-deployment).
- The onboarding runtime and the app still read the owner secrets for their
  own engine-written queries (discovery, samples, dashboards). Only the system
  agents' model-written SQL moved to the readers.
- A new demo schema needs an entry in `SEED_READER_SCHEMAS` or
  `ERP_READER_SCHEMAS`; tests pin both lists to the schemas the seed and
  db-init create.
- Reader passwords are not rotated; a rotation would need the custom resource
  to re-run before the next provision.
- The reader resources add no cdk-nag error (the app synth runs cdk-nag by
  default) and no stock checkov failure: the setup function writes to the
  default Lambda log group (no log-retention custom resource), and the
  remaining findings (customer-managed key on the secrets, reserved
  concurrency, dead-letter queue, the provider framework function outside the
  VPC) are skipped with written reasons on each resource.
